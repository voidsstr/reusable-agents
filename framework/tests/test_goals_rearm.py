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
def tracker(monkeypatch, tmp_path):
    # write_goal_definitions reads the site's site.yaml (site_goals block);
    # point it at a missing file so the live site repos never leak in.
    monkeypatch.setenv("SITE_GOALS_SITE_YAML", str(tmp_path / "no-site.yaml"))
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


def _captured_seeds(tracker, monkeypatch, profile, site_yaml=None):
    """The goal list write_goal_definitions hands to init_goals."""
    seen = {}
    monkeypatch.setattr(tracker.goals_mod, "init_goals",
                        lambda agent_id, goals, storage=None: seen.setdefault("goals", goals))
    tracker.write_goal_definitions(profile, profile["agent_id"], site_yaml=site_yaml)
    return _by_id(seen["goals"])


def test_tracker_rules_and_targets_come_only_from_goal_overrides(tracker, monkeypatch):
    """No site branching for the re-arm rules: strip a profile's
    goal_overrides and no seed carries a ratchet, reopen or annotation."""
    for site in ("specpicks", "aisleprompt"):
        bare = {k: v for k, v in tracker.SITE_PROFILES[site].items() if k != "goal_overrides"}
        for gid, x in _captured_seeds(tracker, monkeypatch, bare, site_yaml={}).items():
            assert not {"ratchet", "reopen"} & set(x["metric"]), (site, gid)
            assert "annotations" not in x, (site, gid)
    sp = _captured_seeds(tracker, monkeypatch, tracker.SITE_PROFILES["specpicks"], site_yaml={})
    assert sp["goal-amazon-clicks-30d"]["metric"]["target"] == 3000
    assert sp["goal-amazon-clicks-30d"]["metric"]["ratchet"] == {"factor": 1.25}
    assert sp["goal-total-conversions-30d"]["metric"]["target"] == 3200
    ap = _captured_seeds(tracker, monkeypatch, tracker.SITE_PROFILES["aisleprompt"], site_yaml={})
    assert ap["goal-amazon-clicks-30d"]["metric"]["target"] == 450
    assert all(ap[gid]["metric"]["reopen"] is True for gid in
               ("goal-amazon-clicks-30d", "goal-amazon-clicks-7d", "goal-total-conversions-30d"))
    assert ap["goal-amazon-clicks-7d"]["annotations"][0]["value"] == 48


def test_tracker_site_yaml_goal_overrides_win(tracker, monkeypatch, tmp_path, storage):
    """site.yaml site_goals.goal_overrides is merged over the profile per key;
    junk is skipped, an unquoted YAML date becomes an ISO string, and a null
    ratchet removes the built-in one from the stored goal."""
    import yaml
    path = tmp_path / "site.yaml"
    path.write_text(
        "site: {id: specpicks}\n"
        "site_goals:\n"
        "  goal_overrides:\n"
        "    goal-amazon-clicks-30d:\n"
        "      target: 4000\n"
        "      ratchet: null\n"
        "      reopen: true\n"
        "      bogus: 1\n"
        "      annotations:\n"
        "        - {ts: 2026-12-01, note: Cyber Week read, value: 3100}\n"
        "        - {note: missing ts}\n"
        "    goal-total-conversions-30d: {ratchet: {factor: 1.0}}\n"
        "    goal-no-such-goal: {target: 1}\n")
    monkeypatch.setenv("SITE_GOALS_SITE_YAML", str(path))
    profile = tracker.SITE_PROFILES["specpicks"]
    y = tracker.load_site_yaml(profile)
    seeds = _captured_seeds(tracker, monkeypatch, profile, site_yaml=y)
    m = seeds["goal-amazon-clicks-30d"]["metric"]
    assert m["target"] == 4000 and m["ratchet"] is None and m["reopen"] is True
    assert "bogus" not in m
    assert seeds["goal-amazon-clicks-30d"]["annotations"] == [
        {"ts": "2026-12-01", "note": "Cyber Week read", "value": 3100}]
    # a ratchet that would not raise is dropped, the built-in one stays
    assert seeds["goal-total-conversions-30d"]["metric"]["ratchet"] == {"factor": 1.25}
    assert "goal-no-such-goal" not in seeds
    assert yaml.safe_load(path.read_text())["site_goals"]   # the file itself is untouched

    # Through the real init_goals: the stored ratchet is removed.
    monkeypatch.undo()
    monkeypatch.setenv("SITE_GOALS_SITE_YAML", str(tmp_path / "no-site.yaml"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("AGENT_STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    from framework.core.storage import reset_default_backend
    reset_default_backend()
    agent = profile["agent_id"]
    tracker.write_goal_definitions(profile, agent, site_yaml={})
    assert _by_id(g.read_active_goals(agent, storage=storage))["goal-amazon-clicks-30d"]["metric"]["ratchet"]
    tracker.write_goal_definitions(profile, agent, site_yaml=y)
    stored = _by_id(g.read_active_goals(agent, storage=storage))["goal-amazon-clicks-30d"]
    assert "ratchet" not in stored["metric"] and stored["metric"]["target"] == 4000


def test_site_config_schema_accepts_site_goals_block():
    import json
    schema = json.loads((_ROOT / "shared" / "schemas" / "site-config.schema.json").read_text())
    block = schema["properties"]["site_goals"]
    jsonschema.validate({"goal_overrides": {"goal-amazon-clicks-30d": {
        "target": 450, "reopen": True, "ratchet": None,
        "annotations": [{"ts": "2026-11-11", "note": "x", "kind": "measurement-correction",
                         "value": 326}]}}}, block)
    for bad in ({"goal_overrides": {"goal-x": {"ratchet": {"factor": 1.0}}}},
                {"goal_overrides": {"goal-x": {"targett": 5}}},
                {"goal_overrides": {"goal-x": {"annotations": [{"note": "no ts"}]}}},
                {"overrides": {}}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, block)


# ---------------------------------------------------------------------------
# Review fixes: opt-out seeds, malformed config, NaN, mixed-version imports
# ---------------------------------------------------------------------------

def _regressed_accomplished_goal(storage, agent="a", gid="goal-x"):
    """Accomplished at 80 against 75, then fell back to 60 with no rule."""
    g.init_goals(agent, [_goal(gid, target=75)], storage=storage)
    _accomplish(storage, agent, gid, 80)
    g.record_goal_progress(agent, gid, 60, storage=storage)
    assert _by_id(g.read_active_goals(agent, storage=storage))[gid]["status"] == "accomplished"


def test_seed_opting_out_never_rearms(storage):
    _regressed_accomplished_goal(storage)
    for seed_metric in ({"reopen": False}, {"ratchet": None}, {"reopen": None}):
        doc = g.init_goals("a", [_goal("goal-x", target=75, **seed_metric)], storage=storage)
        got = _by_id(doc)["goal-x"]
        assert got["status"] == "accomplished", seed_metric
        assert "reopened_at" not in got
    # a moved target still re-arms, whatever the reopen rule says
    doc = g.init_goals("a", [_goal("goal-x", target=70, reopen=False)], storage=storage)
    assert _by_id(doc)["goal-x"]["status"] == "active"


def test_new_goal_seed_with_null_rules_is_valid(storage):
    doc = g.init_goals("a", [_goal("goal-new", target=5, ratchet=None, reopen=None)],
                       storage=storage)
    m = _by_id(doc)["goal-new"]["metric"]
    assert "ratchet" not in m and "reopen" not in m


@pytest.mark.parametrize("cfg", [
    {"by_agent_id": ["a"]},
    {"by_agent_id": "a"},
    {"by_agent_id": {"a": "yes"}},
    {"by_agent_id": {"a": {"reopen_on_regression": "true"}}},
    {"reopen_on_regression": "true"},
    ["not", "a", "mapping"],
])
def test_malformed_goals_config_never_blocks_recording(storage, cfg):
    g.init_goals("a", [_goal("goal-x", target=75), _goal("goal-other", target=1000)],
                 storage=storage)
    _accomplish(storage, "a", "goal-x", 80)
    storage.write_json(g.CONFIG_KEY, cfg)
    assert g.reopen_on_regression("a", storage=storage) is False
    assert g.reopen_on_regression("a", config=cfg) is False

    doc = g.record_goal_progress("a", "goal-x", 60, storage=storage)
    assert _by_id(doc)["goal-x"]["metric"]["current"] == 60
    assert _by_id(doc)["goal-x"]["status"] == "accomplished"        # re-open stays off

    mh.record_many("a", {"goal-x": 50, "goal-other": 7}, storage=storage)
    by = _by_id(g.read_active_goals("a", storage=storage))
    assert by["goal-x"]["metric"]["current"] == 50
    assert by["goal-other"]["metric"]["current"] == 7                # unrelated goal recorded
    mh.record("a", "goal-other", 8, storage=storage)
    assert _by_id(g.read_active_goals("a", storage=storage))["goal-other"]["metric"]["current"] == 8


def test_config_read_errors_mean_reopen_off(storage, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("config backend down")
    monkeypatch.setattr(g, "reopen_on_regression", boom)
    assert g.lazy_reopen_default("a", storage=storage)() is False
    g.init_goals("a", [_goal("goal-x", target=75)], storage=storage)
    _accomplish(storage, "a", "goal-x", 80)
    doc = g.record_goal_progress("a", "goal-x", 60, storage=storage)
    assert _by_id(doc)["goal-x"]["metric"]["current"] == 60
    mh.record_many("a", {"goal-x": 55}, storage=storage)
    assert _by_id(g.read_active_goals("a", storage=storage))["goal-x"]["metric"]["current"] == 55


def test_status_rule_failure_keeps_every_measurement(storage, monkeypatch):
    g.init_goals("a", [_goal("goal-bad", target=10), _goal("goal-ok", target=10)],
                 storage=storage)
    real = g.apply_measurement

    def flaky(goal, value, **kw):
        if goal["id"] == "goal-bad":
            raise ValueError("rule bug")
        return real(goal, value, **kw)
    monkeypatch.setattr(g, "apply_measurement", flaky)
    mh.record_many("a", {"goal-bad": 20, "goal-ok": 20}, storage=storage)
    by = _by_id(g.read_active_goals("a", storage=storage))
    assert by["goal-bad"]["metric"]["current"] == 20 and by["goal-bad"]["status"] == "active"
    assert by["goal-ok"]["status"] == "accomplished"
    doc = g.record_goal_progress("a", "goal-bad", 30, storage=storage)
    assert _by_id(doc)["goal-bad"]["metric"]["current"] == 30
    assert len(_by_id(doc)["goal-bad"]["progress_history"]) == 1


def test_nan_measurement_never_changes_status(storage):
    g.init_goals("a", [_goal("goal-x", target=75, reopen=True),
                       _goal("goal-r", target=10, ratchet={"factor": 1.25})], storage=storage)
    _accomplish(storage, "a", "goal-x", 80)
    doc = g.record_goal_progress("a", "goal-x", float("nan"), storage=storage)
    assert _by_id(doc)["goal-x"]["status"] == "accomplished"
    doc = g.record_goal_progress("a", "goal-r", float("nan"), storage=storage)
    assert _by_id(doc)["goal-r"]["metric"]["target"] == 10
    assert g.target_met({"target": 1, "current": float("nan")}) is None


def test_metric_helper_loads_beside_an_older_goals_module(storage, monkeypatch):
    """A long-running API that imported goals.py before a deploy keeps the
    old module in memory. The new metric_helper must still import (its
    read paths serve /goals/cache) and still record values."""
    import sys
    import types
    old_goals = types.ModuleType("framework.core.goals")
    old_goals.goals_key = g.goals_key          # an older goals.py: no status-rule API
    monkeypatch.setitem(sys.modules, "framework.core.goals", old_goals)
    spec = iu.spec_from_file_location("framework.core.metric_helper_mixed_under_test",
                                      _ROOT / "framework" / "core" / "metric_helper.py")
    fresh = iu.module_from_spec(spec)
    fresh.__package__ = "framework.core"
    spec.loader.exec_module(fresh)              # would raise ImportError with a top-level import

    storage.write_json("agents/a/goals/active.json", {
        "schema_version": "1", "agent_id": "a", "updated_at": "",
        "goals": [_goal("goal-x", target=10)]})
    fresh.record("a", "goal-x", 3, storage=storage)
    fresh.record_many("a", {"goal-x": 4}, storage=storage)
    assert storage.read_json("agents/a/goals/active.json")["goals"][0]["metric"]["current"] == 4
    assert fresh.read_cache("a", storage=storage)["goals"]["goal-x"]["latest_value"] == 4
    assert [p["value"] for p in fresh.read_goal_progress("a", "goal-x", storage=storage)] == [3, 4]
