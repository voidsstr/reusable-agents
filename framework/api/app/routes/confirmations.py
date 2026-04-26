"""Pending dangerous-action confirmations.

UI/dashboard alternative path for approving (vs reply-to-email-via-responder).
Either path resolves the same ConfirmationRecord; the next agent run picks
it up via pre_run() drainage.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from framework.core import confirmations

from ..auth import require_token


router = APIRouter(prefix="/api", tags=["confirmations"], dependencies=[Depends(require_token)])


class ResolveRequest(BaseModel):
    approver: str = ""
    notes: str = ""


@router.get("/agents/{agent_id}/confirmations")
def list_for_agent(agent_id: str):
    return [r.to_dict() for r in confirmations.list_pending_confirmations(agent_id)]


@router.get("/confirmations")
def list_all_pending():
    return [r.to_dict() for r in confirmations.list_pending_confirmations(None)]


@router.get("/confirmations/pending-emails")
def get_pending_email_recs():
    """Return all outbound-emails records with expects_response=true that
    haven't been responded to yet. Surfaces pending email-recommendation
    confirmations to the dashboard's Confirmations page.
    """
    from framework.core.storage import get_storage
    from framework.core.registry import list_agents
    s = get_storage()
    out = []
    for m in list_agents(storage=s):
        outbound_prefix = f"agents/{m.id}/outbound-emails/"
        archive_prefix = f"agents/{m.id}/responses-archive/"
        outbound_keys = s.list_prefix(outbound_prefix)
        replied: set[str] = set()
        for ak in s.list_prefix(archive_prefix):
            doc = s.read_json(ak)
            if isinstance(doc, dict) and doc.get("request_id"):
                replied.add(doc["request_id"])
        for k in outbound_keys:
            d = s.read_json(k)
            if not isinstance(d, dict):
                continue
            if not d.get("expects_response"):
                continue
            if d.get("request_id") in replied:
                continue
            out.append({
                "agent_id": m.id,
                "agent_name": m.name,
                "request_id": d.get("request_id", ""),
                "subject": d.get("subject", ""),
                "to": d.get("to", []),
                "rec_count": d.get("rec_count", 0),
                "rec_ids": d.get("rec_ids", []),
                "site": d.get("site", ""),
                "run_ts": d.get("run_ts", ""),
                "sent_at": d.get("sent_at", ""),
                "kind": d.get("kind", "email-recommendations"),
            })
    out.sort(key=lambda x: x.get("sent_at", ""), reverse=True)
    return out


@router.get("/confirmations/{agent_id}/{confirmation_id}")
def get_one(agent_id: str, confirmation_id: str):
    rec = confirmations.get_confirmation(agent_id, confirmation_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="confirmation not found")
    return rec.to_dict()


@router.post("/confirmations/{agent_id}/{confirmation_id}/approve")
def approve(agent_id: str, confirmation_id: str, req: ResolveRequest):
    rec = confirmations.approve(agent_id, confirmation_id,
                                 approver=req.approver or "ui",
                                 notes=req.notes)
    if rec is None:
        raise HTTPException(status_code=404, detail="confirmation not found")
    return rec.to_dict()


@router.post("/confirmations/{agent_id}/{confirmation_id}/reject")
def reject(agent_id: str, confirmation_id: str, req: ResolveRequest):
    rec = confirmations.reject(agent_id, confirmation_id,
                                rejector=req.approver or "ui",
                                notes=req.notes)
    if rec is None:
        raise HTTPException(status_code=404, detail="confirmation not found")
    return rec.to_dict()
