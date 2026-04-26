"""Date-indexed prior-run context — keeps each run's context budget small
while letting the agent learn over time.

Each run writes a verbose `context-summary.md` to its run dir. The framework
also keeps a per-day rollup at `agents/<id>/context-summaries/<YYYY-MM-DD>.md`
that's typically a concatenation of that day's per-run summaries (or, for
high-frequency agents, a hand-summarized digest).

When `pre_run()` runs, the agent calls
  `find_context(since="2026-04-01", limit_chars=20000)`
which loads recent summaries up to a character budget — the agent gets
recent memory without the context window exploding.

Why date-indexed and not LLM-vectorized:
- Easy to debug; humans can read a day's summary
- Survives `git diff`-style review
- No vector DB to operate
- LLM-driven retrieval can be added later as an optimization

The framework provides utilities to build the rollup. Agents typically
call `record_run_context()` in post_run() then occasionally
`build_daily_rollup()` (e.g., on a midnight cron) to compress that day's
runs into a single summary.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.context")


def run_summary_key(agent_id: str, run_ts: str) -> str:
    return f"agents/{agent_id}/runs/{run_ts}/context-summary.md"


def daily_rollup_key(agent_id: str, ymd: str) -> str:
    return f"agents/{agent_id}/context-summaries/{ymd}.md"


def _today_ymd() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record_run_context(
    agent_id: str,
    run_ts: str,
    summary_md: str,
    storage: Optional[StorageBackend] = None,
) -> None:
    """Write the verbose narrative for this run.
    Called from post_run() after summarize_decisions_md() etc.
    """
    s = storage or get_storage()
    s.write_text(run_summary_key(agent_id, run_ts), summary_md)


def find_context(
    agent_id: str,
    *,
    since: Optional[str] = None,           # YYYY-MM-DD lower bound (inclusive)
    limit_chars: int = 20000,              # cap on total chars returned
    prefer_daily_rollups: bool = True,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Load recent context for the agent, newest-first, up to limit_chars.

    Strategy:
      1. If prefer_daily_rollups: list all daily rollups in date order, take newest first.
      2. If still under budget after rollups: take per-run summaries from the last
         24 hours (most recent runs).
      3. Concatenate with separators; truncate at the budget.

    Returns: a single Markdown blob suitable for prepending to the agent's
    next-run prompt or context buffer.
    """
    s = storage or get_storage()
    chunks: list[str] = []
    used = 0

    if prefer_daily_rollups:
        rollups_prefix = f"agents/{agent_id}/context-summaries/"
        rollup_keys = sorted(s.list_prefix(rollups_prefix), reverse=True)  # newest YYYY-MM-DD first
        for key in rollup_keys:
            ymd = key.removeprefix(rollups_prefix).removesuffix(".md")
            if since and ymd < since:
                break
            content = s.read_text(key) or ""
            if not content.strip():
                continue
            sep = f"\n\n---\n\n## {ymd}\n\n"
            if used + len(sep) + len(content) > limit_chars:
                # Take a partial chunk if there's room
                remaining = limit_chars - used - len(sep) - 100  # keep some slack
                if remaining > 200:
                    chunks.append(sep + content[:remaining] + "\n\n…(truncated)…")
                    used = limit_chars
                break
            chunks.append(sep + content)
            used += len(sep) + len(content)

    # Fall back / supplement with recent per-run summaries
    if used < limit_chars * 0.5:
        runs_prefix = f"agents/{agent_id}/runs/"
        run_keys = sorted(
            (k for k in s.list_prefix(runs_prefix) if k.endswith("/context-summary.md")),
            reverse=True,
        )
        for key in run_keys[:24]:  # cap at last 24 runs
            content = s.read_text(key) or ""
            if not content.strip():
                continue
            sep = f"\n\n---\n\n"
            if used + len(sep) + len(content) > limit_chars:
                break
            chunks.append(sep + content)
            used += len(sep) + len(content)

    return "".join(chunks).strip()


def build_daily_rollup(
    agent_id: str,
    ymd: Optional[str] = None,
    storage: Optional[StorageBackend] = None,
) -> Optional[str]:
    """Concatenate today's (or YYYY-MM-DD's) per-run context summaries
    into the daily rollup.

    Idempotent — safe to call repeatedly during the day; later calls
    just append more recent runs to the rollup.

    Returns the rollup content, or None if no runs that day.
    """
    s = storage or get_storage()
    target_ymd = ymd or _today_ymd()
    runs_prefix = f"agents/{agent_id}/runs/"
    runs_today: list[tuple[str, str]] = []  # (run_ts, content)
    for key in sorted(s.list_prefix(runs_prefix)):
        # key looks like agents/<id>/runs/<run-ts>/context-summary.md
        if not key.endswith("/context-summary.md"):
            continue
        run_ts = key[len(runs_prefix):].split("/", 1)[0]
        # run_ts format: 20260426T120000Z
        if len(run_ts) < 8:
            continue
        run_ymd = f"{run_ts[:4]}-{run_ts[4:6]}-{run_ts[6:8]}"
        if run_ymd != target_ymd:
            continue
        content = s.read_text(key) or ""
        if content.strip():
            runs_today.append((run_ts, content))

    if not runs_today:
        return None

    rollup_lines = [f"# {agent_id} — daily rollup — {target_ymd}", ""]
    for run_ts, content in runs_today:
        rollup_lines.append(f"## Run {run_ts}")
        rollup_lines.append("")
        rollup_lines.append(content.strip())
        rollup_lines.append("")
    rollup = "\n".join(rollup_lines)
    s.write_text(daily_rollup_key(agent_id, target_ymd), rollup)
    return rollup
