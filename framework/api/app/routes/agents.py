"""Agent registry CRUD + discovery + trigger."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from framework.core import registry, scheduler
from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=[Depends(require_token)])


class AgentSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    category: str = "misc"
    task_type: str = "desktop-task"
    cron_expr: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    owner: str = ""
    last_run_status: str = ""
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    runnable_modes: list[str] = Field(default_factory=lambda: ["cron", "manual"])
    confirmation_flow: dict = Field(default_factory=dict)


class AgentDetail(AgentSummary):
    repo_dir: str = ""
    runbook_path: str = ""
    skill_path: str = ""
    entry_command: str = ""
    capabilities: list[str] = Field(default_factory=list)
    capabilities_detail: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    depends_on: list[dict] = Field(default_factory=list)
    runbook_body: Optional[str] = None
    skill_body: Optional[str] = None
    current_status: Optional[dict] = None
    recent_runs: list[dict] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    category: str = "misc"
    task_type: str = "desktop-task"
    cron_expr: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    repo_dir: str = ""
    runbook_path: str = ""
    skill_path: str = ""
    entry_command: str = ""
    owner: str = ""
    autowire_cron: bool = True
    metadata: dict = Field(default_factory=dict)
    depends_on: list[dict] = Field(default_factory=list)
    runnable_modes: list[str] = Field(default_factory=lambda: ["cron", "manual"])
    confirmation_flow: dict = Field(default_factory=dict)


class PatchRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    cron_expr: Optional[str] = None
    timezone: Optional[str] = None
    enabled: Optional[bool] = None
    entry_command: Optional[str] = None
    owner: Optional[str] = None
    runbook_body: Optional[str] = None
    skill_body: Optional[str] = None


class TriggerResponse(BaseModel):
    ok: bool
    run_id: str = ""
    detail: str = ""


class DiscoverRequest(BaseModel):
    agents_dir: str


def _summary(m: registry.AgentManifest) -> AgentSummary:
    s = get_storage()
    status = s.read_json(f"agents/{m.id}/status.json") or {}
    return AgentSummary(
        id=m.id, name=m.name, description=m.description, category=m.category,
        task_type=m.task_type, cron_expr=m.cron_expr, timezone=m.timezone,
        enabled=m.enabled, owner=m.owner,
        last_run_status=status.get("state", ""),
        last_run_at=status.get("updated_at"),
        runnable_modes=list(m.runnable_modes or ["cron", "manual"]),
        confirmation_flow=dict(m.confirmation_flow or {}),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[AgentSummary])
def list_all():
    return [_summary(m) for m in registry.list_agents()]


@router.post("/register", response_model=AgentSummary)
def register(req: RegisterRequest):
    s = get_storage()
    manifest = registry.AgentManifest(
        id=req.id, name=req.name or req.id, description=req.description,
        category=req.category, task_type=req.task_type, cron_expr=req.cron_expr,
        timezone=req.timezone, enabled=req.enabled, repo_dir=req.repo_dir,
        runbook_path=req.runbook_path, skill_path=req.skill_path,
        entry_command=req.entry_command, owner=req.owner,
        metadata=req.metadata,
        depends_on=list(req.depends_on or []),
        runnable_modes=list(req.runnable_modes or ["cron", "manual"]),
        confirmation_flow=dict(req.confirmation_flow or {}),
    )
    registry.register_agent(manifest, storage=s)
    if req.autowire_cron and req.cron_expr and req.entry_command:
        try:
            scheduler.write_systemd_units(
                agent_id=req.id, cron_expr=req.cron_expr,
                entry_command=req.entry_command,
                working_directory=req.repo_dir or os.path.expanduser("~"),
                timezone=req.timezone,
            )
            scheduler.reload_and_enable(req.id)
        except Exception as e:
            # Don't fail the register on systemd issues; surface in detail
            return AgentSummary(**{**_summary(manifest).dict(), "owner": f"(autowire: {e})"})
    return _summary(manifest)


@router.post("/discover")
def discover(req: DiscoverRequest):
    """Walk a directory of agent subdirs, register each."""
    try:
        result = registry.discover_agents_from_dir(req.agents_dir)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, **result}


@router.get("/{agent_id}", response_model=AgentDetail)
def get_one(agent_id: str):
    m = registry.get_agent(agent_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
    s = get_storage()
    status = s.read_json(f"agents/{agent_id}/status.json")
    runs = []
    runs_prefix = f"agents/{agent_id}/runs/"
    run_keys = sorted(
        (k for k in s.list_prefix(runs_prefix) if k.endswith("/progress.json")),
        reverse=True,
    )[:20]
    for key in run_keys:
        rd = s.read_json(key)
        if rd:
            runs.append(rd)

    runbook_body = None
    if m.runbook_path and Path(m.runbook_path).is_file():
        try: runbook_body = Path(m.runbook_path).read_text()
        except Exception: pass
    skill_body = None
    if m.skill_path and Path(m.skill_path).is_file():
        try: skill_body = Path(m.skill_path).read_text()
        except Exception: pass

    base = _summary(m).dict()
    return AgentDetail(
        **base,
        repo_dir=m.repo_dir,
        runbook_path=m.runbook_path,
        skill_path=m.skill_path,
        entry_command=m.entry_command,
        capabilities=m.capabilities,
        capabilities_detail=(m.metadata or {}).get("capabilities", []),
        metadata=m.metadata,
        depends_on=list(m.depends_on or []),
        runbook_body=runbook_body,
        skill_body=skill_body,
        current_status=status,
        recent_runs=runs,
    )


@router.patch("/{agent_id}", response_model=AgentSummary)
def patch(agent_id: str, req: PatchRequest):
    s = get_storage()
    m = registry.get_agent(agent_id, storage=s)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id!r}")
    # Body content edits (runbook / skill) write to disk, not the manifest
    if req.runbook_body is not None and m.runbook_path:
        Path(m.runbook_path).parent.mkdir(parents=True, exist_ok=True)
        Path(m.runbook_path).write_text(req.runbook_body)
    if req.skill_body is not None and m.skill_path:
        Path(m.skill_path).parent.mkdir(parents=True, exist_ok=True)
        Path(m.skill_path).write_text(req.skill_body)

    patch_dict = {
        k: v for k, v in req.dict().items()
        if k in {"name", "description", "category", "cron_expr", "timezone",
                 "enabled", "entry_command", "owner"}
        and v is not None
    }
    if patch_dict:
        registry.update_agent(agent_id, patch_dict, storage=s)

    return _summary(registry.get_agent(agent_id, storage=s))


@router.post("/{agent_id}/enable")
def enable(agent_id: str):
    m = registry.update_agent(agent_id, {"enabled": True})
    if m is None: raise HTTPException(status_code=404, detail="unknown agent")
    return {"ok": True, "enabled": True}


@router.post("/{agent_id}/disable")
def disable(agent_id: str):
    m = registry.update_agent(agent_id, {"enabled": False})
    if m is None: raise HTTPException(status_code=404, detail="unknown agent")
    return {"ok": True, "enabled": False}


@router.delete("/{agent_id}")
def deregister(agent_id: str, delete_storage: bool = False):
    ok = registry.deregister_agent(agent_id, delete_storage=delete_storage)
    if not ok: raise HTTPException(status_code=404, detail="unknown agent")
    # Best-effort: remove systemd timer if it was autowired
    try: scheduler.remove_systemd_units(agent_id)
    except Exception: pass
    return {"ok": True, "deleted_storage": delete_storage}


@router.post("/{agent_id}/trigger", response_model=TriggerResponse)
def trigger(agent_id: str):
    """Enqueue an agent run by writing a job file the host-worker picks up.

    The host-worker (`framework/api/host_worker.sh` or its predecessor in
    nsc-assistant/agents/lib/host-worker.sh) polls /tmp/agent-trigger-queue/
    every 2s and exec's the agent on the host (where it has access to
    docker, git, az, etc. — the API container can't.)
    """
    m = registry.get_agent(agent_id)
    if m is None: raise HTTPException(status_code=404, detail="unknown agent")
    if not m.entry_command:
        raise HTTPException(status_code=400, detail="agent has no entry_command")
    # Gate: agent must allow manual triggering.
    if m.runnable_modes and "manual" not in m.runnable_modes:
        raise HTTPException(
            status_code=409,
            detail=(
                f"agent {agent_id} is not manually runnable "
                f"(runnable_modes={m.runnable_modes}). It is queue-driven — "
                "an upstream agent dispatches work to it. See the dependency "
                "graph for incoming edges."
            ),
        )

    queue_dir = Path(os.getenv("AGENT_TRIGGER_QUEUE_DIR", "/tmp/agent-trigger-queue"))
    queue_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job = {
        "agent_id": agent_id,
        "run_id": run_id,
        "entry_command": m.entry_command,
        "triggered_by": "manual:api",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    job_path = queue_dir / f"{agent_id}-{run_id}.json"
    tmp = job_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2))
    tmp.rename(job_path)
    return TriggerResponse(ok=True, run_id=run_id,
                           detail=f"queued at {job_path}")
