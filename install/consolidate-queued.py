#!/usr/bin/env python3
"""Consolidate queued recs that target the same URL into one mega-rec.

When N recs from the same agent target the same URL, we collapse them
into a single `page-cleanup` mega-rec whose rationale lists all the
sub-issues. The implementer then processes them in one LLM call with
full context of every problem on the page, instead of N calls each
seeing only one issue.

Mutations are minimal + auditable:
  • In recommendations.json: keep all original recs, mark each
    consolidated rec with `consolidated_into: <mega_rec_id>` (no
    deletion). Append the new mega-rec to the array.
  • In auto-queue/<request_id>.json: replace the consolidated rec_ids
    with the single mega-rec id. If a file becomes empty, delete it.
  • Stamp each touched run dir with `consolidated_at` in
    recommendations.json metadata.

Idempotent — re-running skips groups already collapsed.

Usage:
    python3 install/consolidate-queued.py [--dry-run] [--min-group-size 2]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, '/app')
sys.path.insert(0, '/home/voidsstr/development/reusable-agents')

from framework.core.storage import get_storage


def _primary_url(rec: dict) -> str:
    url = (rec.get("affected_url") or rec.get("page_url")
           or rec.get("url") or "")
    if not url:
        evid = rec.get("evidence") or []
        if isinstance(evid, list) and evid and isinstance(evid[0], dict):
            url = evid[0].get("url") or ""
    return (url or "").split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _build_mega_rec(group: list[dict], url: str) -> dict:
    """Synthesize a single rec representing all issues on this page."""
    types = sorted({(r.get("type") or r.get("category") or "?").lower()
                    for r in group})
    severities = [r.get("severity", "") for r in group if r.get("severity")]
    severity = "high" if "high" in severities or "critical" in severities \
        else (severities[0] if severities else "medium")

    sub_issues = []
    for r in group:
        rid = r.get("id") or r.get("rec_id") or "?"
        t = r.get("type") or r.get("category") or "?"
        title = (r.get("title") or "").strip()
        rat = (r.get("rationale") or "").strip()
        sub_issues.append(
            f"  • [{rid}] type={t}\n"
            f"    title: {title}\n"
            f"    rationale: {rat}"
        )
    rationale = (
        f"This page has {len(group)} accumulated quality issues spanning "
        f"types: {', '.join(types)}. Fix all of them in one pass — they "
        f"all touch the same template/data so a single edit cycle can "
        f"address every issue listed below.\n\n"
        f"Page: {url}\n\n"
        f"Issues:\n" + "\n\n".join(sub_issues)
    )

    return {
        "type": "page-cleanup",
        "category": "page-cleanup",
        "severity": severity,
        "tier": "review",
        "confidence": 0.85,
        "title": f"Page cleanup ({len(group)} issues): {url}",
        "rationale": rationale,
        "expected_impact": (
            f"Resolves {len(group)} separate quality flags in one edit pass; "
            f"saves the implementer ~{len(group)-1} additional LLM calls."
        ),
        "affected_url": url,
        "page_url": url,
        "url": url,
        "consolidated_from": sorted(
            r.get("id") or r.get("rec_id") for r in group),
        "consolidated_types": types,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-group-size", type=int, default=2,
                    help="only consolidate groups with at least this many recs")
    args = ap.parse_args()

    s = get_storage()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Walk all auto-queue files. Group recs in each file by URL.
    auto_q_keys = sorted([
        k for k in s.list_prefix("agents/responder-agent/auto-queue/")
        if k.endswith(".json")
    ])

    # (agent, run_ts) -> list of rec_ids referenced from any auto-queue
    queued_per_run: dict[tuple[str, str], set[str]] = defaultdict(set)
    queue_file_recs: dict[str, dict] = {}   # ak -> auto-queue payload

    for ak in auto_q_keys:
        try:
            aq = s.read_json(ak) or {}
        except Exception:
            continue
        queue_file_recs[ak] = aq
        agent = aq.get("source_agent", "")
        run_ts = aq.get("run_ts", "")
        for rid in aq.get("rec_ids") or []:
            if agent and run_ts and rid:
                queued_per_run[(agent, run_ts)].add(rid)

    # For each (agent, run_ts), load recommendations.json, group by url,
    # consolidate groups ≥ min_group_size.
    summary = {
        "runs_touched": 0, "groups_consolidated": 0,
        "recs_consolidated_in": 0, "mega_recs_created": 0,
        "queue_files_updated": 0, "queue_files_deleted": 0,
    }
    # Track new mega-rec ids per run, mapped from consolidated source ids
    consolidated_src_to_mega: dict[tuple[str, str, str], str] = {}

    for (agent, run_ts), queued_ids in queued_per_run.items():
        rk = f"agents/{agent}/runs/{run_ts}/recommendations.json"
        try:
            rd = s.read_json(rk) or {}
        except Exception:
            continue
        if not isinstance(rd, dict):
            continue
        recs = rd.get("recommendations") or []
        if not isinstance(recs, list):
            continue
        # Already consolidated this run? skip.
        if rd.get("consolidated_at"):
            continue

        by_id = {(r.get("id") or r.get("rec_id")): r for r in recs
                 if isinstance(r, dict)}
        # Build URL groups, but ONLY for currently-queued recs (don't
        # consolidate recs that aren't even pending).
        by_url: dict[str, list[dict]] = defaultdict(list)
        for rid in queued_ids:
            r = by_id.get(rid)
            if not r:
                continue
            # Skip recs already consolidated.
            if r.get("consolidated_into"):
                continue
            url = _primary_url(r)
            if not url:
                continue
            by_url[url].append(r)

        # For each multi-rec URL, synthesize a mega-rec.
        run_dirty = False
        for url, group in by_url.items():
            if len(group) < args.min_group_size:
                continue
            mega = _build_mega_rec(group, url)
            mega["id"] = f"rec-cons-{len(recs)+1:03d}"
            recs.append(mega)
            for r in group:
                r["consolidated_into"] = mega["id"]
                consolidated_src_to_mega[
                    (agent, run_ts, r.get("id") or r.get("rec_id"))
                ] = mega["id"]
            summary["groups_consolidated"] += 1
            summary["mega_recs_created"] += 1
            summary["recs_consolidated_in"] += len(group)
            run_dirty = True

        if run_dirty:
            rd["consolidated_at"] = now_iso
            rd["recommendations"] = recs
            if not args.dry_run:
                s.write_json(rk, rd)
            summary["runs_touched"] += 1

    # Now rewrite the auto-queue files to swap consolidated rec_ids
    # for their mega-rec id. If a file has no remaining ids, delete it.
    for ak, aq in queue_file_recs.items():
        agent = aq.get("source_agent", "")
        run_ts = aq.get("run_ts", "")
        old_ids = list(aq.get("rec_ids") or [])
        new_ids: list[str] = []
        seen_mega: set[str] = set()
        for rid in old_ids:
            mega = consolidated_src_to_mega.get((agent, run_ts, rid))
            if mega:
                if mega not in seen_mega:
                    new_ids.append(mega)
                    seen_mega.add(mega)
            else:
                new_ids.append(rid)
        if new_ids != old_ids:
            aq["rec_ids"] = new_ids
            aq["consolidated_at"] = now_iso
            if new_ids:
                if not args.dry_run:
                    s.write_json(ak, aq)
                summary["queue_files_updated"] += 1
            else:
                if not args.dry_run:
                    try: s.delete(ak)
                    except Exception: pass
                summary["queue_files_deleted"] += 1

    print("=" * 60)
    print("CONSOLIDATION REPORT" + (" (DRY RUN)" if args.dry_run else ""))
    print("=" * 60)
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if summary["recs_consolidated_in"]:
        savings = summary["recs_consolidated_in"] - summary["mega_recs_created"]
        pct = (savings / summary["recs_consolidated_in"]) * 100
        print(f"\n  → saved {savings} LLM dispatches "
              f"({pct:.1f}% reduction in pending recs)")


if __name__ == "__main__":
    main()
