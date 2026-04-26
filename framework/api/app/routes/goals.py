"""Agent goals — persistent objectives the dashboard surfaces in a Goals tab.

Endpoints:
  GET  /api/agents/{id}/goals             — full goal set + progress
  PUT  /api/agents/{id}/goals             — replace goal set (schema-validated)
  POST /api/agents/{id}/goals/{goal_id}/progress — append a progress entry
  GET  /api/agents/{id}/goals/accomplished       — accomplished log
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from framework.core import goals as goals_mod
from framework.core.registry import get_agent
from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["goals"], dependencies=[Depends(require_token)])


@router.get("/{agent_id}/goals")
def get_goals(agent_id: str):
    if get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    s = get_storage()
    doc = s.read_json(goals_mod.goals_key(agent_id)) or {
        "schema_version": "1", "agent_id": agent_id, "goals": [],
    }
    return doc


class GoalsPut(BaseModel):
    goals: list[dict[str, Any]]


@router.put("/{agent_id}/goals")
def put_goals(agent_id: str, body: GoalsPut):
    if get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    try:
        doc = goals_mod.init_goals(agent_id, body.goals)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid goals: {e}")
    return doc


class ProgressPost(BaseModel):
    value: float
    run_ts: str = ""
    note: str = ""
    accomplished: Optional[bool] = None


@router.post("/{agent_id}/goals/{goal_id}/progress")
def post_progress(agent_id: str, goal_id: str, body: ProgressPost):
    if get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    try:
        doc = goals_mod.record_goal_progress(
            agent_id, goal_id, body.value,
            run_ts=body.run_ts, note=body.note,
            accomplished=body.accomplished,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return doc


@router.get("/{agent_id}/goals/accomplished")
def get_accomplished(agent_id: str):
    s = get_storage()
    return {"agent_id": agent_id,
            "entries": s.read_jsonl(f"agents/{agent_id}/goals/accomplished.jsonl") or []}
