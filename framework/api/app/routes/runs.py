"""Run history + run detail."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from framework.core.decision_log import read_decisions, read_changelog
from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["runs"], dependencies=[Depends(require_token)])


class RunSummary(BaseModel):
    agent_id: str
    run_ts: str
    status: str
    started_at: str = ""
    ended_at: Optional[str] = None
    summary: str = ""
    iteration_count: int = 0
    progress: float = 0.0


@router.get("/{agent_id}/runs", response_model=list[RunSummary])
def list_runs(agent_id: str, limit: int = Query(20, le=200), offset: int = 0):
    s = get_storage()
    prefix = f"agents/{agent_id}/runs/"
    keys = sorted(
        (k for k in s.list_prefix(prefix) if k.endswith("/progress.json")),
        reverse=True,
    )[offset:offset + limit]
    out = []
    for key in keys:
        d = s.read_json(key)
        if not d:
            continue
        out.append(RunSummary(
            agent_id=agent_id,
            run_ts=d.get("run_ts", ""),
            status=d.get("status", ""),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at"),
            summary=d.get("summary", ""),
            iteration_count=int(d.get("iteration_count", 0)),
            progress=float(d.get("progress", 0.0)),
        ))
    return out


@router.get("/{agent_id}/runs/{run_ts}")
def get_run(agent_id: str, run_ts: str):
    s = get_storage()
    base = f"agents/{agent_id}/runs/{run_ts}/"
    progress = s.read_json(base + "progress.json")
    if progress is None:
        raise HTTPException(status_code=404, detail="run not found")
    decisions = read_decisions(agent_id, run_ts, storage=s)
    context_summary_md = s.read_text(base + "context-summary.md") or ""
    recommendations = s.read_json(base + "recommendations.json")
    responses = s.read_json(base + "responses.json")
    deploy = s.read_json(base + "deploy.json")
    return {
        "agent_id": agent_id,
        "run_ts": run_ts,
        "progress": progress,
        "decisions": decisions,
        "context_summary_md": context_summary_md,
        "recommendations": recommendations,
        "responses": responses,
        "deploy": deploy,
    }


@router.get("/{agent_id}/changelog")
def changelog(agent_id: str, limit: int = Query(50, le=500)):
    return read_changelog(agent_id, limit=limit)


@router.get("/{agent_id}/runs/{run_ts}/artifacts")
def list_run_artifacts(agent_id: str, run_ts: str):
    """List every blob under agents/<id>/runs/<run-ts>/ with kind hints so
    the UI can pick the right viewer (json / jsonl / html / markdown / text)."""
    s = get_storage()
    prefix = f"agents/{agent_id}/runs/{run_ts}/"
    keys = s.list_prefix(prefix)
    out = []
    for k in keys:
        rel = k[len(prefix):]
        if not rel:
            continue
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        kind = (
            "json" if ext == "json"
            else "jsonl" if ext == "jsonl"
            else "html" if ext == "html"
            else "markdown" if ext in ("md", "markdown")
            else "text"
        )
        out.append({"key": k, "name": rel, "ext": ext, "kind": kind})
    out.sort(key=lambda x: x["name"])
    return {"agent_id": agent_id, "run_ts": run_ts, "artifacts": out}
