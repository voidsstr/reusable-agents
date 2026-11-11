"""product-hydration-agent: demand-ranked price candidates (plan A21).

The price pass spent ~5.6k re-prices a day ordered by review_count (median
~5.5-7.6k reviews) while 83% of clicked long-tail products have under 1,000
reviews. With framework price_refresh_priority turned on for the agent, the
candidates come in demand order; without it nothing changes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from framework.core import price_refresh_priority as prp

_AGENT = Path(__file__).resolve().parents[2] / "agents" / "product-hydration-agent" / "agent.py"


@pytest.fixture(scope="module")
def hyd():
    spec = importlib.util.spec_from_file_location("hydration_agent_under_test", _AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BLOCK = {"enabled": True, "sources": {"human_clicks": {"table": "clicks", "spec": {}},
                                      "ai_traffic": {"prefixes": ["/product/"]}}}


def test_off_by_default(hyd, storage):
    assert hyd._demand_ranked_config({"price_freshness_hours": 24}, agent_id="x-hyd",
                                     site="x", site_id_filter=None, storage=storage) is None


def test_site_block_turns_it_on_with_the_agents_catalog(hyd, storage):
    cfg = hyd._demand_ranked_config(
        {"price_freshness_hours": 24, "prioritize_featured_and_linked": True,
         "demand_priority": BLOCK},
        agent_id="x-hyd", site="x", site_id_filter="pc", storage=storage)
    assert cfg and cfg["enabled"]
    cat = cfg["catalog"]
    assert cat["table"] == "products"
    # an attempt (last_fetched_at, see _stamp_miss) counts as fresh: a miss
    # goes to the back of its tier, not the head of the queue (review fix)
    assert cat["fresh_cols"] == ["price_updated_at", "last_fetched_at"]
    assert "c.site_id = %(hyd_site_id)s" in cat["where"] and cat["params"] == {"hyd_site_id": "pc"}
    assert cfg["long_tail"] == {"enabled": True, "gate_hours": 24}
    # featured / editorial keep their meaning, after the demand tiers
    assert [g["name"] for g in cfg["groups"]][-2:] == ["featured", "editorial"]
    assert [g["name"] for g in cfg["groups"]][0] == "clicked-48h"


def test_storage_enables_and_kills_per_agent(hyd, storage):
    storage.write_json(prp.CONFIG_KEY, {"by_site": {"x": {"sources": BLOCK["sources"]}},
                                        "by_agent_id": {"x-hyd": {"enabled": True}}})
    assert hyd._demand_ranked_config({"price_freshness_hours": 24}, agent_id="x-hyd",
                                     site="x", site_id_filter=None, storage=storage)
    storage.write_json(prp.CONFIG_KEY, {"by_agent_id": {"x-hyd": {"enabled": False}}})
    assert hyd._demand_ranked_config({"demand_priority": BLOCK}, agent_id="x-hyd",
                                     site="x", site_id_filter=None, storage=storage) is None


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
        self.rows = list(self.conn.rows)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.rollbacks = 0

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        self.rollbacks += 1


def _cands():
    return [prp.RefreshCandidate("B0AAAAAAA2", "clicked-48h", 5.0, 9.0, 1.0, 0),
            prp.RefreshCandidate("B0AAAAAAA1", "long-tail", 24.0, 30.0, 0.0, 4)]


def test_demand_path_orders_rows_by_rank_and_tags_tiers(hyd, monkeypatch):
    monkeypatch.setattr(prp, "collect_tiers", lambda conn, cfg, storage=None: prp.Tiers())
    seen = {}

    def rank(conn, cfg, *, limit=None, storage=None, tiers=None):
        seen["limit"] = limit
        return _cands()
    monkeypatch.setattr(prp, "rank_refresh_candidates", rank)
    conn = _Conn(rows=[{"id": 1, "asin": "B0AAAAAAA1"}, {"id": 2, "asin": "B0AAAAAAA2"},
                       {"id": 3, "asin": "B0AAAAAAA2"}])
    info = {}
    rows = hyd._select_stale_amazon_prices(conn, site_id_filter=None, freshness_hours=24,
                                           limit=500, demand_cfg={"enabled": True},
                                           demand_info=info)
    assert [(r["id"], r["tier"]) for r in rows] == [(2, "clicked-48h"), (3, "clicked-48h"),
                                                    (1, "long-tail")]
    assert info["by_tier"] == {"clicked-48h": 1, "long-tail": 1}
    assert seen["limit"] == 500
    assert "p.asin = ANY(%s)" in conn.executed[-1][0]


def test_demand_failure_falls_back_to_the_old_selector(hyd, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("catalog query timed out")
    monkeypatch.setattr(prp, "collect_tiers", boom)
    conn = _Conn(rows=[{"id": 9, "asin": "B0OLDOLD01"}])
    info = {}
    rows = hyd._select_stale_amazon_prices(conn, site_id_filter=None, freshness_hours=24,
                                           limit=50, demand_cfg={"enabled": True},
                                           demand_info=info)
    assert rows == [{"id": 9, "asin": "B0OLDOLD01"}]
    assert "catalog query timed out" in info["error"] and conn.rollbacks == 1
    assert "COALESCE(p.review_count, 0) DESC" in conn.executed[-1][0]


def test_without_demand_cfg_the_sql_is_the_old_one(hyd):
    conn = _Conn()
    hyd._select_stale_amazon_prices(conn, site_id_filter="pc", freshness_hours=24,
                                    limit=10, prioritize_featured_and_linked=True)
    sql, params = conn.executed[-1]
    assert "WITH editorial_asins AS (" in sql and "related_product_asins" in sql
    assert "ORDER BY tier ASC, COALESCE(p.review_count, 0) DESC" in sql
    assert params == ["pc", 24, 10]


class _Client:
    def __init__(self, items):
        self.items = items

    def get_items(self, asins):
        return ({a: self.items[a] for a in asins if a in self.items}, {})


def test_creators_pass_writes_every_row_of_an_asin(hyd, monkeypatch):
    rows = [{"id": 2, "asin": "B0AAAAAAA2", "tier": "clicked-48h"},
            {"id": 3, "asin": "B0AAAAAAA2", "tier": "clicked-48h"},
            {"id": 1, "asin": "B0AAAAAAA1", "tier": "long-tail"}]

    def select(conn, **kw):
        if kw.get("demand_info") is not None:
            kw["demand_info"].update({"by_tier": {"clicked-48h": 1, "long-tail": 1},
                                      "signals": {"errors": {}}})
        return rows
    persisted = []
    monkeypatch.setattr(hyd, "_select_stale_amazon_prices", select)
    monkeypatch.setattr(hyd, "_persist_provider_refresh",
                        lambda conn, pid, payload, provider: persisted.append(pid))
    item = {"asin": "x", "offersV2": {"listings": [{"price": {"money": {
        "amount": 19.99, "currency": "USD"}}, "availability": {"type": "IN_STOCK"}}]}}
    decisions = []
    summary = hyd._refresh_via_creators(
        conn=_Conn(), creators_client=_Client({"B0AAAAAAA2": item, "B0AAAAAAA1": item}),
        raw_cfg={"max_refresh_per_run": 10}, site_id_filter=None,
        status_cb=lambda *a, **k: None, decide_cb=lambda kind, msg: decisions.append(msg),
        demand_cfg={"enabled": True})
    assert sorted(persisted) == [1, 2, 3]
    assert summary["refreshed"] == 3 and summary["demand_ranked"] is True
    assert summary["refreshed_by_tier"] == {"clicked-48h": 2, "long-tail": 1}
    assert any("demand-ranked clicked-48h=1, long-tail=1" in d for d in decisions)
