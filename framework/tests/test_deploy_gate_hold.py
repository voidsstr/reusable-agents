"""Deploy-gate hold: app-code dispatches wait while a site's gate stays red.

2026-11-09..11: one site's gate was red for two days on failures no code
change could fix. Every app-code dispatch still ran an Opus implementer
session, then waited 40-70 minutes in the gate and exited rc=1 (9 such runs
and 3 reaped as stale in half a day). deploy_repair.gate_hold() decides,
dispatch.dispatch_now() asks it before spawning, and gated_dispatch_now()
turns a hold into the same None the email-approval path returns.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from framework.core import deploy_repair as dr
from framework.core import defer_backoff, dispatch

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
SITE = "site-a"
ON = {"schema_version": "1", "enabled": True, "red_for_h": 6, "probe_every_h": 4}


@pytest.fixture
def gate_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_GATE_STATE_DIR", str(tmp_path / "gate-state"))
    monkeypatch.delenv("DEPLOY_GATE_HOLD_DISABLED", raising=False)
    monkeypatch.delenv("DATA_ONLY_KINDS", raising=False)
    return tmp_path / "gate-state"


def _red(hours_red: float, *, last_run_h_ago: float = 0.5, site: str = SITE,
         now: datetime = NOW) -> dr.GateState:
    st = dr.GateState.load(site)
    code = dr.Classification("code", "5 test(s) failed", 1, [dr.TestFailure("t", "a.ts")])
    st.record_red(code, "sig1", attempts=0, exhausted=False, cfg=dr.DEFAULTS,
                  now=now - timedelta(hours=hours_red))
    if last_run_h_ago < hours_red:
        st.record_red(code, "sig1", attempts=0, exhausted=False, cfg=dr.DEFAULTS,
                      now=now - timedelta(hours=last_run_h_ago))
    st.save()
    return st


def _red_now(hours_red: float, **kw) -> dr.GateState:
    """Red relative to the wall clock: dispatch_now asks gate_hold() about now."""
    return _red(hours_red, now=datetime.now(timezone.utc), **kw)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def test_off_unless_the_storage_config_enables_it(storage, gate_dir):
    _red(49)
    d = dr.gate_hold(SITE, "seo", storage=storage, now=NOW)
    assert not d.hold and not d.probe
    storage.write_json(dr.HOLD_CONFIG_KEY, {"red_for_h": 1})       # present, but not enabled
    assert not dr.gate_hold(SITE, "seo", storage=storage, now=NOW).hold


def test_holds_app_code_once_red_for_long_enough(storage, gate_dir):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red(49)
    d = dr.gate_hold(SITE, "seo", storage=storage, now=NOW)
    assert d.hold and not d.probe and d.red_for_h == 49.0 and d.gate_status == "red"
    assert "red for 49h" in d.reason and "prefer DB-only levers" in d.reason
    assert "db_only: true" in d.reason and "5 test(s) failed" in d.reason
    assert d.retry_at == (NOW + timedelta(hours=1)).isoformat()


# ---------------------------------------------------------------------------
# A batch of recs marked DB-only is never held
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recs", [
    [{"id": "growth-01", "db_only": True}],
    [{"id": "rec-009", "dispatch_kind": "db-only"}, {"id": "rec-010", "db_only": True}],
    [{"id": "rec-001", "dispatch_kind": "catalog-audit"}],
    [{"id": "shelf-1", "dispatch_kind": "shelf-audit"}],          # a pass_kinds entry, per rec
])
def test_a_batch_whose_every_rec_is_marked_db_only_passes(storage, gate_dir, recs):
    storage.write_json(dr.HOLD_CONFIG_KEY, {**ON, "pass_kinds": ["shelf-audit"]})
    _red(49)
    d = dr.gate_hold(SITE, "seo", subject_tag="growth", recs=recs, storage=storage, now=NOW)
    assert not d.hold and not d.probe and "marked DB-only" in d.reason


@pytest.mark.parametrize("recs", [
    [{"id": "growth-01", "db_only": True}, {"id": "growth-02"}],     # one unmarked rec holds the batch
    [{"id": "rec-001", "db_only": "yes"}],                           # only a real true counts
    [{}],                                                            # a rec id missing from the file
    [],
])
def test_a_batch_with_any_unmarked_rec_is_held(storage, gate_dir, recs):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red(49)
    assert dr.gate_hold(SITE, "seo", subject_tag="growth", recs=recs, storage=storage, now=NOW).hold


def test_status_message_tells_strategists_how_to_get_db_only_work_through(storage, gate_dir):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red(49)
    msg = dr.gate_hold_status(SITE, storage=storage, now=NOW)["message"]
    assert msg.startswith("DEPLOY GATE:") and "mark them db_only" in msg


@pytest.mark.parametrize("kind,tag", [("h2h", ""), ("catalog-audit", ""), ("article-author", ""),
                                      ("article-refresh", ""), ("seo", "catalog-audit"),
                                      ("seo", "product-hydration")])
def test_data_only_kinds_always_pass(storage, gate_dir, kind, tag):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red(49)
    assert not dr.gate_hold(SITE, kind, subject_tag=tag, storage=storage, now=NOW).hold


def test_extra_pass_kinds_from_config(storage, gate_dir):
    storage.write_json(dr.HOLD_CONFIG_KEY, {**ON, "pass_kinds": ["shelf-audit"]})
    _red(49)
    assert not dr.gate_hold(SITE, "seo", subject_tag="shelf-audit", storage=storage, now=NOW).hold
    assert dr.gate_hold(SITE, "seo", subject_tag="growth", storage=storage, now=NOW).hold


def test_no_hold_while_green_or_red_for_less_than_red_for_h(storage, gate_dir):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    assert not dr.gate_hold(SITE, "seo", storage=storage, now=NOW).hold    # no state yet
    _red(2)
    d = dr.gate_hold(SITE, "seo", storage=storage, now=NOW)
    assert not d.hold and d.red_for_h == 2.0
    st = dr.GateState.load(SITE)
    st.record_green("abc", repaired=False)
    st.save()
    assert not dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(days=3)).hold


def test_one_probe_per_interval_so_the_gate_is_re_tested(storage, gate_dir):
    # Nothing else re-runs a held site's gate: without a probe a red that
    # an operator fixed (say, the test env) would never clear.
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red(49, last_run_h_ago=5)
    first = dr.gate_hold(SITE, "seo", storage=storage, now=NOW)
    assert not first.hold and first.probe and "re-tested" in first.reason
    second = dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(minutes=5))
    assert second.hold and not second.probe                     # the probe was granted once
    later = dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(hours=4, minutes=1))
    assert later.probe


def test_a_probe_that_got_no_gate_verdict_is_granted_again_after_the_grace(storage, gate_dir):
    # The probe batch made no commit, failed, or never reached the deployer:
    # without the grace the gate would not be re-tested for probe_every_h.
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    st = _red(49, last_run_h_ago=5)
    assert dr.gate_hold(SITE, "seo", storage=storage, now=NOW, state=st).probe
    assert dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(hours=2, minutes=59), state=st).hold
    again = dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(hours=3, minutes=1), state=st)
    assert again.probe and not again.hold


def test_a_probe_answered_by_a_gate_verdict_waits_probe_every_h(storage, gate_dir):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    st = _red(49, last_run_h_ago=5)
    assert dr.gate_hold(SITE, "seo", storage=storage, now=NOW, state=st).probe
    # The probe's gate run ends red an hour later.
    st.record_red(dr.Classification("code", "5 test(s) failed", 1, [dr.TestFailure("t", "a.ts")]),
                  "sig1", attempts=0, exhausted=False, cfg=dr.DEFAULTS, now=NOW + timedelta(hours=1))
    assert dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(hours=3, minutes=30), state=st).hold
    assert dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(hours=5, minutes=1), state=st).probe


def test_release_probe_gives_back_only_the_probe_it_names(storage, gate_dir):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    st = _red(49, last_run_h_ago=5)
    d = dr.gate_hold(SITE, "seo", storage=storage, now=NOW, state=st)
    assert d.probe and d.probe_at == NOW.isoformat()
    assert not dr.release_probe(SITE, granted_at="2026-11-11T09:00:00+00:00", storage=storage)
    assert not dr.release_probe(SITE, granted_at="", storage=storage)
    assert dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(minutes=1), state=st).hold
    assert dr.release_probe(SITE, granted_at=d.probe_at, storage=storage)
    doc = storage.read_json(f"{dr.HOLD_STATE_PREFIX}{SITE}.json")
    assert "last_probe_at" not in doc and doc["probes_released"] == 1
    assert dr.gate_hold(SITE, "seo", storage=storage, now=NOW + timedelta(minutes=2), state=st).probe


def test_status_for_prompts_never_spends_the_probe(storage, gate_dir):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red(49, last_run_h_ago=5)
    s = dr.gate_hold_status(SITE, storage=storage, now=NOW)
    assert s["probe"] and not s["hold"] and s["message"] == ""
    assert storage.read_json(f"{dr.HOLD_STATE_PREFIX}{SITE}.json") is None
    _red(49, last_run_h_ago=0.5)
    s = dr.gate_hold_status(SITE, storage=storage, now=NOW)
    assert s["hold"] and s["message"].startswith("DEPLOY GATE: the site-a deploy gate has been red")


def test_by_site_overrides_and_the_env_kill_switch(storage, gate_dir, monkeypatch):
    storage.write_json(dr.HOLD_CONFIG_KEY, {**ON, "by_site": {SITE: {"enabled": False}, "site-b": {"red_for_h": 100}}})
    _red(49)
    _red(49, site="site-b")
    _red(49, site="site-c")
    assert not dr.gate_hold(SITE, "seo", storage=storage, now=NOW).hold
    assert not dr.gate_hold("site-b", "seo", storage=storage, now=NOW).hold
    assert dr.gate_hold("site-c", "seo", storage=storage, now=NOW).hold
    monkeypatch.setenv("DEPLOY_GATE_HOLD_DISABLED", "1")
    assert not dr.gate_hold("site-c", "seo", storage=storage, now=NOW).hold


def test_broken_storage_or_state_means_no_hold(gate_dir):
    class Broken:
        def read_json(self, key):
            raise RuntimeError("blob archived")

        def write_json(self, key, doc):
            raise RuntimeError("blob archived")
    _red(49)
    assert not dr.gate_hold(SITE, "seo", storage=Broken(), now=NOW).hold
    (gate_dir / f"{SITE}.json").write_text("{not json")
    assert not dr.gate_hold(SITE, "seo", storage=Broken(), now=NOW).hold


# ---------------------------------------------------------------------------
# dispatch_kind_for mirrors agents/implementer/run.sh
# ---------------------------------------------------------------------------

def _run_dir(tmp_path, doc, name="rd"):
    d = tmp_path / name
    d.mkdir()
    (d / "recommendations.json").write_text(json.dumps(doc))
    return d


@pytest.mark.parametrize("doc,kind", [
    ({"agent_id": "x-head-to-head-agent", "recommendations": [{"id": "r1"}]}, "h2h"),
    ({"agent": "x-catalog-audit-agent", "recommendations": [{"id": "r1"}]}, "catalog-audit"),
    ({"agent_id": "x-article-proposal-agent", "recommendations": []}, "article-author"),
    ({"recommendations": [{"type": "seo-fix"}, {"type": "article-author-proposal"}]}, "article-author"),
    ({"recommendations": [{"dispatch_kind": "article-refresh"}, {"type": "article-refresh"}]}, "article-refresh"),
    ({"recommendations": [{"type": "article-refresh"}, {"type": "seo-fix"}]}, "seo"),
    ({"run_ts": "x", "mode": "growth", "recommendations": [{"id": "growth-01", "tier": "lever"}]}, "seo"),
    ([{"id": "r1"}], "seo"),
])
def test_dispatch_kind_for_mirrors_run_sh(tmp_path, doc, kind):
    assert dispatch.dispatch_kind_for(str(_run_dir(tmp_path, doc))) == kind


def test_dispatch_kind_for_without_recommendations_is_seo(tmp_path):
    assert dispatch.dispatch_kind_for(str(tmp_path)) == "seo"
    assert dispatch.dispatch_kind_for("") == "seo"


def test_run_sh_and_dispatch_agree_on_producer_suffixes():
    text = (Path(__file__).resolve().parents[2] / "agents" / "implementer" / "run.sh").read_text()
    for suffix, kind in dispatch._PRODUCER_KIND_SUFFIXES:
        assert f'*{suffix})' in text and f'DISPATCH_KIND="{kind}"' in text, suffix


# ---------------------------------------------------------------------------
# dispatch_now / gated_dispatch_now
# ---------------------------------------------------------------------------

@pytest.fixture
def no_spawn(monkeypatch, tmp_path):
    """dispatch_now with the spawn, script lookup and site lock stubbed."""
    spawned = []
    script = tmp_path / "agent.py"
    script.write_text("")

    def spawn(**kw):
        spawned.append(kw)
        return dispatch.DispatchHandle(pid=1, unit="u", log_path=tmp_path, site=kw["site"],
                                       agent_id=kw["agent_id"], rec_ids=kw["rec_ids"],
                                       request_id=kw["request_id"])

    @contextmanager
    def no_lock(site, timeout_s=0):
        yield None
    monkeypatch.setattr(dispatch, "_spawn_implementer", spawn)
    monkeypatch.setattr(dispatch, "_resolve_implementer_script", lambda: str(script))
    monkeypatch.setattr(dispatch, "site_dispatch_lock", no_lock)
    monkeypatch.setattr(dispatch, "LOG_DIR", tmp_path)
    return spawned


GROWTH = {"run_ts": "20261111T151200Z", "mode": "growth",
          "recommendations": [{"id": "growth-20261111T151200Z-01", "tier": "lever"}]}


def test_dispatch_now_holds_app_code_without_spawning_or_queueing(storage, gate_dir, no_spawn, tmp_path):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49)
    rd = _run_dir(tmp_path, GROWTH)
    with pytest.raises(dispatch.DispatchHeld) as e:
        dispatch.dispatch_now(agent_id="site-a-user-growth-strategist", run_dir=str(rd),
                              rec_ids=["growth-20261111T151200Z-01"], site=SITE, subject_tag="growth")
    assert isinstance(e.value, dispatch.DispatchError) and e.value.decision.hold
    assert no_spawn == []
    assert storage.list_prefix("agents/responder-agent/auto-queue/") == []
    skip, why = defer_backoff.should_skip("growth-20261111T151200Z-01", "site-a-user-growth-strategist",
                                          storage=storage)
    assert skip and "deploy-gate-hold" in why
    log = storage.read_json(f"{dr.HOLD_STATE_PREFIX}{SITE}.json")
    assert log["held_total"] == 1 and log["held"][0]["kind"] == "seo"
    assert log["parked"] == {"site-a-user-growth-strategist": ["growth-20261111T151200Z-01"]}
    entry = storage.read_json("framework/defer-backoff/site-a-user-growth-strategist.json")["recs"][
        "growth-20261111T151200Z-01"]
    assert entry["count"] == 0 and entry["held_until"] == entry["next_retry_at"]     # a hold, not a defer


SEO_RUN = {"agent_id": "site-a-seo-opportunity-agent", "recommendations": [{"id": "rec-001"}]}


def _entry(storage, agent, rid):
    return (storage.read_json(f"framework/defer-backoff/{agent}.json") or {"recs": {}})["recs"].get(rid)


def test_holds_never_climb_the_backoff_and_lift_once_the_gate_is_green(storage, gate_dir, no_spawn, tmp_path):
    # SEO and PI reuse rec ids (rec-001 in every run). Held recs used to climb
    # 1m..12h and never reset, so a fresh run's rec-001 was still skipped half
    # a day after the gate went green, and the next red started at 12h.
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49)
    agent = "site-a-seo-opportunity-agent"
    rd = _run_dir(tmp_path, SEO_RUN)
    for _ in range(8):
        with pytest.raises(dispatch.DispatchHeld):
            dispatch.dispatch_now(agent_id=agent, run_dir=str(rd), rec_ids=["rec-001"], site=SITE,
                                  subject_tag="seo")
    e = _entry(storage, agent, "rec-001")
    left = datetime.fromisoformat(e["next_retry_at"]) - datetime.now(timezone.utc)
    assert e["count"] == 0 and timedelta(minutes=55) < left <= timedelta(hours=1)
    st = dr.GateState.load(SITE)
    st.record_green("abc", repaired=False)
    st.save()
    # Any later dispatch for the site sees the green gate and lifts the holds.
    other = _run_dir(tmp_path, {"agent_id": "site-a-progressive-improvement-agent",
                                "recommendations": [{"id": "rec-004"}]}, "pi")
    dispatch.dispatch_now(agent_id="site-a-progressive-improvement-agent", run_dir=str(other),
                          rec_ids=["rec-004"], site=SITE, subject_tag="progressive-improvement")
    assert _entry(storage, agent, "rec-001") is None
    assert defer_backoff.should_skip("rec-001", agent, storage=storage) == (False, "")
    assert storage.read_json(f"{dr.HOLD_STATE_PREFIX}{SITE}.json")["parked"] == {}
    # The next red starts from a one-hour hold again, not from 12 h.
    _red_now(49)
    with pytest.raises(dispatch.DispatchHeld):
        dispatch.dispatch_now(agent_id=agent, run_dir=str(rd), rec_ids=["rec-001"], site=SITE, subject_tag="seo")
    left = datetime.fromisoformat(_entry(storage, agent, "rec-001")["next_retry_at"]) - datetime.now(timezone.utc)
    assert left <= timedelta(hours=1)


def test_a_hold_keeps_a_longer_real_defer_and_release_restores_it(storage):
    agent, now = "site-a-article-proposal-agent", NOW
    for i in range(4):                                            # 4 real defers: a 2 h cooldown
        defer_backoff.record_defer("rec-001", agent, "required-model-unavailable", storage=storage,
                                   now=now + timedelta(seconds=i))
    real = _entry(storage, agent, "rec-001")["next_retry_at"]
    defer_backoff.hold("rec-001", agent, now + timedelta(hours=1), "deploy-gate-hold: red", storage=storage,
                       now=now)
    e = _entry(storage, agent, "rec-001")
    assert e["next_retry_at"] == real and e["count"] == 4         # the longer cooldown wins
    defer_backoff.hold("rec-002", agent, now + timedelta(hours=1), "deploy-gate-hold: red", storage=storage,
                       now=now)
    assert defer_backoff.should_skip("rec-002", agent, storage=storage, now=now)[0]
    assert defer_backoff.release_holds(agent, ["rec-001", "rec-002"], storage=storage) == 2
    e = _entry(storage, agent, "rec-001")
    assert e["count"] == 4 and e["next_retry_at"] == real and "held_until" not in e
    assert _entry(storage, agent, "rec-002") is None
    assert defer_backoff.release_holds(agent, None, storage=storage) == 0


def test_dispatch_now_lets_a_db_only_growth_batch_through(storage, gate_dir, no_spawn, tmp_path):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49)
    doc = {**GROWTH, "recommendations": [{"id": "growth-20261111T151200Z-01", "tier": "lever", "db_only": True},
                                         {"id": "growth-20261111T151200Z-02", "tier": "lever"}]}
    rd = _run_dir(tmp_path, doc)
    h = dispatch.dispatch_now(agent_id="site-a-user-growth-strategist", run_dir=str(rd),
                              rec_ids=["growth-20261111T151200Z-01"], site=SITE, subject_tag="growth")
    assert h.pid == 1 and len(no_spawn) == 1 and not no_spawn[0].get("extra_env")
    with pytest.raises(dispatch.DispatchHeld):                    # the unmarked rec of the same run
        dispatch.dispatch_now(agent_id="site-a-user-growth-strategist", run_dir=str(rd),
                              rec_ids=["growth-20261111T151200Z-02"], site=SITE, subject_tag="growth")


def test_dispatch_now_marks_the_probe_batch_for_the_implementer(storage, gate_dir, no_spawn, tmp_path):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49, last_run_h_ago=5)
    rd = _run_dir(tmp_path, SEO_RUN)
    dispatch.dispatch_now(agent_id="site-a-seo-opportunity-agent", run_dir=str(rd), rec_ids=["rec-001"],
                          site=SITE, subject_tag="seo")
    at = storage.read_json(f"{dr.HOLD_STATE_PREFIX}{SITE}.json")["last_probe_at"]
    assert no_spawn[0]["extra_env"] == {"DEPLOY_GATE_PROBE": "1", "DEPLOY_GATE_PROBE_AT": at}


@pytest.mark.parametrize("failure", [dispatch.DispatchError("implementer script not found"),
                                     TimeoutError("could not acquire dispatch-site-a")])
def test_dispatch_now_gives_the_probe_back_when_the_dispatch_fails(storage, gate_dir, no_spawn, tmp_path,
                                                                   monkeypatch, failure):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49, last_run_h_ago=5)

    def fail(**kw):
        raise failure
    monkeypatch.setattr(dispatch, "_spawn_implementer", fail)
    rd = _run_dir(tmp_path, SEO_RUN)
    with pytest.raises(type(failure)):
        dispatch.dispatch_now(agent_id="site-a-seo-opportunity-agent", run_dir=str(rd), rec_ids=["rec-001"],
                              site=SITE, subject_tag="seo", fallback_to_queue=False, notify_on_failure=False)
    doc = storage.read_json(f"{dr.HOLD_STATE_PREFIX}{SITE}.json")
    assert "last_probe_at" not in doc and doc["probes_released"] == 1
    # The next app-code dispatch gets the probe at once.
    d = dr.gate_hold(SITE, "seo", storage=storage)
    assert d.probe and not d.hold


def test_the_deployer_lifts_holds_when_the_gate_turns_green(storage, gate_dir):
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "agents" / "deployer" / "deployer.py"
    spec = importlib.util.spec_from_file_location("deployer_hold_test", path)
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    agent = "site-a-seo-opportunity-agent"
    defer_backoff.hold("rec-001", agent, datetime.now(timezone.utc) + timedelta(hours=1), "deploy-gate-hold: red",
                       storage=storage)
    dr.record_hold(SITE, agent_id=agent, rec_ids=["rec-001"], kind="seo", reason="red", storage=storage)
    d._release_gate_holds(SITE)
    assert _entry(storage, agent, "rec-001") is None
    d._release_gate_holds(SITE)                                   # nothing parked: a no-op


def test_dispatch_now_lets_data_only_work_and_forced_dispatches_through(storage, gate_dir, no_spawn, tmp_path):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49)
    cat = _run_dir(tmp_path, {"agent_id": "site-a-catalog-audit-agent",
                              "recommendations": [{"id": "rec-001"}]}, "cat")
    h = dispatch.dispatch_now(agent_id="site-a-catalog-audit-agent", run_dir=str(cat),
                              rec_ids=["rec-001"], site=SITE, subject_tag="catalog-audit")
    assert h.pid == 1
    rd = _run_dir(tmp_path, GROWTH)
    h = dispatch.dispatch_now(agent_id="site-a-user-growth-strategist", run_dir=str(rd),
                              rec_ids=["growth-20261111T151200Z-01"], site=SITE, subject_tag="growth",
                              honor_gate_hold=False)
    assert h.pid == 1 and len(no_spawn) == 2


def test_dispatch_now_is_unchanged_when_the_hold_is_off(storage, gate_dir, no_spawn, tmp_path):
    _red_now(49)
    rd = _run_dir(tmp_path, GROWTH)
    h = dispatch.dispatch_now(agent_id="site-a-user-growth-strategist", run_dir=str(rd),
                              rec_ids=["growth-20261111T151200Z-01"], site=SITE, subject_tag="growth")
    assert h.pid == 1 and len(no_spawn) == 1
    assert storage.read_json("framework/defer-backoff/site-a-user-growth-strategist.json") is None


def test_gated_dispatch_now_returns_none_and_logs_the_hold(storage, gate_dir, no_spawn, tmp_path):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49)
    decisions = []

    class Agent:
        def decide(self, kind, msg, evidence=None):
            decisions.append((kind, msg, evidence))
    rd = _run_dir(tmp_path, GROWTH)
    out = dispatch.gated_dispatch_now(agent=Agent(), agent_id="site-a-user-growth-strategist",
                                      run_dir=str(rd), rec_ids=["growth-20261111T151200Z-01"],
                                      site=SITE, subject_tag="growth")
    assert out is None and no_spawn == []
    (kind, msg, ev), = decisions
    assert kind == "observation" and msg.startswith("deploy-gate hold: 1 rec(s) not dispatched")
    assert ev["gate"] == "deploy-gate-hold" and ev["hold"] is True


def test_a_failing_hold_check_dispatches_as_before(storage, gate_dir, no_spawn, tmp_path, monkeypatch):
    storage.write_json(dr.HOLD_CONFIG_KEY, ON)
    _red_now(49)

    def boom(*a, **k):
        raise RuntimeError("bug in the hold")
    monkeypatch.setattr(dr, "gate_hold", boom)
    rd = _run_dir(tmp_path, GROWTH)
    h = dispatch.dispatch_now(agent_id="site-a-user-growth-strategist", run_dir=str(rd),
                              rec_ids=["growth-20261111T151200Z-01"], site=SITE, subject_tag="growth")
    assert h.pid == 1


def test_site_lock_bypass_reads_the_shared_kind_list(storage, gate_dir, no_spawn, tmp_path, monkeypatch):
    locked = []

    @contextmanager
    def lock(site, timeout_s=0):
        locked.append(site)
        yield None
    monkeypatch.setattr(dispatch, "site_dispatch_lock", lock)
    rd = _run_dir(tmp_path, {"recommendations": [{"id": "r1"}]})
    dispatch.dispatch_now(agent_id="a", run_dir=str(rd), rec_ids=["r1"], site=SITE, subject_tag="article-refresh")
    dispatch.dispatch_now(agent_id="a", run_dir=str(rd), rec_ids=["r1"], site=SITE, subject_tag="seo")
    assert locked == [SITE]
    monkeypatch.setenv("DATA_ONLY_KINDS", "seo")
    dispatch.dispatch_now(agent_id="a", run_dir=str(rd), rec_ids=["r1"], site=SITE, subject_tag="seo")
    assert locked == [SITE]
