"""Implementation queue — single primitive for handing recs to the implementer.

When an analyzer/audit/research agent produces recommendations and wants
them shipped, it writes ONE auto-queue file under
`agents/responder-agent/auto-queue/<request-id>.json`. The responder
picks the file up on its next tick and dispatches batches to the
implementer.

Before this module existed, every producer agent re-implemented the
JSON shape inline. Centralizing it here means:
  - one schema, audit-able in one place
  - any future change (add a field, change request-id format) is a
    one-file edit
  - producer agents call `queue_recs(...)` and don't carry the contract

Used by `AgentBase.queue_recs(...)` — most agents should hit that
shorthand. Direct callers: orchestrators that don't subclass AgentBase
(if any remain after the AgentBase-mandatory directive).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


def queue_recs(
    *,
    source_agent: str,
    site: str,
    run_ts: str,
    rec_ids: list[str],
    subject_tag: str = "",
    action: str = "implement",
    source: str = "auto-queue",
    storage: Optional[StorageBackend] = None,
) -> str:
    """Write the responder auto-queue file. Returns the request_id.

    Producer agents call this at the end of run() once the work product
    (recommendations.json) is on disk. The responder picks it up.

    Args:
      source_agent  agent_id that produced the recs (per-site: e.g.
                    "aisleprompt-seo-opportunity-agent")
      site          short site label, free-form ("aisleprompt", "specpicks")
      run_ts        canonical run-ts ("YYYYMMDDTHHMMSSZ") — used in
                    request_id and in the dispatched job's reference
                    back to the originating run
      rec_ids       list of rec ids to dispatch ("rec-001", "art-014")
      subject_tag   short tag for routing/filtering ("seo", "audit",
                    "article", "improve") — defaults to source_agent's
                    leading word
      action        "implement" | "skip" — usually "implement"
      source        free-form trace tag for "where did this come from",
                    purely for debugging the dispatch chain
    """
    if not rec_ids:
        return ""
    s = storage or get_storage()
    if not subject_tag:
        # heuristic: first non-site segment of agent id
        parts = source_agent.split("-")
        for p in parts:
            if p not in ("aisleprompt", "specpicks", "agent", "opportunity"):
                subject_tag = p
                break
    request_id = f"r-{run_ts}-{subject_tag or 'work'}-{site or 'site'}"
    payload = {
        "schema_version": "1",
        "request_id": request_id,
        "source_agent": source_agent,
        "subject_tag": subject_tag,
        "site": site,
        "run_ts": run_ts,
        "rec_ids": list(rec_ids),
        "action": action,
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
    }
    s.write_json(f"agents/responder-agent/auto-queue/{request_id}.json", payload)
    return request_id
