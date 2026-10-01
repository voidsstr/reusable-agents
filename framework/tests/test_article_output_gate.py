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
                 occ=None):
        self.batch = list(batch)
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
