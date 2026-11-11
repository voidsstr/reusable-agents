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
    assert d.hold and not d.probe and d.red_for_h == 49.0
    assert "red for 49h" in d.reason and "prefer DB-only levers" in d.reason
    assert "5 test(s) failed" in d.reason


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
