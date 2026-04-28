"""Ghost-run reaper — flips orphaned `running`/`starting` agents to
`failure` when their status hasn't been heartbeated in too long.

Why this exists: an agent's terminal state (success/failure) is written
by either the agent itself (post_run) or the host-worker bash (after
the subprocess exits). Both can fail to fire — host-worker bash crash,
SIGKILL, OOM, host reboot, docker compose stop, etc. When that happens
the dashboard sees `state="running"` indefinitely and the next manual
trigger is blocked because the UI thinks "a run is already in progress".

The host-worker writes a heartbeat every 5s. If updated_at is older
than `STALE_RUN_GRACE_S` (default 180s = 36 missed heartbeats), the run
is presumed dead and reaped.

Reaping is idempotent and safe to call from multiple paths:
  - API service startup (once at boot)
  - API list-agents endpoint (lazy — costs one extra read per
    state=running agent on each list, which is rare and cheap)
  - Periodic background sweep (every 60s)
  - One-shot CLI (for manual recovery)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .status import _events_key, status_key
from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.ghost_reaper")


STALE_RUN_GRACE_S = int(os.environ.get("AGENT_STALE_RUN_GRACE_S", "180"))


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def reap_one(
    agent_id: str,
    *,
    grace_s: int = STALE_RUN_GRACE_S,
    storage: Optional[StorageBackend] = None,
) -> Optional[dict]:
    """If `agent_id` is in a running/starting state with a stale
    heartbeat, mark it failed and append an event. Returns the new
    status dict on reap, None on no-op.
    """
    s = storage or get_storage()
    key = status_key(agent_id)
    try:
        cur = s.read_json(key)
    except Exception:
        return None
    if not cur:
        return None
    if cur.get("state") not in ("running", "starting"):
        return None
    updated_at = _parse_iso(cur.get("updated_at") or "")
    if not updated_at:
        return None
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if age <= grace_s:
        return None

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    msg = (
        f"Ghost run reaped — heartbeat stale {int(age)}s "
        f"(>{grace_s}s grace). Host-worker bash likely died."
    )
    cur.update({
        "state": "failure",
        "message": msg,
        "current_action": "",
        "progress": 1.0,
        "updated_at": now,
        "internal": {**(cur.get("internal") or {}), "reaped_by": "ghost_reaper"},
    })
    cur.setdefault("agent_id", agent_id)
    cur.setdefault("schema_version", "1")
    try:
        s.write_json(key, cur)
        s.append_jsonl(_events_key(), {
            "ts": now,
            "agent_id": agent_id,
            "run_ts": cur.get("current_run_ts", ""),
            "state": "failure",
            "message": msg,
            "current_action": "ghost-reaper",
        })
        # Also flush the live-LLM tail's is_active=false so the UI's
        # "● tailing" badge clears immediately.
        try:
            from . import llm_stream
            run_ts = cur.get("current_run_ts") or ""
            if run_ts:
                tail = llm_stream.LiveLLMTail(agent_id, run_ts, storage=s)
                tail.finalize()
        except Exception:
            pass
        logger.warning("[ghost-reaper] reaped %s (age=%ds)", agent_id, int(age))
        return cur
    except Exception as e:
        logger.warning("[ghost-reaper] write failed for %s: %s", agent_id, e)
        return None


def reap_all(
    *,
    grace_s: int = STALE_RUN_GRACE_S,
    storage: Optional[StorageBackend] = None,
) -> list[str]:
    """Sweep every status.json under agents/. Returns the list of
    agent_ids that got reaped this pass."""
    s = storage or get_storage()
    reaped: list[str] = []
    try:
        # status.json keys are agents/<id>/status.json
        keys = s.list_prefix("agents/") if hasattr(s, "list_prefix") else []
    except Exception as e:
        logger.warning("[ghost-reaper] list_prefix failed: %s", e)
        return reaped
    for k in keys:
        if not k.endswith("/status.json"):
            continue
        agent_id = k[len("agents/"):-len("/status.json")]
        if "/" in agent_id:
            continue
        if reap_one(agent_id, grace_s=grace_s, storage=s) is not None:
            reaped.append(agent_id)
    return reaped
