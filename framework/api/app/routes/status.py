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

    Two storage paths, in priority order:

      1. **Framework storage** (preferred — works for ALL providers,
         survives across hosts, durable):
         agents/<agent_id>/runs/<latest_run_ts>/llm-output.jsonl
         Each AIClient.chat() call appends request + response (or
         streamed chunks for claude-cli) as JSONL records here.
         since_offset enables incremental tailing.

      2. **Local /tmp dispatch log** (legacy — bash-script implementer
         scope output, only on the host that ran the dispatch):
         /tmp/reusable-agents-logs/dispatch-implementer-<site>-<ts>.log
         Used as fallback for the implementer scope itself, which writes
         to stdout via shell-script `claude --print`.

    Returns:
      {
        agent_id, source: 'framework-storage'|'local-fs',
        run_ts, log_path, content (rendered text),
        records (only when source=framework-storage; structured),
        tail_bytes, since_offset (next byte offset for incremental fetch),
        mtime
      }
    """
    # 1. Framework storage path — this is the durable, all-providers path.
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
                # Render text view for backward compat with the old UI.
                rendered_lines = []
                for r in records:
                    kind = r.get("kind")
                    ts = r.get("ts", "")
                    if kind == "request":
                        rendered_lines.append(f"\n=== {ts} | REQUEST · {r.get('model','')} ({r.get('meta',{}).get('provider','')}) ===")
                        if r.get("system"): rendered_lines.append(f"\n[SYSTEM]\n{r['system']}")
                        if r.get("user"):   rendered_lines.append(f"\n[USER]\n{r['user']}\n")
                    elif kind == "chunk":
                        rendered_lines.append(r.get("text", ""))
                    elif kind == "response":
                        meta = r.get("meta") or {}
                        rendered_lines.append(f"\n=== {ts} | RESPONSE · {meta.get('model','')} · {r.get('duration_s',0):.1f}s ===")
                        rendered_lines.append(r.get("text", ""))
                        rendered_lines.append("---\n")
                    elif kind == "error":
                        rendered_lines.append(f"\n=== {ts} | ERROR ===\n{r.get('msg','')}\n---\n")
                content = "".join(rendered_lines)
                return {
                    "agent_id": agent_id,
                    "source": "framework-storage",
                    "run_ts": run_ts,
                    "log_path": key,
                    "content": content,
                    "records": records,
                    "tail_bytes": len(effective),
                    "since_offset": len(text),  # next call passes this back
                    "mtime": (records[-1].get("ts") if records else None),
                }
    except Exception as e:
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
