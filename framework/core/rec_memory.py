"""Rec memory — shared dedup primitive for any producer agent.

Producer agents (competitor-research, progressive-improvement,
seo-opportunity, article-author, catalog-audit, head-to-head, ...)
were each re-emitting the same recs every cron tick because the
LLM prompt that generates recs had no awareness of what was already
proposed. This module gives all agents a single, uniform way to:

  1. Load the open + recently-implemented proposals from prior runs
  2. Build a `prior_block` string to inject into the LLM prompt so
     the model knows what NOT to re-emit
  3. After the run, persist the new recs back to the accumulator

Stored at `agents/<id>/state/accumulator.json` (one file per agent).

Compatible with the existing `competitor-research-agent/_accumulator.py`
schema so a future PR can collapse the two into one — for now this
file is the canonical version for all OTHER producers to import.

USAGE in a producer agent's run():

    from framework.core.rec_memory import prior_recs_prompt_block, persist_run

    prior_block = prior_recs_prompt_block(
        storage=self.storage, agent_id=self.agent_id,
        max_entries=120,
    )
    user_msg = f"... {COMPARE_INPUTS} ...{prior_block}\\n\\n... Produce up to N recs ..."
    raw = self.ai_chat([...{"role":"user","content":user_msg}...])
    ...
    persist_run(
        storage=self.storage, agent_id=self.agent_id, run_ts=self.run_ts,
        new_recs=recs,  # list of {id, title, type, ...}
    )

The LLM is explicitly instructed to skip recs equivalent to anything
in the prior block — see the system-prompt fragment `SKIP_PRIOR_RULE`
below for the canonical wording.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional


SKIP_PRIOR_RULE = (
    "NEVER propose a feature substantively equivalent to anything "
    "listed under PREVIOUSLY-PROPOSED RECOMMENDATIONS. If you would "
    "propose one of those, SKIP it and emit a DIFFERENT gap instead. "
    "The operator has already seen those — re-emitting them wastes "
    "review cycles + LLM tokens."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _accumulator_key(agent_id: str) -> str:
    return f"agents/{agent_id}/state/accumulator.json"


def _normalize_title(title: str) -> str:
    """Strip whitespace, lowercase, drop non-alphanumeric — used as the
    dedup key. Two recs are "the same" if their normalized title matches.
    """
    t = (title or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", t)[:120]


def _proposal_id(title: str) -> str:
    """Stable opaque id for a proposal, derived from its normalized title.
    Lets us match across runs even when the rec's `id` field rotates
    (e.g. rec-001 on Monday + rec-001 on Tuesday usually point at
    different actual recs)."""
    h = hashlib.sha1(_normalize_title(title).encode("utf-8")).hexdigest()
    return f"p-{h[:10]}"


def load_active(storage, agent_id: str) -> dict:
    """Read the accumulator doc. Returns an empty skeleton if missing."""
    try:
        d = storage.read_json(_accumulator_key(agent_id))
    except Exception:
        d = None
    if not isinstance(d, dict):
        d = {}
    d.setdefault("agent_id", agent_id)
    d.setdefault("proposals", [])
    d.setdefault("last_run_ts", "")
    return d


def save_active(storage, agent_id: str, doc: dict) -> None:
    storage.write_json(_accumulator_key(agent_id), doc)


def prior_recs_prompt_block(
    *,
    storage,
    agent_id: str,
    max_entries: int = 120,
    include_states: tuple[str, ...] = ("open", "implemented", "deferred"),
) -> str:
    """Build the LLM-prompt fragment that lists previously-proposed recs.

    Returns an EMPTY string when no prior recs exist (so callers can
    interpolate it unconditionally without extra branching).
    """
    doc = load_active(storage, agent_id)
    lines: list[str] = []
    for p in doc.get("proposals", []):
        state = p.get("state", "open")
        if state not in include_states:
            continue
        title = (p.get("title") or "").strip()
        if not title:
            continue
        lines.append(f"  [{state}] {title[:140]}")
        if len(lines) >= max_entries:
            break
    if not lines:
        return ""
    return (
        "\n\nPREVIOUSLY-PROPOSED RECOMMENDATIONS (already in backlog "
        "or shipped — DO NOT re-emit anything substantively equivalent; "
        "pick a DIFFERENT gap instead):\n" + "\n".join(lines)
    )


def merge_run(doc: dict, *, new_recs: list[dict], run_ts: str) -> dict:
    """Merge this run's emitted recs into the accumulator. Existing
    proposals (matched by normalized title) get `last_seen_at` bumped
    and `seen_count` incremented. Genuinely new titles get appended in
    state=open."""
    proposals = list(doc.get("proposals", []))
    by_norm = {_normalize_title(p.get("title", "")): p for p in proposals}
    now = _now_iso()
    added = 0
    bumped = 0
    for r in new_recs or []:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "").strip()
        if not title:
            continue
        norm = _normalize_title(title)
        if norm in by_norm:
            existing = by_norm[norm]
            existing["last_seen_at"] = now
            existing["last_seen_run_ts"] = run_ts
            existing["seen_count"] = int(existing.get("seen_count", 1)) + 1
            bumped += 1
        else:
            proposals.append({
                "id": _proposal_id(title),
                "title": title,
                "rec_type": r.get("type") or r.get("category") or "",
                "severity": r.get("severity", "medium"),
                "state": "open",
                "first_seen_at": now,
                "last_seen_at": now,
                "first_seen_run_ts": run_ts,
                "last_seen_run_ts": run_ts,
                "seen_count": 1,
                # Carry the rec body so we can re-dispatch the full content later
                "body": {
                    k: r.get(k) for k in (
                        "description", "rationale", "files", "evidence",
                        "expected_impact", "confidence", "tier_recommendation",
                    ) if r.get(k) is not None
                },
            })
            added += 1
    doc["proposals"] = proposals
    doc["last_run_ts"] = run_ts
    doc["last_merge"] = {"added": added, "bumped": bumped, "at": now}
    return doc


def transition(
    doc: dict, proposal_id: str, *, to_state: str, run_ts: str,
    implementation_sha: Optional[str] = None,
) -> bool:
    """Move a proposal to a new state (implemented / deferred / skipped).
    Returns True if a proposal was found + updated."""
    for p in doc.get("proposals", []):
        if p.get("id") == proposal_id:
            p["state"] = to_state
            p[f"{to_state}_at"] = _now_iso()
            p[f"{to_state}_run_ts"] = run_ts
            if implementation_sha:
                p["implementation_sha"] = implementation_sha
            return True
    return False


def open_proposals(doc: dict) -> list[dict]:
    return [p for p in doc.get("proposals", []) if p.get("state") == "open"]


def stats(doc: dict) -> dict:
    by_state: dict[str, int] = {}
    for p in doc.get("proposals", []):
        st = p.get("state", "open")
        by_state[st] = by_state.get(st, 0) + 1
    return by_state


def persist_run(
    *,
    storage,
    agent_id: str,
    run_ts: str,
    new_recs: list[dict],
) -> dict:
    """Convenience: load → merge → save in one call. Returns updated stats."""
    doc = load_active(storage, agent_id)
    merge_run(doc, new_recs=new_recs, run_ts=run_ts)
    save_active(storage, agent_id, doc)
    return stats(doc)
