"""active.json keeps a bounded progress_history; the full log is append-only.

Regression for 2026-11-11: an uncapped progress_history made one 1-minute
agent's goals/active.json grow to 65.8 MB, and every run re-downloaded and
re-uploaded it (~430 GB/day of blob egress)."""
from __future__ import annotations

from framework.core import goals as g


def _goal(gid, target=10**9):
    return {"id": gid, "title": gid,
            "metric": {"name": "m", "current": 0, "target": target, "direction": "increase"}}


def _hist(storage, agent, gid):
    doc = storage.read_json(g.goals_key(agent))
    return next(x for x in doc["goals"] if x["id"] == gid)["progress_history"]


def test_default_cap_bounds_history_and_keeps_newest(storage):
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    n = g.DEFAULT_PROGRESS_HISTORY_MAX + 25
    for i in range(n):
        g.record_goal_progress("a", "goal-x", i, storage=storage)
    hist = _hist(storage, "a", "goal-x")
    assert len(hist) == g.DEFAULT_PROGRESS_HISTORY_MAX
    assert hist[-1]["value"] == n - 1
    assert hist[0]["value"] == n - g.DEFAULT_PROGRESS_HISTORY_MAX


def test_full_history_is_appended_to_the_progress_log(storage):
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    for i in range(7):
        g.record_goal_progress("a", "goal-x", i, run_ts=f"r{i}", storage=storage)
    log = storage.read_jsonl("agents/a/goals/progress/goal-x.jsonl")
    assert [e["value"] for e in log] == list(range(7))
    assert log[-1]["run_ts"] == "r6"


def test_cap_is_configurable_and_zero_means_unlimited(storage):
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    storage.write_json(g.CONFIG_KEY, {"progress_history_max": 3})
    for i in range(10):
        g.record_goal_progress("a", "goal-x", i, storage=storage)
    assert [e["value"] for e in _hist(storage, "a", "goal-x")] == [7, 8, 9]

    storage.write_json(g.CONFIG_KEY, {"progress_history_max": 0})
    for i in range(10, 15):
        g.record_goal_progress("a", "goal-x", i, storage=storage)
    assert len(_hist(storage, "a", "goal-x")) == 8


def test_an_oversized_existing_history_is_trimmed_on_the_next_record(storage):
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    doc = storage.read_json(g.goals_key("a"))
    doc["goals"][0]["progress_history"] = [{"ts": "t", "value": i} for i in range(5000)]
    storage.write_json(g.goals_key("a"), doc)
    g.record_goal_progress("a", "goal-x", 1, storage=storage)
    assert len(_hist(storage, "a", "goal-x")) == g.DEFAULT_PROGRESS_HISTORY_MAX


def test_malformed_cap_config_falls_back_to_default():
    assert g.progress_history_cap(config={"progress_history_max": "lots"}) == g.DEFAULT_PROGRESS_HISTORY_MAX
    assert g.progress_history_cap(config={"progress_history_max": -1}) == 0
    assert g.progress_history_cap(config=[]) == g.DEFAULT_PROGRESS_HISTORY_MAX
