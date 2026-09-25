"""partition_by_hash — optional time-based cooldown.

The tick-count revisit rule assumes the same items show up every run. When
the candidate pool changes run to run (per-page rewrite ledgers), a per-run
counter never accumulates, so the cooldown has to be measured in wall-clock
time instead.
"""
from datetime import datetime, timedelta, timezone

from framework.core.short_circuit import partition_by_hash

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _items():
    return [{"k": "a", "h": "h1"}, {"k": "b", "h": "h2"},
            {"k": "c", "h": "h3"}, {"k": "d", "h": "h4"}]


def _split(**kw):
    fresh, cached, _ = partition_by_hash(
        _items(), kw.pop("prior_hashes"),
        key_fn=lambda i: i["k"], hash_fn=lambda i: i["h"], **kw)
    return sorted(i["k"] for i in fresh), sorted(i["k"] for i in cached)


def test_cooldown_caches_unchanged_recent_items_only():
    fresh, cached = _split(
        prior_hashes={"a": "h1", "b": "h2", "c": "CHANGED"},
        prior_seen_at={
            "a": _iso(NOW - timedelta(days=5)),    # unchanged, recent → skip
            "b": _iso(NOW - timedelta(days=31)),   # unchanged, expired → due
            "c": _iso(NOW - timedelta(days=1)),    # inputs changed → due
        },                                         # d: never seen → due
        cooldown_hours=30 * 24, now=NOW,
    )
    assert cached == ["a"]
    assert fresh == ["b", "c", "d"]


def test_cooldown_missing_or_bad_timestamp_counts_as_expired():
    fresh, cached = _split(
        prior_hashes={"a": "h1", "b": "h2"},
        prior_seen_at={"b": "not-a-date"},
        cooldown_hours=24, now=NOW,
    )
    assert cached == []
    assert fresh == ["a", "b", "c", "d"]


def test_cooldown_accepts_naive_and_z_suffixed_timestamps():
    fresh, cached = _split(
        prior_hashes={"a": "h1", "b": "h2"},
        prior_seen_at={"a": "2026-09-24T06:00:00Z",
                       "b": "2026-09-24T06:00:00"},
        cooldown_hours=24, now=NOW,
    )
    assert cached == ["a", "b"]


def test_cooldown_mode_ignores_tick_counter():
    # A counter past revisit_after_runs would force a revisit in tick
    # mode; in cooldown mode only the clock matters.
    fresh, cached = _split(
        prior_hashes={"a": "h1"},
        prior_seen_at={"a": _iso(NOW - timedelta(hours=1))},
        revisit_counter={"a": 99}, revisit_after_runs=1,
        cooldown_hours=24, now=NOW,
    )
    assert cached == ["a"]


def test_default_behaviour_unchanged_without_cooldown():
    fresh, cached, counter = partition_by_hash(
        _items(), {"a": "h1", "b": "h2"},
        key_fn=lambda i: i["k"], hash_fn=lambda i: i["h"],
        revisit_counter={"b": 12}, revisit_after_runs=12,
        # prior_seen_at is ignored when cooldown_hours is None
        prior_seen_at={"a": "2000-01-01T00:00:00+00:00"},
    )
    assert sorted(i["k"] for i in cached) == ["a"]
    assert sorted(i["k"] for i in fresh) == ["b", "c", "d"]
    assert counter == {"a": 1, "b": 0, "c": 0, "d": 0}
