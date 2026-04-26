"""Agent directives — view + propose changes (with email confirmation).

Directives are the editable operational rules the agent reads each run
(typically AGENT.md content + selected fields from goals.json). Changes
to directives go through a confirmation flow: the framework writes the
proposed change to a pending location, sends a confirmation email, and
only commits the change once approved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from framework.core import confirmations, email_codes
from framework.core.mailer import LogMailer
from framework.core.registry import get_agent
from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["directives"], dependencies=[Depends(require_token)])


def _directives_key(agent_id: str) -> str:
    return f"agents/{agent_id}/directives.json"


def _proposed_directives_key(agent_id: str, request_id: str) -> str:
    return f"agents/{agent_id}/proposed-directives/{request_id}.json"


class ProposeChangeRequest(BaseModel):
    new_content: str
    reason: str = ""
    proposed_by: str = ""


@router.get("/{agent_id}/directives")
def get_directives(agent_id: str):
    s = get_storage()
    m = get_agent(agent_id, storage=s)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    return {
        "current": s.read_json(_directives_key(agent_id)) or {},
        "manifest_summary": {
            "id": m.id, "name": m.name, "description": m.description,
            "category": m.category, "owner": m.owner,
        },
    }


@router.post("/{agent_id}/directives/propose")
def propose_change(agent_id: str, req: ProposeChangeRequest):
    """Stage a directive change pending confirmation. Returns a request_id;
    the change is applied once a confirmation reply (email or UI approve) lands."""
    s = get_storage()
    m = get_agent(agent_id, storage=s)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown agent")

    request_id = email_codes.new_request_id()
    confirmation_id = email_codes.new_confirmation_id(
        agent_id, "update_directives", req.new_content[:200], request_id,
    )

    # Stage the proposed content
    s.write_json(_proposed_directives_key(agent_id, request_id), {
        "request_id": request_id,
        "proposed_by": req.proposed_by or "ui",
        "reason": req.reason,
        "new_content": req.new_content,
        "current_directives": s.read_json(_directives_key(agent_id)) or {},
    })

    # Record the confirmation
    rec = confirmations.ConfirmationRecord(
        confirmation_id=confirmation_id,
        agent_id=agent_id,
        method_name="update_directives",
        reason=req.reason or "directive change",
        state="pending",
        requested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        request_id=request_id,
    )
    confirmations.write_confirmation(rec, s)

    # Send confirmation email (if a real mailer is wired the API would inject;
    # for now we log to storage which is enough for the UI to present the change).
    mailer = LogMailer(storage=s)
    subject = email_codes.encode_subject(
        agent_id, request_id, f"CONFIRM: directive change for {m.name}",
    )
    body = (
        f"<h2>Directive change proposed</h2>"
        f"<p>Agent <code>{agent_id}</code> directives are about to change.</p>"
        f"<p><b>Proposed by:</b> {req.proposed_by or 'unknown'}<br>"
        f"<b>Reason:</b> {req.reason or '(none)'}</p>"
        f"<details><summary>New content (preview)</summary>"
        f"<pre>{req.new_content[:2000]}</pre></details>"
        f"<p>Reply <code>yes</code> to apply, <code>no</code> to reject.</p>"
        f"<p style='color:#64748b;font-size:11px'>Confirmation id: {confirmation_id}</p>"
    )
    mailer.send(
        agent_id=agent_id, request_id=request_id,
        subject=subject, body_html=body,
        to=[m.owner] if m.owner else [],
        expects_response=True,
    )

    return {
        "ok": True,
        "request_id": request_id,
        "confirmation_id": confirmation_id,
        "status": "pending",
    }


@router.post("/{agent_id}/directives/apply/{request_id}")
def apply_proposed(agent_id: str, request_id: str):
    """Apply a previously-proposed directive change.
    Should only succeed if the matching confirmation is approved."""
    s = get_storage()
    proposed = s.read_json(_proposed_directives_key(agent_id, request_id))
    if proposed is None:
        raise HTTPException(status_code=404, detail="no proposed change for this request_id")
    # Find matching confirmation
    pending = confirmations.list_pending_confirmations(agent_id, s)
    matching = [c for c in confirmations._read_all(agent_id, s) if c.request_id == request_id] \
        if hasattr(confirmations, "_read_all") else []
    # Naive: list all confirmation files for this agent, check state
    found_confirmation = None
    for key in s.list_prefix(f"agents/{agent_id}/confirmations/"):
        rec = s.read_json(key)
        if rec and rec.get("request_id") == request_id:
            found_confirmation = rec; break
    if found_confirmation is None:
        raise HTTPException(status_code=404, detail="no confirmation record")
    if found_confirmation.get("state") != "approved":
        raise HTTPException(status_code=400,
                            detail=f"confirmation is {found_confirmation.get('state')!r} — not approved")
    # Commit: replace the directives doc
    s.write_json(_directives_key(agent_id), {
        "content": proposed["new_content"],
        "applied_at": found_confirmation.get("resolved_at"),
        "applied_via_request_id": request_id,
        "applied_by": found_confirmation.get("approved_by", ""),
    })
    return {"ok": True, "applied": True}
