"""Live status reporting for agents.

Each agent writes a single `status.json` to its storage prefix. The API
service watches this file (mtime poll + WebSocket push) so the UI can
glow when an agent is working.

Writes are throttled to at-most one per second to avoid blob churn —
agents can call `status(...)` from tight loops without paying for every
tick. The final state ("idle" / "success" / "failure") is always flushed.

Events log: every state transition AND every status.message change is
appended to `registry/events.jsonl` so the dashboard can show a global
activity feed.

Schema:
  agents/<agent-id>/status.json
    {
      "schema_version": "1",
      "agent_id": "...",
      "state": "idle"|"starting"|"running"|"success"|"failure"|"blocked"|"cancelled",
      "message": "Pulling GSC data",
      "progress": 0.45,                 # [0.0..1.0] optional
      "current_action": "fetch",        # short tag for the current step
      "started_at": "ISO ts",
      "updated_at": "ISO ts",
      "current_run_ts": "20260426T120000Z",
      "iteration_count": 4,
      "internal": {...}                 # agent-specific blob
    }
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.status")


_VALID_STATES = {
    "idle", "starting", "running", "success", "failure", "blocked", "cancelled",
}

_THROTTLE_INTERVAL_S = 1.0


def status_key(agent_id: str) -> str:
    return f"agents/{agent_id}/status.json"


def _events_key() -> str:
    return "registry/events.jsonl"


class StatusReporter:
    """One reporter per agent run. Holds throttle state in memory."""

    def __init__(
        self,
        agent_id: str,
        run_ts: str,
        iteration_count: int = 0,
        storage: Optional[StorageBackend] = None,
    ):
        self.agent_id = agent_id
        self.run_ts = run_ts
        self.iteration_count = iteration_count
        self.storage = storage or get_storage()
        self._started_at = self._iso_now()
        self._last_write_ts = 0.0
        self._last_state: Optional[str] = None
        self._lock = threading.Lock()

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def update(
        self,
        state: str,
        message: str = "",
        progress: float = 0.0,
        current_action: str = "",
        internal: Optional[dict] = None,
        force: bool = False,
    ) -> None:
        """Update the agent's live status. Throttled to ≤1 write/sec unless `force`."""
        if state not in _VALID_STATES:
            raise ValueError(f"invalid state {state!r} (expected one of {_VALID_STATES})")

        with self._lock:
            now = time.monotonic()
            state_changed = state != self._last_state
            # Always flush terminal states + state transitions; throttle the rest.
            terminal = state in ("success", "failure", "blocked", "cancelled", "idle")
            if not force and not state_changed and not terminal \
                    and (now - self._last_write_ts) < _THROTTLE_INTERVAL_S:
                return
            self._last_write_ts = now
            self._last_state = state

        payload: dict[str, Any] = {
            "schema_version": "1",
            "agent_id": self.agent_id,
            "state": state,
            "message": message,
            "progress": max(0.0, min(1.0, float(progress))),
            "current_action": current_action,
            "started_at": self._started_at,
            "updated_at": self._iso_now(),
            "current_run_ts": self.run_ts,
            "iteration_count": self.iteration_count,
            "internal": internal or {},
        }
        try:
            self.storage.write_json(
                status_key(self.agent_id), payload,
                cache_control="public, max-age=2",
            )
        except Exception as e:
            logger.warning(f"status write failed for {self.agent_id}: {e}")

        # Always append the transition to the global event log
        if state_changed or terminal:
            try:
                self.storage.append_jsonl(_events_key(), {
                    "ts": payload["updated_at"],
                    "agent_id": self.agent_id,
                    "run_ts": self.run_ts,
                    "state": state,
                    "message": message,
                    "current_action": current_action,
                })
            except Exception as e:
                logger.warning(f"event log append failed: {e}")

    # ---- Convenience shorthands ----

    def starting(self, message: str = "starting", **kw: Any) -> None:
        self.update("starting", message, **kw)

    def running(self, message: str = "", **kw: Any) -> None:
        self.update("running", message, **kw)

    def success(self, message: str = "ok", **kw: Any) -> None:
        self.update("success", message, force=True, **kw)

    def failure(self, message: str, **kw: Any) -> None:
        self.update("failure", message, force=True, **kw)

    def blocked(self, message: str, **kw: Any) -> None:
        self.update("blocked", message, force=True, **kw)

    def cancelled(self, message: str = "cancelled", **kw: Any) -> None:
        self.update("cancelled", message, force=True, **kw)

    def idle(self) -> None:
        self.update("idle", "", force=True)


def read_status(agent_id: str, storage: Optional[StorageBackend] = None) -> Optional[dict]:
    return (storage or get_storage()).read_json(status_key(agent_id))


def read_recent_events(
    since_ts: Optional[str] = None,
    limit: int = 100,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """Read most recent events from the global event log.
    Inefficient for very large logs; revisit with a tail-window strategy
    if events.jsonl grows beyond a few MB."""
    s = storage or get_storage()
    events = s.read_jsonl(_events_key())
    if since_ts:
        events = [e for e in events if e.get("ts", "") > since_ts]
    return events[-limit:]
