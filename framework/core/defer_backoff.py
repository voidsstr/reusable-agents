"""Per-rec defer backoff — prevents tight defer→re-queue loops.

When the implementer defers a rec because the required model isn't
available (claude-pool exhausted, no Opus capacity), the rec stays in
the producer's run-dir. The backlog-dispatcher picks it up again on
the very next tick (1 min cadence), the implementer re-defers, and
the cycle burns through ~1,440 wasted dispatcher cycles per day per
stuck rec.

This primitive records how many times a rec has deferred + computes the
next retry time using exponential backoff. Both producer (dispatcher)
and consumer (implementer) call into it:

  Dispatcher (before adding rec to dispatch candidates):
    if defer_backoff.should_skip(rec_id, agent_id):
        continue  # still cooling down

  Implementer (when writing deferred.json):
    defer_backoff.record_defer(rec_id, agent_id, reason)

  Implementer (when a rec successfully ships):
    defer_backoff.record_success(rec_id, agent_id)

Storage layout (in framework storage backend — Azure blob in prod):

  framework/defer-backoff/<source_agent_id>.json
    {
      "schema_version": "1",
      "recs": {
        "<rec_id>": {
          "count": 3,
          "first_deferred_at": "2026-06-09T10:00:00+00:00",
          "last_deferred_at":  "2026-06-09T12:30:00+00:00",
          "next_retry_at":     "2026-06-09T14:30:00+00:00",
          "last_reason":       "required-model-unavailable: opus"
        }
      }
    }

Backoff schedule (intentionally aggressive after first few attempts):
  attempt 1 → retry in   60s (1 min)   — likely transient
  attempt 2 → retry in  300s (5 min)
  attempt 3 → retry in 1800s (30 min)
  attempt 4 → retry in 7200s (2 h)
  attempt 5 → retry in 21600s (6 h)
  attempt 6+ → retry in 43200s (12 h)  — capacity probably needs operator

A success record() resets the count to 0 so the next defer starts fresh.

Author: 2026-06-09 (queue prioritization audit)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


SCHEMA_VERSION = "1"

# Backoff schedule in seconds. Index = (count - 1); cap at last entry.
BACKOFF_SECONDS: tuple[int, ...] = (
    60,        # 1 min  — attempt 1 result
    300,       # 5 min  — attempt 2 result
    1800,      # 30 min — attempt 3 result
    7200,      # 2 h    — attempt 4 result
    21600,     # 6 h    — attempt 5 result
    43200,     # 12 h   — attempt 6+ result (cap)
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state_key(source_agent_id: str) -> str:
    safe = (source_agent_id or "unknown").replace("/", "_")
    return f"framework/defer-backoff/{safe}.json"


def _load(source_agent_id: str, storage: Optional[StorageBackend] = None) -> dict:
    s = storage or get_storage()
    try:
        doc = s.read_json(_state_key(source_agent_id)) or {}
    except Exception:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("recs", {})
    return doc


def _save(source_agent_id: str, doc: dict, storage: Optional[StorageBackend] = None) -> None:
    s = storage or get_storage()
    try:
        s.write_json(_state_key(source_agent_id), doc)
    except Exception:
        # Best-effort — never break the dispatch path on a state-write error
        pass


def _next_retry_seconds(count: int) -> int:
    """Lookup the backoff seconds for a given (1-indexed) attempt count."""
    if count <= 0:
        return 0
    idx = min(count - 1, len(BACKOFF_SECONDS) - 1)
    return BACKOFF_SECONDS[idx]


def should_skip(
    rec_id: str,
    source_agent_id: str,
    *,
    storage: Optional[StorageBackend] = None,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """Return (skip, reason). reason is a short human string when skipping,
    empty when not. Safe to call on every dispatcher tick — O(1) read of a
    small JSON blob, cached by the storage layer."""
    if not rec_id or not source_agent_id:
        return False, ""
    doc = _load(source_agent_id, storage=storage)
    entry = doc["recs"].get(rec_id)
    if not entry:
        return False, ""
    next_retry = entry.get("next_retry_at", "")
    if not next_retry:
        return False, ""
    try:
        next_t = datetime.fromisoformat(next_retry)
    except Exception:
        return False, ""
    now = now or _now()
    if now < next_t:
        remaining = int((next_t - now).total_seconds())
        return True, (
            f"defer-backoff (attempt {entry.get('count', 0)}, "
            f"{remaining}s remaining; last_reason={entry.get('last_reason','')!r})"
        )
    return False, ""


def record_defer(
    rec_id: str,
    source_agent_id: str,
    reason: str = "",
    *,
    storage: Optional[StorageBackend] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Increment the defer count for this rec + compute next-retry time.
    Returns the updated entry."""
    if not rec_id or not source_agent_id:
        return {}
    now = now or _now()
    doc = _load(source_agent_id, storage=storage)
    entry = doc["recs"].get(rec_id) or {}
    entry["count"] = int(entry.get("count", 0)) + 1
    entry.setdefault("first_deferred_at", now.isoformat(timespec="seconds"))
    entry["last_deferred_at"] = now.isoformat(timespec="seconds")
    entry["last_reason"] = (reason or "")[:200]
    delay_s = _next_retry_seconds(entry["count"])
    entry["next_retry_at"] = (now + timedelta(seconds=delay_s)).isoformat(timespec="seconds")
    doc["recs"][rec_id] = entry
    _save(source_agent_id, doc, storage=storage)
    return entry


def record_success(
    rec_id: str,
    source_agent_id: str,
    *,
    storage: Optional[StorageBackend] = None,
) -> None:
    """Clear any defer history for this rec. Called when the implementer
    actually ships. A future defer starts from count=0."""
    if not rec_id or not source_agent_id:
        return
    doc = _load(source_agent_id, storage=storage)
    if rec_id in doc["recs"]:
        del doc["recs"][rec_id]
        _save(source_agent_id, doc, storage=storage)


def summary(
    source_agent_id: str,
    *,
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Operator-grade: how many recs are currently in cooldown, longest
    backoff, etc. Used by the dashboard."""
    doc = _load(source_agent_id, storage=storage)
    now = _now()
    in_cooldown = 0
    max_remaining = 0
    by_count: dict[int, int] = {}
    for entry in doc["recs"].values():
        try:
            next_t = datetime.fromisoformat(entry.get("next_retry_at", ""))
            if now < next_t:
                in_cooldown += 1
                remaining = int((next_t - now).total_seconds())
                if remaining > max_remaining:
                    max_remaining = remaining
            cnt = int(entry.get("count", 0))
            by_count[cnt] = by_count.get(cnt, 0) + 1
        except Exception:
            continue
    return {
        "source_agent_id":   source_agent_id,
        "total_tracked":     len(doc["recs"]),
        "in_cooldown_now":   in_cooldown,
        "max_remaining_s":   max_remaining,
        "by_attempt_count":  by_count,
    }


def reset_all(
    source_agent_id: str,
    *,
    storage: Optional[StorageBackend] = None,
) -> int:
    """Operator escape hatch: clear all defer state for an agent. Returns
    the number of entries dropped. Useful after the operator fixes the
    underlying capacity issue and wants all stuck recs to retry now."""
    doc = _load(source_agent_id, storage=storage)
    n = len(doc["recs"])
    doc["recs"] = {}
    _save(source_agent_id, doc, storage=storage)
    return n
