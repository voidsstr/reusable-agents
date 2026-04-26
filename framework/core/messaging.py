"""Inter-agent async messaging.

Messages live at `shared/messages/<message-id>.json`. Every message is
addressed to one or more agent ids (`to`). An index at
`shared/inboxes/<agent-id>/<message-id>` (zero-byte marker) makes
"my inbox" a fast prefix listing.

Messages don't expire automatically — agents acknowledge them by
calling `mark_read()`. This keeps the historical log intact for
analytics ("what did agent X tell agent Y last month?").

Message envelope:
  {
    "schema_version": "1",
    "message_id": "m-<UTC-ts>-<seq>",
    "from": "<agent-id>",
    "to": ["<agent-id>", ...],          # multi-cast supported
    "kind": "request"|"info"|"response"|"error"|"signal",
    "subject": "...",                    # one-line summary
    "body": {...},                       # arbitrary JSON
    "in_reply_to": "<message-id>",       # optional
    "ts": "ISO ts",
    "read_by": {"<agent-id>": "ISO ts"}, # ack timestamps
  }
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.messaging")


def message_key(message_id: str) -> str:
    return f"shared/messages/{message_id}.json"


def inbox_marker_key(agent_id: str, message_id: str) -> str:
    return f"shared/inboxes/{agent_id}/{message_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_seq_lock = threading.Lock()
_seq_state = {"last_ts": "", "seq": 0}


def new_message_id() -> str:
    """Format: m-YYYYMMDD-HHMMSS-NNNN — sortable, monotonic."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    with _seq_lock:
        if ts == _seq_state["last_ts"]:
            _seq_state["seq"] += 1
        else:
            _seq_state["last_ts"] = ts
            _seq_state["seq"] = 1
        seq = _seq_state["seq"]
    return f"m-{ts}-{seq:04d}"


def send_message(
    *,
    from_agent: str,
    to_agents: list[str] | str,
    kind: str = "info",
    subject: str = "",
    body: Optional[dict] = None,
    in_reply_to: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Persist a message and update each recipient's inbox marker. Returns message_id."""
    if isinstance(to_agents, str):
        to_agents = [to_agents]
    if not to_agents:
        raise ValueError("messaging: to_agents is empty")
    s = storage or get_storage()
    msg_id = new_message_id()
    envelope: dict[str, Any] = {
        "schema_version": "1",
        "message_id": msg_id,
        "from": from_agent,
        "to": list(to_agents),
        "kind": kind,
        "subject": subject,
        "body": body or {},
        "in_reply_to": in_reply_to or "",
        "ts": _now(),
        "read_by": {},
    }
    s.write_json(message_key(msg_id), envelope)
    for recipient in to_agents:
        # Zero-byte marker; the message body lives in shared/messages/
        s.write_text(inbox_marker_key(recipient, msg_id), msg_id)
    return msg_id


def list_inbox(
    agent_id: str,
    *,
    unread_only: bool = True,
    limit: int = 100,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """Return messages addressed to this agent, newest first."""
    s = storage or get_storage()
    prefix = f"shared/inboxes/{agent_id}/"
    marker_keys = sorted(s.list_prefix(prefix), reverse=True)[:limit * 2]
    out: list[dict] = []
    for marker_key in marker_keys:
        msg_id = marker_key[len(prefix):]
        envelope = s.read_json(message_key(msg_id))
        if envelope is None:
            continue
        if unread_only and agent_id in (envelope.get("read_by") or {}):
            continue
        out.append(envelope)
        if len(out) >= limit:
            break
    return out


def mark_read(
    agent_id: str,
    message_id: str,
    storage: Optional[StorageBackend] = None,
) -> bool:
    s = storage or get_storage()
    envelope = s.read_json(message_key(message_id))
    if envelope is None:
        return False
    envelope.setdefault("read_by", {})[agent_id] = _now()
    s.write_json(message_key(message_id), envelope)
    return True


def get_message(
    message_id: str,
    storage: Optional[StorageBackend] = None,
) -> Optional[dict]:
    return (storage or get_storage()).read_json(message_key(message_id))


def list_thread(
    message_id: str,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """Walk the in_reply_to chain to find related messages.
    Best-effort linear walk — assumes messages keep one parent."""
    s = storage or get_storage()
    chain: list[dict] = []
    seen: set[str] = set()
    current_id: Optional[str] = message_id
    while current_id and current_id not in seen:
        seen.add(current_id)
        envelope = s.read_json(message_key(current_id))
        if not envelope:
            break
        chain.append(envelope)
        current_id = envelope.get("in_reply_to") or None
    return list(reversed(chain))
