"""Placement watch: verified-human clicks per money placement before and after
deploys (framework/core/placement_watch.py)."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from framework.core import deploy_repair as dr
from framework.core import human_clicks
from framework.core import placement_watch as pw

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)

CFG = {**pw.DEFAULTS,
       "table": "outbound_clicks", "placement_col": "placement",
       "spec": {"time_col": "clicked_at", "referer_col": "source_page", "country_col": "country",
                "bot_flag_col": "is_bot"},
       "match": {"target": "amazon", "via": "beacon"},
       "money_placements": {"hero": ["pdp-hero", "pdp-hero-live"], "sticky": ["sticky-mobile"],
                            "cart": ["build-cart"]}}


def _hourly(per_day_pattern, days=14, start=T0, scale_from=None, scale=1.0):
    """{hour: n}: the same 24-hour pattern every day; from `scale_from` on,
    counts are multiplied by `scale` (rounded down)."""
    out = {}
    for d in range(days):
        for h, n in enumerate(per_day_pattern):
            t = start + timedelta(days=d, hours=h)
            out[t] = int(n * scale) if scale_from is not None and t >= scale_from else n
    return out


HERO = [2] * 8 + [3] * 8 + [4] * 8          # 72 a day
STICKY = [0, 1] * 12                        # 12 a day
CART = [0] * 23 + [1]                       # 1 a day: too sparse to judge


def _series(**over):
    s = {"hero": _hourly(HERO), "sticky": _hourly(STICKY), "cart": _hourly(CART)}
    s.update(over)
    s[pw.TOTAL] = {}
    for g in ("hero", "sticky", "cart"):
        for h, n in s[g].items():
            s[pw.TOTAL][h] = s[pw.TOTAL].get(h, 0) + n
    return s


DEPLOY_AT = T0 + timedelta(days=10, hours=9, minutes=40)
DEPLOYS = [{"at": (T0 + timedelta(days=9, hours=20)).isoformat(), "tag": "20261110-2000", "sha": "a" * 40},
           {"at": DEPLOY_AT.isoformat(), "tag": "20261111-0940", "sha": "b" * 40}]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_site_config_layers_and_opt_in(storage):
    assert pw.site_config("site-a", storage=storage) is None            # nothing stored: not watched
    storage.write_json(pw.CONFIG_KEY, {"_note": "x", "defaults": {"min_z": 3, "_why": "n"},
                                       "sites": {"site-a": {"table": "t", "drop_threshold": 0.5},
                                                 "site-b": {"table": "t", "enabled": False}}})
    cfg = pw.site_config("site-a", storage=storage)
    assert cfg["min_z"] == 3 and cfg["drop_threshold"] == 0.5 and cfg["min_window_h"] == 24
    assert "_why" not in cfg and cfg["table"] == "t"
    assert pw.site_config("site-b", storage=storage) is None
    assert pw.site_config("site-c", storage=storage) is None


def test_broken_storage_watches_nothing():
    class Broken:
        def read_json(self, key):
            raise RuntimeError("archived")
    assert pw.site_config("site-a", storage=Broken()) is None


def test_placement_groups_first_group_wins():
    groups = pw.placement_groups({"money_placements": {"hero": ["pdp-hero", "x"], "other": ["x", "y"]}})
    assert groups == {"pdp-hero": "hero", "x": "hero", "y": "other"}


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def test_a_deploy_is_judged_only_after_min_window_h_full_hours():
    s = _series()
    early = pw.evaluate(s, DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=23))
    assert early["anchor"]["tag"] == "20261110-2000"          # the older deploy is judged instead
    assert "20261111-0940" in early["pending"]
    later = pw.evaluate(s, DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=24, minutes=30))
    assert later["anchor"]["tag"] == "20261111-0940" and later["anchor"]["window_h"] == 24
    assert later["flagged"] == [] and later["placements"]["hero"]["status"] == "ok"


def test_steady_clicks_raise_nothing():
    s = _series()
    for h in range(0, 72, 3):
        r = pw.evaluate(s, DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=24 + h))
        assert r["flagged"] == []


def test_a_50_percent_drop_after_a_deploy_is_flagged_with_its_likely_cause():
    start = pw._ceil_hour(DEPLOY_AT)
    s = _series(hero=_hourly(HERO, scale_from=start, scale=0.5))
    r = pw.evaluate(s, DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=25))
    assert r["flagged"] == ["hero"]
    hero = r["placements"]["hero"]
    assert hero["status"] == "drop" and hero["drop_pct"] >= 40 and hero["z"] >= 2.5
    assert hero["after"] == 8 * 1 + 8 * 1 + 8 * 2 and hero["baseline_mean"] == 72.0   # int() halving
    assert hero["likely_cause"]["tag"] == "20261111-0940"
    assert hero["likely_cause"]["after_pct_of_baseline"] == round(100 * 32 / 72)
    assert hero["likely_cause"]["before_pct_of_baseline"] == 100
    assert r["placements"]["sticky"]["status"] == "ok"
    assert r["placements"]["cart"]["status"] == "sparse"
    assert r["total"]["after"] < r["total"]["baseline_mean"]


def test_the_older_deploys_window_already_shows_a_drop_that_started_later():
    # The judged deploy predates the drop by 14 h; its window is still mostly
    # after the drop, and the likely cause points at the later deploy.
    start = pw._ceil_hour(DEPLOY_AT)
    s = _series(hero=_hourly(HERO, scale_from=start, scale=0.3))
    r = pw.evaluate(s, DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=12))
    assert r["anchor"]["tag"] == "20261110-2000" and r["flagged"] == ["hero"]
    assert r["placements"]["hero"]["likely_cause"]["tag"] == "20261111-0940"
    assert r["placements"]["hero"]["deploys_in_window"] == ["20261111-0940"]


def test_a_noisy_placement_needs_more_than_a_30_percent_dip():
    # Days alternate 6 / 18 clicks: one 50% day is inside that spread.
    noisy = {}
    for d in range(14):
        n = 6 if d % 2 else 18
        for h in range(24):
            noisy[T0 + timedelta(days=d, hours=h)] = (n if h == 12 else 0)
    start = pw._ceil_hour(DEPLOY_AT)
    for h, n in list(noisy.items()):
        if h >= start and n:
            noisy[h] = 6
    r = pw.evaluate(_series(sticky=noisy), DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=25))
    st = r["placements"]["sticky"]
    assert st["drop_pct"] >= 30 and st["z"] < 2.5 and st["status"] == "ok"


def test_deploys_older_than_anchor_max_age_are_not_judged():
    r = pw.evaluate(_series(), DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=73))
    assert r["anchor"] is None and r["placements"] == {}


def test_windows_cap_at_max_window_h():
    only_old = DEPLOYS[:1]
    r = pw.evaluate(_series(), only_old, CFG, now=T0 + timedelta(days=12, hours=10))
    assert r["anchor"]["window_h"] == 48


# ---------------------------------------------------------------------------
# Incidents: one email per drop, closed when back in range
# ---------------------------------------------------------------------------

def _drop_result():
    start = pw._ceil_hour(DEPLOY_AT)
    s = _series(hero=_hourly(HERO, scale_from=start, scale=0.5))
    return pw.evaluate(s, DEPLOYS, CFG, now=DEPLOY_AT + timedelta(hours=25))


EMAIL = {"to": ["ops@example.com"], "from": "Agent <bot@example.com>"}


def test_a_drop_opens_one_incident_and_sends_one_email(storage):
    sent = []

    def send(email, subject, body):
        sent.append((email, subject, body))
        return True, "sent"
    now = DEPLOY_AT + timedelta(hours=25)
    out = pw.update_incidents(_drop_result(), "site-a", storage=storage, email=EMAIL, send=send, now=now)
    assert out["new"] == ["hero"] and out["alerted"] == ["hero"] and len(sent) == 1
    _, subject, body = sent[0]
    assert subject.startswith("[placement-watch:site-a] clicks down on hero after deploy 20261111-0940")
    assert "20261111-0940" in body and "likely cause" in body and "implementer-allowed-dispatch-kinds" in body
    st = storage.read_json(pw.state_key("site-a"))
    assert st["open"]["hero"]["alerted_at"] and st["open"]["hero"]["likely_cause"]["tag"] == "20261111-0940"
    # Still down on the next run: no second email.
    out = pw.update_incidents(_drop_result(), "site-a", storage=storage, email=EMAIL, send=send,
                              now=now + timedelta(days=1))
    assert out["new"] == [] and len(sent) == 1 and out["open"] == ["hero"]
    # Back in range: closed into the history.
    ok = pw.evaluate(_series(), DEPLOYS, CFG, now=now)
    out = pw.update_incidents(ok, "site-a", storage=storage, email=EMAIL, send=send, now=now + timedelta(days=2))
    assert out["closed"] == ["hero"] and len(sent) == 1
    st = storage.read_json(pw.state_key("site-a"))
    assert st["open"] == {} and st["incidents"][-1]["closed_reason"] == "back in range"


def test_an_email_that_did_not_go_out_is_retried_next_run(storage):
    calls = []

    def flaky_send(email, subject, body):
        calls.append(subject)
        return (len(calls) > 1), ("sent" if len(calls) > 1 else "graph down")
    now = DEPLOY_AT + timedelta(hours=25)
    out = pw.update_incidents(_drop_result(), "site-a", storage=storage, email=EMAIL, send=flaky_send, now=now)
    assert out["alerted"] == [] and out["email"] == "graph down"
    out = pw.update_incidents(_drop_result(), "site-a", storage=storage, email=EMAIL, send=flaky_send,
                              now=now + timedelta(hours=1))
    assert out["alerted"] == ["hero"] and len(calls) == 2


def test_no_reporter_email_records_the_incident_without_sending(storage):
    out = pw.update_incidents(_drop_result(), "site-a", storage=storage, email={},
                              now=DEPLOY_AT + timedelta(hours=25))
    assert out["new"] == ["hero"] and out["alerted"] == [] and "no reporter.email" in out["email"]


def test_dry_run_writes_and_sends_nothing(storage):
    def send(*a):
        raise AssertionError("dry run sent mail")
    out = pw.update_incidents(_drop_result(), "site-a", storage=storage, email=EMAIL, send=send,
                              now=DEPLOY_AT + timedelta(hours=25), dry_run=True)
    assert out["new"] == ["hero"] and out["dry_run"]
    assert storage.read_json(pw.state_key("site-a")) is None


# ---------------------------------------------------------------------------
# SQL and the series
# ---------------------------------------------------------------------------

SPEC = human_clicks.resolve_spec(CFG["spec"], config={})


def test_query_uses_human_clicks_own_verdicts_and_velocity_rule():
    start, end = T0, T0 + timedelta(days=1)
    sql, params = pw.build_series_query(CFG, SPEC, start=start, end=end, today=T0.date())
    reason, rparams = human_clicks._reason_case(SPEC, today=T0.date())
    assert reason in sql and "_per_ip_day <= 20" in sql
    assert "PARTITION BY _ip, (_t AT TIME ZONE 'UTC')::date" in sql
    assert "COALESCE(CAST(placement AS text), '')" in sql
    assert sql.count("%s") == len(params)
    assert params[:len(rparams)] == rparams
    assert params[len(rparams):len(rparams) + 2] == [start, end] and params[-2:] == ["amazon", "beacon"]


def test_query_from_referer_rules_and_identifier_checks():
    cfg = {**CFG, "placement_col": None, "match": {"source": ["amazon", "AMAZON"]},
           "placement_from_referer": [{"value": "guide", "pattern": "/blog/best-"}]}
    spec = human_clicks.resolve_spec({"time_col": "created_at", "referer_col": "referer"}, config={})
    sql, params = pw.build_series_query(cfg, spec, start=T0, end=T0, today=T0.date())
    assert "CASE WHEN COALESCE(referer, '') ~* %s THEN %s ELSE '' END" in sql
    assert "source = ANY(%s)" in sql and ["amazon", "AMAZON"] in params
    assert "/blog/best-" in params and "guide" in params and sql.count("%s") == len(params)
    with pytest.raises(ValueError):
        pw.build_series_query({**CFG, "table": "t; DROP TABLE x"}, SPEC, start=T0, end=T0)
    with pytest.raises(ValueError):
        pw.build_series_query({**cfg, "placement_from_referer": [{"value": "x"}]}, spec, start=T0, end=T0)
    with pytest.raises(ValueError):
        pw.build_series_query({**CFG, "placement_col": None}, SPEC, start=T0, end=T0)


class _Cur:
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, None, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self):
        return self.cur


def test_fetch_series_groups_raw_placements_and_keeps_a_total():
    h = datetime(2026, 11, 10, 14, 0)                     # date_trunc returns a naive UTC hour
    conn = _Conn([(h, "pdp-hero", 3), (h, "pdp-hero-live", 2), (h, "inline", 4), (h, None, 1),
                  (h + timedelta(hours=1), "sticky-mobile", 1)])
    s = pw.fetch_series(conn, CFG, SPEC, start=T0, end=T0)
    hu = h.replace(tzinfo=timezone.utc)
    assert s["hero"] == {hu: 5} and s["sticky"] == {hu + timedelta(hours=1): 1} and s["cart"] == {}
    assert s[pw.TOTAL] == {hu: 10, hu + timedelta(hours=1): 1}


def test_scan_range_covers_every_window_from_a_utc_midnight():
    now = datetime(2026, 11, 11, 18, 37, tzinfo=timezone.utc)
    start, end = pw.scan_range(CFG, now)
    assert end == now and start.hour == 0 and start.minute == 0
    assert now - start >= timedelta(hours=72 + 24 * 10)


# ---------------------------------------------------------------------------
# Deploys and the consumer entry point
# ---------------------------------------------------------------------------

def test_deploys_come_from_the_gate_state_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_GATE_STATE_DIR", str(tmp_path / "gs"))
    st = dr.GateState.load("site-a")
    st.record_deployed("c" * 40, "20261111-1200", now=datetime(2026, 11, 11, 12, 3, tzinfo=timezone.utc))
    st.save()
    assert pw.deploys_for("site-a") == [{"at": "2026-11-11T12:03:00+00:00", "sha": "c" * 40,
                                         "tag": "20261111-1200"}]


def test_deploys_from_release_tags(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True,  # noqa: E731
                                    text=True, env={"GIT_COMMITTER_DATE": "2026-11-10T08:00:00+00:00",
                                                    "GIT_AUTHOR_DATE": "2026-11-10T08:00:00+00:00",
                                                    "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (r / "f").write_text("x")
    git("add", "f")
    git("commit", "-q", "-m", "x")
    git("tag", "-a", "release/site-a/0001", "-m", "release 0001")
    git("tag", "-a", "release/other/0001", "-m", "release 0001")
    out = pw.deploys_from_release_tags(str(r), "site-a")
    assert len(out) == 1 and out[0]["tag"] == "release/site-a/0001"
    assert out[0]["at"] == "2026-11-10T08:00:00+00:00" and len(out[0]["sha"]) == 40


def test_watch_site_end_to_end_with_a_fake_db(storage):
    storage.write_json(pw.CONFIG_KEY, {"sites": {"site-a": {k: v for k, v in CFG.items()
                                                            if k not in pw.DEFAULTS}}})
    now = DEPLOY_AT + timedelta(hours=25)
    start = pw._ceil_hour(DEPLOY_AT)
    rows = [(h.replace(tzinfo=None), "pdp-hero", n)
            for h, n in _hourly(HERO, scale_from=start, scale=0.5).items() if n and h < now]
    sent = []
    res = pw.watch_site("site-a", conn=_Conn(rows), storage=storage, email=EMAIL, now=now,
                        deploys=DEPLOYS, send=lambda e, s, b: (sent.append(s) or (True, "sent")))
    assert res["flagged"] == ["hero"] and res["incidents"]["new"] == ["hero"] and len(sent) == 1
    assert any("hero: drop" in line for line in pw.summary_lines(res))
    assert pw.watch_site("site-b", conn=_Conn([]), storage=storage)["skipped"]


# ---------------------------------------------------------------------------
# The consumer: agents/site-goals-tracker
# ---------------------------------------------------------------------------

def _tracker(monkeypatch):
    import importlib.util as iu
    from pathlib import Path
    for k in ("SITE_GOALS_SITE_YAML", "SITE_GOALS_REPOS_ROOT", "INDEXNOW_SITE_REPOS_ROOT"):
        monkeypatch.delenv(k, raising=False)
    path = Path(__file__).resolve().parents[2] / "agents" / "site-goals-tracker" / "agent.py"
    spec = iu.spec_from_file_location("site_goals_tracker_pw", path)
    m = iu.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _RoConn:
    def __init__(self):
        self.session, self.closed = None, False

    def set_session(self, **kw):
        self.session = kw

    def close(self):
        self.closed = True


def test_tracker_skips_a_site_with_no_placement_watch_entry(monkeypatch, storage):
    mod = _tracker(monkeypatch)
    monkeypatch.setattr(mod.psycopg2, "connect", lambda *a, **k: pytest.fail("connected"))
    assert mod.run_placement_watch("specpicks", dict(mod.SITE_PROFILES["specpicks"])) is None


def test_tracker_runs_the_watch_read_only_with_the_sites_reporter_email(monkeypatch, storage, tmp_path):
    mod = _tracker(monkeypatch)
    storage.write_json(pw.CONFIG_KEY, {"sites": {"specpicks": {"table": "outbound_clicks"}}})
    yml = tmp_path / "site.yaml"
    yml.write_text("reporter:\n  email:\n    to: [ops@example.com]\n    from: bot@example.com\n")
    monkeypatch.setenv("SITE_GOALS_SITE_YAML", str(yml))
    monkeypatch.setenv("SPECPICKS_DATABASE_URL", "test-dsn-placeholder")
    conn = _RoConn()
    monkeypatch.setattr(mod.psycopg2, "connect", lambda *a, **k: conn)
    seen = {}

    def fake_watch(site, **kw):
        seen.update(kw, site=site)
        return {"site": site, "anchor": None, "pending": []}
    monkeypatch.setattr(pw, "watch_site", fake_watch)
    res = mod.run_placement_watch("specpicks", dict(mod.SITE_PROFILES["specpicks"]), dry_run=True)
    assert res["site"] == "specpicks" and seen["site"] == "specpicks" and seen["dry_run"] is True
    assert seen["email"] == {"to": ["ops@example.com"], "from": "bot@example.com"}
    assert seen["profile"] == "specpicks-site-goals-tracker" and seen["cfg"]["table"] == "outbound_clicks"
    assert conn.session == {"readonly": True, "autocommit": True} and conn.closed


def test_tracker_survives_a_failing_watch(monkeypatch, storage):
    mod = _tracker(monkeypatch)
    storage.write_json(pw.CONFIG_KEY, {"sites": {"specpicks": {"table": "outbound_clicks"}}})
    monkeypatch.setenv("SPECPICKS_DATABASE_URL", "test-dsn-placeholder")
    monkeypatch.setattr(mod.psycopg2, "connect", lambda *a, **k: _RoConn())

    def boom(site, **kw):
        raise RuntimeError("db went away")
    monkeypatch.setattr(pw, "watch_site", boom)
    assert mod.run_placement_watch("specpicks", dict(mod.SITE_PROFILES["specpicks"])) is None


# ---------------------------------------------------------------------------
# Sparse placements: judged from the point a total collapse could flag
# ---------------------------------------------------------------------------

def _flat(per_window, start=T0, days=14):
    """per_window clicks in hour 12 of each day: a 24 h window holds exactly that many."""
    return {start + timedelta(days=d, hours=12): per_window for d in range(days)}


def test_the_sparse_floor_is_where_a_total_collapse_could_reach_min_z():
    assert pw.sparse_floor(CFG) == 6.25                                 # min_z 2.5 squared
    assert pw.sparse_floor({**CFG, "min_z": 2.0}) == 4.0
    assert pw.sparse_floor({**CFG, "min_baseline_clicks": 8}) == 8.0     # a higher floor still wins
    a = T0 + timedelta(days=10)
    seven = _flat(7)
    assert pw._stats(seven, a, 24, CFG)["status"] == "ok"               # mean 7: judged now (was sparse at 8)
    gone = {h: (0 if h >= a else n) for h, n in seven.items()}
    st = pw._stats(gone, a, 24, CFG)
    assert st["status"] == "drop" and st["z"] >= 2.5                    # a total collapse is caught
    one_left = {h: (1 if h >= a else n) for h, n in seven.items()}
    assert pw._stats(one_left, a, 24, CFG)["status"] == "ok"            # only a near-total collapse flags
    assert pw._stats(_flat(6), a, 24, CFG)["status"] == "sparse"        # z could never reach 2.5
    assert pw._stats({h: 0 for h in _flat(6)}, a, 24, CFG)["status"] == "sparse"


# ---------------------------------------------------------------------------
# The live CLI the deployer starts after every gated run
# ---------------------------------------------------------------------------

LIVE_CFG = {"sites": {"site-a": {k: v for k, v in CFG.items() if k not in pw.DEFAULTS}}}


@pytest.fixture
def live(storage, monkeypatch):
    import psycopg2
    calls = []
    monkeypatch.setenv("DATABASE_URL_SITE_A", "test-dsn-placeholder")
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: (calls.append(("connect", a, k)) or _RoConnPW()))

    def fake_watch(site, **kw):
        calls.append(("watch", site, kw))
        return {"site": site, "anchor": None, "pending": []}
    monkeypatch.setattr(pw, "watch_site", fake_watch)
    return calls


class _RoConnPW:
    def set_session(self, **kw):
        self.session = kw

    def close(self):
        pass


def test_cli_live_records_and_emails_through_watch_site(storage, live):
    storage.write_json(pw.CONFIG_KEY, LIVE_CFG)
    assert pw.main(["--site", "site-a", "--live", "--min-interval-m", "55",
                    "--email-json", json.dumps(EMAIL)]) == 0
    (_, _, ck), (_, site, kw) = live
    assert site == "site-a" and kw["dry_run"] is False and kw["email"] == EMAIL
    assert "statement_timeout" in ck.get("options", "")
    assert pw.main(["--site", "site-a"]) == 0 and live[-1][2]["dry_run"] is True     # no --live: a dry run


def test_cli_live_skips_a_site_evaluated_within_the_interval(storage, live):
    storage.write_json(pw.CONFIG_KEY, LIVE_CFG)
    storage.write_json(pw.state_key("site-a"), {"last_run": {
        "at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()}})
    assert pw.main(["--site", "site-a", "--live", "--min-interval-m", "55"]) == 0
    assert live == []                                                    # no DB connection, no evaluation
    assert pw.main(["--site", "site-a", "--live", "--min-interval-m", "15"]) == 0
    assert [c[0] for c in live] == ["connect", "watch"]


def test_cli_live_refuses_backtest_inputs_and_skips_unwatched_sites(storage, live, tmp_path):
    storage.write_json(pw.CONFIG_KEY, LIVE_CFG)
    f = tmp_path / "cfg.json"
    f.write_text(json.dumps(LIVE_CFG))
    for extra in (["--config", str(f)], ["--tags-repo", str(tmp_path)], ["--as-of", "2026-11-11T12:00:00Z"]):
        assert pw.main(["--site", "site-a", "--live", *extra]) == 2
    assert pw.main(["--site", "site-a", "--live", "--email-json", "{not json"]) == 2
    assert pw.main(["--site", "site-z", "--live"]) == 0                  # not watched: nothing to do
    assert pw.main(["--site", "site-z"]) == 1
    assert live == []


def test_deployer_starts_the_live_watch_in_the_background(monkeypatch, tmp_path):
    import importlib.util as iu
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "agents" / "deployer" / "deployer.py"
    spec = iu.spec_from_file_location("deployer_pw_test", path)
    d = iu.module_from_spec(spec)
    spec.loader.exec_module(d)
    started = []
    monkeypatch.setattr(d.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    monkeypatch.setenv("FRAMEWORK_DISPATCH_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("PLACEMENT_WATCH_AFTER_DEPLOY", raising=False)

    class Cfg(dict):
        site_id = "site-a"
    d._kick_placement_watch(Cfg(reporter={"email": EMAIL}))
    (cmd, kw), = started
    assert cmd[1:5] == ["-m", "framework.core.placement_watch", "--site", "site-a"]
    assert "--live" in cmd and cmd[cmd.index("--min-interval-m") + 1] == "55"
    assert json.loads(cmd[cmd.index("--email-json") + 1]) == EMAIL
    root = str(Path(__file__).resolve().parents[2])
    assert kw["cwd"] == root and kw["env"]["PYTHONPATH"].split(":")[0] == root
    assert kw["start_new_session"] is True and kw["stdin"] is d.subprocess.DEVNULL
    assert (tmp_path / "placement-watch-site-a.log").exists()
    monkeypatch.setenv("PLACEMENT_WATCH_AFTER_DEPLOY", "0")
    d._kick_placement_watch(Cfg())
    assert len(started) == 1
    monkeypatch.delenv("PLACEMENT_WATCH_AFTER_DEPLOY")

    def boom(cmd, **kw):
        raise OSError("no fork")
    monkeypatch.setattr(d.subprocess, "Popen", boom)
    d._kick_placement_watch(Cfg())                                       # never raises into the deploy


def test_deployer_runs_the_watch_only_after_gated_runs():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "agents" / "deployer" / "deployer.py").read_text()
    tail = text[text.rindex("    finally:"):]
    assert "if not args.dry_run and not args.skip_test:\n            _kick_placement_watch(cfg)" in tail
