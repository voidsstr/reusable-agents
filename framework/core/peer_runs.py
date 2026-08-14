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


# A run dir is exactly YYYYmmddTHHMMSSZ. Anything else under runs/ is debris —
# test fixtures ("test-direct-write-20260522"), persisted dispatch copies
# ("rundir-<agent>-<ts>-<slug>"), strays. They matter because 't'/'r' sort AFTER
# '2', so a single stray becomes latest_run_ts() and the caller acts on a
# non-run. Truncation used to hide this by cutting the list off in May.
_RUN_TS_RE = __import__("re").compile(r"^\d{8}T\d{6}Z$")


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

    # Enumerate run DIRECTORIES, not every blob beneath them. list_prefix()
    # caps at 10000 keys and returns them lexicographically, so a busy agent
    # silently yields its OLDEST slice: on 2026-08-14
    # specpicks-seo-opportunity-agent had >10000 blobs under runs/ and this
    # function returned a newest run of 20260511T060000Z -- three months stale --
    # which latest_run_ts() then handed downstream as "current". A consumer
    # acting on that would forward May recommendations as today's.
    try:
        children = st.list_child_prefixes(prefix)
    except Exception:
        children = []
    if children:
        return sorted(c for c in children if c and _RUN_TS_RE.match(c))

    # Fallback for backends without delimiter support. Still cap-bound, so
    # treat a full page as untrustworthy rather than returning a stale answer.
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
        if head and "/" not in head and head != rest and _RUN_TS_RE.match(head):
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
