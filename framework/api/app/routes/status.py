"""Agent live status — polled fallback for the WebSocket push channel."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from framework.core.status import read_status

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["status"], dependencies=[Depends(require_token)])


@router.get("/{agent_id}/status")
def get_status(agent_id: str):
    status = read_status(agent_id)
    if status is None:
        raise HTTPException(status_code=404, detail="no status recorded yet")
    return status


@router.get("/{agent_id}/live-llm-output")
def get_live_llm_output(agent_id: str, tail_kb: int = 256):
    """Tail the most recent dispatch log for an agent's LLM-driven work.

    Returns the tail of the implementer's claude --print output. The
    dispatch logs live at /tmp/reusable-agents-logs/dispatch-seo-implementer-<site>-<ts>.log
    and contain the trace of the entire implementer scope including
    claude's reasoning + tool calls + final output.

    Selection logic:
      - For source agents (PI/CR/SEO/catalog-audit), find the latest
        dispatch log whose contents reference this agent's id (the
        implementer log includes the source agent's run_dir + recs).
      - For seo-implementer itself, return its most recent dispatch log.
      - For agents that don't dispatch through seo-implementer, fall
        back to /tmp/reusable-agents-logs/<agent_id>-<run_ts>.log.
    """
    log_dir = Path(os.environ.get("REUSABLE_AGENTS_LOG_DIR", "/tmp/reusable-agents-logs"))
    if not log_dir.is_dir():
        return {"agent_id": agent_id, "log_path": "",
                "content": "", "tail_bytes": 0, "mtime": None}

    target: Path | None = None

    # 1. seo-implementer's own dispatch logs are named dispatch-seo-implementer-*.log
    if agent_id == "seo-implementer":
        candidates = sorted(log_dir.glob("dispatch-seo-implementer-*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        target = candidates[0] if candidates else None

    # 2. For source agents (PI/CR/SEO/catalog-audit), the dispatch log is
    #    named dispatch-seo-implementer-<site>-<ts>.log. Find the latest
    #    one whose body references this agent_id (matches via the
    #    RESPONDER_RUN_DIR or recommendations.json path).
    if target is None:
        candidates = sorted(log_dir.glob("dispatch-seo-implementer-*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for p in candidates[:10]:  # only check 10 most recent
            try:
                head = p.read_text(errors="ignore")[:4000]
                if agent_id in head:
                    target = p
                    break
            except Exception:
                continue

    # 3. Fallback to the agent's own host-worker log (e.g. for agents
    #    that run python directly — PI/CR write status here too).
    if target is None:
        own = sorted(log_dir.glob(f"{agent_id}-*.log"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
        target = own[0] if own else None

    if target is None or not target.is_file():
        return {"agent_id": agent_id, "log_path": "",
                "content": "", "tail_bytes": 0, "mtime": None}

    # Tail the last N bytes
    tail_bytes = max(8, tail_kb) * 1024
    try:
        size = target.stat().st_size
        with open(target, "rb") as f:
            if size > tail_bytes:
                f.seek(-tail_bytes, 2)
                # Skip partial first line
                f.readline()
            data = f.read()
        content = data.decode("utf-8", errors="replace")
        mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    except Exception as e:
        return {"agent_id": agent_id, "log_path": str(target),
                "content": f"[error reading log: {e}]", "tail_bytes": 0, "mtime": None}
    return {
        "agent_id": agent_id,
        "log_path": str(target),
        "content": content,
        "tail_bytes": len(data),
        "mtime": mtime,
    }
