"""Producer history — prevents an agent from re-emitting recs it has
already proposed in prior runs.

The problem this solves:
  Producer agents (article-author, seo-opp, PI, catalog-audit, etc.)
  run on a cron and regenerate `recommendations.json` from scratch
  each tick. Without history-awareness, they emit the SAME logical
  recs every tick — e.g. aisleprompt-article-author emitted "The
  Complete Keto Meal Plan: 40+ Recipes" in 8 separate run_ts dirs
  over 5 days because the producer didn't know it already proposed
  that title. Even though the dispatcher's title-cache catches it
  downstream (so the implementer doesn't re-process), the producer
  bloats its own `recommendations.json` history with duplicates,
  the dashboard reports inflated pending counts, and the producer
  burns LLM tokens generating recs that will be skipped.

Design:
  - A SINGLE rolling cache per agent at
    `agents/<agent_id>/state/emitted-titles.json` — capped list of
    {sig, title, first_emitted_at, last_emitted_at, state} entries.
  - `load_history(agent_id)` returns the set of title signatures the
    agent has ever emitted (one blob read).
  - `filter_proposals(new_recs, agent_id)` drops proposals whose
    title-signature is already in history. Returns deduped list.
  - `record_emitted(recs, agent_id)` appends the surviving titles to
    the cache. Cap at HISTORY_MAX_TITLES (default 1000) by oldest.

Usage in a producer's run():

    from framework.core.producer_history import (
        filter_proposals, record_emitted,
    )

    proposals = self._generate_proposals(...)  # LLM call
    kept = filter_proposals(proposals, self.agent_id, self.storage)
    # ... persist `kept` to recommendations.json as usual ...
    record_emitted(kept, self.agent_id, self.storage)

That's three lines added to each producer. Cache reads are 1 blob
GET per tick (~5-20KB). Writes are 1 blob PUT per tick.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .title_dedup import title_signature

HISTORY_KEY_TEMPLATE = "agents/{agent_id}/state/emitted-titles.json"
HISTORY_MAX_TITLES = 1000


def _key(agent_id: str) -> str:
    return HISTORY_KEY_TEMPLATE.format(agent_id=agent_id)


def load_history(agent_id: str, storage: Any) -> dict[str, dict]:
    """Return {sig_str: entry_dict} for all titles this agent has emitted.

    `sig_str` is the title-signature joined by `|` for stable dict
    keys (frozensets aren't JSON-serializable).
    """
    try:
        doc = storage.read_json(_key(agent_id)) or {}
    except Exception:
        return {}
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return {}
    out = {}
    for e in entries:
        if isinstance(e, dict) and e.get("sig"):
            out[str(e["sig"])] = e
    return out


def _sig_str(title: str) -> str:
    sig = title_signature(title or "")
    return "|".join(sorted(sig)) if sig else ""


def filter_proposals(
    new_recs: list[dict],
    agent_id: str,
    storage: Any,
    *,
    title_key: str = "title",
    on_skip: Callable[[str], None] | None = None,
) -> list[dict]:
    """Drop proposals whose title-signature is already in this agent's
    emitted-titles history. Returns the kept list (same order).
    """
    if not new_recs:
        return []
    history = load_history(agent_id, storage)
    seen_sigs: set[str] = set(history.keys())
    kept: list[dict] = []
    for r in new_recs:
        if not isinstance(r, dict):
            continue
        title = (r.get(title_key) or "").strip()
        sig = _sig_str(title)
        if not sig:
            kept.append(r)
            continue
        if sig in seen_sigs:
            if on_skip:
                on_skip(title)
            continue
        seen_sigs.add(sig)  # also dedups within this batch
        kept.append(r)
    return kept


def record_emitted(
    recs: Iterable[dict],
    agent_id: str,
    storage: Any,
    *,
    title_key: str = "title",
    max_entries: int = HISTORY_MAX_TITLES,
) -> int:
    """Append titles from `recs` to the agent's emitted-titles cache.
    Returns the number of new entries added.

    Cap-strategy: when exceeding `max_entries`, drop the oldest by
    `first_emitted_at`. Rolling window means truly-old titles can
    eventually be re-emitted (after ~1000 newer recs), which is the
    desired behavior — a topic stays "off-limits" for a long time
    but isn't blocklisted forever.
    """
    history = load_history(agent_id, storage)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    for r in recs:
        if not isinstance(r, dict):
            continue
        title = (r.get(title_key) or "").strip()
        sig = _sig_str(title)
        if not sig:
            continue
        if sig in history:
            # Refresh last_emitted_at for accurate "still in flight" signals
            history[sig]["last_emitted_at"] = now
            continue
        history[sig] = {
            "sig": sig,
            "title": title[:200],
            "first_emitted_at": now,
            "last_emitted_at": now,
        }
        added += 1
    # Cap at max_entries by oldest first_emitted_at
    if len(history) > max_entries:
        sorted_entries = sorted(
            history.values(),
            key=lambda e: e.get("first_emitted_at", ""),
        )
        history = {e["sig"]: e for e in sorted_entries[-max_entries:]}
    storage.write_json(
        _key(agent_id),
        {
            "schema_version": "1",
            "entries": list(history.values()),
            "count": len(history),
            "updated_at": now,
        },
    )
    return added


def is_emitted(title: str, agent_id: str, storage: Any) -> bool:
    """Quick single-title check. Convenience wrapper for filters that
    want to inspect ONE title without doing a bulk filter call."""
    sig = _sig_str(title)
    if not sig:
        return False
    history = load_history(agent_id, storage)
    return sig in history
