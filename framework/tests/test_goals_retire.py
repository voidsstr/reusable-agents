"""Retiring (abandoning) a goal through the seed path.

A vanity goal such as "recs emitted per run" used to be removable only by
hand-editing storage: PUT /goals merges (init_goals keeps unknown ids), and
record_goal_progress re-accomplished it every run. These tests pin the
retire contract: a seed with status "abandoned" retires an existing goal,
its history survives, measurements never flip it back, and prompt text
drops it.
"""
from framework.core import goals as g


def _goal(gid, name="m", target=10, direction="increase", **kw):
    d = {"id": gid, "title": gid, "metric": {"name": name, "current": 0,
                                             "target": target,
                                             "direction": direction}}
    d.update(kw)
    return d


def test_seed_can_retire_and_unretire(storage):
    g.init_goals("a", [_goal("goal-recs-per-run", target_metric="rec_count"),
                       _goal("goal-ai-referrals-30d")], storage=storage)
    g.record_goal_progress("a", "goal-recs-per-run", 3, storage=storage)

    doc = g.init_goals("a", [{"id": "goal-recs-per-run", "status": "abandoned"}], storage=storage)
    by = {x["id"]: x for x in doc["goals"]}
    assert by["goal-recs-per-run"]["status"] == "abandoned"
    assert by["goal-recs-per-run"]["abandoned_at"]
    assert len(by["goal-recs-per-run"]["progress_history"]) == 1   # kept
    assert by["goal-recs-per-run"]["title"] == "goal-recs-per-run"  # not clobbered
    assert by["goal-ai-referrals-30d"]["status"] == "active"       # untouched

    # A later plain re-seed (no status) must not resurrect it.
    doc = g.init_goals("a", [_goal("goal-recs-per-run")], storage=storage)
    assert {x["id"]: x for x in doc["goals"]}["goal-recs-per-run"]["status"] == "abandoned"

    # Explicit un-retire.
    doc = g.init_goals("a", [{"id": "goal-recs-per-run", "status": "active"}], storage=storage)
    got = {x["id"]: x for x in doc["goals"]}["goal-recs-per-run"]
    assert got["status"] == "active" and "abandoned_at" not in got


def test_retire_entry_for_unknown_goal_is_a_noop(storage):
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    doc = g.init_goals("a", [{"id": "goal-never-existed", "status": "abandoned"}],
                       storage=storage)
    assert [x["id"] for x in doc["goals"]] == ["goal-x"]


def test_seed_cannot_force_accomplished(storage):
    g.init_goals("a", [_goal("goal-x")], storage=storage)
    doc = g.init_goals("a", [{"id": "goal-x", "status": "accomplished"}], storage=storage)
    assert doc["goals"][0]["status"] == "active"


def test_measurement_never_reaccomplishes_a_retired_goal(storage):
    g.init_goals("a", [_goal("goal-recs-per-run", target=12)], storage=storage)
    g.init_goals("a", [{"id": "goal-recs-per-run", "status": "abandoned"}], storage=storage)
    doc = g.record_goal_progress("a", "goal-recs-per-run", 12, storage=storage)
    got = doc["goals"][0]
    assert got["status"] == "abandoned"
    assert got["metric"]["current"] == 12


def test_directives_text_drops_retired_goals(storage):
    g.init_goals("a", [_goal("goal-keep", directives=["do the thing"]),
                       _goal("goal-drop", directives=["count recs"])],
                 storage=storage)
    g.init_goals("a", [{"id": "goal-drop", "status": "abandoned"}],
                 storage=storage)
    txt = g.goals_directives_text("a", storage=storage)
    assert "goal-keep" in txt and "goal-drop" not in txt
