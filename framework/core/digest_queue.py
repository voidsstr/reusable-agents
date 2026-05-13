"""Digest queue — single primitive for funneling per-agent emails into
the periodic digest rollup.

Writes `digest-queue/<ts>-<hash>.json`. The `digest-rollup-agent`
drains this prefix every few hours and renders ONE consolidated email
to the operator instead of N flood emails from N agents.

Replaces the prior pattern where each agent had its own DIGEST_ONLY
gate + inline JSON shape. Producer agents now call `queue(...)` and
the contract lives in this one file.

Used by `AgentBase.queue_for_digest(...)` shorthand.
"""
from __future__ import annotations

import datetime as _dt
import hashlib as _hashlib
from typing import Optional

from .storage import StorageBackend, get_storage


def queue(
    *,
    agent: str,
    subject: str,
    body_html: str,
    to: Optional[list[str]] = None,
    sender: str = "",
    site: str = "",
    extra_headers: Optional[dict] = None,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Write a digest-queue entry. Returns the storage key.

    Args:
      agent         producer agent_id (used for filtering in the rollup
                    renderer — e.g. SEO entries get an inline section)
      subject       human subject line
      body_html     rendered HTML the rollup will embed under <details>
      to            recipients (informational; rollup decides actual To)
      sender        From: address (informational)
      site          short site label so the rollup can group per site
      extra_headers free-form tags (X-Reusable-Agent-Site, etc.)
    """
    s = storage or get_storage()
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    key_hash = _hashlib.sha1(f"{subject}|{ts}".encode()).hexdigest()[:10]
    headers = dict(extra_headers or {})
    headers.setdefault("X-Reusable-Agent", agent)
    if site:
        headers.setdefault("X-Reusable-Agent-Site", site)
    key = f"digest-queue/{ts}-{key_hash}.json"
    s.write_json(key, {
        "ts": ts,
        "agent": agent,
        "subject": subject,
        "body_html": (body_html or "")[:30000],
        "to": list(to or []),
        "sender": sender,
        "extra_headers": headers,
    })
    return key
