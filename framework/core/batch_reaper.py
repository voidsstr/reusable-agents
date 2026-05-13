"""Batch reaper — terminates stale entries in `dispatch-batches.json`.

The implementer writes one `dispatch-batches.json` per source-run-dir
listing every batch the responder split off. Each batch transitions
through `pending` → `running` → `completed` (or `paused`/`failed`).

When the host-worker bash crashes, OOMs, or the host reboots between
batches, the file gets stuck with non-terminal statuses (`paused`,
`pending`, `running`, `in_progress`) — the dashboard's Batches view
then keeps showing "in progress" indefinitely even though no process
is alive. The ghost-run reaper handles agent-level state but not
per-batch state, so we need a sibling primitive.

Reaping rules:
  - `running`/`in_progress`/`pending`/`paused` batches whose source
    run is older than `STALE_BATCH_GRACE_S` (default 6h) → flipped
    to `abandoned` + `completion_status="abandoned"` with a reason.
  - Idempotent: a second sweep on the same file is a no-op.
  - Cheap: only writes when at least one batch flipped.

Usage:
  - API service startup (one-shot)
  - Periodic sweep (every N minutes alongside ghost reaper)
  - One-shot CLI:  python3 -m framework.cli.batch_reaper
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.batch_reaper")

# Default grace: 6h. The longest legit batch (catalog-audit with many
# articles, full PI run) finishes well inside an hour. Anything paused
# or pending past 6h means the host process is gone.
STALE_BATCH_GRACE_S = 6 * 3600

NON_TERMINAL = {"paused", "pending", "running", "in_progress"}


def _parse_run_ts(run_ts: str) -> Optional[datetime]:
    """Parse a `20260505T193802Z` / ISO timestamp into a UTC datetime."""
    if not run_ts:
        return None
    try:
        if "T" in run_ts and run_ts.endswith("Z") and "-" not in run_ts:
            # 20260505T193802Z form
            return datetime.strptime(run_ts[:15], "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc,
            )
        return datetime.fromisoformat(run_ts.replace("Z", "+00:00"))
    except Exception:
        return None


def reap_one(
    key: str,
    *,
    grace_s: int = STALE_BATCH_GRACE_S,
    storage: Optional[StorageBackend] = None,
    now: Optional[datetime] = None,
) -> int:
    """Reap a single dispatch-batches.json. Returns count of reaped batches."""
    s = storage or get_storage()
    now = now or datetime.now(timezone.utc)
    try:
        body = s.read_json(key)
    except Exception as e:
        logger.debug("[batch-reaper] read %s failed: %s", key, e)
        return 0
    if not isinstance(body, dict):
        return 0

    source_run_ts = body.get("source_run_ts") or ""
    source_dt = _parse_run_ts(source_run_ts)
    # Belt-and-suspenders: also derive from the key path
    # `agents/<aid>/runs/<run_ts>/dispatch-batches.json`.
    if source_dt is None:
        parts = key.split("/")
        if len(parts) >= 4 and parts[-1] == "dispatch-batches.json":
            source_dt = _parse_run_ts(parts[-2])
    if source_dt is None:
        return 0
    if (now - source_dt).total_seconds() < grace_s:
        return 0

    reaped = 0
    iso_now = now.isoformat(timespec="seconds")
    for b in (body.get("batches") or []):
        if b.get("status") in NON_TERMINAL:
            b["status"] = "abandoned"
            b["completion_status"] = b.get("completion_status") or "abandoned"
            b["abandoned_at"] = iso_now
            b["abandoned_reason"] = (
                "batch_reaper sweep — source run older than "
                f"{grace_s}s and no terminal status was ever written "
                "(host process gone, OOM, or restart)"
            )
            reaped += 1
    if reaped:
        try:
            s.write_json(key, body)
            logger.info("[batch-reaper] %s: reaped %d batch(es)", key, reaped)
        except Exception as e:
            logger.warning("[batch-reaper] write %s failed: %s", key, e)
            return 0
    return reaped


def reap_all(
    *,
    grace_s: int = STALE_BATCH_GRACE_S,
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Sweep every dispatch-batches.json under agents/.

    Returns {"files_updated": N, "batches_reaped": M}.
    """
    s = storage or get_storage()
    files = 0
    batches = 0
    try:
        keys = s.list_prefix("agents/") if hasattr(s, "list_prefix") else []
    except Exception as e:
        logger.warning("[batch-reaper] list_prefix failed: %s", e)
        return {"files_updated": 0, "batches_reaped": 0}
    for k in keys:
        if not k.endswith("/dispatch-batches.json"):
            continue
        n = reap_one(k, grace_s=grace_s, storage=s)
        if n > 0:
            files += 1
            batches += n
    return {"files_updated": files, "batches_reaped": batches}
