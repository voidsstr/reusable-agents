"""Per-run decision log + per-agent changelog.

Decisions are append-only, structured, and live alongside each run dir:
  agents/<id>/runs/<run-ts>/decisions.jsonl

Each decision is a single JSON object on its own line — easy to grep,
ingest into time-series tools, replay during debugging, or summarize
into a `context-summary.md` for the next run.

The changelog lives at agents/<id>/changelog.jsonl and tracks code-shipping
events (release tags, deploys, prompt rewrites). Decisions are about *why*
the agent did what it did; the changelog is about *what happened* in the
real world.

Design note: jsonl over json — append is O(1), and it survives partial
writes / crashes since each line is self-contained.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.decisions")


def decisions_key(agent_id: str, run_ts: str) -> str:
    return f"agents/{agent_id}/runs/{run_ts}/decisions.jsonl"


def changelog_key(agent_id: str) -> str:
    return f"agents/{agent_id}/changelog.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

class DecisionLog:
    """Decision recorder for a single run."""

    # Standard decision categories — agents may use others, but these get
    # special treatment in summaries (rendered as section headers).
    STANDARD_CATEGORIES = {
        "plan": "What I plan to do",
        "observation": "Something I noticed",
        "choice": "Why I chose this option",
        "skip": "Why I skipped something",
        "defer": "Deferred until a later run",
        "warning": "Caveat the next run should know",
        "result": "Outcome of an action",
    }

    def __init__(self, agent_id: str, run_ts: str, storage: Optional[StorageBackend] = None):
        self.agent_id = agent_id
        self.run_ts = run_ts
        self.storage = storage or get_storage()

    def record(
        self,
        category: str,
        message: str,
        *,
        rec_id: Optional[str] = None,
        action: Optional[str] = None,
        evidence: Optional[dict] = None,
    ) -> None:
        """Append a decision entry."""
        entry: dict[str, Any] = {
            "ts": _now(),
            "category": category,
            "message": message,
        }
        if rec_id:    entry["rec_id"] = rec_id
        if action:    entry["action"] = action
        if evidence:  entry["evidence"] = evidence
        try:
            self.storage.append_jsonl(decisions_key(self.agent_id, self.run_ts), entry)
        except Exception as e:
            logger.warning(f"decision append failed: {e}")

    # Shorthands for common categories
    def plan(self, message: str, **kw: Any) -> None: self.record("plan", message, **kw)
    def observe(self, message: str, **kw: Any) -> None: self.record("observation", message, **kw)
    def choose(self, message: str, **kw: Any) -> None: self.record("choice", message, **kw)
    def skip(self, message: str, **kw: Any) -> None: self.record("skip", message, **kw)
    def defer(self, message: str, **kw: Any) -> None: self.record("defer", message, **kw)
    def warn(self, message: str, **kw: Any) -> None: self.record("warning", message, **kw)
    def result(self, message: str, **kw: Any) -> None: self.record("result", message, **kw)


def read_decisions(
    agent_id: str,
    run_ts: str,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    return (storage or get_storage()).read_jsonl(decisions_key(agent_id, run_ts))


def summarize_decisions_md(
    agent_id: str,
    run_ts: str,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Render a run's decisions as a Markdown summary, grouped by category.

    Used by post_run() when writing context-summary.md (the verbose
    narrative the next run pulls in for context).
    """
    entries = read_decisions(agent_id, run_ts, storage)
    if not entries:
        return f"# {agent_id} — {run_ts}\n\n_No decisions recorded._\n"
    grouped: dict[str, list[dict]] = {}
    for e in entries:
        grouped.setdefault(e.get("category", "other"), []).append(e)

    lines = [f"# {agent_id} — {run_ts}", ""]
    for cat in (
        "plan", "observation", "choice", "skip", "defer", "warning", "result",
    ):
        if cat not in grouped:
            continue
        header = DecisionLog.STANDARD_CATEGORIES.get(cat, cat.title())
        lines.append(f"## {header}")
        for e in grouped[cat]:
            ts = e.get("ts", "")
            msg = e.get("message", "")
            extras = []
            if e.get("rec_id"):  extras.append(f"`{e['rec_id']}`")
            if e.get("action"):  extras.append(f"action=`{e['action']}`")
            extra_str = " — " + ", ".join(extras) if extras else ""
            lines.append(f"- _{ts}_ {msg}{extra_str}")
            if e.get("evidence"):
                import json
                ev = json.dumps(e["evidence"], indent=2, default=str)
                # Indent the evidence block as nested markdown
                ev_indented = "\n".join("  > " + ln for ln in ev.splitlines())
                lines.append(ev_indented)
        lines.append("")
    # Catch-all for non-standard categories
    extras = sorted(c for c in grouped if c not in DecisionLog.STANDARD_CATEGORIES)
    for cat in extras:
        lines.append(f"## {cat.title()}")
        for e in grouped[cat]:
            lines.append(f"- _{e.get('ts','')}_ {e.get('message','')}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Changelog (real-world consequences — releases, deploys)
# ---------------------------------------------------------------------------

def append_changelog(
    agent_id: str,
    *,
    kind: str,                  # release | deploy | prompt-update | directive-change | other
    message: str,
    release_id: Optional[str] = None,
    commit_sha: Optional[str] = None,
    files: Optional[list[str]] = None,
    extra: Optional[dict] = None,
    storage: Optional[StorageBackend] = None,
) -> None:
    s = storage or get_storage()
    entry: dict[str, Any] = {
        "ts": _now(),
        "agent_id": agent_id,
        "kind": kind,
        "message": message,
    }
    if release_id: entry["release_id"] = release_id
    if commit_sha: entry["commit_sha"] = commit_sha
    if files:      entry["files"] = files
    if extra:      entry["extra"] = extra
    try:
        s.append_jsonl(changelog_key(agent_id), entry)
    except Exception as e:
        logger.warning(f"changelog append failed for {agent_id}: {e}")


def read_changelog(
    agent_id: str,
    limit: int = 200,
    storage: Optional[StorageBackend] = None,
) -> list[dict]:
    s = storage or get_storage()
    entries = s.read_jsonl(changelog_key(agent_id))
    return entries[-limit:]
