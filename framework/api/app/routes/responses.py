"""Per-agent response queue (parsed user replies awaiting agent pickup)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["responses"], dependencies=[Depends(require_token)])


@router.get("/{agent_id}/responses")
def list_pending(agent_id: str, include_archive: bool = False, limit: int = Query(100, le=1000)):
    s = get_storage()
    out = []
    for key in s.list_prefix(f"agents/{agent_id}/responses-queue/")[:limit]:
        d = s.read_json(key)
        if d:
            out.append({**d, "_status": "pending", "_key": key})
    if include_archive:
        for key in s.list_prefix(f"agents/{agent_id}/responses-archive/")[:limit]:
            d = s.read_json(key)
            if d:
                out.append({**d, "_status": "archived", "_key": key})
    out.sort(key=lambda r: r.get("ts", "") + r.get("request_id", ""), reverse=True)
    return out[:limit]


@router.delete("/{agent_id}/responses/{request_id}")
def delete_response(agent_id: str, request_id: str):
    s = get_storage()
    deleted_any = False
    for prefix in (
        f"agents/{agent_id}/responses-queue/{request_id}.json",
        f"agents/{agent_id}/responses-archive/{request_id}.json",
    ):
        if s.delete(prefix):
            deleted_any = True
    if not deleted_any:
        raise HTTPException(status_code=404, detail="response not found")
    return {"ok": True}
