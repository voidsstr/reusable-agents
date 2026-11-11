"""Tests for framework.core.article_output_gate."""
import datetime as dt

from framework.core import article_output_gate as gate

NOW = dt.datetime(2026, 9, 25, 4, 45, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 9, 25)
# A date with no NOW/IMMINENT tech occasion (mid-August sits between
# back-to-school-tech and Labor Day only for the general audience).
QUIET_DAY = dt.date(2026, 2, 20)


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "information_schema" in sql:
            self.rows = [(c,) for c in self.conn.cols]
            return
        if "unnest(" in sql:
            self.conn.occ_queries.append((sql, params))
            if params and str(params[0][0]).startswith("lane:"):
                self.rows = [(f"lane:{h}", n) for h, n in self.conn.lanes.items()]
            else:
                self.rows = [(f"holiday:{h}", n) for h, n in self.conn.occ.items()]
            return
        if "editorial_articles" in sql:
            if self.conn.fail_batch:
                raise RuntimeError("no such table")
            self.rows = [(s, NOW - dt.timedelta(days=5)) for s in self.conn.batch]
        elif "ai_traffic_log" in sql:
            if self.conn.fail_ai:
                raise RuntimeError("no such table")
            self.conn.ai_queries.append((sql, params))
            self.rows = list(self.conn.ai.items())
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, batch=(), ai=None, fail_batch=False, fail_ai=False,
                 cols=("ts", "kind", "path", "ip", "user_agent", "status_code"),
                 occ=None, lanes=None):
        self.batch = list(batch)
        self.lanes = dict(lanes or {})
        self.ai = dict(ai or {})
        self.cols = list(cols)
        self.occ = dict(occ or {})
        self.ai_queries = []
        self.occ_queries = []
        self.fail_batch = fail_batch
        self.fail_ai = fail_ai

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        pass


def _seed_gsc(storage, rows, run_ts="20260924T223000Z",
              agent="site-a-seo-opportunity-agent"):
    storage.write_json(f"agents/{agent}/run-index.json",
                       {"recent": [{"run_ts": run_ts, "status": "success"}]})
    storage.write_json(f"agents/{agent}/runs/{run_ts}/data/gsc-pages-90d.json",
                       {"rows": rows})


def _eval(storage, conn, **kw):
    return gate.evaluate(storage=storage, agent_id="site-a-article-proposal-agent",
                         site_id="site-a", conn=conn, now=NOW,
                         today=kw.pop("today", QUIET_DAY),
                         audience=kw.pop("audience", "tech"), **kw)


def test_cap_blocks_second_batch_same_day(storage):
    conn = _Conn(batch=[])
    d1 = _eval(storage, conn)
    assert d1.allowed and d1.slots == 1
    gate.record_batch(storage, "site-a-article-proposal-agent",
                      slugs=["x"], run_ts="r1", now=NOW)
    d2 = _eval(storage, conn)
    assert not d2.allowed and d2.slots == 0
    assert "daily cap" in d2.reason
    # Next calendar day (UTC) opens again
    d3 = gate.evaluate(storage=storage, agent_id="site-a-article-proposal-agent",
                       site_id="site-a", conn=conn,
                       now=NOW + dt.timedelta(days=1), today=QUIET_DAY)
    assert d3.allowed and d3.slots == 1


def test_calendar_day_not_rolling(storage):
    # queued at 00:47 local yesterday == 04:47Z; the 04:45Z run the next day
    # is 23h58m later and must still be allowed.
    gate.record_batch(storage, "site-a-article-proposal-agent", slugs=["x"],
                      now=NOW - dt.timedelta(hours=23, minutes=58))
    d = _eval(storage, _Conn())
    assert d.allowed


def test_traffic_gate_closed_when_no_hits(storage):
    _seed_gsc(storage, [{"keys": ["https://site-a.test/reviews/other"],
                         "impressions": 50}])
    d = _eval(storage, _Conn(batch=["a1", "a2"], ai={"/reviews/zzz": 3}))
    assert not d.allowed
    assert d.batch_size == 2 and d.batch_hits == []
    assert d.mode == "cap+traffic"


def test_traffic_gate_open_on_gsc_impression(storage):
    _seed_gsc(storage, [{"keys": ["https://site-a.test/reviews/a2/"],
                         "impressions": 1}])
    d = _eval(storage, _Conn(batch=["a1", "a2"]))
    assert d.allowed and [h["slug"] for h in d.batch_hits] == ["a2"]


def test_traffic_gate_open_on_ai_visit(storage):
    d = _eval(storage, _Conn(batch=["a1"], ai={"/reviews/a1?x=1": 2}))
    assert d.allowed and d.batch_hits[0]["ai_visits"] == 2
    assert d.sources["gsc"].startswith("unavailable")


def test_path_prefix_filter(storage):
    cfg = {"traffic_gate": {"article_path_prefixes": ["/blog/"]}}
    d = _eval(storage, _Conn(batch=["a1"], ai={"/recipes/a1": 5}), site_cfg=cfg)
    assert not d.allowed
    d = _eval(storage, _Conn(batch=["a1"], ai={"/blog/a1": 1}), site_cfg=cfg)
    assert d.allowed


def test_falls_back_to_cap_only_without_data(storage):
    d = _eval(storage, _Conn(batch=["a1"], fail_ai=True))
    assert d.allowed and d.mode == "cap-only"
    d = _eval(storage, _Conn(fail_batch=True))
    assert d.allowed and d.mode == "cap-only"
    d = gate.evaluate(storage=storage, agent_id="z", site_id="site-a",
                      dsn=None, now=NOW, today=QUIET_DAY)
    assert d.allowed and d.mode == "cap-only"


def test_stale_gsc_is_unavailable(storage):
    _seed_gsc(storage, [{"keys": ["https://site-a.test/r/a1"], "impressions": 9}],
              run_ts="20260901T000000Z")
    d = _eval(storage, _Conn(batch=["a1"], fail_ai=True))
    assert d.mode == "cap-only" and "older than" in d.sources["gsc"]


def test_empty_prior_batch_is_a_probe(storage):
    d = _eval(storage, _Conn(batch=[]))
    assert d.allowed and "probe" in d.reason


def test_seasonal_override_only_tagged(storage):
    # 2026-09-25: prime-big-deal-days (tech) is IMMINENT
    d = _eval(storage, _Conn(batch=["a1"]), today=TODAY)
    assert d.allowed and d.mode == "seasonal-override"
    assert d.require_holiday_ids == ["prime-big-deal-days"]
    props = [{"slug": "evergreen", "tags": ["gpu"]},
             {"slug": "deal", "tags": ["holiday:prime-big-deal-days"]}]
    kept, dropped = gate.select_for_queue(props, d)
    assert [p["slug"] for p in kept] == ["deal"]
    kept, _ = gate.select_for_queue(props[:1], d)
    assert kept == []


def test_priority_holiday_moves_first(storage):
    d = _eval(storage, _Conn(batch=[]), today=TODAY)
    assert d.allowed and d.priority_holiday_ids == ["prime-big-deal-days"]
    props = [{"slug": "a"}, {"slug": "b", "holiday": "prime-big-deal-days"}]
    kept, dropped = gate.select_for_queue(props, d)
    assert [p["slug"] for p in kept] == ["b"]
    assert [p["slug"] for p in dropped] == ["a"]
    assert "Only the FIRST 1" in d.prompt_block()


def test_operator_override_disables(storage):
    storage.write_json(gate.CONFIG_KEY, {
        "defaults": {}, "by_agent_id": {"site-a-article-proposal-agent":
                                        {"enabled": False}}})
    d = _eval(storage, _Conn(batch=["a1"]))
    assert d.allowed and d.mode == "disabled" and d.slots >= gate.UNLIMITED


def test_site_cfg_cap(storage):
    d = _eval(storage, _Conn(batch=[]), site_cfg={"max_proposals_per_day": 2})
    assert d.slots == 2
    kept, dropped = gate.select_for_queue([{"slug": s} for s in "abc"], d)
    assert len(kept) == 2 and len(dropped) == 1


def test_slug_from_path():
    assert gate.slug_from_path("https://x.com/reviews/Foo-Bar/?q=1") == "foo-bar"
    assert gate.slug_from_path("/blog/a", ["/reviews/"]) == ""
    assert gate.slug_from_path("") == ""


# ── AI-visit evidence hygiene + per-occasion seasonal cap (2026-10 audit) ──

def test_ai_visits_sql_excludes_self_and_private_traffic(monkeypatch):
    from framework.core import ai_traffic
    monkeypatch.setenv("AI_TRAFFIC_EXCLUDE_IPS", "2600:1702:7930:33f0:,108.80.179.176")
    tg = gate.resolve_config(None, "a", {"traffic_gate": {
        "ai_visit_exclude_user_agent_regex": "micro-cache-refresh"}})["traffic_gate"]
    conn = _Conn(cols=("ts", "kind", "path", "ip", "user_agent", "status_code",
                       "ip_verified"))
    gate.read_ai_visits(conn, table="ai_traffic_log", kinds=["referral"], days=15, tg=tg)
    sql, params = conn.ai_queries[-1]
    assert "COUNT(DISTINCT l.ip)" in sql
    assert "l.ip_verified IS NOT FALSE" in sql
    assert "(l.status_code IS NULL OR l.status_code = 200)" in sql
    assert params["exclude_ips"] == ["108.80.179.176"]
    assert params["exclude_ip_prefixes"] == ["2600:1702:7930:33f0:%"]
    assert params["exclude_ua_re"] == "micro-cache-refresh"
    import re
    rx = re.compile(params["private_ip_re"], re.I)
    for ip in ("127.0.0.1", "172.17.0.1", "10.1.2.3", "192.168.0.5", "::1",
               "::ffff:127.0.0.1", "fd12:3456::1", "fe80::1"):
        assert rx.search(ip), ip
    for ip in ("111.92.133.168", "2605:a601:9b32::1", "172.32.0.1", "8.8.8.8"):
        assert not rx.search(ip), ip
    assert ai_traffic.config()["exclude_ips"]  # the shared list is the source


def test_ai_visits_sql_without_optional_columns():
    tg = gate.DEFAULTS["traffic_gate"]
    conn = _Conn(cols=("ts", "kind", "path"))
    gate.read_ai_visits(conn, table="ai_traffic_log", kinds=["referral"], days=15, tg=tg)
    sql, params = conn.ai_queries[-1]
    assert "COUNT(*) AS n" in sql and "exclude_ips" not in params
    assert "private_ip_re" not in params and "user_agent" not in sql


def test_ai_visits_can_count_rows(storage):
    tg = gate.resolve_config(None, "a", {"traffic_gate": {"ai_visit_distinct_ips": False}})
    conn = _Conn()
    gate.read_ai_visits(conn, table="ai_traffic_log", kinds=["referral"], days=15,
                        tg=tg["traffic_gate"])
    assert "COUNT(*) AS n" in conn.ai_queries[-1][0]


def test_seasonal_override_per_occasion_cap(storage):
    # 2 tagged pieces already exist for the occasion: the override is spent.
    d = _eval(storage, _Conn(batch=["a1"], occ={"prime-big-deal-days": 2}), today=TODAY)
    assert not d.allowed and d.slots == 0
    assert "max_per_occasion" in d.reason and "prime-big-deal-days 2/2" in d.reason
    assert d.occasion_counts == {"prime-big-deal-days": 2}

    # 1 of 2 used: still open, for that occasion only.
    d = _eval(storage, _Conn(batch=["a1"], occ={"prime-big-deal-days": 1}), today=TODAY)
    assert d.allowed and d.mode == "seasonal-override" and d.slots == 1


def test_seasonal_override_cap_counts_ledger_and_is_configurable(storage):
    gate.record_batch(storage, "site-a-article-proposal-agent", slugs=["d1", "d2"],
                      holiday_ids=gate.holiday_ids_for([
                          {"tags": ["holiday:prime-big-deal-days"]},
                          {"holiday": "prime-big-deal-days"}]),
                      now=NOW - dt.timedelta(days=2))
    d = _eval(storage, _Conn(batch=["a1"]), today=TODAY)
    assert not d.allowed and d.occasion_counts == {"prime-big-deal-days": 2}
    # Operator raises the cap without a commit.
    d = _eval(storage, _Conn(batch=["a1"]), today=TODAY,
              site_cfg={"seasonal_override": {"max_per_occasion": 5}})
    assert d.allowed and d.mode == "seasonal-override"
    # None = unlimited, and no occasion query is made.
    conn = _Conn(batch=["a1"], occ={"prime-big-deal-days": 50})
    d = _eval(storage, conn, today=TODAY,
              site_cfg={"seasonal_override": {"max_per_occasion": None}})
    assert d.allowed and conn.occ_queries == []


def test_occasion_cap_does_not_touch_open_traffic_gate(storage):
    _seed_gsc(storage, [{"keys": ["https://x/reviews/a1"], "impressions": 3}])
    d = _eval(storage, _Conn(batch=["a1"], occ={"prime-big-deal-days": 9}), today=TODAY)
    assert d.allowed and d.mode == "cap+traffic"


LANES = {"format_lanes": {"x-or-y": {"max_per_window": 2, "window_days": 7},
                          "cuisine-roundup": {"max_per_window": 1}}}


def test_format_lane_opens_closed_gate_for_tagged_only(storage):
    d = _eval(storage, _Conn(batch=["a1"]), site_cfg=LANES)
    assert d.allowed and d.mode == "format-lane" and d.slots == 1
    assert d.lane_ids == ["x-or-y", "cuisine-roundup"]
    assert "lane:<id>" in d.prompt_block() and "x-or-y" in d.prompt_block()
    props = [{"slug": "evergreen", "tags": ["gpu"]},
             {"slug": "pair", "tags": ["lane:x-or-y"]},
             {"slug": "roundup", "lane": "cuisine-roundup"}]
    kept, dropped = gate.select_for_queue(props, d)
    assert [p["slug"] for p in kept] == ["pair"]
    assert {p["slug"] for p in dropped} == {"evergreen", "roundup"}


def test_format_lane_cap_counts_db_and_ledger(storage):
    # x-or-y spent in the DB, cuisine-roundup spent in the ledger: closed.
    gate.record_batch(storage, "site-a-article-proposal-agent", slugs=["r1"],
                      lane_ids=gate.lane_ids_for([{"tags": ["lane:cuisine-roundup"]}]),
                      now=NOW - dt.timedelta(days=2))
    d = _eval(storage, _Conn(batch=["a1"], lanes={"x-or-y": 2}), site_cfg=LANES)
    assert not d.allowed
    # One x-or-y left: open for x-or-y only, one slot.
    d = _eval(storage, _Conn(batch=["a1"], lanes={"x-or-y": 1}), site_cfg=LANES,
              )
    assert d.allowed and d.lane_ids == ["x-or-y"] and d.lane_room == {"x-or-y": 1}
    # A ledger entry older than the lane window no longer counts.
    storage.write_json(gate.LEDGER_KEY_TMPL.format(
        agent_id="site-a-article-proposal-agent"), {"batches": []})
    gate.record_batch(storage, "site-a-article-proposal-agent", slugs=["r0"],
                      lane_ids=["cuisine-roundup"], now=NOW - dt.timedelta(days=8))
    d = _eval(storage, _Conn(batch=["a1"], lanes={"x-or-y": 2}), site_cfg=LANES)
    assert d.allowed and d.lane_ids == ["cuisine-roundup"]


def test_format_lane_boost_raises_cap_until_its_date(storage):
    cfg = {"format_lanes": {"x-or-y": {
        "max_per_window": 2, "window_days": 7,
        "boost": {"max_per_window": 3, "until": "2026-02-28"}}}}
    # Two already this week: the boosted cap of 3 leaves one slot.
    d = _eval(storage, _Conn(batch=["a1"], lanes={"x-or-y": 2}), site_cfg=cfg)
    assert d.allowed and d.lane_room == {"x-or-y": 1}
    # The until date itself is still boosted.
    d = _eval(storage, _Conn(batch=["a1"], lanes={"x-or-y": 2}), site_cfg=cfg,
              today=dt.date(2026, 2, 28))
    assert d.allowed and d.lane_room == {"x-or-y": 1}
    # The day after, the lane is back at 2 and closed.
    d = _eval(storage, _Conn(batch=["a1"], lanes={"x-or-y": 2}), site_cfg=cfg,
              today=dt.date(2026, 3, 1))
    assert not d.allowed
    # A malformed boost is ignored, not fatal.
    bad = {"format_lanes": {"x-or-y": {"max_per_window": 2,
                                       "boost": {"until": "soon"}}}}
    d = _eval(storage, _Conn(batch=["a1"], lanes={"x-or-y": 2}), site_cfg=bad)
    assert not d.allowed


def test_holiday_ranks_before_lane_under_seasonal_override(storage):
    d = _eval(storage, _Conn(batch=["a1"]), today=TODAY,
              site_cfg={**LANES, "max_proposals_per_day": 3})
    assert d.mode == "seasonal-override"
    assert d.require_holiday_ids == ["prime-big-deal-days"]
    assert d.lane_ids == ["x-or-y", "cuisine-roundup"]
    props = [{"slug": "p1", "tags": ["lane:x-or-y"]},
             {"slug": "p2", "tags": ["lane:x-or-y"]},
             {"slug": "p3", "tags": ["lane:x-or-y"]},
             {"slug": "deal", "tags": ["holiday:prime-big-deal-days"]}]
    kept, _ = gate.select_for_queue(props, d)
    # Holiday first, then x-or-y up to its room of 2.
    assert [p["slug"] for p in kept] == ["deal", "p1", "p2"]


def test_lanes_do_not_restrict_open_traffic_gate(storage):
    _seed_gsc(storage, [{"keys": ["https://x/reviews/a1"], "impressions": 3}])
    d = _eval(storage, _Conn(batch=["a1"]), site_cfg=LANES)
    assert d.mode == "cap+traffic" and d.lane_ids == []
    kept, _ = gate.select_for_queue([{"slug": "evergreen"}], d)
    assert [p["slug"] for p in kept] == ["evergreen"]


# ── Affiliate-click evidence (traffic_gate.affiliate_clicks, plan A09) ──

class _ClickCur(_Cur):
    """_Cur plus the human_clicks breakdown query on the click table."""

    def execute(self, sql, params=None):
        if "WITH w AS (" in sql and "outbound_clicks" in sql:
            if self.conn.fail_clicks:
                raise RuntimeError("no such table")
            self.conn.click_queries.append((sql, params))
            self.rows = list(self.conn.click_rows)
            return
        super().execute(sql, params)


class _ClickConn(_Conn):
    def __init__(self, *a, click_rows=(), fail_clicks=False, **kw):
        super().__init__(*a, **kw)
        self.click_rows = list(click_rows)
        self.fail_clicks = fail_clicks
        self.click_queries = []

    def cursor(self):
        return _ClickCur(self)


CLICKS = {"traffic_gate": {
    "article_path_prefixes": ["/reviews/"],
    "affiliate_clicks": {
        "enabled": True, "table": "outbound_clicks",
        "spec": {"time_col": "clicked_at", "referer_col": "source_page",
                 "country_col": "country", "bot_flag_col": "is_bot"},
        "match": {"target": "amazon", "via": "beacon"},
        "group_col": "source_page"}}}


def test_affiliate_clicks_off_by_default_changes_nothing(storage):
    conn = _ClickConn(batch=["a1"], click_rows=[("human", "/reviews/a1", 5)])
    d = _eval(storage, conn)
    assert not d.allowed and d.mode == "cap+traffic"
    assert conn.click_queries == [] and "affiliate_clicks" not in d.sources
    assert d.click_label == "" and "output_gate_prior_batch_affiliate_clicks" not in d.metrics()
    assert "earned a GSC impression or an AI/search visit" in d.reason
    assert gate.DEFAULTS["traffic_gate"]["affiliate_clicks"]["enabled"] is False
    # Old prompt wording when the source is off.
    d = _eval(storage, _ClickConn(batch=["a1"], ai={"/reviews/a1": 1}))
    assert "DID earn impressions or AI visits" in d.prompt_block()
    assert "affiliate_clicks" not in d.batch_hits[0]


def test_verified_click_opens_the_gate(storage):
    rows = [("human", "/reviews/a2", 7), ("site-flagged-bot", "/reviews/a1", 90),
            ("human", "/reviews/a1?utm_source=x", 1), ("human", "/product/B0X", 4),
            ("velocity", "/reviews/a3", 30)]
    conn = _ClickConn(batch=["a1", "a2", "a3"], click_rows=rows)
    d = _eval(storage, conn, site_cfg=CLICKS)
    assert d.allowed and d.mode == "cap+traffic"
    assert "impressions, AI visits or Amazon clicks" in d.reason
    # Click winners first; only human rows count; /product/ is outside the prefixes.
    assert [(h["slug"], h["affiliate_clicks"]) for h in d.batch_hits] == [("a2", 7), ("a1", 1)]
    assert d.sources["affiliate_clicks"].startswith("ok: 8 verified click(s) on 2")
    assert d.metrics()["output_gate_prior_batch_affiliate_clicks"] == 8.0
    block = d.prompt_block()
    assert "earned impressions, AI visits or Amazon clicks" in block
    assert "a2 (Amazon clicks: 7)" in block and "a1 (Amazon clicks: 1)" in block
    # One human_clicks query: window = lookback_days + 1, the configured match.
    sql, params = conn.click_queries[-1]
    assert "FROM outbound_clicks" in sql and "source_page AS _grp" in sql
    assert params[-3:] == [15, "amazon", "beacon"]


def test_min_clicks_and_closed_reason(storage):
    cfg = gate._deep_merge(CLICKS, {"traffic_gate": {"affiliate_clicks": {"min_clicks": 2}}})
    d = _eval(storage, _ClickConn(batch=["a1"], click_rows=[("human", "/reviews/a1", 1)]),
              site_cfg=cfg)
    assert not d.allowed
    assert "2+ verified Amazon clicks" in d.reason
    # min_clicks 0 would open the gate for everything; it is floored at 1.
    cfg0 = gate._deep_merge(CLICKS, {"traffic_gate": {"affiliate_clicks": {"min_clicks": 0}}})
    d = _eval(storage, _ClickConn(batch=["a1"]), site_cfg=cfg0)
    assert not d.allowed


def test_clicks_alone_are_enough_data(storage):
    # No GSC export, AI table unreadable: before, cap-only. With clicks
    # readable the gate still judges the batch.
    d = _eval(storage, _ClickConn(batch=["a1"], fail_ai=True), site_cfg=CLICKS)
    assert d.mode == "cap+traffic" and not d.allowed
    d = _eval(storage, _ClickConn(batch=["a1"], fail_ai=True,
                                  click_rows=[("human", "https://x.test/reviews/a1/", 2)]),
              site_cfg=CLICKS)
    assert d.allowed and d.batch_hits[0]["affiliate_clicks"] == 2


def test_click_source_failure_is_reported_not_fatal(storage):
    d = _eval(storage, _ClickConn(batch=["a1"], ai={"/reviews/a1": 1}, fail_clicks=True),
              site_cfg=CLICKS)
    assert d.allowed and d.sources["affiliate_clicks"].startswith("unavailable")
    assert d.click_label == "" and "affiliate_clicks" not in d.batch_hits[0]
    d = _eval(storage, _ClickConn(batch=["a1"], fail_ai=True, fail_clicks=True),
              site_cfg=CLICKS)
    assert d.mode == "cap-only" and "affiliate clicks unreadable" in d.reason
    bad = gate._deep_merge(CLICKS, {"traffic_gate": {"affiliate_clicks": {"table": None}}})
    d = _eval(storage, _ClickConn(batch=["a1"]), site_cfg=bad)
    assert d.sources["affiliate_clicks"].startswith("unavailable: ValueError")
    d = gate.evaluate(storage=storage, agent_id="z", site_id="site-a", dsn=None,
                      now=NOW, today=QUIET_DAY, site_cfg=CLICKS)
    assert d.sources["affiliate_clicks"] == "unavailable: no DSN"


def test_click_filter_resolves_with_the_agent_profile(storage):
    from framework.core import human_clicks
    storage.write_json(human_clicks.CONFIG_KEY, {
        "defaults": {"extra_bot_ua_terms": ["Version/17.0 Safari/"]},
        "by_profile": {"site-a-article-proposal-agent": {"max_clicks_per_ip_day": 3}}})
    conn = _ClickConn(batch=["a1"])
    _eval(storage, conn, site_cfg=CLICKS)
    sql, params = conn.click_queries[-1]
    assert any("Version/17\\.0\\ Safari/" in str(p) for p in params)
    assert "_per_ip_day > 3" in sql


def test_affiliate_clicks_by_slug_maps_urls_and_prefixes(storage, monkeypatch):
    # Hermetic: the click-filter config comes from the fixture storage, never
    # from the process default backend (Azure when secrets.env is sourced).
    from framework.core import storage as storage_mod
    default_backend_calls = []
    monkeypatch.setattr(storage_mod, "get_storage",
                        lambda *a, **k: default_backend_calls.append(a) or storage)
    conn = _ClickConn(click_rows=[("human", "https://s.test/reviews/A1?x=1", 2),
                                  ("human", "/reviews/a1/", 1), ("human", "/blog/b", 5),
                                  ("human", "", 4), ("ua-bot", "/reviews/z", 9)])
    ac = CLICKS["traffic_gate"]["affiliate_clicks"]
    out = gate.affiliate_clicks_by_slug(conn, ac, days=30, prefixes=["/reviews/"],
                                        storage=storage)
    assert out == {"a1": 3}
    assert gate.affiliate_clicks_by_slug(conn, ac, days=30, storage=storage)["b"] == 5
    assert default_backend_calls == []


# ── Canonical holiday ids + per-occasion caps by id (plan A02 / A01) ──

BF_DAY = dt.date(2026, 11, 13)   # black-friday + thanksgiving IMMINENT (tech)


def test_alias_tagged_proposal_survives_seasonal_override(storage):
    conn = _Conn(batch=["a1"], occ={"black-friday": 1})
    d = _eval(storage, conn, today=BF_DAY)
    assert d.mode == "seasonal-override" and d.audience == "tech"
    assert set(d.require_holiday_ids) == {"black-friday", "thanksgiving"}
    props = [{"slug": "evergreen", "tags": ["gpu"]},
             {"slug": "bf", "tags": ["holiday:black-friday-cyber-monday"]}]
    kept, dropped = gate.select_for_queue(props, d)
    assert [p["slug"] for p in kept] == ["bf"]
    assert gate.proposal_holiday_ids(props[1]) == {"black-friday"}
    assert gate.holiday_ids_for(props + [{"tags": ["holiday:bfcm"]},
                                         {"tags": ["holiday:none"]}]) == \
        ["black-friday", "black-friday"]
    assert gate.proposal_holiday_ids({"holiday": "Black Friday"}) == {"black-friday"}
    assert gate.proposal_holiday_ids({"tags": ["holiday:labor-day-2026"]}) == {"labor-day"}


def test_unknown_holiday_ids_are_dropped_with_a_warning(caplog):
    from framework.core import seasonal_calendar as sc
    sc._WARNED_UNKNOWN.discard("zz-not-a-holiday")
    with caplog.at_level("WARNING"):
        assert gate.proposal_holiday_ids({"tags": ["holiday:zz-not-a-holiday"]}) == set()
        gate.proposal_holiday_ids({"tags": ["holiday:zz-not-a-holiday"]})
    assert sum("zz-not-a-holiday" in r.getMessage() for r in caplog.records) == 1


def test_occasion_counts_include_alias_tags(storage):
    # 7 exact + 3 alias-tagged Black Friday articles: 10 against the cap.
    conn = _Conn(batch=["a1"], occ={"black-friday": 7, "black-friday-cyber-monday": 3})
    d = _eval(storage, conn, today=BF_DAY)
    assert d.occasion_counts["black-friday"] == 10
    assert "black-friday 10/2" in d.reason and d.require_holiday_ids == ["thanksgiving"]
    sql, params = conn.occ_queries[-1]
    assert "LIKE %s" in sql and params[0] == "holiday:%"


def test_canonical_occasion_count_is_exact_with_slugs():
    from framework.core import seasonal_calendar as sc

    class _SlugCur(_Cur):
        def execute(self, sql, params=None):
            self.rows = [("holiday:black-friday", 2, ["a", "b"]),
                         ("holiday:cyber-monday", 2, ["b", "c"]),
                         ("holiday:thanksgiving", 1, ["t"]), ("holiday:none", 5, list("vwxyz"))]

    class _SlugConn(_Conn):
        def cursor(self):
            return _SlugCur(self)

    out = gate.read_occasion_counts(_SlugConn(), table="editorial_articles", tags_column="tags",
                                    holiday_ids=["black-friday", "thanksgiving"], days=60,
                                    canonical=sc.canonicalizer("tech", warn=False))
    assert out == {"black-friday": 3, "thanksgiving": 1}


def test_ledger_alias_entries_count_toward_the_occasion(storage):
    # An older ledger entry may hold the alias the proposer wrote.
    gate.record_batch(storage, "site-a-article-proposal-agent", slugs=["x", "y"],
                      holiday_ids=["black-friday-cyber-monday", "black-friday"],
                      now=NOW - dt.timedelta(days=2))
    d = _eval(storage, _Conn(batch=["a1"]), today=BF_DAY)
    assert d.occasion_counts["black-friday"] == 2


def test_max_per_occasion_by_id_raises_one_occasion_only(storage):
    conn = _Conn(batch=["a1"], occ={"black-friday": 7, "black-friday-cyber-monday": 3,
                                    "thanksgiving": 2})
    cfg = {"seasonal_override": {"max_per_occasion_by_id": {
        "black-friday": {"max_per_occasion": 20, "until": "2026-12-01"}}}}
    d = _eval(storage, conn, today=BF_DAY, site_cfg=cfg)
    assert d.allowed and d.mode == "seasonal-override"
    assert d.require_holiday_ids == ["black-friday"]          # thanksgiving 2/2 stays shut
    assert "thanksgiving 2/2" in d.reason and d.occasion_counts["black-friday"] == 10
    assert d.slots == 1                                       # daily cap still 1
    # Expired: the default cap of 2 applies again, no config change.
    late = {"seasonal_override": {"max_per_occasion_by_id": {
        "black-friday": {"max_per_occasion": 20, "until": "2026-11-12"}}}}
    d = _eval(storage, conn, today=BF_DAY, site_cfg=late)
    assert not d.allowed and "black-friday 10/2" in d.reason


def test_occasion_caps_parsing():
    canon = __import__("framework.core.seasonal_calendar", fromlist=["x"]).canonicalizer(warn=False)
    so = {"max_per_occasion": 2, "max_per_occasion_by_id": {
        "cyber-monday": 20,                                   # alias key
        "thanksgiving": None,                                 # unlimited
        "christmas": {"until": "2026-12-31"},                 # malformed: ignored
        "halloween": {"max_per_occasion": "x"},               # malformed: ignored
        "labor-day": -1}}                                     # unlimited
    caps = gate.occasion_caps(so, ["black-friday", "thanksgiving", "christmas",
                                   "halloween", "labor-day", "easter"],
                              today=BF_DAY, canonical=canon)
    assert caps == {"black-friday": 20, "thanksgiving": None, "christmas": 2,
                    "halloween": 2, "labor-day": None, "easter": 2}
    assert gate.occasion_caps({"max_per_occasion": None}, ["x"]) == {"x": None}


def test_unlimited_by_id_occasion_opens_without_room_math(storage):
    conn = _Conn(batch=["a1"], occ={"black-friday": 40})
    cfg = {"max_proposals_per_day": 2,
           "seasonal_override": {"max_per_occasion_by_id": {"black-friday": None}}}
    d = _eval(storage, conn, today=BF_DAY, site_cfg=cfg)
    assert d.allowed and "black-friday" in d.require_holiday_ids and d.slots == 2


def test_operator_storage_override_for_one_agent(storage):
    # The 2026-11-12 shape (plan A01): one agent raised, everyone else on defaults.
    storage.write_json(gate.CONFIG_KEY, {
        "schema_version": "1",
        "by_agent_id": {"site-a-article-proposal-agent": {
            "max_proposals_per_day": 1, "traffic_gate": {"enabled": True},
            "seasonal_override": {"max_per_occasion_by_id": {
                "black-friday": {"max_per_occasion": 20, "until": "2026-12-01"}}}}}})
    conn = _Conn(batch=["a1"], occ={"black-friday": 10})
    d = _eval(storage, conn, today=BF_DAY)
    assert d.allowed and "black-friday" in d.require_holiday_ids and d.slots == 1
    other = gate.evaluate(storage=storage, agent_id="site-b-article-proposal-agent",
                          site_id="site-b", conn=conn, now=NOW, today=BF_DAY,
                          audience="tech")
    assert "black-friday" not in other.require_holiday_ids


def test_storage_calendar_alias_reaches_select_for_queue(storage):
    from framework.core import seasonal_calendar as sc
    storage.write_json(sc.CONFIG_KEY, {"aliases": {"black-friday": ["doorbusters"]}})
    d = _eval(storage, _Conn(batch=["a1"]), today=BF_DAY)
    kept, _ = gate.select_for_queue([{"slug": "p", "tags": ["holiday:doorbusters"]}], d)
    assert [p["slug"] for p in kept] == ["p"]
    assert "_occasions" not in d.to_dict()


def test_format_lane_drops_capped_occasion_proposals(storage):
    # Every NOW/IMMINENT occasion is at its cap: only lane proposals qualify,
    # whatever spelling the capped occasion's tag uses.
    conn = _Conn(batch=["a1"], occ={"black-friday": 10, "thanksgiving": 2})
    d = _eval(storage, conn, today=BF_DAY, site_cfg=LANES)
    assert d.mode == "format-lane" and d.require_holiday_ids == []
    assert set(d.priority_holiday_ids) == {"black-friday", "thanksgiving"}
    props = [{"slug": "bf-exact", "tags": ["holiday:black-friday"]},
             {"slug": "bf-alias", "tags": ["holiday:black-friday-cyber-monday"]},
             {"slug": "tg-2026", "tags": ["holiday:thanksgiving-2026"]},
             {"slug": "pair", "tags": ["lane:x-or-y"]}]
    kept, dropped = gate.select_for_queue(props, d)
    assert [p["slug"] for p in kept] == ["pair"]
    assert {p["slug"] for p in dropped} == {"bf-exact", "bf-alias", "tg-2026"}
    # A capped-occasion piece that also carries an open lane tag is a lane piece.
    both = {"slug": "bf-lane", "tags": ["holiday:black-friday", "lane:x-or-y"]}
    kept, _ = gate.select_for_queue([both, props[3]], d)
    assert [p["slug"] for p in kept] == ["bf-lane"]
    block = d.prompt_block()
    assert "Holiday-tagged proposals go first" not in block
    assert "Already at their seasonal cap" in block
    assert "black-friday" in block and "lane tags above" in block


def test_restricted_selection_never_falls_back_to_priority_ids():
    # The reviewer's probe: a bare format-lane decision with capped
    # priority occasions. Exact, alias and year-suffixed tags all lose.
    d = gate.GateDecision(True, 1, "probe", "format-lane",
                          priority_holiday_ids=["black-friday", "thanksgiving"],
                          require_holiday_ids=[], lane_ids=["ai-hardware"],
                          lane_room={"ai-hardware": 1}, audience="tech")
    for tag in ("holiday:black-friday", "holiday:black-friday-cyber-monday",
                "holiday:thanksgiving-2026"):
        kept, _ = gate.select_for_queue(
            [{"slug": "h", "tags": [tag]}, {"slug": "lane", "tags": ["lane:ai-hardware"]}], d)
        assert [p["slug"] for p in kept] == ["lane"], tag


def test_seasonal_override_keeps_open_occasion_and_drops_capped_one(storage):
    conn = _Conn(batch=["a1"], occ={"black-friday": 1, "thanksgiving": 2})
    d = _eval(storage, conn, today=BF_DAY)
    assert d.mode == "seasonal-override" and d.require_holiday_ids == ["black-friday"]
    props = [{"slug": "tg", "tags": ["holiday:thanksgiving-2026"]},
             {"slug": "bf", "tags": ["holiday:cyber-monday"]}]
    kept, _ = gate.select_for_queue(props, d)
    assert [p["slug"] for p in kept] == ["bf"]
    block = d.prompt_block()
    assert "Holiday-tagged proposals go first" in block
    assert "Already at their seasonal cap, so not written: proposals for thanksgiving." in block
