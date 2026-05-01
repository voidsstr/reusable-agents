"""Inter-agent handoff protocol.

When agent A discovers work that belongs to agent B's domain, A calls
`send_handoff()` instead of trying to do it (or in addition to a
deferral). The framework persists the handoff at
`agents/<to_agent>/handoff-queue/<request-id>.json`. On its next run,
agent B's `pre_run()` calls `drain_handoffs()` to pick up the queue
and add the handed-off items to its own work plan.

Why this exists:

  • The implementer is a code editor. It can't run an LLM-driven
    article-author pass. Without a handoff primitive, "this is content-
    team work" deferrals just rot in the dashboard until somebody
    notices and re-routes manually. With the primitive, the implementer
    sends the rec to article-author-agent which picks it up next tick.

  • Auto-improving system: every handoff is a record of "agent A asked
    agent B to do X". `handoffs.jsonl` per agent gives us the data to
    learn which routings work and surface "stuck handoffs" (B never
    acted on what A handed off) as a doctor signal.

  • Capability discovery: the framework's `agent_capabilities.work_type`
    field lets agents express what kinds of work they handle. A sender
    can call `find_handler(work_type)` and get the right agent without
    hardcoding ids.

Schema of a handoff envelope:

    {
      "schema_version": "1",
      "request_id": "h-<UTC-ts>-<seq>",
      "from_agent": "<sender>",
      "to_agent": "<receiver>",
      "work_type": "internal_link_addition",   # canonical from work_types.py
      "rec_id": "rec-001",                     # the original rec, when applicable
      "rec": {...},                            # the full rec object, for context
      "source_run_ts": "20260501T200138Z",     # which run produced the rec
      "source_agent": "specpicks-seo-opportunity-agent",
      "rationale": "content-team work, not a code edit",
      "deadline_hint": null,                   # optional ISO ts ('respond by')
      "queued_at": "2026-05-01T22:00:00+00:00"
    }

The receiver acks by moving the file to
`agents/<to_agent>/handoff-processed/<request-id>.json` and appending
to `agents/<to_agent>/handoffs.jsonl` with the outcome. Outcome rows:

    {
      "request_id": "...",
      "from_agent": "...",
      "work_type": "...",
      "rec_id": "...",
      "received_at": "...",
      "acted_at": "...",                  # when the work actually started
      "outcome": "shipped|deferred|rejected|in_progress",
      "outcome_detail": "...",
      "downstream_run_ts": "..."          # the run dir that did the work
    }

The digest agent reads these to surface "agent X handed off N items to
agent Y this window; M shipped, K still pending" so the user sees the
inter-agent flow without poking at internals.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.handoff")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_seq_lock = threading.Lock()
_seq_state = {"last_ts": "", "seq": 0}


def new_request_id() -> str:
    """Format: h-YYYYMMDD-HHMMSS-NNNN — sortable, monotonic per process."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    with _seq_lock:
        if ts == _seq_state["last_ts"]:
            _seq_state["seq"] += 1
        else:
            _seq_state["last_ts"] = ts
            _seq_state["seq"] = 1
        seq = _seq_state["seq"]
    return f"h-{ts}-{seq:04d}"


def queue_key(to_agent: str, request_id: str) -> str:
    return f"agents/{to_agent}/handoff-queue/{request_id}.json"


def processed_key(to_agent: str, request_id: str) -> str:
    return f"agents/{to_agent}/handoff-processed/{request_id}.json"


def metrics_key(agent_id: str) -> str:
    return f"agents/{agent_id}/handoffs.jsonl"


def send_handoff(
    *,
    from_agent: str,
    to_agent: str,
    work_type: str,
    rec_id: str = "",
    rec: Optional[dict] = None,
    source_run_ts: str = "",
    source_agent: str = "",
    rationale: str = "",
    deadline_hint: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Persist a handoff and return its request_id.

    The receiver picks it up on its next run via `drain_handoffs()`.
    Idempotent only against duplicate request_ids — caller is
    responsible for deduping by (rec_id, source_run_ts) if needed.
    """
    if not from_agent or not to_agent:
        raise ValueError("handoff: from_agent and to_agent are required")
    if from_agent == to_agent:
        raise ValueError(f"handoff: refusing self-loop from {from_agent}")
    s = storage or get_storage()
    request_id = new_request_id()
    envelope: dict[str, Any] = {
        "schema_version": "1",
        "request_id": request_id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "work_type": work_type,
        "rec_id": rec_id,
        "rec": rec or {},
        "source_run_ts": source_run_ts,
        "source_agent": source_agent or from_agent,
        "rationale": rationale,
        "deadline_hint": deadline_hint,
        "queued_at": _now(),
    }
    s.write_json(queue_key(to_agent, request_id), envelope)

    # Log to the SENDER's handoffs.jsonl as outbound — gives the digest
    # an easy "what did I hand off this window" view.
    try:
        _append_metrics_line(s, from_agent, {
            "direction": "out",
            "request_id": request_id,
            "to_agent": to_agent,
            "work_type": work_type,
            "rec_id": rec_id,
            "rationale": rationale,
            "ts": _now(),
        })
    except Exception as e:
        logger.warning("handoff metrics out-log failed: %s", e)
    return request_id


def list_handoffs(
    agent_id: str,
    *,
    limit: int = 50,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """List pending handoffs in the agent's queue (oldest first so FIFO)."""
    s = storage or get_storage()
    prefix = f"agents/{agent_id}/handoff-queue/"
    keys = sorted(s.list_prefix(prefix))[:limit]
    out: list[dict] = []
    for k in keys:
        env = s.read_json(k)
        if env:
            out.append(env)
    return out


def drain_handoffs(
    agent_id: str,
    *,
    limit: int = 50,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """Read every pending handoff for `agent_id`, return them, and
    archive each by moving from handoff-queue/ to handoff-processed/.

    The caller is responsible for actually doing the work and calling
    `record_handoff_outcome()` once the action completes. Returning the
    list here just hands the receiver a list of (rec, work_type, ...)
    dicts to incorporate into its run plan.
    """
    s = storage or get_storage()
    pending = list_handoffs(agent_id, limit=limit, storage=s)
    drained: list[dict] = []
    for env in pending:
        request_id = env.get("request_id")
        if not request_id:
            continue
        # Move queue → processed
        try:
            s.write_json(processed_key(agent_id, request_id), env)
            s.delete(queue_key(agent_id, request_id))
        except Exception as e:
            logger.warning("handoff archive failed for %s: %s", request_id, e)
            continue
        # Record receipt
        try:
            _append_metrics_line(s, agent_id, {
                "direction": "in",
                "request_id": request_id,
                "from_agent": env.get("from_agent"),
                "work_type": env.get("work_type"),
                "rec_id": env.get("rec_id"),
                "received_at": _now(),
                "outcome": "in_progress",
            })
        except Exception as e:
            logger.warning("handoff metrics in-log failed: %s", e)
        drained.append(env)
    return drained


def record_handoff_outcome(
    *,
    agent_id: str,
    request_id: str,
    outcome: str,
    outcome_detail: str = "",
    downstream_run_ts: str = "",
    storage: Optional[StorageBackend] = None,
) -> None:
    """Append the final outcome of a previously-drained handoff.

    `outcome` is one of: 'shipped', 'deferred', 'rejected', 'in_progress'.
    Writers should call this once after acting on the handoff (or
    deciding not to). Lets the digest agent show 'X of Y handoffs
    shipped this window' instead of just 'received'.
    """
    s = storage or get_storage()
    try:
        _append_metrics_line(s, agent_id, {
            "direction": "outcome",
            "request_id": request_id,
            "outcome": outcome,
            "outcome_detail": outcome_detail,
            "downstream_run_ts": downstream_run_ts,
            "acted_at": _now(),
        })
    except Exception as e:
        logger.warning("handoff outcome log failed for %s: %s",
                        request_id, e)


def list_recent_metrics(
    agent_id: str,
    *,
    limit: int = 200,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """Read the last N rows from agents/<id>/handoffs.jsonl. Used by
    the digest agent."""
    s = storage or get_storage()
    raw = s.read_text(metrics_key(agent_id)) or ""
    rows: list[dict] = []
    for line in raw.splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _append_metrics_line(s: StorageBackend, agent_id: str, row: dict) -> None:
    """Append a JSONL row to agents/<id>/handoffs.jsonl. Best-effort
    read-modify-write — handoffs are low-frequency so we tolerate the
    O(N) cost rather than maintaining a separate appendable index."""
    key = metrics_key(agent_id)
    existing = s.read_text(key) or ""
    # Avoid double trailing newlines but keep one between rows
    if existing and not existing.endswith("\n"):
        existing += "\n"
    s.write_text(key, existing + json.dumps(row) + "\n")
