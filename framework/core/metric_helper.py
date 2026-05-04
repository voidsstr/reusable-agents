"""Metric helper — record agent goal-progress metrics in a performant way.

Why this exists:
  - The original `goals.record_goal_progress` puts every progress point
    inline in agents/<id>/goals/active.json. As history grows, every
    write rewrites the entire file, and the dashboard had to walk
    EVERY run dir to build a time-series chart (60+ storage reads per
    agent). Goals page was slow.
  - This helper writes ONLY the small bits that change (metric.current,
    a one-line progress entry, a tiny cache update) and keeps the
    big history in append-only blobs that the dashboard reads on demand.

Storage layout:
  agents/<id>/goals/active.json
      Goal definitions only (target, direction, unit, etc.).
      Small file, schema-validated, rarely grows.

  agents/<id>/goals/progress/<goal-id>.jsonl
      Append-only timeseries per goal. One line per run.
      Bounded growth (a daily agent makes 365 lines/yr per goal).
      Dashboard reads only when rendering the chart for that goal.

  agents/<id>/goals/timeseries-cache.json
      Single file with last 100 points per goal (newest first) +
      latest metric values. Dashboard reads ONLY this for the goals
      page list. Updated incrementally on each record() call —
      no run-dir scan needed.

CLI mode (for shell agents):
  python3 -m framework.core.metric_helper record \
      <agent-id> <goal-id> <value> [--note "..."] [--run-ts "..."]
  python3 -m framework.core.metric_helper record-many \
      <agent-id> --json '{"goal-foo": 42, "goal-bar": 99}'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


CACHE_POINTS_LIMIT = 200  # Keep last N points per goal in the fast cache


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _active_key(agent_id: str) -> str:
    return f"agents/{agent_id}/goals/active.json"


def _progress_key(agent_id: str, goal_id: str) -> str:
    return f"agents/{agent_id}/goals/progress/{goal_id}.jsonl"


def _cache_key(agent_id: str) -> str:
    return f"agents/{agent_id}/goals/timeseries-cache.json"


def record(
    agent_id: str,
    goal_id: str,
    value: float,
    *,
    run_ts: str = "",
    note: str = "",
    storage: Optional[StorageBackend] = None,
) -> None:
    """Record a single metric point. Three small writes (cache + jsonl +
    active update) instead of one big write of the full doc.
    """
    s = storage or get_storage()
    now = _now()
    entry = {"ts": now, "value": float(value), "run_ts": run_ts, "note": note}

    # 1. Append to per-goal jsonl (cheap append, no read)
    try:
        s.append_jsonl(_progress_key(agent_id, goal_id), entry)
    except Exception as e:
        print(f"[metric_helper] append_jsonl failed: {e}", file=sys.stderr)

    # 2. Update only metric.current in active.json (small read+write)
    try:
        doc = s.read_json(_active_key(agent_id)) or {
            "schema_version": "1", "agent_id": agent_id,
            "updated_at": now, "goals": [],
        }
        for g in doc.get("goals", []):
            if g.get("id") != goal_id:
                continue
            m = g.setdefault("metric", {})
            m["current"] = float(value)
            m["updated_at"] = now
            # Auto-detect accomplishment by direction + target
            if "target" in m:
                tgt = m.get("target")
                direction = m.get("direction", "increase")
                if tgt is not None:
                    if direction == "increase" and value >= tgt and g.get("status") != "accomplished":
                        g["status"] = "accomplished"
                        g["accomplished_at"] = now
                    elif direction == "decrease" and value <= tgt and g.get("status") != "accomplished":
                        g["status"] = "accomplished"
                        g["accomplished_at"] = now
            break
        doc["updated_at"] = now
        s.write_json(_active_key(agent_id), doc)
    except Exception as e:
        print(f"[metric_helper] active.json update failed: {e}", file=sys.stderr)

    # 3. Update the timeseries cache (small read+write, bounded size)
    try:
        cache = s.read_json(_cache_key(agent_id)) or {
            "agent_id": agent_id, "updated_at": now, "goals": {},
        }
        goals_idx = cache.setdefault("goals", {})
        goal_entry = goals_idx.setdefault(goal_id, {"points": []})
        goal_entry["points"].append({"ts": now, "value": float(value), "run_ts": run_ts})
        # Keep newest CACHE_POINTS_LIMIT
        if len(goal_entry["points"]) > CACHE_POINTS_LIMIT:
            goal_entry["points"] = goal_entry["points"][-CACHE_POINTS_LIMIT:]
        goal_entry["latest_value"] = float(value)
        goal_entry["latest_ts"] = now
        cache["updated_at"] = now
        s.write_json(_cache_key(agent_id), cache)
    except Exception as e:
        print(f"[metric_helper] cache update failed: {e}", file=sys.stderr)


def record_many(
    agent_id: str,
    metrics: dict[str, float],
    *,
    run_ts: str = "",
    note: str = "",
    storage: Optional[StorageBackend] = None,
) -> None:
    """Record multiple goal metrics in one call. More efficient than calling
    record() N times — does ONE active.json read/write + ONE cache read/write.
    """
    s = storage or get_storage()
    now = _now()

    # 1. Append to each per-goal jsonl
    for goal_id, value in metrics.items():
        try:
            s.append_jsonl(_progress_key(agent_id, goal_id),
                           {"ts": now, "value": float(value), "run_ts": run_ts, "note": note})
        except Exception as e:
            print(f"[metric_helper] append_jsonl({goal_id}) failed: {e}", file=sys.stderr)

    # 2. ONE active.json update for all goals
    try:
        doc = s.read_json(_active_key(agent_id)) or {
            "schema_version": "1", "agent_id": agent_id,
            "updated_at": now, "goals": [],
        }
        for g in doc.get("goals", []):
            gid = g.get("id")
            if gid not in metrics:
                continue
            value = float(metrics[gid])
            m = g.setdefault("metric", {})
            m["current"] = value
            m["updated_at"] = now
            if "target" in m:
                tgt = m.get("target")
                direction = m.get("direction", "increase")
                if tgt is not None:
                    if direction == "increase" and value >= tgt and g.get("status") != "accomplished":
                        g["status"] = "accomplished"; g["accomplished_at"] = now
                    elif direction == "decrease" and value <= tgt and g.get("status") != "accomplished":
                        g["status"] = "accomplished"; g["accomplished_at"] = now
        doc["updated_at"] = now
        s.write_json(_active_key(agent_id), doc)
    except Exception as e:
        print(f"[metric_helper] active.json update failed: {e}", file=sys.stderr)

    # 3. ONE cache update for all goals
    try:
        cache = s.read_json(_cache_key(agent_id)) or {
            "agent_id": agent_id, "updated_at": now, "goals": {},
        }
        goals_idx = cache.setdefault("goals", {})
        for goal_id, value in metrics.items():
            ge = goals_idx.setdefault(goal_id, {"points": []})
            ge["points"].append({"ts": now, "value": float(value), "run_ts": run_ts})
            if len(ge["points"]) > CACHE_POINTS_LIMIT:
                ge["points"] = ge["points"][-CACHE_POINTS_LIMIT:]
            ge["latest_value"] = float(value)
            ge["latest_ts"] = now
        cache["updated_at"] = now
        s.write_json(_cache_key(agent_id), cache)
    except Exception as e:
        print(f"[metric_helper] cache update failed: {e}", file=sys.stderr)


def read_cache(agent_id: str, storage: Optional[StorageBackend] = None) -> dict:
    """Fast read for the goals dashboard — single storage call."""
    s = storage or get_storage()
    return s.read_json(_cache_key(agent_id)) or {
        "agent_id": agent_id, "goals": {}, "updated_at": "",
    }


def read_goal_progress(
    agent_id: str, goal_id: str, *, limit: int = 1000,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """Read full progress jsonl for one goal — used by the dashboard's chart
    component for high-resolution history."""
    s = storage or get_storage()
    rows = s.read_jsonl(_progress_key(agent_id, goal_id)) or []
    return rows[-limit:]


# ---------------------------------------------------------------------------
# CLI for shell-based agents
# ---------------------------------------------------------------------------

def _main() -> None:
    p = argparse.ArgumentParser(prog="metric_helper")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="record a single metric value")
    r.add_argument("agent_id")
    r.add_argument("goal_id")
    r.add_argument("value", type=float)
    r.add_argument("--run-ts", default="")
    r.add_argument("--note", default="")
    rm = sub.add_parser("record-many", help="record multiple metrics in one call (--json '{\"goal-x\": 42}')")
    rm.add_argument("agent_id")
    rm.add_argument("--json", required=True, help="JSON dict {goal_id: value}")
    rm.add_argument("--run-ts", default="")
    rm.add_argument("--note", default="")
    rc = sub.add_parser("read-cache", help="dump the timeseries cache for an agent")
    rc.add_argument("agent_id")
    args = p.parse_args()
    if args.cmd == "record":
        record(args.agent_id, args.goal_id, args.value, run_ts=args.run_ts, note=args.note)
        print(f"recorded {args.agent_id}/{args.goal_id} = {args.value}")
    elif args.cmd == "record-many":
        try:
            metrics = json.loads(args.json)
        except Exception as e:
            raise SystemExit(f"invalid --json: {e}")
        record_many(args.agent_id, metrics, run_ts=args.run_ts, note=args.note)
        print(f"recorded {len(metrics)} metrics for {args.agent_id}")
    elif args.cmd == "read-cache":
        print(json.dumps(read_cache(args.agent_id), indent=2))


if __name__ == "__main__":
    _main()
