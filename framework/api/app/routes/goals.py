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


# ---------------------------------------------------------------------------
# Change history per goal — what the agent has shipped + outcome
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/goals/changes")
def get_goal_changes(agent_id: str, goal_id: str = "", site: str = "",
                       limit: int = 200):
    """Read the goal-changes log. Optionally filter by goal_id or site."""
    from framework.core import goal_changes as gc
    return {
        "agent_id": agent_id,
        "filters": {"goal_id": goal_id or None, "site": site or None},
        "entries": gc.read_changes(
            agent_id,
            goal_id=goal_id or None,
            site=site or None,
            limit=limit,
        ),
    }


class GoalChangePost(BaseModel):
    rec_id: str
    goal_ids: list[str]
    site: str = ""
    rec_title: str = ""
    rec_category: str = ""
    rec_check_id: str = ""
    rec_severity: str = ""
    rec_tier: str = ""
    implementer_agent: str = ""
    implementer_run_ts: str = ""
    metric_before: Optional[dict[str, Any]] = None
    user_feedback: Optional[str] = None


@router.get("/{agent_id}/goals/timeseries")
def get_goal_timeseries(agent_id: str, limit_runs: int = 60):
    """Aggregate goal-progress.json across the last N runs into a
    time-series per goal_id. Each goal returns a list of measurements
    (ts, run_ts, current, baseline, target, progress_pct, status).

    The dashboard's Goals tab uses this to plot trend lines and overlay
    rec-shipped dates so you can see what each shipped change moved.
    """
    if get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    s = get_storage()
    runs_prefix = f"agents/{agent_id}/runs/"
    # List all run dir basenames; sort desc by name (run-ts is sortable)
    seen_runs: set[str] = set()
    try:
        for k in s.list_prefix(runs_prefix):
            tail = k[len(runs_prefix):]
            if "/" not in tail:
                continue
            run_id = tail.split("/", 1)[0]
            seen_runs.add(run_id)
    except Exception:
        pass
    sorted_runs = sorted(seen_runs, reverse=True)[:limit_runs]
    # Per-goal time series: goal_id → list of measurement dicts
    series: dict[str, dict[str, Any]] = {}  # goal_id → {goal_meta, points: [...]}
    for run_id in sorted_runs:
        gp_key = f"{runs_prefix}{run_id}/goal-progress.json"
        try:
            gp = s.read_json(gp_key)
        except Exception:
            continue
        if not isinstance(gp, dict):
            continue
        measured_at = gp.get("measured_at") or run_id
        for g in gp.get("goals", []):
            gid = g.get("id")
            if not gid:
                continue
            entry = series.setdefault(gid, {
                "goal_id": gid,
                "description": g.get("description", ""),
                "target_metric": g.get("target_metric", ""),
                "baseline": g.get("baseline"),
                "target": g.get("target"),
                "from_rec": g.get("from_rec", ""),
                "is_top5_goal": g.get("is_top5_goal", False),
                "is_revenue_goal": g.get("is_revenue_goal", False),
                "rationale": g.get("rationale", ""),
                "check_by": g.get("check_by", ""),
                "points": [],
            })
            # Always overwrite description/target as the latest run is canonical
            entry["description"] = g.get("description", entry["description"])
            entry["target"] = g.get("target", entry["target"])
            entry["points"].append({
                "ts": measured_at,
                "run_ts": run_id,
                "current": g.get("current"),
                "progress_pct": g.get("progress_pct"),
                "status": g.get("status", ""),
            })
    # Sort points ascending by ts (oldest first) for chart rendering
    for entry in series.values():
        entry["points"].sort(key=lambda p: p.get("ts") or "")

    # Annotations: recs that have been shipped (per source recommendations.json
    # across runs). Each annotation: {ts, rec_id, title, goal_id, kind}.
    annotations: list[dict[str, Any]] = []
    for run_id in sorted_runs:
        rk = f"{runs_prefix}{run_id}/recommendations.json"
        try:
            rd = s.read_json(rk)
        except Exception:
            continue
        if not isinstance(rd, dict):
            continue
        for r in rd.get("recommendations", []):
            if not r.get("shipped") and not r.get("implemented"):
                continue
            rid = r.get("id")
            # Map rec → goal via from_rec field already in series
            gids = [gid for gid, ent in series.items() if ent.get("from_rec") == rid]
            for gid in gids:
                annotations.append({
                    "ts": r.get("shipped_at") or r.get("implemented_at") or "",
                    "rec_id": rid,
                    "title": r.get("title", "")[:120],
                    "goal_id": gid,
                    "kind": "shipped" if r.get("shipped") else "implemented",
                })
    # Dedupe annotations
    seen_ann: set[tuple[str, str, str]] = set()
    deduped = []
    for a in annotations:
        key = (a["ts"], a["rec_id"], a["goal_id"])
        if key in seen_ann:
            continue
        seen_ann.add(key)
        deduped.append(a)
    deduped.sort(key=lambda a: a["ts"])
    return {
        "agent_id": agent_id,
        "runs_scanned": len(sorted_runs),
        "goal_count": len(series),
        "goals": list(series.values()),
        "annotations": deduped,
    }


@router.post("/{agent_id}/goals/changes")
def post_goal_change(agent_id: str, body: GoalChangePost):
    """Record a new change. The responder calls this when it dispatches
    an `implement` action against an agent's recs."""
    from framework.core import goal_changes as gc
    if get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    entry = gc.record_goal_change(
        agent_id=agent_id,
        rec_id=body.rec_id,
        goal_ids=body.goal_ids,
        site=body.site,
        rec_title=body.rec_title,
        rec_category=body.rec_category,
        rec_check_id=body.rec_check_id,
        rec_severity=body.rec_severity,
        rec_tier=body.rec_tier,
        implementer_agent=body.implementer_agent,
        implementer_run_ts=body.implementer_run_ts,
        metric_before=body.metric_before,
        user_feedback=body.user_feedback,
    )
    return {"ok": True, "entry": entry}
