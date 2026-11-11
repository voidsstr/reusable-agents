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

Status rules: `apply_measurement` is the one place they live. It is shared by
record_goal_progress and metric_helper.record/record_many:
  - A measurement that reaches metric.target accomplishes the goal.
  - metric.ratchet {"factor": F}: reaching the target instead raises it to
    ceil(value × F) (a decrease goal lowers it to floor(value ÷ F)), and the
    goal stays active. A north-star count never "finishes", so it never drops
    out of the prompts. A ratchet implies re-open.
  - Re-open: an accomplished goal whose new measurement falls back past its
    target becomes active again. This happens when metric.reopen is true, when
    the goal has a ratchet, or when storage config/goals-config.json sets
    reopen_on_regression (fleet default, or by_agent_id.<id>). It is off by
    default, so goals without these settings behave as before.
init_goals (and so PUT /api/agents/<id>/goals) re-activates an accomplished
goal whose current value misses the seeded target, when the seed moved that
target or set a rule that asks for re-opening (a ratchet, or reopen: true). A
seed that only opts out (reopen: false, ratchet: null) never re-opens a goal.
A seed never accomplishes a goal. Nor does a seed ratchet one: that needs a
fresh measurement, not a stale current.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

from .storage import StorageBackend, get_storage


_HERE = Path(__file__).resolve().parent
SCHEMA_PATH = _HERE.parent.parent / "shared" / "schemas" / "agent-goals.schema.json"

# Storage config (optional): {"schema_version": "1",
#   "reopen_on_regression": false,
#   "by_agent_id": {"<agent-id>": {"reopen_on_regression": true}}}
CONFIG_KEY = "config/goals-config.json"
RATCHET_HISTORY_LIMIT = 20
ANNOTATIONS_LIMIT = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_goals_doc(doc: dict) -> None:
    # Imported here, not at module load, so metric_helper (which shares the
    # status rules below) does not need jsonschema just to record a value.
    try:
        import jsonschema
    except ImportError as e:
        raise SystemExit("jsonschema required (pip install jsonschema)") from e
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.validate(doc, schema)


# ---------------------------------------------------------------------------
# Status rules: accomplish / ratchet / re-open
# ---------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    # NaN is not a measurement: it must not accomplish, ratchet or re-open.
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return float(v)


def load_goals_config(storage: Optional[StorageBackend] = None) -> dict:
    """Storage config/goals-config.json; {} when absent or unreadable."""
    try:
        doc = (storage or get_storage()).read_json(CONFIG_KEY)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def reopen_on_regression(agent_id: str, storage: Optional[StorageBackend] = None,
                         config: Optional[dict] = None) -> bool:
    """Fleet default for re-opening goals that have no metric.reopen of their
    own: by_agent_id.<id>.reopen_on_regression, else the top-level value,
    else False. A malformed config (by_agent_id not a mapping, a non-bool
    flag) counts as unset; only a literal true turns re-open on."""
    cfg = load_goals_config(storage) if config is None else config
    if not isinstance(cfg, dict):
        return False
    by = cfg.get("by_agent_id")
    per = by.get(agent_id) if isinstance(by, dict) else None
    if isinstance(per, dict) and isinstance(per.get("reopen_on_regression"), bool):
        return per["reopen_on_regression"]
    return cfg.get("reopen_on_regression") is True


DEFAULT_PROGRESS_HISTORY_MAX = 500


def progress_history_cap(storage: Optional[StorageBackend] = None,
                         config: Optional[dict] = None) -> int:
    """Max progress_history points kept inside active.json (older points live in
    agents/<id>/goals/progress/<goal>.jsonl). config/goals-config.json key
    `progress_history_max`; 0 or a negative value means unlimited."""
    try:
        cfg = load_goals_config(storage) if config is None else config
        raw = cfg.get("progress_history_max", DEFAULT_PROGRESS_HISTORY_MAX) \
            if isinstance(cfg, dict) else DEFAULT_PROGRESS_HISTORY_MAX
        n = int(raw)
    except Exception:  # noqa: BLE001 - malformed config never breaks recording
        n = DEFAULT_PROGRESS_HISTORY_MAX
    return n if n > 0 else 0


def lazy_reopen_default(agent_id: str,
                        storage: Optional[StorageBackend] = None) -> Callable[[], bool]:
    """A zero-arg callable that reads the fleet default once, on first use.
    Recording a value for a goal that opts in or out itself never reads it.
    Any error reading the config means False (re-open off), so a broken
    config can never stop a measurement from being recorded."""
    memo: list[bool] = []

    def _get() -> bool:
        if not memo:
            try:
                memo.append(reopen_on_regression(agent_id, storage=storage))
            except Exception as e:  # noqa: BLE001 - recording must not fail on config
                print(f"[goals] {CONFIG_KEY} unusable for {agent_id} ({e}); "
                      "reopen_on_regression treated as false", file=sys.stderr)
                memo.append(False)
        return memo[0]
    return _get


def target_met(metric: Optional[dict], value: Any = None) -> Optional[bool]:
    """True/False when `value` (default: metric.current) meets metric.target
    in metric.direction; None when either is not a number or the direction
    is unknown."""
    m = metric or {}
    tgt = _num(m.get("target"))
    val = _num(m.get("current") if value is None else value)
    if tgt is None or val is None:
        return None
    direction = m.get("direction", "increase")
    if direction == "increase":
        return val >= tgt
    if direction == "decrease":
        return val <= tgt
    return None


def ratchet_factor(metric: Optional[dict]) -> Optional[float]:
    """metric.ratchet.factor when it is a number above 1, else None."""
    r = (metric or {}).get("ratchet")
    f = _num(r.get("factor")) if isinstance(r, dict) else None
    return f if f is not None and f > 1 else None


def ratcheted_target(value: float, target: float, direction: str,
                     factor: float) -> Optional[Union[int, float]]:
    """The next target once `value` has met `target`; None when it cannot
    move (e.g. a decrease goal already at 0). Whole-number targets stay whole
    numbers; fractional ones (a CTR) keep 6 decimals."""
    whole = float(target).is_integer()
    if direction == "increase":
        base = max(value, target)
        raw = base * factor
        new = int(math.ceil(raw)) if whole else round(raw, 6)
        return new if new > base else None
    if direction == "decrease":
        base = min(value, target)
        raw = base / factor
        new = int(math.floor(raw)) if whole else round(raw, 6)
        return new if new < base else None
    return None


def _reopen_wanted(metric: dict, reopen_default: Union[bool, Callable[[], bool]]) -> bool:
    if ratchet_factor(metric):
        return True
    if isinstance(metric.get("reopen"), bool):
        return metric["reopen"]
    return bool(reopen_default() if callable(reopen_default) else reopen_default)


def apply_measurement(goal: dict, value: float, *, now: Optional[str] = None,
                      reopen_default: Union[bool, Callable[[], bool]] = False) -> list[dict]:
    """Apply the automatic status rules for one fresh measurement.

    Mutates `goal` (status, accomplished_at, metric.target, ratchet_history,
    reopened_at/reopened_reason) and returns what happened as events:
    [{"event": "accomplished"|"ratcheted"|"reopened", ...}]. The caller has
    already stored `value` in metric.current. A retired (abandoned) goal
    keeps its measurements but never changes status.
    """
    status = goal.get("status") or "active"
    m = goal.get("metric")
    if status == "abandoned" or not isinstance(m, dict):
        return []
    met = target_met(m, value)
    if met is None:
        return []
    now = now or _now()
    target = m["target"]
    direction = m.get("direction", "increase")
    factor = ratchet_factor(m)
    if met:
        if factor:
            new_target = ratcheted_target(float(value), float(target), direction, factor)
            if new_target is not None:
                m["target"] = new_target
                hist = goal.get("ratchet_history")
                hist = hist if isinstance(hist, list) else []
                hist.append({"ts": now, "value": value,
                             "from_target": target, "to_target": new_target})
                goal["ratchet_history"] = hist[-RATCHET_HISTORY_LIMIT:]
                if status == "accomplished":
                    goal["status"] = "active"
                    goal["accomplished_at"] = None
                return [{"event": "ratcheted", "value": value,
                         "from_target": target, "to_target": new_target}]
        if status != "accomplished":
            goal["status"] = "accomplished"
            goal["accomplished_at"] = now
            return [{"event": "accomplished", "value": value, "target": target}]
        return []
    if status == "accomplished" and _reopen_wanted(m, reopen_default):
        goal["status"] = "active"
        goal["accomplished_at"] = None
        goal["reopened_at"] = now
        goal["reopened_reason"] = (f"measurement {float(value):g} fell back past "
                                   f"target {float(target):g}")
        return [{"event": "reopened", "value": value, "target": target}]
    return []


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


def _apply_seed_target(metric: dict, seed_target: Any) -> None:
    """Apply a seed's metric.target. On a ratcheting goal, a routine re-seed
    keeps the ratcheted target: a re-seed is routine when its target equals
    metric.seed_target, the target the last seed asked for. Agents such as
    site-goals-tracker re-seed every run. A changed seed target always wins."""
    if (ratchet_factor(metric) and "target" in metric
            and metric.get("seed_target") == seed_target):
        return
    metric["target"] = seed_target
    if ratchet_factor(metric):
        metric["seed_target"] = seed_target
    else:
        metric.pop("seed_target", None)


def _merge_annotations(goal: dict, notes: Any) -> None:
    """Add a seed's dated notes. Identity is (ts, note), and notes already on
    the goal are never dropped."""
    have = goal.get("annotations")
    have = [a for a in have if isinstance(a, dict)] if isinstance(have, list) else []
    seen = {(a.get("ts"), a.get("note")) for a in have}
    for a in notes if isinstance(notes, list) else []:
        if (isinstance(a, dict) and a.get("ts") and a.get("note")
                and (a["ts"], a["note"]) not in seen):
            have.append(dict(a))
            seen.add((a["ts"], a["note"]))
    goal["annotations"] = have[-ANNOTATIONS_LIMIT:]


def init_goals(
    agent_id: str,
    goals: list[dict],
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Idempotently seed a default set of goals for an agent. Existing goals
    with the same `id` are preserved (their progress_history is kept), so
    re-running this never wipes progress. New goal ids get added.

    An accomplished goal whose current value misses the seeded target is
    re-activated when the seed moved that target, or changed its rules to
    ones that ask for re-opening (a metric.ratchet, or metric.reopen true).
    This is how a stale target is re-armed. A seed that only opts out
    (reopen false, ratchet null) leaves the goal accomplished. A null
    ratchet/reopen removes that rule (and is ignored on a new goal)."""
    s = storage or get_storage()
    existing = read_active_goals(agent_id, storage=s)
    by_id = {g["id"]: g for g in existing}
    merged: list[dict] = []
    now = _now()
    for g in goals:
        gid = g["id"]
        if gid in by_id:
            cur = by_id[gid]
            # Update only TITLE/DESCRIPTION/DIRECTIVES/TARGET if changed; keep history
            cur["title"] = g.get("title", cur.get("title"))
            _desc = g.get("description", cur.get("description"))
            if _desc is not None:  # a status-only retire entry has none
                cur["description"] = _desc
            cur["directives"] = g.get("directives", cur.get("directives", []))
            # Top-level fields read by agent_base auto-tracking + UI;
            # update them on re-seed so wiring changes actually stick.
            for _k in ("target_metric", "baseline", "target", "direction",
                       "from_rec", "rationale", "check_by", "is_top5_goal",
                       "is_revenue_goal"):
                if _k in g:
                    cur[_k] = g[_k]
            # Explicit retire / un-retire. A seed may only move a goal into
            # or out of "abandoned" (accomplished stays owned by
            # record_goal_progress). Retiring is how a vanity goal ("recs
            # emitted per run") stops steering prompts and re-accomplishing
            # itself every run without losing its history.
            want = g.get("status")
            if want == "abandoned" and cur.get("status") != "abandoned":
                cur["status"] = "abandoned"
                cur["abandoned_at"] = _now()
            elif want == "active" and cur.get("status") == "abandoned":
                cur["status"] = "active"
                cur.pop("abandoned_at", None)
            rearm = False
            if "metric" in g:
                cur_metric = cur.get("metric", {})
                seed_m = g["metric"] or {}
                before = (cur_metric.get("target"), cur_metric.get("ratchet"),
                          cur_metric.get("reopen"))
                for k, v in seed_m.items():
                    if k in ("direction", "unit", "horizon_weeks", "name"):
                        cur_metric[k] = v
                    elif k in ("ratchet", "reopen"):  # re-arm rules; null removes
                        if v is None:
                            cur_metric.pop(k, None)
                        else:
                            cur_metric[k] = v
                if "target" in seed_m:
                    _apply_seed_target(cur_metric, seed_m["target"])
                # Ensure 'current' is a number (schema requires it). Use the
                # incoming default if cur_metric.current is null/missing.
                if cur_metric.get("current") is None:
                    cur_metric["current"] = seed_m.get("current", 0)
                cur["metric"] = cur_metric
                after = (cur_metric.get("target"), cur_metric.get("ratchet"),
                         cur_metric.get("reopen"))
                # Re-arm only on a moved target, or on a rule change whose
                # new rule wants re-opening. An opt-out (reopen false,
                # ratchet removed) changes the rules but must not re-open.
                rearm = before != after and (
                    before[0] != after[0]
                    or bool(ratchet_factor(cur_metric))
                    or cur_metric.get("reopen") is True)
            if "annotations" in g:
                _merge_annotations(cur, g["annotations"])
            # Re-arm: an accomplished goal whose current value misses the
            # seeded target goes back to active. A routine re-seed changes
            # nothing, so it never flips status.
            if (rearm and cur.get("status") == "accomplished"
                    and target_met(cur.get("metric")) is False):
                m = cur["metric"]
                cur["status"] = "active"
                cur["accomplished_at"] = None
                cur["reopened_at"] = now
                cur["reopened_reason"] = (
                    f"re-armed by seed: target {m.get('target')} is past the "
                    f"current value {m.get('current')}")
            merged.append(cur)
        elif g.get("status") == "abandoned" and not g.get("title"):
            continue  # retire entry for a goal this agent never had
        else:
            seeded = dict(g)
            seeded.setdefault("status", "active")
            seeded.setdefault("created_at", now)
            seeded.setdefault("progress_history", [])
            # Same null-guard for new goals
            sm = seeded.get("metric")
            if isinstance(sm, dict):
                sm = seeded["metric"] = dict(sm)
                if sm.get("current") is None:
                    sm["current"] = 0
                for _rule in ("ratchet", "reopen"):  # null = no rule
                    if _rule in sm and sm[_rule] is None:
                        sm.pop(_rule)
                if ratchet_factor(sm) and "target" in sm:
                    sm["seed_target"] = sm["target"]
            if "annotations" in seeded:
                seeded.pop("annotations")
                _merge_annotations(seeded, g["annotations"])
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
    """Append a progress entry to one goal. Optionally mark it accomplished.

    With `accomplished=None` the status follows `apply_measurement`:
    accomplish on reaching the target, ratchet (metric.ratchet), or re-open
    on falling back (metric.reopen / ratchet / config reopen_on_regression).
    Accomplishments and ratchets are logged to accomplished.jsonl. If the
    status rules fail, the value is still recorded and the status is left
    as it was."""
    s = storage or get_storage()
    doc = s.read_json(goals_key(agent_id)) or {
        "schema_version": "1", "agent_id": agent_id,
        "updated_at": _now(), "goals": [],
    }
    goals = doc.get("goals", [])
    for g in goals:
        if g["id"] != goal_id:
            continue
        now = _now()
        entry = {"ts": now, "value": value, "run_ts": run_ts, "note": note}
        hist = g.setdefault("progress_history", [])
        hist.append(entry)
        # The full history goes to an append-only log (server-side append, no
        # read-modify-write); active.json keeps only the newest points. An
        # uncapped list made every run re-download and re-upload the whole doc:
        # one 1-minute agent's active.json reached 65.8 MB and cost ~430 GB/day
        # of blob egress (2026-11-11).
        try:
            s.append_jsonl(f"agents/{agent_id}/goals/progress/{goal_id}.jsonl", entry)
        except Exception as e:  # noqa: BLE001 - the in-doc point is still kept
            print(f"[goals] progress log append failed for {agent_id}/{goal_id}: {e}",
                  file=sys.stderr)
        cap = progress_history_cap(storage=s)
        if cap and len(hist) > cap:
            del hist[:len(hist) - cap]
        m = g.setdefault("metric", {})
        m["current"] = value
        if accomplished is True:
            g["status"] = "accomplished"
            g["accomplished_at"] = now
            # Append to accomplished.jsonl for fast audit
            s.append_jsonl(
                f"agents/{agent_id}/goals/accomplished.jsonl",
                {"ts": now, "goal_id": goal_id, "title": g.get("title", ""),
                 "value": value, "run_ts": run_ts},
            )
        elif accomplished is False:
            g["status"] = "active"
            g["accomplished_at"] = None
        # Automatic status rules (accomplish / ratchet / re-open). A retired
        # (abandoned) goal keeps its measurements but never flips status.
        elif accomplished is None and "target" in m and g.get("status") != "abandoned":
            try:
                events = apply_measurement(
                    g, value, now=now,
                    reopen_default=lazy_reopen_default(agent_id, storage=s))
            except Exception as e:  # noqa: BLE001 - keep the measurement
                print(f"[goals] status rules skipped for {agent_id}/{goal_id}: {e}",
                      file=sys.stderr)
                events = []
            for ev in events:
                if ev["event"] not in ("accomplished", "ratcheted"):
                    continue
                entry = {"ts": now, "goal_id": goal_id, "title": g.get("title", ""),
                         "value": value, "run_ts": run_ts, "auto_detected": True}
                if ev["event"] == "ratcheted":
                    entry.update(ratcheted=True, from_target=ev["from_target"],
                                 to_target=ev["to_target"])
                s.append_jsonl(f"agents/{agent_id}/goals/accomplished.jsonl", entry)
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
    accomplished and abandoned (retired) goals."""
    goals = read_active_goals(agent_id, storage=storage)
    active = [g for g in goals
              if g.get("status") not in ("accomplished", "abandoned")]
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
