"""Record an implementer dispatch as a first-class run in the dashboard.

Each implementer dispatch (one batch of recs spawned via systemd-run by
`framework.core.dispatch._spawn_implementer`) writes a progress.json +
run-index entry under `agents/implementer/runs/<run_ts>/`, mirroring
the shape `AgentBase.post_run` writes. The dashboard's `/api/agents/
implementer/runs` endpoint then surfaces every dispatch as a run row.

Two subcommands:
  start  — write progress.json with status=running before LLM work begins.
  end    — overwrite progress.json with terminal status + update run-index.

Invoked from agents/implementer/run.sh; never fails the implementer if
the storage write fails (best-effort).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from framework.core.storage import get_storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _progress_key(run_ts: str) -> str:
    return f"agents/implementer/runs/{run_ts}/progress.json"


def _index_key() -> str:
    return "agents/implementer/run-index.json"


def cmd_start(args: argparse.Namespace) -> int:
    storage = get_storage()
    started_at = _now()
    rec_ids = [r for r in (args.rec_ids or "").split(",") if r.strip()]
    summary = (
        f"dispatched from {args.source_agent or '?'} "
        f"({len(rec_ids)} rec{'s' if len(rec_ids) != 1 else ''})"
    )
    progress = {
        "schema_version": "1",
        "agent_id": "implementer",
        "run_ts": args.run_ts,
        "started_at": started_at,
        "ended_at": None,
        "iteration_count": 0,
        "status": "running",
        "progress": 0.0,
        "metrics": {
            "rec_count": len(rec_ids),
        },
        "summary": summary,
        "triggered_by": f"dispatch:{args.source_agent}" if args.source_agent else "dispatch",
        "dispatch": {
            "source_agent": args.source_agent or "",
            "source_run_ts": args.source_run_ts or "",
            "site": args.site or "",
            "request_id": args.request_id or "",
            "subject_tag": args.subject_tag or "",
            "rec_ids": rec_ids,
            "log_path": args.log_path or "",
        },
    }
    try:
        storage.write_json(_progress_key(args.run_ts), progress)
        _update_index(storage, progress)
        _write_agent_status(storage, args.run_ts, "running", summary, progress_pct=0.05)
    except Exception as e:
        print(f"[dispatch-run-record] start failed (non-fatal): {e}", file=sys.stderr)
        return 0
    print(f"[dispatch-run-record] started agents/implementer/runs/{args.run_ts}/")
    return 0


def _write_agent_status(storage, run_ts: str, state: str, message: str,
                         progress_pct: float = 1.0) -> None:
    """Write/overwrite agents/implementer/status.json with the dispatch's
    state. 2026-05-11: implementer dispatches went through `run.sh` and
    only wrote per-run progress.json; the agent-level status.json was
    only updated by stale ghost-reap entries, so the dashboard never
    saw "running" for an active dispatch. With concurrent dispatches
    the last writer wins (fine semantically — terminal = mixed history
    visible in run-index)."""
    payload = {
        "schema_version": "1",
        "agent_id": "implementer",
        "state": state,
        "message": message,
        "progress": max(0.0, min(1.0, float(progress_pct))),
        "current_action": "code-editor chain" if state == "running" else "",
        "started_at": _now() if state == "running" else None,
        "updated_at": _now(),
        "current_run_ts": run_ts,
        "iteration_count": 0,
        "internal": {"writer": "dispatch_run_record"},
    }
    # For terminal states, preserve started_at from the per-run progress
    # so the duration on the dashboard reflects actual run time.
    if state != "running":
        try:
            prog = storage.read_json(_progress_key(run_ts)) or {}
            payload["started_at"] = prog.get("started_at") or _now()
        except Exception:
            payload["started_at"] = _now()
    storage.write_json("agents/implementer/status.json", payload,
                       cache_control="public, max-age=2")


def cmd_end(args: argparse.Namespace) -> int:
    storage = get_storage()
    ended_at = _now()
    status = "success" if args.rc == 0 else "failure"
    rec_ids = [r for r in (args.rec_ids or "").split(",") if r.strip()]
    summary = (
        f"{status}: {len(rec_ids)} rec{'s' if len(rec_ids) != 1 else ''} "
        f"from {args.source_agent or '?'} (rc={args.rc})"
    )

    existing = {}
    try:
        existing = storage.read_json(_progress_key(args.run_ts)) or {}
    except Exception:
        existing = {}

    progress = {
        "schema_version": "1",
        "agent_id": "implementer",
        "run_ts": args.run_ts,
        "started_at": existing.get("started_at") or ended_at,
        "ended_at": ended_at,
        "iteration_count": existing.get("iteration_count", 0) or 0,
        "status": status,
        "progress": 1.0,
        "metrics": {
            **(existing.get("metrics") or {}),
            "rec_count": len(rec_ids),
            "rc": args.rc,
        },
        "summary": summary,
        "triggered_by": existing.get("triggered_by")
            or (f"dispatch:{args.source_agent}" if args.source_agent else "dispatch"),
        "dispatch": existing.get("dispatch") or {
            "source_agent": args.source_agent or "",
            "source_run_ts": args.source_run_ts or "",
            "site": args.site or "",
            "request_id": args.request_id or "",
            "subject_tag": args.subject_tag or "",
            "rec_ids": rec_ids,
            "log_path": args.log_path or "",
        },
    }

    try:
        storage.write_json(_progress_key(args.run_ts), progress)
        _update_index(storage, progress)
        _write_agent_status(storage, args.run_ts, status, summary)
    except Exception as e:
        print(f"[dispatch-run-record] end failed (non-fatal): {e}", file=sys.stderr)
        return 0
    print(f"[dispatch-run-record] ended agents/implementer/runs/{args.run_ts}/ status={status}")
    return 0


def _update_index(storage, progress: dict) -> None:
    """Insert/replace this run's entry at the head of run-index.json.

    Same read-modify-write pattern as AgentBase.post_run; tolerates races
    by capping `recent` at 50 and accepting the index is regenerable.
    """
    idx_key = _index_key()
    idx = {}
    try:
        idx = storage.read_json(idx_key) or {}
    except Exception:
        idx = {}
    entry = {
        "agent_id": "implementer",
        "run_ts": progress["run_ts"],
        "status": progress["status"],
        "summary": progress.get("summary", ""),
        "started_at": progress.get("started_at"),
        "ended_at": progress.get("ended_at"),
        "iteration_count": progress.get("iteration_count", 0),
        "progress": progress.get("progress", 0.0),
        "metrics": progress.get("metrics") or {},
        "triggered_by": progress.get("triggered_by", ""),
    }
    recent = [entry] + [
        r for r in (idx.get("recent") or [])
        if r.get("run_ts") != progress["run_ts"]
    ]
    new_total = int(idx.get("total_runs", 0))
    if progress["status"] != "running":
        # Only count terminal transitions toward total (start writes
        # status=running, end writes success/failure). Avoids double-count.
        new_total += 1
    storage.write_json(idx_key, {
        "total_runs": new_total,
        "recent": recent[:50],
    })


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dispatch_run_record")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run-ts", required=True)
    common.add_argument("--source-agent", default="")
    common.add_argument("--source-run-ts", default="")
    common.add_argument("--site", default="")
    common.add_argument("--request-id", default="")
    common.add_argument("--subject-tag", default="")
    common.add_argument("--rec-ids", default="", help="comma-separated")
    common.add_argument("--log-path", default="")

    s = sub.add_parser("start", parents=[common])
    s.set_defaults(func=cmd_start)

    e = sub.add_parser("end", parents=[common])
    e.add_argument("--rc", type=int, required=True)
    e.set_defaults(func=cmd_end)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
