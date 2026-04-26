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
