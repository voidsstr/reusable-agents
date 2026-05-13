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


# Long enough to cover a single claude --print web-search turn (which
# can legitimately take 8-10 min while the host-worker heartbeat is
# also blocked in Azure storage I/O — see benchmark-research run that
# got false-positively reaped at 222s of stale heartbeat). The
# secondary liveness signal below (live-LLM blob mtime) makes false
# positives nearly impossible for LLM-using agents even at this
# threshold.
STALE_RUN_GRACE_S = int(os.environ.get("AGENT_STALE_RUN_GRACE_S", "900"))


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

    # Secondary liveness signal: even if status.json hasn't been
    # touched in `grace_s`, the agent may still be actively streaming
    # LLM output (long claude --print calls don't go through
    # StatusReporter, and the host-worker bash heartbeat occasionally
    # blocks in Azure storage retries). If the live-LLM tail blob has
    # been updated within `grace_s`, treat the run as alive and skip
    # reaping. Best-effort — any failure here falls through to reap.
    try:
        from . import llm_stream
        tail_key = llm_stream.live_llm_tail_key(agent_id)
        body = s.read_text(tail_key) or ""
        if body.startswith("__META__: "):
            import json as _json
            nl = body.find("\n")
            meta_line = body[len("__META__: "):nl] if nl > 0 else ""
            try:
                meta = _json.loads(meta_line)
            except Exception:
                meta = {}
            tail_updated = _parse_iso(meta.get("updated_at") or "")
            if tail_updated:
                tail_age = (datetime.now(timezone.utc) - tail_updated).total_seconds()
                if tail_age <= grace_s:
                    logger.info(
                        "[ghost-reaper] %s status stale %ds but live-LLM "
                        "tail fresh %ds ago — keeping run alive",
                        agent_id, int(age), int(tail_age),
                    )
                    return None
    except Exception:
        pass

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


def reap_run_index(
    agent_id: str,
    *,
    grace_s: int = STALE_RUN_GRACE_S,
    storage: Optional[StorageBackend] = None,
) -> int:
    """Sweep `agents/<id>/run-index.json::recent[]` for entries stuck in
    `running` state whose started_at is older than `grace_s`. Flip them
    to `failure` so the dashboard's Runs tab doesn't show perpetual
    spinners after a host-worker / dispatch-scope crash.

    Distinct from reap_one (which handles the live `status.json`).
    This handles HISTORICAL run records that never got their terminal
    state written because the scope died abruptly. Common for the
    implementer because each dispatch is a one-shot systemd-run scope
    that doesn't always exit cleanly.

    Returns the number of entries flipped.
    """
    s = storage or get_storage()
    key = f"agents/{agent_id}/run-index.json"
    try:
        ri = s.read_json(key) or {}
    except Exception:
        return 0
    recent = ri.get("recent") or []
    if not isinstance(recent, list) or not recent:
        return 0
    now = datetime.now(timezone.utc)
    cleared = 0
    for r in recent:
        if not isinstance(r, dict):
            continue
        if r.get("status") != "running":
            continue
        started = _parse_iso(r.get("started_at") or "")
        if started is None:
            continue
        age = (now - started).total_seconds()
        if age <= grace_s:
            continue
        # Optionally consult live systemd state for implementer scopes,
        # but skipping that here — if a scope older than grace_s is
        # still running, the reaper running every 60s will catch up the
        # NEXT pass after the scope finally exits. Worst case: one false
        # reap on a single tick, which is acceptable since the actual
        # run-record.json holds the real terminal state.
        r["status"] = "failure"
        r["ended_at"] = now.isoformat(timespec="seconds")
        sm = r.get("summary") or ""
        if "[ghost-cleaned" not in sm:
            r["summary"] = sm + f" [ghost-cleaned: stale running, age={int(age/60)}m]"
        cleared += 1
    if cleared:
        try:
            ri["recent"] = recent
            s.write_json(key, ri)
        except Exception as e:
            logger.warning("[ghost-reaper] run-index write failed for %s: %s",
                            agent_id, e)
            return 0
    return cleared


def reap_all(
    *,
    grace_s: int = STALE_RUN_GRACE_S,
    storage: Optional[StorageBackend] = None,
) -> list[str]:
    """Sweep every status.json AND run-index.json under agents/. Returns
    the list of agent_ids that got reaped this pass.

    Uses the registry to enumerate agent_ids instead of `list_prefix(
    'agents/')` — the latter hit Azure's 10K pagination cap (2026-05-11:
    `implementer/status.json` and 6 other agents were invisible to the
    reaper because they sorted alphabetically after the first 10K keys
    under agents/, which is dominated by run-dir contents). The
    registry is a single small file (`registry/agents.json`) so this
    scales linearly with agent count, not blob count.
    """
    s = storage or get_storage()
    reaped: list[str] = []
    try:
        from . import registry
        agent_ids = [a.id for a in registry.list_agents(storage=s)]
    except Exception as e:
        logger.warning("[ghost-reaper] registry list failed: %s", e)
        return reaped
    for agent_id in agent_ids:
        # Live-status reap (existing behavior)
        if reap_one(agent_id, grace_s=grace_s, storage=s) is not None:
            reaped.append(agent_id)
        # Run-index history reap (new — clears dashboard ghosts)
        n = reap_run_index(agent_id, grace_s=grace_s, storage=s)
        if n and agent_id not in reaped:
            reaped.append(agent_id)
    return reaped
