"""Outbound-email recorder — single primitive for the dashboard's
Confirmations page to surface "an email was sent on behalf of agent X
that may receive a reply".

Writes `agents/<agent_id>/outbound-emails/<request_id>.json`.

Used by `AgentBase.record_outbound(...)` shorthand. Separate from the
digest_queue + implementation_queue primitives because this is the
*record of intent* — it's what the dashboard displays — whereas the
others are work-in-flight queues.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


def record(
    *,
    agent_id: str,
    run_ts: str,
    request_id: str,
    subject: str,
    body_hash: str = "",
    body_excerpt: str = "",
    to: Optional[list[str]] = None,
    expects_response: bool = False,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Record an outbound email. Returns the storage key.

    Args:
      agent_id          producing agent
      run_ts            canonical run-ts of the run that generated the email
      request_id        stable id used to route any reply back to this run
                        (typically `r-<ts>-<tag>-<site>`)
      subject           email subject
      body_hash         sha1/sha256 of body_html (optional — for dedup
                        across retries)
      body_excerpt      first ~2000 chars of body for the Confirmations
                        UI preview
      to                recipients
      expects_response  True if the operator is expected to reply (the
                        Confirmations page badges these differently)
    """
    s = storage or get_storage()
    sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = f"agents/{agent_id}/outbound-emails/{request_id}.json"
    s.write_json(key, {
        "schema_version": "1",
        "request_id": request_id,
        "run_ts": run_ts,
        "sent_at": sent_at,
        "subject": subject,
        "body_hash": body_hash,
        "body_excerpt": (body_excerpt or "")[:2000],
        "to": list(to or []),
        "expects_response": bool(expects_response),
    })
    return key
