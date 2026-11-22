"""agents/amazon-catalog-discovery-agent: the MATCH phase (model_matching).

"Search and confirm" (operator decision 2026-11-22): an ASIN is linked to a
model only when Amazon's brand field matches the maker (alias-aware) AND the
model's exact designation appears in the listing title as a whole token
sequence. Titles and every other Amazon field stay in memory; only the ASIN,
model id, rule id, confidence and time persist.

Offline: the Creators client and the catalog connection are fakes, framework
storage is the tmp LocalFilesystemStorage from conftest. The model records are
real makers / model names from fixtures/model_matcher/power-models.json. An
optional real-Postgres check runs when MODEL_MATCHING_TEST_PG_DSN points at a
throwaway database.
"""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util as iu
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = _ROOT / "agents" / "amazon-catalog-discovery-agent"
EXAMPLE = AGENT_DIR / "examples" / "home-power.site.yaml"
MODELS_FIXTURE = Path(__file__).parent / "fixtures" / "model_matcher" / "power-models.json"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from catalog_discovery import catalog as K  # noqa: E402
from catalog_discovery import config as C  # noqa: E402
from catalog_discovery import engine as E  # noqa: E402
from catalog_discovery import matching as MP  # noqa: E402
from framework.core import amazon_creators  # noqa: E402
from framework.core.amazon_creators import CreatorsRequestError, CreatorsUnavailable  # noqa: E402

_spec = iu.spec_from_file_location("amazon_catalog_discovery_agent_match_test", AGENT_DIR / "agent.py")
agent_mod = iu.module_from_spec(_spec)
_spec.loader.exec_module(agent_mod)

NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)
RUN_TS = "20261122T120000Z"
#: In every synthetic title: it must never reach the DB, a report, a log or storage.
SENTINEL = "QZXSENTINEL7"

KNOWN_SQL = "SELECT asin FROM products WHERE asin = ANY(%(asins)s)"
INSERT_SQL = ("INSERT INTO products (asin, kind, lane, category_confidence, discovered_via, discovered_seed) "
              "VALUES (%(asin)s, %(lane_kind)s, %(lane)s, %(confidence)s, %(via)s, %(seed)s) "
              "ON CONFLICT (asin) DO NOTHING")
MODELS_SQL = "SELECT * FROM test_models"
CANDIDATES_SQL = "SELECT asin, kind FROM products WHERE is_active AND model_id IS NULL"
LINK_SQL = ("UPDATE products SET model_id = %(model_id)s, category_confidence = %(confidence)s "
            "WHERE asin = %(asin)s AND model_id IS NULL AND is_active")
STATS_SQL = ("SELECT count(*) FILTER (WHERE is_active) AS active_products, "
             "count(*) FILTER (WHERE is_active AND model_id IS NOT NULL) AS matched_products FROM products")

ALL_MODELS = {r["id"]: r for r in json.loads(MODELS_FIXTURE.read_text())["models"]}
MODEL_IDS = ["ecoflow-delta-3", "ecoflow-delta-3-plus", "ecoflow-delta-3-max-plus", "ecoflow-delta-2",
             "ecoflow-delta-2-max", "ecoflow-delta-2-max-smart-extra-battery", "ecoflow-100w-rigid-solar-panel",
             "ecoflow-delta-3-max-plus-solar-generator-2-x-100w-rigid-solar-panel",
             "jackery-explorer-1000-plus", "jackery-explorer-1000-v2", "bluetti-ac180", "bluetti-ac180p",
             "bluetti-ac200l", "anker-solix-c1000x"]


def model_rows(*, linked=(), priority=None, variants=None, searchable=None):
    out = []
    for i, mid in enumerate(MODEL_IDS):
        r = dict(ALL_MODELS[mid])
        r["priority"] = (priority or {}).get(mid, 100 + i)
        r["linked"] = mid in linked
        r["variants"] = (variants or {}).get(mid, 0)
        r["searchable"] = (searchable or {}).get(mid, True)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def raw_item(asin, title, *, brand, price=499.0, nodes=(), condition="New"):
    """A Creators item (getItems or searchItems shape); every title carries SENTINEL."""
    listing = {"price": {"money": {"amount": price, "currency": "USD"}}, "condition": {"value": condition},
               "isBuyBoxWinner": True, "availability": {"type": "IN_STOCK", "message": "In Stock"}}
    return {"asin": asin, "detailPageURL": f"https://www.amazon.com/dp/{asin}?tag=t-20",
            "itemInfo": {"title": {"displayValue": f"{title} {SENTINEL}"},
                         "byLineInfo": {"brand": {"displayValue": brand}}},
            "offersV2": {"listings": [listing] if price is not None else []},
            "browseNodeInfo": {"browseNodes": [{"id": n, "displayName": "Node"} for n in nodes]}}


class FakeCreators:
    def __init__(self, items=None, searches=None, *, budget=10_000, errors=(), throttle_get=False,
                 throttle_search=(), get_status=None, search_status=None, unavailable=False):
        self.items = dict(items or {})        # asin -> raw item (getItems)
        self.searches = dict(searches or {})  # keywords -> raw items
        self.budget = budget
        self.errors = set(errors)
        self.throttle_get = throttle_get
        self.throttle_search = set(throttle_search)
        self.get_status = get_status
        self.search_status = dict(search_status or {})
        self.unavailable = unavailable
        self.calls: list[tuple] = []
        self.throttled_remainder: list = []
        self.last_search_meta: dict = {}

    def remaining_budget(self, consumer=""):
        return max(0, self.budget - len(self.calls))

    def budget_ok(self, consumer="", calls=1):
        return self.remaining_budget() >= calls

    def get_items(self, asins, *, resources=None):
        self.calls.append(("getItems", tuple(asins), tuple(resources or ())))
        self.throttled_remainder = []
        if self.unavailable:
            raise CreatorsUnavailable("network down")
        if self.get_status:
            raise CreatorsRequestError(f"HTTP {self.get_status}", status=self.get_status, detail="x")
        if self.throttle_get:
            self.throttled_remainder = list(asins)
            return {}, {}
        return ({a: copy.deepcopy(self.items[a]) for a in asins if a in self.items},
                {a: f"ItemNotAccessible {a}" for a in asins if a in self.errors})

    def search_items(self, keywords, *, search_index="All", item_count=10, item_page=1, **filters):
        self.calls.append(("searchItems", keywords, search_index, item_count))
        self.last_search_meta = {"total_result_count": 0, "throttled": keywords in self.throttle_search}
        if keywords in self.search_status:
            status = self.search_status[keywords]
            raise CreatorsRequestError(f"HTTP {status}", status=status, detail="x")
        if keywords in self.throttle_search:
            return []
        return [copy.deepcopy(i) for i in self.searches.get(keywords, [])][:item_count]


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1
        self.description = None
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        db = self.db
        db.executed.append((sql, copy.deepcopy(params)))
        head = sql.strip().split()[0].upper()
        if head in ("SAVEPOINT", "RELEASE", "ROLLBACK"):
            return
        rows = db.view()
        if sql == KNOWN_SQL:
            self._rows = [(a,) for a in params["asins"] if a in rows]
        elif sql == INSERT_SQL:
            if params["asin"] in rows:
                self.rowcount = 0
            else:
                db.pending[params["asin"]] = {"asin": params["asin"], "kind": params["lane_kind"],
                                              "lane": params["lane"], "model_id": None, "is_active": True,
                                              "category_confidence": params["confidence"],
                                              "discovered_via": params["via"], "discovered_seed": params["seed"]}
                self.rowcount = 1
        elif sql == LINK_SQL:
            if db.link_raises:
                raise db.link_raises
            row = rows.get(params["asin"])
            if row and row["model_id"] is None and row["is_active"]:
                db.pending[params["asin"]] = {**row, "model_id": params["model_id"],
                                              "category_confidence": params["confidence"]}
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif "match_candidates" in sql:
            assert C.MatchingSettings.plain(CANDIDATES_SQL) in sql
            self.description = [("asin",), ("kind",)]
            todo = sorted(a for a, r in rows.items() if r["is_active"] and r["model_id"] is None)
            todo = [a for a in todo if a > params["after"]][:params["limit"]]
            self._rows = [(a, rows[a]["kind"]) for a in todo]
        elif sql == MODELS_SQL:
            cols = sorted({k for r in db.models for k in r})
            self.description = [(c,) for c in cols]
            self._rows = [tuple(r.get(c) for c in cols) for r in db.models]
        elif sql == C.MatchingSettings.plain(STATS_SQL):
            self.description = [("active_products",), ("matched_products",)]
            active = [r for r in rows.values() if r["is_active"]]
            self._rows = [(len(active), sum(1 for r in active if r["model_id"] is not None))]
        else:
            raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, products=(), models=None):
        self.rows = {p["asin"]: {"model_id": None, "is_active": True, "kind": "station",
                                 "category_confidence": 0.45, **p} for p in products}
        self.models = list(models if models is not None else model_rows())
        self.pending: dict = {}
        self.executed: list = []
        self.commits = self.rollbacks = 0
        self.link_raises = None
        self.closed = False

    def view(self):
        return {**self.rows, **self.pending}

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

    def writes(self):
        return [(s, p) for s, p in self.executed if s in (INSERT_SQL, LINK_SQL)]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def match_block(**over):
    block = {
        "enabled": True, "max_calls_per_run": 60, "confidence": 0.9,
        "brand_aliases": {"EcoFlow": ["EF ECOFLOW"], "Anker SOLIX": ["Anker"]},
        "compatible_kinds": {"station": ["station", "kit"], "kit": ["kit"], "battery": ["battery"],
                             "panel": ["panel"], "cable": ["cable"]},
        "models_sql": MODELS_SQL, "candidates_sql": CANDIDATES_SQL, "link_sql": LINK_SQL,
        "stats_sql": STATS_SQL,
        "asin_first": {"enabled": True, "batch": 10},
        "model_first": {"enabled": True, "item_count": 10, "queries_per_run": 50},
    }
    for k, v in over.items():
        if v is None:
            block.pop(k, None)
        elif isinstance(v, dict) and isinstance(block.get(k), dict):
            block[k] = {**block[k], **v}
        else:
            block[k] = v
    return block


def site_doc(**over):
    doc = yaml.safe_load(EXAMPLE.read_text())
    doc["catalog"] = {"dsn_env": "TEST_CATALOG_DSN", "known_sql": KNOWN_SQL, "insert_sql": INSERT_SQL}
    doc.pop("seed_asins", None)
    doc.pop("qa", None)
    doc["model_matching"] = match_block(**over)
    return doc


def build(doc=None, **over):
    return C.build_config(doc or site_doc(**over), base_dir=EXAMPLE.parent, source="test")


def run_phase(cfg, client, conn, *, state=None, allowed=60, dry_run=False, now=NOW, max_new=None):
    state = {} if state is None else state
    cat = K.Catalog(conn, cfg.catalog, run_ts=RUN_TS, dry_run=dry_run)
    rep = MP.MatchPhase(cfg, now=now, run_ts=RUN_TS).run(client, cat, state=state, allowed_calls=allowed,
                                                         max_new=max_new)
    return rep, state


ITEMS = {
    "B0DELTA3P1": raw_item("B0DELTA3P1", "EF ECOFLOW DELTA 3 Plus Portable Power Station, 1024Wh LiFePO4, 1800W",
                           brand="EF ECOFLOW"),
    "B0JACK1KP1": raw_item("B0JACK1KP1", "Jackery Explorer 1000 Plus Portable Power Station, 1264Wh",
                           brand="Jackery"),
    "B0BLUAC180": raw_item("B0BLUAC180", "BLUETTI AC180 vs AC200L Portable Power Station", brand="BLUETTI"),
    "B0NOBRAND1": raw_item("B0NOBRAND1", "Generic Portable Power Station 1000Wh", brand="Zorgo"),
    "B0ANKC1KX1": raw_item("B0ANKC1KX1", "Anker SOLIX C1000X Portable Power Station 1056Wh", brand="Anker"),
}


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_model_matching_block_validates_and_describes():
    cfg = build()
    m = cfg.matching
    assert m.enabled and m.max_calls_per_run == 60 and m.asin_first.batch == 10
    assert m.model_first.queries_per_run == 50 and m.brand_aliases["EcoFlow"] == ("EF ECOFLOW",)
    assert cfg.describe()["model_matching"]["enabled"] is True
    assert C.build_config({k: v for k, v in site_doc().items() if k != "model_matching"},
                          base_dir=EXAMPLE.parent).matching.enabled is False


@pytest.mark.parametrize("over,expect", [
    ({"link_sql": "UPDATE products SET model_id = %(model_id)s WHERE asin = %(asin)s"}, "model_id IS NULL"),
    # the guard must be a top-level AND term of the WHERE, not text anywhere (review 2026-11-22)
    ({"link_sql": "UPDATE products SET model_id = %(model_id)s WHERE asin = %(asin)s OR model_id IS NULL"},
     "model_id IS NULL"),
    ({"link_sql": "UPDATE products SET model_id = %(model_id)s WHERE asin = %(asin)s -- model_id IS NULL"},
     "model_id IS NULL"),
    ({"link_sql": "UPDATE products SET model_id = %(model_id)s WHERE asin = %(asin)s "
                  "AND (model_id IS NULL OR true)"}, "model_id IS NULL"),
    ({"link_sql": "UPDATE products SET model_id = %(model_id)s WHERE asin = %(asin)s "
                  "/* AND model_id IS NULL */"}, "model_id IS NULL"),
    ({"link_sql": "UPDATE products SET model_id = %(model_id)s WHERE asin = %(asin)s "
                  "AND note = 'model_id IS NULL'"}, "model_id IS NULL"),
    ({"link_sql": LINK_SQL.replace("%(confidence)s", "%(title)s")}, "unknown parameter %(title)s"),
    ({"link_sql": "DELETE FROM products WHERE asin = %(asin)s AND model_id IS NULL"}, "one UPDATE"),
    ({"models_sql": "SELECT * FROM m WHERE id = %(x)s"}, "takes no parameters"),
    ({"candidates_sql": "SELECT asin FROM p; DELETE FROM p"}, "single statement"),
    ({"models_sql": None}, "models_sql: required"),
    ({"candidates_sql": None}, "candidates_sql: required"),
    ({"asin_first": {"batch": 11}}, "batch"),
    ({"kind_roles": {"station": "spaceship"}}, "kind_roles"),
    ({"asin_first": {"enabled": False}, "model_first": {"enabled": False}}, "neither asin_first"),
    ({"typo_key": 1}, "unknown key(s): typo_key"),
    ({"model_fields": {"enabled": True, "fields": ["model", "warranty"]}}, "warranty"),
    ({"model_fields": {"enabled": True, "fields": []}}, "model_fields/fields"),
    ({"model_fields": {"enabled": True, "typo": 1}}, "unknown key(s): typo"),
    ({"brand_census": {"enabled": True}}, "brand_census.keys: required when brand_census is enabled"),
    ({"brand_census": {"keys": ["None"]}}, "'none' is the count"),
    ({"brand_census": {"keys": ["EcoFlow", "eco-flow"]}}, "the same after folding"),
    ({"brand_census": {"keys": ["--"]}}, "no letters or digits"),
])
def test_model_matching_config_errors(over, expect):
    with pytest.raises(C.ConfigError) as err:
        build(**over)
    assert expect in str(err.value)


def test_model_fields_and_brand_census_default_off():
    m = build().matching
    assert not m.model_fields.enabled and m.model_fields.fields == ("model", "itemPartNumber")
    assert not m.brand_census.enabled and m.brand_census.keys == ()
    d = build(model_fields={"enabled": True, "fields": ["itemPartNumber"]},
              brand_census={"enabled": True, "keys": ["EcoFlow", "Jackery"]}).describe()["model_matching"]
    assert d["model_fields"] == {"enabled": True, "fields": ["itemPartNumber"]}
    assert d["brand_census"] == {"enabled": True, "keys": 2}


def test_budget_allowance_takes_the_phase_cap():
    cfg = build()
    client = FakeCreators(budget=25)
    assert E.budget_allowance(cfg, client, cap=60).allowed_calls == 25
    assert E.budget_allowance(cfg, client, cap=60, max_calls=3).allowed_calls == 3
    assert E.budget_allowance(cfg, client).max_calls_per_run == cfg.limits.max_calls_per_run


# ---------------------------------------------------------------------------
# ASIN-first
# ---------------------------------------------------------------------------

def test_asin_first_links_only_confirmed_matches():
    products = [{"asin": a} for a in ITEMS] + [{"asin": "B0MISSING1"}, {"asin": "B0GONE0001"}]
    conn = FakeConn(products)
    client = FakeCreators(ITEMS, errors={"B0GONE0001"})
    rep, state = run_phase(build(model_first={"enabled": False}), client, conn)
    linked = {a: r["model_id"] for a, r in conn.rows.items() if r["model_id"]}
    assert linked == {"B0DELTA3P1": "ecoflow-delta-3-plus", "B0JACK1KP1": "jackery-explorer-1000-plus",
                      "B0ANKC1KX1": "anker-solix-c1000x"}
    assert all(conn.rows[a]["category_confidence"] == 0.9 for a in linked)
    assert conn.rows["B0BLUAC180"]["category_confidence"] == 0.45   # never raised without a match
    assert (rep.linked, rep.ambiguous, rep.calls) == (3, 1, 1)
    assert rep.reasons["brand-unknown"] == 1 and rep.reasons["not-accessible"] == 1
    assert rep.reasons["not-returned"] == 1
    assert client.calls[0][0] == "getItems" and client.calls[0][2] == MP.MATCH_RESOURCES
    assert len(client.calls[0][1]) == 7
    assert all(set(r) == {"asin", "model_id", "rule", "confidence"} for r in rep.rows)
    assert state["match"]["asin_cursor"] == ""      # read to the end
    assert rep.asin_first_state == "cycle-complete"


def test_asin_first_never_overwrites_an_existing_link():
    conn = FakeConn([{"asin": "B0DELTA3P1"}])
    conn.rows["B0DELTA3P1"]["model_id"] = "ecoflow-delta-3"   # an operator's link
    rep, _ = run_phase(build(model_first={"enabled": False}), FakeCreators(ITEMS), conn)
    assert conn.rows["B0DELTA3P1"]["model_id"] == "ecoflow-delta-3"
    assert rep.calls == 0 and rep.linked == 0   # not a candidate at all

    # Linked by someone else between the candidate read and the UPDATE: a no-op.
    conn = FakeConn([{"asin": "B0DELTA3P1"}])
    phase = MP.MatchPhase(build(model_first={"enabled": False}), now=NOW, run_ts=RUN_TS)
    original = MP.MatchStore.candidates

    def racing(self, **kw):
        rows = original(self, **kw)
        conn.rows["B0DELTA3P1"]["model_id"] = "ecoflow-delta-3"
        return rows
    MP.MatchStore.candidates = racing
    try:
        rep = phase.run(FakeCreators(ITEMS), K.Catalog(conn, phase.cfg.catalog, run_ts=RUN_TS),
                        state={}, allowed_calls=5)
    finally:
        MP.MatchStore.candidates = original
    assert conn.rows["B0DELTA3P1"]["model_id"] == "ecoflow-delta-3" and rep.link_noop == 1 and rep.linked == 0


def test_asin_first_never_links_a_title_that_hits_a_global_reject():
    items = {"B0CPAPST01": raw_item("B0CPAPST01", "EF ECOFLOW DELTA 3 Plus Power Station for CPAP, 1024Wh",
                                    brand="EF ECOFLOW")}
    conn = FakeConn([{"asin": "B0CPAPST01"}])
    cfg = C.build_config({**site_doc(model_first={"enabled": False}),
                          "reject_patterns": [{"id": "medical", "pattern": "\\bc-?pap\\b"}]},
                         base_dir=EXAMPLE.parent)
    rep, _ = run_phase(cfg, FakeCreators(items), conn)
    assert rep.linked == 0 and rep.reasons["global-reject:medical"] == 1
    assert conn.rows["B0CPAPST01"]["model_id"] is None and conn.rows["B0CPAPST01"]["category_confidence"] == 0.45


def test_asin_first_kind_check():
    conn = FakeConn([{"asin": "B0DELTA3P1", "kind": "cable"}])
    rep, _ = run_phase(build(model_first={"enabled": False}), FakeCreators(ITEMS), conn)
    assert rep.linked == 0 and rep.reasons["kind-mismatch"] == 1 and conn.rows["B0DELTA3P1"]["model_id"] is None


def test_asin_first_never_links_an_accessory_renewed_or_multi_pack_seed():
    """Review 2026-11-22: an untitled seed (0.45, never through the lanes) whose
    listing is a case, a bag, a renewed unit or a two-pack must never be linked
    to the station and raised to 0.9. The lane rules screen it, and the matcher
    refuses it when the title still reads as in-lane."""
    titles = {
        "B0CASE0001": ("BLUETTI", "BLUETTI AC180 Carrying Case"),
        "B0CASE0002": ("BLUETTI", "BLUETTI AC180 Portable Power Station Carrying Case, Protective Bag"),
        "B0BAG00001": ("EcoFlow", "EF ECOFLOW DELTA 2 Max Waterproof Bag"),
        "B0RENEW001": ("EcoFlow", "EF ECOFLOW DELTA 2 Max Portable Power Station 2048Wh (Renewed)"),
        "B0TWOPK001": ("Jackery", "Jackery Explorer 1000 Plus Portable Power Station 1264Wh, 2-Pack"),
        "B0BATT0001": ("EcoFlow", "EF ECOFLOW DELTA 2 Max 1200W Portable Power Station"),
    }
    items = {a: raw_item(a, t, brand=b) for a, (b, t) in titles.items()}
    conn = FakeConn([{"asin": a, "kind": None} for a in titles])
    rep, _ = run_phase(build(model_first={"enabled": False}), FakeCreators(items), conn)
    assert rep.linked == 0 and all(r["model_id"] is None for r in conn.rows.values())
    assert all(r["category_confidence"] == 0.45 for r in conn.rows.values())
    # the three accessories: refused by the lane rules here (the matcher would refuse them too)
    assert sum(n for k, n in rep.reasons.items() if k.startswith("lane:")) == 3
    assert rep.reasons["condition"] == 1 and rep.reasons["multi-pack"] == 1
    assert rep.reasons["wattage-mismatch"] == 1      # 'DELTA 2 Max 1200W': the 'Max' no longer hides it
    assert SENTINEL not in json.dumps(rep.to_dict())
    # Defence in depth: with a lane that takes anything, the matcher alone refuses the accessories.
    doc = site_doc(model_first={"enabled": False})
    doc["lanes"] = [{"id": "station", "kind": "station", "category": "power-stations", "phase": "launch",
                     "include_all": ["."]}]
    conn = FakeConn([{"asin": a, "kind": None} for a in titles])
    rep, _ = run_phase(C.build_config(doc, base_dir=EXAMPLE.parent), FakeCreators(items), conn)
    assert rep.linked == 0 and rep.reasons["accessory"] == 3


def test_asin_first_lane_kind_must_fit_the_model():
    """A row with no kind whose title lands in another lane (a cable named after
    a station) is refused by the lane's kind even if the matcher found a model."""
    items = {"B0CBL00001": raw_item("B0CBL00001", "EF ECOFLOW DELTA 3 Plus Portable Power Station 1024Wh",
                                    brand="EF ECOFLOW")}
    conn = FakeConn([{"asin": "B0CBL00001", "kind": None}])
    cfg = build(model_first={"enabled": False}, compatible_kinds={"station": ["kit"]})
    rep, _ = run_phase(cfg, FakeCreators(items), conn)
    assert rep.linked == 0 and rep.reasons["kind-mismatch"] == 1


def test_model_first_marks_a_model_searched_only_after_its_listings_commit(monkeypatch):
    """Review 2026-11-22: a failure while a model's listings are handled rolls
    them back, and the model must be searched again next run, not in 168 h."""
    def broken(self, *a, **k):
        raise RuntimeError(f"boom {SENTINEL}")
    monkeypatch.setattr(MP.MatchPhase, "_listing", broken)
    rows = model_rows(linked=[m for m in MODEL_IDS if m != "bluetti-ac180p"])
    conn = FakeConn(models=rows)
    rep, state = run_phase(build(asin_first={"enabled": False}), FakeCreators(searches=SEARCHES), conn)
    assert rep.failure == "model matching stopped by RuntimeError" and conn.rollbacks >= 1
    assert "bluetti-ac180p" not in state["match"].get("models_queried", {})
    assert SENTINEL not in json.dumps(rep.to_dict())


def test_asin_first_resumes_from_its_cursor_and_waits_after_a_full_pass():
    items = {f"B0RESUME{i:02d}": raw_item(f"B0RESUME{i:02d}", "EcoFlow DELTA 2 Portable Power Station 1024Wh",
                                          brand="EcoFlow") for i in range(25)}
    conn = FakeConn([{"asin": a} for a in items])
    cfg = build(model_first={"enabled": False}, asin_first={"max_calls": 2})
    client = FakeCreators(items)
    rep, state = run_phase(cfg, client, conn)
    assert rep.calls == 2 and rep.linked == 20 and state["match"]["asin_cursor"] == "B0RESUME19"
    client2 = FakeCreators(items)
    rep2, state = run_phase(cfg, client2, conn, state=state)
    assert client2.calls[0][1] == ("B0RESUME20", "B0RESUME21", "B0RESUME22", "B0RESUME23", "B0RESUME24")
    assert rep2.linked == 5 and rep2.asin_first_state == "cycle-complete"
    assert state["match"]["asin_cycle_done_at"] == "2026-11-22T12:00:00Z"
    conn.rows["B0LATE0001"] = {"asin": "B0LATE0001", "model_id": None, "is_active": True, "kind": "station"}
    rep3, _ = run_phase(cfg, FakeCreators(items), conn, state=state, now=NOW + dt.timedelta(hours=1))
    assert rep3.calls == 0 and rep3.asin_first_state == "cooldown"
    rep4, _ = run_phase(cfg, FakeCreators(items), conn, state=state, now=NOW + dt.timedelta(hours=169))
    assert rep4.calls == 1 and rep4.reasons["not-returned"] == 1


def test_asin_first_throttle_server_and_client_errors():
    conn = FakeConn([{"asin": a} for a in ITEMS])
    cfg = build(model_first={"enabled": False})
    rep, state = run_phase(cfg, FakeCreators(ITEMS, throttle_get=True), conn)
    assert rep.stopped_reason == "throttled" and "asin_cursor" not in state["match"] and rep.linked == 0
    rep, state = run_phase(cfg, FakeCreators(ITEMS, get_status=503), conn)
    assert rep.stopped_reason == "server-errors" and rep.transient_errors == 1 and not state["match"].get("asin_cursor")
    rep, state = run_phase(cfg, FakeCreators(ITEMS, get_status=400), conn)
    assert rep.request_errors == 1 and state["match"]["asin_cursor"] == ""   # moved past, then the end
    rep, _ = run_phase(cfg, FakeCreators(ITEMS, unavailable=True), conn)
    assert rep.failure.startswith("Creators unavailable") and "network down" not in rep.failure


# ---------------------------------------------------------------------------
# Model-first
# ---------------------------------------------------------------------------

SEARCHES = {
    "EcoFlow DELTA 3 Max Plus": [
        raw_item("B0D3MP0001", "EF ECOFLOW DELTA 3 Max Plus Portable Power Station 2048Wh, 3000W",
                 brand="EF ECOFLOW", price=1499.0, nodes=["348967011"]),
        raw_item("B0D3MP0002", "EF ECOFLOW DELTA 3 Max Plus Power Station 2048Wh Home Backup",
                 brand="EF ECOFLOW", price=1449.0),
        raw_item("B0D3MP0003", "EF ECOFLOW DELTA 3 Max Plus Solar Generator with 2x100W Rigid Solar Panels, 2048Wh",
                 brand="EF ECOFLOW", price=1899.0),
        raw_item("B0D3MPCHEP", "EF ECOFLOW DELTA 3 Max Plus Power Station 2048Wh", brand="EF ECOFLOW",
                 price=0.5),
        raw_item("B0D3MPGAS1", "DELTA 3 Max Plus dual fuel gas generator kit", brand="EF ECOFLOW", price=900.0),
    ],
    "BLUETTI AC180P": [
        raw_item("B0AC180P01", "BLUETTI AC180P Portable Power Station, 1440Wh LiFePO4", brand="BLUETTI",
                 price=699.0),
    ],
    "Jackery Explorer 1000 v2": [
        raw_item("B0J1KV2001", "Jackery Explorer 1000 v2 Portable Power Station 1070Wh, 1500W", brand="Jackery",
                 price=449.0),
    ],
}


def test_model_first_inserts_and_links_in_priority_order():
    linked = [m for m in MODEL_IDS if m not in ("ecoflow-delta-3-max-plus", "bluetti-ac180p",
                                                 "jackery-explorer-1000-v2",
                                                 "ecoflow-delta-3-max-plus-solar-generator-2-x-100w-rigid-solar-panel")]
    rows = model_rows(linked=linked, priority={"bluetti-ac180p": 1, "ecoflow-delta-3-max-plus": 2,
                                               "jackery-explorer-1000-v2": 3})
    conn = FakeConn(models=rows)
    conn.rows["B0J1KV2001"] = {"asin": "B0J1KV2001", "model_id": None, "is_active": True, "kind": "station",
                               "lane": "station"}
    client = FakeCreators(searches=SEARCHES)
    rep, state = run_phase(build(asin_first={"enabled": False}), client, conn)
    searched = [c[1] for c in client.calls]
    assert searched[:3] == ["BLUETTI AC180P", "EcoFlow DELTA 3 Max Plus", "Jackery Explorer 1000 v2"]
    got = {a: (r["model_id"], r.get("lane")) for a, r in conn.rows.items() if r["model_id"]}
    assert got == {
        "B0AC180P01": ("bluetti-ac180p", "station"),
        "B0D3MP0001": ("ecoflow-delta-3-max-plus", "station"),          # one product per model per run
        "B0D3MP0003": ("ecoflow-delta-3-max-plus-solar-generator-2-x-100w-rigid-solar-panel", "kit"),
        "B0J1KV2001": ("jackery-explorer-1000-v2", "station"),          # known product: linked, not inserted
    }
    assert "B0D3MP0002" not in conn.rows and rep.reasons["model-has-product"] == 1
    assert "B0D3MPCHEP" not in conn.rows and rep.reasons["pricing:price-below-min"] == 1
    assert "B0D3MPGAS1" not in conn.rows and rep.reasons["global-reject:fuel-generator"] == 1
    assert (rep.inserted, rep.linked, rep.linked_model_first) == (3, 4, 4)
    assert conn.rows["B0D3MP0001"]["discovered_via"] == "match:station/model-first"
    assert rep.models_with_product == len(linked) + 4
    assert set(state["match"]["models_queried"]) >= {"bluetti-ac180p", "ecoflow-delta-3-max-plus"}
    # Searched models wait retry_hours; a model with a product is never searched.
    client2 = FakeCreators(searches=SEARCHES)
    rep2, _ = run_phase(build(asin_first={"enabled": False}), client2,
                        FakeConn(models=model_rows(linked=linked)), state=state)
    assert not {c[1] for c in client2.calls} & set(searched) and rep2.models_waiting >= 3


def test_model_first_variants_and_unsearchable_models():
    rows = model_rows(linked=[m for m in MODEL_IDS if m != "ecoflow-delta-3-max-plus"],
                      variants={"ecoflow-delta-3-max-plus": 2})
    conn = FakeConn(models=rows)
    rep, _ = run_phase(build(asin_first={"enabled": False}), FakeCreators(searches=SEARCHES), conn)
    assert {a for a, r in conn.rows.items() if r["model_id"] == "ecoflow-delta-3-max-plus"} == \
        {"B0D3MP0001", "B0D3MP0002"}
    rows = model_rows(searchable={m: False for m in MODEL_IDS})
    client = FakeCreators(searches=SEARCHES)
    rep, _ = run_phase(build(asin_first={"enabled": False}), client, FakeConn(models=rows))
    assert client.calls == [] and rep.calls == 0


def test_model_first_throttle_and_errors():
    rows = model_rows(linked=[m for m in MODEL_IDS if m not in ("bluetti-ac180p", "ecoflow-delta-3-max-plus")],
                      priority={"bluetti-ac180p": 1, "ecoflow-delta-3-max-plus": 2})
    rep, state = run_phase(build(asin_first={"enabled": False}),
                           FakeCreators(searches=SEARCHES, throttle_search={"BLUETTI AC180P"}), FakeConn(models=rows))
    assert rep.stopped_reason == "throttled" and "bluetti-ac180p" not in state["match"].get("models_queried", {})
    rep, state = run_phase(build(asin_first={"enabled": False}),
                           FakeCreators(searches=SEARCHES, search_status={"BLUETTI AC180P": 400}),
                           FakeConn(models=rows))
    assert rep.request_errors == 1 and "bluetti-ac180p" in state["match"]["models_queried"] and rep.linked == 1
    rep, state = run_phase(build(asin_first={"enabled": False}),
                           FakeCreators(searches=SEARCHES, search_status={"BLUETTI AC180P": 502}),
                           FakeConn(models=rows))
    assert rep.transient_errors == 1 and "bluetti-ac180p" not in state["match"]["models_queried"]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

def test_allowance_and_budget_are_never_exceeded():
    items = {f"B0BUDGET{i:02d}": raw_item(f"B0BUDGET{i:02d}", "EcoFlow DELTA 2 Power Station", brand="EcoFlow")
             for i in range(50)}
    rows = model_rows()
    for allowed in (0, 1, 3):
        client = FakeCreators(items, searches=SEARCHES)
        rep, _ = run_phase(build(asin_first={"batch": 5}), client, FakeConn([{"asin": a} for a in items],
                                                                            models=rows), allowed=allowed)
        assert len(client.calls) == rep.calls <= allowed
        assert rep.stopped_reason == ("no-allowance" if allowed == 0 else "max-calls")
    client = FakeCreators(items, searches=SEARCHES, budget=2)
    rep, state = run_phase(build(asin_first={"batch": 5}), client, FakeConn([{"asin": a} for a in items]))
    assert len(client.calls) == 2 and rep.stopped_reason == "budget-exhausted"
    assert state["match"]["asin_cursor"] == "B0BUDGET09"   # resumable


def test_asin_first_max_calls_leaves_room_for_model_first():
    items = {f"B0SHARE0{i:02d}": raw_item(f"B0SHARE0{i:02d}", "EcoFlow DELTA 2", brand="EcoFlow")
             for i in range(40)}
    rows = model_rows(linked=[m for m in MODEL_IDS if m != "bluetti-ac180p"])
    client = FakeCreators(items, searches=SEARCHES)
    rep, _ = run_phase(build(asin_first={"batch": 10, "max_calls": 2}), client,
                       FakeConn([{"asin": a} for a in items], models=rows), allowed=5)
    assert [c[0] for c in client.calls] == ["getItems", "getItems", "searchItems"]
    assert (rep.asin_first_calls, rep.model_first_calls) == (2, 1)


# ---------------------------------------------------------------------------
# The agent run: report, metrics, dry run, modes, and nothing Amazon wrote leaves memory
# ---------------------------------------------------------------------------

@pytest.fixture
def make_agent(storage, tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_DSN", "postgresql://user:sekret@db.invalid/catalog")
    monkeypatch.delenv("AGENT_DEFAULT_OWNER_EMAIL", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    made = []

    def make(doc, *, client=None, conn=None, run_ts=RUN_TS):
        path = tmp_path / "site.yaml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        a = agent_mod.AmazonCatalogDiscoveryAgent(storage=storage, run_ts=run_ts)
        a.config_path = str(path)
        a.send_run_summary_email = False
        a.auto_recovery_enabled = False
        a._connect = lambda dsn, **kw: conn
        a._make_client = lambda cfg: client
        a._now = lambda: NOW
        made.append(a)
        return a

    yield make
    for a in made:
        a._stop_heartbeat()


def _storage_text(storage) -> str:
    root = Path(storage.root)
    return "\n".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


class SearchOnly(FakeCreators):
    """Search slots of the discovery phase answer with titled listings too."""

    def search_items(self, keywords, **kw):
        if keywords in self.searches:
            return super().search_items(keywords, **kw)
        self.calls.append(("searchItems", keywords, kw.get("search_index"), kw.get("item_count")))
        self.last_search_meta = {"total_result_count": 0, "throttled": False}
        return [raw_item("B0SRCH0001", "EcoFlow DELTA 2 Portable Power Station 1024Wh", brand="EcoFlow",
                         nodes=["348967011"])] if kw.get("item_page", 1) == 1 else []


def test_agent_run_end_to_end_keeps_every_title_in_memory(make_agent, storage, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    doc = site_doc(asin_first={"batch": 3})
    doc["limits"] = {**doc.get("limits", {}), "max_calls_per_run": 2}
    rows = model_rows(linked=[m for m in MODEL_IDS if m not in ("bluetti-ac180p", "ecoflow-delta-3-max-plus")],
                      priority={"bluetti-ac180p": 1})
    conn = FakeConn([{"asin": a} for a in ITEMS], models=rows)
    client = SearchOnly(ITEMS, searches=SEARCHES)
    agent = make_agent(doc, client=client, conn=conn)
    result = agent.run_once()
    assert result.status == "success", result.summary
    m = result.metrics
    for key in ("models_with_product", "products_model_matched_pct", "match_ambiguous", "match_calls_per_link",
                "match_linked", "match_calls"):
        assert key in m, key
    assert m["budget_overrun_calls"] == 0 and m["match_calls"] <= 60
    assert m["match_linked"] >= 5 and m["match_ambiguous"] >= 1
    assert "Model matching:" in result.summary
    report = storage.read_json(f"agents/{agent.agent_id}/runs/{RUN_TS}/discovery-report.json")
    sec = report["match"]
    assert set(sec["counts"]) >= {"checked", "matched", "ambiguous", "unmatched", "rejected", "calls"}
    assert sec["rows"] and all(set(r) == {"asin", "model_id", "rule", "confidence"} for r in sec["rows"])
    assert result.next_state["match"]["models_queried"]
    # The sentinel (and the brand field) never left memory.
    stored = _storage_text(storage)
    assert SENTINEL not in stored and "EF ECOFLOW" not in stored and "Zorgo" not in stored
    for sql, params in conn.executed:
        assert SENTINEL not in sql + json.dumps(params, default=str)
    assert SENTINEL not in json.dumps({**conn.rows, **conn.pending}, default=str)
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err
    assert SENTINEL not in caplog.text
    assert SENTINEL not in json.dumps(result.__dict__, default=str)


def test_a_failure_with_title_text_is_reported_by_class_only(make_agent, storage, caplog):
    caplog.set_level(logging.DEBUG)
    conn = FakeConn([{"asin": "B0DELTA3P1"}])
    conn.link_raises = ValueError(f"row failed: EF ECOFLOW DELTA 3 Plus {SENTINEL}")
    agent = make_agent(site_doc(model_first={"enabled": False}), client=FakeCreators(ITEMS), conn=conn)
    result = agent.run_once()
    assert result.status == "success"   # a row error is counted, the run goes on
    assert result.metrics["match_errors"] == 1
    assert SENTINEL not in _storage_text(storage) and SENTINEL not in caplog.text

    def boom(*a, **k):
        raise RuntimeError(f"matcher exploded on {SENTINEL}")
    conn = FakeConn([{"asin": "B0DELTA3P1"}])
    agent = make_agent(site_doc(model_first={"enabled": False}), client=FakeCreators(ITEMS), conn=conn,
                       run_ts="20261122T130000Z")
    original = MP.MatchPhase._asin_first
    MP.MatchPhase._asin_first = boom
    try:
        result = agent.run_once()
    finally:
        MP.MatchPhase._asin_first = original
    assert result.status == "failure" and "model matching stopped by RuntimeError" in result.summary
    assert SENTINEL not in _storage_text(storage) and SENTINEL not in (result.error_text or "")


def test_matcher_errors_are_counted_not_raised(monkeypatch):
    from framework.core import model_matcher as mm

    def broken(*a, **k):
        raise KeyError(f"title {SENTINEL}")
    monkeypatch.setattr(mm, "match", broken)
    rep, _ = run_phase(build(model_first={"enabled": False}), FakeCreators(ITEMS),
                       FakeConn([{"asin": "B0DELTA3P1"}]))
    assert rep.errors == 1 and rep.reasons["matcher-error"] == 1 and SENTINEL not in json.dumps(rep.to_dict())


def test_dry_run_computes_matches_and_writes_nothing(make_agent, storage, capsys):
    conn = FakeConn([{"asin": a} for a in ITEMS],
                    models=model_rows(linked=[m for m in MODEL_IDS if m != "bluetti-ac180p"]))
    agent = make_agent(site_doc(), client=FakeCreators(ITEMS, searches=SEARCHES), conn=conn)
    agent.dry_run = True
    agent.mode = "match-only"
    result, report = agent.execute()
    assert result.status == "success"
    assert conn.writes() == [] and conn.commits == 0
    assert report["match"]["counts"]["linked"] == 4
    # 3 via getItems + 1 new product found by search: 4 of 6 active rows
    assert result.metrics["products_model_matched_pct"] == round(100 * 4 / 6, 2)
    assert all(r["model_id"] is None for r in conn.rows.values())
    assert SENTINEL not in capsys.readouterr().out


def test_modes_match_only_and_no_match(make_agent):
    conn = FakeConn([{"asin": "B0DELTA3P1"}])
    client = SearchOnly(ITEMS)
    agent = make_agent(site_doc(model_first={"enabled": False}), client=client, conn=conn)
    agent.mode = "match-only"
    result = agent.run()
    assert [c[0] for c in client.calls] == ["getItems"] and "new_in_lane_asins" not in result.metrics
    client = SearchOnly(ITEMS)
    agent = make_agent(site_doc(), client=client, conn=FakeConn([{"asin": "B0DELTA3P1"}]),
                       run_ts="20261122T130000Z")
    agent.mode = "no-match"
    result = agent.run()
    assert "getItems" not in [c[0] for c in client.calls] and "match_calls" not in result.metrics


def test_cli_accepts_the_match_flags(capsys, tmp_path):
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(site_doc(), sort_keys=False))
    assert agent_mod.main(["--config", str(path), "--validate"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model_matching"]["enabled"] is True
    with pytest.raises(SystemExit):
        agent_mod.main(["--config", str(path), "--match-only", "--no-match"])


def test_match_code_names_no_site():
    from framework.core import site_registry
    names = {s["name"] for s in site_registry.read_defaults()["sites"]}
    pattern = re.compile("|".join(re.escape(n) for n in names), re.I)
    for path in (AGENT_DIR / "catalog_discovery" / "matching.py", _ROOT / "framework/core/model_matcher.py",
                 Path(__file__)):
        assert not pattern.search(path.read_text()), path


# ---------------------------------------------------------------------------
# 2026-11-22: manufacturer model fields, their refusal, the brand census, and
# bundle signatures on bundle-no-kit rows. Every field value carries a sentinel.
# ---------------------------------------------------------------------------

FIELD_SENTINEL = "QZXFIELD8"
BRAND_SENTINEL = "QZXBRAND9"


def raw_item_mfr(asin, title, *, brand, model=None, part=None, **kw):
    """A Creators item that also carries itemInfo.manufactureInfo."""
    it = raw_item(asin, title, brand=brand, **kw)
    info = {}
    if model is not None:
        info["model"] = {"displayValue": model, "label": "Model", "locale": "en_US"}
    if part is not None:
        info["itemPartNumber"] = {"displayValue": part, "label": "Part Number", "locale": "en_US"}
    it["itemInfo"]["manufactureInfo"] = info
    return it


class ResourceCreators(FakeCreators):
    """Records the resources each call asks for; with ``refuse_fields`` it answers
    a call that asks for itemInfo.manufactureInfo with Creators' 400 for an
    unknown resource."""

    def __init__(self, *a, refuse_fields=False, **k):
        super().__init__(*a, **k)
        self.resources: list = []
        self.refuse_fields = refuse_fields

    def _refuse(self, resources):
        if self.refuse_fields and MP.MANUFACTURE_INFO in (resources or ()):
            raise CreatorsRequestError("HTTP 400", status=400,
                                       detail="InvalidParameterValue: The value [itemInfo.manufactureInfo] "
                                              "provided in the request for Resources is invalid.")

    def get_items(self, asins, *, resources=None):
        self.resources.append(("getItems", tuple(resources or ())))
        if self.refuse_fields and MP.MANUFACTURE_INFO in (resources or ()):
            self.calls.append(("getItems", tuple(asins), tuple(resources or ())))
            self._refuse(resources)
        return super().get_items(asins, resources=resources)

    def search_items(self, keywords, *, resources=None, **kw):
        self.resources.append(("searchItems", tuple(resources or ())))
        if self.refuse_fields and MP.MANUFACTURE_INFO in (resources or ()):
            self.calls.append(("searchItems", keywords, kw.get("search_index"), kw.get("item_count")))
            self._refuse(resources)
        return super().search_items(keywords, **kw)


FIELD_ITEMS = {
    # no designation in the title: only the part number names the DELTA 2
    "B0MFRFLD01": raw_item_mfr("B0MFRFLD01", "EF ECOFLOW Portable Power Station 1024Wh LiFePO4, 1800W AC Output",
                               brand="EF ECOFLOW", model=f"{FIELD_SENTINEL} 1800W", part="EFD330"),
    # the field names the DELTA 2, the title's capacity does not
    "B0MFRFLD02": raw_item_mfr("B0MFRFLD02", "EF ECOFLOW Portable Power Station 2048Wh LiFePO4",
                               brand="EF ECOFLOW", model=f"EFD330 {FIELD_SENTINEL}"),
    # an accessory whose seller copied the station's model
    "B0MFRFLD03": raw_item_mfr("B0MFRFLD03", "EF ECOFLOW Waterproof Bag for Portable Power Station",
                               brand="EF ECOFLOW", model="EFD330"),
}


def test_model_fields_link_a_title_without_a_designation_and_stay_in_memory(caplog):
    caplog.set_level(logging.DEBUG)
    conn = FakeConn([{"asin": a, "kind": None} for a in FIELD_ITEMS])
    client = ResourceCreators(FIELD_ITEMS)
    rep, state = run_phase(build(model_first={"enabled": False}, model_fields={"enabled": True}), client, conn)
    assert client.resources == [("getItems", MP.MATCH_RESOURCES + (MP.MANUFACTURE_INFO,))]
    assert {a: r["model_id"] for a, r in conn.rows.items() if r["model_id"]} == {"B0MFRFLD01": "ecoflow-delta-2"}
    row = next(r for r in rep.rows if r["asin"] == "B0MFRFLD01")
    assert row == {"asin": "B0MFRFLD01", "model_id": "ecoflow-delta-2", "rule": "model-field", "confidence": 0.91}
    assert conn.rows["B0MFRFLD01"]["category_confidence"] == 0.9
    assert rep.reasons["model-field"] == 1 and rep.reasons["capacity-mismatch"] == 1
    assert sum(n for k, n in rep.reasons.items() if k.startswith("lane:")) + rep.reasons["accessory"] == 1
    assert rep.to_dict()["model_fields"] == {"enabled": True, "resource_refused": 0}
    blob = json.dumps(rep.to_dict()) + json.dumps(state) + json.dumps(conn.executed, default=str)
    assert FIELD_SENTINEL not in blob and SENTINEL not in blob and FIELD_SENTINEL not in caplog.text


def test_model_fields_off_ask_for_and_read_nothing_more():
    conn = FakeConn([{"asin": a, "kind": None} for a in FIELD_ITEMS])
    client = ResourceCreators(FIELD_ITEMS)
    rep, _ = run_phase(build(model_first={"enabled": False}), client, conn)
    assert client.resources == [("getItems", MP.MATCH_RESOURCES)]
    assert rep.linked == 0 and rep.reasons["no-designation"] >= 1
    assert rep.to_dict()["model_fields"] == {"enabled": False, "resource_refused": 0}


def test_model_first_reads_the_fields_of_search_results():
    searches = {"EcoFlow DELTA 2": [
        raw_item_mfr("B0MFRSRCH1", "EF ECOFLOW Portable Power Station 1024Wh LiFePO4, 1800W AC Output",
                     brand="EF ECOFLOW", model="DELTA 2", part=f"{FIELD_SENTINEL}")]}
    rows = model_rows(linked=[m for m in MODEL_IDS if m != "ecoflow-delta-2"])
    conn = FakeConn(models=rows)
    client = ResourceCreators(searches=searches)
    rep, state = run_phase(build(asin_first={"enabled": False}, model_fields={"enabled": True}), client, conn)
    assert client.resources == [("searchItems", amazon_creators.SEARCH_RESOURCES + (MP.MANUFACTURE_INFO,))]
    assert conn.rows["B0MFRSRCH1"]["model_id"] == "ecoflow-delta-2" and rep.inserted == 1
    assert rep.rows[-1]["rule"] == "model-field"
    assert FIELD_SENTINEL not in json.dumps(rep.to_dict()) + json.dumps(state)


def test_a_refused_manufacture_info_resource_is_dropped_for_the_run():
    rows = model_rows(linked=[m for m in MODEL_IDS if m != "bluetti-ac180p"])
    conn = FakeConn([{"asin": "B0DELTA3P1"}], models=rows)
    client = ResourceCreators(ITEMS, searches=SEARCHES, refuse_fields=True)
    rep, state = run_phase(build(model_fields={"enabled": True}), client, conn)
    assert client.resources == [("getItems", MP.MATCH_RESOURCES + (MP.MANUFACTURE_INFO,)),
                                ("getItems", MP.MATCH_RESOURCES),
                                ("searchItems", amazon_creators.SEARCH_RESOURCES)]
    assert rep.calls == len(client.calls) == 3 and rep.request_errors == 0
    assert rep.to_dict()["model_fields"] == {"enabled": True, "resource_refused": 1}
    assert conn.rows["B0DELTA3P1"]["model_id"] == "ecoflow-delta-3-plus"
    assert conn.rows["B0AC180P01"]["model_id"] == "bluetti-ac180p"
    assert "bluetti-ac180p" in state["match"]["models_queried"]
    # model-first alone: the same model is searched again without the fields
    client = ResourceCreators(searches=SEARCHES, refuse_fields=True)
    rep, state = run_phase(build(asin_first={"enabled": False}, model_fields={"enabled": True}), client,
                           FakeConn(models=rows))
    assert [c[1] for c in client.calls] == ["BLUETTI AC180P", "BLUETTI AC180P"] and rep.linked == 1
    # no call left for the repeat: the model waits for the next run, not retry_hours
    rep, state = run_phase(build(asin_first={"enabled": False}, model_fields={"enabled": True}),
                           ResourceCreators(searches=SEARCHES, refuse_fields=True), FakeConn(models=rows),
                           allowed=1)
    assert rep.calls == 1 and rep.stopped_reason == "max-calls"
    assert "bluetti-ac180p" not in state["match"].get("models_queried", {})


def test_other_request_errors_are_not_taken_for_a_refused_resource():
    rows = model_rows(linked=[m for m in MODEL_IDS if m != "bluetti-ac180p"])
    rep, state = run_phase(build(asin_first={"enabled": False}, model_fields={"enabled": True}),
                           FakeCreators(searches=SEARCHES, search_status={"BLUETTI AC180P": 400}),
                           FakeConn(models=rows))
    assert rep.request_errors == 1 and rep.model_fields_refused == 0
    assert "bluetti-ac180p" in state["match"]["models_queried"]


def test_brand_census_counts_our_keys_only():
    title = "Portable Power Station 1024Wh LiFePO4, 1800W AC Output"
    items = {
        "B0CENSUS01": raw_item("B0CENSUS01", title, brand=f"EcoFlow Official Store {BRAND_SENTINEL}"),
        "B0CENSUS02": raw_item("B0CENSUS02", title, brand=f"Zorgo {BRAND_SENTINEL}"),
        "B0CENSUS03": raw_item("B0CENSUS03", title, brand=f"Jackery-EcoFlow {BRAND_SENTINEL}"),
        "B0DELTA3P1": ITEMS["B0DELTA3P1"],
    }
    conn = FakeConn([{"asin": a} for a in items])
    cfg = build(model_first={"enabled": False},
                brand_census={"enabled": True, "keys": ["EcoFlow", "Jackery", "BLUETTI"]})
    rep, state = run_phase(cfg, FakeCreators(items), conn)
    assert rep.to_dict()["brand_census"] == {"listings": 3,
                                             "counts": {"EcoFlow": 2, "Jackery": 1, "BLUETTI": 0, "none": 1}}
    assert rep.linked == 1 and rep.reasons["brand-unknown"] == 3
    assert BRAND_SENTINEL not in json.dumps(rep.to_dict()) + json.dumps(state)
    rep, _ = run_phase(build(model_first={"enabled": False}), FakeCreators(items),
                       FakeConn([{"asin": a} for a in items]))
    assert rep.to_dict()["brand_census"] is None


def test_bundle_no_kit_rows_carry_our_signature():
    items = {"B0BUNDLE01": raw_item("B0BUNDLE01", "EF ECOFLOW DELTA 3 Max Plus Solar Generator with 220W Bifacial "
                                                  "Solar Panel, 2048Wh", brand="EF ECOFLOW")}
    conn = FakeConn([{"asin": "B0BUNDLE01", "kind": "kit"}])
    rep, _ = run_phase(build(model_first={"enabled": False}), FakeCreators(items), conn)
    assert rep.rows == [{"asin": "B0BUNDLE01", "model_id": None, "rule": "bundle-no-kit", "confidence": 0.0,
                         "bundle": {"station_id": "ecoflow-delta-3-max-plus", "panels": {"220": 1},
                                    "battery": False, "panel_ids": [], "battery_ids": []}}]
    assert SENTINEL not in json.dumps(rep.to_dict())


# ---------------------------------------------------------------------------
# Optional: a real throwaway Postgres
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("MODEL_MATCHING_TEST_PG_DSN")


@pytest.mark.skipif(not PG_DSN, reason="MODEL_MATCHING_TEST_PG_DSN not set")
def test_match_phase_against_postgres():
    import psycopg2
    schema = f"mm_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    ids = {mid: i + 1 for i, mid in enumerate(MODEL_IDS)}
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}; SET search_path = {schema};"
                    "CREATE TABLE products (asin text PRIMARY KEY, kind text, lane text, model_id bigint,"
                    " category_confidence numeric, discovered_via text, discovered_seed text,"
                    " is_active boolean NOT NULL DEFAULT true);"
                    "CREATE TABLE test_models (id bigint, brand text, model_name text, kind text, aliases text[],"
                    " model_number text, capacity_wh numeric, power_w numeric, components bigint[],"
                    " component_qty int[], priority int, linked boolean, variants int, searchable boolean)")
        for r in model_rows():
            cur.execute("INSERT INTO test_models VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (ids[r["id"]], r["brand"], r["model_name"], r["kind"], r["aliases"], r["model_number"],
                         r["capacity_wh"], r["power_w"], [ids[c] for c in r["components"]], r["component_qty"],
                         r["priority"], False, 0, True))
        for a in ITEMS:
            cur.execute("INSERT INTO products (asin, kind, category_confidence) VALUES (%s, 'station', 0.45)", (a,))
    try:
        cfg = build(model_first={"enabled": False})
        conn = K.connect(PG_DSN, application_name="mm-test", statement_timeout_ms=10000)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path = {schema}")
        conn.commit()
        rep, _ = run_phase(cfg, FakeCreators(ITEMS), conn)
        with conn.cursor() as cur:
            cur.execute("SELECT asin, model_id, category_confidence FROM products WHERE model_id IS NOT NULL "
                        "ORDER BY asin")
            got = [(a, m, float(c)) for a, m, c in cur.fetchall()]
            cur.execute(f"SELECT string_agg(t::text, ' ') FROM {schema}.products t")
            dump = cur.fetchone()[0]
        assert got == [("B0ANKC1KX1", ids["anker-solix-c1000x"], 0.9),
                       ("B0DELTA3P1", ids["ecoflow-delta-3-plus"], 0.9),
                       ("B0JACK1KP1", ids["jackery-explorer-1000-plus"], 0.9)]
        assert rep.stats == {"active_products": 5, "matched_products": 3}
        assert SENTINEL not in dump
        conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()
