"""Implementer dispatch queue — lists pending responses-queue items and
recent dispatch log files so the ImplementerQueue UI page can show what's
been queued, what's running, and what recently completed."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/implementer", tags=["dispatch"], dependencies=[Depends(require_token)])

_LOG_DIR = Path(os.getenv("AGENT_LOG_DIR", "/tmp/reusable-agents-logs"))
_DISPATCH_PAT = re.compile(r"dispatch-seo-implementer-([a-z]+)-(\d{8}T\d{6}Z)\.log$")
_REC_IDS_PAT = re.compile(r"\[implementer\] recs=([^\n\r]+)")
_COMMIT_PAT = re.compile(r"\[implementer\] committed ([a-f0-9]{7,40})")
_DONE_PAT = re.compile(r"\[implementer\] done")


def _parse_dispatch_log(path: Path) -> dict:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        text = ""
    rec_ids: list[str] = []
    m = _REC_IDS_PAT.search(text)
    if m:
        raw = m.group(1).strip()
        rec_ids = [r.strip().strip("'\"[]") for r in raw.split(",") if r.strip()]
    commit_sha = ""
    cm = _COMMIT_PAT.search(text)
    if cm:
        commit_sha = cm.group(1)
    done = bool(_DONE_PAT.search(text))
    # last 4 kB for the tail snippet
    tail = text[-4096:] if len(text) > 4096 else text
    return {
        "rec_ids": rec_ids,
        "rec_count": len(rec_ids),
        "commit_sha": commit_sha,
        "done": done,
        "tail": tail,
    }


def _list_dispatches(limit: int = 20) -> list[dict]:
    if not _LOG_DIR.is_dir():
        return []
    results = []
    for p in sorted(_LOG_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        m = _DISPATCH_PAT.match(p.name)
        if not m:
            continue
        site, run_ts = m.group(1), m.group(2)
        stat = p.stat()
        parsed = _parse_dispatch_log(p)
        # "running" = file modified in last 5 min AND not done
        age_s = datetime.now(timezone.utc).timestamp() - stat.st_mtime
        running = (not parsed["done"]) and age_s < 300
        started = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
        results.append({
            "id": f"{site}-{run_ts}",
            "site": site,
            "run_ts": run_ts,
            "log_filename": p.name,
            "started_at": started,
            "size_bytes": stat.st_size,
            "status": "running" if running else "completed",
            **parsed,
        })
        if len(results) >= limit:
            break
    return results


@router.get("/queue")
def get_queue(limit: int = Query(20, le=100)):
    """Return pending responses-queue items across all agents + recent dispatches."""
    s = get_storage()

    # Collect pending queue items from all agent response queues
    pending: list[dict] = []
    for agent_prefix in s.list_prefix("agents/"):
        parts = agent_prefix.split("/")
        if len(parts) < 2:
            continue
        agent_id = parts[1]
        if not agent_id:
            continue
        for key in s.list_prefix(f"agents/{agent_id}/responses-queue/"):
            d = s.read_json(key)
            if d:
                pending.append({"agent_id": agent_id, "_key": key, **d})

    pending.sort(key=lambda r: r.get("ts", ""), reverse=True)

    dispatches = _list_dispatches(limit)

    return {
        "pending": pending[:limit],
        "dispatches": dispatches,
    }


@router.get("/dispatches")
def list_dispatches(limit: int = Query(20, le=100)):
    return _list_dispatches(limit)


@router.get("/dispatches/{dispatch_id}/log")
def get_dispatch_log(dispatch_id: str, tail_bytes: int = Query(32768, le=262144)):
    """Return the content of a specific dispatch log."""
    if not _LOG_DIR.is_dir():
        return {"content": "", "size_bytes": 0, "status": "not_found"}
    # dispatch_id is "<site>-<run_ts>"
    for p in _LOG_DIR.iterdir():
        m = _DISPATCH_PAT.match(p.name)
        if not m:
            continue
        site, run_ts = m.group(1), m.group(2)
        if f"{site}-{run_ts}" == dispatch_id:
            stat = p.stat()
            try:
                if tail_bytes and stat.st_size > tail_bytes:
                    with p.open("rb") as f:
                        f.seek(-tail_bytes, 2)
                        content = f.read().decode("utf-8", errors="replace")
                else:
                    content = p.read_text(errors="replace")
            except Exception as e:
                content = f"[error reading log: {e}]"
            parsed = _parse_dispatch_log(p)
            age_s = datetime.now(timezone.utc).timestamp() - stat.st_mtime
            running = (not parsed["done"]) and age_s < 300
            return {
                "id": dispatch_id,
                "site": site,
                "run_ts": run_ts,
                "content": content,
                "size_bytes": stat.st_size,
                "status": "running" if running else "completed",
                **parsed,
            }
    return {"content": "", "size_bytes": 0, "status": "not_found", "id": dispatch_id}
