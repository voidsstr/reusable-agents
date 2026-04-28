"""Agent live status — polled fallback for the WebSocket push channel."""
from __future__ import annotations

import json
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
def get_live_llm_output(agent_id: str, tail_kb: int = 256, since_offset: int = 0):
    """Live LLM input/output for the dashboard's Live LLM tab.

    Three storage paths, in priority order (each falls through to the
    next on miss/error):

      1. **Live tail blob** (fast-path — single pre-rendered Azure
         blob read, sub-200ms typical):
         agents/<agent_id>/live-llm-output.txt
         Format: `__META__: {json}\\n` header line followed by the last
         ~256KB of human-readable rendered output. Refreshed every ~3s
         by the agent's `LiveLLMTail` writer. The dashboard polls this
         every 1-2s; this is the hot path. is_active flips to false on
         post_run() so the UI knows when a run ended.

      2. **JSONL stream replay** (durable — works for ALL providers,
         survives across hosts, structured records):
         agents/<agent_id>/runs/<latest_run_ts>/llm-output.jsonl
         Used when the live-tail blob is missing (older runs, or a
         brand-new run before its first flush). Renders records on the
         fly so the UI sees the same shape.

      3. **Local /tmp dispatch log** (legacy — bash-script implementer
         scope output, only on the host that ran the dispatch):
         /tmp/reusable-agents-logs/dispatch-implementer-<site>-<ts>.log
         Used as fallback for the implementer scope itself, which writes
         to stdout via shell-script `claude --print`.

    Returns:
      {
        agent_id,
        source:      'azure-live-blob' | 'framework-storage' | 'local-fs' | 'none',
        run_ts,      log_path,
        content:     rendered text the UI's <pre> renders,
        is_active:   bool — true while a run is in progress,
        started_at,  updated_at,
        records:     structured records (framework-storage path only),
        tail_bytes,  since_offset (next byte offset for incremental fetch),
        mtime
      }
    """
    # 1. Fast-path — pre-rendered live-tail blob.
    try:
        from framework.core import llm_stream
        from framework.core.storage import get_storage
        s = get_storage()
        body = s.read_text(llm_stream.live_llm_tail_key(agent_id)) or ""
        if body.startswith("__META__: "):
            nl = body.find("\n")
            meta_line = body[len("__META__: "):nl] if nl > 0 else body[len("__META__: "):]
            content = body[nl + 1:] if nl > 0 else ""
            try:
                meta = json.loads(meta_line)
            except Exception:
                meta = {}
            return {
                "agent_id":   agent_id,
                "source":     "azure-live-blob",
                "run_ts":     meta.get("run_ts"),
                "log_path":   llm_stream.live_llm_tail_key(agent_id),
                "content":    content,
                "is_active":  bool(meta.get("is_active", False)),
                "started_at": meta.get("started_at"),
                "updated_at": meta.get("updated_at"),
                "tail_bytes": len(content.encode("utf-8")) if content else 0,
                "since_offset": len(body),
                "mtime":      meta.get("updated_at"),
            }
    except Exception:
        # Fall through; we'll try the JSONL replay path next.
        pass

    # 2. JSONL replay — durable, all-providers path.
    try:
        from framework.core import llm_stream
        from framework.core.storage import get_storage
        s = get_storage()
        # Most recent run_ts via status.json or run-dir scan.
        run_ts = llm_stream.latest_run_ts(agent_id, storage=s)
        if run_ts:
            key = llm_stream.llm_stream_key(agent_id, run_ts)
            text = s.read_text(key) or ""
            if text:
                # Slice for incremental tailing.
                effective = text[since_offset:] if since_offset and since_offset < len(text) else text
                # Parse records.
                records = []
                for line in effective.splitlines():
                    line = line.strip()
                    if not line: continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
                content = llm_stream._render_records_for_tail(records)
                # Best-effort is_active: read run progress.json — if
                # ended_at is set, is_active=false.
                is_active = True
                try:
                    progress = s.read_json(f"agents/{agent_id}/runs/{run_ts}/progress.json") or {}
                    if progress.get("ended_at"):
                        is_active = False
                except Exception:
                    pass
                return {
                    "agent_id": agent_id,
                    "source": "framework-storage",
                    "run_ts": run_ts,
                    "log_path": key,
                    "content": content,
                    "is_active": is_active,
                    "started_at": (records[0].get("ts") if records else None),
                    "updated_at": (records[-1].get("ts") if records else None),
                    "records": records,
                    "tail_bytes": len(effective),
                    "since_offset": len(text),  # next call passes this back
                    "mtime": (records[-1].get("ts") if records else None),
                }
    except Exception:
        # Fall through to local-FS on any framework-storage error.
        pass

    # 2. Local /tmp fallback — only useful for the bash implementer.
    log_dir = Path(os.environ.get("REUSABLE_AGENTS_LOG_DIR", "/tmp/reusable-agents-logs"))
    if not log_dir.is_dir():
        return {"agent_id": agent_id, "source": "none", "log_path": "",
                "content": "", "tail_bytes": 0, "mtime": None}

    target: Path | None = None

    # 1. implementer's own dispatch logs are named dispatch-implementer-*.log
    if agent_id == "implementer":
        candidates = sorted(log_dir.glob("dispatch-implementer-*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        target = candidates[0] if candidates else None

    # 2. For source agents (PI/CR/SEO/catalog-audit), the dispatch log is
    #    named dispatch-implementer-<site>-<ts>.log. Find the latest
    #    one whose body references this agent_id (matches via the
    #    RESPONDER_RUN_DIR or recommendations.json path).
    if target is None:
        candidates = sorted(log_dir.glob("dispatch-implementer-*.log"),
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
