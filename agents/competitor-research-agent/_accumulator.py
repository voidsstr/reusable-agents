"""Proposal accumulator for the competitor-research-agent.

Persists proposed features across runs at:
  agents/<agent_id>/proposals/active.json

Each proposal is keyed by a stable hash of its normalized title so the
same feature surfaced by multiple runs is recognized as one entry.
States flow: open → (implemented | deferred | skipped). Emails include
all open proposals so the operator sees the full backlog every cycle,
not just this run's slice.

Schema (v1):
{
  "schema_version": "1",
  "site_id": "<site>",
  "agent_id": "<agent>",
  "updated_at": "<iso>",
  "proposals": [
    {
      "proposal_id":         "<sha1[:16]>",   # hash(normalized_title)
      "state":               "open"|"implemented"|"deferred"|"skipped",
      "first_proposed_at":   "<iso>",
      "first_proposed_run_ts": "<run_ts>",
      "last_seen_at":        "<iso>",
      "last_seen_run_ts":    "<run_ts>",
      "times_seen":          <int>,
      "state_changed_at":    "<iso>"|null,
      "state_changed_reason": "<str>"|null,
      "title":               "...",
      "category":            "parity-feature"|"competitive-advantage"|"ux-improvement"|"content-gap"|...,
      "severity":            "critical"|"high"|"medium"|"low",
      "tier":                "auto"|"review"|"experimental",
      "confidence":          0.0..1.0,
      "competitor":          "...",
      "rationale":           "...",
      "expected_impact":     "...",
      "user_story":          "...",
      "blueprint":           {...},        # full structured blueprint
      "success_metrics":     [...],
      "implementation_outline": {...},
      "evidence":            [...],
      "fix_suggestion":      "..."
    }
  ]
}
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "1"


def proposal_id(title: str) -> str:
    """Stable hash of a proposal's title — used to dedupe across runs.

    Normalization: lowercase, collapse whitespace, strip leading/trailing
    punctuation, drop trailing year suffixes ('(2026)'). Two runs that
    re-surface the same feature get the same proposal_id.
    """
    s = (title or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\(\[](?:19|20)\d{2}[\)\]]\s*$", "", s).strip()  # drop year suffix
    s = re.sub(r"[^\w\s\-:.]", "", s)                              # strip punct
    s = s.strip(" -:")
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def storage_key(agent_id: str) -> str:
    """Where the accumulator file lives."""
    return f"agents/{agent_id}/proposals/active.json"


def load_active(storage, agent_id: str) -> dict:
    """Read the accumulator file (or return an empty doc)."""
    try:
        doc = storage.read_json(storage_key(agent_id))
    except Exception:
        doc = None
    if not isinstance(doc, dict):
        doc = {
            "schema_version": SCHEMA_VERSION,
            "agent_id": agent_id,
            "updated_at": _now_iso(),
            "proposals": [],
        }
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("agent_id", agent_id)
    doc.setdefault("proposals", [])
    return doc


def save_active(storage, agent_id: str, doc: dict) -> None:
    doc["updated_at"] = _now_iso()
    storage.write_json(storage_key(agent_id), doc)


# Fields we copy from the new rec onto the accumulator entry. Keep this
# list aligned with the keys the email renderer + responder consume.
_PROPOSAL_FIELDS: tuple[str, ...] = (
    "title", "category", "severity", "tier", "confidence",
    "competitor", "rationale", "expected_impact", "user_story",
    "blueprint", "success_metrics", "implementation_outline",
    "evidence", "fix_suggestion",
)


def merge_run(
    doc: dict,
    *,
    new_recs: list[dict],
    site_id: str,
    run_ts: str,
) -> dict:
    """Merge a run's recs into the accumulator.

    Behaviour:
      • Proposals already in state in {implemented, skipped, deferred}
        are NEVER re-opened — even if the LLM re-surfaces the same title.
        This keeps the operator's accept/skip decisions sticky.
      • Existing OPEN proposals get last_seen_run_ts + times_seen
        incremented and their fields refreshed (newer rationale wins).
      • Truly-new proposals are appended with state='open',
        first_proposed_at=now.

    Returns the same `doc` (mutated). Caller calls `save_active` to
    persist.
    """
    doc["site_id"] = site_id

    by_id: dict[str, dict] = {p["proposal_id"]: p for p in doc.get("proposals", [])
                              if isinstance(p, dict) and p.get("proposal_id")}
    now = _now_iso()

    for rec in new_recs:
        if not isinstance(rec, dict):
            continue
        title = rec.get("title", "")
        if not title:
            continue
        pid = proposal_id(title)
        existing = by_id.get(pid)
        if existing:
            state = existing.get("state", "open")
            # Don't reopen closed proposals.
            existing["last_seen_at"] = now
            existing["last_seen_run_ts"] = run_ts
            existing["times_seen"] = int(existing.get("times_seen", 1)) + 1
            if state == "open":
                # Refresh fields from latest rec (LLM may improve over time).
                for f in _PROPOSAL_FIELDS:
                    if f in rec and rec[f] is not None:
                        existing[f] = rec[f]
        else:
            entry: dict[str, Any] = {
                "proposal_id": pid,
                "state": "open",
                "first_proposed_at": now,
                "first_proposed_run_ts": run_ts,
                "last_seen_at": now,
                "last_seen_run_ts": run_ts,
                "times_seen": 1,
                "state_changed_at": None,
                "state_changed_reason": None,
            }
            for f in _PROPOSAL_FIELDS:
                if f in rec and rec[f] is not None:
                    entry[f] = rec[f]
            entry.setdefault("title", title)
            doc["proposals"].append(entry)
            by_id[pid] = entry

    return doc


def transition_state(
    doc: dict,
    proposal_id_to: str,
    new_state: str,
    *,
    reason: str = "",
) -> bool:
    """Set state on one proposal. Returns True if found + changed."""
    for p in doc.get("proposals", []):
        if p.get("proposal_id") == proposal_id_to:
            if p.get("state") == new_state:
                return False
            p["state"] = new_state
            p["state_changed_at"] = _now_iso()
            p["state_changed_reason"] = reason or ""
            return True
    return False


def open_proposals(doc: dict) -> list[dict]:
    """Return open proposals sorted: severity → tier → confidence desc → first_proposed_at desc."""
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    tier_order = {"auto": 0, "review": 1, "experimental": 2}
    out = [p for p in doc.get("proposals", []) if p.get("state", "open") == "open"]
    out.sort(key=lambda p: (
        sev_order.get(p.get("severity", "low"), 9),
        tier_order.get(p.get("tier", "review"), 9),
        -float(p.get("confidence") or 0),
        # newer first within ties
        -(int(p.get("first_proposed_at", "0").replace("-", "")
              .replace(":", "").replace("T", "")[:14] or 0)),
    ))
    return out


def stats(doc: dict) -> dict:
    """Counts by state for the email header."""
    counts = {"open": 0, "implemented": 0, "deferred": 0, "skipped": 0}
    for p in doc.get("proposals", []):
        st = p.get("state", "open")
        if st in counts:
            counts[st] += 1
    counts["total"] = sum(counts.values())
    return counts
