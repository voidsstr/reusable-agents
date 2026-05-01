"""Helpers for reading + writing the standardized run-dir files.

Every sub-agent reads/writes through these helpers instead of touching
the JSON files directly. Centralizes path conventions + schema versioning.

Run dir layout (matches docs/runs.md):

  <runs_root>/<site>/<run-ts>/
    data/                    # raw collector output
    snapshot.json            # analyzer-recorded metric snapshot
    comparison.json          # snapshot vs prior
    recommendations.json     # the contract
    goals.json               # this run's declared goals
    goal-progress.json       # prior run's goals scored
    changes/                 # per-rec implementation artifacts
      rec-001.diff
      rec-001.summary.md
    deploy.json              # deployer output
    responses.json           # user feedback (input to responder-agent)
    run.json                 # status, timing, agent versions
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSIONS = {
    "recommendations": "1",
    "responses": "1",
    "snapshot": "1",
    "goals": "1",
    "run": "1",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def write_recommendations(
    run_dir: Path,
    *,
    site: str,
    run_ts: str,
    mode: str,
    summary: str,
    recommendations: list[dict],
    pre_traffic_mode: bool = False,
    extra: Optional[dict] = None,
) -> Path:
    """Write recommendations.json. recommendations is a list of dicts matching
    the recommendations.schema.json Recommendation definition.

    pre_traffic_mode: True when GSC has < N impressions/90d, used by the
    reporter to switch the email layout to lead with content-creation recs.
    extra: optional dict of additional top-level fields to merge in (e.g.
    revenue_focus snapshot, total_impr_90d).
    """
    payload = {
        "schema_version": SCHEMA_VERSIONS["recommendations"],
        "site": site,
        "run_ts": run_ts,
        "mode": mode,
        "summary": summary,
        "pre_traffic_mode": pre_traffic_mode,
        "recommendations": recommendations,
    }
    if extra:
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
    out = run_dir / "recommendations.json"
    out.write_text(json.dumps(payload, indent=2))
    return out


def read_recommendations(run_dir: Path) -> Optional[dict]:
    p = run_dir / "recommendations.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def update_recommendation(run_dir: Path, rec_id: str, patch: dict) -> bool:
    """Update one recommendation in-place. Returns True if updated."""
    data = read_recommendations(run_dir)
    if data is None:
        return False
    for rec in data.get("recommendations", []):
        if rec.get("id") == rec_id:
            rec.update(patch)
            (run_dir / "recommendations.json").write_text(json.dumps(data, indent=2))
            return True
    return False


# ---------------------------------------------------------------------------
# Responses (the feedback queue)
# ---------------------------------------------------------------------------

def append_response(
    run_dir: Path,
    *,
    site: str,
    from_run: str,
    rec_id: str,
    action: str,
    source: str = "dashboard",
    notes: str = "",
    merge_with: str = "",
) -> Path:
    """Append a response. Creates responses.json if absent."""
    p = run_dir / "responses.json"
    if p.is_file():
        data = json.loads(p.read_text())
    else:
        data = {"schema_version": SCHEMA_VERSIONS["responses"], "responses": []}
    entry = {
        "ts": _now_iso(),
        "site": site,
        "from_run": from_run,
        "rec_id": rec_id,
        "action": action,
        "source": source,
        "notes": notes,
        "consumed_at": None,
        "consumed_in_run_ts": None,
    }
    if action == "merge":
        if not merge_with:
            raise ValueError("merge_with is required for action=merge")
        entry["merge_with"] = merge_with
    data["responses"].append(entry)
    p.write_text(json.dumps(data, indent=2))
    return p


def read_responses(run_dir: Path) -> list[dict]:
    p = run_dir / "responses.json"
    if not p.is_file():
        return []
    return json.loads(p.read_text()).get("responses", [])


def pending_responses(run_dir: Path) -> list[dict]:
    return [r for r in read_responses(run_dir) if r.get("consumed_at") is None]


def mark_response_consumed(run_dir: Path, rec_id: str, by_run_ts: str) -> bool:
    p = run_dir / "responses.json"
    if not p.is_file():
        return False
    data = json.loads(p.read_text())
    for r in data.get("responses", []):
        if r.get("rec_id") == rec_id and r.get("consumed_at") is None:
            r["consumed_at"] = _now_iso()
            r["consumed_in_run_ts"] = by_run_ts
            p.write_text(json.dumps(data, indent=2))
            return True
    return False


# ---------------------------------------------------------------------------
# Run metadata
# ---------------------------------------------------------------------------

def write_run_meta(
    run_dir: Path,
    *,
    site: str,
    run_ts: str,
    triggered_by: str = "cron",
    agents_used: Optional[list[str]] = None,
    extra: Optional[dict] = None,
) -> Path:
    payload = {
        "schema_version": SCHEMA_VERSIONS["run"],
        "site": site,
        "run_ts": run_ts,
        "started_at": _now_iso(),
        "ended_at": None,
        "status": "running",
        "triggered_by": triggered_by,
        "agents_used": agents_used or [],
        "deploy_tag": "",
        "git_sha": "",
        "summary": "",
    }
    if extra:
        payload.update(extra)
    p = run_dir / "run.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


def update_run_meta(run_dir: Path, patch: dict) -> bool:
    p = run_dir / "run.json"
    if not p.is_file():
        return False
    data = json.loads(p.read_text())
    data.update(patch)
    if patch.get("status") in ("success", "failure", "blocked", "cancelled"):
        data.setdefault("ended_at", _now_iso())
        if data["ended_at"] is None:
            data["ended_at"] = _now_iso()
    p.write_text(json.dumps(data, indent=2))
    return True


# ---------------------------------------------------------------------------
# Per-recommendation implementation artifacts
# ---------------------------------------------------------------------------

def write_change_artifact(
    run_dir: Path,
    rec_id: str,
    *,
    diff: str = "",
    summary_md: str = "",
) -> tuple[Path, Path]:
    """Write changes/<rec_id>.{diff,summary.md} artifacts."""
    changes = run_dir / "changes"
    changes.mkdir(exist_ok=True)
    diff_path = changes / f"{rec_id}.diff"
    summary_path = changes / f"{rec_id}.summary.md"
    if diff:
        diff_path.write_text(diff)
    if summary_md:
        summary_path.write_text(summary_md)
    return diff_path, summary_path


# ---------------------------------------------------------------------------
# Standardized export — global responses queue
# ---------------------------------------------------------------------------
# Beyond the per-run responses.json, we also append every response to a
# global queue file the responder-agent can poll across runs:
#   <runs_root>/_queue/responses.jsonl
# This is just a stream of entries (one JSON object per line).

def append_to_global_response_queue(runs_root: Path, response_entry: dict) -> None:
    queue_dir = runs_root / "_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue = queue_dir / "responses.jsonl"
    with queue.open("a") as f:
        f.write(json.dumps(response_entry) + "\n")


def drain_global_response_queue(runs_root: Path) -> list[dict]:
    """Atomically rename the queue file out of the way and return its contents.
    Caller is responsible for processing + storing the entries durably."""
    queue = runs_root / "_queue" / "responses.jsonl"
    if not queue.is_file():
        return []
    drained = queue.with_suffix(f".jsonl.draining-{_now_iso().replace(':', '')}")
    queue.rename(drained)
    entries = []
    for line in drained.read_text().splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries
