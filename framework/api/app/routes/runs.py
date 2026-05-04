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
    """Fallback: list runs by scanning the runs/ prefix.

    Handles two run-dir shapes:
      • AgentBase shape: <ts>/progress.json  (PI, article-author, etc.)
      • SEO orchestrator shape: <ts>/recommendations.json + goals.json
        + snapshot.json + run-summary.md  (no progress.json — the
        SEO pipeline is multi-step and doesn't go through AgentBase)

    Both encode the same canonical timestamp pattern (<8d>T<6d>Z) as
    the dir name, so we discover by listing canonical-shaped dirs +
    fetching whichever marker file each one has.
    """
    import re as _re
    s = get_storage()
    prefix = f"agents/{agent_id}/runs/"
    ts_re = _re.compile(r"^\d{8}T\d{6}Z$")

    # Collect canonical run timestamps (filter out implementer dispatch
    # tempdirs named `rundir-<agent>-<ts>-<rand>` — those are downstream
    # work, not original agent runs).
    ts_set: set[str] = set()
    for k in s.list_prefix(prefix):
        rest = k[len(prefix):]
        if "/" not in rest:
            continue
        first = rest.split("/", 1)[0]
        if ts_re.match(first):
            ts_set.add(first)
    sorted_ts = sorted(ts_set, reverse=True)
    page = sorted_ts[offset:offset + limit]
    if not page:
        return []

    # For each ts, try progress.json first (AgentBase), fall back to
    # recommendations.json + run-summary.md (SEO/orchestrator). Synthesize
    # a RunSummary that the dashboard can render.
    def _fetch(ts: str) -> "RunSummary | None":
        # Fast path: AgentBase shape
        d = s.read_json(f"{prefix}{ts}/progress.json")
        if d:
            return _entry_to_summary(agent_id, d)
        # SEO/orchestrator shape — synthesize from recommendations.json
        rd = s.read_json(f"{prefix}{ts}/recommendations.json") or {}
        recs = rd.get("recommendations") or []
        n_recs = len(recs) if isinstance(recs, list) else 0
        meta = rd.get("metadata") or {}
        short_circuited = bool(meta.get("short_circuited"))
        summary = (
            f"short-circuit: replayed {n_recs} recs from "
            f"{meta.get('replayed_from_run','prior run')}"
            if short_circuited else
            f"{n_recs} recommendations"
        )
        # status: best effort — if a deploy.json exists with success,
        # we know it shipped; else success-by-default for a run that
        # produced output.
        deploy = s.read_json(f"{prefix}{ts}/deploy.json") or {}
        status = "success"
        if deploy.get("status") == "failure":
            status = "failure"
        # Infer started_at from the run-ts itself (canonical UTC stamp).
        started_iso = ""
        try:
            started_iso = (
                f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T"
                f"{ts[9:11]}:{ts[11:13]}:{ts[13:15]}+00:00"
            )
        except Exception:
            pass
        return RunSummary(
            agent_id=agent_id, run_ts=ts,
            status=status, started_at=started_iso,
            ended_at=None, summary=summary,
            iteration_count=0, progress=1.0,
        )

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_fetch, page))
    return [r for r in results if r]


@router.get("/{agent_id}/runs", response_model=list[RunSummary])
def list_runs(agent_id: str, limit: int = Query(20, le=200), offset: int = 0):
    s = get_storage()
    # Fast path: run-index.json — last 50 summaries pre-aggregated by
    # AgentBase.post_run(). One blob read replaces 1 list + N reads.
    idx = s.read_json(f"agents/{agent_id}/run-index.json")
    if idx is not None and isinstance(idx.get("recent"), list):
        # Trust the index even when `recent` is empty AS LONG AS the
        # index reports total_runs > 0. An empty recent + total>0 means
        # the index is stale (older than the actual runs); fall through
        # to legacy + best-effort rebuild on the side.
        recent = idx["recent"]
        total = int(idx.get("total_runs") or 0)
        if recent or (total == 0):
            slice_ = recent[offset:offset + limit]
            return [_entry_to_summary(agent_id, e) for e in slice_]
        # Empty recent but agent has runs → index is stale; fall through.
    # Fallback: legacy list_prefix + parallel read.
    out = _list_runs_legacy(agent_id, limit, offset)
    # Self-heal: if we just walked the prefix and got real summaries,
    # rebuild the run-index.json for next time. Best-effort, never block.
    # Only fires when the existing index was stale or missing — so the
    # cost of rebuilding amortizes across many subsequent fast-path
    # responses.
    if out and (idx is None or not idx.get("recent")):
        try:
            _rebuild_run_index_async(agent_id, out)
        except Exception:
            pass
    return out


def _rebuild_run_index_async(agent_id: str, recent: list) -> None:
    """Best-effort: write a fresh run-index.json with the legacy-path
    summaries we just computed. Done in a daemon thread so the user-
    facing response doesn't wait on the write. The next list_runs call
    hits the fast path."""
    import threading as _t
    s = get_storage()

    def _do():
        try:
            recent_dicts = [_entry_to_dict(r) for r in recent[:50]]
            s.write_json(f"agents/{agent_id}/run-index.json", {
                "total_runs": len(recent),
                "recent": recent_dicts,
                "updated_at_iso": "",
                "rebuilt_by": "api-list_runs-fallback",
            })
        except Exception:
            pass

    th = _t.Thread(target=_do, daemon=True, name=f"run-index-rebuild-{agent_id}")
    th.start()


def _entry_to_dict(r: "RunSummary") -> dict:
    """RunSummary → dict in the shape AgentBase writes to run-index.json."""
    return {
        "run_ts": r.run_ts,
        "status": r.status,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "summary": r.summary,
        "iteration_count": r.iteration_count,
        "progress": r.progress,
    }


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
