"""Agent dependency graph — defaults + per-agent overrides.

Two endpoints:
  - GET /api/agents/dependencies     → {nodes, edges, kinds}
  - PATCH /api/agents/{id}/dependencies → replace one agent's depends_on
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from framework.core.dependencies import build_dependency_graph
from framework.core.registry import get_agent, register_agent
from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["dependencies"], dependencies=[Depends(require_token)])


class DependencyEntry(BaseModel):
    agent_id: str
    kind: str = "depends-on"
    description: str = ""


class DependenciesPatch(BaseModel):
    depends_on: list[DependencyEntry]


@router.get("/dependencies")
def get_graph(include_blueprints: bool = False):
    """Return the merged dependency graph.

    Combines framework defaults (`_DEFAULT_EDGES` in
    framework.core.dependencies) with per-agent overrides
    (manifest.depends_on). Blueprints are excluded by default.
    """
    return build_dependency_graph(include_blueprints=include_blueprints)


@router.patch("/{agent_id}/dependencies")
def patch_dependencies(agent_id: str, body: DependenciesPatch):
    """Replace this agent's depends_on list. Pass `[]` to clear."""
    s = get_storage()
    m = get_agent(agent_id, storage=s)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    m.depends_on = [e.model_dump() for e in body.depends_on]
    register_agent(m, storage=s)
    return {"ok": True, "agent_id": agent_id, "depends_on": m.depends_on}


class GraphLayout(BaseModel):
    """Per-user persisted graph layout (positions + viewport).

    Stored at `users/<user-id>/graph-layout.json` in framework storage.
    For unauthenticated browsers, the UI falls back to localStorage."""
    positions: dict[str, dict[str, float]] = {}
    viewport: dict[str, float] = {}


@router.get("/dependencies/layout/{user_id}")
def get_layout(user_id: str) -> GraphLayout:
    s = get_storage()
    raw = s.read_json(f"users/{user_id}/graph-layout.json") or {}
    return GraphLayout(**raw)


@router.put("/dependencies/layout/{user_id}")
def put_layout(user_id: str, layout: GraphLayout) -> dict[str, Any]:
    s = get_storage()
    s.write_json(f"users/{user_id}/graph-layout.json", layout.model_dump())
    return {"ok": True}
