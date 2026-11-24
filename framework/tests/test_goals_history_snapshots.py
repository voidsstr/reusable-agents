"""Per-run goal snapshots (agents/<id>/goals/history/<run_ts>.json) are off by
default: nothing reads them, and a once-a-minute agent wrote 3.5 TB of them
(2026-11-24 cost review). GOALS_HISTORY_SNAPSHOTS=1 turns them back on."""
from __future__ import annotations

from framework.core import goals as g


def _goal(gid):
    return {"id": gid, "title": gid, "description": "d",
            "metric": {"name": "m", "current": 0, "target": 10**9, "direction": "increase", "unit": "n"},
            "directives": ["x"]}


def test_no_history_snapshot_by_default(storage, monkeypatch):
    monkeypatch.delenv("GOALS_HISTORY_SNAPSHOTS", raising=False)
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    g.record_goal_progress("a", "goal-x", 5, run_ts="20261124T000000Z", storage=storage)
    assert storage.read_json("agents/a/goals/history/20261124T000000Z.json") is None
    assert storage.read_json(g.goals_key("a")) is not None


def test_history_snapshot_when_opted_in(storage, monkeypatch):
    monkeypatch.setenv("GOALS_HISTORY_SNAPSHOTS", "1")
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    g.record_goal_progress("a", "goal-x", 5, run_ts="20261124T000000Z", storage=storage)
    assert storage.read_json("agents/a/goals/history/20261124T000000Z.json") is not None
