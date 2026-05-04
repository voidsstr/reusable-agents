"""Agent goals — persistent objectives the agent works toward each run.

Storage layout:
  agents/<id>/goals/active.json          # current full goal set (validated)
  agents/<id>/goals/history/<run-ts>.json # snapshot per run (audit trail)
  agents/<id>/goals/accomplished.jsonl   # append-only log of accomplishments

Convention: every agent in the framework declares goals (3-7 typical).
Each `run()` should:
  1. Read active goals at start (helper: `read_active_goals`)
  2. Use them to bias the run (e.g. "this run found 3 broken pages,
     contributing toward goal-zero-broken-pages")
  3. At end of run, update goal metrics + push progress_history (helper:
     `record_goal_progress`)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import jsonschema
except ImportError as e:
    raise SystemExit("jsonschema required (pip install jsonschema)") from e

from .storage import StorageBackend, get_storage


_HERE = Path(__file__).resolve().parent
SCHEMA_PATH = _HERE.parent.parent / "shared" / "schemas" / "agent-goals.schema.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_goals_doc(doc: dict) -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(doc, schema)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def goals_key(agent_id: str) -> str:
    return f"agents/{agent_id}/goals/active.json"


def read_active_goals(
    agent_id: str, storage: Optional[StorageBackend] = None
) -> list[dict]:
    """Return the full list of goals (active + accomplished). Empty if none yet."""
    s = storage or get_storage()
    doc = s.read_json(goals_key(agent_id)) or {}
    return list(doc.get("goals", []))


def write_goals_doc(
    agent_id: str,
    goals: list[dict],
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Validate + persist the full goal set."""
    s = storage or get_storage()
    doc = {
        "schema_version": "1",
        "agent_id": agent_id,
        "updated_at": _now(),
        "goals": goals,
    }
    _validate_goals_doc(doc)
    s.write_json(goals_key(agent_id), doc)
    return doc


def init_goals(
    agent_id: str,
    goals: list[dict],
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Idempotently seed a default set of goals for an agent. Existing goals
    with the same `id` are preserved (their progress_history is kept), so
    re-running this never wipes progress. New goal ids get added."""
    s = storage or get_storage()
    existing = read_active_goals(agent_id, storage=s)
    by_id = {g["id"]: g for g in existing}
    merged: list[dict] = []
    for g in goals:
        gid = g["id"]
        if gid in by_id:
            cur = by_id[gid]
            # Update only TITLE/DESCRIPTION/DIRECTIVES/TARGET if changed; keep history
            cur["title"] = g.get("title", cur.get("title"))
            cur["description"] = g.get("description", cur.get("description"))
            cur["directives"] = g.get("directives", cur.get("directives", []))
            if "metric" in g:
                cur_metric = cur.get("metric", {})
                cur_metric.update({
                    k: v for k, v in g["metric"].items()
                    if k in ("target", "direction", "unit", "horizon_weeks", "name")
                })
                # Ensure 'current' is a number (schema requires it). Use the
                # incoming default if cur_metric.current is null/missing.
                if cur_metric.get("current") is None:
                    cur_metric["current"] = g["metric"].get("current", 0)
                cur["metric"] = cur_metric
            merged.append(cur)
        else:
            seeded = dict(g)
            seeded.setdefault("status", "active")
            seeded.setdefault("created_at", _now())
            seeded.setdefault("progress_history", [])
            # Same null-guard for new goals
            sm = seeded.get("metric")
            if isinstance(sm, dict) and sm.get("current") is None:
                sm["current"] = 0
            merged.append(seeded)
    # Preserve any existing goals not mentioned in the new seed list
    seeded_ids = {g["id"] for g in goals}
    for gid, g in by_id.items():
        if gid not in seeded_ids:
            # Ensure metric.current is a valid number (schema requires it).
            # Some legacy rows had null currents that block re-validation.
            gm = g.get("metric")
            if isinstance(gm, dict) and gm.get("current") is None:
                gm["current"] = 0
            merged.append(g)
    return write_goals_doc(agent_id, merged, storage=s)


# ---------------------------------------------------------------------------
# Per-run progress
# ---------------------------------------------------------------------------

def record_goal_progress(
    agent_id: str,
    goal_id: str,
    value: float,
    *,
    run_ts: str = "",
    note: str = "",
    accomplished: Optional[bool] = None,
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Append a progress entry to one goal. Optionally mark it accomplished."""
    s = storage or get_storage()
    doc = s.read_json(goals_key(agent_id)) or {
        "schema_version": "1", "agent_id": agent_id,
        "updated_at": _now(), "goals": [],
    }
    goals = doc.get("goals", [])
    for g in goals:
        if g["id"] != goal_id:
            continue
        g.setdefault("progress_history", []).append({
            "ts": _now(), "value": value, "run_ts": run_ts, "note": note,
        })
        m = g.setdefault("metric", {})
        m["current"] = value
        if accomplished is True:
            g["status"] = "accomplished"
            g["accomplished_at"] = _now()
            # Append to accomplished.jsonl for fast audit
            s.append_jsonl(
                f"agents/{agent_id}/goals/accomplished.jsonl",
                {"ts": _now(), "goal_id": goal_id, "title": g.get("title", ""),
                 "value": value, "run_ts": run_ts},
            )
        elif accomplished is False:
            g["status"] = "active"
            g["accomplished_at"] = None
        # Auto-detect accomplishment if metric.target reached
        elif accomplished is None and "target" in m:
            target = m.get("target")
            direction = m.get("direction", "increase")
            if direction == "increase" and value >= target and g.get("status") != "accomplished":
                g["status"] = "accomplished"; g["accomplished_at"] = _now()
                s.append_jsonl(
                    f"agents/{agent_id}/goals/accomplished.jsonl",
                    {"ts": _now(), "goal_id": goal_id, "title": g.get("title", ""),
                     "value": value, "run_ts": run_ts, "auto_detected": True},
                )
            elif direction == "decrease" and value <= target and g.get("status") != "accomplished":
                g["status"] = "accomplished"; g["accomplished_at"] = _now()
                s.append_jsonl(
                    f"agents/{agent_id}/goals/accomplished.jsonl",
                    {"ts": _now(), "goal_id": goal_id, "title": g.get("title", ""),
                     "value": value, "run_ts": run_ts, "auto_detected": True},
                )
        break
    else:
        raise ValueError(f"goal_id {goal_id!r} not found for agent {agent_id!r}")
    doc["updated_at"] = _now()
    s.write_json(goals_key(agent_id), doc)

    # Per-run snapshot for audit trail
    if run_ts:
        s.write_json(
            f"agents/{agent_id}/goals/history/{run_ts}.json",
            doc,
        )
    return doc


def goals_directives_text(
    agent_id: str, storage: Optional[StorageBackend] = None
) -> str:
    """Render active goals + their directives as a plain-text block the
    agent can paste into its LLM system prompt at run start. Drops
    accomplished goals."""
    goals = read_active_goals(agent_id, storage=storage)
    active = [g for g in goals if g.get("status") != "accomplished"]
    if not active:
        return ""
    lines = ["AGENT GOALS — bias every run toward advancing these:"]
    for g in active:
        m = g.get("metric", {})
        lines.append(f"\n  • [{g['id']}] {g['title']}")
        if g.get("description"):
            lines.append(f"      ↳ {g['description']}")
        if m.get("name"):
            cur = m.get("current", "?")
            tgt = m.get("target", "?")
            unit = m.get("unit", "")
            dir_arrow = "↗" if m.get("direction", "increase") == "increase" else "↘"
            lines.append(f"      metric: {m['name']} = {cur}{unit} {dir_arrow} target {tgt}{unit}")
        for d in (g.get("directives") or []):
            lines.append(f"      - directive: {d}")
    return "\n".join(lines)
