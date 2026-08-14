"""Tests for framework.core.peer_runs — cross-agent run lookup.

Regression cover for the specpicks-schema-fix-specialist failure mode:
an agent that hand-rolled `Path(STORAGE_ROOT)/agents/<id>/runs` saw an
empty directory under STORAGE_BACKEND=azure and reported
"No <agent> runs." on every run while the source agent was succeeding.
These tests pin the behaviour to the storage backend, not the local FS.
"""
from __future__ import annotations

import pytest

from framework.core import peer_runs
from framework.core.storage import LocalFilesystemStorage


SOURCE = "specpicks-seo-opportunity-agent"


@pytest.fixture()
def storage(tmp_path):
    return LocalFilesystemStorage(str(tmp_path))


def _seed(storage, agent_id, run_ts, **files):
    for name, doc in files.items():
        storage.write_json(f"agents/{agent_id}/runs/{run_ts}/{name}", doc)


def test_list_run_ts_is_chronological(storage):
    for ts in ("20260511T060000Z", "20260510T220013Z", "20260511T020000Z"):
        _seed(storage, SOURCE, ts, **{"recommendations.json": {"recommendations": []}})
    got = peer_runs.list_run_ts(SOURCE, storage=storage)
    assert got == [
        "20260510T220013Z", "20260511T020000Z", "20260511T060000Z",
    ]


def test_latest_run_ts_picks_newest(storage):
    for ts in ("20260510T220013Z", "20260511T060000Z"):
        _seed(storage, SOURCE, ts, **{"recommendations.json": {"recommendations": []}})
    assert peer_runs.latest_run_ts(SOURCE, storage=storage) == "20260511T060000Z"


def test_no_runs_returns_empty_not_error(storage):
    assert peer_runs.list_run_ts("never-ran-agent", storage=storage) == []
    assert peer_runs.latest_run_ts("never-ran-agent", storage=storage) is None
    assert peer_runs.read_latest_artifact(
        "never-ran-agent", "recommendations.json", storage=storage,
    ) is None


def test_read_latest_artifact_returns_doc(storage):
    _seed(storage, SOURCE, "20260511T060000Z",
          **{"recommendations.json": {"recommendations": [{"id": "rec-001"}]}})
    doc = peer_runs.read_latest_artifact(
        SOURCE, "recommendations.json", storage=storage,
    )
    assert doc["recommendations"][0]["id"] == "rec-001"


def test_latest_run_with_artifact_skips_runs_missing_it(storage):
    """The newest run is often in-flight or short-circuited, so it may
    carry no recommendations.json. Consumers want the newest run that
    actually has results."""
    _seed(storage, SOURCE, "20260511T020000Z",
          **{"recommendations.json": {"recommendations": [{"id": "rec-009"}]}})
    # Newer run exists but only has a progress file — no recs yet.
    _seed(storage, SOURCE, "20260511T060000Z",
          **{"progress.json": {"phase": "collect"}})

    assert peer_runs.latest_run_ts(SOURCE, storage=storage) == "20260511T060000Z"

    run_ts, doc = peer_runs.latest_run_with_artifact(
        SOURCE, "recommendations.json", storage=storage,
    )
    assert run_ts == "20260511T020000Z"
    assert doc["recommendations"][0]["id"] == "rec-009"


def test_read_run_artifact_missing_file_is_none(storage):
    _seed(storage, SOURCE, "20260511T060000Z", **{"progress.json": {"phase": "x"}})
    assert peer_runs.read_run_artifact(
        SOURCE, "20260511T060000Z", "recommendations.json", storage=storage,
    ) is None
