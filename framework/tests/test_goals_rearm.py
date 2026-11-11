"""Re-arming goals: re-open, ratchet and seed-driven re-activation.

Every Amazon-click goal on both sites sat 'accomplished' against a stale
target (30 / 200 / 900) while the value kept moving. The SEO audit only shows
'active' goals, so none of them reached a prompt (2026-11-11 audit, A10).
These tests pin the contract in framework/core/goals.py:

- a seed that moves an accomplished goal's target past its current value
  re-activates it, but never accomplishes or ratchets one;
- metric.ratchet raises the target past the value instead of accomplishing,
  and a ratcheted target survives routine re-seeds;
- metric.reopen / ratchet / config reopen_on_regression re-open a goal that
  falls back, with re-open off by default (behaviour-preserving);
- metric_helper.record / record_many follow the same rules.
"""
from __future__ import annotations

import importlib.util as iu
from pathlib import Path

import jsonschema
import pytest

from framework.core import goals as g
from framework.core import metric_helper as mh

_ROOT = Path(__file__).resolve().parents[2]


def _goal(gid, *, current=0, target=10, direction="increase", **metric_kw):
    m = {"name": "m", "current": current, "target": target, "direction": direction}
    m.update(metric_kw)
    return {"id": gid, "title": gid, "metric": m}


def _by_id(doc_or_goals):
    goals = doc_or_goals["goals"] if isinstance(doc_or_goals, dict) else doc_or_goals
    return {x["id"]: x for x in goals}


def _accomplish(storage, agent, gid, value):
    """Drive a goal to 'accomplished' the normal way (a measurement)."""
    doc = g.record_goal_progress(agent, gid, value, storage=storage)
    assert _by_id(doc)[gid]["status"] == "accomplished"


# ---------------------------------------------------------------------------
# Seed-driven re-activation (init_goals / PUT /goals)
# ---------------------------------------------------------------------------

def test_seed_raising_target_past_current_rearms_accomplished_goal(storage):
    g.init_goals("a", [_goal("goal-clicks", target=30)], storage=storage)
    _accomplish(storage, "a", "goal-clicks", 1345)

    doc = g.init_goals("a", [_goal("goal-clicks", target=3000)], storage=storage)
    got = _by_id(doc)["goal-clicks"]
    assert got["status"] == "active"
    assert got["accomplished_at"] is None
    assert got["reopened_at"] and "3000" in got["reopened_reason"]
    assert got["metric"]["target"] == 3000
    assert got["metric"]["current"] == 1345               # measurement kept
    assert len(got["progress_history"]) == 1               # history kept


def test_seed_does_not_rearm_when_current_still_meets_new_target(storage):
    """A stale/inflated current that still meets the new target leaves the
    goal accomplished until a fresh measurement says otherwise; the seed
    never ratchets off a stale value."""
    g.init_goals("a", [_goal("goal-clicks", target=200)], storage=storage)
    _accomplish(storage, "a", "goal-clicks", 1549)
    doc = g.init_goals("a", [_goal("goal-clicks", target=450, reopen=True)], storage=storage)
    got = _by_id(doc)["goal-clicks"]
    assert got["status"] == "accomplished"
    assert got["metric"]["target"] == 450                  # not ratcheted / moved

    doc = g.record_goal_progress("a", "goal-clicks", 326, storage=storage)
    got = _by_id(doc)["goal-clicks"]
    assert got["status"] == "active"                       # reopen on the fresh value
    assert got["reopened_at"]


def test_routine_reseed_never_flips_status(storage):
    """site-goals-tracker re-seeds every run with status 'active'. An
    unchanged seed must not re-open a regressed goal that has no reopen
    rule: that would change behaviour for every goal fleet-wide."""
    seed = _goal("goal-crawled-not-indexed", target=0, direction="decrease")
    seed["status"] = "active"
    g.init_goals("a", [seed], storage=storage)
    _accomplish(storage, "a", "goal-crawled-not-indexed", 0)
    g.record_goal_progress("a", "goal-crawled-not-indexed", 618, storage=storage)
    doc = g.init_goals("a", [seed], storage=storage)
    assert _by_id(doc)["goal-crawled-not-indexed"]["status"] == "accomplished"


def test_seed_adding_reopen_rule_rearms_a_regressed_goal(storage):
    g.init_goals("a", [_goal("goal-ai-referrals", target=75)], storage=storage)
    _accomplish(storage, "a", "goal-ai-referrals", 80)
    g.record_goal_progress("a", "goal-ai-referrals", 69, storage=storage)
    assert _by_id(g.read_active_goals("a", storage=storage))["goal-ai-referrals"]["status"] == "accomplished"
    doc = g.init_goals("a", [_goal("goal-ai-referrals", target=75, reopen=True)], storage=storage)
    assert _by_id(doc)["goal-ai-referrals"]["status"] == "active"


def test_seed_still_cannot_force_accomplished_or_resurrect_retired(storage):
    g.init_goals("a", [_goal("goal-x", target=10)], storage=storage)
    doc = g.init_goals("a", [{"id": "goal-x", "status": "accomplished"}], storage=storage)
    assert _by_id(doc)["goal-x"]["status"] == "active"
    g.init_goals("a", [{"id": "goal-x", "status": "abandoned"}], storage=storage)
    doc = g.init_goals("a", [_goal("goal-x", target=99, ratchet={"factor": 1.25})],
                       storage=storage)
    assert _by_id(doc)["goal-x"]["status"] == "abandoned"


# ---------------------------------------------------------------------------
# Ratchet
# ---------------------------------------------------------------------------

def test_ratchet_raises_target_and_keeps_goal_active(storage):
    g.init_goals("a", [_goal("goal-clicks", target=100, ratchet={"factor": 1.25})],
                 storage=storage)
    doc = g.record_goal_progress("a", "goal-clicks", 120, run_ts="r1", storage=storage)
    got = _by_id(doc)["goal-clicks"]
    assert got["status"] == "active"
    assert got["metric"]["target"] == 150                  # ceil(120 x 1.25)
    assert got["ratchet_history"][-1] == {
        "ts": got["ratchet_history"][-1]["ts"], "value": 120,
        "from_target": 100, "to_target": 150}
    log = storage.read_jsonl("agents/a/goals/accomplished.jsonl")
    assert log[-1]["ratcheted"] is True and log[-1]["to_target"] == 150
    # below the new target: still active, nothing logged
    doc = g.record_goal_progress("a", "goal-clicks", 140, storage=storage)
    assert _by_id(doc)["goal-clicks"]["status"] == "active"
    assert len(storage.read_jsonl("agents/a/goals/accomplished.jsonl")) == 1


def test_ratchet_rearms_a_legacy_accomplished_goal_on_next_measurement(storage):
    g.init_goals("a", [_goal("goal-clicks", target=30)], storage=storage)
    _accomplish(storage, "a", "goal-clicks", 1352)
    # Adding a ratchet alone (target unchanged, current still meets it)
    # does not ratchet off the stored value...
    doc = g.init_goals("a", [_goal("goal-clicks", target=30, ratchet={"factor": 1.25})],
                       storage=storage)
    assert _by_id(doc)["goal-clicks"]["status"] == "accomplished"
    # ...the next fresh measurement does.
    doc = g.record_goal_progress("a", "goal-clicks", 1360, storage=storage)
    got = _by_id(doc)["goal-clicks"]
    assert got["status"] == "active" and got["metric"]["target"] == 1700


def test_routine_reseed_keeps_ratcheted_target_but_changed_seed_wins(storage):
    seed = _goal("goal-clicks", target=100, ratchet={"factor": 1.25})
    g.init_goals("a", [seed], storage=storage)
    assert _by_id(g.read_active_goals("a", storage=storage))["goal-clicks"]["metric"]["seed_target"] == 100
    g.record_goal_progress("a", "goal-clicks", 130, storage=storage)   # -> 163
    doc = g.init_goals("a", [seed], storage=storage)                     # routine
    assert _by_id(doc)["goal-clicks"]["metric"]["target"] == 163
    doc = g.init_goals("a", [_goal("goal-clicks", target=500, ratchet={"factor": 1.25})],
                       storage=storage)                                  # operator change
    m = _by_id(doc)["goal-clicks"]["metric"]
    assert m["target"] == 500 and m["seed_target"] == 500


def test_seed_null_removes_ratchet(storage):
    g.init_goals("a", [_goal("goal-clicks", target=100, ratchet={"factor": 1.25})],
                 storage=storage)
    doc = g.init_goals("a", [_goal("goal-clicks", target=100, ratchet=None)], storage=storage)
    m = _by_id(doc)["goal-clicks"]["metric"]
    assert "ratchet" not in m and "seed_target" not in m
    doc = g.record_goal_progress("a", "goal-clicks", 120, storage=storage)
    assert _by_id(doc)["goal-clicks"]["status"] == "accomplished"


def test_decrease_ratchet_and_floor(storage):
    g.init_goals("a", [_goal("goal-backlog", target=100, direction="decrease",
                             ratchet={"factor": 1.25})], storage=storage)
    doc = g.record_goal_progress("a", "goal-backlog", 80, storage=storage)
    got = _by_id(doc)["goal-backlog"]
    assert got["status"] == "active" and got["metric"]["target"] == 64
    # At 0 the target cannot move any lower: it is accomplished for real.
    g.init_goals("a", [_goal("goal-zero", target=0, direction="decrease",
                             ratchet={"factor": 1.25})], storage=storage)
    doc = g.record_goal_progress("a", "goal-zero", 0, storage=storage)
    assert _by_id(doc)["goal-zero"]["status"] == "accomplished"


def test_fractional_target_ratchet_keeps_decimals():
    assert g.ratcheted_target(0.025, 0.02, "increase", 1.25) == pytest.approx(0.03125)
    assert g.ratcheted_target(3001, 3000, "increase", 1.25) == 3752
    assert g.ratcheted_target(0, 0, "increase", 1.25) is None


def test_schema_rejects_a_ratchet_that_would_not_raise(storage):
    with pytest.raises(jsonschema.ValidationError):
        g.init_goals("a", [_goal("goal-clicks", ratchet={"factor": 1.0})], storage=storage)


# ---------------------------------------------------------------------------
# Re-open
# ---------------------------------------------------------------------------

def test_regression_without_reopen_stays_accomplished(storage):
    """Default behaviour is unchanged: no metric.reopen, no ratchet, no config."""
    g.init_goals("a", [_goal("goal-x", target=75)], storage=storage)
    _accomplish(storage, "a", "goal-x", 80)
    doc = g.record_goal_progress("a", "goal-x", 69, storage=storage)
    assert _by_id(doc)["goal-x"]["status"] == "accomplished"


def test_reopen_reopens_then_reaccomplishes(storage):
    g.init_goals("a", [_goal("goal-x", target=75, reopen=True)], storage=storage)
    _accomplish(storage, "a", "goal-x", 80)
    doc = g.record_goal_progress("a", "goal-x", 69, storage=storage)
    got = _by_id(doc)["goal-x"]
    assert got["status"] == "active" and got["accomplished_at"] is None
    assert "69" in got["reopened_reason"] and "75" in got["reopened_reason"]
    doc = g.record_goal_progress("a", "goal-x", 76, storage=storage)
    assert _by_id(doc)["goal-x"]["status"] == "accomplished"
    assert len(storage.read_jsonl("agents/a/goals/accomplished.jsonl")) == 2


def test_fleet_config_reopen_on_regression(storage):
    g.init_goals("a", [_goal("goal-x", target=75), _goal("goal-optout", target=75, reopen=False)],
                 storage=storage)
    g.init_goals("b", [_goal("goal-x", target=75)], storage=storage)
    for agent in ("a", "b"):
        _accomplish(storage, agent, "goal-x", 80)
    _accomplish(storage, "a", "goal-optout", 80)
    storage.write_json(g.CONFIG_KEY, {"schema_version": "1", "reopen_on_regression": False,
                                      "by_agent_id": {"a": {"reopen_on_regression": True}}})
    assert g.reopen_on_regression("a", storage=storage) is True
    assert g.reopen_on_regression("b", storage=storage) is False
    doc_a = g.record_goal_progress("a", "goal-x", 60, storage=storage)
    doc_b = g.record_goal_progress("b", "goal-x", 60, storage=storage)
    assert _by_id(doc_a)["goal-x"]["status"] == "active"
    assert _by_id(doc_b)["goal-x"]["status"] == "accomplished"
    doc_a = g.record_goal_progress("a", "goal-optout", 60, storage=storage)
    assert _by_id(doc_a)["goal-optout"]["status"] == "accomplished"   # per-goal wins
    storage.write_json(g.CONFIG_KEY, {"schema_version": "1", "reopen_on_regression": True})
    doc_b = g.record_goal_progress("b", "goal-x", 60, storage=storage)
    assert _by_id(doc_b)["goal-x"]["status"] == "active"


def test_abandoned_goal_never_ratchets_or_reopens(storage):
    g.init_goals("a", [_goal("goal-x", target=10, ratchet={"factor": 1.25})], storage=storage)
    g.init_goals("a", [{"id": "goal-x", "status": "abandoned"}], storage=storage)
    doc = g.record_goal_progress("a", "goal-x", 50, storage=storage)
    got = _by_id(doc)["goal-x"]
    assert got["status"] == "abandoned" and got["metric"]["target"] == 10


# ---------------------------------------------------------------------------
# metric_helper shares the rules
# ---------------------------------------------------------------------------

def test_metric_helper_ratchets_reopens_and_keeps_plain_goals_unchanged(storage):
    g.init_goals("a", [_goal("goal-ratchet", target=100, ratchet={"factor": 1.25}),
                       _goal("goal-reopen", target=75, reopen=True),
                       _goal("goal-plain", target=75)], storage=storage)
    mh.record_many("a", {"goal-ratchet": 120, "goal-reopen": 80, "goal-plain": 80},
                   storage=storage)
    by = _by_id(g.read_active_goals("a", storage=storage))
    assert by["goal-ratchet"]["status"] == "active" and by["goal-ratchet"]["metric"]["target"] == 150
    assert by["goal-reopen"]["status"] == "accomplished"
    assert by["goal-plain"]["status"] == "accomplished"
    mh.record_many("a", {"goal-reopen": 60, "goal-plain": 60}, storage=storage)
    mh.record("a", "goal-ratchet", 200, storage=storage)
    by = _by_id(g.read_active_goals("a", storage=storage))
    assert by["goal-reopen"]["status"] == "active"
    assert by["goal-plain"]["status"] == "accomplished"          # unchanged default
    assert by["goal-ratchet"]["metric"]["target"] == 250


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

def test_annotations_merge_and_survive_reseeds(storage):
    note = {"ts": "2026-11-11", "kind": "measurement-correction",
            "note": "measurement correction: Safari-17 fleet removed", "value": 326}
    seed = _goal("goal-clicks", target=450)
    g.init_goals("a", [dict(seed, annotations=[note])], storage=storage)
    g.init_goals("a", [dict(seed, annotations=[note])], storage=storage)   # no duplicate
    doc = g.init_goals("a", [seed], storage=storage)                        # kept
    assert _by_id(doc)["goal-clicks"]["annotations"] == [note]
    later = {"ts": "2026-12-01", "note": "Cyber Week read"}
    doc = g.init_goals("a", [dict(seed, annotations=[later])], storage=storage)
    assert [a["ts"] for a in _by_id(doc)["goal-clicks"]["annotations"]] == ["2026-11-11", "2026-12-01"]


# ---------------------------------------------------------------------------
# The prompt sees a re-armed goal (seo-opportunity llm_audit)
# ---------------------------------------------------------------------------

def _llm_audit():
    spec = iu.spec_from_file_location(
        "llm_audit_rearm_under_test",
        _ROOT / "agents" / "seo-opportunity-agent" / "lib" / "analyzer" / "llm_audit.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rearmed_amazon_goal_reaches_the_audit_prompt_first(storage):
    la = _llm_audit()
    amazon = _goal("goal-human-amazon-clicks-30d", target=30)
    amazon.update(title="Verified-human Amazon clicks / 30d",
                  directives=["audit the /product/* pages that earn verified clicks first"])
    g.init_goals("a", [_goal("goal-impressions-90d", target=1000), amazon], storage=storage)
    _accomplish(storage, "a", "goal-human-amazon-clicks-30d", 1352)
    assert "Amazon" not in la._format_goals(g.read_active_goals("a", storage=storage))

    amazon["metric"] = dict(amazon["metric"], target=3000, ratchet={"factor": 1.25})
    amazon["is_revenue_goal"] = True
    g.init_goals("a", [amazon], storage=storage)
    block = la._format_goals(g.read_active_goals("a", storage=storage))
    lines = block.splitlines()
    assert lines[1].startswith("- Verified-human Amazon clicks / 30d")   # revenue first
    assert "1352 -> 3000" in lines[1]
    assert "audit the /product/* pages" in block


# ---------------------------------------------------------------------------
# install/seed-default-goals.sh stays valid and carries the re-armed goals
# ---------------------------------------------------------------------------

def _seed_script_bodies() -> dict[str, dict]:
    import json
    import re
    text = (_ROOT / "install" / "seed-default-goals.sh").read_text()
    out = {}
    for m in re.finditer(r"^([A-Z_]+)='(\{\"goals\":\[.*?\]\})'\s*$", text, re.M | re.S):
        body = m.group(2).replace("'\\''", "'").replace("__TARGET__", "1")
        out[m.group(1)] = json.loads(body)
    return out


def test_seed_script_bodies_parse_and_validate(storage):
    bodies = _seed_script_bodies()
    assert {"SEO_GOALS", "AP_AI_GOALS", "AP_GROWTH_GOALS", "SP_SEO_GOALS",
            "SP_GROWTH_GOALS"} <= set(bodies)
    for name, body in bodies.items():
        g.init_goals(f"seed-check-{name.lower().replace('_', '-')}", body["goals"],
                     storage=storage)                       # schema-validated

    def goal(block, gid):
        return next(x for x in bodies[block]["goals"] if x["id"] == gid)
    for block in ("SP_SEO_GOALS", "SP_GROWTH_GOALS"):
        m = goal(block, "goal-human-amazon-clicks-30d")["metric"]
        assert m["target"] == 3000 and m["ratchet"] == {"factor": 1.25}
    for block in ("AP_AI_GOALS", "AP_GROWTH_GOALS"):
        x = goal(block, "goal-amazon-clicks-30d")
        assert x["metric"]["target"] == 450 and x["metric"]["reopen"] is True
        assert "Safari-17 fleet removed" in x["annotations"][0]["note"]


def test_tracker_and_seed_script_share_the_fleet_note(tracker):
    """Same (ts, note) everywhere, so applying the storage patch and the
    tracker's own re-seed never leave two copies of the annotation."""
    ap = _seed_script_bodies()["AP_AI_GOALS"]
    note = next(x for x in ap["goals"] if x["id"] == "goal-amazon-clicks-30d")["annotations"][0]
    assert note["note"] == tracker.SAFARI_FLEET_NOTE and note["ts"] == "2026-11-11"


# ---------------------------------------------------------------------------
# site-goals-tracker definitions
# ---------------------------------------------------------------------------

@pytest.fixture()
def tracker(monkeypatch):
    spec = iu.spec_from_file_location("site_goals_tracker_rearm_under_test",
                                      _ROOT / "agents" / "site-goals-tracker" / "agent.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tracker_specpicks_counts_beacon_rows_only(tracker):
    ev = tracker.SITE_PROFILES["specpicks"]["human_clicks"]["events"]
    assert ev["amazon-clicks"] == {"target": "amazon", "via": "beacon"}
    assert ev["ebay-clicks"] == {"target": "ebay", "via": "beacon"}


def test_tracker_rearms_stale_amazon_goals(storage, tracker):
    # The 2026-11-11 state: both sites' Amazon goals accomplished on stale targets.
    sp, ap = "specpicks-site-goals-tracker", "aisleprompt-site-goals-tracker"
    for agent, value, target in ((sp, 1379, 900), (ap, 1549, 200)):
        g.init_goals(agent, [_goal("goal-amazon-clicks-30d", target=target)], storage=storage)
        mh.record_many(agent, {"goal-amazon-clicks-30d": value}, storage=storage)
        assert _by_id(g.read_active_goals(agent, storage=storage))["goal-amazon-clicks-30d"]["status"] == "accomplished"

    tracker.write_goal_definitions(tracker.SITE_PROFILES["specpicks"], sp)
    tracker.write_goal_definitions(tracker.SITE_PROFILES["aisleprompt"], ap)
    sp_goal = _by_id(g.read_active_goals(sp, storage=storage))["goal-amazon-clicks-30d"]
    assert sp_goal["status"] == "active"                     # 1379 < 3000
    assert sp_goal["metric"]["target"] == 3000 and sp_goal["metric"]["ratchet"] == {"factor": 1.25}
    ap_goal = _by_id(g.read_active_goals(ap, storage=storage))["goal-amazon-clicks-30d"]
    assert ap_goal["status"] == "accomplished"               # stale 1549 still >= 450
    assert ap_goal["metric"]["target"] == 450 and ap_goal["metric"]["reopen"] is True
    assert "Safari-17 fleet removed" in ap_goal["annotations"][0]["note"]

    # The next run measures the filtered value (A05): AislePrompt re-opens.
    mh.record_many(ap, {"goal-amazon-clicks-30d": 326, "goal-amazon-clicks-7d": 48},
                   storage=storage)
    assert _by_id(g.read_active_goals(ap, storage=storage))["goal-amazon-clicks-30d"]["status"] == "active"

    # Re-running the definitions (every run) keeps a ratcheted target.
    mh.record_many(sp, {"goal-amazon-clicks-30d": 3100}, storage=storage)
    tracker.write_goal_definitions(tracker.SITE_PROFILES["specpicks"], sp)
    sp_goal = _by_id(g.read_active_goals(sp, storage=storage))["goal-amazon-clicks-30d"]
    assert sp_goal["status"] == "active" and sp_goal["metric"]["target"] == 3875
