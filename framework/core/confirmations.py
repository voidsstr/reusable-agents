"""Email-confirmation guardrail for dangerous actions.

Pattern:

    class MyAgent(AgentBase):
        @requires_confirmation(reason="commits + force-push to production")
        def deploy_to_azure(self, tag: str): ...

Behavior at call time:
  1. Compute deterministic confirmation_id from method name + args + run_ts.
  2. Look up agents/<id>/confirmations/<confirmation_id>.json:
     - approved=True → method runs.
     - rejected=True → raise ConfirmationRejected(reason).
     - pending=True or absent → enqueue an outbound email asking for
       approval, write a pending row, raise ConfirmationPending. The
       agent's run() should catch ConfirmationPending, save state, and
       exit cleanly. The next run's pre_run() drains the response queue
       and resolves the confirmation, so the next call to the same method
       with the same args will succeed.

The decorator is *declarative*: every dangerous action declares its risk
in code so reviewers see it on inspection. There's no global "dangerous"
flag; each method is gated individually.
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .email_codes import new_confirmation_id, new_request_id, encode_subject
from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.confirmations")


def confirmations_prefix(agent_id: str) -> str:
    return f"agents/{agent_id}/confirmations/"


def confirmation_key(agent_id: str, confirmation_id: str) -> str:
    return f"{confirmations_prefix(agent_id)}{confirmation_id}.json"


def outbound_email_key(agent_id: str, request_id: str) -> str:
    return f"agents/{agent_id}/outbound-emails/{request_id}.json"


def responses_queue_prefix(agent_id: str) -> str:
    return f"agents/{agent_id}/responses-queue/"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfirmationPending(Exception):
    """Raised when a dangerous action is awaiting human approval.
    The agent's run() should catch this, save state, and exit cleanly."""
    def __init__(self, confirmation_id: str, reason: str = ""):
        super().__init__(f"awaiting confirmation: {confirmation_id} ({reason})")
        self.confirmation_id = confirmation_id
        self.reason = reason


class ConfirmationRejected(Exception):
    def __init__(self, confirmation_id: str, reason: str = ""):
        super().__init__(f"confirmation rejected: {confirmation_id} ({reason})")
        self.confirmation_id = confirmation_id
        self.reason = reason


# ---------------------------------------------------------------------------
# Confirmation record + lookup
# ---------------------------------------------------------------------------

@dataclass
class ConfirmationRecord:
    confirmation_id: str
    agent_id: str
    method_name: str
    reason: str
    state: str            # 'pending' | 'approved' | 'rejected' | 'expired'
    requested_at: str
    request_id: str = ""  # the email request_id this confirmation rides on
    resolved_at: str = ""
    approved_by: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "confirmation_id": self.confirmation_id,
            "agent_id": self.agent_id,
            "method_name": self.method_name,
            "reason": self.reason,
            "state": self.state,
            "requested_at": self.requested_at,
            "request_id": self.request_id,
            "resolved_at": self.resolved_at,
            "approved_by": self.approved_by,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConfirmationRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore
        return cls(**{k: v for k, v in d.items() if k in known})


def get_confirmation(
    agent_id: str,
    confirmation_id: str,
    storage: Optional[StorageBackend] = None,
) -> Optional[ConfirmationRecord]:
    s = storage or get_storage()
    d = s.read_json(confirmation_key(agent_id, confirmation_id))
    return ConfirmationRecord.from_dict(d) if d else None


def write_confirmation(
    record: ConfirmationRecord,
    storage: Optional[StorageBackend] = None,
) -> None:
    s = storage or get_storage()
    s.write_json(confirmation_key(record.agent_id, record.confirmation_id), record.to_dict())


def list_pending_confirmations(
    agent_id: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
) -> list[ConfirmationRecord]:
    """List pending confirmations across all agents, or for one agent if given."""
    s = storage or get_storage()
    out: list[ConfirmationRecord] = []
    if agent_id:
        for key in s.list_prefix(confirmations_prefix(agent_id)):
            d = s.read_json(key)
            if d and d.get("state") == "pending":
                out.append(ConfirmationRecord.from_dict(d))
    else:
        # Walk all agents — relies on registry naming convention
        from .registry import list_agents
        for agent in list_agents(s):
            out.extend(list_pending_confirmations(agent.id, s))
    return out


def approve(
    agent_id: str,
    confirmation_id: str,
    approver: str = "",
    notes: str = "",
    storage: Optional[StorageBackend] = None,
) -> Optional[ConfirmationRecord]:
    s = storage or get_storage()
    rec = get_confirmation(agent_id, confirmation_id, s)
    if rec is None:
        return None
    rec.state = "approved"
    rec.resolved_at = _now()
    rec.approved_by = approver or "unknown"
    if notes:
        rec.notes = (rec.notes + "\n" + notes) if rec.notes else notes
    write_confirmation(rec, s)
    return rec


def reject(
    agent_id: str,
    confirmation_id: str,
    rejector: str = "",
    notes: str = "",
    storage: Optional[StorageBackend] = None,
) -> Optional[ConfirmationRecord]:
    s = storage or get_storage()
    rec = get_confirmation(agent_id, confirmation_id, s)
    if rec is None:
        return None
    rec.state = "rejected"
    rec.resolved_at = _now()
    rec.approved_by = rejector or "unknown"
    if notes:
        rec.notes = (rec.notes + "\n" + notes) if rec.notes else notes
    write_confirmation(rec, s)
    return rec


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def requires_confirmation(reason: str, *, expires_after_runs: int = 5):
    """Decorator: gate a method behind email-confirmation.

    Usage:
        @requires_confirmation(reason="deploys to production Azure")
        def deploy_to_azure(self, tag): ...

    The decorated method must be called from inside a method on an
    AgentBase subclass (so we can find self.agent_id and self.run_ts).
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            agent_id = getattr(self, "agent_id", None)
            run_ts = getattr(self, "run_ts", "")
            if not agent_id or not run_ts:
                raise RuntimeError(
                    "@requires_confirmation only works on methods of an "
                    "AgentBase subclass with .agent_id and .run_ts attrs"
                )
            args_repr = repr(args) + repr(sorted(kwargs.items()))
            confirmation_id = new_confirmation_id(
                agent_id, fn.__name__, args_repr, run_ts,
            )
            storage: StorageBackend = getattr(self, "storage", None) or get_storage()

            existing = get_confirmation(agent_id, confirmation_id, storage)
            if existing:
                if existing.state == "approved":
                    logger.info(f"[confirmations] {agent_id}.{fn.__name__} approved by {existing.approved_by}")
                    return fn(self, *args, **kwargs)
                if existing.state == "rejected":
                    raise ConfirmationRejected(confirmation_id, existing.notes)
                if existing.state == "pending":
                    raise ConfirmationPending(confirmation_id, reason)
                # 'expired' or unknown — re-request
            # No record yet → request confirmation
            request_id = new_request_id()
            record = ConfirmationRecord(
                confirmation_id=confirmation_id,
                agent_id=agent_id,
                method_name=fn.__name__,
                reason=reason,
                state="pending",
                requested_at=_now(),
                request_id=request_id,
                approved_by="",
                notes="",
            )
            write_confirmation(record, storage)

            # Send the email via the agent's outbound mailer (if one is wired).
            mailer = getattr(self, "mailer", None)
            if mailer is not None:
                subject = encode_subject(
                    agent_id, request_id,
                    f"CONFIRM: {fn.__name__}",
                )
                body = (
                    f"<h2>Confirmation requested</h2>"
                    f"<p>Agent <code>{agent_id}</code> needs your approval to:</p>"
                    f"<blockquote>{reason}</blockquote>"
                    f"<p><b>Method:</b> <code>{fn.__name__}</code><br>"
                    f"<b>Args (truncated):</b> <code>{args_repr[:400]}</code></p>"
                    f"<p>Reply with <code>yes</code> or <code>approve</code> "
                    f"to authorize, or <code>no</code> / <code>reject</code> to skip.</p>"
                    f"<p style='color:#64748b;font-size:11px'>"
                    f"Confirmation id: <code>{confirmation_id}</code></p>"
                )
                try:
                    mailer.send(subject=subject, body_html=body, request_id=request_id,
                                expects_response=True, agent_id=agent_id)
                except Exception as e:
                    logger.warning(f"confirmation email send failed: {e}")
            else:
                logger.warning(
                    f"[confirmations] {agent_id}.{fn.__name__} pending but no mailer "
                    f"wired on agent — must be approved via API/UI."
                )

            raise ConfirmationPending(confirmation_id, reason)
        return wrapper
    return decorator
