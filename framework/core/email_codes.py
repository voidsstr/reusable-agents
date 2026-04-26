"""Email subject codes for routing replies back to the originating agent.

Outbound emails carry a `[<agent-id>:<request-id>] <human subject>` prefix.
Inbound replies (responder-agent) extract both ids from the subject and
look up the original outbound-email metadata in storage to confirm the
mail is ours, then route the parsed action to that agent's response queue.

request-id format: `r-<UTC-yyyymmdd-HHMMSS>-<seq4>`
  - sortable lexicographically
  - monotonic per-process
  - 4-digit sequence ensures multiple emails in the same second don't collide

Also exposes confirmation-id helpers (`new_confirmation_id`,
`confirmation_id_for`) — confirmations are deterministic from method + args
so the same agent run that requested confirmation can resolve it next pass.
"""
from __future__ import annotations

import hashlib
import re
import threading
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Request-id (per outbound email)
# ---------------------------------------------------------------------------

_seq_lock = threading.Lock()
_seq_state = {"last_ts": "", "seq": 0}


def new_request_id() -> str:
    """Generate a fresh, monotonic request id.
    Format: r-YYYYMMDD-HHMMSS-NNNN
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    with _seq_lock:
        if ts == _seq_state["last_ts"]:
            _seq_state["seq"] += 1
        else:
            _seq_state["last_ts"] = ts
            _seq_state["seq"] = 1
        seq = _seq_state["seq"]
    return f"r-{ts}-{seq:04d}"


# ---------------------------------------------------------------------------
# Confirmation-id (deterministic from method + args + run)
# ---------------------------------------------------------------------------

def new_confirmation_id(
    agent_id: str,
    method_name: str,
    args_repr: str,
    run_ts: str,
) -> str:
    """Deterministic id for an idempotent confirmation request.
    Same agent + method + args + run → same id, so the next run can
    resolve it even after a process restart.
    Format: c-<run-ts>-<short-sha>
    """
    h = hashlib.sha256(
        f"{agent_id}|{method_name}|{args_repr}".encode("utf-8")
    ).hexdigest()[:10]
    return f"c-{run_ts}-{h}"


# ---------------------------------------------------------------------------
# Subject encoding / decoding
# ---------------------------------------------------------------------------

# `[<agent-id>:<request-id>] <subject>` — agent-id and request-id are kebab-case
_SUBJECT_TAG_RE = re.compile(
    r"\[(?P<agent>[a-z0-9-]+):(?P<req>r-\d{8}-\d{6}-\d{4})\]\s*"
)


def encode_subject(agent_id: str, request_id: str, subject: str) -> str:
    """Prepend the routing tag to a subject."""
    return f"[{agent_id}:{request_id}] {subject}"


def decode_subject(subject: str) -> tuple[Optional[str], Optional[str], str]:
    """Pull out (agent_id, request_id, original_subject) from a tagged subject.

    Tolerant of mail-client mutations:
      - 'Re: ' / 'Fwd: ' prefixes
      - RFC-2047 decoded subjects (caller is expected to decode first)
      - Multiple tags (returns first)
    """
    if not subject:
        return None, None, ""
    s = subject
    # Strip leading reply prefixes
    s = re.sub(r"^(Re|Fwd?|RE|FWD?|FW):\s*", "", s, flags=re.I).strip()
    m = _SUBJECT_TAG_RE.search(s)
    if not m:
        return None, None, s
    return m.group("agent"), m.group("req"), s[:m.start()] + s[m.end():].strip()
