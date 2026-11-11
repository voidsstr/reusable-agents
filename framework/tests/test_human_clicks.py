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


def test_human_click_paths_renders_and_merges_paths():
    spec = hc.resolve_spec({"referer_col": "referer"}, config={})
    rows = [("human", "https://site.test/blog/a?utm_source=chatgpt.com", 3),
            ("human", "https://site.test/blog/a", 2),
            ("human", "https://site.test/k/x#top", 4),
            ("human", "", 9), ("human", "not a path", 1),
            ("ua-bot", "https://site.test/blog/z", 50)]
    out = hc.human_click_paths(_FakeConn(rows), spec, table="clicks",
                               group_col="referer")
    assert out == [{"path": "/blog/a", "clicks": 5}, {"path": "/k/x", "clicks": 4}]
    ids = [("human", "B0AAA", 2), ("human", "B0BBB", 7)]
    out = hc.human_click_paths(_FakeConn(ids), spec, table="clicks",
                               group_col="asin", path_template="/product/{value}",
                               limit=1)
    assert out == [{"path": "/product/B0BBB", "clicks": 7}]


# ── Safari floor (stale_browser.min_versions) ──────────────────────────────

import re as _re
from datetime import datetime, timedelta, timezone

# The six strings behind 348 of 681 'human' clicks on one deployment
# (30 days to 2026-11-11), one click per IP each.
SAFARI_FLEET_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]
REAL_UAS = [
    # current Safari (frozen OS token, live Version/)
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.2 Mobile/15E148 Safari/604.1",
    # devices stuck on a final point release at or above the floor
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1",
    # other engines that carry a Version/ token
    "Mozilla/5.0 (Linux; Android 10; K; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/156.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/156.0.0.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 EdgiOS/156.0 Mobile/15E148 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Snapchat/12.60.0.37 (like Safari/8617.2.4.10.8, panda)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
]


def _py_floor_verdict(spec, ua):
    """Python mirror of the SQL min_versions predicate (ARE ~ Python re here)."""
    sb = spec["stale_browser"]
    for engine, floor in hc.version_floors(spec).items():
        excl = (sb.get("engine_exclude_ua_regex") or {}).get(engine)
        if excl and _re.search(excl, ua, _re.I):
            continue
        m = _re.search(sb["engine_ua_regex"][engine], ua)
        if m and [int(x) for x in m.group(1).split(".")] < floor:
            return True
    return False


def test_version_floors_parse_and_disable():
    spec = hc.resolve_spec({}, config={})
    assert hc.version_floors(spec) == {"safari": [17, 3]}
    spec["stale_browser"]["min_versions"] = {"safari": "x.1", "other": "", "e": "120"}
    assert hc.version_floors(spec) == {"e": [120]}
    spec["stale_browser"]["enabled"] = False
    assert hc.version_floors(spec) == {}


def test_safari_floor_is_storage_tunable(storage):
    storage.write_json(hc.CONFIG_KEY, {
        "defaults": {"stale_browser": {"min_versions": {"safari": "18.0"}}},
        "by_profile": {"p": {"stale_browser": {"min_versions": {"safari": ""}}}}})
    assert hc.version_floors(hc.resolve_spec({}, storage=storage)) == {"safari": [18, 0]}
    # an empty floor turns the engine off for one consumer only
    assert hc.version_floors(hc.resolve_spec({}, profile="p", storage=storage)) == {}
    # the anchors are untouched by a floor override
    assert hc.resolve_spec({}, storage=storage)["stale_browser"]["anchors"] == \
        hc.DEFAULT_SPEC["stale_browser"]["anchors"]


def test_safari_floor_sql_shape():
    spec = hc.resolve_spec({"referer_col": "referer", "bot_flag_col": "is_bot"}, config={})
    sql, params = hc.build_breakdown_query(spec, table="clicks", window_days=30,
                                           match={"source": "amazon"},
                                           today=date(2026, 11, 11))
    assert sql.count("'stale-browser'") == 1          # one verdict, more engines
    assert "string_to_array(substring(user_agent from %s), '.')::int[] < %s::int[]" in sql
    assert [17, 3] in params
    assert spec["stale_browser"]["engine_ua_regex"]["safari"] in params
    assert spec["stale_browser"]["engine_exclude_ua_regex"]["safari"] in params
    assert sql.count("%s") == len(params)
    assert sql.replace("%s", "").count("%") == 0
    # the Chromium / Firefox anchor predicates are unchanged
    assert sql.count("COALESCE(substring(user_agent from %s)::int, 2147483647) < %s") == 2
    th = hc.stale_thresholds(spec, today=date(2026, 11, 11))
    assert th["chromium"] in params and th["firefox"] in params


def test_disabling_the_floor_restores_the_anchor_only_rule():
    spec = hc.resolve_spec({"stale_browser": {"min_versions": {"safari": ""}}}, config={})
    sql, params = hc.build_breakdown_query(spec, table="clicks", window_days=30,
                                           today=date(2026, 11, 11))
    assert "string_to_array" not in sql
    assert spec["stale_browser"]["engine_exclude_ua_regex"]["safari"] not in params
    assert sql.count("'stale-browser'") == 1


@pytest.mark.parametrize("ua", SAFARI_FLEET_UAS)
def test_safari_fleet_strings_are_stale(ua):
    assert _py_floor_verdict(hc.resolve_spec({}, config={}), ua) is True


@pytest.mark.parametrize("ua", REAL_UAS)
def test_real_and_other_engine_uas_pass(ua):
    assert _py_floor_verdict(hc.resolve_spec({}, config={}), ua) is False


def test_raising_floor_catches_monterey_only_when_asked():
    ua = REAL_UAS[2]   # Safari 17.6, macOS Monterey's final release
    assert _py_floor_verdict(hc.resolve_spec({}, config={}), ua) is False
    raised = hc.resolve_spec({"stale_browser": {"min_versions": {"safari": "18.0"}}}, config={})
    assert _py_floor_verdict(raised, ua) is True


def test_engine_regexes_have_exactly_one_capture_group():
    for engine, pat in hc.DEFAULT_SPEC["stale_browser"]["engine_ua_regex"].items():
        assert _re.compile(pat).groups == 1, engine


# ── self-referer ───────────────────────────────────────────────────────────

def test_self_referer_is_off_by_default_and_ordered_when_set():
    base = hc.resolve_spec({"referer_col": "referer"}, config={})
    assert base["self_referer_regex"] is None
    sql, _ = hc.build_breakdown_query(base, table="clicks", window_days=30)
    assert "'self-referer'" not in sql
    spec = hc.resolve_spec({"referer_col": "referer",
                            "self_referer_regex": "^(https?://[^/]+)?/k/"}, config={})
    sql, params = hc.build_breakdown_query(spec, table="clicks", window_days=30,
                                           match={"source": "amazon"})
    assert "WHEN COALESCE(referer, '') ~* %s THEN 'self-referer'" in sql
    assert "^(https?://[^/]+)?/k/" in params
    assert sql.index("'no-referer'") < sql.index("'self-referer'") < sql.index("'scanner-referer'")
    assert sql.count("%s") == len(params)
    assert sql.replace("%s", "").count("%") == 0
    rx = _re.compile(spec["self_referer_regex"], _re.I)
    assert rx.search("https://site.test/k/oxo-spatula?source=amazon")
    assert rx.search("/k/oxo-spatula")
    assert not rx.search("https://site.test/kitchen/oxo-spatula")
    assert not rx.search("https://site.test/blog/k/")


def test_self_referer_needs_a_referer_column():
    spec = hc.resolve_spec({"referer_col": None, "self_referer_regex": "/k/"}, config={})
    sql, _ = hc.build_breakdown_query(spec, table="clicks", window_days=30)
    assert "'self-referer'" not in sql


def test_self_referer_by_profile_override(storage):
    storage.write_json(hc.CONFIG_KEY, {"by_profile": {
        "site-a-tracker": {"self_referer_regex": "^(https?://[^/]+)?/k/"}}})
    assert hc.resolve_spec({}, profile="site-a-tracker", storage=storage)[
        "self_referer_regex"] == "^(https?://[^/]+)?/k/"
    assert hc.resolve_spec({}, profile="site-b-tracker", storage=storage)[
        "self_referer_regex"] is None


# ── attribution: paths, templates, trends ──────────────────────────────────

TEMPLATES = [{"name": "guide", "pattern": "^/blog/best-"},
             {"name": "blog", "pattern": "^/blog/"},
             {"name": "product", "pattern": "^/product/"},
             {"name": "home", "pattern": "^/?$"}]


def test_page_path_and_classify_path():
    assert hc.page_path("https://site.test/blog/a?utm_source=x#t") == "/blog/a"
    assert hc.page_path("/product/B0AAA?ref=1") == "/product/B0AAA"
    assert hc.page_path(None) == ""
    assert hc.classify_path("https://site.test/blog/best-air-fryers-2026", TEMPLATES) == "guide"
    assert hc.classify_path("/blog/how-to-sear", TEMPLATES) == "blog"
    assert hc.classify_path("/PRODUCT/B0AAA", TEMPLATES) == "product"      # case-insensitive
    assert hc.classify_path("https://site.test", TEMPLATES) == "home"
    assert hc.classify_path("https://site.test/", TEMPLATES) == "home"
    assert hc.classify_path("/kitchen/x", TEMPLATES) == "other"
    assert hc.classify_path("/kitchen/x", None) == "all"


def test_trend_query_shape_and_params():
    spec = hc.resolve_spec({"time_col": "clicked_at", "referer_col": "source_page",
                            "country_col": "country", "bot_flag_col": "is_bot"},
                           config={})
    sql, params = hc.build_trend_query(
        spec, table="outbound_clicks", windows=(30, 7, 14),
        match={"target": "amazon", "via": "beacon"}, group_col="placement",
        templates=TEMPLATES, today=date(2026, 11, 11))
    reason, rparams = hc._reason_case(spec, today=date(2026, 11, 11))
    assert reason in sql                                   # the one verdict definition
    assert params[:len(rparams)] == rparams
    # template patterns follow the verdict params, in template order
    n = len(rparams)
    assert params[n:n + len(TEMPLATES)] == [t["pattern"] for t in TEMPLATES]
    assert params[n + len(TEMPLATES)] == 30                # widest window bounds the scan
    # per-window velocity + human counts, smallest window first
    assert params[n + len(TEMPLATES) + 1:n + len(TEMPLATES) + 7] == [7, 14, 30, 7, 14, 30]
    assert params[-2:] == ["amazon", "beacon"]
    assert sql.count("OVER (PARTITION BY _ip, (_t AT TIME ZONE 'UTC')::date)") == 3
    assert "_v0 <= 20" in sql and "_v2 <= 20" in sql
    assert "CAST(placement AS text) AS _grp" in sql
    assert sql.count("%s") == len(params)
    assert sql.replace("%s", "").count("%") == 0
    assert sql.rstrip().endswith("GROUP BY 1, 2")


def test_trend_query_without_velocity_templates_or_group():
    spec = hc.resolve_spec({"max_clicks_per_ip_day": 0, "referer_col": "ref"}, config={})
    sql, params = hc.build_trend_query(spec, table="t", windows=(7,))
    assert "OVER (PARTITION BY" not in sql
    assert "-1 AS _tmpl" in sql and "NULL::text AS _grp" in sql
    assert sql.count("%s") == len(params)
    spec2 = hc.resolve_spec({"referer_col": None}, config={})
    with pytest.raises(ValueError):
        hc.build_trend_query(spec2, table="t", templates=TEMPLATES)
    with pytest.raises(ValueError):
        hc.build_trend_query(spec, table="t", windows=())
    with pytest.raises(ValueError):
        hc.build_trend_query(spec, table="t", group_col="a; drop table t")


def test_trend_by_maps_templates_and_computes_prev_window():
    spec = hc.resolve_spec({"referer_col": "ref"}, config={})
    rows = [(0, "pdp-hero", 5, 9, 20),      # guide x pdp-hero
            (2, "sticky", 3, 3, 4),         # product x sticky
            (-1, None, 1, 2, 2),            # other
            (1, "x", 0, 0, 0)]              # bots only in the window: dropped
    out = hc.trend_by(_FakeConn(rows), spec, table="t", templates=TEMPLATES,
                      group_col="placement")
    assert out[0] == {"template": "guide", "group": "pdp-hero", "d7": 5, "d14": 9,
                      "d30": 20, "prev7": 4, "wow_pct": 25}
    assert out[1]["template"] == "product" and out[1]["prev7"] == 0
    assert out[1]["wow_pct"] is None
    assert out[2]["template"] == "other" and out[2]["group"] == ""
    assert len(out) == 3
    dict_rows = [{"_tmpl": -1, "_grp": None, "h0": 2, "h1": 3}]
    out = hc.trend_by(_FakeConn(dict_rows), spec, table="t", windows=(7, 30))
    assert out == [{"template": "all", "group": "", "d7": 2, "d30": 3}]


def test_human_rows_query_keeps_only_human_rows():
    spec = hc.resolve_spec({"referer_col": "ref", "bot_flag_col": "is_bot"}, config={})
    sql, params = hc.build_rows_query(spec, table="t", window_days=21,
                                      match={"source": "amazon"}, group_col="placement")
    assert "WHERE _reason IS NULL AND _per_ip_day <= 20 AND source = %s" in sql
    assert params[-2:] == [21, "amazon"]
    assert sql.count("%s") == len(params)
    assert sql.replace("%s", "").count("%") == 0


def _ts(days_ago, now):
    return now - timedelta(days=days_ago)


def test_before_after_counts_pages_groups_and_site():
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    pivot = now - timedelta(days=10)
    rows = [
        {"t": _ts(13, now), "path": "/blog/best-a", "group": "hero"},     # before, lever
        {"t": _ts(12, now), "path": "/blog/best-a/", "group": "inline"},  # before, wrong group
        {"t": _ts(18, now), "path": "/blog/best-a", "group": "hero"},     # outside (> 7d before)
        {"t": _ts(9, now), "path": "/blog/best-a", "group": "hero"},      # after, lever
        {"t": _ts(8, now), "path": "/reviews/b", "group": "hero"},        # after, prefix page
        {"t": _ts(5, now), "path": "/other", "group": "hero"},            # after, site only
        {"t": _ts(1, now), "path": "/blog/best-a", "group": "hero"},      # past pivot+7d
    ]
    res = hc.before_after(rows, pivot=pivot, days=7,
                          pages=["https://site.test/blog/best-a?x=1", "/reviews/*"],
                          groups=["HERO"], now=now)
    assert (res["before"], res["after"]) == (1, 2)
    assert (res["site_before"], res["site_after"]) == (2, 3)
    assert res["after_days"] == 7.0 and res["scoped"] is True
    # the after window ends at now for a recent pivot
    recent = hc.before_after(rows, pivot=now - timedelta(days=2), days=7, now=now)
    assert recent["after_days"] == 2.0 and recent["scoped"] is False
    assert hc.before_after(rows, pivot="not a date", now=now)["after_days"] == 0.0


class _RowsConn(_FakeConn):
    def cursor(self):
        cur = _FakeCursor(self.rows)
        self.last = cur
        return cur


def test_lever_outcomes_uses_one_query_for_all_levers():
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    rows = [(_ts(12, now), "/blog/best-a", "hero"), (_ts(8, now), "/blog/best-a", "hero"),
            (_ts(3, now), "/blog/best-a", None)]
    conn = _RowsConn(rows)
    spec = hc.resolve_spec({"referer_col": "ref"}, config={})
    levers = [{"id": "L1", "live_since": (now - timedelta(days=10)).isoformat(),
               "target_urls": ["/blog/best-a"], "placements": ["hero"]},
              {"id": "L2", "live_since": None, "target_urls": ["/x"]},
              {"id": "L3", "live_since": (now - timedelta(days=4)).isoformat(),
               "target_urls": ["/blog/best-a"], "placements": ["hero"]}]
    hc.lever_outcomes(conn, spec, table="t", levers=levers, now=now)
    assert levers[1]["outcome"] is None
    o1 = levers[0]["outcome"]
    assert (o1["before"], o1["after"]) == (1, 1)
    assert o1.get("placements_ignored") is True        # no group_col on this table
    o3 = levers[2]["outcome"]
    assert (o3["before"], o3["after"], o3["after_days"]) == (1, 1, 4.0)
    # the scan covers the oldest live lever plus the before window
    assert "make_interval(days => %s)" in conn.last.sql


def test_conversion_attribution_and_engagement():
    spec = hc.resolve_spec({"referer_col": "ref"}, config={})

    class _TwoQueryConn:
        def __init__(self):
            self.calls = 0

        def cursor(self):
            self.calls += 1
            if self.calls == 1:      # template x placement
                return _FakeCursor([(0, "hero", 6, 10, 30), (0, "inline", 2, 2, 4),
                                    (2, "hero", 4, 5, 9)])
            return _FakeCursor([(-1, "/blog/best-a", 6, 9, 25),   # per page
                                (-1, "/product/B0X", 4, 5, 9),
                                (-1, "/blog/best-b", 2, 3, 9)])

    attr = hc.conversion_attribution(_TwoQueryConn(), spec, table="t",
                                     templates=TEMPLATES, group_col="placement")
    assert attr["totals"] == {"d7": 12, "prev7": 5, "d30": 43, "wow_pct": 140}
    assert attr["by_template"][0] == {"template": "guide", "d7": 8, "prev7": 4,
                                      "d30": 34, "wow_pct": 100}
    assert attr["by_group"][0]["group"] == "hero" and attr["by_group"][0]["d7"] == 10
    assert attr["rows"][0]["share_d7_pct"] == 50.0
    # biggest d7 - prev7 gain first; ties broken by the larger d7
    assert [(p["page"], p["delta"]) for p in attr["rising_pages"]] == [
        ("/blog/best-a", 3), ("/product/B0X", 3), ("/blog/best-b", 1)]
    hc.add_engagement(attr, {"/blog/best-a": 300, "/blog/best-c?x=1": 100,
                             "/product/B0X": 50, "/kitchen/k": 80, "(not set)": 5},
                      min_engaged=25)
    rows = {r["template"]: r for r in attr["by_template"]}
    assert rows["guide"]["engaged_sessions_30d"] == 400
    assert rows["guide"]["clicks_per_100_engaged"] == 8.5
    assert rows["other"]["d30"] == 0 and rows["other"]["engaged_sessions_30d"] == 85
    assert attr["zero_click_high_attention"] == [{"template": "other",
                                                  "engaged_sessions_30d": 85}]
    assert attr["site_clicks_per_100_engaged"] == round(43 * 100 / 535, 1)
    block = hc.attribution_prompt_block(attr)
    assert "TOTAL d7=12 prev7=5 d30=43 (+140% wow)" in block
    assert "guide x hero" in block and "Fastest-rising pages" in block
    assert "ZERO human clicks" in block
    assert hc.attribution_prompt_block({"error": "boom"}).startswith("(attribution unavailable")
    assert hc.add_engagement({"error": "x"}, {"/a": 1}) == {"error": "x"}


def test_focus_rows_top_plus_fastest_growing():
    attr = {"group_col": "placement", "rows": [
        {"template": "product", "group": "hero", "d7": 488, "prev7": 288, "d30": 834},
        {"template": "product", "group": "sticky", "d7": 73, "prev7": 31, "d30": 106},
        {"template": "review", "group": "fold", "d7": 63, "prev7": 30, "d30": 106},
        {"template": "product", "group": "alt", "d7": 36, "prev7": 10, "d30": 53},
        {"template": "product", "group": "inline", "d7": 19, "prev7": 13, "d30": 36},
        {"template": "other", "group": "in-article", "d7": 14, "prev7": 0, "d30": 18},
        {"template": "product", "group": "status", "d7": 11, "prev7": 11, "d30": 22},
        {"template": "review", "group": "sticky", "d7": 4, "prev7": 0, "d30": 4},   # < min_d7
        {"template": "x", "group": "y", "d7": 0, "prev7": 9, "d30": 9}]}
    got = [(r["template"], r["group"]) for r in hc.focus_rows(attr)]
    assert got == [("product", "hero"), ("product", "sticky"), ("review", "fold"),
                   ("product", "alt"), ("other", "in-article"), ("product", "inline")]
    assert hc.focus_rows({}) == []
    block = hc.attribution_prompt_block({**attr, "totals": {"d7": 1, "prev7": 1, "d30": 1}})
    assert "FOCUS ROWS (top-converting + fastest-growing, last 7 days): product x hero d7=488" in block


def test_ga4_engaged_sessions_request_and_fleet_filter():
    body = hc.ga4_engaged_sessions_request(exclude_sources=["aocr.org"], exclude_countries=["sg"])
    assert body["dimensions"] == [{"name": "landingPage"}, {"name": "sessionDefaultChannelGroup"}]
    assert body["metrics"] == [{"name": "engagedSessions"}, {"name": "averageSessionDuration"}]
    filters = [e["filter"] for e in body["dimensionFilter"]["notExpression"]["orGroup"]["expressions"]]
    assert {"fieldName": "sessionSource",
            "stringFilter": {"matchType": "CONTAINS", "value": "aocr.org"}} in filters
    assert {"fieldName": "countryId", "stringFilter": {"value": "SG"}} in filters
    assert "dimensionFilter" not in hc.ga4_engaged_sessions_request()

    def row(page, channel, engaged, avg):
        return {"dimensionValues": [{"value": page}, {"value": channel}],
                "metricValues": [{"value": str(engaged)}, {"value": str(avg)}]}
    rep = {"rows": [row("/chat", "Direct", 9538, 5.28),           # GA4-running fleet
                    row("/blog/best-a", "Direct", 940, 71.0),
                    row("/blog/best-a", "AI Assistant", 9, 232.0),
                    row("/kitchen/x", "Direct", 2486, 1.0)]}
    assert hc.engaged_by_page(rep) == {"/blog/best-a": 949.0}
    assert hc.engaged_by_page(rep, min_avg_session_s=0)["/chat"] == 9538.0
    assert hc.engaged_by_page({"error": "x"}) == {}
