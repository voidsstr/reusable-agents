"""Inter-agent messaging API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from framework.core import messaging

from ..auth import require_token


router = APIRouter(prefix="/api", tags=["messages"], dependencies=[Depends(require_token)])


class SendMessageRequest(BaseModel):
    from_agent: str
    to_agents: list[str]
    kind: str = "info"
    subject: str = ""
    body: dict = Field(default_factory=dict)
    in_reply_to: Optional[str] = None


@router.get("/agents/{agent_id}/messages")
def list_inbox(agent_id: str, unread_only: bool = True, limit: int = Query(50, le=500)):
    return messaging.list_inbox(agent_id, unread_only=unread_only, limit=limit)


@router.post("/messages")
def send(req: SendMessageRequest):
    if not req.to_agents:
        raise HTTPException(status_code=400, detail="to_agents is empty")
    msg_id = messaging.send_message(
        from_agent=req.from_agent, to_agents=req.to_agents,
        kind=req.kind, subject=req.subject, body=req.body,
        in_reply_to=req.in_reply_to,
    )
    return {"ok": True, "message_id": msg_id}


@router.post("/messages/{message_id}/mark-read")
def mark_read(message_id: str, agent_id: str):
    if not messaging.mark_read(agent_id, message_id):
        raise HTTPException(status_code=404, detail="message not found")
    return {"ok": True}


@router.get("/messages/{message_id}")
def get_message(message_id: str):
    msg = messaging.get_message(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    return msg


@router.get("/messages/{message_id}/thread")
def get_thread(message_id: str):
    return messaging.list_thread(message_id)
