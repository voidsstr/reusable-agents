"""agents/amazon-price-refresh-agent: the price-refresh engine for cache-model sites.

Offline: no Creators call, no network, no production database. A fake Creators
client serves synthetic getItems answers (agents/amazon-price-refresh-agent/
fixtures/creators-getitems.json); a fake connection records every statement
and answers the read queries the engine and price_refresh_priority issue.
One opt-in test runs the whole engine against a throwaway Postgres
(PRICE_REFRESH_TEST_PG_DSN); it skips without one.

Ported from the reference site's price verifier tests (its agent dir is the
origin of this engine):
  test_priority_sql.py     -> the ranking / SQL-shape tests: signal arms passed
                              as array params, never a scan of the traffic log
                              inside the ranking query; no bare '%' in SQL that
                              runs with named params; demand rows re-checked
                              before the 24h display limit.
  test_retry_and_demand.py -> the deadlock and serialization-failure retry
                              tests, the demand-signal degradation and kill
                              switch, and the budget-spent path.
New here: no blanking on an LWA token timeout (unit and real DB), the purge
(every API column, only after a successful run, at a horizon that keeps every
stored value under 24h on the instance's cron), the spare (rows a run asked
about and could not refresh, i.e. a failed batch, an omitted ASIN or an
unstorable answer, are never NULLed while still displayable; unit and real
DB), the blanking counts taken from the purge's own RETURNING ages, the gate
check's purge-slack rule, the not_found guard, runs Creators never answered
(every batch rejected, a 429 from the first call) failing without a purge,
answers whose values cannot be stored skipped without stopping the sweep, and
no session idle in a transaction across a Creators call.
"""
from __future__ import annotations

import copy
import importlib.util as iu
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

from framework.core import price_refresh_priority as prp
from framework.core.amazon_creators import (
    CreatorsRequestError, CreatorsUnavailable,
)
from framework.core.amazon_price_refresh import (
    ORDERABLE_TEXT, PRICE_RESOURCES, PriceOutcome, classify_item,
)

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agents" / "amazon-price-refresh-agent"
FIXTURE = json.loads((AGENT_DIR / "fixtures" / "creators-getitems.json").read_text())
ITEMS: dict[str, dict] = FIXTURE["items"]
ERRORS: dict[str, str] = FIXTURE["errors"]

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
_spec = iu.spec_from_file_location("amazon_price_refresh_agent_under_test", AGENT_DIR / "agent.py")
A = iu.module_from_spec(_spec)
sys.modules[_spec.name] = A        # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(A)
S = A.settings_mod
C = A.cache_mod

AGENT_ID = "example-site-price-refresh"
CONSUMER = "example-site-price-refresh"
PICKS_KEY = "agents/example-site-pick-builder/state/pick-asins.json"

#: The API columns the license rule names (title, image URL and dims,
#: detailPageURL, price, list price, currency, availability and its type,
#: condition, buy-box flag, MAP-hidden flag, browse nodes, product group).
LICENSED_API_COLUMNS = {
    "title", "image_url", "image_w", "image_h", "detail_page_url", "price", "list_price",
    "currency", "availability", "availability_type", "condition", "is_buybox_winner",
    "map_hidden", "browse_node_ids", "product_group",
}
#: Browse nodes and product group are opt-in (the license review keeps them
#: out of the cache); everything else is on by default.
OPT_IN_COLUMNS = {"browse_node_ids", "product_group"}
DEFAULT_API_COLUMNS = LICENSED_API_COLUMNS - OPT_IN_COLUMNS
OWN_COLUMNS = ("asin", "outcome", "fetched_at", "consumer")
#: A spec with every API field on (a site that opted in to the two above).
FULL_COLUMNS = {f: f for f in LICENSED_API_COLUMNS}
#: 24h - 4h cron - 30-minute run.
HORIZON_4H = 19.5
#: 24h minus the kit's default HTML cache age (s-maxage 600 + max-age 60).
QUOTABLE_H = round(24 - 660 / 3600, 4)
#: The template's long-tail gate: re-checked at ~16h on the 4h cron.
TAIL_GATE = 15.5

LWA_TIMEOUT = ("LWA token request failed after 4 attempt(s): <urlopen error _ssl.c:989: "
               "The handshake operation timed out>")


# ---------------------------------------------------------------------------
# Guards: nothing in this file may reach Amazon, a production DB or the host's
# shared lock directory.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    from framework.core import amazon_creators as ac

    def live(*_a, **_k):
        raise AssertionError("a test reached a live Creators / database / lock path")

    monkeypatch.setattr(ac, "client_from_env", live)
    monkeypatch.setattr(ac.AmazonCreatorsClient, "_access_token", live)
    monkeypatch.setattr(ac.AmazonCreatorsClient, "_post", live)
    monkeypatch.setattr(A, "default_connect", live)
    monkeypatch.setattr(A, "default_lock_factory", live)
    monkeypatch.setattr(A, "default_client_factory", live)
    monkeypatch.setenv("AMAZON_CREATORS_TRACK_USAGE", "0")
    from framework.core import site_registry
    site_registry.clear_cache()
    yield
    site_registry.clear_cache()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _priced_item(asin: str, amount: float) -> dict:
    """A fresh priced answer for any ASIN, cloned from the fixture's template."""
    item = copy.deepcopy(ITEMS["B0AAAAAAA1"])
    item["asin"] = asin
    item["detailPageURL"] = f"https://www.amazon.com/dp/{asin}?tag=example-site-20"
    item["itemInfo"]["title"]["displayValue"] = f"Example Widget {asin}"
    item["offersV2"]["listings"][0]["price"]["money"]["amount"] = amount
    del item["offersV2"]["listings"][0]["price"]["savingBasis"]
    return item


class FakeCreators:
    """Serves synthetic getItems answers and records every batch.

    unavailable_after=n: the n-th call (0-based) and later raise
    CreatorsUnavailable (n=0 is the LWA token timeout before any answer);
    throttle_after=n: from the n-th call on, a persistent 429 (the batch lands
    in throttled_remainder); error_batches: calls that fail with an HTTP 400;
    on_call(n): called first on every call (e.g. to inspect the DB session)."""

    def __init__(self, items=None, errors=None, *, budget: Optional[int] = None,
                 unavailable_after: Optional[int] = None, throttle_after: Optional[int] = None,
                 error_batches=(), unavailable_msg: str = LWA_TIMEOUT, on_call=None):
        self.items = dict(ITEMS if items is None else items)
        self.errors = dict(ERRORS if errors is None else errors)
        self.calls: list[list[str]] = []
        self.resources_seen: list[tuple] = []
        self.throttled_remainder: list[str] = []
        self.track_usage = budget is not None
        self._budget = budget
        self.unavailable_after = unavailable_after
        self.throttle_after = throttle_after
        self.error_batches = set(error_batches)
        self.unavailable_msg = unavailable_msg
        self.on_call = on_call
        self.attempts = 0

    def remaining_budget(self):
        return self._budget

    def get_items(self, asins, *, resources=None):
        n = self.attempts
        self.attempts += 1
        if self.on_call:
            self.on_call(n)
        self.throttled_remainder = []
        if self.unavailable_after is not None and n >= self.unavailable_after:
            raise CreatorsUnavailable(self.unavailable_msg)
        if self.throttle_after is not None and n >= self.throttle_after:
            self.throttled_remainder = list(asins)
            return {}, {}
        self.calls.append(list(asins))
        self.resources_seen.append(tuple(resources or ()))
        if n in self.error_batches:
            raise CreatorsRequestError(
                "Creators getItems failed HTTP 400: InvalidParameterValue: the value "
                "[browseNodeInfo.browseNodes] provided in the request for Resources is invalid",
                status=400, detail="InvalidParameterValue")
        items = {a: copy.deepcopy(self.items[a]) for a in asins if a in self.items}
        errors = {a: self.errors[a] for a in asins if a in self.errors and a not in items}
        return items, errors


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows: list = []
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self.conn.tx_open = True        # psycopg2: the first statement opens a transaction
        for needle, answer in self.conn.answers:
            if needle in sql:
                if callable(answer) and not isinstance(answer, BaseException):
                    answer = answer(sql, params)
                if isinstance(answer, BaseException):
                    raise answer
                if isinstance(answer, int) and not isinstance(answer, bool):
                    self.rows, self.rowcount = [], answer
                else:
                    self.rows = list(answer or [])
                    self.rowcount = len(self.rows)
                return
        self.rows, self.rowcount = [], 0

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.autocommit = False
        self.tx_open = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.tx_open = False

    def rollback(self):
        self.rollbacks += 1
        self.tx_open = False

    def close(self):
        self.closed += 1

    def sql(self, needle: str) -> list[tuple[str, Any]]:
        return [(s, p) for s, p in self.executed if needle in s]


def rank_answer(catalog: dict[str, Optional[float]]):
    """Evaluate price_refresh_priority's rank query over a fake catalog
    {asin: hours since the last answer, None = never checked}: due rows in
    tier order, never-checked first, then oldest first, then score."""
    def answer(sql, params):
        ranks = dict(zip(params["prp_t_asins"], params["prp_t_ranks"]))
        gates = dict(zip(params["prp_t_asins"], params["prp_t_gates"]))
        scores = dict(zip(params["prp_t_asins"], params["prp_t_scores"]))
        rows = []
        for asin, age in catalog.items():
            if asin not in ranks and not params["prp_tail_on"]:
                continue
            rnk = ranks.get(asin, params["prp_tail_rank"])
            gate = gates.get(asin, params["prp_tail_gate"])
            if age is None or age > gate:
                rows.append((asin, rnk, gate, scores.get(asin, 0.0), age))
        rows.sort(key=lambda r: (r[1], r[4] is not None, -(r[4] or 0.0), -r[3], r[0]))
        lim = params.get("prp_limit")
        return rows[:lim] if lim else rows
    return answer


#: hours since the last answer (None = never). Demand: B0AAAAAAA4 clicked,
#: B0AAAAAAA3 assistant-fetched, B0AAAAAAA5 a pick; B0AAAAAAA8 is fresh.
CATALOG = {
    "B0AAAAAAA1": None, "B0AAAAAAA2": 30.0, "B0AAAAAAA3": 17.0, "B0AAAAAAA4": 13.0,
    "B0AAAAAAA5": 20.0, "B0AAAAAAA6": None, "B0AAAAAAA7": 25.0, "B0AAAAAAA8": 2.0,
}
EXPECTED_ORDER = ["B0AAAAAAA4", "B0AAAAAAA3", "B0AAAAAAA5",       # clicked, assistant, picks
                  "B0AAAAAAA1", "B0AAAAAAA6",                      # long tail: never checked
                  "B0AAAAAAA2", "B0AAAAAAA7"]                      # long tail: oldest first


class FakeCache:
    """The cache table's state for the fake connection: {asin: hours since
    its answer} of every row holding Amazon data. Upserts make a row fresh
    (a not_found answer leaves it with no data); the purge NULLs rows past
    the horizon except the spared ones, evaluated from the statement's params
    exactly as the SQL states them; RETURNING gives (asin, age).

    ignore_spare=True plays a broken purge that drops the spare clause (to
    prove the blanking counts read what the purge did, not what it should)."""

    def __init__(self, ages=None, *, ignore_spare: bool = False):
        self.ages: dict[str, Optional[float]] = dict(ages or {})
        self.purged: dict[str, Optional[float]] = {}
        self.purge_params: list[dict] = []
        self.ignore_spare = ignore_spare

    def _past(self, params) -> dict:
        horizon = params["prf_max_age_s"] / 3600.0
        return {a: age for a, age in self.ages.items() if age is None or age > horizon}

    def _spared(self, params, past) -> list[str]:
        spare = set(params.get("prf_spare") or ())
        window = (params.get("prf_spare_s") or 0.0) / 3600.0
        return [a for a, age in past.items() if a in spare and age is not None and age <= window]

    def insert(self, sql, params):
        i = 0
        while f"r{i}_key" in params:
            if params.get(f"r{i}_outcome") == "not_found":
                self.ages.pop(params[f"r{i}_key"], None)       # every API column NULL
            else:
                self.ages[params[f"r{i}_key"]] = 0.0
            i += 1
        return []

    def purge(self, sql, params):
        self.purge_params.append(dict(params))
        past = self._past(params)
        keep = set() if self.ignore_spare else set(self._spared(params, past))
        rows = [(a, age) for a, age in past.items() if a not in keep]
        for a, age in rows:
            del self.ages[a]
            self.purged[a] = age
        return rows

    def spared(self, sql, params):
        return [(a,) for a in self._spared(params, self._past(params))]


def make_conn(catalog=None, *, purge_rows=3, fresh=(8, 7, 6), stale=(0, 0), extra=(),
              cache: Optional[FakeCache] = None, tier_rows=None):
    """A FakeConn over ``catalog`` (ranking) and ``cache`` (the cache table).
    Without ``cache``: every catalog row that has an answer is cached at its
    age, plus ``purge_rows`` rows of retired products 30h old (purged by any
    purge, never displayable, outside the catalog)."""
    catalog = CATALOG if catalog is None else catalog
    if cache is None:
        cache = FakeCache({**{a: age for a, age in catalog.items() if age is not None},
                           **{f"B0RETIRED{i}": 30.0 for i in range(purge_rows)}})
    tier_rows = tier_rows if tier_rows is not None else [
        (0, 1, 1, 1, 0, 0, 0), (1, 1, 1, 1, 0, 0, 0), (2, 1, 1, 1, 0, 0, 0),
        (3, 0, 5, 3, 1, 1, 2)]
    conn = FakeConn(list(extra) + [
        ("FULL JOIN t", tier_rows),
        ("LEFT JOIN t ON t.asin = c.asin", rank_answer(catalog)),
        ("WITH idx AS MATERIALIZED", [fresh]),
        ("AS stale_api_rows", [stale]),
        ("AS spared_key", cache.spared),
        ("AS catalog_rows", lambda sql, params: [(len(set(params["prf_keys"]) & set(catalog)),)]),
        ("INSERT INTO amazon_item_cache", cache.insert),
        ("UPDATE amazon_item_cache AS t SET", cache.purge),
    ])
    conn.cache = cache
    return conn


def fake_signals(monkeypatch, *, clicks=None, ai_rows=None, fail_ai=False, forbid=False):
    from framework.core import ai_traffic, human_clicks
    calls: dict[str, list] = {"clicks": [], "ai": []}

    def counts_by(conn, spec, *, table, group_col, window_days=30, match=None):
        if forbid:
            raise AssertionError("a demand signal was read in fallback mode")
        calls["clicks"].append({"table": table, "group_col": group_col,
                                "window_days": window_days, "match": dict(match or {}),
                                "spec": dict(spec)})
        return dict({"B0AAAAAAA4": 3} if clicks is None else clicks)

    def landed(conn, *, cfg=None, prefixes=(), limit=200):
        if forbid:
            raise AssertionError("a demand signal was read in fallback mode")
        calls["ai"].append({"cfg": cfg, "prefixes": list(prefixes)})
        if fail_ai:
            raise RuntimeError("canceling statement due to statement timeout")
        return list([{"path": "/product/B0AAAAAAA3", "score": 4},
                     {"path": "/product/EBAY_1234567", "score": 9},
                     {"path": "/reviews/some-guide", "score": 50}]
                    if ai_rows is None else ai_rows)

    monkeypatch.setattr(human_clicks, "human_counts_by", counts_by)
    monkeypatch.setattr(ai_traffic, "landed_paths", landed)
    return calls


def example_block() -> dict:
    block, _ = S.read_block(str(AGENT_DIR / "site.example.yaml"))
    return block


def make_settings(storage=None, mutate=None, agent_id: str = AGENT_ID):
    block = copy.deepcopy(example_block())
    if mutate:
        mutate(block)
    return S.build_settings(block, agent_id=agent_id, storage=storage, source="test")


def run(settings, storage, client, conn, **kw):
    seen: dict[str, Any] = {"tags": [], "decisions": []}

    def factory(s, st, tag):
        seen["tags"].append(tag)
        return client

    def connect(s, environ=None, readonly=False):
        return conn

    rep = A.run_refresh(settings, storage=storage, client_factory=factory, connect=connect,
                        say=lambda msg, progress=0.0: None,
                        decide=lambda category, message, evidence=None:
                        seen["decisions"].append((category, message)), **kw)
    return rep, seen


def _no_stray_percent(sql: str, params: Optional[dict] = None) -> bool:
    stripped = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "", sql.replace("%%", ""))
    return "%" not in stripped


@pytest.fixture
def picks(storage):
    storage.write_json(PICKS_KEY, {"asins": ["B0AAAAAAA5"]})
    return storage


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_example_config_is_the_launch_plan(storage):
    assert S.schema_problems(example_block()) == []
    s = make_settings(storage)
    assert (s.site, s.consumer, s.partner_tag_value({})) == ("example-site", CONSUMER,
                                                            "example-site-20")
    assert s.dsn_env == "DATABASE_URL_EXAMPLE_SITE" and s.max_calls_per_run == 250
    assert s.demand_priority["catalog"]["table"] == "v_refresh_catalog"
    assert s.demand_priority["catalog"]["fresh_cols"] == ["fetched_at"]
    cfg = A.priority_config(s, storage=storage)
    assert cfg["enabled"] is True and cfg["config_problems"] == []
    assert [(g["name"], g["kind"], g["gate_hours"]) for g in cfg["groups"]] == [
        ("clicked", "human_clicks", 12.0), ("assistant-fetched", "ai_traffic", 12.0),
        ("picks", "storage_list", 12.0)]
    # 15.5h, not 16h: a 16h gate is first re-checked at ~20h on the 4h cron,
    # past the 19.5h purge horizon (see the gate tests below).
    assert cfg["long_tail"] == {"enabled": True, "gate_hours": TAIL_GATE}
    assert (s.cron_period_hours, s.max_run_minutes) == (4, 30) and s.warnings == ()
    assert (s.tolerate_failed_runs, s.long_tail_tolerate_failed_runs) == (1, 1)
    # Price, availability, condition, buy box, title, image: the design's set.
    # Browse nodes and classifications are opt-in (license review), so the
    # unverified getItems resource is not requested by default.
    assert s.resources == PRICE_RESOURCES + ("itemInfo.title", "images.primary.large")
    assert set(s.cache.api_columns) == DEFAULT_API_COLUMNS
    assert s.cache.own_columns == OWN_COLUMNS
    # The purge horizon keeps every stored value under 24h on the 4h cron.
    assert s.cache.max_age_hours == HORIZON_4H
    assert s.quotable_hours(24) == QUOTABLE_H and s.max_html_cache_age_s == 660
    assert s.describe()["cache"]["worst_case_stored_age_hours"] == 24.0
    # A row a run asked about and could not refresh is spared while still
    # displayable: stored up to the quotable window plus a period and a run.
    assert s.describe()["cache"]["worst_case_spared_age_hours"] == round(QUOTABLE_H + 4.5, 4)


def test_human_clicks_source_is_the_verified_human_beacon_series(storage):
    # Ported pin: the click definition the demand tiers rank by.
    hc = make_settings(storage).demand_priority["sources"]["human_clicks"]
    assert hc["table"] == "outbound_clicks"
    assert hc["match"] == {"target": "amazon", "via": "beacon"}
    assert hc["spec"]["bot_flag_col"] == "is_bot" and hc["group_col"] == "asin"
    at = make_settings(storage).demand_priority["sources"]["ai_traffic"]
    # Answer-time fetchers only: the index crawler is not demand.
    assert at["config"]["live_fetch_sources"] == ["chatgpt-user", "perplexity-user", "claude-user"]


@pytest.mark.parametrize("mutate, needle", [
    (lambda b: b["creators"].__setitem__("max_calls_per_rnu", 5), "max_calls_per_rnu"),
    (lambda b: b["cache"]["purge"].__setitem__("max_age_hours", 48), "max_age_hours"),
    (lambda b: b["cache"]["purge"].__setitem__("max_age_hours", "never"), "max_age_hours"),
    (lambda b: b["cache"].setdefault("columns", {}).__setitem__("titel", "title"), "titel"),
    (lambda b: b["cache"].setdefault("columns", {}).__setitem__("price", None), "price"),
    (lambda b: b.__setitem__("schedul", {}), "schedul"),
    (lambda b: b["schedule"].__setitem__("cron_period_hours", None), "cron_period_hours"),
    (lambda b: b["metrics"].__setitem__("max_html_cache_age_s", -1), "max_html_cache_age_s"),
])
def test_schema_rejects_typos_and_out_of_range_values(storage, mutate, needle):
    with pytest.raises(S.SettingsError) as ei:
        make_settings(storage, mutate)
    assert any(needle in p for p in ei.value.problems)


def test_semantic_checks_catch_what_the_schema_cannot(storage):
    def bad(mutate) -> str:
        with pytest.raises(S.SettingsError) as ei:
            make_settings(storage, mutate)
        return " | ".join(ei.value.problems)

    assert "cache.table must be a SQL identifier" in bad(
        lambda b: b["cache"].__setitem__("table", "amazon_item_cache; DROP TABLE products"))
    # Ported: a bare '%' raises inside psycopg2 when the SQL runs with params.
    assert "bare '%'" in bad(lambda b: b["demand_priority"]["catalog"].__setitem__(
        "where", "c.title LIKE 'x%'"))
    make_settings(storage, lambda b: b["demand_priority"]["catalog"].__setitem__(
        "where", "c.title LIKE 'x%%'"))
    assert "bare '%'" in bad(lambda b: b["metrics"].__setitem__(
        "indexable_sql", "SELECT asin FROM products WHERE slug LIKE 'a%'"))
    assert "partner_tag" in bad(lambda b: b["creators"].pop("partner_tag"))
    assert "classifier's resources" in bad(lambda b: b["creators"].__setitem__(
        "resources", ["itemInfo.title"]))
    assert "must never be purged" in bad(lambda b: b["cache"].__setitem__(
        "extra_purge_columns", ["fetched_at"]))
    assert "unknown kind" in bad(lambda b: b["demand_priority"]["groups"].append(
        {"name": "x", "kind": "nope", "gate_hours": 1}))
    assert "'catalog' is a required property" in bad(lambda b: b["demand_priority"].pop("catalog"))
    assert "catalog.table is required" in bad(lambda b: b.pop("demand_priority"))
    assert "reserved prp_/prf_" in bad(lambda b: b["demand_priority"]["catalog"].__setitem__(
        "params", {"prf_x": 1}))
    # A DSN pasted where an env var NAME belongs is rejected and never echoed.
    msg = bad(lambda b: b["database"].__setitem__("dsn_env", "postgresql://u:secret@h/db"))
    assert "environment variable NAME" in msg and "secret" not in msg


def test_cron_period_is_required(storage):
    # The purge horizon depends on it, so a missing period is an error, not a
    # warning (it used to only skip the gate check).
    with pytest.raises(S.SettingsError) as ei:
        make_settings(storage, lambda b: b.pop("schedule"))
    assert any("schedule.cron_period_hours is required" in p for p in ei.value.problems)


def test_explicit_resources_must_cover_the_enabled_columns(storage):
    # Every answer replaces every enabled column: a list without the title or
    # image resource would write NULL titles and images on every refresh.
    with pytest.raises(S.SettingsError) as ei:
        make_settings(storage, lambda b: b["creators"].__setitem__(
            "resources", list(PRICE_RESOURCES)))
    msg = " | ".join(ei.value.problems)
    assert "lacks itemInfo.title" in msg and "lacks images.primary.large" in msg
    assert "image_url, image_w, image_h" in msg
    # Covering them, or switching the columns off, is accepted.
    full = list(PRICE_RESOURCES) + ["itemInfo.title", "images.primary.large"]
    assert make_settings(storage, lambda b: b["creators"].__setitem__(
        "resources", full)).resources == tuple(full)

    def lean(b):
        b["creators"]["resources"] = list(PRICE_RESOURCES)
        b["cache"]["columns"] = {"title": None, "image_url": None, "image_w": None,
                                 "image_h": None}
    assert make_settings(storage, lean).resources == PRICE_RESOURCES
    # An opted-in column needs its resource too.
    with pytest.raises(S.SettingsError, match="lacks browseNodeInfo.browseNodes"):
        make_settings(storage, lambda b: (
            b["cache"].__setitem__("columns", {"browse_node_ids": "browse_node_ids"}),
            b["creators"].__setitem__("resources", full)))


def test_fresh_cols_defaults_to_the_cache_fetched_at(storage):
    # Unset, price_refresh_priority would rank on its own default
    # (price_updated_at), which the catalog view does not have.
    s = make_settings(storage, lambda b: b["demand_priority"]["catalog"].pop("fresh_cols"))
    assert s.demand_priority["catalog"]["fresh_cols"] == ["fetched_at"]
    assert A.priority_config(s, storage=storage)["catalog"]["fresh_cols"] == ["fetched_at"]
    renamed = make_settings(storage, lambda b: (
        b["demand_priority"]["catalog"].pop("fresh_cols"),
        b["cache"].__setitem__("fetched_at_col", "answered_at")))
    assert renamed.demand_priority["catalog"]["fresh_cols"] == ["answered_at"]
    sql, _ = prp.build_rank_sql(A.priority_config(s, storage=storage), prp.Tiers(), limit=5)
    assert "MIN(c.fetched_at)" in sql and "price_updated_at" not in sql
    # A site that names its own column keeps it.
    kept = make_settings(storage, lambda b: b["demand_priority"]["catalog"].__setitem__(
        "fresh_cols", ["cache_fetched_at"]))
    assert kept.demand_priority["catalog"]["fresh_cols"] == ["cache_fetched_at"]


# ---------------------------------------------------------------------------
# The purge horizon (the 24h license limit applies to storage, not to a
# threshold checked once per cron period)
# ---------------------------------------------------------------------------

def test_purge_horizon_is_24h_minus_the_cron_period_and_a_run(storage):
    assert C.purge_horizon_hours(4, 30) == HORIZON_4H
    assert C.worst_case_stored_age_hours(HORIZON_4H, 4, 30) == 24.0
    # The old threshold: purging at 24h once per 4h run stores data up to 28h+.
    assert C.worst_case_stored_age_hours(24, 4, 30) == 28.5
    assert S.worst_case_age_hours(24, 4) == 28.0
    # 'auto' follows the schedule.
    two_h = make_settings(storage, lambda b: b["schedule"].__setitem__("cron_period_hours", 2))
    assert two_h.cache.max_age_hours == 21.5
    slow = make_settings(storage, lambda b: b["schedule"].__setitem__("max_run_minutes", 60))
    assert slow.cache.max_age_hours == 19.0
    # A smaller explicit horizon is fine; a larger one is rejected with the reason.
    tight = make_settings(storage, lambda b: b["cache"]["purge"].__setitem__("max_age_hours", 18))
    assert tight.cache.max_age_hours == 18
    with pytest.raises(S.SettingsError) as ei:
        make_settings(storage, lambda b: b["cache"]["purge"].__setitem__("max_age_hours", 24))
    msg = " | ".join(ei.value.problems)
    assert "keeps Amazon data up to ~28.5h" in msg and "at most 19.5h" in msg
    # A cron period that leaves no horizon at all is a config error.
    with pytest.raises(S.SettingsError, match="no purge horizon"):
        make_settings(storage, lambda b: (b["schedule"].__setitem__("cron_period_hours", 23),
                                          b["schedule"].__setitem__("max_run_minutes", 120)))
    # Without a schedule (a direct caller) 'auto' is the license limit itself.
    assert C.build_cache_spec({}).max_age_hours == 24.0
    assert C.build_cache_spec({}, period_hours=4).max_age_hours == HORIZON_4H


def test_example_schedule_never_stores_amazon_data_past_24h(storage):
    """Simulate the purge on the example's schedule: runs every 4h whose
    purge lands anywhere from seconds (budget spent) to 30 min (full sweep)
    after the tick; a row answered at any minute of a run. The oldest any
    row gets before a purge removes it stays at or under 24h, while the old
    24h threshold let rows live to ~28h."""
    s = make_settings(storage)
    period, run_h = s.cron_period_hours, s.max_run_minutes / 60.0

    def oldest(horizon: float) -> float:
        worst = 0.0
        steps = 31
        for i in range(steps):                         # when the row was answered in its run
            answered = run_h * i / (steps - 1)
            for pattern in range(8):                   # purge offsets of the next runs
                age = 0.0
                for k in range(1, 12):
                    offset = run_h if (pattern >> (k % 3)) & 1 else 0.0
                    age = k * period + offset - answered
                    if age > horizon:                  # purged at this run
                        break
                worst = max(worst, age)
        return worst

    assert oldest(s.cache.max_age_hours) <= 24.0 + 1e-9
    assert oldest(24.0) > 27.0


def _oldest_spared(answered: float, *, period: float, run_h: float, horizon: float,
                   window: float, gate: float, max_fails: int, steps: int = 6) -> float:
    """The oldest a value answered ``answered`` hours after a cron tick gets in
    storage, over every outcome of the runs after it, by the engine's rules.
    Each run ranks 10 s after its tick (due when older than ``gate``:
    price_refresh_priority's ``fresh < NOW() - gate``), then answers, then
    purges, all within ``run_h`` of the tick. A due row is answered (its
    value is replaced), its batch fails (one of the run's unrefreshed keys)
    or the budget never reaches it (a spill). The purge NULLs a row older
    than ``horizon`` unless its batch failed in that run and it is at most
    ``window`` old (THE SPARE); a spilled or not-due row is never spared. At
    most ``max_fails`` failed batches."""
    rank = 10 / 3600.0
    offsets = [rank + (run_h - rank) * i / steps for i in range(steps + 1)]

    def from_tick(k: int, fails: int) -> float:
        age = k * period - answered                   # the value's age at tick k
        if age > 72:
            raise AssertionError("the replay never removed the value")
        outcomes = ("answered", "failed", "spilled") if age + rank > gate else ("not due",)
        oldest = 0.0
        for outcome in outcomes:
            if outcome == "answered":                 # replaced, at the latest at the purge
                oldest = max(oldest, age + offsets[-1])
                continue
            n = fails + (outcome == "failed")
            if n > max_fails:
                continue
            kept = False
            for y in offsets:                         # this run's purge
                spared = outcome == "failed" and age + y <= window
                if age + y > horizon and not spared:
                    oldest = max(oldest, age + y)     # NULLed by this purge
                else:
                    kept = True
            if kept:
                oldest = max(oldest, from_tick(k + 1, n))
        return oldest

    return from_tick(1, 0)


def test_example_schedule_bounds_how_long_a_spared_row_is_stored(storage):
    """The spare on the example's schedule (_oldest_spared). Budget spills
    alone stay within the horizon's 24h, and a row answered on its cadence
    and spared once is gone by ~24.5h (the docs used to call that the worst
    case). A row answered off its cadence (a manual or catch-up run) reaches
    ~28.0h: answered 3.4h before a tick, its batch failing at ~19.4h and
    ~23.4h, it is stored until ~27.9h. Nothing passes
    worst_case_spared_age_hours (the window plus a period and a run, ~28.3h),
    which a row on its cadence whose batch fails twice in a row reaches."""
    s = make_settings(storage)
    cfg = A.priority_config(s, storage=storage)
    period, run_h = s.cron_period_hours, s.max_run_minutes / 60.0
    horizon = s.cache.max_age_hours
    window = s.quotable_hours(cfg.get("display_hours"))   # the run's spare window
    assert (horizon, window) == (HORIZON_4H, QUOTABLE_H)
    bound = C.worst_case_spared_age_hours(window, period, s.max_run_minutes)
    assert bound == s.describe()["cache"]["worst_case_spared_age_hours"] == 28.3167
    no_spare = C.worst_case_stored_age_hours(horizon, period, s.max_run_minutes)
    gates = sorted({g["gate_hours"] for g in cfg["groups"]} | {cfg["long_tail"]["gate_hours"]})
    assert gates == [12.0, TAIL_GATE]

    def oldest(answered: float, max_fails: int, gate: float = TAIL_GATE) -> float:
        return _oldest_spared(answered, period=period, run_h=run_h, horizon=horizon,
                              window=window, gate=gate, max_fails=max_fails)

    # The review's case (answered 3.4h before a tick, first due at ~19.4h):
    # failing at ~19.4h and ~23.4h, it is spared at ~23.4h and stored until
    # ~27.9h. Failing only at ~23.4h does the same after a ~19.4h run that
    # never asked it and purged within 6 minutes. Spills alone stay in 24h.
    twice = oldest(period - 3.4, 2)
    assert twice > 24.0 and round(twice, 4) == 27.9
    assert round(oldest(period - 3.4, 1), 4) == 27.9
    assert oldest(period - 3.4, 0) <= no_spare == 24.0

    minutes = [m / 60.0 for m in range(1, int(period * 60))]   # answered at any minute
    for gate in gates:
        worst = {n: {a: oldest(a, n, gate) for a in minutes} for n in range(4)}
        assert max(worst[0].values()) <= no_spare                             # spills alone
        assert max(v for a, v in worst[1].items() if a <= run_h) <= 24.5      # on cadence, once
        assert max(v for a, v in worst[3].items() if a > run_h) <= (          # off cadence
            horizon + 2 * period + run_h) == 28.0
        assert max(worst[3].values()) <= bound
        assert max(worst[2].values()) > bound - 0.05                          # and it is reached


def test_worst_case_spared_age_is_never_below_the_horizon_bound(storage):
    assert C.worst_case_spared_age_hours(QUOTABLE_H, 4, 30) == round(QUOTABLE_H + 4.5, 4)
    assert C.worst_case_spared_age_hours(QUOTABLE_H, 4, 30, horizon_hours=18) == 28.3167
    # The spare only keeps rows past the horizon: a window at or under it
    # spares nothing, and the bound is the horizon's own, never a smaller one.
    assert C.worst_case_spared_age_hours(19.0, 4, 30) == C.worst_case_stored_age_hours(
        HORIZON_4H, 4, 30) == 24.0
    assert C.worst_case_spared_age_hours(17.0, 4, 30, horizon_hours=18) == 22.5
    short = make_settings(storage, lambda b: b["demand_priority"].__setitem__(
        "display_hours", 19.5))
    assert short.quotable_hours(19.5) < short.cache.max_age_hours == HORIZON_4H
    assert short.describe()["cache"]["worst_case_spared_age_hours"] == 24.0


# ---------------------------------------------------------------------------
# Listing pick (a local copy of amazon_creators' private picker)
# ---------------------------------------------------------------------------

def test_chosen_listing_matches_the_price_parser():
    """price_refresh_cache keeps its own copy of the listing order so it never
    imports a private name from amazon_creators (edited in the 12-02 window).
    If that order changes, this fails: the buy-box flag must describe the
    listing parse_item() took the price from."""
    from framework.core import amazon_creators as ac
    new_bb = {"condition": {"value": "New"}, "isBuyBoxWinner": True, "id": "new-bb"}
    new = {"condition": {"value": "New"}, "isBuyBoxWinner": False, "id": "new"}
    used_bb = {"condition": {"value": "Used"}, "isBuyBoxWinner": True, "id": "used-bb"}
    used = {"condition": {"value": "Used"}, "isBuyBoxWinner": False, "id": "used"}
    bare = {"id": "bare"}
    cases = [[], [bare], [used, used_bb], [used_bb, new], [new, new_bb], [used, bare],
             [used_bb, new, new_bb], [bare, used], ["junk", new]]
    cases += [list(((i.get("offersV2") or {}).get("listings")) or []) for i in ITEMS.values()]
    for listings in cases:
        assert C._chosen_listing(listings) == ac._pick_listing(listings), listings


def test_site_and_dsn_fall_back_to_the_site_registry(storage):
    from framework.core import site_registry
    storage.write_json(site_registry.CONFIG_KEY, {"schema_version": "1", "sites": [
        {"name": "example-site", "domain": "example.com", "repo_path": "/tmp/example-site",
         "branch": "main"}]})

    def strip(b):
        b.pop("site")
        b["database"].pop("dsn_env")
        b["creators"].pop("consumer")

    s = make_settings(storage, strip)
    assert (s.site, s.dsn_env, s.consumer) == ("example-site", "DATABASE_URL_EXAMPLE_SITE",
                                               "example-site-price-refresh")
    with pytest.raises(S.SettingsError) as ei:
        make_settings(storage, strip, agent_id="other-site-price-refresh")
    assert any("database.dsn_env" in p for p in ei.value.problems)


def test_load_settings_from_env_with_a_dotted_key(tmp_path, storage):
    import yaml
    p = tmp_path / "site.yaml"
    p.write_text(yaml.safe_dump({"agents": {"refresh": example_block()}}))
    s = S.load_settings(None, agent_id=AGENT_ID, storage=storage,
                        environ={S.ENV_CONFIG: f"{p}#agents.refresh"})
    assert s.consumer == CONSUMER and s.source.endswith("#agents.refresh")
    with pytest.raises(S.SettingsError, match="PRICE_REFRESH_CONFIG"):
        S.load_settings(None, agent_id=AGENT_ID, storage=storage, environ={})
    with pytest.raises(S.SettingsError, match="price_refresh"):
        S.load_settings(str(p), agent_id=AGENT_ID, storage=storage, environ={})


# ---------------------------------------------------------------------------
# Gates (ported: priority rows re-check before the 24h display gate)
# ---------------------------------------------------------------------------

#: The gate check's verdict on a 16h long tail on the 4h cron with 30-minute
#: runs (its first re-check, ~20.5h, lands past the 19.5h purge horizon).
NO_SLACK_16H = ("long-tail: a 16h gate on a 4h cron first re-checks rows at ~20.5h, past the "
                "19.5h purge horizon: a row that run cannot answer (the budget runs out before "
                "it) is NULLed by its own purge while still displayable; lower the gate below "
                "16h by at least a run (e.g. 15.5h) or shorten the cron")


def test_launch_gates_stay_inside_the_quotable_window(storage):
    s = make_settings(storage)
    cfg = A.priority_config(s, storage=storage)
    assert s.gate_problems(cfg) == []
    # The off-by-minutes effect: a row answered just after tick T is re-checked
    # at T + (floor(gate / P) + 1) * P.
    assert S.worst_case_age_hours(16, 4) == 20 and S.worst_case_age_hours(16, 4, 1) == 24
    assert S.worst_case_age_hours(12, 4) == 16 and S.worst_case_age_hours(12, 4, 1) == 20
    assert (S.worst_case_age_hours(TAIL_GATE, 4), S.worst_case_age_hours(TAIL_GATE, 4, 1)) == (
        16, 20)
    assert S.worst_case_age_hours(17, 6) == 18      # the reference verifier's 17h gate, 6h cron
    # The draft design's 22h long tail is exactly 24h with no failure at all
    # (amendment 10), past the quotable window once a run's minutes count.
    assert S.worst_case_age_hours(22, 4) == 24
    tail22 = make_settings(storage, lambda b: b["demand_priority"]["long_tail"].__setitem__(
        "gate_hours", 22))
    problems = tail22.gate_problems(A.priority_config(tail22, storage=storage))
    assert problems == [
        f"long-tail: a 22h gate on a 4h cron re-checks rows at up to ~24.5h, ~28.5h after 1 "
        f"failed run(s), past the {QUOTABLE_H:g}h quotable window",
        "long-tail: a 22h gate is not a run (30 min) below the 19.5h purge horizon: rows are "
        "purged before they fall due"]


def test_a_16h_long_tail_leaves_no_run_before_the_purge(storage):
    """Amendment 10's 16h tail was sized as ceil(gate / P) * P = 16h. Rows are
    answered minutes after a tick, so it is first re-checked at ~20h (+ a
    run), past the 19.5h purge horizon: a row that run cannot answer is
    blanked by that same run. The check used to pass it (it only compared
    the gate itself with the horizon); it flags it now. The template's 15.5h
    tail and the 12h groups re-check at ~16.5h and pass."""
    s = make_settings(storage, lambda b: (
        b["demand_priority"]["long_tail"].__setitem__("gate_hours", 16),
        b["schedule"].__setitem__("long_tail_tolerate_failed_runs", 0)))
    cfg = A.priority_config(s, storage=storage)
    assert s.gate_problems(cfg) == [NO_SLACK_16H]
    # It is the purge-slack rule that flags it: the quotable window alone is
    # fine (~20.5h), and the old purge rule (gate + run >= horizon, 16.5h)
    # did not see it.
    kw = dict(period_hours=4, failed_runs=0, display_hours=QUOTABLE_H, run_margin_hours=0.5)
    assert S.gate_buffer_problems(cfg, **kw) == []
    assert S.gate_buffer_problems(cfg, **kw, purge_age_hours=HORIZON_4H) == [NO_SLACK_16H]
    # Asked to absorb a failed run too, it also leaves the quotable window.
    s1 = make_settings(storage, lambda b: b["demand_priority"]["long_tail"].__setitem__(
        "gate_hours", 16))
    assert s1.gate_problems(A.priority_config(s1, storage=storage)) == [
        f"long-tail: a 16h gate on a 4h cron re-checks rows at up to ~20.5h, ~24.5h after 1 "
        f"failed run(s), past the {QUOTABLE_H:g}h quotable window", NO_SLACK_16H]
    # Without a run margin and against 24h the old check passed all of this.
    assert S.gate_buffer_problems(cfg, period_hours=4, failed_runs=1) == []
    # The two remedies the hint names: a gate a run under 16h, or a 2h cron
    # (horizon 21.5h; a 16h gate first re-checks at ~18.5h).
    tail155 = make_settings(storage, lambda b: b["demand_priority"]["long_tail"].__setitem__(
        "gate_hours", 15.5))
    assert tail155.gate_problems(A.priority_config(tail155, storage=storage)) == []
    two_h = make_settings(storage, lambda b: (
        b["demand_priority"]["long_tail"].__setitem__("gate_hours", 16),
        b["schedule"].__setitem__("cron_period_hours", 2)))
    assert two_h.cache.max_age_hours == 21.5
    assert two_h.gate_problems(A.priority_config(two_h, storage=storage)) == []
    # A cron so long that no gate leaves a run before the horizon says so.
    assert "shorten the cron: no gate" in " ".join(S.gate_buffer_problems(
        {"groups": [], "long_tail": {"gate_hours": 1}}, period_hours=12, failed_runs=0,
        run_margin_hours=0.5, purge_age_hours=11.5))


def test_gate_buffer_violation_is_reported_by_the_run(storage, picks, monkeypatch):
    fake_signals(monkeypatch)
    s = make_settings(storage, lambda b: b["demand_priority"]["long_tail"].__setitem__(
        "gate_hours", 22))
    rep, seen = run(s, storage, FakeCreators(), make_conn())
    assert rep.metrics["gate_buffer_violations"] == 2
    assert sum(1 for c, m in seen["decisions"] if c == "warning" and "gate buffer" in m) == 2


# ---------------------------------------------------------------------------
# One answer -> one cache row
# ---------------------------------------------------------------------------

def test_priced_answer_fills_every_api_column():
    item = ITEMS["B0AAAAAAA1"]
    vals = C.row_values(classify_item("B0AAAAAAA1", item), item)
    assert set(vals) == set(C.API_FIELDS) == LICENSED_API_COLUMNS
    assert vals == {
        "title": "Example Widget Pro 2000, 120-Piece Kit",
        "image_url": "https://m.media-amazon.com/images/I/41ExampleAAL._SL500_.jpg",
        "image_w": 375, "image_h": 500,
        "detail_page_url": "https://www.amazon.com/dp/B0AAAAAAA1?tag=example-site-20&linkCode=ogi&th=1&psc=1",
        "price": 1299.0, "list_price": 1599.0, "currency": "USD",
        "availability": "In Stock", "availability_type": "IN_STOCK", "condition": "New",
        "is_buybox_winner": True, "map_hidden": False,
        "browse_node_ids": ["1000000001", "1000000002"],
        "product_group": "Home Improvement",
    }


def test_unbuyable_answers_clear_the_price_but_keep_the_fresh_item():
    def vals(asin):
        o = classify_item(asin, ITEMS[asin])
        return o.status, C.row_values(o, ITEMS[asin])

    status, v = vals("B0AAAAAAA2")                      # out of stock
    assert status == "unavailable" and v["price"] is None and v["list_price"] is None
    assert v["availability"] == "Currently unavailable." and v["currency"] is None
    assert v["title"] and v["image_url"] and v["image_w"] == 500
    assert v["availability_type"] == "OUT_OF_STOCK"     # the listing shown: its type stays
    assert v["is_buybox_winner"] is None                # not in the answer
    status, v = vals("B0AAAAAAA3")                      # used-only listing
    assert status == "no_offer" and v["price"] is None
    assert v["availability"] is None                    # the used listing's "Only 2 left"
    # Its type and buy box would say "buyable" next to a NULL price just as
    # loudly: NULL too. The condition explains the no_offer and stays.
    assert v["availability_type"] is None and v["is_buybox_winner"] is None
    assert v["condition"] == "Used" and v["title"]
    status, v = vals("B0AAAAAAA4")                      # MAP: buyable, price hidden
    assert status == "no_offer" and v["map_hidden"] is True and v["price"] is None
    assert v["availability_type"] == "IN_STOCK" and v["is_buybox_winner"] is True
    status, v = vals("B0AAAAAAA5")                      # foreign marketplace
    assert status == "non_usd" and v["price"] is None and v["currency"] == "INR"
    assert v["availability_type"] is None and v["is_buybox_winner"] is None
    status, v = vals("B0AAAAAAA6")                      # pre-order, empty message
    assert status == "priced" and v["price"] == 399.99
    assert v["availability"] == ORDERABLE_TEXT["PREORDER"]


def test_not_found_nulls_every_api_column_and_non_answers_are_never_written():
    nf = classify_item("B0AAAAAAA7", None, ERRORS["B0AAAAAAA7"])
    assert nf.status == "not_found"
    assert C.row_values(nf, None) == dict.fromkeys(C.API_FIELDS)
    for o in (classify_item("B0AAAAAAA9", None), PriceOutcome("B0AAAAAAA9", "error")):
        with pytest.raises(ValueError, match="previous values"):
            C.row_values(o, None)
    w = C.CacheWriter(FakeConn(), C.CacheSpec(), consumer=CONSUMER)
    with pytest.raises(ValueError, match="without an answer"):
        w.upsert([("B0AAAAAAA9", "missing", {})])


@pytest.mark.parametrize("status", sorted(C.CARRIES_ITEM))
def test_an_answer_that_lost_its_item_is_never_written_as_blank(status):
    # Writing it would stamp the row fresh with every value NULL; a raw-capture
    # miss must fail loudly instead.
    with pytest.raises(ValueError, match="without its item"):
        C.row_values(PriceOutcome("B0AAAAAAA1", status, price=9.99), None)
    with pytest.raises(ValueError, match="without its item"):
        C.row_values(PriceOutcome("B0AAAAAAA1", status, price=9.99), {})


def test_row_values_clean_what_postgres_would_reject():
    item = copy.deepcopy(ITEMS["B0AAAAAAA1"])
    item["itemInfo"]["title"]["displayValue"] = "Bad\x00Title " + "x" * 3000
    item["detailPageURL"] = "javascript:alert(1)"
    item["images"]["primary"]["large"]["width"] = "wide"
    v = C.row_values(classify_item("B0AAAAAAA1", item), item, max_text_chars=100)
    assert "\x00" not in v["title"] and len(v["title"]) == 100 and v["title"].startswith("BadTitle")
    assert v["detail_page_url"] is None and v["image_w"] is None and v["image_h"] == 500


def test_row_values_drop_lone_surrogates():
    # JSON's \\ud83d (half an emoji) parses into a str psycopg2 cannot encode:
    # one such title used to fail its batch on every run.
    item = copy.deepcopy(ITEMS["B0AAAAAAA1"])
    item["itemInfo"]["title"]["displayValue"] = json.loads('"Widget \\ud83d Pro \\ud83d\\ude00"')
    item["browseNodeInfo"]["browseNodes"][0]["id"] = json.loads('"1000\\udc00000001"')
    v = C.row_values(classify_item("B0AAAAAAA1", item), item)
    assert v["title"] == "Widget  Pro \U0001F600"        # the valid pair survives
    assert v["browse_node_ids"][0] == "1000000001"
    for value in v.values():
        if isinstance(value, str):
            value.encode("utf-8")


def test_resources_follow_the_enabled_columns():
    default = C.resources_for(C.CacheSpec())
    assert default == PRICE_RESOURCES + ("itemInfo.title", "images.primary.large")
    assert set(C.CacheSpec().api_columns) == DEFAULT_API_COLUMNS
    full = C.resources_for(C.build_cache_spec({"columns": FULL_COLUMNS}))
    assert full[len(PRICE_RESOURCES):] == ("itemInfo.title", "images.primary.large",
                                           "browseNodeInfo.browseNodes",
                                           "itemInfo.classifications")
    lean = C.build_cache_spec({"columns": {"image_url": None, "image_w": None, "image_h": None,
                                           "title": "amazon_title"}})
    assert C.resources_for(lean) == PRICE_RESOURCES + ("itemInfo.title",)
    assert "browse_node_ids" not in lean.api_columns and "amazon_title" in lean.api_columns


# ---------------------------------------------------------------------------
# SQL (ported: SQL shapes, no bare '%' in SQL that runs with named params)
# ---------------------------------------------------------------------------

def test_upsert_writes_every_api_column_with_a_fresh_stamp():
    spec = C.build_cache_spec({"columns": FULL_COLUMNS}, period_hours=4)
    sql = C.build_upsert_sql(spec, 2)
    assert sql.startswith("INSERT INTO amazon_item_cache (asin, title, image_url, image_w")
    assert "ON CONFLICT (asin) DO UPDATE SET" in sql
    for col in sorted(LICENSED_API_COLUMNS) + ["outcome", "fetched_at", "consumer"]:
        assert f"{col} = EXCLUDED.{col}" in sql
    assert sql.count("NOW()") == 2 and "%(r1_browse_node_ids)s::text[]" in sql
    assert _no_stray_percent(sql)
    params = C.upsert_params(spec, [("B0AAAAAAA1", "priced", {"title": "t", "price": 9.5})], CONSUMER)
    assert params["r0_key"] == "B0AAAAAAA1" and params["r0_outcome"] == "priced"
    assert params["r0_title"] == "t" and params["r0_image_url"] is None
    assert params["consumer"] == CONSUMER
    # By default the opt-in columns are neither written nor named.
    default_sql = C.build_upsert_sql(C.CacheSpec(), 1)
    for col in OPT_IN_COLUMNS:
        assert col not in default_sql


def test_purge_nulls_every_api_column_and_keeps_our_own():
    spec = C.build_cache_spec({"columns": FULL_COLUMNS}, period_hours=4)
    sql, params = C.build_purge_sql(spec)
    assert params == {"prf_max_age_s": HORIZON_4H * 3600.0}
    sets = sql.split(" WHERE ")[0]
    for col in LICENSED_API_COLUMNS:
        assert f"{col} = NULL" in sets
    for col in OWN_COLUMNS:
        assert f" {col} = NULL" not in sets
    assert "(t.fetched_at IS NULL OR t.fetched_at < NOW() - make_interval(secs => %(prf_max_age_s)s))" in sql
    # Each purged row's key and age come back: the blanking counts are read
    # from what the purge did, not assumed.
    assert sql.endswith("RETURNING t.asin, EXTRACT(EPOCH FROM (NOW() - t.fetched_at)) / 3600.0 AS age_h")
    assert _no_stray_percent(sql) and "prf_spare" not in sql
    # With a spare: the run's unrefreshed keys inside the window are left out,
    # and a row without fetched_at is never spared.
    ssql, sparams = C.build_purge_sql(spec, spare=["B0AAAAAAA1", "B0AAAAAAA1", "B0AAAAAAA2"],
                                      spare_max_age_hours=QUOTABLE_H)
    assert sparams == {"prf_max_age_s": HORIZON_4H * 3600.0,
                       "prf_spare": ["B0AAAAAAA1", "B0AAAAAAA2"],
                       "prf_spare_s": QUOTABLE_H * 3600.0}
    assert ("AND NOT (t.asin = ANY(%(prf_spare)s::text[])\n        AND t.fetched_at IS NOT NULL\n"
            "        AND t.fetched_at >= NOW() - make_interval(secs => %(prf_spare_s)s))") in ssql
    assert ssql.split(" WHERE ")[0] == sets and _no_stray_percent(ssql)
    # Nothing to spare, or no window: the plain purge.
    assert C.build_purge_sql(spec, spare=[], spare_max_age_hours=QUOTABLE_H) == (sql, params)
    assert C.build_purge_sql(spec, spare=["B0AAAAAAA1"], spare_max_age_hours=0) == (sql, params)
    assert C.build_spared_sql(spec, [], QUOTABLE_H) is None
    spared_sql, spared_params = C.build_spared_sql(spec, ["B0AAAAAAA1"], QUOTABLE_H)
    assert "AS spared_key" in spared_sql and spared_params["prf_spare"] == ["B0AAAAAAA1"]
    assert _no_stray_percent(spared_sql)
    # The default spec purges the default columns.
    dsets = C.build_purge_sql(C.build_cache_spec({}, period_hours=4))[0].split(" WHERE ")[0]
    assert {c for c in LICENSED_API_COLUMNS if f"{c} = NULL" in dsets} == DEFAULT_API_COLUMNS
    # A remapped column and another writer's Amazon column are purged too.
    other = C.build_cache_spec({"columns": {"title": "amazon_title"},
                                "extra_purge_columns": ["features_json"]}, period_hours=4)
    osql, _ = C.build_purge_sql(other)
    assert "amazon_title = NULL" in osql and "features_json = NULL" in osql
    assert " title = NULL" not in osql
    stale, stparams = C.build_stale_counts_sql(spec)
    assert "AS stale_api_rows" in stale and "AS api_rows_past_24h" in stale
    assert stparams == {**params, "prf_licence_s": 86400.0} and _no_stray_percent(stale)
    assert C.STALE_COUNT_NAMES == ("stale_api_rows", "stale_price_rows", "api_rows_past_24h")
    # The rows a spare kept are not "should have been purged".
    kept, kparams = C.build_stale_counts_sql(spec, spare=["B0AAAAAAA1"],
                                             spare_max_age_hours=QUOTABLE_H)
    assert "count(*) FILTER (WHERE NOT (t.asin = ANY(%(prf_spare)s::text[])" in kept
    assert kparams["prf_spare"] == ["B0AAAAAAA1"] and _no_stray_percent(kept)
    catalog_sql, cparams = C.build_catalog_count_sql(
        {"table": "v_refresh_catalog", "where": "c.is_active", "asin_regex": "^B[0-9A-Z]{9}$"},
        ["B0AAAAAAA1", "B0AAAAAAA1"])
    assert "FROM v_refresh_catalog c" in catalog_sql and "(c.is_active)" in catalog_sql
    assert cparams["prf_keys"] == ["B0AAAAAAA1"] and _no_stray_percent(catalog_sql)


def test_no_price_history_anywhere():
    # Participation 6(y): no price history, price alerts or "lowest ever".
    spec = C.CacheSpec()
    texts = [C.build_upsert_sql(spec, 1), C.build_purge_sql(spec)[0],
             C.build_stale_counts_sql(spec)[0],
             C.build_freshness_sql(spec, {"table": "v_refresh_catalog"}, display_hours=24,
                                   min_price=1.0)[0]]
    texts += [(AGENT_DIR / n).read_text() for n in ("agent.py", "price_refresh_cache.py",
                                                    "price_refresh_settings.py")]
    for t in texts:
        assert "price_history" not in t.lower() and "lowest ever" not in t.lower()


def test_rank_sql_takes_the_demand_arms_as_params_not_table_scans(storage, picks, monkeypatch):
    fake_signals(monkeypatch)
    s = make_settings(storage)
    cfg = A.priority_config(s, storage=storage)
    tiers = prp.collect_tiers(make_conn(), cfg, storage=storage)
    assert tiers.asins() == ["B0AAAAAAA4", "B0AAAAAAA3", "B0AAAAAAA5"]
    assert "EBAY_1234567" not in tiers.members        # the catalog's ASIN regex
    sql, params = prp.build_rank_sql(cfg, tiers, limit=2500)
    assert "FROM v_refresh_catalog c" in sql and "MIN(c.fetched_at)" in sql
    assert "unnest(%(prp_t_asins)s::text[]" in sql
    assert "ai_traffic_log" not in sql and "outbound_clicks" not in sql
    assert params["prp_tail_gate"] == TAIL_GATE and params["prp_t_gates"] == [12.0, 12.0, 12.0]
    assert _no_stray_percent(sql)


def test_freshness_sql_defaults_to_the_catalog_and_takes_an_override():
    spec = C.CacheSpec()
    cat = {"table": "v_refresh_catalog", "asin_col": "asin", "where": "c.is_active",
           "asin_regex": "^B[0-9A-Z]{9}$"}
    sql, params = C.build_freshness_sql(spec, cat, display_hours=24, min_price=1.0)
    assert "FROM v_refresh_catalog c" in sql and "(c.is_active)" in sql
    assert "t.outcome = 'priced'" in sql and "t.price >= %(prf_min_price)s" in sql
    assert params["prf_display_s"] == 86400.0 and _no_stray_percent(sql)
    sql, _ = C.build_freshness_sql(spec, cat, display_hours=24, min_price=1.0,
                                   indexable_sql="SELECT asin FROM products WHERE indexable")
    assert "AS q(asin)" in sql and "v_refresh_catalog" not in sql
    with pytest.raises(ValueError, match="reserved"):
        C.build_freshness_sql(spec, {**cat, "params": {"prf_x": 1}}, display_hours=24,
                              min_price=1.0)


# ---------------------------------------------------------------------------
# Write retries (ported from the verifier's deadlock-safe writes)
# ---------------------------------------------------------------------------

class RetryConn(FakeConn):
    def __init__(self, fail_next=()):
        super().__init__()
        self.fail_next = list(fail_next)

    def cursor(self):
        conn = self

        class _Cur(FakeCursor):
            def execute(self, sql, params=None):
                conn.executed.append((sql, params))
                if conn.fail_next:
                    exc = conn.fail_next.pop(0)
                    if exc is not None:
                        raise exc
                self.rowcount = 2
                self.rows = [("B0AAAAAAA1", 20.0), ("B0AAAAAAA2", 25.0)]
        return _Cur(self)


def _deadlock():
    from psycopg2 import errors
    return errors.DeadlockDetected("deadlock detected")


def _writer(conn, sleeps):
    return C.CacheWriter(conn, C.CacheSpec(), consumer=CONSUMER, retries=3, backoff_s=1.0,
                         sleep=sleeps.append, jitter=lambda: 0.25)


ROW = [("B0AAAAAAA1", "priced", {"price": 19.99})]


def test_cache_write_retries_a_deadlock():
    sleeps: list = []
    conn = RetryConn(fail_next=[_deadlock()])
    assert _writer(conn, sleeps).upsert(ROW) == 1
    assert conn.rollbacks == 1 and conn.commits == 1 and sleeps == [1.25]


def test_cache_write_retries_serialization_failures():
    from psycopg2 import errors
    sleeps: list = []
    conn = RetryConn(fail_next=[errors.SerializationFailure("could not serialize"),
                                errors.SerializationFailure("again")])
    _writer(conn, sleeps).upsert(ROW)
    assert conn.commits == 1 and conn.rollbacks == 2 and sleeps == [1.25, 2.25]


def test_cache_write_gives_up_after_the_retries():
    sleeps: list = []
    conn = RetryConn(fail_next=[_deadlock() for _ in range(4)])
    with pytest.raises(Exception) as ei:
        _writer(conn, sleeps).upsert(ROW)
    assert type(ei.value).__name__ == "DeadlockDetected"
    assert conn.commits == 0 and conn.rollbacks == 4 and len(sleeps) == 3


def test_other_write_errors_are_not_retried():
    class UndefinedColumn(Exception):
        pgcode = "42703"

    sleeps: list = []
    conn = RetryConn(fail_next=[UndefinedColumn('column "title" does not exist')])
    with pytest.raises(UndefinedColumn):
        _writer(conn, sleeps).upsert(ROW)
    assert sleeps == [] and conn.rollbacks == 1 and conn.commits == 0


class PgDataError(Exception):
    """A per-value Postgres error (SQLSTATE class 22: numeric overflow)."""
    pgcode = "22003"


def _rows(asins):
    return [(a, "priced", {"price": 10.0, "title": f"t {a}"}) for a in asins]


def _failing_for(bad: set, exc_type=PgDataError):
    """An INSERT answer that fails whenever the statement carries a bad ASIN."""
    def answer(sql, params):
        keys = {v for k, v in (params or {}).items() if k.endswith("_key")}
        return exc_type("value out of range") if keys & bad else []
    return answer


def test_a_value_error_skips_only_the_rows_that_carry_it():
    # One answer whose values Postgres cannot store must not stop the batch,
    # the sweep or the purge, run after run: the batch is re-written row by
    # row and the bad row is skipped (it keeps its previous values).
    conn = FakeConn([("INSERT INTO amazon_item_cache", _failing_for({"B0IIIIII02"}))])
    w = C.CacheWriter(conn, C.CacheSpec(), consumer=CONSUMER)
    assert w.upsert(_rows([f"B0IIIIII0{i}" for i in range(5)])) == 4
    assert w.rows_written == 4 and w.poison == [("B0IIIIII02", "PgDataError")]
    inserts = conn.sql("INSERT INTO amazon_item_cache")
    assert len(inserts) == 6                              # the batch, then one per row
    assert conn.commits == 4 and conn.rollbacks == 2
    # A client-side encoding error is a value error too; a single bad row is
    # skipped without being re-sent.
    conn = FakeConn([("INSERT INTO amazon_item_cache",
                      _failing_for({"B0IIIIII09"}, lambda m: UnicodeEncodeError(
                          "utf-8", "\ud83d", 0, 1, "surrogates not allowed")))])
    w = C.CacheWriter(conn, C.CacheSpec(), consumer=CONSUMER)
    assert w.upsert(_rows(["B0IIIIII09"])) == 0
    assert w.poison == [("B0IIIIII09", "UnicodeEncodeError")]
    assert len(conn.sql("INSERT INTO")) == 1


def test_a_value_error_on_every_row_is_the_table_and_raises():
    conn = FakeConn([("INSERT INTO amazon_item_cache", PgDataError("value too long"))])
    w = C.CacheWriter(conn, C.CacheSpec(), consumer=CONSUMER)
    with pytest.raises(PgDataError):
        w.upsert(_rows(["B0IIIIII01", "B0IIIIII02", "B0IIIIII03"]))
    assert w.rows_written == 0 and w.poison == [] and conn.commits == 0
    assert C.is_value_error(PgDataError()) and C.is_value_error(ValueError("NUL"))
    assert not C.is_value_error(RuntimeError("server closed the connection"))


def test_retry_also_recognises_the_sqlstate_and_covers_the_purge():
    class PgErr(Exception):
        pgcode = "40P01"

    sleeps: list = []
    conn = RetryConn(fail_next=[PgErr("deadlock")])
    assert _writer(conn, sleeps).purge() == 2
    assert conn.rollbacks == 1 and conn.commits == 1 and sleeps == [1.25]
    # The detailed form returns every purged key with its age; with a spare it
    # lists the spared keys in the same transaction (re-run with it).
    conn = RetryConn(fail_next=[None, PgErr("deadlock")])
    res = _writer(conn, sleeps).purge_report(spare=["B0AAAAAAA9"], spare_max_age_hours=QUOTABLE_H)
    assert res.purged == (("B0AAAAAAA1", 20.0), ("B0AAAAAAA2", 25.0)) and res.count == 2
    assert res.within(QUOTABLE_H) == ["B0AAAAAAA1"] and res.keys() == ["B0AAAAAAA1", "B0AAAAAAA2"]
    assert len(conn.sql("UPDATE amazon_item_cache")) == 2 and len(conn.sql("AS spared_key")) == 2
    assert conn.commits == 1


# ---------------------------------------------------------------------------
# Engine runs
# ---------------------------------------------------------------------------

def test_successful_run_refreshes_in_demand_order_then_purges(storage, picks, monkeypatch):
    calls = fake_signals(monkeypatch)
    s = make_settings(storage)
    client, conn = FakeCreators(), make_conn()
    rep, seen = run(s, storage, client, conn)
    assert rep.status == "success", rep.summary
    assert seen["tags"] == ["example-site-20"]
    # One getItems call, ASINs in rank order: clicked, assistant, picks, then
    # the long tail never-checked first and oldest first. Fresh B0AAAAAAA8 waits.
    assert client.calls == [EXPECTED_ORDER]
    assert client.resources_seen == [s.resources]
    # The click and AI signals were read with the site's definitions.
    assert calls["clicks"][0]["table"] == "outbound_clicks"
    assert calls["clicks"][0]["match"] == {"target": "amazon", "via": "beacon"}
    assert calls["ai"][0]["prefixes"] == ["/product/"]
    inserts = conn.sql("INSERT INTO amazon_item_cache")
    assert len(inserts) == 2
    first = inserts[0][1]
    assert [first[f"r{i}_key"] for i in range(6)] == [a for a in EXPECTED_ORDER if a != "B0AAAAAAA7"]
    assert "r6_key" not in first                       # not_found held to the end of the sweep
    by_asin = {first[f"r{i}_key"]: i for i in range(6)}
    i4, i1 = by_asin["B0AAAAAAA4"], by_asin["B0AAAAAAA1"]
    assert first[f"r{i4}_outcome"] == "no_offer" and first[f"r{i4}_map_hidden"] is True
    assert first[f"r{i1}_outcome"] == "priced" and first[f"r{i1}_price"] == 1299.0
    assert first[f"r{i1}_title"] == "Example Widget Pro 2000, 120-Piece Kit"
    assert first["consumer"] == CONSUMER
    nf = inserts[1][1]
    assert nf["r0_key"] == "B0AAAAAAA7" and nf["r0_outcome"] == "not_found"
    assert all(nf[f"r0_{f}"] is None for f in s.cache.fields)
    assert not any(f"r0_{f}" in nf for f in OPT_IN_COLUMNS)
    # The purge ran once, after every write.
    stmts = [sql for sql, _ in conn.executed]
    purge_at = [i for i, q in enumerate(stmts) if q.startswith("UPDATE amazon_item_cache AS t")]
    insert_at = [i for i, q in enumerate(stmts) if q.startswith("INSERT INTO amazon_item_cache")]
    assert len(purge_at) == 1 and purge_at[0] > max(insert_at)
    m = rep.metrics
    assert (m["candidates"], m["api_calls"], m["budget_calls"]) == (7, 1, 250)
    assert (m["refreshed"], m["out_of_stock"], m["no_offer"], m["non_usd"], m["not_found"],
            m["map_hidden"], m["missing"]) == (2, 1, 2, 1, 1, 1, 0)
    assert (m["written_rows"], m["not_found_written"], m["not_found_held"]) == (7, 1, 0)
    assert (m["purged_rows"], m["purge_skipped"], m["blanked_on_failed_run"]) == (3, 0, 0)
    assert (m["candidates_clicked"], m["candidates_assistant_fetched"], m["candidates_picks"],
            m["candidates_long_tail"]) == (1, 1, 1, 4)
    assert m["demand_ranking"] == 1 and m["demand_signal_errors"] == 0
    assert m["gate_buffer_violations"] == 0
    assert (m["indexable_pages"], m["fresh_checked_pct"], m["fresh_priced_pct"]) == (8, 87.5, 75.0)
    assert (m["stale_api_rows"], m["stale_price_rows"]) == (0, 0)
    assert m["demand_fresh_pct"] == 100.0 and m["fresh_pct_long_tail"] == 60.0
    assert m["hours_since_last_purge"] == 0.0
    assert (m["purge_horizon_hours"], m["quotable_hours"], m["poison_rows"]) == (
        HORIZON_4H, QUOTABLE_H, 0)
    assert 0 <= m["purge_offset_s"] < 60
    assert rep.state["last_purge_at"] and rep.state["last_successful_sweep_at"]
    assert "purge NULLed 3 row(s) older than 19.5h" in rep.summary
    # Purge and stale counts use the horizon; freshness uses the quotable window.
    assert conn.sql("UPDATE amazon_item_cache")[0][1] == {"prf_max_age_s": HORIZON_4H * 3600.0}
    assert conn.sql("WITH idx AS MATERIALIZED")[0][1]["prf_display_s"] == QUOTABLE_H * 3600.0
    assert conn.sql("FULL JOIN t")[0][1]["prp_display_s"] == QUOTABLE_H * 3600.0
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in m.values())


def test_lwa_token_timeout_blanks_nothing(storage, picks, monkeypatch):
    """2026-11-11: one LWA token timeout left ~3.8k pages of the reference site
    without a price for ~6h. Here it must leave every cached value as it was."""
    fake_signals(monkeypatch)
    conn = make_conn()
    rep, _ = run(make_settings(storage), storage, FakeCreators(unavailable_after=0), conn)
    assert rep.status == "failure"
    assert "LWA token request failed" in rep.error_text
    assert "nothing written, every cached value kept, purge skipped" in rep.summary
    assert conn.sql("INSERT INTO") == [] and conn.sql("UPDATE ") == []
    assert conn.commits == 0                       # no write transaction at all
    m = rep.metrics
    assert (m["written_rows"], m["purged_rows"], m["purge_skipped"],
            m["blanked_on_failed_run"]) == (0, 0, 1, 0)
    assert "last_purge_at" not in rep.state and "last_successful_sweep_at" not in rep.state


def test_mid_run_outage_keeps_the_work_and_skips_the_purge(storage, monkeypatch):
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"B0BBBBBB{i:02d}": None for i in range(15)}
    items = {a: _priced_item(a, 10.0 + i) for i, a in enumerate(catalog) if a != "B0BBBBBB03"}
    client = FakeCreators(items=items, errors={"B0BBBBBB03": "ItemNotAccessible B0BBBBBB03"},
                          unavailable_after=1, unavailable_msg="Creators getItems unreachable: timed out")
    conn = make_conn(catalog)
    rep, _ = run(make_settings(storage), storage, client, conn)
    assert rep.status == "success"                 # partial: the first batch is kept
    m = rep.metrics
    assert m["aborted"] == 1 and m["api_calls"] == 1 and m["written_rows"] == 9
    assert m["not_found_written"] == 0 and m["not_found_held"] == 1
    assert conn.sql("UPDATE amazon_item_cache") == [] and m["purge_skipped"] == 1
    assert m["blanked_on_failed_run"] == 0 and "purge skipped (the sweep stopped early)" in rep.summary
    assert "last_successful_sweep_at" not in rep.state


def test_persistent_throttle_skips_the_purge(storage, monkeypatch):
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"B0CCCCCC{i:02d}": None for i in range(25)}
    items = {a: _priced_item(a, 20.0) for a in catalog}
    conn = make_conn(catalog)
    rep, _ = run(make_settings(storage), storage, FakeCreators(items=items, throttle_after=2), conn)
    m = rep.metrics
    assert rep.status == "success" and m["throttled"] == 1 and m["written_rows"] == 20
    assert m["purge_skipped"] == 1 and conn.sql("UPDATE amazon_item_cache") == []
    assert "Creators throttled" in rep.summary_md or "429" in rep.summary


def test_spent_budget_makes_no_call_but_still_purges(storage, monkeypatch):
    # Budget exhaustion is normal operation, not a failure: rows past the
    # horizon must still go. Ported from "budget spent: refresh() stops after
    # sizing". The run asked about nothing, so nothing is spared, and the one
    # due catalog row still displayable (B0AAAAAAA5, 20h) is reported blanked.
    fake_signals(monkeypatch)
    client, conn = FakeCreators(budget=0), make_conn()
    rep, seen = run(make_settings(storage), storage, client, conn)
    assert rep.status == "success" and client.calls == []
    assert conn.sql("LEFT JOIN t ON t.asin = c.asin") == []    # nothing ranked
    m = rep.metrics
    assert (m["budget_left_at_start"], m["budget_calls"], m["candidates"]) == (0, 0, 0)
    assert m["purge_skipped"] == 0 and m["spared_rows"] == 0
    # 3 retired rows (30h) + B0AAAAAAA2 (30h), B0AAAAAAA7 (25h), B0AAAAAAA5 (20h).
    assert sorted(conn.cache.purged) == ["B0AAAAAAA2", "B0AAAAAAA5", "B0AAAAAAA7",
                                         "B0RETIRED0", "B0RETIRED1", "B0RETIRED2"]
    assert m["purged_rows"] == 6 and m["blanked_due_rows"] == 1
    assert m["blanked_on_failed_run"] == 0              # nothing was asked: nothing failed
    catalog_q = conn.sql("AS catalog_rows")
    assert len(catalog_q) == 1 and catalog_q[0][1]["prf_keys"] == ["B0AAAAAAA5"]
    assert "(1 of them due catalog rows still displayable" in rep.summary
    assert rep.summary.startswith("No Creators call: the daily budget of consumer "
                                  f"{CONSUMER} is spent")
    assert any(c == "warning" and "1 due catalog row(s)" in msg for c, msg in seen["decisions"])


def test_budget_clamps_the_run(storage, monkeypatch):
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"B0DDDDDD{i:02d}": None for i in range(40)}
    items = {a: _priced_item(a, 30.0) for a in catalog}
    client, conn = FakeCreators(items=items, budget=2), make_conn(catalog)
    rep, _ = run(make_settings(storage), storage, client, conn)
    assert [len(c) for c in client.calls] == [10, 10]
    rank_params = conn.sql("LEFT JOIN t ON t.asin = c.asin")[0][1]
    assert rank_params["prp_limit"] == 20            # 2 calls x 10 ASINs
    assert rep.metrics["budget_calls"] == 2 and rep.metrics["purge_skipped"] == 0


def test_write_failure_fails_the_run_without_purging(storage, picks, monkeypatch):
    fake_signals(monkeypatch)
    conn = make_conn(extra=[("INSERT INTO amazon_item_cache", ValueError("value too long"))])
    rep, _ = run(make_settings(storage), storage, FakeCreators(), conn)
    assert rep.status == "failure" and "value too long" in rep.error_text
    assert conn.sql("UPDATE amazon_item_cache") == []
    m = rep.metrics
    assert m["purge_skipped"] == 1 and m["written_rows"] == 0 and m["blanked_on_failed_run"] == 0
    assert "sweep FAILED (write" in rep.summary


def test_write_failure_mid_run_keeps_and_counts_the_batches_before_it(storage, monkeypatch):
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"B0HHHHHH{i:02d}": None for i in range(25)}
    items = {a: _priced_item(a, 50.0) for a in catalog}
    second_batch = set(list(catalog)[10:20])

    def insert(sql, params):
        # Every row of the second batch fails, on its own too: the table's
        # fault, not one answer's, so the sweep stops there.
        keys = {v for k, v in params.items() if k.endswith("_key")}
        return (ValueError("value too long for type character varying(100)")
                if keys & second_batch else [])

    conn = make_conn(catalog, extra=[("INSERT INTO amazon_item_cache", insert)])
    client = FakeCreators(items=items)
    rep, _ = run(make_settings(storage), storage, client, conn)
    m = rep.metrics
    assert rep.status == "failure" and len(client.calls) == 2   # the sweep stopped at the error
    assert (m["written_rows"], m["refreshed"], m["api_calls"], m["untried"]) == (10, 10, 2, 15)
    assert m["purge_skipped"] == 1 and conn.sql("UPDATE amazon_item_cache") == []
    assert "10 row(s) written before it" in rep.summary


def test_not_found_guard_holds_a_mass_not_found(storage, monkeypatch):
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"B0EEEEEE{i:02d}": None for i in range(30)}
    errors = {a: f"ItemNotAccessible {a}" for a in catalog}
    conn = make_conn(catalog)
    rep, _ = run(make_settings(storage), storage, FakeCreators(items={}, errors=errors), conn)
    m = rep.metrics
    assert rep.status == "failure" and m["not_found"] == 30 and m["not_found_held"] == 30
    assert conn.sql("INSERT INTO amazon_item_cache") == []
    assert conn.sql("UPDATE amazon_item_cache") == [] and m["purge_skipped"] == 1
    assert "not_found guard tripped" in rep.summary and m["blanked_on_failed_run"] == 0


def test_not_found_below_the_guard_is_written_after_the_sweep(storage, monkeypatch):
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"B0FFFFFF{i:02d}": None for i in range(30)}
    nf = list(catalog)[:5]
    items = {a: _priced_item(a, 40.0) for a in catalog if a not in nf}
    conn = make_conn(catalog)
    rep, _ = run(make_settings(storage), storage,
                 FakeCreators(items=items, errors={a: f"ItemNotAccessible {a}" for a in nf}), conn)
    m = rep.metrics
    assert rep.status == "success" and m["not_found_written"] == 5 and m["not_found_held"] == 0
    assert m["written_rows"] == 30 and m["purge_skipped"] == 0


def test_kill_switch_falls_back_to_oldest_first(storage, monkeypatch):
    # Ported: storage by_agent_id.<id>.enabled=false switches the ranking off
    # without a commit; the run reads no demand signal and goes oldest first.
    fake_signals(monkeypatch, forbid=True)
    storage.write_json(prp.CONFIG_KEY, {"by_agent_id": {AGENT_ID: {"enabled": False}}})
    client, conn = FakeCreators(), make_conn()
    rep, seen = run(make_settings(storage), storage, client, conn)
    assert rep.status == "success" and rep.metrics["demand_ranking"] == 0
    params = conn.sql("LEFT JOIN t ON t.asin = c.asin")[0][1]
    assert params["prp_t_asins"] == [] and params["prp_tail_on"] is True
    assert client.calls == [["B0AAAAAAA1", "B0AAAAAAA6", "B0AAAAAAA2", "B0AAAAAAA7",
                             "B0AAAAAAA5", "B0AAAAAAA3"]]   # never checked, then oldest past 15.5h
    assert any("switched off" in msg for _, msg in seen["decisions"])


def test_a_failing_signal_empties_only_its_group(storage, picks, monkeypatch):
    # Ported: an unavailable AI-traffic read degrades to "no AI arm this run".
    fake_signals(monkeypatch, fail_ai=True)
    client, conn = FakeCreators(), make_conn()
    rep, seen = run(make_settings(storage), storage, client, conn)
    assert rep.status == "success" and rep.metrics["demand_signal_errors"] == 1
    assert client.calls[0][:2] == ["B0AAAAAAA4", "B0AAAAAAA5"]  # clicked, then picks
    assert any("assistant-fetched" in msg and "statement timeout" in msg
               for c, msg in seen["decisions"] if c == "warning")
    # Every signal read ran in a savepoint that was rolled back: the caller's
    # transaction stays usable for the writes.
    assert len(conn.sql("ROLLBACK TO SAVEPOINT price_refresh_priority")) >= 3


def test_no_credentials_or_no_db_touches_nothing(storage):
    s = make_settings(storage)
    conn = FakeConn()
    rep = A.run_refresh(s, storage=storage, client_factory=lambda *a: None,
                        connect=lambda *a, **k: conn, say=lambda *a, **k: None)
    assert rep.status == "failure" and "not configured" in rep.summary and conn.executed == []

    def no_db(*a, **k):
        raise RuntimeError("could not connect to server")

    rep = A.run_refresh(s, storage=storage, client_factory=lambda *a: FakeCreators(),
                        connect=no_db, say=lambda *a, **k: None)
    assert rep.status == "failure" and "Cannot reach the site DB" in rep.summary
    env_tag = make_settings(storage, lambda b: (b["creators"].pop("partner_tag"),
                                                b["creators"].__setitem__("partner_tag_env",
                                                                          "EXAMPLE_TAG")))
    rep = A.run_refresh(env_tag, storage=storage, client_factory=lambda *a: FakeCreators(),
                        connect=lambda *a, **k: conn, say=lambda *a, **k: None, environ={})
    assert rep.status == "failure" and "Partner tag" in rep.summary


def test_ranking_query_failure_spends_no_call(storage, picks, monkeypatch):
    fake_signals(monkeypatch)
    client = FakeCreators()
    conn = make_conn(extra=[("LEFT JOIN t ON t.asin = c.asin",
                             RuntimeError('relation "v_refresh_catalog" does not exist'))])
    rep, _ = run(make_settings(storage), storage, client, conn)
    assert rep.status == "failure" and client.calls == []
    assert conn.sql("INSERT INTO") == [] and conn.sql("UPDATE amazon_item_cache") == []


def test_dry_run_writes_nothing(storage, capsys):
    client = FakeCreators()
    rep = A.run_refresh(make_settings(storage), storage=storage,
                        client_factory=lambda *a: client,
                        connect=lambda *a, **k: pytest.fail("a dry run over ASINs opened the DB"),
                        say=lambda *a, **k: None, dry_run=True,
                        explicit_asins=["B0AAAAAAA1", "B0AAAAAAA7"], limit_asins=10)
    out = capsys.readouterr().out
    assert rep.status == "success" and client.calls == [["B0AAAAAAA1", "B0AAAAAAA7"]]
    assert "DRY B0AAAAAAA1 priced" in out and "title=yes" in out
    assert rep.metrics["dry_run"] == 1 and rep.metrics["purge_skipped"] == 1
    with pytest.raises(ValueError):
        A.run_refresh(make_settings(storage), storage=storage, explicit_asins=["B0AAAAAAA1"])


def _catalog_run(storage, monkeypatch, prefix: str, n: int, *, conn_kw=None, **client_kw):
    """A run over ``n`` never-checked ASINs, all priced unless the client
    says otherwise; demand signals empty."""
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"{prefix}{i:02d}": None for i in range(n)}
    client_kw.setdefault("items", {a: _priced_item(a, 25.0) for a in catalog})
    client = FakeCreators(**client_kw)
    conn = make_conn(catalog, **(conn_kw or {}))
    rep, seen = run(make_settings(storage), storage, client, conn)
    return rep, seen, client, conn, list(catalog)


def test_every_batch_rejected_fails_the_run_and_skips_the_purge(storage, monkeypatch):
    # e.g. getItems refusing a resource name: every batch is an HTTP 400 and
    # refresh_prices() aborts after max_consecutive_errors. That used to be a
    # green run that refreshed nothing and never purged again.
    rep, _, client, conn, _ = _catalog_run(storage, monkeypatch, "B0JJJJJJ", 40,
                                           error_batches={0, 1, 2, 3})
    m = rep.metrics
    assert rep.status == "failure" and len(client.calls) == 3
    assert (m["api_calls"], m["batch_errors"], m["aborted"]) == (0, 3, 1)
    assert m["written_rows"] == 0 and conn.sql("INSERT INTO") == []
    assert m["purge_skipped"] == 1 and conn.sql("UPDATE amazon_item_cache") == []
    assert m["blanked_on_failed_run"] == 0
    assert "InvalidParameterValue" in rep.error_text and "browseNodeInfo" in rep.error_text
    assert rep.summary.startswith("Creators answered none of the getItems calls for 40 due")
    assert "purge skipped" in rep.summary and "last_successful_sweep_at" not in rep.state


def test_every_batch_rejected_below_the_abort_threshold_never_purges(storage, monkeypatch):
    # Two batches, both rejected: refresh_prices() does not abort (3 in a row
    # needed), and the old gating purged after a run that answered nothing.
    rep, _, client, conn, _ = _catalog_run(storage, monkeypatch, "B0KKKKKK", 15,
                                           error_batches={0, 1})
    m = rep.metrics
    assert rep.status == "failure" and len(client.calls) == 2
    assert (m["api_calls"], m["batch_errors"], m["aborted"]) == (0, 2, 0)
    assert conn.sql("UPDATE amazon_item_cache") == [] and m["purged_rows"] == 0
    assert m["purge_skipped"] == 1 and m["blanked_on_failed_run"] == 0
    assert "Purge (horizon 19.5h): skipped (Creators answered no call)" in rep.summary_md


def test_a_429_from_the_first_call_fails_the_run(storage, monkeypatch):
    rep, _, client, conn, _ = _catalog_run(storage, monkeypatch, "B0LLLLLL", 15,
                                           throttle_after=0)
    m = rep.metrics
    assert rep.status == "failure" and m["api_calls"] == 0 and m["throttled"] == 1
    assert conn.sql("INSERT INTO") == [] and conn.sql("UPDATE amazon_item_cache") == []
    assert "persistent HTTP 429" in rep.error_text and m["purge_skipped"] == 1


def _aged_run(storage, monkeypatch, prefix: str, ages: list, *, mutate=None,
              ignore_spare: bool = False, **client_kw):
    """A long-tail run over ASINs ``prefix + NN`` cached ``ages[i]`` hours ago
    (None = never checked), every one answered with a price unless the client
    says otherwise; demand signals empty."""
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"{prefix}{i:02d}": age for i, age in enumerate(ages)}
    client_kw.setdefault("items", {a: _priced_item(a, 25.0) for a in catalog})
    client = FakeCreators(**client_kw)
    cache = FakeCache({a: age for a, age in catalog.items() if age is not None},
                      ignore_spare=ignore_spare)
    conn = make_conn(catalog, cache=cache)
    rep, seen = run(make_settings(storage, mutate), storage, client, conn)
    return rep, seen, client, conn, list(catalog)


def test_one_isolated_failed_batch_keeps_its_rows_and_the_run_succeeds(storage, monkeypatch):
    """The review's probe A: 30 long-tail rows cached 20h ago (past the 19.5h
    horizon, still displayable), 3 calls, the second batch rejected. That
    batch is a failed run for its rows: the purge must not blank them. It used
    to (this test passed only because its rows had no cache data and the fake
    purge was canned), while the summary said they 'kept their previous
    values' and blanked_on_failed_run read 0."""
    rep, seen, client, conn, asins = _aged_run(storage, monkeypatch, "B0MMMMMM", [20.0] * 30,
                                               error_batches={1})
    m = rep.metrics
    assert rep.status == "success", rep.summary
    assert (m["api_calls"], m["batch_errors"], m["aborted"], m["written_rows"]) == (2, 1, 0, 20)
    failed = client.calls[1]
    assert failed == asins[10:20]
    written = {v for _, p in conn.sql("INSERT INTO amazon_item_cache")
               for k, v in p.items() if k.endswith("_key")}
    assert written == set(asins[:10] + asins[20:])
    # The purge ran, carrying the failed batch as its spare and the quotable
    # window as the spare window; it NULLed none of those rows.
    purges = conn.sql("UPDATE amazon_item_cache")
    assert len(purges) == 1 and m["purge_skipped"] == 0
    assert purges[0][1]["prf_spare"] == failed
    assert purges[0][1]["prf_spare_s"] == QUOTABLE_H * 3600.0
    assert conn.cache.purged == {} and all(conn.cache.ages[a] == 20.0 for a in failed)
    assert (m["failed_batch_asins"], m["unrefreshed_asins"], m["spared_rows"]) == (10, 10, 10)
    assert (m["purged_rows"], m["purged_unrefreshed_rows"], m["blanked_on_failed_run"],
            m["blanked_due_rows"]) == (0, 0, 0, 0)
    assert "1 failed batch(es): no answer for 10 ASIN(s), nothing written for them" in rep.summary
    assert "purge NULLed 0 row(s) older than 19.5h; spared 10 row(s) this run could not refresh" \
        in rep.summary
    assert "kept their previous values" not in rep.summary
    # The stale-row counts leave the spared rows out (they were kept on purpose).
    stale_params = conn.sql("AS stale_api_rows")[0][1]
    assert stale_params["prf_spare"] == failed


def test_a_failed_batch_row_past_the_quotable_window_is_purged_not_spared(storage, monkeypatch):
    # The spare keeps what a visitor can still see. A row of a failed batch
    # that is already past the quotable window (24.5h) shows nothing, so the
    # purge removes it (license hygiene), and that is not counted as blanking.
    ages = [24.5] * 11 + [20.0] * 19          # batch 0: ten 24.5h rows; batch 1: one + nine 20h
    rep, _, client, conn, asins = _aged_run(storage, monkeypatch, "B0SSSSSS", ages,
                                            error_batches={1})
    m = rep.metrics
    assert rep.status == "success" and client.calls[1] == asins[10:20]
    assert sorted(conn.cache.purged) == [asins[10]]
    assert (m["spared_rows"], m["purged_unrefreshed_rows"], m["blanked_on_failed_run"],
            m["blanked_due_rows"]) == (9, 1, 0, 0)
    assert "1 row(s) it could not refresh were already past that window and were purged" \
        in rep.summary


def test_omitted_and_unstorable_answers_are_spared_too(storage, monkeypatch):
    # An ASIN Amazon silently left out of an answer (missing) and an answer
    # whose values cannot be stored (poison) are not refreshed either: their
    # rows keep their values while displayable, and the next run re-asks them.
    omitted, poisoned = "B0UUUUUU03", "B0UUUUUU05"
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    catalog = {f"B0UUUUUU{i:02d}": 21.0 for i in range(10)}
    items = {a: _priced_item(a, 25.0) for a in catalog if a != omitted}
    cache = FakeCache(dict(catalog))
    insert = cache.insert

    def insert_or_poison(sql, params):
        keys = {v for k, v in params.items() if k.endswith("_key")}
        return PgDataError("numeric field overflow") if poisoned in keys else insert(sql, params)

    conn = make_conn(catalog, cache=cache,
                     extra=[("INSERT INTO amazon_item_cache", insert_or_poison)])
    rep, _ = run(make_settings(storage), storage, FakeCreators(items=items), conn)
    m = rep.metrics
    assert rep.status == "success", rep.summary
    assert (m["missing"], m["poison_rows"], m["unrefreshed_asins"], m["spared_rows"]) == (1, 1, 2, 2)
    assert conn.cache.purged == {}
    assert conn.cache.ages[omitted] == 21.0 and conn.cache.ages[poisoned] == 21.0
    assert sorted(conn.sql("UPDATE amazon_item_cache")[0][1]["prf_spare"]) == [omitted, poisoned]


def test_budget_spill_at_the_first_due_run_is_reported_as_blanked(storage, monkeypatch):
    """The review's probe B, on the binding 16h tail: 30 rows cached 20h ago,
    a 2-call budget. The 10 rows the budget left out were never asked, so they
    are not spared: the purge NULLs them (the license horizon), and the run
    now says so (blanked_due_rows, the due backlog, the gate check) instead of
    reading as a clean success."""
    def tail16(b):
        b["demand_priority"]["long_tail"]["gate_hours"] = 16
        b["schedule"]["long_tail_tolerate_failed_runs"] = 0

    rep, seen, client, conn, asins = _aged_run(storage, monkeypatch, "B0VVVVVV", [20.0] * 30,
                                               mutate=tail16, budget=2)
    m = rep.metrics
    assert rep.status == "success" and [len(c) for c in client.calls] == [10, 10]
    assert sorted(conn.cache.purged) == asins[20:]
    assert (m["purged_rows"], m["blanked_due_rows"], m["spared_rows"],
            m["blanked_on_failed_run"]) == (10, 10, 0, 0)
    assert m["gate_buffer_violations"] == 1
    assert any(c == "warning" and msg == f"gate buffer: {NO_SLACK_16H}"
               for c, msg in seen["decisions"])
    assert "purge NULLed 10 row(s) older than 19.5h (10 of them due catalog rows still " \
           "displayable" in rep.summary
    assert m["due_backlog"] == 2 and "2 row(s) still due" in rep.summary    # the fake tier rows


def test_a_purge_that_ignored_the_spare_would_be_counted(storage, monkeypatch):
    # blanked_on_failed_run is read from the purge's RETURNING ages, so a purge
    # that dropped the spare (a regression) shows up instead of reading 0.
    rep, seen, _, conn, asins = _aged_run(storage, monkeypatch, "B0WWWWWW", [20.0] * 30,
                                          error_batches={1}, ignore_spare=True)
    m = rep.metrics
    assert sorted(conn.cache.purged) == asins[10:20]
    assert m["blanked_on_failed_run"] == 10 and m["purged_unrefreshed_rows"] == 10
    assert any(c == "warning" and "an engine regression" in msg for c, msg in seen["decisions"])


def test_dry_run_fails_when_a_call_is_rejected(storage, capsys):
    # The first-run checklist relies on the dry run to catch a resource name
    # getItems does not accept: it must not exit 0 over a rejected batch.
    client = FakeCreators(error_batches={0})
    rep = A.run_refresh(make_settings(storage), storage=storage,
                        client_factory=lambda *a: client,
                        connect=lambda *a, **k: pytest.fail("a dry run over ASINs opened the DB"),
                        say=lambda *a, **k: None, dry_run=True,
                        explicit_asins=["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"], limit_asins=10)
    assert rep.status == "failure" and rep.metrics["batch_errors"] == 1
    assert "InvalidParameterValue" in rep.summary and "InvalidParameterValue" in rep.error_text
    assert rep.summary.startswith("[DRY RUN] Creators answered none")
    # Partly answered: still a failure for the dry run, with the error shown.
    client = FakeCreators(error_batches={1})
    rep = A.run_refresh(make_settings(storage), storage=storage,
                        client_factory=lambda *a: client, connect=lambda *a, **k: None,
                        say=lambda *a, **k: None, dry_run=True,
                        explicit_asins=[f"B0NNNNNN{i:02d}" for i in range(15)], limit_asins=20)
    assert rep.status == "failure" and rep.metrics["api_calls"] == 1
    assert "1 of 2 getItems call(s) failed" in rep.error_text
    assert "(last: CreatorsRequestError" in rep.summary


def test_an_answer_that_cannot_be_stored_never_stops_the_run(storage, monkeypatch):
    # One poison answer used to fail its batch on every run (it is never
    # written, so it stays at the head of the queue) and block the purge.
    bad = "B0OOOOOO03"
    rep, seen, client, conn, _ = _catalog_run(
        storage, monkeypatch, "B0OOOOOO", 25,
        conn_kw={"extra": [("INSERT INTO amazon_item_cache", _failing_for({bad}))]})
    m = rep.metrics
    assert rep.status == "success", rep.summary
    assert (m["written_rows"], m["poison_rows"], m["api_calls"]) == (24, 1, 3)
    assert len(conn.sql("UPDATE amazon_item_cache")) == 1 and m["purge_skipped"] == 0
    assert any(c == "warning" and bad in msg and "PgDataError" in msg
               for c, msg in seen["decisions"])
    assert "1 answer(s) could not be stored and were skipped" in rep.summary


def test_a_value_error_on_every_row_still_fails_the_run(storage, monkeypatch):
    rep, _, _, conn, _ = _catalog_run(
        storage, monkeypatch, "B0PPPPPP", 12,
        conn_kw={"extra": [("INSERT INTO amazon_item_cache", PgDataError("value too long"))]})
    assert rep.status == "failure" and "sweep FAILED (write" in rep.summary
    assert rep.metrics["written_rows"] == 0 and conn.sql("UPDATE amazon_item_cache") == []


def test_rows_past_24h_with_a_skipped_purge_escalate(storage, monkeypatch):
    # A partial run (throttled after one batch) may skip the purge, but not
    # while rows already hold Amazon data past 24h: that needs a person.
    rep, _, _, _, _ = _catalog_run(storage, monkeypatch, "B0QQQQQQ", 25, throttle_after=1,
                                   conn_kw={"stale": (7, 3, 2)})
    m = rep.metrics
    assert m["throttled"] == 1 and m["api_calls"] == 1 and m["purge_skipped"] == 1
    assert (m["stale_api_rows"], m["stale_price_rows"], m["api_rows_past_24h"]) == (7, 3, 2)
    assert rep.status == "failure"
    assert rep.error_text.startswith("2 cache row(s) hold Amazon data older than 24h")
    assert "2 row(s) hold Amazon data past 24h" in rep.summary
    # Rows past the horizon but not yet past 24h: a success that says so.
    rep, _, _, _, _ = _catalog_run(storage, monkeypatch, "B0RRRRRR", 25, throttle_after=1,
                                   conn_kw={"stale": (7, 3, 0)})
    assert rep.status == "success"
    assert "7 row(s) past the 19.5h purge horizon await the next purge" in rep.summary


def test_no_session_is_idle_in_a_transaction_across_a_creators_call(storage, picks, monkeypatch):
    # The ranking reads in savepoints, which leave the outer transaction open;
    # the LWA token fetch alone can take minutes (idle_in_transaction timeouts,
    # Azure's load balancer). The read transaction is closed first.
    fake_signals(monkeypatch)
    conn, states = make_conn(), []
    client = FakeCreators(on_call=lambda n: states.append(conn.tx_open))
    rep, _ = run(make_settings(storage), storage, client, conn)
    assert rep.status == "success" and states == [False]
    assert conn.sql("ROLLBACK TO SAVEPOINT price_refresh_priority")   # the ranking ran first


def test_a_connection_lost_before_the_sweep_spends_no_call(storage, picks, monkeypatch):
    fake_signals(monkeypatch)
    conn = make_conn()

    def dead():
        raise RuntimeError("server closed the connection unexpectedly")

    conn.rollback = dead
    client = FakeCreators()
    rep, _ = run(make_settings(storage), storage, client, conn)
    assert rep.status == "failure" and client.calls == []
    assert "connection failed before the sweep" in rep.summary


def test_default_collaborators_are_looked_up_at_call_time(storage):
    # The offline guard patches the module's defaults; run_refresh must use
    # the patched ones (they used to be bound at definition time).
    with pytest.raises(AssertionError, match="live Creators"):
        A.run_refresh(make_settings(storage), storage=storage, say=lambda *a, **k: None)
    rep = A.run_refresh(make_settings(storage), storage=storage, say=lambda *a, **k: None,
                        client_factory=lambda *a: FakeCreators())
    assert rep.status == "failure" and "Cannot reach the site DB" in rep.summary
    assert "AssertionError" in rep.error_text


def test_usage_ledger_metrics(storage):
    from framework.core import amazon_creators as ac
    storage.write_json(ac.BUDGET_CONFIG_KEY, {"total_calls_per_day": 8000,
                                              "shares": {CONSUMER: 0.07}})
    storage.write_json(ac.usage_key(CONSUMER), {"calls": 140, "throttled": 3})
    storage.write_json(ac.usage_key("price-refresh"), {"calls": 3000})
    m = A.creators_usage_metrics(CONSUMER, storage)
    assert m == {"creators_calls_today": 140, "creators_throttled_today": 3,
                 "creators_share_calls": 560, "creators_calls_all_consumers_today": 3140,
                 "creators_calls_vs_share": 0.25}


# ---------------------------------------------------------------------------
# AgentBase wrapper
# ---------------------------------------------------------------------------

class Lock:
    def __init__(self, free=True):
        self.free, self.released = free, 0

    def try_acquire(self):
        return self.free

    def release(self):
        self.released += 1


def make_agent(storage, **kw):
    kw.setdefault("lock_factory", lambda agent_id: Lock())
    return A.AmazonPriceRefreshAgent(storage=storage, run_ts="20261117T200000Z",
                                     agent_id=AGENT_ID, **kw)


def test_agent_run_wires_settings_lock_and_state(storage, picks, monkeypatch):
    fake_signals(monkeypatch)
    lock = Lock()
    agent = make_agent(storage, settings=make_settings(storage),
                       client_factory=lambda *a: FakeCreators(),
                       connect=lambda *a, **k: make_conn(), lock_factory=lambda agent_id: lock)
    agent.state = {"carried": 1}
    res = agent.run()
    assert res.status == "success" and res.metrics["refreshed"] == 2
    assert lock.released == 1
    assert res.next_state["carried"] == 1 and res.next_state["last_purge_at"]
    assert res.summary_md.startswith("## Price refresh: example-site")
    assert agent.signals() is None                 # time is the input: never short-circuit


def test_agent_lwa_timeout_is_a_failure_that_blanks_nothing(storage, picks, monkeypatch):
    fake_signals(monkeypatch)
    conn = make_conn()
    agent = make_agent(storage, settings=make_settings(storage),
                       client_factory=lambda *a: FakeCreators(unavailable_after=0),
                       connect=lambda *a, **k: conn)
    res = agent.run()
    assert res.status == "failure" and "LWA" in res.error_text
    assert conn.sql("INSERT INTO") == [] and conn.sql("UPDATE ") == []
    assert res.metrics["blanked_on_failed_run"] == 0


def test_agent_skips_when_another_run_holds_the_lock(storage):
    def never(*a, **k):
        raise AssertionError("a locked-out run touched Creators or the DB")

    agent = make_agent(storage, settings=make_settings(storage), client_factory=never,
                       connect=never, lock_factory=lambda agent_id: Lock(free=False))
    res = agent.run()
    assert res.status == "success" and res.metrics == {"skipped_locked": 1}
    assert "nothing to do" in res.summary


def test_agent_invalid_config_fails_fast(storage, tmp_path):
    p = tmp_path / "site.yaml"
    p.write_text("price_refresh:\n  creators:\n    consumr: x\n")

    def never(*a, **k):
        raise AssertionError("an invalid config reached Creators or the DB")

    res = make_agent(storage, config_spec=str(p), client_factory=never, connect=never).run()
    assert res.status == "failure" and res.metrics["config_errors"] >= 1
    assert "consumr" in res.error_text


def test_skipped_purge_reports_the_age_of_the_last_one(storage, monkeypatch):
    fake_signals(monkeypatch, clicks={}, ai_rows=[])
    rep, _ = run(make_settings(storage), storage, FakeCreators(unavailable_after=0), make_conn(),
                 prior_state={"last_purge_at": "2026-11-17T00:00:00+00:00"})
    assert rep.metrics["purge_skipped"] == 1 and rep.metrics["hours_since_last_purge"] > 0
    assert "last_purge_at" not in rep.state          # the old stamp is kept by the caller


def test_dry_run_over_ranked_candidates_reads_only(storage, picks, monkeypatch, capsys):
    fake_signals(monkeypatch)
    conn, seen = make_conn(), {}

    def connect(s, environ=None, readonly=False):
        seen["readonly"] = readonly
        return conn

    rep = A.run_refresh(make_settings(storage), storage=storage,
                        client_factory=lambda *a: FakeCreators(), connect=connect,
                        say=lambda *a, **k: None, dry_run=True, limit_asins=3)
    assert seen["readonly"] is True and rep.status == "success"
    assert conn.sql("INSERT INTO") == [] and conn.sql("UPDATE amazon_item_cache") == []
    assert "DRY B0AAAAAAA4 no_offer" in capsys.readouterr().out
    assert rep.metrics["candidates"] == 3 and rep.metrics["purge_skipped"] == 1


def test_raw_capture_passes_the_budget_and_throttle_state_through():
    client = FakeCreators(budget=7, throttle_after=1)
    cap = A.RawCapture(client)
    assert cap.remaining_budget() == 7 and cap.track_usage is True
    items, _ = cap.get_items(["B0AAAAAAA1"], resources=PRICE_RESOURCES)
    assert "B0AAAAAAA1" in cap.raw and cap.answered_calls == 1
    cap.get_items(["B0AAAAAAA2"], resources=PRICE_RESOURCES)
    assert cap.throttled_remainder == ["B0AAAAAAA2"] and cap.answered_calls == 1


def test_cli_config_sql_and_plan(storage, picks, monkeypatch, capsys):
    yaml_path = str(AGENT_DIR / "site.example.yaml")
    assert A.main(["--config", "--site-yaml", yaml_path, "--agent-id", AGENT_ID]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["settings"]["consumer"] == CONSUMER and doc["settings"]["dsn_env"] == "DATABASE_URL_EXAMPLE_SITE"
    assert doc["demand_priority"]["enabled"] is True
    assert doc["settings"]["cache"]["purge_horizon_hours"] == HORIZON_4H
    assert A.main(["--print-sql", "--site-yaml", yaml_path, "--agent-id", AGENT_ID]) == 0
    out = capsys.readouterr().out
    assert "-- purge (horizon 19.5h)\nUPDATE amazon_item_cache AS t SET title = NULL" in out
    fake_signals(monkeypatch)
    conn = make_conn()
    monkeypatch.setattr(A, "default_connect", lambda s, readonly=False, environ=None: conn)
    assert A.main(["--plan", "--site-yaml", yaml_path, "--agent-id", AGENT_ID, "--limit", "4"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [c["asin"] for c in plan["candidates"]] == EXPECTED_ORDER[:4]
    assert plan["consumer"] == CONSUMER and plan["gate_buffer"] == []
    assert (plan["quotable_hours"], plan["purge_horizon_hours"]) == (QUOTABLE_H, HORIZON_4H)
    assert conn.sql("INSERT INTO") == [] and conn.commits == 0
    assert A.main(["--config", "--site-yaml", yaml_path + "#nope", "--agent-id", AGENT_ID]) == 2


def test_cli_run_honours_agent_id(storage, monkeypatch):
    # State, status, the lock and the registry fallback resolve under the id.
    seen = {}

    def run_once(self):
        seen["agent_id"], seen["spec"] = self.agent_id, self._config_spec

    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.setattr(A.AmazonPriceRefreshAgent, "run_once", run_once)
    monkeypatch.setattr(A.AmazonPriceRefreshAgent, "_start_heartbeat", lambda self: None)
    assert A.main(["--site-yaml", "/x/site.yaml", "--agent-id", AGENT_ID]) == 0
    assert seen == {"agent_id": AGENT_ID, "spec": "/x/site.yaml"}
    monkeypatch.setenv("AGENT_ID", "from-env-price-refresh")
    assert A.main(["--site-yaml", "/x/site.yaml"]) == 0
    assert seen["agent_id"] == "from-env-price-refresh"


def test_cli_dry_run_exit_code_follows_the_calls(storage, monkeypatch, capsys):
    yaml_path = str(AGENT_DIR / "site.example.yaml")
    args = ["--dry-run", "--site-yaml", yaml_path, "--agent-id", AGENT_ID,
            "--asins", "B0AAAAAAA1,B0AAAAAAA2"]
    monkeypatch.setattr(A, "default_client_factory", lambda *a: FakeCreators())
    assert A.main(args) == 0
    monkeypatch.setattr(A, "default_client_factory",
                        lambda *a: FakeCreators(error_batches={0}))
    assert A.main(args) == 1
    captured = capsys.readouterr()
    assert "dry run FAILED" in captured.err and "InvalidParameterValue" in captured.err


# ---------------------------------------------------------------------------
# Manifest, goals, runbook
# ---------------------------------------------------------------------------

def test_manifest_is_inert_and_complete():
    m = json.loads((AGENT_DIR / "manifest.json").read_text())
    assert m["id"] == AGENT_DIR.name == A.ENGINE_ID
    assert m["enabled"] is False and not m.get("cron_expr") and not m.get("entry_command")
    assert (AGENT_DIR / m["runbook"]).is_file()
    assert (ROOT / m["metadata"]["config_schema"]).is_file()
    from framework.core.registry import AgentManifest
    fields = set(AgentManifest.__dataclass_fields__)
    AgentManifest(**{k: v for k, v in m.items() if k in fields})


def test_goals_are_valid_and_bound_to_emitted_metrics(storage, picks, monkeypatch):
    import jsonschema
    doc = json.loads((AGENT_DIR / "goals.json").read_text())
    schema = json.loads((ROOT / "shared" / "schemas" / "agent-goals.schema.json").read_text())
    jsonschema.validate(doc, schema)
    assert 3 <= len(doc["goals"]) <= 7
    from framework.core import amazon_creators as ac
    storage.write_json(ac.BUDGET_CONFIG_KEY, {"shares": {CONSUMER: 0.07}})
    fake_signals(monkeypatch)
    rep, _ = run(make_settings(storage), storage, FakeCreators(), make_conn())
    for g in doc["goals"]:
        assert g["target_metric"] == g["metric"]["name"]
        assert g["target_metric"] in rep.metrics, g["id"]
    targets = {g["target_metric"]: g["metric"]["target"] for g in doc["goals"]}
    assert targets["fresh_priced_pct"] == 90 and targets["stale_price_rows"] == 0
    assert targets["creators_throttled_today"] == 50 and targets["blanked_on_failed_run"] == 0
    # The incident directive must stop the engine: the storage setting only
    # turns demand ranking off (the engine keeps writing and purging).
    blank = next(g for g in doc["goals"] if g["target_metric"] == "blanked_on_failed_run")
    directive = " ".join(blank["directives"])
    assert "enabled=false" in directive and "timer" in directive
    assert "demand-ranking off switch" in directive and "kill switch" not in json.dumps(doc)


def test_engine_names_no_site():
    # The runbooks too: a shipped agent names no site anywhere (CLAUDE.md).
    scanned = 0
    for p in sorted(AGENT_DIR.rglob("*")):
        if (p.is_file() and p.suffix in (".py", ".json", ".yaml", ".md", ".txt")
                and "__pycache__" not in p.parts):
            scanned += 1
            text = p.read_text().lower()
            for site in ("aisleprompt", "specpicks", "runtimepicks"):
                assert site not in text, f"{p.name} names {site}"
    assert scanned >= 10       # AGENT.md and README.md included


# ---------------------------------------------------------------------------
# Real Postgres (opt-in): the SQL against a real cache table and catalog view
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("PRICE_REFRESH_TEST_PG_DSN", "")

SCHEMA_DDL = """
CREATE TABLE products (
  id        bigserial PRIMARY KEY,
  asin      text NOT NULL UNIQUE CHECK (asin ~ '^B[0-9A-Z]{9}$'),
  is_active boolean NOT NULL DEFAULT true
);
CREATE TABLE amazon_item_cache (
  asin text PRIMARY KEY,
  title text, image_url text, image_w integer, image_h integer, detail_page_url text,
  price numeric(12,2), list_price numeric(12,2), currency text, availability text,
  availability_type text, condition text, is_buybox_winner boolean, map_hidden boolean,
  browse_node_ids text[], product_group text,
  outcome text, fetched_at timestamptz, consumer text
);
CREATE TABLE pick_asins (asin text PRIMARY KEY);
CREATE VIEW v_refresh_catalog AS
  SELECT p.asin, p.is_active, c.fetched_at, c.outcome
    FROM products p LEFT JOIN amazon_item_cache c ON c.asin = p.asin;
"""

#: asin -> (active, hours since the cached answer or None for no row)
PG_SEED = {
    "B0AAAAAAA1": (True, None), "B0AAAAAAA6": (True, None),
    "B0AAAAAAA2": (True, 30.0),
    "B0AAAAAAA3": (True, 17.0), "B0AAAAAAA4": (True, 13.0),      # picks (12h gate)
    "B0GGGGGG01": (True, 23.0), "B0GGGGGG02": (True, 22.0), "B0GGGGGG03": (True, 21.0),
    "B0AAAAAAA5": (True, 20.0), "B0GGGGGG04": (True, 19.0),
    "B0AAAAAAA7": (True, 18.0), "B0GGGGGG05": (True, 17.5),      # due, past the budget
    "B0AAAAAAA8": (True, 2.0),                                   # fresh, not due
    "B0AAAAAAA9": (False, 26.0),                                 # retired, past 24h
}


@pytest.fixture
def pg():
    if not PG_DSN:
        pytest.skip("PRICE_REFRESH_TEST_PG_DSN not set (a DSN on a throwaway Postgres)")
    import psycopg2
    schema = "prf_test_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")

    def connect(*_a, **_k):
        return psycopg2.connect(PG_DSN, options=f"-c search_path={schema}")

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(SCHEMA_DDL)
        for asin, (active, age) in PG_SEED.items():
            cur.execute("INSERT INTO products (asin, is_active) VALUES (%s, %s)", (asin, active))
            if age is not None:
                cur.execute(
                    "INSERT INTO amazon_item_cache (asin, title, image_url, image_w, image_h, "
                    "detail_page_url, price, list_price, currency, availability, "
                    "availability_type, condition, is_buybox_winner, map_hidden, "
                    "browse_node_ids, product_group, outcome, fetched_at, consumer) VALUES "
                    "(%s, %s, 'https://m.media-amazon.com/images/I/old.jpg', 500, 500, "
                    "'https://www.amazon.com/dp/old', 49.99, 59.99, 'USD', 'In Stock', "
                    "'IN_STOCK', 'New', true, false, ARRAY['1'], 'Group', 'priced', "
                    "NOW() - make_interval(secs => %s), 'seed')",
                    (asin, f"old title {asin}", age * 3600.0))
        cur.execute("INSERT INTO pick_asins VALUES ('B0AAAAAAA3'), ('B0AAAAAAA4')")
    conn.commit()
    conn.close()
    try:
        yield connect
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


def _snapshot(connect) -> dict:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM amazon_item_cache ORDER BY asin")
            cols = [d[0] for d in cur.description]
            return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
    finally:
        conn.close()


def _pg_settings(storage):
    def mutate(b):
        b["creators"]["max_calls_per_run"] = 1          # 10 ASINs: two due rows stay untried
        b["demand_priority"].pop("sources")
        b["demand_priority"]["groups"] = [
            {"name": "picks", "kind": "sql", "sql": "SELECT asin FROM pick_asins",
             "gate_hours": 12}]
        # This table opted in to the two optional columns: exercises the
        # text[] cast and their purge against a real server.
        b["cache"]["columns"] = {"browse_node_ids": "browse_node_ids",
                                 "product_group": "product_group"}
    return make_settings(storage, mutate)


def test_real_postgres_lwa_timeout_then_successful_run_and_purge(storage, pg):
    s = _pg_settings(storage)
    before = _snapshot(pg)
    rep = A.run_refresh(s, storage=storage, client_factory=lambda *a: FakeCreators(unavailable_after=0),
                        connect=pg, say=lambda *a, **k: None)
    assert rep.status == "failure"
    assert _snapshot(pg) == before                   # nothing blanked, nothing purged
    assert before["B0AAAAAAA9"]["title"] is not None  # past 24h, but the purge waits

    items = dict(ITEMS)
    for i, a in enumerate(["B0GGGGGG01", "B0GGGGGG03", "B0GGGGGG04", "B0GGGGGG05"]):
        items[a] = _priced_item(a, 10.0 + i)
    client = FakeCreators(items=items, errors={"B0GGGGGG02": "ItemNotAccessible B0GGGGGG02"})
    rep = A.run_refresh(s, storage=storage, client_factory=lambda *a: client, connect=pg,
                        say=lambda *a, **k: None)
    assert rep.status == "success", rep.summary
    assert client.calls == [["B0AAAAAAA3", "B0AAAAAAA4",           # picks, oldest first
                             "B0AAAAAAA1", "B0AAAAAAA6",           # never checked
                             "B0AAAAAAA2", "B0GGGGGG01", "B0GGGGGG02", "B0GGGGGG03",
                             "B0AAAAAAA5", "B0GGGGGG04"]]          # oldest first
    after = _snapshot(pg)
    new = after["B0AAAAAAA1"]
    assert new["outcome"] == "priced" and float(new["price"]) == 1299.0
    assert new["title"] == "Example Widget Pro 2000, 120-Piece Kit"
    assert new["browse_node_ids"] == ["1000000001", "1000000002"]
    assert (new["image_w"], new["image_h"], new["consumer"]) == (375, 500, CONSUMER)
    oos = after["B0AAAAAAA2"]                        # was priced 30h ago, now out of stock
    assert oos["outcome"] == "unavailable" and oos["price"] is None
    assert oos["title"] == "Example Widget Mini (Discontinued Color)"
    assert oos["fetched_at"] > before["B0AAAAAAA2"]["fetched_at"]
    nf = after["B0GGGGGG02"]
    assert nf["outcome"] == "not_found" and all(nf[c] is None for c in LICENSED_API_COLUMNS)
    assert after["B0AAAAAAA4"]["map_hidden"] is True and after["B0AAAAAAA5"]["currency"] == "INR"
    # Not reached (budget) and under 24h: untouched. Fresh: untouched.
    for a in ("B0AAAAAAA7", "B0GGGGGG05", "B0AAAAAAA8"):
        assert after[a] == before[a]
    # Retired and past 24h: every API column NULLed, our own columns kept.
    gone = after["B0AAAAAAA9"]
    assert all(gone[c] is None for c in LICENSED_API_COLUMNS)
    assert (gone["asin"], gone["outcome"], gone["consumer"]) == ("B0AAAAAAA9", "priced", "seed")
    assert gone["fetched_at"] == before["B0AAAAAAA9"]["fetched_at"]
    m = rep.metrics
    assert (m["written_rows"], m["purged_rows"], m["stale_api_rows"], m["stale_price_rows"]) == (10, 1, 0, 0)
    # 13 active pages; priced and fresh: the 2 new priced + 3 synthesized + the
    # untouched fresh seeds (B0AAAAAAA7, B0GGGGGG05, B0AAAAAAA8).
    assert m["indexable_pages"] == 13 and m["fresh_priced_pages"] == 8
    assert m["fresh_priced_pct"] == round(100 * 8 / 13, 1) and m["demand_fresh_pct"] == 100.0
    assert m["api_rows_past_24h"] == 0 and m["purge_horizon_hours"] == HORIZON_4H
    # The license invariant, checked independently: no Amazon value older than
    # 24h, nor past the horizon that keeps it so until the next purge.
    conn = pg()
    with conn.cursor() as cur:
        any_api = " OR ".join(f"{c} IS NOT NULL" for c in sorted(LICENSED_API_COLUMNS))
        for age in ("24 hours", "19.5 hours"):
            cur.execute(f"SELECT count(*) FROM amazon_item_cache "
                        f"WHERE fetched_at < NOW() - interval '{age}' AND ({any_api})")
            assert cur.fetchone()[0] == 0, age
    conn.close()


def test_real_postgres_poison_answer_surrogate_title_and_idle_session(storage, pg):
    """Against a real server: a title with a lone surrogate is stored without
    it; a price numeric(12,2) cannot hold fails only its own row (which keeps
    its old values: 21h old, past the horizon but still displayable, it is
    spared like any row the run could not refresh); and the session is idle,
    not idle in a transaction, whenever Creators is called."""
    import psycopg2.extensions as ext
    s = _pg_settings(storage)
    items = dict(ITEMS)
    for i, a in enumerate(["B0GGGGGG01", "B0GGGGGG03", "B0GGGGGG04", "B0GGGGGG05"]):
        items[a] = _priced_item(a, 10.0 + i)
    items["B0GGGGGG01"]["itemInfo"]["title"]["displayValue"] = json.loads(
        '"Example Widget \\ud83d One"')
    items["B0GGGGGG03"] = _priced_item("B0GGGGGG03", 1e11)          # numeric overflow
    holder: dict = {}
    states: list = []

    def connect(*a, **k):
        holder["conn"] = pg()
        return holder["conn"]

    client = FakeCreators(items=items, errors={"B0GGGGGG02": "ItemNotAccessible B0GGGGGG02"},
                          on_call=lambda n: states.append(holder["conn"].get_transaction_status()))
    before = _snapshot(pg)
    rep = A.run_refresh(s, storage=storage, client_factory=lambda *a: client, connect=connect,
                        say=lambda *a, **k: None)
    assert rep.status == "success", rep.summary
    assert states == [ext.TRANSACTION_STATUS_IDLE]
    m = rep.metrics
    # Purged: only the retired 26h row. Spared: the poisoned 21h row.
    assert (m["written_rows"], m["poison_rows"], m["purged_rows"], m["spared_rows"]) == (9, 1, 1, 1)
    assert m["blanked_on_failed_run"] == 0 and m["stale_price_rows"] == 0
    after = _snapshot(pg)
    assert after["B0GGGGGG01"]["title"] == "Example Widget  One"
    assert after["B0GGGGGG03"] == before["B0GGGGGG03"]       # never written, never blanked
    assert all(after["B0AAAAAAA9"][c] is None for c in LICENSED_API_COLUMNS)
    assert after["B0GGGGGG04"]["outcome"] == "priced" and float(after["B0GGGGGG04"]["price"]) == 12.0


#: The review's probes A and B in one long-tail catalog: 11 rows cached 24.5h
#: ago, 29 cached 20h ago, one retired product cached 20h ago.
SPARE_OLD = [f"B0OOOOOO{i:02d}" for i in range(11)]
SPARE_DUE = [f"B0TTTTTT{i:02d}" for i in range(29)]
SPARE_RETIRED = "B0RRRRRR00"


def _seed_spare_catalog(connect) -> None:
    conn = connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM amazon_item_cache")
        cur.execute("DELETE FROM pick_asins")
        cur.execute("DELETE FROM products")
        for asin, active, age in ([(a, True, 24.5) for a in SPARE_OLD]
                                  + [(a, True, 20.0) for a in SPARE_DUE]
                                  + [(SPARE_RETIRED, False, 20.0)]):
            cur.execute("INSERT INTO products (asin, is_active) VALUES (%s, %s)", (asin, active))
            cur.execute(
                "INSERT INTO amazon_item_cache (asin, title, image_url, detail_page_url, price, "
                "currency, availability, outcome, fetched_at, consumer) VALUES (%s, %s, "
                "'https://m.media-amazon.com/images/I/old.jpg', 'https://www.amazon.com/dp/old', "
                "49.99, 'USD', 'In Stock', 'priced', NOW() - make_interval(secs => %s), 'seed')",
                (asin, f"old title {asin}", age * 3600.0))
    conn.commit()
    conn.close()


def test_real_postgres_failed_batch_is_spared_and_budget_spill_is_reported(storage, pg):
    """A long-tail row 20h old in a failed batch is not NULLed by that run
    (the review's probe A); a row the budget never reached is NULLed and
    reported (probe B); a failed-batch row already past the quotable window
    is purged without counting as blanking; a retired product's row is
    purged without counting as a due row; and the next run re-asks the
    spared rows and refreshes them."""
    _seed_spare_catalog(pg)

    def mutate(b):
        b["creators"]["max_calls_per_run"] = 3              # 30 of the 40 due rows
        b["demand_priority"].pop("sources")
        b["demand_priority"]["groups"] = []

    s = make_settings(storage, mutate)
    items = {a: _priced_item(a, 30.0) for a in SPARE_OLD + SPARE_DUE}
    client = FakeCreators(items=items, error_batches={1})
    before = _snapshot(pg)
    rep = A.run_refresh(s, storage=storage, client_factory=lambda *a: client, connect=pg,
                        say=lambda *a, **k: None)
    assert rep.status == "success", rep.summary
    # Oldest first: the 24.5h rows, then the 20h rows by ASIN.
    assert client.calls[0] == SPARE_OLD[:10]
    assert client.calls[1] == SPARE_OLD[10:] + SPARE_DUE[:9]                 # rejected
    assert client.calls[2] == SPARE_DUE[9:19]
    after = _snapshot(pg)
    for a in SPARE_DUE[:9]:                                     # failed batch, 20h: spared
        assert after[a] == before[a], a
    for a in SPARE_DUE[19:] + [SPARE_OLD[10], SPARE_RETIRED]:   # spill, 24.5h failed, retired
        assert all(after[a][c] is None for c in LICENSED_API_COLUMNS), a
        assert after[a]["fetched_at"] == before[a]["fetched_at"]
    for a in SPARE_OLD[:10] + SPARE_DUE[9:19]:                  # answered: fresh
        assert after[a]["title"] == f"Example Widget {a}" and after[a]["fetched_at"] > before[a]["fetched_at"]
    m = rep.metrics
    assert (m["purged_rows"], m["spared_rows"], m["purged_unrefreshed_rows"]) == (12, 9, 1)
    # Blanked while still displayable: the 10 due rows the budget never reached
    # (the retired row is not a page; the 24.5h row was no longer displayable).
    assert (m["blanked_due_rows"], m["blanked_on_failed_run"]) == (10, 0)
    # Still due after the run: 9 spared + 10 spilled + the purged 24.5h row.
    assert m["due_backlog"] == 20 and m["due_backlog_long_tail"] == 20
    assert (m["stale_api_rows"], m["stale_price_rows"], m["api_rows_past_24h"]) == (0, 0, 0)
    assert "spared 9 row(s) this run could not refresh" in rep.summary
    assert "(10 of them due catalog rows still displayable" in rep.summary
    # The license invariant, checked independently: nothing past 24h, and past
    # the horizon only the spared rows (inside the quotable window).
    conn = pg()
    with conn.cursor() as cur:
        any_api = " OR ".join(f"{c} IS NOT NULL" for c in sorted(DEFAULT_API_COLUMNS))
        cur.execute(f"SELECT asin FROM amazon_item_cache WHERE fetched_at < NOW() - "
                    f"interval '19.5 hours' AND ({any_api}) ORDER BY asin")
        assert [r[0] for r in cur.fetchall()] == SPARE_DUE[:9]
        cur.execute(f"SELECT count(*) FROM amazon_item_cache WHERE fetched_at < NOW() - "
                    f"interval '{QUOTABLE_H} hours' AND ({any_api})")
        assert cur.fetchone()[0] == 0
    conn.close()

    # The next run re-asks every row still due, the spared ones included.
    client2 = FakeCreators(items=items)
    rep2 = A.run_refresh(s, storage=storage, client_factory=lambda *a: client2, connect=pg,
                         say=lambda *a, **k: None)
    assert rep2.status == "success", rep2.summary
    asked = [a for call in client2.calls for a in call]
    assert sorted(asked) == sorted(SPARE_DUE[:9] + SPARE_DUE[19:] + [SPARE_OLD[10]])
    final = _snapshot(pg)
    for a in SPARE_DUE[:9]:
        assert final[a]["title"] == f"Example Widget {a}" and final[a]["outcome"] == "priced"
    assert rep2.metrics["due_backlog"] == 0 and rep2.metrics["spared_rows"] == 0
