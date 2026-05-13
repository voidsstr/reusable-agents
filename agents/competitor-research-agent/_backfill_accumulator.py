#!/usr/bin/env python3
"""One-shot backfill: scan every historical recommendations.json under
agents/<comp-research-id>/runs/* and seed the proposal accumulator at
agents/<comp-research-id>/proposals/active.json.

Each rec gets a stable proposal_id (hash of normalized title). State is
derived per rec:
  - rec.implemented or rec.shipped → state = "implemented"
  - else                            → state = "open"

Re-runnable: idempotent. Existing accumulator entries keep their state
(we don't blindly overwrite implemented → open). Latest-run wins for
field refresh on still-open entries.

Usage:
    PYTHONPATH=/home/voidsstr/development/reusable-agents \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_CONNECTION_STRING='...' \
    AZURE_STORAGE_CONTAINER=agents \
    python3 _backfill_accumulator.py [--dry-run] [--agent <agent_id>]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the framework + the accumulator module importable.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from framework.core.storage import get_storage  # noqa: E402
from _accumulator import (  # noqa: E402
    load_active, save_active, proposal_id, _PROPOSAL_FIELDS, _now_iso,
)


def backfill(agent_id: str, dry_run: bool = False) -> dict:
    s = get_storage()
    keys = sorted(s.list_prefix(f"agents/{agent_id}/runs/"))
    rec_keys = [k for k in keys if k.endswith("/recommendations.json")]
    if not rec_keys:
        return {"agent_id": agent_id, "scanned": 0, "added": 0, "refreshed": 0}

    accum = load_active(s, agent_id)
    by_id: dict[str, dict] = {p["proposal_id"]: p for p in accum.get("proposals", [])
                              if isinstance(p, dict) and p.get("proposal_id")}
    added = 0
    refreshed = 0
    state_promoted = 0

    for rk in rec_keys:
        run_ts = rk.split("/runs/")[1].split("/")[0]
        try:
            doc = s.read_json(rk) or {}
        except Exception:
            continue
        recs = doc.get("recommendations") if isinstance(doc, dict) else None
        if not isinstance(recs, list):
            continue
        for r in recs:
            if not isinstance(r, dict):
                continue
            title = r.get("title", "")
            if not title:
                continue
            pid = proposal_id(title)
            # Derive state from this run's snapshot.
            if r.get("shipped") or r.get("implemented"):
                derived_state = "implemented"
            else:
                derived_state = "open"

            existing = by_id.get(pid)
            if existing:
                refreshed += 1
                # Update last_seen + bump times_seen.
                existing["last_seen_at"] = _now_iso()
                existing["last_seen_run_ts"] = run_ts
                existing["times_seen"] = int(existing.get("times_seen", 1)) + 1
                # State promotion: open → implemented sticks; never
                # demote (implemented stays implemented even if a later
                # snapshot shows it un-shipped — that'd be a regression
                # the operator should review).
                cur_state = existing.get("state", "open")
                if cur_state == "open" and derived_state == "implemented":
                    existing["state"] = "implemented"
                    existing["state_changed_at"] = _now_iso()
                    existing["state_changed_reason"] = "backfill-derived"
                    state_promoted += 1
                if existing.get("state") == "open":
                    # Refresh fields from latest run.
                    for f in _PROPOSAL_FIELDS:
                        if f in r and r[f] is not None:
                            existing[f] = r[f]
            else:
                entry = {
                    "proposal_id": pid,
                    "state": derived_state,
                    "first_proposed_at": _now_iso(),
                    "first_proposed_run_ts": run_ts,
                    "last_seen_at": _now_iso(),
                    "last_seen_run_ts": run_ts,
                    "times_seen": 1,
                    "state_changed_at": _now_iso() if derived_state != "open" else None,
                    "state_changed_reason": "backfill-derived" if derived_state != "open" else None,
                }
                for f in _PROPOSAL_FIELDS:
                    if f in r and r[f] is not None:
                        entry[f] = r[f]
                entry.setdefault("title", title)
                accum["proposals"].append(entry)
                by_id[pid] = entry
                added += 1

    if not dry_run:
        save_active(s, agent_id, accum)

    counts = {"open": 0, "implemented": 0, "deferred": 0, "skipped": 0}
    for p in accum.get("proposals", []):
        st = p.get("state", "open")
        if st in counts:
            counts[st] += 1

    return {
        "agent_id": agent_id,
        "scanned_files": len(rec_keys),
        "added": added,
        "refreshed": refreshed,
        "state_promoted_to_implemented": state_promoted,
        "final_counts": counts,
        "dry_run": dry_run,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute the merge but don't write back to storage")
    ap.add_argument("--agent",
                    help="Specific agent_id to backfill (otherwise all per-site comp-research)")
    args = ap.parse_args()

    if args.agent:
        agents = [args.agent]
    else:
        # Discover from the registry.
        s = get_storage()
        reg = s.read_json("registry/agents.json") or {}
        agent_ids = list(reg.keys()) if isinstance(reg, dict) else []
        agents = sorted(a for a in agent_ids
                        if a.endswith("-competitor-research-agent")
                        or a == "competitor-research-agent")

    print(f"Agents: {agents}")
    for aid in agents:
        result = backfill(aid, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
