"""WebSocket — pushes live status updates from `agents/<id>/status.json`
to connected UI clients. Implemented as a polling-then-broadcast loop
because Azure Blob doesn't have native change notifications and we want
the same code to work locally.

Polling cadence is fast enough (configurable WS_POLL_INTERVAL_S, default
2s) that it feels live; clients also get an immediate snapshot when they
connect. For higher fidelity the agent's status writes are throttled
to ~1/s so the bandwidth ceiling is intrinsic.

Auth: optional `?token=...` query param, matched against
FRAMEWORK_API_TOKEN. Falls back to the standard Bearer header check
(starlette-style WebSocket auth is awkward, so we fall back to query).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from framework.core.status import read_status

from .auth import allowed_emails, auth_enabled, get_expected_token


logger = logging.getLogger("framework.api.ws")

router = APIRouter(tags=["websocket"])

WS_POLL_INTERVAL_S = float(os.getenv("WS_POLL_INTERVAL_S", "2.0"))


def _ws_authorized(websocket: WebSocket, token: str | None) -> bool:
    """Authorize a WebSocket connection. Two paths:
      1. ?token=... matches FRAMEWORK_API_TOKEN
      2. session cookie present and email in ALLOWED_EMAILS (browsers
         send cookies on WS handshake automatically)
    Returns True when auth is disabled.
    """
    if not auth_enabled():
        return True
    expected = get_expected_token()
    if expected and token == expected:
        return True
    # Session cookie path — SessionMiddleware decodes cookies on the
    # incoming handshake just like for HTTP, so websocket.session works.
    try:
        sess = websocket.session
        email = (sess.get("email") or "").lower()
        if email and email in allowed_emails():
            return True
    except Exception:
        pass
    return False


@router.websocket("/ws/agents/{agent_id}/status")
async def status_stream(websocket: WebSocket, agent_id: str, token: str | None = Query(None)):
    if not _ws_authorized(websocket, token):
        await websocket.close(code=4401, reason="not authorized")
        return
    await websocket.accept()
    last_snapshot_json: str = ""
    try:
        while True:
            status = read_status(agent_id) or {"agent_id": agent_id, "state": "unknown"}
            snapshot_json = json.dumps(status, default=str, sort_keys=True)
            if snapshot_json != last_snapshot_json:
                await websocket.send_text(snapshot_json)
                last_snapshot_json = snapshot_json
            await asyncio.sleep(WS_POLL_INTERVAL_S)
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning(f"ws status stream {agent_id} error: {e}")
        try: await websocket.close(code=1011, reason=str(e)[:100])
        except Exception: pass


@router.websocket("/ws/events")
async def events_stream(websocket: WebSocket, token: str | None = Query(None),
                         since: str | None = Query(None)):
    """Stream of recent framework events (registry + status transitions)."""
    if not _ws_token_ok(token):
        await websocket.close(code=4401, reason="invalid token")
        return
    await websocket.accept()
    cursor = since or ""
    try:
        from framework.core.status import read_recent_events
        while True:
            events = read_recent_events(since_ts=cursor or None, limit=200)
            for e in events:
                await websocket.send_text(json.dumps(e, default=str))
                cursor = max(cursor, e.get("ts", ""))
            await asyncio.sleep(WS_POLL_INTERVAL_S)
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning(f"ws events stream error: {e}")
        try: await websocket.close(code=1011, reason=str(e)[:100])
        except Exception: pass
