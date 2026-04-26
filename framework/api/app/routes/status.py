"""Agent live status — polled fallback for the WebSocket push channel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from framework.core.status import read_status

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["status"], dependencies=[Depends(require_token)])


@router.get("/{agent_id}/status")
def get_status(agent_id: str):
    status = read_status(agent_id)
    if status is None:
        raise HTTPException(status_code=404, detail="no status recorded yet")
    return status
