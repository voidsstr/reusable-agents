"""product-hydration-agent: the Creators price pass writes by the framework
price outcome rules (amazon_price_refresh.classify_item).

Found in review, 2026-11-11: when Amazon answered with no offer, the pass
kept the OLD price and still stamped price_updated_at = NOW(); an
ItemNotAccessible or an ASIN missing from the response did the same through
_stamp_miss. The 24h display gate reads price_updated_at, so a price Amazon
did not return was shown as freshly checked: 90 specpicks rows read
"Currently unavailable." next to a price this agent had just re-stamped.
Pointing the demand-ranked order at clicked products would have made this
agent their first re-pricer, so the same defect would have sat on the pages
that convert. These tests pin the outcome rules the price verifier and
catalog-freshness already follow."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from framework.core import price_refresh_priority as prp

_AGENT = Path(__file__).resolve().parents[2] / "agents" / "product-hydration-agent" / "agent.py"


@pytest.fixture(scope="module")
def hyd():
    spec = importlib.util.spec_from_file_location("hydration_agent_outcomes_under_test", _AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self.rows = list(self.conn.rows) if not sql.lstrip().startswith("UPDATE") else []

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def updates(self):
        """[(sql, params)] of every UPDATE, in order."""
        return [(s, p) for s, p in self.executed if s.lstrip().startswith("UPDATE")]

    def update_for(self, product_id):
        hits = [(s, p) for s, p in self.updates() if p and p[-1] == product_id]
        assert len(hits) == 1, (product_id, hits)
        return hits[0]


def _listing(amount=None, currency="USD", avail="IN_STOCK", message="In Stock",
             condition="New", buybox=True, **extra):
    price = {"money": {"amount": amount, "currency": currency}} if amount is not None else {}
    l = {"price": price, "availability": {"type": avail, "message": message},
         "condition": {"value": condition}, "isBuyBoxWinner": buybox}
    l.update(extra)
    return l


def _item(*listings, title="Some product title"):
    return {"asin": "x", "itemInfo": {"title": {"displayValue": title}},
            "offersV2": {"listings": list(listings)},
            "customerReviews": {"starRating": {"value": 4.5}, "count": 120}}


# -- the persist primitive -----------------------------------------------------

def test_priced_creators_write_uses_amazons_current_list_price(hyd):
    conn = _Conn()
    hyd._persist_provider_refresh(conn, 7, {"price": 19.99, "original_price": None,
                                            "list_price_current": True, "currency": "USD",
                                            "availability": "In Stock"}, provider="creators")
    sql, params = conn.updates()[0]
    assert "price = %s" in sql and "price_updated_at = NOW()" in sql
    assert "last_fetched_at = NOW()" in sql
    # a retained old list price is how "Save $X" off a stale basis appears
    assert "original_price = %s" in sql and "CASE WHEN original_price" not in sql
    assert params[:2] == [19.99, None]
    assert conn.commits == 1


def test_unpriced_write_clears_the_price_and_redates_it(hyd):
    conn = _Conn()
    hyd._persist_provider_refresh(conn, 7, {"price": None, "unpriced": True,
                                            "availability": None, "rating": 4.1},
                                  provider="creators")
    sql, params = conn.updates()[0]
    assert "price = NULL" in sql and "original_price = NULL" in sql
    assert "COALESCE(%s, 'Currently unavailable')" in sql
    assert "price_updated_at = NOW()" in sql and "last_fetched_at = NOW()" in sql
    assert params[0] is None and params[-1] == 7
    assert "rating = %s" in sql          # metadata from the answer is still kept


def test_no_price_and_no_verdict_never_redates_the_price(hyd):
    """A provider payload without a price and without an unpriced verdict
    (PA-API / BrightData) says nothing about the price: it must keep ageing
    toward the display gate."""
    conn = _Conn()
    hyd._persist_provider_refresh(conn, 7, {"price": None, "availability": "In Stock",
                                            "review_count": 5}, provider="paapi")
    sql, _ = conn.updates()[0]
    assert "price_updated_at" not in sql
    assert "last_fetched_at = NOW()" in sql
    assert "price =" not in sql.replace("original_price", "") and "availability" not in sql


def test_legacy_list_price_rule_is_unchanged_without_the_flag(hyd):
    conn = _Conn()
    hyd._persist_provider_refresh(conn, 7, {"price": 25.0}, provider="paapi")
    sql, params = conn.updates()[0]
    assert "CASE WHEN original_price < %s THEN NULL ELSE original_price END" in sql
    assert params[:2] == [25.0, 25.0]


def test_a_miss_records_the_attempt_not_a_price_check(hyd):
    conn = _Conn()
    hyd._stamp_miss(conn, 7, "creators: not in response")
    sql, params = conn.updates()[0]
    assert "price_updated_at" not in sql and "last_fetched_at = NOW()" in sql
    assert "fetch_attempts = COALESCE(fetch_attempts, 0) + 1" in sql
    assert params == ["creators: not in response", 7]


def test_not_found_clears_the_price(hyd):
    conn = _Conn()
    hyd._mark_not_found(conn, 7, "creators: The ItemId B0X is not accessible")
    sql, params = conn.updates()[0]
    assert "price = NULL" in sql and "original_price = NULL" in sql
    assert "price_updated_at = NOW()" in sql and "'Currently unavailable'" in sql
    assert "is_active" not in sql        # never deactivated
    assert params[-1] == 7


# -- the Creators pass end to end -------------------------------------------------

class _Client:
    def __init__(self, items, errors=None):
        self.items, self.errors = items, errors or {}

    def get_items(self, asins):
        return ({a: self.items[a] for a in asins if a in self.items},
                {a: self.errors[a] for a in asins if a in self.errors})


ASINS = {
    "B0PRICED01": 1, "B0NOOFFER1": 2, "B0OOSTOCK1": 3, "B0USEDONL1": 4,
    "B0CADPRCE1": 5, "B0NOTFOUND": 6, "B0MISSING1": 7,
}


def _run(hyd, monkeypatch, *, items, errors=None, demand_cfg=None, rows=None, select=None):
    if rows is None:
        rows = [{"id": i, "asin": a} for a, i in ASINS.items()]
    if select is None:
        def select(conn, **kw):
            return rows
    monkeypatch.setattr(hyd, "_select_stale_amazon_prices", select)
    conn = _Conn()
    decisions = []
    summary = hyd._refresh_via_creators(
        conn=conn, creators_client=_Client(items, errors), raw_cfg={"max_refresh_per_run": 50},
        site_id_filter=None, status_cb=lambda *a, **k: None,
        decide_cb=lambda kind, msg: decisions.append((kind, msg)), demand_cfg=demand_cfg)
    return conn, summary, decisions


def test_every_outcome_is_written_by_the_framework_rules(hyd, monkeypatch):
    items = {
        "B0PRICED01": _item(_listing(19.99)),
        # answered, but no price on the New buy box (no usable offer)
        "B0NOOFFER1": _item(_listing(None)),
        "B0OOSTOCK1": _item(_listing(None, avail="OUT_OF_STOCK", message="Temporarily out of stock.")),
        # a used-only listing: its price is not the product's price
        "B0USEDONL1": _item(_listing(12.0, condition="Used")),
        "B0CADPRCE1": _item(_listing(30.0, currency="CAD")),
    }
    errors = {"B0NOTFOUND": "The ItemId B0NOTFOUND is not accessible through the Creators API."}
    conn, s, _ = _run(hyd, monkeypatch, items=items, errors=errors)

    sql, params = conn.update_for(1)
    assert "price = %s" in sql and params[0] == 19.99 and "price_updated_at = NOW()" in sql
    for pid in (2, 3, 4, 5):
        sql, params = conn.update_for(pid)
        assert "price = NULL" in sql and "price_updated_at = NOW()" in sql, pid
        assert 12.0 not in params and 30.0 not in params, pid
    assert conn.update_for(3)[1][0] == "Temporarily out of stock."
    assert conn.update_for(2)[1][0] is None          # -> 'Currently unavailable'
    sql, _ = conn.update_for(6)
    assert "price = NULL" in sql and "fetch_error = %s" in sql
    sql, _ = conn.update_for(7)
    assert "price_updated_at" not in sql and "last_fetched_at = NOW()" in sql
    assert (s["refreshed"], s["unpriced"], s["inaccessible"], s["failed"]) == (5, 4, 1, 0)
    assert "demand_ranked" not in s and "refreshed_by_tier" not in s


def test_a_priced_row_never_keeps_an_unavailable_text(hyd, monkeypatch):
    items = {a: _item(_listing(49.0, message="")) for a in ASINS}
    conn, _, _ = _run(hyd, monkeypatch, items=items,
                      rows=[{"id": 1, "asin": "B0PRICED01"}])
    sql, params = conn.update_for(1)
    assert "availability = LEFT(%s, 100)" in sql and "In Stock" in params


def test_map_hidden_offer_is_not_priced_from_a_stale_value(hyd, monkeypatch):
    """A New in-stock offer whose price Amazon withholds (MAP) has no price
    to show; the old one must not be re-dated."""
    items = {"B0PRICED01": _item(_listing(None, violatesMAP=True))}
    conn, s, _ = _run(hyd, monkeypatch, items=items, rows=[{"id": 1, "asin": "B0PRICED01"}])
    sql, _ = conn.update_for(1)
    assert "price = NULL" in sql and s["unpriced"] == 1


def test_a_failed_batch_writes_nothing(hyd, monkeypatch):
    class Boom:
        def get_items(self, asins):
            raise RuntimeError("HTTP 503")
    monkeypatch.setattr(hyd, "_select_stale_amazon_prices",
                        lambda conn, **kw: [{"id": 1, "asin": "B0PRICED01"}])
    conn = _Conn()
    s = hyd._refresh_via_creators(conn=conn, creators_client=Boom(), raw_cfg={},
                                  site_id_filter=None, status_cb=lambda *a, **k: None,
                                  decide_cb=lambda *a: None)
    assert conn.updates() == [] and s["failed"] == 1


# -- demand accounting on the fallback path -----------------------------------------

def test_fallback_selection_reports_no_demand_refreshes(hyd, monkeypatch):
    """Demand ranking enabled but its selection failed: the old tiered
    selector ran (integer tiers 0/1/2). Nothing may be booked as a demand
    refresh (review finding: 3 of 4 were, faking goal progress)."""
    def boom(*a, **kw):
        raise RuntimeError("catalog query timed out")
    monkeypatch.setattr(prp, "collect_tiers", boom)
    tiered = [{"id": 1, "asin": "B0PRICED01", "tier": 0},
              {"id": 2, "asin": "B0NOOFFER1", "tier": 1},
              {"id": 3, "asin": "B0OOSTOCK1", "tier": 2},
              {"id": 4, "asin": "B0USEDONL1", "tier": 2}]
    real_select = hyd._select_stale_amazon_prices

    def select(conn, **kw):
        return real_select(_Conn(rows=tiered), **kw)
    items = {r["asin"]: _item(_listing(10.0 + r["id"])) for r in tiered}
    _, s, decisions = _run(hyd, monkeypatch, items=items, demand_cfg={"enabled": True},
                           select=select)
    assert s["refreshed"] == 4
    assert s["demand_ranked"] is False and "catalog query timed out" in s["demand_error"]
    assert "refreshed_by_tier" not in s
    m = hyd._price_outcome_metrics(s)
    assert m["prices_demand_ranked"] == 0 and m["prices_refreshed_demand"] == 0
    assert any(k == "error" and "review_count order" in msg for k, msg in decisions)


def test_demand_metrics_count_named_tiers_on_a_ranked_run(hyd):
    m = hyd._price_outcome_metrics({
        "demand_ranked": True, "unpriced": 2, "inaccessible": 1,
        "refreshed_by_tier": {"clicked-48h": 3, "ai-answer-time": 2, "long-tail": 9, 0: 4}})
    assert m == {"prices_demand_ranked": 1, "prices_refreshed_demand": 5,
                 "prices_unpriced": 2, "prices_inaccessible": 1}
    assert hyd._price_outcome_metrics({})["prices_refreshed_demand"] == 0


# -- misses do not return to the head of either queue --------------------------------

def test_legacy_selector_backs_off_after_an_attempt(hyd):
    for featured in (False, True):
        conn = _Conn()
        hyd._select_stale_amazon_prices(conn, site_id_filter="pc", freshness_hours=24,
                                        limit=10, prioritize_featured_and_linked=featured)
        sql, params = conn.executed[-1]
        assert "GREATEST(p.price_updated_at, p.last_fetched_at) IS NULL" in sql
        assert "GREATEST(p.price_updated_at, p.last_fetched_at)\n" in sql
        assert params == ["pc", 24, 10]


def test_demand_catalog_counts_an_attempt_as_fresh(hyd, storage):
    cfg = hyd._demand_ranked_config(
        {"price_freshness_hours": 24,
         "demand_priority": {"enabled": True,
                             "sources": {"human_clicks": {"table": "clicks", "spec": {}}}}},
        agent_id="x-hyd", site="x", site_id_filter=None, storage=storage)
    assert cfg["catalog"]["fresh_cols"] == ["price_updated_at", "last_fetched_at"]
    sql, _ = prp.build_rank_sql(cfg, prp.Tiers(), limit=5)
    assert "last_fetched_at" in sql


# -- the site's Amazon-offer flags (price_refresh.outcome_flags) -------------------

FLAGS = {"outcome_flags": {
    "owned": ["unavailable-listing", "no-buybox", "amazon-not-found"],
    "by_status": {"unavailable": ["unavailable-listing"],
                  "not_found": ["unavailable-listing", "amazon-not-found"],
                  "no_offer": ["no-buybox"], "non_usd": ["unavailable-listing", "non-usd-price"]}}}


def test_outcome_flags_config_is_opt_in_and_normalised(hyd):
    assert hyd._outcome_flags_cfg({}) is None
    assert hyd._outcome_flags_cfg({"outcome_flags": {"owned": []}}) is None
    cfg = hyd._outcome_flags_cfg(FLAGS)
    # a flag named per status is always cleared too, so it is never duplicated
    assert cfg["owned"] == ["unavailable-listing", "no-buybox", "amazon-not-found",
                            "non-usd-price"]
    assert cfg["by_status"]["no_offer"] == ["no-buybox"]


def test_without_outcome_flags_no_flag_is_touched(hyd, monkeypatch):
    items = {"B0PRICED01": _item(_listing(19.99)), "B0NOOFFER1": _item(_listing(None))}
    conn, _, _ = _run(hyd, monkeypatch, items=items,
                      errors={"B0NOTFOUND": "not accessible"})
    assert all("quality_flags" not in s for s, _ in conn.updates())


def test_every_verdict_keeps_the_sites_flags_like_its_verifier(hyd, monkeypatch):
    """A product back in stock must not stay flagged unbuyable; one out of
    stock must be flagged. A miss (no verdict) leaves the flags alone."""
    items = {"B0PRICED01": _item(_listing(19.99)),
             "B0NOOFFER1": _item(_listing(None)),
             "B0OOSTOCK1": _item(_listing(None, avail="OUT_OF_STOCK", message="")),
             "B0CADPRCE1": _item(_listing(30.0, currency="CAD"))}
    monkeypatch.setattr(hyd, "_select_stale_amazon_prices", lambda conn, **kw: [
        {"id": i, "asin": a} for a, i in ASINS.items()])
    conn = _Conn()
    hyd._refresh_via_creators(
        conn=conn, creators_client=_Client(items, {"B0NOTFOUND": "not accessible"}),
        raw_cfg={"max_refresh_per_run": 50, **FLAGS}, site_id_filter=None,
        status_cb=lambda *a, **k: None, decide_cb=lambda *a: None)
    owned = hyd._outcome_flags_cfg(FLAGS)["owned"]
    clause = ("quality_flags = (COALESCE(NULLIF(quality_flags, '{}'::jsonb), '[]'::jsonb)"
              " - %s::text[]) || to_jsonb(%s::text[])")
    expect = {1: [], 2: ["no-buybox"], 3: ["unavailable-listing"],
              5: ["unavailable-listing", "non-usd-price"],
              6: ["unavailable-listing", "amazon-not-found"]}
    for pid, added in expect.items():
        sql, params = conn.update_for(pid)
        assert clause in sql, pid
        assert params[0] == owned and params[1] == added, pid
    sql, _ = conn.update_for(7)                     # missing: no verdict
    assert "quality_flags" not in sql


def test_flags_are_never_rewritten_without_a_verdict(hyd):
    conn = _Conn()
    hyd._persist_provider_refresh(conn, 7, {"price": None, "flags_clear": ["no-buybox"],
                                            "flags_add": []}, provider="creators")
    assert "quality_flags" not in conn.updates()[0][0]
