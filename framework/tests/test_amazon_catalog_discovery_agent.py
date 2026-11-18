"""agents/amazon-catalog-discovery-agent (build item F12).

Covers the config contract, the lane classifier (both worked examples), the
engine's own paging (the short-page / itemPage-10 / totalResultCount fixes the
shared loop lacks), the run budget, ASIN-only persistence, seed import, the
golden-label precision metrics and the AgentBase run. Offline: the Creators
client and the catalog connection are fakes, framework storage is the tmp
LocalFilesystemStorage from conftest, no LLM and no email.

An optional real-Postgres check runs when CATALOG_DISCOVERY_TEST_PG_DSN points
at a throwaway database.
"""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util as iu
import json
import os
import re
import sys
import uuid
from collections import Counter
from dataclasses import replace as dataclasses_replace
from pathlib import Path
from unittest import mock

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = _ROOT / "agents" / "amazon-catalog-discovery-agent"
FIXTURES = AGENT_DIR / "fixtures"
EXAMPLES = AGENT_DIR / "examples"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from catalog_discovery import catalog as K  # noqa: E402
from catalog_discovery import config as C  # noqa: E402
from catalog_discovery import engine as E  # noqa: E402
from catalog_discovery import lanes as L  # noqa: E402
from catalog_discovery import rotation as R  # noqa: E402
from catalog_discovery import seeds as S  # noqa: E402
from framework.core import amazon_creators as ac  # noqa: E402
from framework.core import amazon_discovery as ad  # noqa: E402
from framework.core.amazon_creators import CreatorsRequestError, CreatorsUnavailable  # noqa: E402

_spec = iu.spec_from_file_location("amazon_catalog_discovery_agent_under_test",
                                   AGENT_DIR / "agent.py")
agent_mod = iu.module_from_spec(_spec)
_spec.loader.exec_module(agent_mod)

NOW = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.timezone.utc)
RUN_TS = "20261117T120000Z"
KNOWN_SQL = "SELECT asin FROM products WHERE asin = ANY(%(asins)s)"
INSERT_SQL = ("INSERT INTO products (asin, kind, category_confidence, discovered_via) "
              "VALUES (%(asin)s, %(lane_kind)s, %(confidence)s, %(via)s) ON CONFLICT DO NOTHING")
# A token in every synthetic title and a price no other value shares: neither
# may ever reach the catalog, the decision log, the state or the run report.
TITLE_TOKEN = "Zorblax"
PRICE_TOKEN = 123.45


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def raw_item(asin, title, *, price=PRICE_TOKEN, currency="USD", condition="New",
             avail="IN_STOCK", nodes=()):
    """A searchItems item in the Creators response shape (see
    test_amazon_creators_search_budget.py), with a synthetic title."""
    listing = {"price": {"money": {"amount": price, "currency": currency}},
               "condition": {"value": condition}, "isBuyBoxWinner": True,
               "availability": {"type": avail, "message": "In Stock"},
               "merchantInfo": {"name": "Amazon.com"}}
    return {"asin": asin,
            "detailPageURL": f"https://www.amazon.com/dp/{asin}?tag=t-20",
            "images": {"primary": {"large": {"url": f"https://m.media-amazon.com/images/I/{asin}.jpg"}}},
            "itemInfo": {"title": {"displayValue": title},
                         "byLineInfo": {"brand": {"displayValue": "Acmecorp"}}},
            "offersV2": {"listings": [listing] if price is not None else []},
            "browseNodeInfo": {"browseNodes": [{"id": n, "displayName": "Node"} for n in nodes]}}


def page(prefix, n, *, word="widget", start=0, nodes=(), price=PRICE_TOKEN):
    """n in-lane items with ASINs B0<prefix:4><nnnn>."""
    assert len(prefix) == 4
    return [raw_item(f"B0{prefix}{start + i:04d}", f"{TITLE_TOKEN} {word} model {start + i}",
                     nodes=nodes, price=price) for i in range(n)]


class FakeCreators:
    """search_items / budget / throttle double. ``pages`` maps (query, page)
    to raw items, or is a callable (query, page, filters) -> items.
    ``error_on`` maps (query, page) -> HTTP status (a set means 400 each)."""

    def __init__(self, pages=None, *, totals=None, budget=10_000, throttle_on=(),
                 error_on=(), unavailable_on=(), budget_fails_after=None):
        self.pages = pages or {}
        self.totals = totals or {}
        self.budget = budget
        self.throttle_on = set(throttle_on)
        self.error_on = dict(error_on) if isinstance(error_on, dict) else {k: 400 for k in error_on}
        self.unavailable_on = set(unavailable_on)
        self.budget_fails_after = budget_fails_after
        self.calls: list[dict] = []
        self.last_search_meta: dict = {}

    def remaining_budget(self, consumer=""):
        return max(0, self.budget - len(self.calls))

    def budget_ok(self, consumer="", calls=1):
        if self.budget_fails_after is not None and len(self.calls) >= self.budget_fails_after:
            return False
        return self.remaining_budget() >= calls

    def search_items(self, keywords, *, search_index="All", item_count=10, item_page=1, **filters):
        self.calls.append({"q": keywords, "page": item_page, "index": search_index, **filters})
        key = (keywords, item_page)
        self.last_search_meta = {"total_result_count": 0, "search_url": None, "errors": [],
                                 "throttled": False}
        if key in self.unavailable_on:
            raise CreatorsUnavailable("network down")
        if key in self.error_on:
            status = self.error_on[key]
            raise CreatorsRequestError(f"HTTP {status}", status=status,
                                       detail="InvalidParameterValue" if status < 500 else "Unavailable")
        if key in self.throttle_on:
            self.last_search_meta["throttled"] = True
            return []
        if keywords in self.totals:
            self.last_search_meta["total_result_count"] = self.totals[keywords]
        if callable(self.pages):
            return list(self.pages(keywords, item_page, filters))
        return list(self.pages.get(key, []))


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1
        self._rows: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.db.executed.append((sql, copy.deepcopy(params)))
        head = sql.strip().split()[0].upper()
        if head in ("SAVEPOINT", "RELEASE", "ROLLBACK"):
            return
        if "%(asins)s" in sql:
            have = set(self.db.rows) | set(self.db.pending)
            self._rows = [(a,) for a in params["asins"] if a in have]
            return
        if "%(asin)s" in sql:
            asin = params["asin"]
            if asin in self.db.fail_on:
                raise RuntimeError("new row violates check constraint")
            if asin in self.db.rows or asin in self.db.pending:
                self.rowcount = 0
            else:
                self.db.pending[asin] = dict(params)
                self.rowcount = 1
            return
        # a seed SELECT, run as the engine's keyset page (asin > after, by asin)
        assert "%(after)s" in sql and "%(limit)s" in sql, sql
        self.description = [(c,) for c in self.db.seed_columns]
        col = list(self.db.seed_columns).index("asin")
        rows = sorted((r for r in self.db.seed_rows if str(r[col]) > params["after"]),
                      key=lambda r: str(r[col]))
        self._rows = rows[:params["limit"]]

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, n):
        return list(self._rows[:n])


class FakeConn:
    def __init__(self, known=(), *, fail_on=(), seed_columns=("asin",), seed_rows=()):
        self.rows: dict = {a: {"asin": a} for a in known}
        self.pending: dict = {}
        self.fail_on = set(fail_on)
        self.seed_columns = tuple(seed_columns)
        self.seed_rows = list(seed_rows)
        self.executed: list = []
        self.commits = self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.rows.update(self.pending)
        self.pending = {}
        self.commits += 1

    def rollback(self):
        self.pending = {}
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def insert_params(self):
        return [p for s, p in self.executed if isinstance(p, dict) and "asin" in p and "%(asin)s" in s]


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------

def base_doc(**over):
    doc = {
        "schema_version": "1",
        "consumer": "test-discovery",
        "catalog": {"dsn_env": "TEST_CATALOG_DSN", "known_sql": KNOWN_SQL, "insert_sql": INSERT_SQL},
        "active_phases": ["now"],
        "reject_patterns": [{"id": "gas", "pattern": "\\bgas\\b"}],
        "lanes": [
            {"id": "widget", "phase": "now", "kind": "widget-kind", "category": "widgets",
             "include_any": ["\\bwidget\\b"], "browse_nodes": ["111"], "min_price": 10,
             "seeds": [{"id": "w1", "query": "widget"}]},
            {"id": "gadget", "phase": "now", "include_any": ["\\bgadget\\b"],
             "seeds": [{"id": "g1", "query": "gadget"}]},
            {"id": "gizmo", "phase": "later", "include_any": ["\\bgizmo\\b"], "seeds": ["gizmo"]},
        ],
    }
    for key, value in over.items():
        doc[key] = value
    return doc


def build(doc=None, tmp_path=None, **over):
    return C.build_config(doc or base_doc(**over), base_dir=tmp_path or FIXTURES, source="test")


def engine_for(cfg, *, now=NOW):
    return E.DiscoveryEngine(cfg, now=now, log=lambda m: None)


def search(cfg, client, conn, *, state=None, allowed=40, dry_run=False, now=NOW):
    eng = engine_for(cfg, now=now)
    rot = eng.rotation(state)
    cat = K.Catalog(conn, cfg.catalog, run_ts=RUN_TS, dry_run=dry_run)
    rep = eng.run_search(client, cat, rotation=rot, allowed_calls=allowed)
    return rep, rot


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_schema_is_valid_draft7():
    import jsonschema
    jsonschema.Draft7Validator.check_schema(json.loads(C.SCHEMA_PATH.read_text()))


@pytest.mark.parametrize("name,queries,active", [
    ("home-power.site.yaml", 37, {"kit", "expansion", "station", "panel", "cable"}),
    ("cleaning-robots.site.yaml", 10, {"consumable", "robot-vacuum", "robot-mop"}),
])
def test_worked_examples_validate(name, queries, active):
    cfg = C.load_config(EXAMPLES / name)
    assert {lane.id for lane in cfg.active_lanes} == active
    assert len(cfg.queries()) == queries
    assert cfg.limits.max_calls_per_run == 40 and cfg.limits.max_new_per_run == 300
    assert all(q.max_pages <= C.CREATORS_MAX_ITEM_PAGE for q in cfg.queries())


def test_home_power_example_carries_the_spec_values():
    cfg = C.load_config(EXAMPLES / "home-power.site.yaml")
    assert "348967011" in cfg.lane("station").browse_nodes
    assert "3236381" in cfg.lane("panel").browse_nodes
    # Year-1 rejects (spec + amendment 8) are GLOBAL, so no lane's precedence
    # can file a 240 V cord as a station before a cable exclude is tried.
    rule_ids = {r.id for r in cfg.reject_rules}
    assert {"generator-inlet", "transfer-switch", "backfeed", "double-male", "dryer-outlet",
            "grid-tie", "fuel-generator", "camera-solar", "doorbell",
            "cord-240v", "cord-l14-30", "cord-l5-30", "cord-14-50"} <= rule_ids
    assert not {r.id for r in cfg.lane("cable").exclude} & {"cord-240v", "cord-l14-30"}
    assert cfg.creators.result_count_cap == 306 and cfg.creators.max_item_page == 10
    assert cfg.creators.price_filter_unit == "cents"
    assert cfg.pricing.min_price == 1.0
    # The sizing run's search seeds: A01-A20 (A19, the L14-30 cord query, off)
    # plus family B; its price bands; its low-yield stop.
    seeds = {s.id for lane in cfg.lanes for s in lane.seeds}
    assert {f"A{i:02d}" for i in range(1, 21)} - {"A19"} <= seeds and "A19" not in seeds
    assert {f"B{i:02d}" for i in range(1, 13)} <= seeds
    a01 = [q for q in cfg.queries() if q.seed_id == "A01"]
    assert [q.partition.label for q in a01] == ["1-200", "200-700", "700+"]
    assert a01[0].filters("cents") == {"min_price": 100, "max_price": 20000}
    assert (cfg.limits.stop_if_new_in_lane_below, cfg.limits.low_yield_from_page) == (2, 2)
    # Capacity: dot-thousands on the Wh pattern only.
    spec = cfg.measures["capacity_wh"]
    assert [(p.scale, p.dot_thousands) for p in spec.patterns] == [(1.0, True), (1000.0, False)]


def test_config_block_inside_a_larger_site_yaml(tmp_path):
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump({"site_id": "x", "price_refresh": {"anything": 1},
                                 "catalog_discovery": base_doc()}))
    cfg = C.load_config(p)
    assert cfg.consumer == "test-discovery" and cfg.source == str(p)


def test_config_env_var_and_missing_path(tmp_path, monkeypatch):
    p = tmp_path / "d.json"
    p.write_text(json.dumps(base_doc()))
    monkeypatch.setenv(C.CONFIG_ENV, str(p))
    assert C.load_config().consumer == "test-discovery"
    monkeypatch.delenv(C.CONFIG_ENV)
    with pytest.raises(C.ConfigError, match="no config file"):
        C.load_config()


def _mutate(path, value):
    doc = base_doc()
    node = doc
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    last = parts[-1]
    if value is _DELETE:
        del node[int(last) if last.isdigit() else last]
    elif last.isdigit():
        node[int(last)] = value
    else:
        node[last] = value
    return doc


_DELETE = object()


@pytest.mark.parametrize("path,value,expect", [
    ("lanes.0.typo_key", 1, "unknown key(s): typo_key"),
    ("lanes.0.include_any", ["(unclosed"], "invalid regex"),
    ("lanes.1.id", "widget", "duplicate lane id"),
    ("lanes.1.include_any", _DELETE, "no positive rule"),
    ("lanes.0.match", "title_and_node", None),
    ("lanes.1.match", "node", "needs browse_nodes"),
    ("lanes.0.measures", {"capacity": {"min": 1}}, "unknown measure"),
    ("lanes.0.price_partitions", [[300, 100]], "must be below high"),
    ("lanes.0.seeds.0.max_pages", 12, "itemPage ceiling"),
    ("limits", {"max_pages_per_query": 11}, "itemPage ceiling"),
    ("catalog.insert_sql", "INSERT INTO p (asin, title) VALUES (%(asin)s, %(title)s)",
     "unknown parameter %(title)s"),
    ("catalog.insert_sql", "INSERT INTO p (kind) VALUES (%(lane)s)", "must use %(asin)s"),
    ("catalog.insert_sql", "INSERT INTO p (asin) VALUES (%s)", "positional %s"),
    ("catalog.known_sql", "SELECT asin FROM p", "must use %(asins)s"),
    ("lanes.0.seeds.0", {"id": "w1"}, "needs a query, a brand or a browse node"),
    ("lanes.0.seeds", [{"id": "w1", "query": "a"}, {"id": "w1", "query": "b"}], "duplicate seed id"),
    ("lanes.0.seeds.0", {"id": "w1", "query": "a", "brand": "x", "brands": ["y"]}, "brand or brands"),
    ("qa", {"golden": {"B0AAAAAAA1": "toaster"}}, "neither a lane id nor 'reject'"),
    ("qa", {"golden": {"NOTANASIN": "widget"}}, "is not an ASIN"),
    ("seed_asins", {"queries": [{"name": "q", "dsn_env": "X", "sql": "DELETE FROM products"}]},
     "read-only SELECT"),
    ("seed_asins", {"queries": [{"name": "q", "dsn_env": "X", "sql": "SELECT asin FROM p WHERE a = %(x)s"}]},
     "takes no parameters"),
    # One statement only: a COMMIT would end the READ ONLY transaction.
    ("seed_asins", {"queries": [{"name": "q", "dsn_env": "X",
                                 "sql": "SELECT asin FROM p; COMMIT; DELETE FROM p; SELECT 'B0BBBBBBBB' AS asin"}]},
     "single statement"),
    ("seed_asins", {"queries": [{"name": "q", "sql": "SELECT asin FROM p"}]}, "needs `site`"),
    ("seed_asins", {"files": [{"name": "f", "path": "x.json", "lanes": ["toaster"]}]}, "unknown lane"),
    ("seed_asins", {"files": [{"name": "f", "path": "x.json", "lane_map": {"a_b": "toaster"}}]},
     "unknown lane 'toaster'"),
    ("seed_asins", {"files": [{"name": "f", "path": "x.json"}, {"name": "f", "path": "y.json"}]},
     "duplicate seed source name"),
    ("seed_asins", {"files": [{"name": "f", "path": "x.json", "confidence": 1.5}]}, "maximum of 1"),
    ("active_phases", ["never"], "no lane has phase"),
    ("schema_version", "2", "schema_version"),
    # Hard API / policy limits a config may tighten but never loosen.
    ("creators", {"max_item_page": 20}, "maximum of 10"),
    ("pricing", {"min_price": 0}, "minimum of 1"),
    ("pricing", {"min_price": 0.5}, "minimum of 1"),
    ("lanes.0.seeds.0", {"id": "w1", "query": "widget", "min_price": 0.01},
     "is not valid under any of the given schemas"),          # seed oneOf: string | object
    ("measures", {"cap": {"patterns": [{"regex": "(\\d+(?:\\.\\d+)?)\\s?kwh", "scale": 1000,
                                        "dot_thousands": True}]}}, "scaled pattern"),
])
def test_config_rejects_bad_documents(path, value, expect):
    doc = _mutate(path, value)
    if expect is None:
        # title_and_node on a lane that has both kinds of evidence is fine
        assert C.build_config(doc, base_dir=FIXTURES).lane("widget").match == "title_and_node"
        return
    with pytest.raises(C.ConfigError) as err:
        C.build_config(doc, base_dir=FIXTURES)
    assert any(expect in p for p in err.value.problems), err.value.problems


def test_no_active_lane_is_an_error():
    doc = base_doc(active_phases=["later"])
    for lane in doc["lanes"]:
        lane["enabled"] = False
    with pytest.raises(C.ConfigError, match="no lane is active"):
        C.build_config(doc)


def test_invalid_asin_regex_is_a_config_error_not_a_crash():
    doc = base_doc()
    doc["catalog"]["asin_regex"] = "^B[0-9"
    with pytest.raises(C.ConfigError) as err:
        C.build_config(doc)
    assert any(p.startswith("catalog.asin_regex: invalid regex") for p in err.value.problems)


def test_seed_sql_may_use_a_literal_percent():
    doc = base_doc(seed_asins={"queries": [{"name": "q", "dsn_env": "X",
                                            "sql": "SELECT asin FROM p WHERE t LIKE '%station%';\n"}]})
    src = C.build_config(doc).seed_queries[0]
    assert src.sql.endswith("'%station%'")              # the one trailing ';' is dropped
    # It runs as a keyset subquery with bound parameters, so its '%' is escaped.
    paged = src.paged_sql()
    assert "'%%station%%'" in paged and paged.count("%(after)s") == 1 and "%(limit)s" in paged
    assert ";" not in paged


def test_insert_sql_may_use_an_escaped_literal_percent():
    doc = base_doc()
    doc["catalog"]["insert_sql"] = ("INSERT INTO p (asin, category_id) SELECT %(asin)s, id "
                                    "FROM categories WHERE slug LIKE 'power%%' LIMIT 1")
    assert "'power%%'" in C.build_config(doc).catalog.insert_sql
    doc["catalog"]["insert_sql"] = "INSERT INTO p (asin) SELECT %(asin)s WHERE 'a' LIKE 'a%'"
    with pytest.raises(C.ConfigError, match="bare %"):
        C.build_config(doc)


def test_api_and_pricing_limits_hold_without_the_schema():
    """The semantic pass enforces the same hard limits (a caller that skips
    the schema still cannot raise the page ceiling or drop the $1 floor)."""
    doc = base_doc(creators={"max_item_page": 20}, pricing={"min_price": 0})
    with pytest.raises(C.ConfigError) as err:
        C.build_config(doc, check_schema=False)
    assert any("itemPage ceiling" in p for p in err.value.problems)
    assert any("pricing-integrity floor" in p for p in err.value.problems)
    # Whatever the config says, no query and no rotation goes past page 10.
    cfg = build()
    object.__setattr__(cfg, "creators", C.CreatorsSettings(max_item_page=50))
    object.__setattr__(cfg, "limits", C.Limits(max_pages_per_query=50))
    assert cfg.max_pages == C.CREATORS_MAX_ITEM_PAGE
    rot = R.Rotation.from_state(cfg.queries(), None, max_item_page=50, result_count_cap=306, now=NOW)
    q = rot.queries[0]
    assert rot._limit(dataclasses_replace(q, max_pages=50)) == C.CREATORS_MAX_ITEM_PAGE


def test_query_expansion_keys_and_filters():
    doc = base_doc()
    doc["lanes"][0].update({
        "search_browse_node": 111, "price_partitions": [[0, 100], [100, 250.5], [250.5, None]],
        "seeds": [{"id": "w1", "query": "widget"},
                  {"id": "w2", "query": "widget pro", "partitions": False, "browse_node": None,
                   "sort_by": "NewestArrivals"},
                  {"id": "wb", "query": "widget", "brands": ["Acme Co", "Bolt"], "partitions": False}]})
    cfg = build(doc)
    widget = [q for q in cfg.queries() if q.lane == "widget"]
    assert [q.key for q in widget] == [
        "widget/w1/price=-100", "widget/w1/price=100-250.5", "widget/w1/price=250.5+",
        "widget/w2", "widget/wb/brand=acme-co", "widget/wb/brand=bolt"]
    assert widget[0].filters("cents") == {"browse_node_id": "111", "max_price": 10000}
    assert widget[1].filters("cents") == {"browse_node_id": "111", "min_price": 10000, "max_price": 25050}
    assert widget[2].filters("dollars") == {"browse_node_id": "111", "min_price": 250}
    assert widget[3].filters("cents") == {"sort_by": "NewestArrivals"}       # browse_node: null = unscoped
    assert widget[4].filters("cents") == {"browse_node_id": "111", "brand": "Acme Co"}
    # held-back lanes are not searched
    assert not any(q.lane == "gizmo" for q in cfg.queries())


def test_derived_rule_ids_are_unique_for_long_patterns():
    long_a = "^(?!.*\\b\\d{2,4}\\s?w(att)?s?\\b.{0,30}solar panel).*\\bfirst-alternative-here\\b"
    long_b = "^(?!.*\\b\\d{2,4}\\s?w(att)?s?\\b.{0,30}solar panel).*\\bsecond-alternative\\b"
    doc = base_doc()
    doc["lanes"][0]["exclude"] = [long_a, long_b]
    ids = [r.id for r in build(doc).lane("widget").exclude]
    assert len(set(ids)) == 2 and all(len(i) < 60 for i in ids)


# ---------------------------------------------------------------------------
# Lane classifier
# ---------------------------------------------------------------------------

def _lane_cases(fixture):
    doc = json.loads((FIXTURES / fixture).read_text())
    cfg = C.load_config(AGENT_DIR / doc["config"])
    return [(cfg, case) for case in doc["cases"]]


@pytest.mark.parametrize("cfg,case", _lane_cases("lane-cases.home-power.json")
                         + _lane_cases("lane-cases.cleaning-robots.json"),
                         ids=lambda v: v["title"][:48] if isinstance(v, dict) else "cfg")
def test_lane_assignment(cfg, case):
    d = L.LaneClassifier(cfg).classify(case["title"], price=case.get("price"),
                                       browse_nodes=case.get("nodes", []))
    expect = case["expect"]
    if expect == "reject":
        assert (d.lane, d.reason, d.rule) == (None, L.GLOBAL_REJECT, case["rule"]), d
    elif expect == "off-lane":
        assert (d.lane, d.reason) == (None, L.OFF_LANE), d
    elif expect.startswith("held:"):
        assert (d.lane, d.reason, d.matched_lane) == (None, L.HELD_BACK, expect[5:]), d
        assert d.predicted_label == expect[5:]
    else:
        assert (d.lane, d.reason) == (expect, L.OK), d
        if "confidence" in case:
            assert d.confidence == case["confidence"]


def test_precedence_falls_through_to_the_next_lane():
    cfg = C.load_config(EXAMPLES / "home-power.site.yaml")
    d = L.LaneClassifier(cfg).classify(
        "XT60 to MC4 Adapter Cable for Solar Generator Power Station", price=14)
    assert d.lane == "cable"
    assert "station:below-min-price" in d.trace  # tried, turned away, moved on


def test_measures_read_from_titles():
    spec = C.load_config(EXAMPLES / "home-power.site.yaml").measures["capacity_wh"]
    assert L.read_measure("Station 1,024Wh LiFePO4", spec) == 1024
    assert L.read_measure("Station 1.024Wh", spec) == 1024             # dot_thousands (Wh pattern)
    assert L.read_measure("3.6kWh home battery", spec) == 3600         # kWh scale
    # A three-decimal kWh figure is NOT dot-thousands: 2.048 kWh = 2048 Wh.
    assert L.read_measure("Portable Power Station 2.048kWh", spec) == 2048
    assert L.read_measure("Portable Power Station 3.072kWh LiFePO4", spec) == 3072
    assert L.read_measure("Power Station 1.024kWh LiFePO4 Solar Generator 1200W", spec) == 1024
    assert L.read_measure("Solar Generator 2.048 kWh with 2x 200W Solar Panels", spec) == 2048
    assert L.read_measure("519.48Wh pack", spec) == pytest.approx(519.48)
    assert L.read_measure("2048Wh, Expandable to 6kWh", spec) == 2048  # strip pattern
    assert L.read_measure("Expandable to 6kWh 2048Wh", spec) == 2048
    assert L.read_measure("no capacity stated", spec) is None
    watts = C.load_config(EXAMPLES / "home-power.site.yaml").measures["watts"]
    assert L.read_measure("1800W surge 3600W", watts) == 3600         # pick: max


def test_measure_level_dot_thousands_never_applies_to_a_scaled_pattern():
    doc = base_doc(measures={"cap": {"dot_thousands": True, "patterns": [
        "(\\d{1,5}(?:\\.\\d+)?)\\s?wh\\b", {"regex": "(\\d+(?:\\.\\d+)?)\\s?kwh\\b", "scale": 1000}]}})
    spec = build(doc).measures["cap"]
    assert [p.dot_thousands for p in spec.patterns] == [True, False]
    assert L.read_measure("1.024Wh", spec) == 1024 and L.read_measure("3.072kWh", spec) == 3072


# --- The port, checked against the reference -------------------------------
#
# A frozen copy of the sizing run's reader and classifier (listing_churn.py,
# sha256 50ab1598a670812e, functions capacity_wh / max_watts / classify_lane,
# verbatim apart from names) and its rules (fixtures/reference-churn-rules.
# power.json). Translated into this engine's config, the reference rules must
# give every title of the list the same lane under LaneClassifier, and every
# capacity must read the same: the port cannot drift unnoticed.

_REF_WH_RX = re.compile(r"(?<![\d.,])(\d{1,2},\d{3}(?:\.\d+)?|\d{1,5}(?:\.\d+)?)\s?(wh|watt[- ]?hours?)\b", re.I)
_REF_KWH_RX = re.compile(r"(\d+(?:\.\d+)?)\s?kwh\b", re.I)
_REF_W_RX = re.compile(r"(\d{2,4})\s?(w|watts?)\b", re.I)
_REF_EXPAND_RX = re.compile(r"(expand(able|ed|s)?|extend(able|ed)?|scalable|up)\s*(up\s*)?to\s*[\d.,]+\s?k?wh", re.I)


def _ref_rx(p):
    return re.compile(p, re.IGNORECASE)


def _ref_capacity_wh(title):
    t = _REF_EXPAND_RX.sub(" ", title or "")
    found = []
    for m in _REF_WH_RX.finditer(t):
        g = m.group(1)
        if re.match(r"^\d{1,2},\d{3}(\.\d+)?$", g):      # 1,024Wh
            v = float(g.replace(",", ""))
        elif re.match(r"^\d\.\d{3}$", g):                 # 1.024Wh (dot as thousands)
            v = float(g.replace(".", ""))
        else:                                              # 519.48Wh, 2048Wh
            v = float(g)
        found.append((m.start(), v))
    for m in _REF_KWH_RX.finditer(t):
        found.append((m.start(), float(m.group(1)) * 1000))
    return min(found)[1] if found else None


def _ref_max_watts(title):
    vals = [float(m.group(1)) for m in _REF_W_RX.finditer(title or "")]
    return max(vals) if vals else None


def _ref_classify_lane(title, price, cfg):
    t = title or ""
    for p in (cfg.get("global_exclude") or {}).get("patterns") or []:
        if _ref_rx(p).search(t):
            return None, f"global-exclude:{p}"
    reasons = []
    for lane in cfg.get("lanes") or []:
        if not all(_ref_rx(p).search(t) for p in lane.get("include_all") or []):
            continue
        if any(_ref_rx(p).search(t) for p in lane.get("exclude") or []):
            reasons.append(f"{lane['id']}:excluded")
            continue
        ctx = lane.get("context_required")
        if ctx and not _ref_rx(ctx).search(t):
            reasons.append(f"{lane['id']}:no-context")
            continue
        if price is not None and lane.get("min_price") and price < lane["min_price"]:
            reasons.append(f"{lane['id']}:below-min-price")
            continue
        if lane.get("wh_range"):
            wh = _ref_capacity_wh(t)
            lo, hi = lane["wh_range"]
            if wh is not None and not (lo <= wh <= hi):
                reasons.append(f"{lane['id']}:capacity-{int(wh)}Wh-outside-{lo}-{hi}")
                continue
            if wh is None and price is not None and price < lane.get("min_price_if_no_wh", 0):
                reasons.append(f"{lane['id']}:no-capacity-and-cheap")
                continue
        if lane.get("min_watts"):
            w = _ref_max_watts(t)
            if w is not None and w < lane["min_watts"]:
                reasons.append(f"{lane['id']}:{int(w)}W-below-{lane['min_watts']}W")
                continue
        return lane["id"], "ok"
    return None, (";".join(reasons) or "no-lane")


def _reference_rules():
    return json.loads((FIXTURES / "reference-churn-rules.power.json").read_text())


def _reference_as_engine_config(ref):
    """The reference rules in this engine's config shape (all lanes active)."""
    lanes = []
    for lane in ref["lanes"]:
        out = {"id": lane["id"], "phase": lane["phase"], "include_all": lane["include_all"],
               # a repeated exclude is redundant; this engine wants unique rule ids
               "exclude": list(dict.fromkeys(lane.get("exclude") or [])), "seeds": []}
        if lane.get("context_required"):
            out["context_required"] = lane["context_required"]
        if lane.get("min_price"):
            out["min_price"] = lane["min_price"]
        measures = {}
        if lane.get("wh_range"):
            lo, hi = lane["wh_range"]
            measures["capacity_wh"] = {"min": lo, "max": hi,
                                       "missing_min_price": lane.get("min_price_if_no_wh", 0)}
        if lane.get("min_watts"):
            measures["watts"] = {"min": lane["min_watts"]}
        if measures:
            out["measures"] = measures
        lanes.append(out)
    lanes[0]["seeds"] = [{"id": "s1", "query": "x"}]
    return {"schema_version": "1", "consumer": "reference-port",
            "catalog": {"dsn_env": "X", "known_sql": KNOWN_SQL, "insert_sql": INSERT_SQL},
            "reject_patterns": list(ref["global_exclude"]),
            "measures": {
                "capacity_wh": {"patterns": [{"regex": _REF_WH_RX.pattern, "dot_thousands": True},
                                             {"regex": _REF_KWH_RX.pattern, "scale": 1000}],
                                "strip": [_REF_EXPAND_RX.pattern], "pick": "first"},
                "watts": {"patterns": [_REF_W_RX.pattern], "pick": "max"}},
            "lanes": lanes}


_PORT_TITLES = [(c["title"], c.get("price")) for c in json.loads(
    (FIXTURES / "lane-cases.home-power.json").read_text())["cases"]] + [
    ("PECRON-like E3600 Portable Power Station 3.072kWh LiFePO4", 1999),
    ("Voltaro R4000 Portable Power Station 3.168kWh LiFePO4, 4000W", 1599),
    ("Solar Generator 2.048 kWh with 2x 200W Solar Panels Included", 1499),
    ("Portable Power Station 1.024kWh LiFePO4 Solar Generator 1200W", 599),
    ("Station 1.024Wh LiFePO4 Camping RV", 450), ("Station 1,024Wh LiFePO4 Camping", 450),
    ("Power Station 519.48Wh Home Backup", 299), ("Power Station 2048Wh, Expandable to 6kWh", 999),
    ("Expandable to 21.6kWh 3600Wh Home Backup Power Station", 2999),
    ("Power Station 6.1kWh Whole Home", 4999), ("Power Station 12kWh", 9999),
    ("Solar Generator 300Wh with 60W Solar Panel", 249), ("Solar Panel 50W 12V", 49),
    ("Solar Panel 100W 12V Monocrystalline", 89), ("MC4 Extension Cable 20ft for Solar Panel", 25),
    ("Power Station 100Wh", 120), ("Power Station", 120), ("Power Station", 160),
    ("Inverter Charger 3000W 24V", 499), ("LiFePO4 Battery 12V 100Ah", 229), ("", None),
]


@pytest.mark.parametrize("title,price", _PORT_TITLES, ids=lambda v: str(v)[:40])
def test_port_matches_the_reference_classifier(title, price):
    ref = _reference_rules()
    ours = L.LaneClassifier(build(_reference_as_engine_config(ref)))
    expected, _ = _ref_classify_lane(title, price, {"global_exclude": {"patterns": ref["global_exclude"]},
                                                   "lanes": ref["lanes"]})
    assert ours.classify(title, price=price).matched_lane == expected
    spec = build(_reference_as_engine_config(ref)).measures["capacity_wh"]
    assert L.read_measure(L.normalize_title(title), spec) == _ref_capacity_wh(title)
    # The worked example's capacity reader is the same reader.
    example = C.load_config(EXAMPLES / "home-power.site.yaml").measures["capacity_wh"]
    assert L.read_measure(L.normalize_title(title), example) == _ref_capacity_wh(title)


@pytest.mark.parametrize("match,title,nodes,lane,confidence", [
    ("node", "plain listing", ["111"], "widget", 0.6),
    ("node", "a widget", [], None, None),
    ("title_and_node", "a widget", ["111"], "widget", 0.9),
    ("title_and_node", "a widget", ["999"], None, None),
    ("title_or_node", "plain listing", ["111"], "widget", 0.6),
    ("title_or_node", "a widget", [], "widget", 0.7),
])
def test_match_modes_and_confidence(match, title, nodes, lane, confidence):
    doc = base_doc()
    doc["lanes"][0]["match"] = match
    if match == "node":
        del doc["lanes"][0]["include_any"]
    d = L.LaneClassifier(build(doc)).classify(title, price=50, browse_nodes=nodes)
    assert d.lane == lane
    if lane:
        assert d.confidence == confidence


def test_decisions_never_carry_title_text():
    cfg = build()
    for title in (f"{TITLE_TOKEN} widget", f"{TITLE_TOKEN} gas widget", f"{TITLE_TOKEN} thing",
                  f"{TITLE_TOKEN} gizmo"):
        d = L.LaneClassifier(cfg).classify(title, price=50)
        assert TITLE_TOKEN not in repr(d)


def test_title_normalization():
    assert L.normalize_title("Power Station    2000Wh") == "Power Station 2000Wh"


# ---------------------------------------------------------------------------
# Paging (the shared-loop defects W3 fixes centrally)
# ---------------------------------------------------------------------------

def test_shared_loop_stops_on_a_short_page_but_the_engine_does_not():
    pages = {("widget", 1): page("WIDG", 10), ("widget", 2): page("WIDG", 7, start=10),
             ("widget", 3): page("WIDG", 5, start=20), ("widget", 4): []}
    # The defect, documented: discover() alone treats the 7-item page 2 as the end.
    plain = FakeCreators(pages)
    ad.discover(plain, ad.normalize_keywords(["widget"]), known_asins=lambda a: set(),
                pages_per_keyword=4)
    assert [c["page"] for c in plain.calls] == [1, 2]
    # The engine pages on until the empty page.
    cfg = build()
    client, conn = FakeCreators(pages), FakeConn()
    rep, rot = search(cfg, client, conn)
    widget_pages = [c["page"] for c in client.calls if c["q"] == "widget"]
    assert widget_pages == [1, 2, 3, 4]
    assert rep.short_pages_continued == 2
    assert rot.progress["widget/w1"].end_reason == R.END_EMPTY
    assert len([r for r in rep.inserted if r.lane == "widget"]) == 22


def test_never_requests_beyond_item_page_10():
    cfg = build()
    client = FakeCreators(lambda q, p, f: page("WIDG", 10, start=p * 10) if q == "widget" else [])
    rep, rot = search(cfg, client, FakeConn(), allowed=40)
    assert max(c["page"] for c in client.calls if c["q"] == "widget") == 10
    assert rot.progress["widget/w1"].end_reason == R.END_MAX_PAGE
    assert rep.stopped_reason == E.STOP_CYCLE_COMPLETE


@pytest.mark.parametrize("total,expected_pages,saturated", [
    (23, [1, 2, 3], False),          # exact count below the 306 clip: 3 pages cover it
    (306, list(range(1, 11)), True), # at the clip: unknown length, page on to 10
    (150, list(range(1, 11)), True), # more than 10 pages reach
])
def test_total_result_count_rules(total, expected_pages, saturated):
    cfg = build()
    client = FakeCreators(lambda q, p, f: page("WIDG", 10, start=p * 10) if q == "widget" else [],
                          totals={"widget": total})
    rep, rot = search(cfg, client, FakeConn())
    assert [c["page"] for c in client.calls if c["q"] == "widget"] == expected_pages
    assert rot.progress["widget/w1"].saturated is saturated
    if not saturated:
        assert rot.progress["widget/w1"].end_reason == R.END_TOTAL


def test_breadth_first_order_and_resume_across_runs():
    doc = base_doc()
    doc["lanes"][0]["seeds"] = [{"id": "a", "query": "widget a"}, {"id": "b", "query": "widget b"},
                                {"id": "c", "query": "widget c"}]
    doc["lanes"][1]["seeds"] = []
    cfg = build(doc)

    def pages_fn(q, p, f):
        prefix = {"widget a": "WDGA", "widget b": "WDGB", "widget c": "WDGC"}[q]
        return page(prefix, 10, start=p * 10)

    client = FakeCreators(pages_fn)
    rep1, rot1 = search(cfg, client, FakeConn(), allowed=4)
    assert [(c["q"], c["page"]) for c in client.calls] == [
        ("widget a", 1), ("widget b", 1), ("widget c", 1), ("widget a", 2)]
    assert rep1.stopped_reason == E.STOP_MAX_CALLS
    state = rot1.to_state()
    client2 = FakeCreators(pages_fn)
    search(cfg, client2, FakeConn(), state=state, allowed=3)
    assert [(c["q"], c["page"]) for c in client2.calls] == [
        ("widget b", 2), ("widget c", 2), ("widget a", 3)]


def test_cycle_restart_and_cooldown():
    doc = base_doc(rotation={"cycle_cooldown_hours": 24})
    cfg = build(doc)
    client = FakeCreators({("widget", 1): page("WIDG", 3), ("gadget", 1): []})
    rep, rot = search(cfg, client, FakeConn())
    assert rep.stopped_reason == E.STOP_CYCLE_COMPLETE and rot.cycle == 1
    state = rot.to_state()
    assert state["cycle_completed_at"] == "2026-11-17T12:00:00Z"
    # inside the cooldown: no calls at all
    later = NOW + dt.timedelta(hours=5)
    client2 = FakeCreators({})
    rep2, rot2 = search(cfg, client2, FakeConn(), state=state, now=later)
    assert rep2.stopped_reason == "cooldown" and client2.calls == []
    # after it: a new cycle from page 1
    much_later = NOW + dt.timedelta(hours=25)
    client3 = FakeCreators({("widget", 1): page("WIDG", 3)})
    rep3, rot3 = search(cfg, client3, FakeConn(), state=rot2.to_state(), now=much_later)
    assert rot3.cycle == 2 and client3.calls[0]["page"] == 1
    assert rot3.last_cycle_completed_at == "2026-11-17T12:00:00Z"


def test_request_error_ends_the_query_and_the_run_moves_on():
    cfg = build()
    client = FakeCreators({("gadget", 1): page("GADG", 2, word="gadget")},
                          error_on={("widget", 1)})
    rep, rot = search(cfg, client, FakeConn())
    assert rot.progress["widget/w1"].end_reason == R.END_QUERY_ERROR
    assert rep.request_errors == 1
    assert {r.lane for r in rep.inserted} == {"gadget"}
    assert rot.ended_with_errors() == ["widget/w1"]


def test_a_server_error_is_retried_next_run_not_the_end_of_the_query():
    cfg = build()
    full = lambda q, p, f: page("WIDG" if q == "widget" else "GADG", 10, start=p * 10, word=q)  # noqa: E731
    client = FakeCreators(full, error_on={("widget", 3): 503})
    rep, rot = search(cfg, client, FakeConn(), allowed=8)
    widget_pages = [c["page"] for c in client.calls if c["q"] == "widget"]
    assert widget_pages == [1, 2, 3]                     # page 3 failed once, then skipped this run
    p = rot.progress["widget/w1"]
    assert (p.ended, p.next_page, p.retries) == (False, 3, 1)
    assert rep.transient_errors == 1 and rep.request_errors == 1
    assert rot.ended_with_errors() == []
    assert [c["q"] for c in client.calls].count("gadget") == 5   # the run moved on
    # The next run fetches widget page 3 again, and on success pages on.
    client2 = FakeCreators(full)
    rep2, rot2 = search(cfg, client2, FakeConn(), state=rot.to_state(), allowed=2)
    assert [(c["q"], c["page"]) for c in client2.calls] == [("widget", 3), ("widget", 4)]
    assert rot2.progress["widget/w1"].retries == 0


def test_a_client_error_still_ends_the_query_for_the_cycle():
    cfg = build()
    client = FakeCreators(lambda q, p, f: page("WIDG", 10, start=p * 10), error_on={("widget", 2): 400})
    _, rot = search(cfg, client, FakeConn(), allowed=6)
    assert rot.progress["widget/w1"].end_reason == R.END_PAGE_ERROR
    assert [c["page"] for c in client.calls if c["q"] == "widget"] == [1, 2]


def test_a_page_failing_on_every_run_ends_after_the_retry_cap():
    cfg = build()
    state = None
    for _ in range(R.TRANSIENT_RETRY_CAP):
        client = FakeCreators({("gadget", 1): []}, error_on={("widget", 1): 502})
        rep, rot = search(cfg, client, FakeConn(), state=state)
        state = rot.to_state()
    assert rot.progress["widget/w1"].end_reason == R.END_SERVER_ERROR
    assert rot.ended_with_errors() == ["widget/w1"] and rep.stopped_reason == E.STOP_CYCLE_COMPLETE


def test_a_streak_of_server_errors_stops_the_run():
    doc = base_doc()
    doc["lanes"][0]["seeds"] = [{"id": f"s{i}", "query": f"widget {i}"} for i in range(5)]
    cfg = build(doc)
    client = FakeCreators({}, error_on={(f"widget {i}", 1): 500 for i in range(5)})
    rep, rot = search(cfg, client, FakeConn(), allowed=40)
    assert rep.stopped_reason == E.STOP_SERVER_ERRORS
    assert rep.api_calls == E.SERVER_ERROR_STREAK_STOP == rep.transient_errors
    assert not any(p.ended for p in rot.progress.values())


def test_low_yield_stop_ends_a_query_whose_pages_add_little():
    cfg = build(limits={"stop_if_new_in_lane_below": 2, "low_yield_from_page": 2})
    conn = FakeConn(known=[f"B0WIDG{i:04d}" for i in range(10, 30)])   # pages 2 and 3 are known

    def pages_fn(q, p, f):
        return page("WIDG", 10, start=(p - 1) * 10) if q == "widget" else []

    client = FakeCreators(pages_fn)
    rep, rot = search(cfg, client, conn)
    # Page 1 adds 10; page 2 adds 0 (< 2) and is at/after page 2: the query ends.
    assert [c["page"] for c in client.calls if c["q"] == "widget"] == [1, 2]
    assert rot.progress["widget/w1"].end_reason == R.END_LOW_YIELD
    assert rot.ended_low_yield() == ["widget/w1"]
    # Off by default: the same pages are walked until the empty page.
    client2 = FakeCreators(pages_fn)
    search(build(), client2, FakeConn(known=[f"B0WIDG{i:04d}" for i in range(10, 30)]))
    assert [c["page"] for c in client2.calls if c["q"] == "widget"] == list(range(1, 11))


def test_a_low_yield_first_page_is_followed_by_page_two():
    cfg = build(limits={"stop_if_new_in_lane_below": 2, "low_yield_from_page": 2})
    conn = FakeConn(known=[f"B0WIDG{i:04d}" for i in range(0, 10)])    # page 1 fully known
    client = FakeCreators(lambda q, p, f: page("WIDG", 10, start=(p - 1) * 10) if q == "widget" and p <= 3 else [])
    _, rot = search(cfg, client, conn)
    assert [c["page"] for c in client.calls if c["q"] == "widget"] == [1, 2, 3, 4]
    assert rot.progress["widget/w1"].end_reason == R.END_EMPTY


def test_rotation_state_prunes_queries_that_left_the_config():
    cfg = build()
    rot = R.Rotation.from_state(cfg.queries(), {"version": 1, "cycle": 3, "queries": {
        "widget/w1": {"next_page": 4}, "retired/x": {"next_page": 9}}},
        max_item_page=10, result_count_cap=306, now=NOW)
    assert set(rot.progress) == {"widget/w1"} and rot.cycle == 3
    assert rot.next_slot()[0].key == "gadget/g1"   # page 1 of a fresh query comes first


# ---------------------------------------------------------------------------
# Search loop: persistence, caps, budget, failures
# ---------------------------------------------------------------------------

AMAZON_FIELDS = re.compile(r"Zorblax|123\.45|media-amazon|amazon\.com/dp|Acmecorp", re.I)


def test_search_persists_only_the_asin_and_our_labels():
    cfg = build()
    conn = FakeConn()
    client = FakeCreators({("widget", 1): page("WIDG", 3, nodes=["111"]),
                           ("gadget", 1): page("GADG", 2, word="gadget")})
    rep, _ = search(cfg, client, conn)
    params = conn.insert_params()
    assert len(params) == 5
    for p in params:
        assert set(p) == set(C.INSERT_PARAMS)
        assert not AMAZON_FIELDS.search(json.dumps(p)), p
    widget = next(p for p in params if p["lane"] == "widget")
    assert widget == {"asin": "B0WIDG0000", "lane": "widget", "lane_kind": "widget-kind",
                      "lane_category": "widgets", "confidence": 0.9,
                      "via": "discovery:widget/w1", "seed": "widget/w1", "run_ts": RUN_TS,
                      "site_id": None}
    assert rep.node_confirmed == 3 and len(rep.inserted) == 5
    # every statement the engine executed is free of Amazon content
    for sql, p in conn.executed:
        assert not AMAZON_FIELDS.search(sql + json.dumps(p, default=str))
    assert conn.commits >= 2  # page by page


def test_lane_rules_reject_and_hold_back():
    cfg = build()
    items = [raw_item("B0WIDG0001", f"{TITLE_TOKEN} widget"),
             raw_item("B0WIDG0002", f"{TITLE_TOKEN} gas widget"),       # global reject
             raw_item("B0WIDG0003", f"{TITLE_TOKEN} gizmo"),            # held-back lane
             raw_item("B0WIDG0004", f"{TITLE_TOKEN} sprocket"),         # off-lane
             raw_item("B0WIDG0005", f"{TITLE_TOKEN} widget", price=5),  # below lane floor -> off-lane
             raw_item("0123456789", f"{TITLE_TOKEN} widget book")]      # not a product ASIN
    rep, _ = search(build(), FakeCreators({("widget", 1): items}), FakeConn())
    assert [r.asin for r in rep.inserted] == ["B0WIDG0001"]
    assert rep.rejected == {"global-reject": 1, "lane-held-back": 1, "off-lane": 2,
                            "invalid-asin": 1}
    assert rep.rejected_rules["global:gas"] == 1
    assert rep.rejected_rules["held-back:gizmo"] == 1
    assert rep.rejected_rules["off-lane:widget:below-min-price"] == 1
    assert not AMAZON_FIELDS.search(json.dumps(rep.to_dict()))


def test_pricing_gate_rejections_are_counted_not_inserted():
    items = [raw_item("B0WIDG0001", "widget a", condition="Used"),
             raw_item("B0WIDG0002", "widget b", currency="CAD"),
             raw_item("B0WIDG0003", "widget c", price=0.5),
             raw_item("B0WIDG0004", "widget d", avail="OUT_OF_STOCK"),
             raw_item("B0WIDG0005", "widget e")]
    rep, _ = search(build(), FakeCreators({("widget", 1): items}), FakeConn())
    assert [r.asin for r in rep.inserted] == ["B0WIDG0005"]
    assert rep.pricing_rejected == {"used-only": 1, "currency": 1, "price-below-min": 1,
                                    "unavailable": 1}


def test_known_and_repeated_listings_are_judged_once():
    cfg = build()
    conn = FakeConn(known=["B0WIDG0000"])
    pages = {("widget", 1): page("WIDG", 10), ("widget", 2): page("WIDG", 3),  # repeats 0-2
             ("gadget", 1): page("WIDG", 2)}                                     # widgets again
    rep, _ = search(cfg, FakeCreators(pages), conn)
    assert len(rep.inserted) == 9 and rep.known_seen == 1
    assert rep.candidates == 9
    assert len(conn.insert_params()) == 9


def test_cross_lane_listings_take_their_own_lane():
    rep, _ = search(build(), FakeCreators({("widget", 1): [raw_item("B0GADG0001", "a gadget")]}),
                    FakeConn())
    assert [(r.asin, r.lane, r.via) for r in rep.inserted] == [
        ("B0GADG0001", "gadget", "discovery:widget/w1")]
    assert rep.cross_lane == 1


def test_known_listings_that_now_hit_a_global_reject_are_reported():
    """A catalog row that entered under an older rule set (or as an untitled
    seed label) is flagged when its fresh search title is a year-1 reject."""
    conn = FakeConn(known=["B0WIDG0001", "B0WIDG0002"])
    items = [raw_item("B0WIDG0001", f"{TITLE_TOKEN} gas widget"),
             raw_item("B0WIDG0002", f"{TITLE_TOKEN} widget"),
             raw_item("B0WIDG0003", f"{TITLE_TOKEN} widget")]
    rep, _ = search(build(), FakeCreators({("widget", 1): items}), conn)
    assert rep.known_rejected == [{"asin": "B0WIDG0001", "rule": "gas"}]
    assert rep.known_rejected_total == 1 and rep.known_seen == 2
    assert [r.asin for r in rep.inserted] == ["B0WIDG0003"]
    assert "B0WIDG0001" in conn.rows                       # reported, never changed by discovery
    assert not AMAZON_FIELDS.search(json.dumps(rep.to_dict()))


def test_a_seed_floor_can_only_raise_the_pricing_floor():
    doc = base_doc()
    doc["lanes"][0]["seeds"] = [{"id": "w1", "query": "widget", "min_price": 0.5}]
    doc["lanes"][0]["min_price"] = 0
    cfg = C.build_config(doc, base_dir=FIXTURES, check_schema=False)   # the schema forbids it
    items = [raw_item("B0WIDG0001", "widget", price=0.75), raw_item("B0WIDG0002", "widget", price=4.0)]
    rep, _ = search(cfg, FakeCreators({("widget", 1): items}), FakeConn())
    assert [r.asin for r in rep.inserted] == ["B0WIDG0002"]
    assert rep.pricing_rejected == {"price-below-min": 1}


def test_new_asin_cap_defers_the_rest_and_refetches_the_slot():
    cfg = build(limits={"max_new_per_run": 3})
    pages = {("widget", 1): page("WIDG", 5)}
    conn = FakeConn()
    client = FakeCreators(pages)
    rep, rot = search(cfg, client, conn)
    assert len(rep.inserted) == 3 and rep.deferred_max_new == 2
    assert rep.stopped_reason == E.STOP_MAX_NEW
    assert "widget/w1" not in rot.progress   # slot left unrecorded
    client2 = FakeCreators(pages)
    rep2, _ = search(cfg, client2, conn, state=rot.to_state())
    assert client2.calls[0] == {"q": "widget", "page": 1, "index": "All"}
    assert sorted(r.asin for r in rep2.inserted) == ["B0WIDG0003", "B0WIDG0004"]


def test_call_allowance_is_never_exceeded():
    cfg = build()
    client = FakeCreators(lambda q, p, f: page("WIDG", 10, start=p * 10, word=q))
    rep, _ = search(cfg, client, FakeConn(), allowed=7)
    assert len(client.calls) == 7 == rep.api_calls
    assert rep.stopped_reason == E.STOP_MAX_CALLS
    rep0, _ = search(cfg, FakeCreators({}), FakeConn(), allowed=0)
    assert rep0.stopped_reason == E.STOP_NO_ALLOWANCE and rep0.api_calls == 0


def test_daily_budget_and_throttle_stop_and_retry_the_slot():
    cfg = build()
    full = lambda q, p, f: page("WIDG", 10, start=p * 10, word=q)  # noqa: E731
    client = FakeCreators(full, budget_fails_after=2)
    rep, rot = search(cfg, client, FakeConn())
    assert rep.stopped_reason == E.STOP_BUDGET and rep.api_calls == 2
    nxt = rot.next_slot()
    client_t = FakeCreators({("gadget", 1): page("GADG", 3, word="gadget")},
                            throttle_on={("widget", 2)})
    rep_t, rot_t = search(cfg, client_t, FakeConn(), state=rot.to_state())
    assert (nxt[0].key, nxt[1]) == ("widget/w1", 2)
    assert rep_t.stopped_reason == E.STOP_THROTTLED and rep_t.throttled == 1
    assert rot_t.progress["widget/w1"].next_page == 2   # the throttled page is retried


def test_api_outage_keeps_the_work_done_so_far():
    cfg = build()
    conn = FakeConn()
    client = FakeCreators({("widget", 1): page("WIDG", 10), ("gadget", 1): page("GADG", 4, word="gadget")},
                          unavailable_on={("widget", 2)})
    rep, rot = search(cfg, client, conn)
    assert rep.stopped_reason == E.STOP_UNAVAILABLE and "network down" in rep.creators_error
    assert len(conn.rows) == 14                     # committed before the outage
    assert rot.progress["widget/w1"].next_page == 2


def test_one_bad_row_never_aborts_the_batch():
    conn = FakeConn(fail_on={"B0WIDG0001"})
    rep, _ = search(build(), FakeCreators({("widget", 1): page("WIDG", 3)}), conn)
    assert [r.asin for r in rep.inserted] == ["B0WIDG0000", "B0WIDG0002"]
    assert rep.insert_errors == 1 and "B0WIDG0001" in rep.insert_error_samples[0]
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s, _ in conn.executed)


def test_dry_run_executes_no_insert():
    conn = FakeConn()
    rep, _ = search(build(), FakeCreators({("widget", 1): page("WIDG", 3)}), conn, dry_run=True)
    assert len(rep.inserted) == 3
    assert conn.insert_params() == [] and conn.rows == {} and conn.commits == 0
    assert conn.rollbacks >= 1


def test_price_partitions_and_the_unit_check():
    doc = base_doc()
    doc["lanes"][0]["price_partitions"] = [[10, 50], [50, None]]
    cfg = build(doc)
    seen_filters = []

    def pages_fn(q, p, f):
        seen_filters.append(dict(f))
        if q != "widget" or p > 1:
            return []
        lo = f.get("min_price", 0)
        # Prices 100x the band: what a dollars-vs-cents mix-up looks like.
        return page("WDLO" if lo < 5000 else "WDHI", 10, price=3000.0 if lo < 5000 else 9000.0)

    rep, _ = search(cfg, FakeCreators(pages_fn), FakeConn())
    assert {"min_price": 1000, "max_price": 5000} in seen_filters
    assert {"min_price": 5000} in seen_filters
    assert rep.partition_checked == 20 and rep.partition_misses == 10
    assert not rep.partition_unit_suspect          # half outside: not conclusive
    rep2, _ = search(cfg, FakeCreators(lambda q, p, f: page("WDLO", 10, price=900.0, start=p)
                                       if q == "widget" and p == 1 and f.get("max_price") else []),
                     FakeConn())
    assert rep2.partition_misses == rep2.partition_checked == 10
    rep3_cfg = build(doc)
    assert rep3_cfg.creators.price_filter_unit == "cents"


def test_real_creators_client_carries_page_and_filters(storage, monkeypatch):
    """The adapter on the REAL client: urlopen mocked, ledger in tmp storage."""
    monkeypatch.setattr(ac, "_utc_day", lambda: "2026-11-17")
    doc = base_doc()
    doc["lanes"][0].update({"search_browse_node": "111", "price_partitions": [[100, 300]]})
    cfg = build(doc)
    client = ac.AmazonCreatorsClient(
        ac.CreatorsConfig(client_id="id", client_secret="sec", partner_tag="t-20"),
        min_interval_s=0, consumer=cfg.consumer, storage=storage)
    client._token = ac._Token("tok", 9e12)
    bodies = []

    class _Resp:
        def __init__(self, body):
            self._b = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data)
        bodies.append(body)
        if body.get("keywords") == "widget" and body.get("itemPage", 1) == 2:
            return _Resp({"searchResult": {"items": page("WIDG", 4, start=10), "totalResultCount": 14}})
        if body.get("keywords") == "widget":
            return _Resp({"searchResult": {"items": page("WIDG", 10), "totalResultCount": 14}})
        return _Resp({"searchResult": {"items": [], "totalResultCount": 0}})

    with mock.patch.object(ac.urllib.request, "urlopen", side_effect=fake_urlopen):
        rep, rot = search(cfg, client, FakeConn())
    first = bodies[0]
    assert first["keywords"] == "widget" and first["browseNodeId"] == "111"
    assert first["minPrice"] == 10000 and first["maxPrice"] == 30000 and "itemPage" not in first
    widget = [b for b in bodies if b.get("keywords") == "widget"]
    assert [b.get("itemPage", 1) for b in widget] == [1, 2]   # 14 results: 2 pages, then done
    assert bodies[1]["keywords"] == "gadget"                  # breadth first: page 1s first
    assert rot.progress["widget/w1/price=100-300"].end_reason == R.END_TOTAL
    assert len(rep.inserted) == 14
    ledger = storage.read_json(ac.usage_key(cfg.consumer, "2026-11-17"))
    assert ledger["calls"] == len(bodies) == rep.api_calls


# ---------------------------------------------------------------------------
# Budget allowance
# ---------------------------------------------------------------------------

def _ledger(storage, day, **calls):
    for consumer, n in calls.items():
        storage.write_json(ac.usage_key(consumer.replace("_", "-"), day), {"calls": n})


def test_budget_reserves_what_protected_consumers_still_need(storage, monkeypatch):
    monkeypatch.setattr(ac, "_utc_day", lambda: "2026-11-17")
    storage.write_json(ac.BUDGET_CONFIG_KEY, {"total_calls_per_day": 1000,
                                              "shares": {"price-refresh": 0.5,
                                                         "test-discovery": 0.1}})
    _ledger(storage, "2026-11-17", price_refresh=100, hydration=420, test_discovery=10)
    cfg = build(budget={"protect_consumers": ["price-refresh", "test-discovery"]})
    client = ac.AmazonCreatorsClient(
        ac.CreatorsConfig(client_id="i", client_secret="s", partner_tag="t"),
        consumer="test-discovery", storage=storage)
    b = E.budget_allowance(cfg, client, storage=storage)
    # day left 1000-530 = 470; price-refresh still needs 500-100 = 400 -> headroom 70;
    # own share left 100-10 = 90; cap 40.  Self is never "protected".
    assert (b.day_left, b.protected_reserve, b.protected) == (470, 400, ("price-refresh",))
    assert b.allowed_calls == 40
    _ledger(storage, "2026-11-17", hydration=450)
    b2 = E.budget_allowance(cfg, client, storage=storage)
    assert b2.allowed_calls == 40 and b2.day_left == 440
    _ledger(storage, "2026-11-17", hydration=520)
    b3 = E.budget_allowance(cfg, client, storage=storage)
    assert b3.day_left == 370 and b3.allowed_calls == 0     # 370 - 400 reserved -> none
    cfg_keep = build(budget={"keep_unused_fraction_of_day": 0.3})
    b4 = E.budget_allowance(cfg_keep, client, storage=storage)
    assert b4.keep_floor == 300 and b4.allowed_calls == 40   # 370 - 300 = 70 headroom


def test_budget_without_protection_and_the_cli_override():
    cfg = build()
    assert E.budget_allowance(cfg, FakeCreators(budget=25)).allowed_calls == 25
    assert E.budget_allowance(cfg, FakeCreators(budget=999)).allowed_calls == 40
    assert E.budget_allowance(cfg, FakeCreators(budget=999), max_calls=5).allowed_calls == 5
    assert E.budget_allowance(cfg, FakeCreators(budget=999), max_calls=500).allowed_calls == 40


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def seed_doc(**sources):
    """base_doc plus the lanes and phases the seed fixtures name."""
    doc = base_doc(**{"seed_asins": sources}) if sources else base_doc()
    doc["active_phases"] = ["now", "launch"]
    doc["lanes"].append({"id": "station", "phase": "launch", "include_any": ["power station"],
                         "seeds": []})
    doc["lanes"].append({"id": "kit", "phase": "launch", "include_any": ["kit"], "seeds": []})
    doc["lanes"].append({"id": "panel", "phase": "launch", "include_any": ["solar panel"],
                         "seeds": []})
    doc["lanes"].append({"id": "inverter", "phase": "phase2", "include_any": ["inverter"],
                         "seeds": []})
    return doc


def test_seed_file_shapes_and_filters():
    cfg = build(seed_doc(files=[
        {"name": "records", "path": "seed-asins.records.json", "json_path": "asins",
         "where": {"counted": True}, "default_lane": "kit", "trust_labels": True},
        {"name": "list", "path": "seed-asins.list.json", "default_lane": "station"},
        {"name": "map", "path": "seed-asins.map.json", "lanes": ["station"]}]))
    clf = L.LaneClassifier(cfg)
    by_name = {s.name: s for s in cfg.seed_files}
    recs, sha = S.read_seed_file(by_name["records"])
    plan = S.plan_source(recs, by_name["records"], cfg=cfg, classifier=clf)
    assert [(r.asin, r.lane) for r in plan.rows] == [
        ("B0SEEDST01", "station"), ("B0SEEDST02", "station"), ("B0SEEDKI01", "kit"),
        ("B0SEEDCA01", "kit")]                     # no lane -> default_lane
    assert plan.skipped == {"phase-held-back": 1, "unknown-lane": 1, "invalid-asin": 1,
                            "duplicate": 1}
    assert len(sha) == 16
    assert all(r.via == "seed:records" and r.confidence == 0.8 for r in plan.rows)
    recs, _ = S.read_seed_file(by_name["list"])
    plan = S.plan_source(recs, by_name["list"], cfg=cfg, classifier=clf)
    assert [r.lane for r in plan.rows] == ["station"] * 3
    recs, _ = S.read_seed_file(by_name["map"])
    plan = S.plan_source(recs, by_name["map"], cfg=cfg, classifier=clf)
    assert [(r.asin, r.lane) for r in plan.rows] == [("B0MAPST001", "station")]
    assert plan.skipped == {"lane-not-allowed": 1}


def test_untitled_labels_need_explicit_trust():
    """A label with no title cannot be checked against the lane rules (or the
    year-1 rejects): it is used only for lanes the source vouches for."""
    cfg = build(seed_doc())
    clf = L.LaneClassifier(cfg)
    recs = [S.SeedRecord("B0TRUST001", lane="station"), S.SeedRecord("B0TRUST002", lane="panel"),
            S.SeedRecord("B0TRUST003")]
    plan = S.plan_seed_rows(recs, cfg=cfg, classifier=clf, source_name="s", default_lane="kit")
    assert [(r.asin, r.lane) for r in plan.rows] == [("B0TRUST003", "kit")]   # operator's default
    assert plan.skipped == {S.UNTRUSTED_LABEL: 2}
    plan = S.plan_seed_rows(recs, cfg=cfg, classifier=clf, source_name="s",
                            allowed_lanes=frozenset({"station"}))
    assert [(r.asin, r.lane) for r in plan.rows] == [("B0TRUST001", "station")]
    assert plan.skipped == {"lane-not-allowed": 1, "no-lane": 1}
    plan = S.plan_seed_rows(recs, cfg=cfg, classifier=clf, source_name="s", trust_labels=True,
                            confidence=0.45)
    assert [(r.asin, r.lane, r.confidence) for r in plan.rows] == [
        ("B0TRUST001", "station", 0.45), ("B0TRUST002", "panel", 0.45)]


def test_titled_seed_records_are_screened_even_with_a_label():
    """The year-1 boundary holds for seeds: a title is checked against the
    global rejects whatever label the source gave it."""
    cfg = C.load_config(EXAMPLES / "home-power.site.yaml")
    clf = L.LaneClassifier(cfg)
    cord = S.SeedRecord("B0SEEDL143", lane="cable", title="Generator Cord 30A L14-30P to L14-30R 240V 25ft")
    for classify in (True, False):
        plan = S.plan_seed_rows([cord], cfg=cfg, classifier=clf, source_name="x",
                                classify_titles=classify, trust_labels=True)
        assert plan.rows == [] and plan.skipped == {"global-reject": 1}, classify
    recs = [
        S.SeedRecord("B0SEEDOK01", lane="station", title="Voltaro 1024Wh Portable Power Station, 1800W"),
        # our rules file it as a kit: a disputed label is not imported
        S.SeedRecord("B0SEEDKT01", lane="station",
                     title="Voltaro Power Station with 200W Solar Panel, 1024Wh Solar Generator Kit"),
        S.SeedRecord("B0SEEDIN01", lane="station", title="2000W Pure Sine Wave Inverter 12V to 110V"),
        S.SeedRecord("B0SEEDBG01", lane="station", title="Power Station Carrying Case Bag"),
    ]
    plan = S.plan_seed_rows(recs, cfg=cfg, classifier=clf, source_name="x", classify_titles=True)
    assert [(r.asin, r.lane) for r in plan.rows] == [("B0SEEDOK01", "station")]
    assert plan.skipped == {S.LABEL_DISPUTED: 1, "lane-held-back": 1, "off-lane": 1}
    # Without classify_titles the label is kept, but not past its lane's own excludes.
    plan = S.plan_seed_rows(recs, cfg=cfg, classifier=clf, source_name="x")
    assert [(r.asin, r.lane) for r in plan.rows] == [("B0SEEDOK01", "station"),
                                                     ("B0SEEDKT01", "station")]
    assert plan.skipped == {"lane-held-back": 1, S.LANE_EXCLUDED: 1}
    assert plan.notes == {"title-suggests-kit": 1}
    assert TITLE_TOKEN not in repr(plan) and "Voltaro" not in repr(plan)


def test_seed_file_lane_map_where_not_and_title_field(tmp_path):
    (tmp_path / "seeds.json").write_text(json.dumps({"asins": [
        {"asin": "B0MAPEXP01", "lane": "expansion_battery", "counted": True, "subclass": "x"},
        {"asin": "B0MAPCAB01", "lane": "cable", "counted": True, "subclass": "dc-solar",
         "name": "MC4 Solar Extension Cable 10AWG for Solar Panel"},
        {"asin": "B0MAPCAB02", "lane": "cable", "counted": True, "subclass": "ac-rv-generator",
         "name": "TT-30 Adapter Cord"},
        {"asin": "B0MAPCAB03", "lane": "cable", "counted": True, "subclass": "other",
         "name": "Solar Generator 240V Extension Cord 25ft"},
        {"asin": "B0MAPCAB04", "lane": "cable", "counted": False, "subclass": "dc-solar"}]}))
    doc = seed_doc(files=[{"name": "f", "path": "seeds.json", "json_path": "asins",
                           "where": {"counted": True}, "where_not": {"subclass": ["ac-rv-generator"]},
                           "lane_map": {"expansion_battery": "kit"}, "title_field": "name",
                           "trust_labels": True}])
    doc["lanes"].append({"id": "cable", "phase": "launch", "include_any": ["cable|cord"],
                         "exclude": ["\\bmonitor\\b"], "seeds": []})
    doc["reject_patterns"].append({"id": "cord-240v", "pattern": "\\b240\\s?v"})
    cfg = build(doc, tmp_path=tmp_path)
    src = cfg.seed_files[0]
    recs, _ = S.read_seed_file(src)
    assert [r.asin for r in recs] == ["B0MAPEXP01", "B0MAPCAB01", "B0MAPCAB03"]
    plan = S.plan_source(recs, src, cfg=cfg, classifier=L.LaneClassifier(cfg))
    assert [(r.asin, r.lane) for r in plan.rows] == [("B0MAPEXP01", "kit"), ("B0MAPCAB01", "cable")]
    assert plan.skipped == {"global-reject": 1}
    with pytest.raises(S.SeedSourceError, match="object records"):
        list(S.records_from_document(["B0AAAAAAA1"], C.SeedFileSource(
            name="y", path=tmp_path, where_not={"x": 1})))


def test_seed_file_shape_errors(tmp_path):
    src = C.SeedFileSource(name="x", path=tmp_path / "x.json", where={"counted": True})
    (tmp_path / "x.json").write_text('["B0AAAAAAA1"]')
    with pytest.raises(S.SeedSourceError, match="object records"):
        S.read_seed_file(src)
    (tmp_path / "x.json").write_text("not json")
    with pytest.raises(S.SeedSourceError, match="not JSON"):
        S.read_seed_file(src)
    with pytest.raises(S.SeedSourceError, match="no 'missing'"):
        list(S.records_from_document({"a": []}, C.SeedFileSource(name="y", path=tmp_path,
                                                                  json_path="missing")))


def _seed_engine(cfg):
    return engine_for(cfg)


def test_seed_import_inserts_new_and_skips_unchanged_sources():
    cfg = build(seed_doc(files=[{"name": "list", "path": "seed-asins.list.json",
                                 "default_lane": "station"}]))
    conn = FakeConn(known=["B0LISTST02"])
    cat = K.Catalog(conn, cfg.catalog, run_ts=RUN_TS)
    state: dict = {}
    rep = _seed_engine(cfg).import_seeds(cat, state=state, connect_seed=None)
    assert sorted(r.asin for r in rep.imported) == ["B0LISTST01", "B0LISTST03"]
    assert rep.already_known == 1 and state["seed_sources"]["list"]["complete"] is True
    rep2 = _seed_engine(cfg).import_seeds(cat, state=state, connect_seed=None)
    assert rep2.unchanged == ["list"] and rep2.imported == []
    # a config change (e.g. a lane re-activated) re-reads the source
    cfg2 = build(seed_doc(files=[{"name": "list", "path": "seed-asins.list.json",
                                  "default_lane": "station"}], ), )
    object.__setattr__(cfg2, "fingerprint", "changed")
    rep3 = _seed_engine(cfg2).import_seeds(cat, state=state, connect_seed=None)
    assert rep3.unchanged == [] and rep3.imported == [] and rep3.already_known == 3


def test_seed_cap_truncates_and_the_next_run_continues():
    doc = seed_doc(files=[{"name": "list", "path": "seed-asins.list.json", "default_lane": "station"}])
    doc["limits"] = {"max_seed_asins_per_run": 2}
    cfg = build(doc)
    conn = FakeConn()
    cat = K.Catalog(conn, cfg.catalog, run_ts=RUN_TS)
    state: dict = {}
    rep = _seed_engine(cfg).import_seeds(cat, state=state, connect_seed=None)
    assert len(rep.imported) == 2 and rep.truncated == ["list"]
    assert state["seed_sources"]["list"]["complete"] is False
    rep2 = _seed_engine(cfg).import_seeds(cat, state=state, connect_seed=None)
    assert [r.asin for r in rep2.imported] == ["B0LISTST03"]


def test_seed_query_classifies_titles_in_memory_only():
    doc = seed_doc(queries=[{"name": "sister", "dsn_env": "SISTER_DSN", "classify_titles": True,
                             "lanes": ["station", "kit"],
                             "sql": "SELECT asin, title FROM products WHERE title ILIKE '%station%'"}])
    cfg = build(doc)
    seed_conn = FakeConn(seed_columns=("asin", "title"), seed_rows=[
        ("B0SISTER01", f"{TITLE_TOKEN} power station 1000Wh"),
        ("B0SISTER02", f"{TITLE_TOKEN} gas power station"),     # global reject
        ("B0SISTER03", f"{TITLE_TOKEN} solar panel 100W"),      # panel: not allowed here
        ("B0SISTER04", f"{TITLE_TOKEN} faraday bag"),           # off-lane
        ("B0SISTER05", f"{TITLE_TOKEN} 2000W pure sine inverter"),  # held back
    ])
    opened = []

    def connect_seed(src):
        opened.append(src.name)
        return seed_conn

    conn = FakeConn()
    state: dict = {}
    rep = _seed_engine(cfg).import_seeds(K.Catalog(conn, cfg.catalog, run_ts=RUN_TS), state=state,
                                         connect_seed=connect_seed)
    assert [(r.asin, r.lane) for r in rep.imported] == [("B0SISTER01", "station")]
    assert rep.imported[0].confidence == 0.7             # title-only evidence
    assert rep.skipped == {"global-reject": 1, "lane-not-allowed": 1, "off-lane": 1,
                           "lane-held-back": 1}
    assert opened == ["sister"] and seed_conn.closed and seed_conn.rollbacks == 1
    blob = json.dumps({"state": state, "rep": rep.to_dict(),
                       "executed": [(s, p) for s, p in conn.executed]}, default=str)
    assert TITLE_TOKEN not in blob
    # not due again until refresh_hours have passed
    rep2 = _seed_engine(cfg).import_seeds(K.Catalog(conn, cfg.catalog, run_ts=RUN_TS),
                                          state=state, connect_seed=connect_seed)
    assert rep2.unchanged == ["sister"] and opened == ["sister"]


def test_a_broken_seed_source_never_stops_the_others(tmp_path):
    doc = seed_doc(files=[{"name": "missing", "path": str(tmp_path / "nope.json")},
                          {"name": "list", "path": "seed-asins.list.json", "default_lane": "station"}],
                   queries=[{"name": "q", "dsn_env": "X", "sql": "SELECT asin FROM p"}])
    cfg = build(doc)

    def boom(src):
        raise S.SeedSourceError("seed DSN env X is empty")

    rep = _seed_engine(cfg).import_seeds(K.Catalog(FakeConn(), cfg.catalog, run_ts=RUN_TS),
                                         state={}, connect_seed=boom)
    assert set(rep.errors) == {"missing", "q"} and len(rep.imported) == 3


def test_seed_query_reads_keyset_pages_and_continues_next_run():
    """A source larger than max_rows is never cut silently: each run reads
    the next page (asin > cursor) until a short page completes it."""
    doc = seed_doc(queries=[{"name": "sister", "dsn_env": "X", "default_lane": "station",
                             "max_rows": 2, "sql": "SELECT asin FROM products"}])
    cfg = build(doc)
    seed_conn = FakeConn(seed_columns=("asin",), seed_rows=[
        (f"B0KEYS000{i}",) for i in (3, 1, 2, 5, 4)])
    conn, state = FakeConn(), {}

    def run():
        return _seed_engine(cfg).import_seeds(K.Catalog(conn, cfg.catalog, run_ts=RUN_TS),
                                              state=state, connect_seed=lambda src: seed_conn)

    rep = run()
    assert [r.asin for r in rep.imported] == ["B0KEYS0001", "B0KEYS0002"]
    assert rep.continued == ["sister"] and rep.per_source["sister"]["continues_next_run"] is True
    assert state["seed_sources"]["sister"]["complete"] is False
    assert state["seed_sources"]["sister"]["cursor"] == "B0KEYS0002"
    assert [r.asin for r in run().imported] == ["B0KEYS0003", "B0KEYS0004"]
    rep3 = run()
    assert [r.asin for r in rep3.imported] == ["B0KEYS0005"] and rep3.continued == []
    assert state["seed_sources"]["sister"]["complete"] is True
    assert state["seed_sources"]["sister"]["cursor"] == ""
    assert run().unchanged == ["sister"]                      # read to its end: refresh_hours apply
    afters = [p["after"] for s, p in seed_conn.executed if isinstance(p, dict) and "after" in p]
    assert afters == ["", "B0KEYS0002", "B0KEYS0004"]


# The 2026-11-12 sizing file's shape (sizing-power.json .asins): per (lane,
# phase, counted, subclass), how many records it holds. Synthetic ASINs.
SIZING_SHAPE = [
    ("cable", "launch", True, "dc-solar", 310), ("cable", "launch", True, "car-charging", 60),
    ("cable", "launch", True, "parallel-ecosystem", 15), ("cable", "launch", True, "ac-charging-cord", 14),
    ("cable", "launch", True, "other", 29), ("cable", "launch", True, "ac-rv-generator", 125),
    ("panel", "launch", True, "rigid", 305), ("station", "launch", True, "250wh-5kwh", 284),
    ("kit", "launch", True, "250wh-5kwh", 140), ("expansion_battery", "launch", True, "expansion", 18),
    ("lifepo4_battery", "phase2", True, "x", 343), ("inverter", "phase2", True, "x", 156),
    ("bos", "phase2", True, "x", 82), ("charge_controller", "phase2", True, "x", 70),
    (None, None, False, None, 380), ("panel", "launch", False, "rigid", 62),
    ("station", "launch", False, "x", 46), ("kit", "launch", False, "x", 44),
    ("lifepo4_battery", "phase2", False, "x", 23), ("bos", "phase2", False, "x", 20),
    ("cable", "launch", False, "dc-solar", 19), ("inverter", "phase2", False, "x", 19),
    ("charge_controller", "phase2", False, "x", 7), ("expansion_battery", "launch", False, "expansion", 2),
]


def _sizing_shaped_file(path: Path) -> Path:
    records, n = [], 0
    for lane, phase, counted, subclass, count in SIZING_SHAPE:
        for _ in range(count):
            records.append({"asin": f"B0SZ{n:06d}", "lane": lane, "phase": phase, "subclass": subclass,
                            "reject": None if lane else "not-in-any-lane", "counted": counted,
                            "rule": f"head:{lane}" if lane else None})
            n += 1
    path.write_text(json.dumps({"schema_version": "1", "asins": records}))
    return path


def _example_seed_sources(seed_file: Path):
    """The example's seed-file sources, pointed at ``seed_file``."""
    cfg = C.load_config(EXAMPLES / "home-power.site.yaml")
    return cfg, {s.name: dataclasses_replace(s, path=seed_file) for s in cfg.seed_files}


def _assert_sizing_import(seed_file: Path):
    cfg, sources = _example_seed_sources(seed_file)
    clf = L.LaneClassifier(cfg)
    # D1's number: every counted launch-lane ASIN, the 651 phase-2 ones held back.
    everything = dataclasses_replace(sources["sizing-launch"], lanes=None, trust_labels=True)
    recs, _ = S.read_seed_file(everything)
    plan = S.plan_source(recs, everything, cfg=cfg, classifier=clf)
    assert len(plan.rows) == 1300 and plan.skipped == {"phase-held-back": 651}
    assert dict(Counter(r.lane for r in plan.rows)) == {
        "cable": 553, "panel": 305, "station": 284, "kit": 140, "expansion": 18}
    # The shipped example: the 747 non-cable labels; cable labels are not
    # imported on trust (the sizing cable lane admitted L14-30 / 14-50 cords).
    launch = sources["sizing-launch"]
    recs, _ = S.read_seed_file(launch)
    plan = S.plan_source(recs, launch, cfg=cfg, classifier=clf)
    assert len(plan.rows) == 747
    assert plan.skipped == {"phase-held-back": 651, "lane-not-allowed": 553}
    assert all(r.confidence == 0.8 for r in plan.rows)
    # The opt-in DC-solar / car-charging cables, below the pick floor.
    dc = sources["sizing-launch-dc-cables"]
    assert dc.enabled is False
    recs, _ = S.read_seed_file(dc)
    plan = S.plan_source(recs, dc, cfg=cfg, classifier=clf)
    assert len(plan.rows) == 370 and {(r.lane, r.confidence) for r in plan.rows} == {("cable", 0.45)}
    return cfg, launch


def test_sizing_shaped_seed_file_imports_the_counted_launch_lanes(tmp_path):
    cfg, launch = _assert_sizing_import(_sizing_shaped_file(tmp_path / "seed-asins.json"))
    # Through the engine (seed cap 2000, one source): 747 inserted, state complete.
    run_cfg = dataclasses_replace(cfg, seed_files=(launch,), seed_queries=())
    conn, state = FakeConn(), {}
    rep = engine_for(run_cfg).import_seeds(K.Catalog(conn, run_cfg.catalog, run_ts=RUN_TS),
                                           state=state, connect_seed=None)
    assert len(rep.imported) == 747 == len(conn.rows)
    assert state["seed_sources"]["sizing-launch"]["complete"] is True
    assert {p["lane_category"] for p in conn.insert_params()} == {
        "power-stations", "solar-generator-kits", "expansion-batteries", "solar-panels"}


SIZING_FILE = os.environ.get("CATALOG_DISCOVERY_SIZING_FILE")


@pytest.mark.skipif(not (SIZING_FILE and Path(SIZING_FILE).is_file()),
                    reason="CATALOG_DISCOVERY_SIZING_FILE (the sizing run's sizing-power.json) not set")
def test_real_sizing_file_imports_the_counted_launch_lanes():
    """The same numbers on the real sizing file (read-only; ASINs and labels only)."""
    _assert_sizing_import(Path(SIZING_FILE))


# ---------------------------------------------------------------------------
# Golden-label precision
# ---------------------------------------------------------------------------

def test_golden_window_and_precision():
    window = E.update_golden_window({}, [
        {"asin": "B0A", "expected": "station", "predicted": "station"},
        {"asin": "B0B", "expected": "reject", "predicted": "reject"},
        {"asin": "B0C", "expected": "panel", "predicted": "reject"},
        {"asin": "B0D", "expected": "reject", "predicted": "cable"}], run_ts="r1", size=10)
    m = E.golden_metrics(window, min_observations=2)
    assert m == {"golden_observations": 4.0, "reject_precision": 0.5, "in_lane_precision": 0.5}
    assert "reject_precision" not in E.golden_metrics(window, min_observations=3)
    # the latest observation per ASIN wins; the window keeps the newest entries
    window = E.update_golden_window(window, [{"asin": "B0C", "expected": "panel",
                                              "predicted": "panel"}], run_ts="r2", size=3)
    assert len(window) == 3 and window["B0C"]["predicted"] == "panel"
    assert "B0A" not in window  # oldest (r1, B0A) dropped first


def test_golden_labels_are_observed_during_search(tmp_path):
    doc = seed_doc()
    doc["qa"] = {"golden_file": str(FIXTURES / "golden.json"),
                 "golden": {"B0GOLDGA01": "gadget"}, "min_observations": 1}
    cfg = build(doc)
    conn = FakeConn(known=["B0GOLDPA01"])
    items = [raw_item("B0GOLDST01", "widget"), raw_item("B0GOLDRJ01", "gas widget"),
             raw_item("B0GOLDPA01", "widget"), raw_item("B0GOLDGA01", "widget")]
    rep, _ = search(cfg, FakeCreators({("widget", 1): items}), conn)
    got = {o["asin"]: (o["expected"], o["predicted"]) for o in rep.golden}
    assert got == {"B0GOLDST01": ("station", "widget"), "B0GOLDRJ01": ("reject", "reject"),
                   "B0GOLDPA01": ("panel", "widget"),     # a known listing is still judged
                   "B0GOLDGA01": ("gadget", "widget")}


# ---------------------------------------------------------------------------
# The AgentBase run
# ---------------------------------------------------------------------------

@pytest.fixture
def make_agent(storage, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_DSN", "postgresql://user:sekret@db.invalid/catalog")
    monkeypatch.delenv("AGENT_DEFAULT_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    made = []

    def make(doc, *, client=None, conn=None, run_ts=RUN_TS, now=NOW, connect=None):
        path = tmp_path / "site.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        a = agent_mod.AmazonCatalogDiscoveryAgent(storage=storage, run_ts=run_ts)
        a.config_path = str(path)
        a.send_run_summary_email = False
        a.auto_recovery_enabled = False
        a.calls = []
        a._connect = connect or (lambda dsn, **kw: a.calls.append(kw) or conn)
        a._make_client = lambda cfg: client
        a._now = lambda: now
        made.append(a)
        return a

    yield make
    for a in made:
        a._stop_heartbeat()


def _storage_text(storage) -> str:
    root = Path(storage.root)
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


def test_agent_run_end_to_end(make_agent, storage):
    doc = seed_doc(files=[{"name": "list", "path": str(FIXTURES / "seed-asins.list.json"),
                           "default_lane": "station"}])
    doc["qa"] = {"golden": {"B0WIDG0000": "widget", "B0WIDG0002": "reject"}, "min_observations": 1}
    items = page("WIDG", 4, nodes=["111"])
    items[2] = raw_item("B0WIDG0002", f"{TITLE_TOKEN} gas widget")
    client = FakeCreators({("widget", 1): items, ("gadget", 1): page("GADG", 2, word="gadget")})
    conn = FakeConn()
    agent = make_agent(doc, client=client, conn=conn)
    result = agent.run()
    assert result.status == "success", result.summary
    m = result.metrics
    assert m["new_in_lane_asins"] == 5 and m["seed_asins_imported"] == 3
    assert m["api_calls"] == len(client.calls) and m["budget_overrun_calls"] == 0
    assert m["rejected_global"] == 1 and m["reject_precision"] == 1.0 and m["in_lane_precision"] == 1.0
    assert m["calls_per_new_asin"] == round(m["api_calls"] / 5, 3)
    assert "5 new in-lane ASIN(s) (widget 3, gadget 2)" in result.summary
    assert result.next_state["rotation"]["cycle"] == 1
    assert result.next_state["seed_sources"]["list"]["complete"] is True
    assert set(result.next_state["golden"]) == {"B0WIDG0000", "B0WIDG0002"}
    assert agent.calls[0]["statement_timeout_ms"] == 30000 and conn.closed
    report = storage.read_json(f"agents/{agent.agent_id}/runs/{RUN_TS}/discovery-report.json")
    assert report["search"]["inserted"] == 5 and report["seeds"]["imported"] == 3
    assert {r["asin"] for r in report["search"]["rows"]} >= {"B0WIDG0000", "B0GADG0001"}
    # Compliance: nothing Amazon wrote reaches storage (report, decisions, status) or the DB.
    stored = _storage_text(storage)
    assert TITLE_TOKEN not in stored and "123.45" not in stored and "media-amazon" not in stored
    assert "sekret" not in stored
    for sql, p in conn.executed:
        assert not AMAZON_FIELDS.search(sql + json.dumps(p, default=str))


def test_agent_reports_untrusted_labels_known_rejects_and_server_errors(make_agent, storage, tmp_path):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"B0LABEL001": "station", "B0LABEL002": "panel"}))
    doc = seed_doc(files=[{"name": "labels", "path": str(labels)}])          # no lanes, no trust
    conn = FakeConn(known=["B0WIDG0001"])
    client = FakeCreators({("widget", 1): [raw_item("B0WIDG0001", f"{TITLE_TOKEN} gas widget"),
                                           raw_item("B0WIDG0002", f"{TITLE_TOKEN} widget")]},
                          error_on={("gadget", 1): 503})
    result = make_agent(doc, client=client, conn=conn).run()
    assert result.status == "success", result.summary
    m = result.metrics
    assert (m["seed_asins_imported"], m["new_in_lane_asins"]) == (0, 1)
    assert (m["known_now_rejected"], m["transient_errors"], m["query_errors"]) == (1, 1, 0)
    assert m["low_yield_queries"] == 0
    assert "stopped: slots-deferred" in result.summary
    assert result.next_state["rotation"]["queries"]["gadget/g1"] == {"next_page": 1, "pages": 1,
                                                                     "errors": 1, "retries": 1}
    stored = _storage_text(storage)
    assert "2 labelled record(s) without a title were not imported" in stored
    assert "B0WIDG0001 (gas)" in stored
    assert TITLE_TOKEN not in stored and "123.45" not in stored


def test_agent_run_once_records_progress_and_resumes(make_agent, storage):
    doc = base_doc(limits={"max_calls_per_run": 2})
    pages_fn = lambda q, p, f: page("WIDG" if q == "widget" else "GADG", 10, start=p * 10, word=q)  # noqa: E731
    agent = make_agent(doc, client=FakeCreators(pages_fn), conn=FakeConn())
    res = agent.run_once()
    assert res.status == "success"
    progress = storage.read_json(f"agents/{agent.agent_id}/runs/{RUN_TS}/progress.json")
    assert progress["metrics"]["new_in_lane_asins"] == 20
    saved = storage.read_json(f"agents/{agent.agent_id}/state/latest.json")["state"]
    assert saved["rotation"]["queries"]["widget/w1"]["next_page"] == 2
    client2 = FakeCreators(pages_fn)
    agent2 = make_agent(doc, client=client2, conn=FakeConn(), run_ts="20261117T180000Z")
    agent2.run_once()
    assert [(c["q"], c["page"]) for c in client2.calls] == [("widget", 2), ("gadget", 2)]


def test_agent_failure_paths_keep_state(make_agent, monkeypatch):
    prior = {"rotation": {"version": 1, "cycle": 7, "queries": {}}}
    bad = base_doc()
    bad["lanes"][0]["include_any"] = ["(oops"]
    a = make_agent(bad, client=FakeCreators({}), conn=FakeConn())
    a.state = copy.deepcopy(prior)
    r = a.run()
    assert r.status == "failure" and "invalid regex" in r.summary and r.next_state == prior

    a = make_agent(base_doc(enabled=False), client=FakeCreators({}), conn=FakeConn())
    r = a.run()
    assert r.status == "success" and r.metrics == {"discovery_enabled": 0}

    monkeypatch.delenv("TEST_CATALOG_DSN")
    a = make_agent(base_doc(), client=FakeCreators({}), conn=FakeConn())
    a.state = copy.deepcopy(prior)
    r = a.run()
    assert r.status == "failure" and "TEST_CATALOG_DSN" in r.summary and r.next_state == prior

    monkeypatch.setenv("TEST_CATALOG_DSN", "postgresql://user:sekret@db.invalid/x")

    def refuse(dsn, **kw):
        raise RuntimeError('connection to server at "db.invalid" failed: timeout')

    a = make_agent(base_doc(), client=FakeCreators({}), connect=refuse)
    r = a.run()
    assert r.status == "failure" and "sekret" not in r.summary + r.error_text

    # No Creators credentials: the seed phase still lands, the run reports failure.
    doc = seed_doc(files=[{"name": "list", "path": str(FIXTURES / "seed-asins.list.json"),
                           "default_lane": "station"}])
    conn = FakeConn()
    a = make_agent(doc, client=None, conn=conn)
    r = a.run()
    assert r.status == "failure" and "not configured" in r.summary
    assert r.metrics["seed_asins_imported"] == 3 and len(conn.rows) == 3


def test_agent_unexpected_error_keeps_rotation_progress(make_agent):
    class Exploding(FakeConn):
        def __init__(self):
            super().__init__()
            self.known_calls = 0

        def cursor(self):
            cur = FakeCursor(self)
            real = cur.execute

            def execute(sql, params=None):
                if "%(asins)s" in sql:
                    self.known_calls += 1
                    if self.known_calls == 2:
                        raise RuntimeError("statement timeout")
                return real(sql, params)
            cur.execute = execute
            return cur

    pages_fn = lambda q, p, f: page("WIDG", 10, start=p * 10) if q == "widget" else []  # noqa: E731
    a = make_agent(base_doc(), client=FakeCreators(pages_fn), conn=Exploding())
    r = a.run()
    assert r.status == "failure" and "statement timeout" in r.summary
    assert r.next_state["rotation"]["queries"]["widget/w1"]["next_page"] == 2
    assert "Traceback" in r.error_text


def test_modes_seeds_only_and_no_seeds(make_agent):
    doc = seed_doc(files=[{"name": "list", "path": str(FIXTURES / "seed-asins.list.json"),
                           "default_lane": "station"}])
    client = FakeCreators({("widget", 1): page("WIDG", 2)})
    a = make_agent(doc, client=client, conn=FakeConn())
    a.mode = "seeds-only"
    r = a.run()
    assert r.status == "success" and client.calls == [] and r.metrics["seed_asins_imported"] == 3
    a = make_agent(doc, client=client, conn=FakeConn())
    a.mode = "no-seeds"
    r = a.run()
    assert r.metrics["seed_asins_imported"] == 0 and r.metrics["new_in_lane_asins"] == 2


def test_malformed_state_pieces_start_afresh(make_agent):
    doc = seed_doc(files=[{"name": "list", "path": str(FIXTURES / "seed-asins.list.json"),
                           "default_lane": "station"}])
    a = make_agent(doc, client=FakeCreators({("widget", 1): page("WIDG", 2)}), conn=FakeConn())
    a.state = {"rotation": [1, 2], "seed_sources": "garbage", "golden": [3], "other": {"kept": 1}}
    r = a.run()
    assert r.status == "success"
    assert r.metrics["seed_asins_imported"] == 3 and r.metrics["new_in_lane_asins"] == 2
    assert r.next_state["other"] == {"kept": 1}
    assert isinstance(r.next_state["rotation"], dict)
    assert E.golden_metrics([1, 2], min_observations=1) == {"golden_observations": 0.0}


def test_cooldown_run_spends_nothing_and_says_until_when(make_agent):
    doc = base_doc(rotation={"cycle_cooldown_hours": 48})
    state = {"rotation": {"version": 1, "cycle": 2, "cycle_started_at": "2026-11-15T00:00:00Z",
                          "cycle_completed_at": "2026-11-17T06:00:00Z",
                          "queries": {"widget/w1": {"next_page": 4, "ended": True,
                                                    "end_reason": "empty-page"},
                                      "gadget/g1": {"next_page": 2, "ended": True,
                                                    "end_reason": "total-reached"}}}}
    client = FakeCreators({("widget", 1): page("WIDG", 2)})
    a = make_agent(doc, client=client, conn=FakeConn())
    a.state = state
    r = a.run()
    assert r.status == "success" and client.calls == []
    assert "stopped: cooldown until 2026-11-19T06:00:00Z" in r.summary
    assert r.metrics["api_calls"] == 0 and "calls_per_new_asin" not in r.metrics


def test_dry_run_writes_nothing(make_agent, storage, capsys):
    a = make_agent(base_doc(), client=FakeCreators({("widget", 1): page("WIDG", 2)}), conn=FakeConn())
    a.dry_run = True
    before = _storage_text(storage)
    result, report = a.execute()
    assert result.status == "success" and report["search"]["inserted"] == 2
    assert _storage_text(storage) == before
    assert "[plan]" in capsys.readouterr().out


def test_dsn_resolution_through_the_site_registry(storage, monkeypatch):
    from framework.core import site_registry
    site_registry.clear_cache()
    monkeypatch.setenv("ACME_DB_URL", "postgresql://acme")
    monkeypatch.setenv("DATABASE_URL_BETA", "postgresql://beta")
    storage.write_json(site_registry.CONFIG_KEY, {"schema_version": "1", "sites": [
        {"name": "acme", "domain": "acme.example", "repo_path": "/srv/acme", "branch": "main",
         "dsn_env": "ACME_DB_URL"}]})
    try:
        r = K.resolve_dsn(dsn_env=None, site="acme", storage=storage)
        assert (r.env_name, r.value, r.source) == ("ACME_DB_URL", "postgresql://acme", "site-registry")
        r = K.resolve_dsn(dsn_env=None, site="beta", storage=storage)
        assert (r.env_name, r.source, r.ok) == ("DATABASE_URL_BETA", "derived", True)
        r = K.resolve_dsn(dsn_env="EXPLICIT_X", site="acme", storage=storage)
        assert (r.env_name, r.ok) == ("EXPLICIT_X", False)
        assert "postgresql" not in repr(K.resolve_dsn(dsn_env=None, site="acme", storage=storage))
    finally:
        site_registry.clear_cache()


# ---------------------------------------------------------------------------
# CLI, manifest, goals, site literals
# ---------------------------------------------------------------------------

def test_cli_validate(capsys, tmp_path):
    assert agent_mod.main(["--config", str(EXAMPLES / "home-power.site.yaml"), "--validate"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["queries"] == 37
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: '1'\nconsumer: x\n")
    assert agent_mod.main(["--config", str(bad), "--validate"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    with pytest.raises(SystemExit):
        agent_mod.main(["--config", str(bad), "--show-titles"])


def test_manifest_is_inert_and_goals_bind_to_emitted_metrics(make_agent):
    manifest = json.loads((AGENT_DIR / "manifest.json").read_text())
    assert manifest["id"] == "amazon-catalog-discovery-agent"
    assert manifest["enabled"] is False and not manifest.get("cron_expr")
    assert manifest["metadata"]["config_env"] == C.CONFIG_ENV
    goals = json.loads((AGENT_DIR / "goals.json").read_text())
    import jsonschema
    schema = json.loads((_ROOT / "shared" / "schemas" / "agent-goals.schema.json").read_text())
    jsonschema.validate(goals, schema)
    assert 3 <= len(goals["goals"]) <= 7
    doc = base_doc(qa={"golden": {"B0WIDG0000": "widget", "B0WIDG0001": "reject"},
                       "min_observations": 1})
    items = [raw_item("B0WIDG0000", "widget"), raw_item("B0WIDG0001", "gas widget")]
    agent = make_agent(doc, client=FakeCreators({("widget", 1): items}), conn=FakeConn())
    metrics = agent.run().metrics
    for goal in goals["goals"]:
        assert goal["target_metric"] in metrics, goal["id"]
        assert goal["metric"]["name"] == goal["target_metric"]


def test_agent_code_names_no_site():
    """CLAUDE.md: no site names anywhere in a shipped agent (code, runbook,
    manifest, schema, examples, fixtures), nor in this test."""
    from framework.core import site_registry
    names = {s["name"] for s in site_registry.read_defaults()["sites"]}
    assert len(names) >= 2
    pattern = re.compile("|".join(re.escape(n) for n in names), re.I)
    checked = 0
    for path in sorted(AGENT_DIR.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".md", ".json", ".yaml", ".yml", ".sh", ".txt"):
            assert not pattern.search(path.read_text()), path
            checked += 1
    assert checked >= 20
    assert not pattern.search(Path(__file__).read_text())


# ---------------------------------------------------------------------------
# Optional: a real throwaway Postgres
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("CATALOG_DISCOVERY_TEST_PG_DSN")


@pytest.mark.skipif(not PG_DSN, reason="CATALOG_DISCOVERY_TEST_PG_DSN not set")
def test_example_sql_against_postgres():
    import psycopg2
    schema = f"f12_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"""
            SET search_path = {schema};
            CREATE TABLE categories (id serial PRIMARY KEY, slug text UNIQUE NOT NULL);
            INSERT INTO categories (slug) VALUES ('power-stations'), ('solar-panels');
            CREATE TABLE products (
              id serial PRIMARY KEY,
              asin text UNIQUE NOT NULL CHECK (asin ~ '^B[0-9A-Z]{{9}}$'),
              kind text, category_id int REFERENCES categories(id),
              category_confidence numeric, discovered_via text, discovered_at timestamptz,
              is_active boolean NOT NULL DEFAULT true);
            CREATE TABLE sister_products (asin text, title text);
            INSERT INTO sister_products VALUES
              ('B0SISTERPG', 'Lumora 1000Wh Portable Power Station'),
              ('B0SISTERP2', 'Faraday Bag for phones');""")
    try:
        cfg = C.load_config(EXAMPLES / "home-power.site.yaml")
        conn = K.connect(PG_DSN, application_name="f12-test", statement_timeout_ms=10000)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path = {schema}")
        conn.commit()
        cat = K.Catalog(conn, cfg.catalog, run_ts=RUN_TS)
        items = [raw_item("B0PGST0001", f"{TITLE_TOKEN} Portable Power Station 1024Wh", price=499.0,
                          nodes=["348967011"]),
                 raw_item("B0PGPA0001", f"{TITLE_TOKEN} 200W Foldable Solar Panel", price=229.0)]
        # Whatever slot comes first (the kit lane's, by precedence) returns these.
        client = FakeCreators(lambda q, p, f: items if p == 1 else [])
        eng = engine_for(cfg)
        rot = eng.rotation(None)
        rep = eng.run_search(client, cat, rotation=rot, allowed_calls=1)
        assert len(rep.inserted) == 2
        with conn.cursor() as cur:
            cur.execute("SELECT p.asin, p.kind, c.slug, p.category_confidence, p.discovered_via "
                        "FROM products p LEFT JOIN categories c ON c.id = p.category_id ORDER BY 1")
            rows = cur.fetchall()
        assert [(r[0], r[1], r[2], float(r[3])) for r in rows] == [
            ("B0PGPA0001", "panel", "solar-panels", 0.7),
            ("B0PGST0001", "station", "power-stations", 0.9)]
        # a repeat is a no-op, a CHECK violation is isolated
        bad = K.CatalogRow(asin="B0BAD", lane="station", lane_kind="station",
                           lane_category="power-stations", confidence=0.5, via="t")
        again = K.CatalogRow(asin="B0PGST0001", lane="station", lane_kind="station",
                             lane_category="power-stations", confidence=0.5, via="t")
        out = cat.insert([bad, again])
        cat.commit()
        assert (len(out.inserted), out.noop, out.errors) == (0, 1, 1)
        # the seed query path, in a READ ONLY session
        seed_cfg = C.build_config({**yaml.safe_load((EXAMPLES / "home-power.site.yaml").read_text()),
                                   "seed_asins": {"queries": [{
                                       "name": "sister", "dsn_env": "X", "classify_titles": True,
                                       "lanes": ["station"],
                                       "sql": "SELECT asin, title FROM sister_products"}]}},
                                  base_dir=EXAMPLES)

        def connect_seed(src):
            c = K.connect(PG_DSN, application_name="f12-seed", statement_timeout_ms=5000,
                          readonly=True)
            with c.cursor() as cur:
                cur.execute(f"SET search_path = {schema}")
            return c

        srep = engine_for(seed_cfg).import_seeds(cat, state={}, connect_seed=connect_seed)
        assert [(r.asin, r.lane) for r in srep.imported] == [("B0SISTERPG", "station")]
        ro = connect_seed(None)
        with pytest.raises(psycopg2.Error):
            with ro.cursor() as cur:
                cur.execute("INSERT INTO sister_products VALUES ('x', 'y')")
        ro.close()
        # The review's bypass: a COMMIT inside the seed SQL ended the READ ONLY
        # transaction and the DELETE after it ran read-write. Now the SESSION
        # is read-only, so the transaction after a COMMIT is too...
        ro = connect_seed(None)
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            with ro.cursor() as cur:
                cur.execute("SELECT asin FROM sister_products; COMMIT; DELETE FROM sister_products")
        ro.close()
        # ...and the engine never sends such SQL anyway: one statement, run as
        # a keyset subquery (a ';' tail is a syntax error there).
        probe = C.SeedQuerySource(name="probe", dsn_env="X", sql=(
            "SELECT asin FROM sister_products; COMMIT; DELETE FROM sister_products; "
            "SELECT 'B0BBBBBBBB' AS asin"))
        ro = connect_seed(None)
        with pytest.raises(psycopg2.Error):
            S.read_seed_query(ro, probe)
        ro.close()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sister_products")
            assert cur.fetchone()[0] == 2
        conn.rollback()
        # Keyset pages on a real server: two rows, one per page.
        paged = dataclasses_replace(seed_cfg.seed_queries[0], max_rows=1)

        def page_after(after):  # one connection per read, as each run opens its own
            c = connect_seed(None)
            try:
                return S.read_seed_query(c, paged, after=after)
            finally:
                c.close()

        first = page_after("")
        second = page_after(first.last)
        third = page_after(second.last)
        assert ([r.asin for r in first.records], first.more) == (["B0SISTERP2"], True)
        assert ([r.asin for r in second.records], second.more) == (["B0SISTERPG"], True)
        assert (third.records, third.more) == ([], False)
        conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()
