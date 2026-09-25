"""Tests for framework.core.human_clicks — the verified-human click filter.

SQL is exercised structurally (no Postgres in CI); the live numbers were
verified against the reference deployments when the module shipped.
"""
from datetime import date

import pytest

from framework.core import human_clicks as hc


def test_resolve_spec_precedence(storage):
    storage.write_json(hc.CONFIG_KEY, {
        "defaults": {"max_clicks_per_ip_day": 10, "exclude_countries": ["SG", "VN"]},
        "by_profile": {"p1": {"max_clicks_per_ip_day": 5,
                              "extra_datacenter_ip_prefixes": ["203.0.113."]}},
    })
    s = hc.resolve_spec({"time_col": "clicked_at", "max_clicks_per_ip_day": 7},
                        storage=storage)
    assert s["max_clicks_per_ip_day"] == 7            # caller beats config defaults
    assert s["exclude_countries"] == ["SG", "VN"]     # config default applied
    assert s["time_col"] == "clicked_at"
    s2 = hc.resolve_spec({"max_clicks_per_ip_day": 7}, profile="p1", storage=storage)
    assert s2["max_clicks_per_ip_day"] == 5           # by_profile beats caller
    assert "203.0.113." in s2["datacenter_ip_prefixes"]
    assert "43." in s2["datacenter_ip_prefixes"]      # extra_* appends, keeps base
    assert s2["extra_datacenter_ip_prefixes"] == []


def test_resolve_spec_does_not_mutate_defaults():
    s = hc.resolve_spec({"extra_bot_ua_terms": ["zzbot"]}, config={})
    s["bot_ua_terms"].append("mutated")
    assert "mutated" not in hc.DEFAULT_SPEC["bot_ua_terms"]
    assert "zzbot" not in hc.DEFAULT_SPEC["bot_ua_terms"]


def test_estimated_current_major_extrapolates():
    assert hc.estimated_current_major(140, "2025-09-02", today=date(2025, 9, 2)) == 140
    assert hc.estimated_current_major(140, "2025-09-02", today=date(2025, 9, 30)) == 141
    # before the anchor never goes backwards
    assert hc.estimated_current_major(140, "2025-09-02", today=date(2025, 1, 1)) == 140


def test_stale_thresholds_respects_enabled():
    spec = hc.resolve_spec({}, config={})
    th = hc.stale_thresholds(spec, today=date(2026, 9, 24))
    assert th["chromium"] == 153 - 16
    assert th["firefox"] == 156 - 16
    spec["stale_browser"]["enabled"] = False
    assert hc.stale_thresholds(spec) == {}


def test_breakdown_query_shape_and_params():
    spec = hc.resolve_spec({"time_col": "clicked_at", "referer_col": "source_page",
                            "country_col": "country", "bot_flag_col": "is_bot"},
                           config={})
    sql, params = hc.build_breakdown_query(
        spec, table="outbound_clicks", window_days=30,
        match={"target": "amazon", "kind": ["a", "b"]}, today=date(2026, 9, 24))
    # every rule present, in order
    order = ["site-flagged-bot", "no-ua", "ua-bot", "stale-browser",
             "excluded-country", "datacenter-ip", "no-referer", "scanner-referer"]
    pos = [sql.index(f"'{r}'") for r in order]
    assert pos == sorted(pos)
    assert "'velocity'" in sql and "> 20" in sql
    assert "FROM outbound_clicks" in sql
    assert "target = %s" in sql and "kind = ANY(%s)" in sql
    # placeholders line up with params; no stray % in the SQL
    assert sql.count("%s") == len(params)
    assert sql.replace("%s", "").count("%") == 0
    assert params[-3:] == [30, "amazon", ["a", "b"]]
    assert ["SG"] in params


def test_group_col_splits_the_same_verdicts():
    spec = hc.resolve_spec({"referer_col": "referer", "bot_flag_col": "is_bot"},
                           config={})
    plain, pparams = hc.build_breakdown_query(
        spec, table="clicks", window_days=30, match={"source": "amazon"},
        today=date(2026, 9, 24))
    sql, params = hc.build_breakdown_query(
        spec, table="clicks", window_days=30, match={"source": "amazon"},
        today=date(2026, 9, 24), group_col="referer")
    assert params == pparams                       # no extra placeholders
    assert "referer AS _grp" in sql
    assert sql.rstrip().endswith("GROUP BY 1, 2 ORDER BY 3 DESC")
    assert plain.rstrip().endswith("GROUP BY 1 ORDER BY 2 DESC")
    # the verdict CASE is identical — one definition of "human"
    assert plain.split("FROM clicks")[0].replace(", referer AS _grp", "") == \
        sql.split("FROM clicks")[0].replace(", referer AS _grp", "")
    with pytest.raises(ValueError):
        hc.build_breakdown_query(spec, table="clicks", window_days=30,
                                 group_col="referer; drop table x")


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql = sql

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _FakeCursor(self.rows)


def test_human_counts_by_keeps_only_human_rows():
    spec = hc.resolve_spec({"referer_col": "referer"}, config={})
    rows = [("human", "/blog/a", 5), ("site-flagged-bot", "/blog/a", 90),
            ("human", None, 1), ("human", "/k/x", 7), ("velocity", "/k/x", 3)]
    out = hc.human_counts_by(_FakeConn(rows), spec, table="clicks",
                             group_col="referer")
    assert out == {"/k/x": 7, "/blog/a": 5, "": 1}
    assert list(out) == ["/k/x", "/blog/a", ""]    # largest first
    dict_rows = [{"verdict": "human", "_grp": "/b", "count": 2},
                 {"verdict": "ua-bot", "_grp": "/b", "count": 9}]
    assert hc.human_counts_by(_FakeConn(dict_rows), spec, table="clicks",
                              group_col="referer") == {"/b": 2}
    # breakdown() tolerates dict rows too (RealDictCursor connections)
    bd = hc.breakdown(_FakeConn([{"verdict": "human", "count": 3},
                                 {"verdict": "ua-bot", "count": 4}]),
                      spec, table="clicks")
    assert bd == {"human": 3, "ua-bot": 4, "_total": 7}


def test_optional_rules_drop_out():
    spec = hc.resolve_spec({"referer_col": None, "country_col": None,
                            "bot_flag_col": None, "max_clicks_per_ip_day": 0,
                            "stale_browser": {"enabled": False}}, config={})
    sql, params = hc.build_breakdown_query(spec, table="t", window_days=7)
    for r in ("site-flagged-bot", "excluded-country", "no-referer",
              "scanner-referer", "stale-browser", "velocity"):
        assert f"'{r}'" not in sql
    assert sql.count("%s") == len(params)


@pytest.mark.parametrize("bad", ["t; drop table x", "a-b", "", None, "1col"])
def test_identifiers_are_validated(bad):
    spec = hc.resolve_spec({}, config={})
    with pytest.raises(ValueError):
        hc.build_breakdown_query(spec, table=bad, window_days=30)
    with pytest.raises(ValueError):
        hc.build_breakdown_query(spec, table="ok", window_days=30, match={bad: 1})


def test_regexes_escape_literals():
    assert hc._ip_regex(["43.", "104.28."]) == r"^(43\.|104\.28\.)"
    assert "electron/" in hc._ua_regex(["electron/"])
    assert r"screaming\ frog" in hc._ua_regex(["screaming frog"])


def test_no_site_literals_in_module():
    import inspect
    src = inspect.getsource(hc).lower()
    for site in ("aisleprompt", "specpicks"):
        assert site not in src
