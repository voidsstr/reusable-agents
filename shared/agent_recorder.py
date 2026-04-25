"""Agent run recorder — small helper agents call to write run rows.

Talks to the dashboard's HTTP API at AGENT_RECORDER_URL (default
http://localhost:8080/api/agents). Writes to the dashboard's nsc_dashboard.db
which lives inside a Docker volume — agents on the host can't reach it
directly, so we go through HTTP.

If AGENT_RUN_ID is set in the environment (the dashboard sets this when it
launches an agent via Run Now), the recorder reuses that run id instead of
creating a new one.

Usage:

    from agents.lib.agent_recorder import AgentRecorder
    rec = AgentRecorder("seo-opportunity-agent")
    rec.start(triggered_by="cron")
    try:
        # ... do work ...
        rec.complete(
            deploy_tag="aisleprompt:20260425-0400",
            git_sha=sha,
            summary_md="3 pages shipped, top-5 goals declared",
            metrics={"impressions_delta": 142, "carts_28d": 66},
            goals={"declared": [...], "scored": [...]},
        )
    except Exception as e:
        rec.fail(error_text=str(e))

CLI for bash agents:

    RUN_ID=$(python3 -m agents.lib.agent_recorder start --agent seo-opportunity-agent)
    # … work …
    python3 -m agents.lib.agent_recorder complete --run-id "$RUN_ID" --summary "ok"
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("agent_recorder")

DEFAULT_BASE_URL = "http://localhost:8080/api/agents"


def _base_url() -> str:
    return os.getenv("AGENT_RECORDER_URL", DEFAULT_BASE_URL).rstrip("/")


def _post(path: str, body: dict, timeout: float = 10.0,
          method: str = "POST") -> dict:
    url = _base_url() + path
    data = json.dumps(body, default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        logger.warning(f"agent_recorder {method} {path} -> HTTP {e.code}: {body}")
        return {"ok": False, "http_status": e.code, "detail": body}
    except urllib.error.URLError as e:
        logger.warning(f"agent_recorder {method} {path} -> {e}")
        return {"ok": False, "detail": str(e)}
    except Exception as e:
        logger.warning(f"agent_recorder {method} {path} -> {e}")
        return {"ok": False, "detail": str(e)}


class AgentRecorder:
    """Record agent runs via the dashboard's HTTP API.

    Methods are best-effort: a network failure logs a warning but doesn't
    raise. The agent's own success isn't tied to recorder availability.
    """

    def __init__(self, agent_id: str, run_id: Optional[int] = None):
        self.agent_id = agent_id
        # Honor AGENT_RUN_ID from env (dashboard sets this when launching an
        # agent via "Run now"). Caller can override with run_id arg.
        self._run_id: Optional[int] = run_id
        if self._run_id is None:
            env_id = os.getenv("AGENT_RUN_ID")
            if env_id and env_id.isdigit():
                self._run_id = int(env_id)
        self._started_at: Optional[float] = None

    def start(
        self,
        triggered_by: Optional[str] = None,
        log_path: str = "",
        session_url: str = "",
    ) -> int:
        """Begin a new run. If a run_id was inherited from env, this is a no-op.
        Returns the run_id (or -1 on failure)."""
        self._started_at = time.time()
        if self._run_id is not None:
            return self._run_id
        triggered_by = triggered_by or os.getenv("AGENT_TRIGGERED_BY", "cron")
        result = _post(
            f"/{self.agent_id}/runs",
            {
                "triggered_by": triggered_by,
                "log_path": log_path,
                "session_url": session_url,
            },
        )
        if result.get("ok") and "run_id" in result:
            self._run_id = result["run_id"]
            return self._run_id
        return -1

    def update(
        self,
        deploy_tag: Optional[str] = None,
        git_sha: Optional[str] = None,
        summary_md: Optional[str] = None,
        metrics: Optional[dict] = None,
        goals: Optional[dict] = None,
        log_path: Optional[str] = None,
        session_url: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        """Update fields on the in-progress run (without finalizing)."""
        if self._run_id is None:
            return
        body: dict = {}
        if deploy_tag is not None:    body["deploy_tag"] = deploy_tag
        if git_sha is not None:        body["git_sha"] = git_sha
        if summary_md is not None:    body["summary_md"] = summary_md
        if metrics is not None:        body["metrics"] = metrics
        if goals is not None:          body["goals"] = goals
        if log_path is not None:       body["log_path"] = log_path
        if session_url is not None:    body["session_url"] = session_url
        if status is not None:         body["status"] = status
        if not body:
            return
        _post(f"/runs/{self._run_id}", body, method="PATCH")

    def _finalize(self, status: str, **fields: Any) -> None:
        if self._run_id is None:
            return
        duration_ms = int((time.time() - (self._started_at or time.time())) * 1000)
        body: dict = {"status": status, "final": True, "duration_ms": duration_ms}
        for k in ("deploy_tag", "git_sha", "summary_md",
                  "metrics", "goals", "log_path", "session_url", "error_text"):
            if k in fields and fields[k] is not None:
                body[k] = fields[k]
        _post(f"/runs/{self._run_id}", body, method="PATCH")

    def complete(self, **kwargs: Any) -> None:
        """Mark the run successful."""
        self._finalize("success", **kwargs)

    def fail(self, error_text: str = "", **kwargs: Any) -> None:
        """Mark the run failed. error_text should be the first ~50 lines
        of error output for the dashboard to display."""
        self._finalize("failure", error_text=error_text, **kwargs)

    def blocked(self, error_text: str = "", **kwargs: Any) -> None:
        """Mark the run as blocked (e.g. tests failed, no deploy)."""
        self._finalize("blocked", error_text=error_text, **kwargs)


# ---- CLI helpers (for bash agents) ----

def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="agent_recorder")
    p.add_argument("cmd", choices=["start", "update", "complete", "fail", "blocked"])
    p.add_argument("--agent", help="agent id (required for start)")
    p.add_argument("--run-id", type=int, help="run id (for update/complete/fail/blocked)")
    p.add_argument("--triggered-by", default=None)
    p.add_argument("--deploy-tag", default=None)
    p.add_argument("--git-sha", default=None)
    p.add_argument("--summary", default=None)
    p.add_argument("--metrics-file", default=None, help="path to JSON file with metrics")
    p.add_argument("--goals-file", default=None, help="path to JSON file with goals")
    p.add_argument("--error", default=None)
    p.add_argument("--log-path", default=None)
    p.add_argument("--session-url", default=None)
    args = p.parse_args()

    def _read_json(path: Optional[str]) -> Optional[dict]:
        if not path:
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            print(f"warn: failed to read {path}: {e}", file=sys.stderr)
            return None

    if args.cmd == "start":
        if not args.agent:
            sys.exit("--agent is required for start")
        rec = AgentRecorder(args.agent)
        run_id = rec.start(
            triggered_by=args.triggered_by,
            log_path=args.log_path or "",
            session_url=args.session_url or "",
        )
        if run_id < 0:
            sys.exit(2)
        print(run_id)
        return

    if args.run_id is None:
        # Try to recover from $AGENT_RUN_ID
        env = os.getenv("AGENT_RUN_ID")
        if env and env.isdigit():
            args.run_id = int(env)
        else:
            sys.exit("--run-id is required (or set $AGENT_RUN_ID)")

    rec = AgentRecorder(args.agent or "unknown", run_id=args.run_id)
    rec._started_at = time.time()  # duration won't be accurate from CLI restart

    metrics = _read_json(args.metrics_file)
    goals = _read_json(args.goals_file)
    kwargs: dict[str, Any] = {}
    if args.deploy_tag is not None:    kwargs["deploy_tag"] = args.deploy_tag
    if args.git_sha is not None:       kwargs["git_sha"] = args.git_sha
    if args.summary is not None:       kwargs["summary_md"] = args.summary
    if metrics is not None:            kwargs["metrics"] = metrics
    if goals is not None:              kwargs["goals"] = goals
    if args.log_path is not None:      kwargs["log_path"] = args.log_path
    if args.session_url is not None:   kwargs["session_url"] = args.session_url

    if args.cmd == "update":
        rec.update(**kwargs)
    elif args.cmd == "complete":
        rec.complete(**kwargs)
    elif args.cmd == "fail":
        rec.fail(error_text=args.error or "", **kwargs)
    elif args.cmd == "blocked":
        rec.blocked(error_text=args.error or "", **kwargs)


if __name__ == "__main__":
    _cli()
