"""Opportunity accumulator for the app-store-opportunity-agent.

Persists discovered app-store opportunities across runs at:
  agents/<agent_id>/opportunities/active.json

Keyed by (store, store_id) — same app rediscovered on a later run is
recognized as one entry. States flow:
  open -> (pursued | passed | obsolete)

  - open       — surfaced and still worth pursuing
  - pursued    — operator decided to build a competitor; tracked elsewhere
  - passed     — operator looked + said no; never re-open
  - obsolete   — the tracked app shipped a major update / improved rating;
                 framework auto-closes via merge_run

Schema (v1):
{
  "schema_version": "1",
  "agent_id": "...",
  "updated_at": "<iso>",
  "opportunities": [
    {
      "opportunity_id":  "<sha1[:16] of store + store_id>",
      "state":           "open" | "pursued" | "passed" | "obsolete",
      "first_seen_at":   "<iso>",
      "first_seen_run_ts": "...",
      "last_seen_at":    "<iso>",
      "last_seen_run_ts": "...",
      "times_seen":      <int>,
      "state_changed_at": "<iso>" | null,
      "state_changed_reason": "..." | null,

      "store":           "ios" | "android",
      "store_id":        "...",                   # iTunes track ID or Play package
      "store_url":       "...",
      "name":            "...",
      "publisher":       "...",
      "category":        "...",
      "country":         "us",
      "price":           "free" | "<usd>",

      # Snapshot signals (refresh every run while open)
      "average_rating":      0.0..5.0,
      "review_count":        <int>,
      "current_version":     "...",
      "released_at":         "<iso-date>",
      "last_updated_at":     "<iso-date>",
      "days_since_update":   <int>,
      "version_count_12mo":  <int>,

      # First-seen baselines (used for obsolete-detection)
      "baseline_version":    "...",
      "baseline_rating":     0.0,
      "baseline_review_count": <int>,
      "baseline_updated_at": "<iso-date>",

      # LLM analysis (refreshed when new evidence warrants)
      "build_complexity":    1..5,
      "complexity_drivers":  ["external API: ...", ...],
      "revenue_potential":   "low" | "medium" | "high" | "very_high",
      "revenue_thesis":      "...",
      "decline_thesis":      "...",
      "feature_gaps":        ["...", ...],
      "v2_pitch":            "...",
      "moats_to_take":       ["...", ...],

      # Final ranking score
      "opportunity_score":   0.0..1.0,
      "rank_signals": {
        "decline_signal": 0.0..1.0,
        "install_base":   0.0..1.0,
        "monetization":   0.0..1.0,
        "simplicity":     0.0..1.0
      }
    }
  ]
}
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1"


def opportunity_id(store: str, store_id: str) -> str:
    s = f"{(store or '').strip().lower()}::{(store_id or '').strip()}"
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def storage_key(agent_id: str) -> str:
    return f"agents/{agent_id}/opportunities/active.json"


def load_active(storage, agent_id: str) -> dict:
    try:
        doc = storage.read_json(storage_key(agent_id))
    except Exception:
        doc = None
    if not isinstance(doc, dict):
        doc = {
            "schema_version": SCHEMA_VERSION,
            "agent_id": agent_id,
            "updated_at": _now_iso(),
            "opportunities": [],
        }
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("agent_id", agent_id)
    doc.setdefault("opportunities", [])
    return doc


def save_active(storage, agent_id: str, doc: dict) -> None:
    doc["updated_at"] = _now_iso()
    storage.write_json(storage_key(agent_id), doc)


# Snapshot signal fields refreshed on each run while still open.
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "name", "publisher", "category", "country", "price", "store_url",
    "average_rating", "review_count", "current_version",
    "released_at", "last_updated_at", "days_since_update",
    "version_count_12mo",
    # Regional-gap discovery fields (set when an item came from
    # `discover_country_gaps`). Keeps the gap signal visible in the
    # dashboard knowledge view + the daily email.
    "gap_present_in", "gap_absent_in", "gap_score",
)

# LLM-derived fields. Refreshed when complexity/revenue analysis runs.
_LLM_FIELDS: tuple[str, ...] = (
    "build_complexity", "complexity_drivers",
    "revenue_potential", "revenue_thesis",
    "decline_thesis", "feature_gaps", "v2_pitch", "moats_to_take",
    "opportunity_score", "rank_signals",
    # Build blueprint metadata. Persists across runs so the email
    # renderer + responder know which items are shippable.
    "blueprint", "build_status",
)


def _major_version(v: str) -> str:
    if not v:
        return ""
    m = re.match(r"\s*(\d+)", v)
    return m.group(1) if m else v.strip()


def _is_obsolete(existing: dict, fresh: dict) -> tuple[bool, str]:
    """Detect when the tracked app has materially improved since first seen.

    A 'major' version bump OR a >0.5-star rating jump above 3.9 closes
    the opportunity — the publisher woke up, our thesis no longer holds.
    """
    base_v = _major_version(existing.get("baseline_version") or "")
    cur_v  = _major_version(fresh.get("current_version") or "")
    if base_v and cur_v and cur_v != base_v:
        try:
            if int(cur_v) > int(base_v):
                return True, f"major-version-bump {base_v}->{cur_v}"
        except ValueError:
            pass
    base_r = float(existing.get("baseline_rating") or 0)
    cur_r  = float(fresh.get("average_rating") or 0)
    if base_r and cur_r >= 4.2 and (cur_r - base_r) >= 0.5:
        return True, f"rating-recovered {base_r:.2f}->{cur_r:.2f}"
    return False, ""


def merge_run(
    doc: dict,
    *,
    new_apps: list[dict],
    run_ts: str,
) -> dict:
    """Merge a run's discovered apps into the accumulator.

    Behaviour:
      • Closed opportunities (pursued/passed/obsolete) NEVER reopen.
      • Open opportunities refresh snapshot fields, LLM fields, and
        check for obsolete-detection.
      • Truly-new entries appended with state='open' + baselines.
    """
    by_id: dict[str, dict] = {
        o["opportunity_id"]: o
        for o in doc.get("opportunities", [])
        if isinstance(o, dict) and o.get("opportunity_id")
    }
    now = _now_iso()

    for app in new_apps:
        if not isinstance(app, dict):
            continue
        store = app.get("store", "")
        store_id = app.get("store_id", "")
        if not store or not store_id:
            continue
        oid = opportunity_id(store, store_id)
        existing = by_id.get(oid)
        if existing:
            existing["last_seen_at"] = now
            existing["last_seen_run_ts"] = run_ts
            existing["times_seen"] = int(existing.get("times_seen", 1)) + 1
            if existing.get("state", "open") == "open":
                obsolete, reason = _is_obsolete(existing, app)
                if obsolete:
                    existing["state"] = "obsolete"
                    existing["state_changed_at"] = now
                    existing["state_changed_reason"] = reason
                    continue
                for f in _SNAPSHOT_FIELDS + _LLM_FIELDS:
                    if f in app and app[f] is not None:
                        existing[f] = app[f]
        else:
            entry: dict[str, Any] = {
                "opportunity_id": oid,
                "state": "open",
                "first_seen_at": now,
                "first_seen_run_ts": run_ts,
                "last_seen_at": now,
                "last_seen_run_ts": run_ts,
                "times_seen": 1,
                "state_changed_at": None,
                "state_changed_reason": None,
                "store": store,
                "store_id": store_id,
                "baseline_version":      app.get("current_version", ""),
                "baseline_rating":       app.get("average_rating", 0),
                "baseline_review_count": app.get("review_count", 0),
                "baseline_updated_at":   app.get("last_updated_at", ""),
            }
            for f in _SNAPSHOT_FIELDS + _LLM_FIELDS:
                if f in app and app[f] is not None:
                    entry[f] = app[f]
            doc["opportunities"].append(entry)
            by_id[oid] = entry

    return doc


def transition_state(
    doc: dict,
    opportunity_id_to: str,
    new_state: str,
    *,
    reason: str = "",
) -> bool:
    for o in doc.get("opportunities", []):
        if o.get("opportunity_id") == opportunity_id_to:
            if o.get("state") == new_state:
                return False
            o["state"] = new_state
            o["state_changed_at"] = _now_iso()
            o["state_changed_reason"] = reason or ""
            return True
    return False


def open_opportunities(doc: dict) -> list[dict]:
    """Open opportunities sorted by (revenue_potential / build_complexity)
    descending — i.e. the most build-cheap, revenue-rich first."""
    rev_score = {"very_high": 4, "high": 3, "medium": 2, "low": 1, "": 0}
    out = [o for o in doc.get("opportunities", []) if o.get("state", "open") == "open"]

    def sort_key(o: dict) -> tuple:
        complexity = max(1, int(o.get("build_complexity") or 3))
        revenue = rev_score.get(o.get("revenue_potential", ""), 0)
        roi = revenue / complexity
        opp = float(o.get("opportunity_score") or 0)
        # newer first within ties
        seen = (o.get("first_seen_at") or "").replace("-", "").replace(":", "").replace("T", "")[:14]
        try:
            seen_n = int(seen or 0)
        except ValueError:
            seen_n = 0
        return (-roi, -opp, -seen_n)

    out.sort(key=sort_key)
    return out


def stats(doc: dict) -> dict:
    counts = {"open": 0, "pursued": 0, "passed": 0, "obsolete": 0}
    for o in doc.get("opportunities", []):
        st = o.get("state", "open")
        if st in counts:
            counts[st] += 1
    counts["total"] = sum(counts.values())
    return counts
