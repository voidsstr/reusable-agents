#!/usr/bin/env python3
"""Backfill agents/<id>/run-index.json from existing run dirs.

Call this once after deploying the run-index optimization so historical
agents (whose runs predate the upgrade) immediately benefit from the fast
list path. Idempotent — re-running just rewrites the index.

Usage:
    # Backfill all registered agents
    python3 framework/scripts/backfill-run-indexes.py

    # Backfill a specific agent
    python3 framework/scripts/backfill-run-indexes.py specpicks-seo-opportunity-agent

Env:
    AZURE_STORAGE_CONNECTION_STRING — required (or STORAGE_BACKEND=local)
    AZURE_STORAGE_CONTAINER         — defaults to 'agents'
"""
from __future__ import annotations

import argparse
import logging
import sys

# Make the framework package importable when run from anywhere
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))   # repo root
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from framework.core import registry, storage  # noqa: E402


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("backfill-run-indexes")


def backfill_one(s, agent_id: str, max_recent: int = 50) -> dict:
    prefix = f"agents/{agent_id}/runs/"
    keys = sorted(
        (k for k in s.list_prefix(prefix) if k.endswith("/progress.json")),
        reverse=True,
    )
    log.info("  %s: %d runs found", agent_id, len(keys))
    recent = []
    for key in keys[:max_recent]:
        d = s.read_json(key)
        if not d:
            continue
        recent.append({
            "agent_id":        agent_id,
            "run_ts":          d.get("run_ts", ""),
            "status":          d.get("status", ""),
            "summary":         d.get("summary", "") or "",
            "started_at":      d.get("started_at", "") or "",
            "ended_at":        d.get("ended_at"),
            "iteration_count": int(d.get("iteration_count", 0) or 0),
            "progress":        float(d.get("progress", 0.0) or 0.0),
            "metrics":         d.get("metrics") or {},
            "triggered_by":    d.get("triggered_by", "") or "",
        })
    idx = {"total_runs": len(keys), "recent": recent}
    s.write_json(f"agents/{agent_id}/run-index.json", idx)
    return idx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent_ids", nargs="*",
                        help="Specific agent ids to backfill (default: all registered)")
    parser.add_argument("--max-recent", type=int, default=50,
                        help="Cap recent[] at this many entries (default 50)")
    args = parser.parse_args()

    s = storage.get_storage()
    log.info("storage backend: %s", s.name)

    if args.agent_ids:
        ids = list(args.agent_ids)
    else:
        ids = [m.id for m in registry.list_agents(s)]
        log.info("backfilling all %d registered agents", len(ids))

    ok = 0
    for aid in ids:
        try:
            idx = backfill_one(s, aid, max_recent=args.max_recent)
            log.info("    -> wrote run-index.json (total_runs=%d, recent=%d)",
                     idx["total_runs"], len(idx["recent"]))
            ok += 1
        except Exception as e:
            log.warning("    !! %s failed: %s", aid, e)
    log.info("done — %d/%d agents indexed", ok, len(ids))
    return 0 if ok == len(ids) else 1


if __name__ == "__main__":
    sys.exit(main())
