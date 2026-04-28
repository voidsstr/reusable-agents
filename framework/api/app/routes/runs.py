"""Run history + run detail."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def _entry_to_summary(agent_id: str, d: dict) -> RunSummary:
    return RunSummary(
        agent_id=agent_id,
        run_ts=d.get("run_ts", ""),
        status=d.get("status", ""),
        started_at=d.get("started_at", "") or "",
        ended_at=d.get("ended_at"),
        summary=d.get("summary", "") or "",
        iteration_count=int(d.get("iteration_count", 0) or 0),
        progress=float(d.get("progress", 0.0) or 0.0),
    )


def _list_runs_legacy(agent_id: str, limit: int, offset: int) -> list[RunSummary]:
    """Fallback: list runs by scanning the runs/ prefix and reading
    each progress.json. Used when run-index.json is missing (agents
    that haven't run since the perf upgrade)."""
    s = get_storage()
    prefix = f"agents/{agent_id}/runs/"
    keys = sorted(
        (k for k in s.list_prefix(prefix) if k.endswith("/progress.json")),
        reverse=True,
    )[offset:offset + limit]
    # Parallel read — Azure blob client is thread-safe.
    out: list[RunSummary] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda k: (k, s.read_json(k)), keys))
    for _key, d in results:
        if not d:
            continue
        out.append(_entry_to_summary(agent_id, d))
    return out


@router.get("/{agent_id}/runs", response_model=list[RunSummary])
def list_runs(agent_id: str, limit: int = Query(20, le=200), offset: int = 0):
    s = get_storage()
    # Fast path: run-index.json — last 50 summaries pre-aggregated by
    # AgentBase.post_run(). One blob read replaces 1 list + N reads.
    idx = s.read_json(f"agents/{agent_id}/run-index.json")
    if idx and idx.get("recent"):
        slice_ = idx["recent"][offset:offset + limit]
        return [_entry_to_summary(agent_id, e) for e in slice_]
    # Fallback: legacy list_prefix + parallel read.
    return _list_runs_legacy(agent_id, limit, offset)


@router.get("/{agent_id}/runs/{run_ts}")
def get_run(agent_id: str, run_ts: str):
    s = get_storage()
    base = f"agents/{agent_id}/runs/{run_ts}/"

    # Parallelize the run-detail blob reads — these are independent
    # Azure round-trips that previously serialized into ~6 × 200ms.
    tasks = {
        "progress":           ("json", base + "progress.json"),
        "context_summary_md": ("text", base + "context-summary.md"),
        "recommendations":    ("json", base + "recommendations.json"),
        "responses":          ("json", base + "responses.json"),
        "deploy":             ("json", base + "deploy.json"),
    }

    def _read(spec: tuple[str, str]):
        kind, key = spec
        try:
            return s.read_json(key) if kind == "json" else (s.read_text(key) or "")
        except Exception:
            return None if kind == "json" else ""

    results: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {name: ex.submit(_read, spec) for name, spec in tasks.items()}
        # Decisions + changelog go through helpers — kick those off too.
        future_decisions = ex.submit(read_decisions, agent_id, run_ts, s)
        for name, fut in future_map.items():
            results[name] = fut.result()
        decisions = future_decisions.result()

    progress = results.get("progress")
    if progress is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {
        "agent_id": agent_id,
        "run_ts": run_ts,
        "progress": progress,
        "decisions": decisions,
        "context_summary_md": results.get("context_summary_md") or "",
        "recommendations": results.get("recommendations"),
        "responses": results.get("responses"),
        "deploy": results.get("deploy"),
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
