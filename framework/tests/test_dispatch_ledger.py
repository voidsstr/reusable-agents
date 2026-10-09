"""The dispatch ledger lets the backlog-dispatcher see recs a producer already
handed to the implementer itself.

2026-10-08/09: article-proposal agents dispatch their own rec, sync the run-dir
with it still unshipped, and the dispatcher spawned a second implementer for it
seconds later. The second run drafted a full Opus article before deferring as a
duplicate.
"""
from datetime import datetime, timedelta, timezone

from framework.core import dispatch

T0 = datetime(2026, 10, 9, 4, 48, 0, tzinfo=timezone.utc)
AGENT = "specpicks-article-proposal-agent"


def test_recorded_rec_is_seen_under_the_dispatchers_dedup_key(storage):
    dispatch.record_dispatched(AGENT, "20261009T044500Z", ["art-001"], storage=storage, now=T0)
    seen = dispatch.recently_dispatched(AGENT, storage=storage, now=T0 + timedelta(seconds=31))
    assert seen == {"20261009T044500Z:art-001"}


def test_entries_expire_after_the_ttl_so_a_failed_run_is_retried(storage):
    dispatch.record_dispatched(AGENT, "20261009T044500Z", ["art-001"], storage=storage, now=T0)
    later = T0 + timedelta(seconds=dispatch.LEDGER_TTL_S + 1)
    assert dispatch.recently_dispatched(AGENT, storage=storage, now=later) == set()


def test_ledgers_are_per_producer(storage):
    dispatch.record_dispatched(AGENT, "20261009T044500Z", ["art-001"], storage=storage, now=T0)
    assert dispatch.recently_dispatched("aisleprompt-article-proposal-agent",
                                        storage=storage, now=T0) == set()


def test_a_new_record_drops_expired_entries_and_keeps_live_ones(storage):
    dispatch.record_dispatched(AGENT, "20261008T044500Z", ["art-001"], storage=storage, now=T0)
    dispatch.record_dispatched(AGENT, "20261009T001500Z", ["art-001"],
                               storage=storage, now=T0 + timedelta(seconds=dispatch.LEDGER_TTL_S - 60))
    dispatch.record_dispatched(AGENT, "20261009T044500Z", ["art-001", "art-002"],
                               storage=storage, now=T0 + timedelta(seconds=dispatch.LEDGER_TTL_S + 1))
    doc = storage.read_json(f"{dispatch.LEDGER_PREFIX}{AGENT}.json")
    assert set(doc) == {"20261009T001500Z:art-001", "20261009T044500Z:art-001",
                        "20261009T044500Z:art-002"}


def test_missing_run_ts_records_nothing(storage):
    dispatch.record_dispatched(AGENT, "", ["art-001"], storage=storage, now=T0)
    assert storage.read_json(f"{dispatch.LEDGER_PREFIX}{AGENT}.json") is None


def test_unreadable_entries_are_ignored(storage):
    storage.write_json(f"{dispatch.LEDGER_PREFIX}{AGENT}.json",
                       {"20261009T044500Z:art-001": "not-a-time", "x": None})
    assert dispatch.recently_dispatched(AGENT, storage=storage, now=T0) == set()
