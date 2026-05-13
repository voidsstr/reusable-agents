"""Knowledge accumulator — framework primitive for agents that build up
durable findings across runs.

Many agents produce *running collections* — feature recommendations,
opportunities, audit findings — where each run discovers new items,
re-confirms existing ones, and the operator transitions them through
states like open → done/skipped/obsolete. Before this module each such
agent rolled its own per-agent storage layout (`agents/<id>/proposals/
active.json`, `agents/<id>/opportunities/active.json`), each with the
same schema modulo field names. The dashboard had no way to surface
them generically.

This module is the canonical primitive:

  • Storage location:   `agents/<agent_id>/knowledge/<bucket>.json`
    (older legacy paths still readable — see `_LEGACY_BUCKETS`).
  • Item shape:         every item carries a stable `item_id`, a `state`,
    `first_seen_at`, `last_seen_at`, `times_seen`, `state_changed_at`,
    `state_changed_reason`, plus arbitrary agent-defined fields.
  • State machine:      open → terminal states are AGENT-DEFINED. The
    primitive never reopens a closed item.
  • API surface:        `KnowledgeBucket(storage, agent_id, bucket)` →
    `.load()`, `.save()`, `.merge_run(items, ...)`, `.transition_state(
    item_id, new_state)`, `.items(state=)`, `.stats()`.
  • Dashboard:          `GET /api/agents/<id>/knowledge` enumerates a
    given agent's buckets, `GET /api/agents/<id>/knowledge/<bucket>`
    returns the items.

Reference deployments (migrated from per-agent accumulators):
  - aisleprompt/specpicks competitor-research-agent → bucket="proposals"
  - app-store-opportunity-agent              → bucket="opportunities"
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

SCHEMA_VERSION = "1"


# Map of (agent_id_suffix, bucket_name) → legacy storage key. The dashboard
# API checks the canonical path first, then falls back through these so
# the existing data shows up without a migration step.
_LEGACY_BUCKETS: dict[tuple[str, str], str] = {
    ("competitor-research-agent", "proposals"):     "agents/{agent_id}/proposals/active.json",
    ("app-store-opportunity-agent", "opportunities"): "agents/{agent_id}/opportunities/active.json",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_id(*parts: str) -> str:
    """Convenience hash for building deterministic item ids from a tuple
    of identifying strings (e.g. (store, store_id) or (canonical_title,))."""
    s = "::".join((p or "").strip().lower() for p in parts)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def canonical_storage_key(agent_id: str, bucket: str) -> str:
    return f"agents/{agent_id}/knowledge/{bucket}.json"


def resolve_storage_keys(agent_id: str, bucket: str) -> list[str]:
    """Return the storage keys to try in order: canonical first, then any
    legacy path that matches the (agent_id_suffix, bucket) pair. Used by
    both the bucket loader and the dashboard reader."""
    out = [canonical_storage_key(agent_id, bucket)]
    for (suffix, bname), tmpl in _LEGACY_BUCKETS.items():
        if bname == bucket and agent_id.endswith(suffix):
            out.append(tmpl.format(agent_id=agent_id))
    return out


class KnowledgeBucket:
    """One bucket of knowledge for one agent.

    Loads on first access; agents call `.merge_run(items, ...)` at the
    end of each `run()` to fold new items in. The primitive enforces:

    1. Closed items NEVER reopen, even if the agent re-emits the same
       item_id with state="open". This keeps operator decisions sticky.
    2. Open items have their `times_seen` incremented + `last_seen_at`
       refreshed on every recurrence.
    3. Each merge call uses one `run_ts` so the dashboard can show
       which run first introduced an item and which most recently
       re-saw it.

    Item ID derivation is the agent's responsibility — pass `id_fn` to
    `merge_run` (or set each item's `item_id` field before merging).
    Use `stable_id(...)` to derive a deterministic id from identifying
    fields.
    """

    def __init__(self, storage, agent_id: str, bucket: str,
                 *, items_field: str = "items"):
        self.storage = storage
        self.agent_id = agent_id
        self.bucket = bucket
        self.items_field = items_field
        self._doc: Optional[dict] = None

    # ---- load / save ----

    def load(self) -> dict:
        if self._doc is not None:
            return self._doc
        for key in resolve_storage_keys(self.agent_id, self.bucket):
            try:
                d = self.storage.read_json(key)
            except Exception:
                d = None
            if isinstance(d, dict):
                # Normalize legacy shapes that used different items_field
                # names (`proposals`, `opportunities`) → expose under
                # the configured field, but keep the source key too so
                # writes don't clobber the legacy path.
                if self.items_field not in d:
                    for legacy_field in ("items", "proposals", "opportunities", "findings"):
                        if isinstance(d.get(legacy_field), list):
                            d[self.items_field] = d[legacy_field]
                            d.setdefault("_source_field", legacy_field)
                            break
                d.setdefault("schema_version", SCHEMA_VERSION)
                d.setdefault("agent_id", self.agent_id)
                d.setdefault("bucket", self.bucket)
                d.setdefault(self.items_field, [])
                self._doc = d
                return d
        self._doc = {
            "schema_version": SCHEMA_VERSION,
            "agent_id": self.agent_id,
            "bucket": self.bucket,
            "updated_at": _now_iso(),
            self.items_field: [],
        }
        return self._doc

    def save(self) -> None:
        if self._doc is None:
            return
        self._doc["updated_at"] = _now_iso()
        # If this bucket was loaded from a legacy path, keep writing
        # there (so other readers that haven't migrated still see fresh
        # data). Otherwise write the canonical key.
        legacy = self._doc.get("_source_field")
        if legacy and legacy != self.items_field:
            # Rewrite into the legacy shape on persist.
            d_out = dict(self._doc)
            d_out[legacy] = d_out.pop(self.items_field, [])
            d_out.pop("_source_field", None)
            for key in resolve_storage_keys(self.agent_id, self.bucket)[1:]:
                self.storage.write_json(key, d_out)
                return
        self.storage.write_json(
            canonical_storage_key(self.agent_id, self.bucket), self._doc
        )

    # ---- queries ----

    @property
    def items(self) -> list[dict]:
        return self.load().get(self.items_field) or []

    def open_items(self, *, open_state: str = "open",
                   sort_key: Optional[Callable[[dict], Any]] = None
                   ) -> list[dict]:
        out = [i for i in self.items if i.get("state", open_state) == open_state]
        if sort_key:
            out.sort(key=sort_key)
        return out

    def stats(self, *, expected_states: Iterable[str] = ("open",)) -> dict:
        counts: dict[str, int] = {s: 0 for s in expected_states}
        for i in self.items:
            st = i.get("state", "open")
            counts[st] = counts.get(st, 0) + 1
        counts["total"] = sum(counts.values())
        return counts

    # ---- mutations ----

    def merge_run(
        self,
        new_items: list[dict],
        *,
        run_ts: str,
        id_fn: Callable[[dict], Optional[str]],
        copy_fields: tuple[str, ...] = (),
        on_obsolete: Optional[Callable[[dict, dict], tuple[bool, str]]] = None,
    ) -> dict:
        """Fold a run's items into the bucket.

        Args:
          new_items     items the run produced
          run_ts        canonical run timestamp
          id_fn         given a new item, return its stable item_id (or
                        None to skip it)
          copy_fields   field names to refresh from the new item onto
                        existing OPEN items
          on_obsolete   optional: given (existing, fresh) → (True, reason)
                        flips state→"obsolete" with that reason. Called
                        only for currently-open items.
        """
        doc = self.load()
        by_id = {
            i.get("item_id"): i
            for i in doc.get(self.items_field, [])
            if isinstance(i, dict) and i.get("item_id")
        }
        now = _now_iso()
        for src in new_items:
            if not isinstance(src, dict):
                continue
            item_id = id_fn(src)
            if not item_id:
                continue
            existing = by_id.get(item_id)
            if existing:
                existing["last_seen_at"] = now
                existing["last_seen_run_ts"] = run_ts
                existing["times_seen"] = int(existing.get("times_seen", 1)) + 1
                if existing.get("state", "open") == "open":
                    if on_obsolete:
                        is_obs, reason = on_obsolete(existing, src)
                        if is_obs:
                            existing["state"] = "obsolete"
                            existing["state_changed_at"] = now
                            existing["state_changed_reason"] = reason
                            continue
                    for f in copy_fields:
                        if f in src and src[f] is not None:
                            existing[f] = src[f]
            else:
                new = {
                    "item_id": item_id,
                    "state": "open",
                    "first_seen_at": now,
                    "first_seen_run_ts": run_ts,
                    "last_seen_at": now,
                    "last_seen_run_ts": run_ts,
                    "times_seen": 1,
                    "state_changed_at": None,
                    "state_changed_reason": None,
                }
                for f in copy_fields:
                    if f in src and src[f] is not None:
                        new[f] = src[f]
                doc[self.items_field].append(new)
                by_id[item_id] = new
        return doc

    def transition_state(self, item_id: str, new_state: str,
                         *, reason: str = "") -> bool:
        """Transition one item's state. Returns True if found + changed."""
        for i in self.items:
            if i.get("item_id") == item_id:
                if i.get("state") == new_state:
                    return False
                i["state"] = new_state
                i["state_changed_at"] = _now_iso()
                i["state_changed_reason"] = reason
                return True
        return False
