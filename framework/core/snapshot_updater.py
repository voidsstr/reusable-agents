"""Background task: every 5s, snapshot every agent's status into one blob.

The dashboard's GET /api/agents reads this single blob (registry/agent-snapshot.json)
instead of N status.json files — turning N+1 Azure round-trips into 1.

If the snapshot is missing or stale (>30s) the readers fall back to the
existing parallel-read path, so the dashboard never breaks if the updater
is down.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from . import registry, storage


log = logging.getLogger("framework.snapshot_updater")

_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot_once(s) -> dict:
    out: dict = {"updated_at": _now_iso(), "agents": {}}
    try:
        agents = registry.list_agents(s)
    except Exception as e:
        log.warning("snapshot_updater: registry.list_agents failed: %s", e)
        return out
    for m in agents:
        try:
            status = s.read_json(f"agents/{m.id}/status.json") or {}
            out["agents"][m.id] = {
                "state":           status.get("state", ""),
                "message":         status.get("message", ""),
                "progress":        status.get("progress", 0),
                "current_action":  status.get("current_action", ""),
                "started_at":      status.get("started_at"),
                "updated_at":      status.get("updated_at"),
                "current_run_ts":  status.get("current_run_ts"),
                "iteration_count": status.get("iteration_count", 0),
            }
        except Exception as e:
            log.warning("snapshot_updater: %s status read failed: %s", m.id, e)
    return out


def _loop(interval_s: float = 5.0) -> None:
    log.info("snapshot_updater started, interval=%.1fs", interval_s)
    while not _STOP.is_set():
        try:
            s = storage.get_storage()
            snap = _snapshot_once(s)
            # Use cache_control if backend supports it (Azure does; Local ignores)
            try:
                s.write_json(
                    "registry/agent-snapshot.json", snap,
                    cache_control="public, max-age=5",
                )
            except TypeError:
                # Older write_json without cache_control kwarg
                s.write_json("registry/agent-snapshot.json", snap)
        except Exception as e:
            log.warning("snapshot_updater iteration failed: %s", e)
        _STOP.wait(interval_s)
    log.info("snapshot_updater stopped")


def start(interval_s: float = 5.0) -> None:
    """Start the snapshot updater in a daemon thread (idempotent)."""
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop, args=(interval_s,),
        daemon=True, name="snapshot-updater",
    )
    _THREAD.start()


def stop(timeout: float = 2.0) -> None:
    """Signal the updater to exit and wait briefly for it."""
    _STOP.set()
    if _THREAD is not None:
        _THREAD.join(timeout=timeout)
