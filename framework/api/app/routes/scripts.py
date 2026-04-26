"""Agent scripts — list + invoke arbitrary scripts under agents/<id>/scripts/.

Convention:
  agents/<id>/scripts/<name>.{py,sh}     — script file
  agents/<id>/scripts/<name>.json        — optional metadata (description, args)

The framework reads the registered agent's repo_dir, looks under scripts/,
and exposes them as runnable units via the API. Each invocation:
  - Receives env: AGENT_ID, AGENT_RUN_ID (synthetic), AI_PROVIDER (optional)
  - Runs in the agent's repo_dir
  - Output captured to /tmp/reusable-agents-logs/<id>-script-<name>-<ts>.log
  - Returns {ok, log_path, exit_code}

Lets the dashboard expose ad-hoc operations the user wants to trigger
manually (e.g., "re-pull GSC data for this one site", "rebuild the
roadmap from scratch") without requiring an OpenAPI endpoint per script.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from framework.core.registry import get_agent

from ..auth import require_token


router = APIRouter(prefix="/api/agents", tags=["scripts"], dependencies=[Depends(require_token)])


def _scripts_dir(agent_id: str) -> Path:
    m = get_agent(agent_id)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown agent")
    if not m.repo_dir:
        raise HTTPException(status_code=400, detail="agent has no repo_dir")
    sd = Path(m.repo_dir) / "scripts"
    if not sd.is_dir():
        raise HTTPException(status_code=404,
                            detail=f"agent has no scripts/ dir at {sd}")
    return sd


@router.get("/{agent_id}/scripts")
def list_scripts(agent_id: str):
    """List runnable scripts in agents/<id>/scripts/."""
    try:
        sd = _scripts_dir(agent_id)
    except HTTPException as e:
        if e.status_code == 404 and "scripts/" in (e.detail or ""):
            return []
        raise
    out = []
    for p in sorted(sd.iterdir()):
        if p.is_dir() or p.name.startswith("."):
            continue
        if p.suffix not in (".py", ".sh"):
            continue
        meta_path = p.with_suffix(".json")
        meta = {}
        if meta_path.is_file():
            try: meta = json.loads(meta_path.read_text())
            except Exception: pass
        out.append({
            "name": p.stem,
            "filename": p.name,
            "kind": "python" if p.suffix == ".py" else "bash",
            "description": meta.get("description", ""),
            "args": meta.get("args", []),
            "metadata": meta,
        })
    return out


class InvokeRequest(BaseModel):
    args: list[str] = []
    ai_provider: str = ""    # optional override — passed via AI_PROVIDER env


@router.post("/{agent_id}/scripts/{script_name}")
def invoke(agent_id: str, script_name: str, req: InvokeRequest):
    sd = _scripts_dir(agent_id)
    script_path = None
    for ext in (".py", ".sh"):
        candidate = sd / f"{script_name}{ext}"
        if candidate.is_file():
            script_path = candidate; break
    if script_path is None:
        raise HTTPException(status_code=404, detail=f"script {script_name!r} not found in {sd}")

    log_dir = Path(os.getenv("AGENT_LOG_DIR", "/tmp/reusable-agents-logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{agent_id}-script-{script_name}-{run_ts}.log"

    env = os.environ.copy()
    env["AGENT_ID"] = agent_id
    env["AGENT_RUN_ID"] = f"script-{run_ts}"
    env["AGENT_TRIGGERED_BY"] = "manual:script"
    if req.ai_provider:
        env["AI_PROVIDER"] = req.ai_provider

    cmd = [str(script_path), *req.args]
    if script_path.suffix == ".py":
        cmd = ["python3", *cmd]
    elif script_path.suffix == ".sh":
        cmd = ["bash", *cmd]

    try:
        with log_path.open("w") as f:
            proc = subprocess.run(
                cmd, cwd=str(script_path.parent.parent),  # agent's repo_dir
                env=env, stdout=f, stderr=subprocess.STDOUT, timeout=600,
            )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "log_path": str(log_path),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "log_path": str(log_path),
                "detail": "timeout (10 min)"}
    except Exception as e:
        return {"ok": False, "exit_code": -1, "detail": str(e),
                "log_path": str(log_path)}
