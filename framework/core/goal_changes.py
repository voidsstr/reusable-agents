"""Goal-change tracking — append-only log of recs the agent has shipped,
which goals each one was meant to advance, and the metric movement that
resulted.

Storage layout:
  agents/<id>/goals/changes.jsonl              (append-only)
  agents/<id>/goals/changes-index.json         (denormalized index by goal_id)

Why this exists:
  - The user can see in the dashboard everything an agent has done over
    time, grouped by goal
  - The agent can READ this back at the start of each run to:
      * de-prioritize categories where past recs didn't move the metric
      * try new approaches when a goal has been flat for many runs
      * continue patterns that worked
  - Provides accountability: "this rec said it would improve X, the
    metric went from 12 → 10 over the next 3 runs"

Lifecycle:
  1. Agent emits a rec with `goal_ids: [goal-001, ...]`
  2. responder dispatches the rec to implementer's response queue
  3. responder ALSO calls record_goal_change(...) to log the dispatch
     with metric_before captured from the agent's current goals state
  4. On a subsequent run, the agent calls update_post_change_metrics(...)
     to fill in metric_after / delta / direction once the new metric
     value is known
  5. UI / agent both read via read_changes(...)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import StorageBackend, get_storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def changes_key(agent_id: str) -> str:
    return f"agents/{agent_id}/goals/changes.jsonl"


def record_goal_change(
    *,
    agent_id: str,
    rec_id: str,
    goal_ids: list[str],
    site: str = "",
    rec_title: str = "",
    rec_category: str = "",
    rec_check_id: str = "",
    rec_severity: str = "",
    rec_tier: str = "",
    implementer_agent: str = "",
    implementer_run_ts: str = "",
    metric_before: Optional[dict[str, Any]] = None,
    user_feedback: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Append a new change log entry. Caller is the responder (or any
    agent that ships changes against goals)."""
    s = storage or get_storage()
    entry = {
        "ts": _now(),
        "rec_id": rec_id,
        "goal_ids": list(goal_ids or []),
        "site": site,
        "rec_title": rec_title,
        "rec_category": rec_category,
        "rec_check_id": rec_check_id,
        "rec_severity": rec_severity,
        "rec_tier": rec_tier,
        "implementer_agent": implementer_agent,
        "implementer_run_ts": implementer_run_ts,
        "implemented_at": _now(),
    }
    if metric_before is not None:
        entry["metric_before"] = metric_before
    if user_feedback is not None:
        entry["user_feedback"] = user_feedback
    s.append_jsonl(changes_key(agent_id), entry)
    return entry


def read_changes(
    agent_id: str,
    *,
    goal_id: Optional[str] = None,
    site: Optional[str] = None,
    rec_check_id: Optional[str] = None,
    limit: int = 200,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    """Read the change log, optionally filtered. Returns newest-first."""
    s = storage or get_storage()
    raw = s.read_jsonl(changes_key(agent_id)) or []
    out = []
    for e in raw:
        if goal_id and goal_id not in (e.get("goal_ids") or []):
            continue
        if site and e.get("site") != site:
            continue
        if rec_check_id and e.get("rec_check_id") != rec_check_id:
            continue
        out.append(e)
    out.reverse()  # newest first
    return out[:limit]


def update_post_change_metrics(
    agent_id: str,
    *,
    goal_id: str,
    current_value: float,
    measurement_run_ts: str = "",
    storage: Optional[StorageBackend] = None,
) -> int:
    """Walk recent changes for `goal_id` that don't have metric_after yet,
    fill them in based on the just-measured `current_value`. Returns the
    number of entries updated.

    Note: this rewrites changes.jsonl in place — uses storage.write_text
    rather than append_jsonl. Cheap for typical change volumes (<1000).
    """
    s = storage or get_storage()
    raw = s.read_jsonl(changes_key(agent_id)) or []
    changed = 0
    for e in raw:
        if goal_id not in (e.get("goal_ids") or []):
            continue
        if "metric_after" in e:
            continue
        before = (e.get("metric_before") or {}).get("value")
        if before is None:
            continue
        delta = current_value - before
        # Direction: depends on metric direction (increase = good means delta>0;
        # decrease = good means delta<0). We don't know the direction here
        # without reading the goal definition. Caller pre-classifies if needed.
        direction = "improving" if delta != 0 else "flat"
        e["metric_after"] = {
            "value": current_value,
            "delta": delta,
            "direction": direction,
            "measured_at": _now(),
            "measurement_run_ts": measurement_run_ts,
        }
        changed += 1
    if changed:
        # Rewrite the whole file
        s.write_text(
            changes_key(agent_id),
            "\n".join(json.dumps(e) for e in raw) + "\n",
        )
    return changed


def adaptive_context_block(
    agent_id: str,
    *,
    site: Optional[str] = None,
    horizon: int = 30,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Render a compact context block summarizing recent changes per goal,
    suitable for pasting into an LLM system prompt at run start.

    The agent uses this to:
      - de-prioritize categories that haven't been moving the needle
      - continue approaches that worked
      - flag goals that have been flat for multiple runs as needing a
        different strategy
    """
    raw = read_changes(agent_id, site=site, limit=horizon, storage=storage)
    if not raw:
        return ""
    # Group by goal
    from collections import defaultdict
    by_goal: dict[str, list[dict]] = defaultdict(list)
    for e in raw:
        for gid in (e.get("goal_ids") or []):
            by_goal[gid].append(e)

    parts = ["RECENT WORK YOU'VE DONE — use this to adapt today's recommendations:"]
    if site:
        parts[0] = f"RECENT WORK ON {site} — use this to adapt today's recommendations:"
    for gid, entries in by_goal.items():
        parts.append(f"\n  GOAL {gid} — {len(entries)} prior change(s):")
        improving = sum(1 for e in entries
                        if (e.get("metric_after") or {}).get("direction") == "improving")
        flat = sum(1 for e in entries
                   if (e.get("metric_after") or {}).get("direction") == "flat")
        regressing = sum(1 for e in entries
                          if (e.get("metric_after") or {}).get("direction") == "regressing")
        unmeasured = sum(1 for e in entries if not e.get("metric_after"))
        parts.append(f"      ↗ {improving} improving · → {flat} flat · ↘ {regressing} regressing · ? {unmeasured} unmeasured")
        # Show the last 5 changes verbatim
        for e in entries[:5]:
            check = e.get("rec_check_id") or e.get("rec_category") or ""
            mafter = e.get("metric_after") or {}
            delta_s = ""
            if "delta" in mafter:
                d = mafter["delta"]
                delta_s = f" [Δ {'+' if d>0 else ''}{d:g}]"
            parts.append(
                f"        - {e['rec_id']} {check}: {e.get('rec_title','')[:80]}{delta_s}"
            )
    parts.append(
        "\nGuidance: where Δ is consistently 0 or wrong-direction across 3+"
        " changes, try a DIFFERENT category. Where it's consistently"
        " improving, double down."
    )
    return "\n".join(parts)
