"""Read another agent's run artifacts through the configured storage backend.

Consumer agents routinely need "the latest run of agent X, and the
`recommendations.json` inside it" — schema-fix-specialist, gap
consumers, rollups, digesters. Every one of them that hand-rolled the
lookup did it by `Path`-ing into `~/.reusable-agents/storage/agents/...`,
which is only correct when `STORAGE_BACKEND=local`. The fleet runs
`STORAGE_BACKEND=azure`, so those agents saw an empty directory and
reported "No <agent> runs." forever while the producer was succeeding
several times a day. (specpicks-schema-fix-specialist: 10/10 runs
reporting no source runs while the source had 146 runs and 131
recommendations.json objects in blob.)

This module is the one correct way to do it: everything resolves through
`framework.core.storage.get_storage()`, so the same code works on local
FS in tests and on Azure Blob in production.

Usage:
    from framework.core import peer_runs

    run_ts = peer_runs.latest_run_ts("specpicks-seo-opportunity-agent")
    doc = peer_runs.read_latest_artifact(
        "specpicks-seo-opportunity-agent", "recommendations.json",
    )
    # or, requiring the artifact to be present:
    run_ts, doc = peer_runs.latest_run_with_artifact(
        "specpicks-seo-opportunity-agent", "recommendations.json",
    )

Refuse on sight: a new agent that builds `STORAGE_ROOT / "agents" / ...`
by hand — use these helpers instead.
"""
from __future__ import annotations

from typing import Any, Optional

from .storage import get_storage

__all__ = [
    "runs_prefix",
    "list_run_ts",
    "latest_run_ts",
    "read_run_artifact",
    "read_latest_artifact",
    "latest_run_with_artifact",
]


def runs_prefix(agent_id: str) -> str:
    """Storage prefix holding an agent's run directories."""
    return f"agents/{agent_id}/runs/"


def list_run_ts(agent_id: str, *, storage=None) -> list[str]:
    """Every run timestamp for `agent_id`, oldest first.

    Run timestamps are `YYYYmmddTHHMMSSZ`, so lexical sort is
    chronological — no date parsing needed.
    """
    st = storage or get_storage()
    prefix = runs_prefix(agent_id)
    try:
        keys = st.list_prefix(prefix) or []
    except Exception:
        return []
    seen: set[str] = set()
    for key in keys:
        rest = key[len(prefix):] if key.startswith(prefix) else ""
        head = rest.split("/", 1)[0]
        # Only accept a real run-dir segment (a key directly under the
        # prefix with no child path is a stray file, not a run).
        if head and "/" not in head and head != rest:
            seen.add(head)
    return sorted(seen)


def latest_run_ts(agent_id: str, *, storage=None) -> Optional[str]:
    """Most recent run timestamp, or None if the agent has never run."""
    all_ts = list_run_ts(agent_id, storage=storage)
    return all_ts[-1] if all_ts else None


def read_run_artifact(
    agent_id: str, run_ts: str, filename: str, *, storage=None,
) -> Optional[Any]:
    """Read one JSON artifact from a specific run. None if absent."""
    st = storage or get_storage()
    try:
        return st.read_json(f"{runs_prefix(agent_id)}{run_ts}/{filename}")
    except Exception:
        return None


def read_latest_artifact(
    agent_id: str, filename: str, *, storage=None,
) -> Optional[Any]:
    """Read `filename` from the agent's most recent run."""
    st = storage or get_storage()
    run_ts = latest_run_ts(agent_id, storage=st)
    if not run_ts:
        return None
    return read_run_artifact(agent_id, run_ts, filename, storage=st)


def latest_run_with_artifact(
    agent_id: str, filename: str, *, max_scan: int = 25, storage=None,
) -> tuple[Optional[str], Optional[Any]]:
    """Newest run that actually CONTAINS `filename`, as (run_ts, doc).

    Walks back up to `max_scan` runs. The newest run is often still
    in-flight (or was a short-circuit that wrote no artifacts), so
    "latest run" and "latest run with results" are different questions —
    consumers almost always want this one.
    """
    st = storage or get_storage()
    for run_ts in reversed(list_run_ts(agent_id, storage=st)[-max_scan:]):
        doc = read_run_artifact(agent_id, run_ts, filename, storage=st)
        if doc is not None:
            return run_ts, doc
    return None, None
