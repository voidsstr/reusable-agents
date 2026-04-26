"""Global event log — feed the dashboard's activity stream."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from framework.core.status import read_recent_events

from ..auth import require_token


router = APIRouter(prefix="/api", tags=["events"], dependencies=[Depends(require_token)])


@router.get("/events")
def list_events(since: Optional[str] = None, limit: int = Query(100, le=1000)):
    return read_recent_events(since_ts=since, limit=limit)
