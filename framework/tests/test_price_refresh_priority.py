"""framework.core.price_refresh_priority — demand-ranked price refresh.

No DB: a fake connection records every statement and answers the few query
shapes the module issues. Signal readers are exercised through fakes of the
framework readers they call (human_clicks / ai_traffic)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from framework.core import price_refresh_priority as prp

_ROOT = Path(__file__).resolve().parents[2]


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
        for needle, rows in self.conn.answers:
            if needle in sql:
                if isinstance(rows, Exception):
                    raise rows
                self.rows = rows(sql, params) if callable(rows) else list(rows)
                return
        self.rows = []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Conn:
    def __init__(self, answers=(), autocommit=False):
        self.answers = list(answers)
        self.executed = []
        self.autocommit = autocommit

    def cursor(self):
        return _Cur(self)

    def sql(self, needle):
        return [s for s, _ in self.executed if needle in s]


def _cfg(site_cfg=None, file_cfg=None, agent_id="site-a-verifier", site="site-a"):
    return prp.resolve_config(agent_id=agent_id, site=site, site_cfg=site_cfg,
                              file_cfg=file_cfg or {})


SITE = {
    "enabled": True,
    "catalog": {"table": "products", "asin_col": "asin",
                "fresh_cols": ["price_updated_at"], "where": "c.is_active"},
    "sources": {
        "human_clicks": {"table": "clicks", "spec": {"time_col": "clicked_at"},
                         "match": {"target": "amazon"}, "group_col": "asin"},
        "ai_traffic": {"prefixes": ["/product/"]},
    },
}


# -- config -------------------------------------------------------------------

def test_defaults_are_off_and_generic():
    cfg = _cfg()
    assert cfg["enabled"] is False            # behaviour-preserving until opted in
    names = [g["name"] for g in cfg["groups"]]
    assert names == ["clicked-48h", "clicked-14d", "ai-answer-time", "clicked-30d"]
    gates = {g["name"]: g["gate_hours"] for g in cfg["groups"]}
    assert gates == {"clicked-48h": 5.0, "clicked-14d": 12.0,
                     "ai-answer-time": 20.0, "clicked-30d": 12.0}
    assert cfg["long_tail"] == {"enabled": True, "gate_hours": 24}
    # answer-time fetchers only: the OAI-SearchBot index crawler is not demand
    srcs = cfg["sources"]["ai_traffic"]["config"]["live_fetch_sources"]
    assert "oai-searchbot" not in srcs and "claude-user" in srcs


def test_precedence_defaults_site_sitecfg_agent():
    file_cfg = {
        "defaults": {"display_hours": 30, "long_tail": {"gate_hours": 36}},
        "by_site": {"site-a": {"enabled": True, "statement_timeout_ms": 5,
                               "long_tail": {"gate_hours": 48}}},
        "by_agent_id": {"site-a-verifier": {"enabled": False,
                                            "group_overrides": {"clicked-14d": {"gate_hours": 8}}}},
    }
    cfg = _cfg({"statement_timeout_ms": 7, "long_tail": {"enabled": False}}, file_cfg)
    assert cfg["display_hours"] == 30                    # defaults
    assert cfg["long_tail"] == {"enabled": False, "gate_hours": 48}   # by_site + site_cfg
    assert cfg["statement_timeout_ms"] == 7               # site_cfg beats by_site
    assert cfg["enabled"] is False                        # by_agent_id kill switch wins
    assert {g["name"]: g["gate_hours"] for g in cfg["groups"]}["clicked-14d"] == 8.0
    # another agent on the same site is not switched off
    other = _cfg({}, file_cfg, agent_id="site-a-hydration")
    assert other["enabled"] is True


def test_extra_groups_append_overrides_merge_and_bad_groups_drop():
    file_cfg = {"by_site": {"site-a": {"extra_groups": [
        {"name": "shelf", "kind": "storage_list", "key": "agents/x/state/s.json",
         "gate_hours": 18}]}}}
    cfg = _cfg({"extra_groups": [
        {"name": "featured", "kind": "sql", "sql": "SELECT asin FROM p", "gate_hours": 24},
        {"name": "clicked-48h", "kind": "sql", "sql": "x", "gate_hours": 1},  # duplicate
        {"name": "long-tail", "kind": "sql", "sql": "x", "gate_hours": 1},    # reserved
        {"name": "weird", "kind": "nope", "gate_hours": 1},
        {"name": "gateless", "kind": "sql", "sql": "x", "gate_hours": "soon"},
        {"name": "off", "kind": "sql", "sql": "x", "gate_hours": 1, "enabled": False},
    ], "group_overrides": {"ai-answer-time": {"enabled": False}}}, file_cfg)
    names = [g["name"] for g in cfg["groups"]]
    assert names == ["clicked-48h", "clicked-14d", "clicked-30d", "shelf", "featured"]
    probs = " ".join(cfg["config_problems"])
    for bad in ("duplicate group 'clicked-48h'", "long-tail", "unknown kind 'nope'",
                "'gateless': gate_hours"):
        assert bad in probs
    # resolving again does not append the extras twice
    again = prp.resolve_config(site_cfg=cfg, file_cfg={})
    assert [g["name"] for g in again["groups"]] == names


def test_storage_copy_replaces_repo_default(storage):
    assert prp.load_file_config(storage) == json.loads(
        (_ROOT / "config" / "price-refresh-priority-config.json").read_text())
    storage.write_json(prp.CONFIG_KEY, {"defaults": {"enabled": True}})
    assert prp.load_file_config(storage) == {"defaults": {"enabled": True}}
    assert prp.resolve_config(storage=storage)["enabled"] is True


def test_repo_default_config_is_generic():
    raw = (_ROOT / "config" / "price-refresh-priority-config.json").read_text()
    doc = json.loads(raw)
    assert doc["by_site"] == {} and doc["by_agent_id"] == {}
    src = (_ROOT / "framework" / "core" / "price_refresh_priority.py").read_text()
    for literal in ("specpicks", "aisleprompt"):
        assert literal not in raw.lower() and literal not in src.lower()


# -- signals + tiers -----------------------------------------------------------

def _fake_readers(monkeypatch, clicks_by_window=None, ai_rows=None, fail_ai=False):
    from framework.core import ai_traffic, human_clicks
    clicks_by_window = clicks_by_window or {}
    calls = {"clicks": [], "ai": []}

    def counts_by(conn, spec, *, table, group_col, window_days=30, match=None):
        calls["clicks"].append((table, group_col, window_days, dict(match or {}), spec))
        return dict(clicks_by_window.get(window_days, {}))

    def landed(conn, *, cfg=None, prefixes=(), limit=200):
        calls["ai"].append((cfg, list(prefixes), limit))
        if fail_ai:
            raise RuntimeError("statement timeout")
        return list(ai_rows or [])

    monkeypatch.setattr(human_clicks, "human_counts_by", counts_by)
    monkeypatch.setattr(ai_traffic, "landed_paths", landed)
    return calls


def test_tier_is_first_group_gate_is_smallest(monkeypatch):
    calls = _fake_readers(monkeypatch, clicks_by_window={
        2: {"B0AAAAAAA1": 2},
        14: {"B0AAAAAAA1": 3, "B0AAAAAAA2": 1, "": 4},      # "" = NULL asin, dropped
        30: {"B0AAAAAAA1": 3, "B0AAAAAAA2": 1, "B0AAAAAAA3": 1},
    }, ai_rows=[{"path": "/product/B0AAAAAAA4", "score": 6},
                {"path": "/product/B0AAAAAAA3/", "score": 2},   # trailing slash
                {"path": "/product/EBAY_123", "score": 9},       # not an ASIN
                {"path": "/reviews/x", "score": 50}])
    conn = _Conn()
    tiers = prp.collect_tiers(conn, _cfg(SITE), human_clicks_config={})
    m = tiers.members
    assert (m["B0AAAAAAA1"]["tier"], m["B0AAAAAAA1"]["gate_hours"]) == ("clicked-48h", 5.0)
    assert (m["B0AAAAAAA2"]["tier"], m["B0AAAAAAA2"]["gate_hours"]) == ("clicked-14d", 12.0)
    # AI-used AND clicked this month: AI tier, but the click group's 12h gate
    assert (m["B0AAAAAAA3"]["tier"], m["B0AAAAAAA3"]["gate_hours"]) == ("ai-answer-time", 12.0)
    assert m["B0AAAAAAA3"]["groups"] == ["ai-answer-time", "clicked-30d"]
    assert (m["B0AAAAAAA4"]["tier"], m["B0AAAAAAA4"]["gate_hours"]) == ("ai-answer-time", 20.0)
    assert "EBAY_123" not in m
    assert tiers.counts == {"clicked-48h": 1, "clicked-14d": 1,
                            "ai-answer-time": 2, "clicked-30d": 0}
    assert tiers.asins() == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA4", "B0AAAAAAA3"]
    assert tiers.asins(max_gate_hours=12) == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"]
    # the click source and per-group window reach the framework reader
    assert [c[2] for c in calls["clicks"]] == [2, 14, 30]
    assert calls["clicks"][0][:2] == ("clicks", "asin")
    assert calls["clicks"][0][3] == {"target": "amazon"}
    # the AI reader gets answer-time sources and the site prefix
    at_cfg, prefixes, _ = calls["ai"][0]
    assert prefixes == ["/product/"] and "oai-searchbot" not in at_cfg["live_fetch_sources"]
    # every read ran inside a savepoint that was rolled back afterwards
    assert len(conn.sql("SAVEPOINT price_refresh_priority")) >= 4
    assert len(conn.sql("ROLLBACK TO SAVEPOINT price_refresh_priority")) == 4


def test_failing_signal_empties_its_group_only(monkeypatch):
    _fake_readers(monkeypatch, clicks_by_window={2: {"B0AAAAAAA1": 1}}, fail_ai=True)
    conn = _Conn()
    tiers = prp.collect_tiers(conn, _cfg(SITE), human_clicks_config={})
    assert "statement timeout" in tiers.errors["ai-answer-time"]
    assert tiers.sizes["ai-answer-time"] == 0
    assert tiers.members["B0AAAAAAA1"]["tier"] == "clicked-48h"
    # the failed read was rolled back to its savepoint, so the caller's
    # transaction is usable
    assert len(conn.sql("ROLLBACK TO SAVEPOINT")) == 4


def test_unconfigured_source_is_reported_not_raised(monkeypatch):
    _fake_readers(monkeypatch)
    tiers = prp.collect_tiers(_Conn(), _cfg({"enabled": True}), human_clicks_config={})
    assert tiers.members == {}
    assert "human_clicks.table" in tiers.errors["clicked-48h"]
    assert "ai_traffic.prefixes" in tiers.errors["ai-answer-time"]


def test_max_gate_reads_only_hot_groups(monkeypatch):
    calls = _fake_readers(monkeypatch, clicks_by_window={2: {"B0AAAAAAA1": 1}})
    tiers = prp.collect_tiers(_Conn(), _cfg(SITE), max_gate_hours=5,
                              human_clicks_config={})
    assert [g["name"] for g in tiers.groups] == ["clicked-48h"]
    assert len(calls["clicks"]) == 1 and calls["ai"] == []


def test_cap_keeps_best_first(monkeypatch):
    _fake_readers(monkeypatch, clicks_by_window={
        30: {"B0AAAAAAA1": 5, "B0AAAAAAA2": 3, "B0AAAAAAA3": 1}})
    cfg = _cfg({**SITE, "groups": [{"name": "clicked-30d", "kind": "human_clicks",
                                    "window_days": 30, "gate_hours": 12, "cap": 2}]})
    tiers = prp.collect_tiers(_Conn(), cfg, human_clicks_config={})
    assert tiers.asins() == ["B0AAAAAAA1", "B0AAAAAAA2"]


def test_listing_keys_map_to_asins_through_the_catalog(monkeypatch):
    _fake_readers(monkeypatch, clicks_by_window={30: {"17": 4, "99": 2, "5": 1}})
    site = {"enabled": True,
            "catalog": {"table": "listings", "asin_col": "source_id",
                        "fresh_cols": ["fetched_at"], "where": "c.source = 'amazon'",
                        "asin_regex": "^B0[A-Z0-9]{8}$"},
            "sources": {"human_clicks": {"table": "kitchen_clicks", "spec": {},
                                         "group_col": "listing_id", "catalog_key": "id"}},
            "groups": [{"name": "clicked-30d", "kind": "human_clicks",
                        "window_days": 30, "gate_hours": 8}],
            "long_tail": {"enabled": False}}
    conn = _Conn(answers=[("::text = ANY(%(prp_keys)s)",
                           [("5", "B0CCCCCCC3"), {"k": "17", "asin": "B0AAAAAAA1"},
                            ("17", "B0AAAAAAA9")])])
    tiers = prp.collect_tiers(conn, _cfg(site), human_clicks_config={})
    assert tiers.asins() == ["B0AAAAAAA1", "B0AAAAAAA9", "B0CCCCCCC3"]
    sql, params = [(s, p) for s, p in conn.executed if "prp_keys" in s][0]
    assert "c.id::text = ANY(%(prp_keys)s)" in sql and "(c.source = 'amazon')" in sql
    assert params["prp_keys"] == ["17", "99", "5"]


def test_sql_and_storage_list_groups(storage):
    storage.write_json("agents/site-a-shelf/state/surfaced.json",
                       {"asins": ["B0SSSSSSS1", {"asin": "B0SSSSSSS2"}, "B0SSSSSSS1"]})
    site = {"enabled": True, "catalog": SITE["catalog"],
            "groups": [{"name": "featured", "kind": "sql", "gate_hours": 24,
                        "sql": "SELECT asin FROM products WHERE is_featured"},
                       {"name": "shelf", "kind": "storage_list", "gate_hours": 18,
                        "key": "agents/site-a-shelf/state/surfaced.json"}]}
    conn = _Conn(answers=[("WHERE is_featured", [("B0FFFFFFF1",), ("B0SSSSSSS2",)])])
    tiers = prp.collect_tiers(conn, _cfg(site), storage=storage)
    assert tiers.asins() == ["B0FFFFFFF1", "B0SSSSSSS2", "B0SSSSSSS1"]
    assert tiers.members["B0SSSSSSS2"]["gate_hours"] == 18.0
    assert tiers.members["B0SSSSSSS2"]["tier"] == "featured"


def test_autocommit_connection_skips_savepoints(monkeypatch):
    _fake_readers(monkeypatch, clicks_by_window={2: {"B0AAAAAAA1": 1}})
    conn = _Conn(autocommit=True)
    prp.collect_tiers(conn, _cfg(SITE), max_gate_hours=5, human_clicks_config={})
    assert conn.sql("SAVEPOINT") == []


# -- ranking ------------------------------------------------------------------

def _tiers(members, groups=("clicked-48h", "ai-answer-time")):
    t = prp.Tiers(groups=[{"name": g, "gate_hours": 5} for g in groups])
    for i, (a, rank, gate, score) in enumerate(members):
        t.members[a] = {"tier": groups[rank], "rank": rank, "gate_hours": gate,
                        "score": score, "groups": [groups[rank]], "order": i}
    return t


def _no_stray_percent(sql, params):
    stripped = sql
    for k in params:
        stripped = stripped.replace(f"%({k})s", "")
    return "%" not in stripped


def test_rank_sql_shape_and_params():
    cfg = _cfg(SITE)
    t = _tiers([("B0AAAAAAA1", 0, 5.0, 2.0), ("B0AAAAAAA2", 1, 20.0, 6.0)])
    sql, params = prp.build_rank_sql(cfg, t, limit=500)
    assert _no_stray_percent(sql, params)
    assert params["prp_t_asins"] == ["B0AAAAAAA1", "B0AAAAAAA2"]
    assert params["prp_t_gates"] == [5.0, 20.0] and params["prp_t_ranks"] == [0, 1]
    assert params["prp_tail_rank"] == 2 and params["prp_tail_gate"] == 24.0
    assert params["prp_min_gate_s"] == 5.0 * 3600 and params["prp_limit"] == 500
    assert "FROM products c" in sql and "(c.is_active)" in sql
    assert "MIN(c.price_updated_at)" in sql
    assert "ORDER BY 2, c.never DESC, c.fresh ASC NULLS FIRST, 4 DESC, c.asin" in sql
    assert "= ANY(%(prp_t_asins)s::text[])" not in sql      # long tail on: whole catalog


def test_rank_sql_tail_off_multi_fresh_cols_and_where_params():
    site = {**SITE, "catalog": {**SITE["catalog"],
                                "fresh_cols": ["price_updated_at", "last_fetched_at"],
                                "where": "c.is_active AND c.site_id = %(site)s",
                                "params": {"site": "x"}},
            "long_tail": {"enabled": False}}
    sql, params = prp.build_rank_sql(_cfg(site), _tiers([("B0AAAAAAA1", 0, 5.0, 1.0)]))
    assert "GREATEST(c.price_updated_at, c.last_fetched_at)" in sql
    assert "AND c.asin = ANY(%(prp_t_asins)s::text[])" in sql
    assert params["site"] == "x" and params["prp_tail_on"] is False
    assert params["prp_limit"] is None                       # LIMIT NULL = all
    assert _no_stray_percent(sql, params)


def test_rank_sql_rejects_unsafe_identifiers_and_reserved_params():
    bad = {**SITE, "catalog": {**SITE["catalog"], "table": "products; DROP TABLE x"}}
    with pytest.raises(ValueError):
        prp.build_rank_sql(_cfg(bad), _tiers([]))
    bad = {**SITE, "catalog": {**SITE["catalog"], "fresh_cols": ["a b"]}}
    with pytest.raises(ValueError):
        prp.build_rank_sql(_cfg(bad), _tiers([]))
    bad = {**SITE, "catalog": {**SITE["catalog"], "params": {"prp_limit": 1}}}
    with pytest.raises(ValueError):
        prp.build_rank_sql(_cfg(bad), _tiers([]))


def test_rank_refresh_candidates_parses_rows():
    rows = [("B0AAAAAAA1", 0, 5.0, 2.0, 7.25),
            {"asin": "B0AAAAAAA2", "rnk": 1, "gate_h": 20.0, "score": 6.0, "age_h": None},
            ("B0TTTTTTT1", 2, 24.0, 0, 99.0)]
    conn = _Conn(answers=[("WITH t AS MATERIALIZED", rows)])
    t = _tiers([("B0AAAAAAA1", 0, 5.0, 2.0), ("B0AAAAAAA2", 1, 20.0, 6.0)])
    out = prp.rank_refresh_candidates(conn, _cfg(SITE), limit=10, tiers=t)
    assert [(c.asin, c.tier, c.gate_hours) for c in out] == [
        ("B0AAAAAAA1", "clicked-48h", 5.0), ("B0AAAAAAA2", "ai-answer-time", 20.0),
        ("B0TTTTTTT1", "long-tail", 24.0)]
    assert out[1].age_hours is None and out[0].age_hours == 7.25
    asin, tier, gate, *_ = out[2]                  # the (asin, tier, gate_hours) contract
    assert (asin, tier, gate) == ("B0TTTTTTT1", "long-tail", 24.0)
    assert prp.tier_counts(out) == {"clicked-48h": 1, "ai-answer-time": 1, "long-tail": 1}
    assert len(conn.sql("ROLLBACK TO SAVEPOINT")) == 1


def test_rank_needs_a_catalog():
    with pytest.raises(ValueError):
        prp.rank_refresh_candidates(_Conn(), _cfg({"enabled": True}), tiers=_tiers([]))


def test_explain_reports_groups_age_due_and_position():
    t = _tiers([("B0AAAAAAA1", 0, 5.0, 2.0), ("B0AAAAAAA2", 1, 20.0, 6.0)])
    t.members["B0AAAAAAA2"]["groups"] = ["ai-answer-time", "clicked-30d"]
    rank_rows = [("B0AAAAAAA1", 0, 5.0, 2.0, 9.0), ("B0AAAAAAA2", 1, 20.0, 6.0, 30.0)]
    conn = _Conn(answers=[("SELECT count(*) AS n", [(1, "ts", False, 30.0)]),
                          ("WITH t AS MATERIALIZED", rank_rows)])
    out = prp.explain(conn, _cfg(SITE), "B0AAAAAAA2", tiers=t)
    assert out["tier"] == "ai-answer-time" and out["groups"] == ["ai-answer-time", "clicked-30d"]
    assert out["age_hours"] == 30.0 and out["due"] is True
    assert out["position"] == 2 and out["queue_total"] == 2
    # a long-tail product: no groups, the tail's gate
    conn = _Conn(answers=[("SELECT count(*) AS n", [(1, "ts", False, 3.0)])])
    out = prp.explain(conn, _cfg(SITE), "B0ZZZZZZZ9", tiers=t)
    assert out["tier"] == "long-tail" and out["gate_hours"] == 24.0 and out["due"] is False
    # not in the catalog at all
    conn = _Conn(answers=[("SELECT count(*) AS n", [(0, None, None, None)])])
    assert "not in the catalog" in prp.explain(conn, _cfg(SITE), "B0NOPE0000", tiers=t)["note"]


def test_tier_freshness_rows():
    t = _tiers([("B0AAAAAAA1", 0, 5.0, 2.0)])
    rows = [(0, 1, 1, 1, 0, 0, 1), (2, 0, 10, 4, 5, 1, 6)]
    conn = _Conn(answers=[("FULL JOIN t", rows)])
    out = prp.tier_freshness(conn, _cfg(SITE), t)
    assert out[0]["tier"] == "clicked-48h" and out[0]["fresh_pct"] == 100.0
    assert out[1]["tier"] == "long-tail" and out[1]["members"] == 10
    assert out[1]["stale"] == 5 and out[1]["never"] == 1 and out[1]["fresh_pct"] == 40.0
    sql = conn.sql("FULL JOIN t")[0]
    params = [p for s, p in conn.executed if "FULL JOIN t" in s][0]
    assert _no_stray_percent(sql, params)


# -- CLI ----------------------------------------------------------------------

def test_cli_loads_site_cfg_from_yaml_json_and_module(tmp_path, capsys):
    from framework.cli import price_refresh_priority as cli
    y = tmp_path / "site.yaml"
    y.write_text("price_refresh:\n  demand_priority:\n    enabled: true\n"
                 "    catalog: {table: products}\n")
    assert cli.load_site_cfg(f"{y}#price_refresh.demand_priority")["catalog"] == {"table": "products"}
    j = tmp_path / "c.json"
    j.write_text(json.dumps({"enabled": True}))
    assert cli.load_site_cfg(str(j)) == {"enabled": True}
    m = tmp_path / "agent_mod.py"
    m.write_text("CFG = {'enabled': True}\ndef make():\n    return {'long_tail': {'enabled': False}}\n")
    assert cli.load_site_cfg(f"{m}:CFG") == {"enabled": True}
    assert cli.load_site_cfg(f"{m}:make") == {"long_tail": {"enabled": False}}
    # `config` resolves without a DB
    assert cli.main(["config", "--agent-id", "a-1", "--site-cfg", str(j)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["enabled"] is True and printed["agent_id"] == "a-1"
