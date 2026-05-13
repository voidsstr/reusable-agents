#!/usr/bin/env python3
"""Consolidate + requeue deferred recs.

Walks every `deferred-by-allowlist.json` across the rec-emitting agents,
backfills `type` on the source rec in `recommendations.json` (additive
only — never overwrites), dedupes globally by canonical key
(agent, type, normalized title, primary URL) keeping only the most
recent surviving rec per key, then writes one auto-queue file per
(source_agent, run_ts) so the responder picks them up on its next tick.

Stamps each `deferred-by-allowlist.json` with a `requeued_at` +
`requeue_request_id` so re-running the script doesn't double-queue.

Usage:
    PYTHONPATH=/app \
    AZURE_STORAGE_CONNECTION_STRING='...' \
    AZURE_STORAGE_CONTAINER=agents \
    python3 install/requeue-deferred.py [--dry-run] [--agent <id>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, '/app')
sys.path.insert(0, '/home/voidsstr/development/reusable-agents')

from framework.core.storage import get_storage

# Inline `queue_recs` so this script works against an older container
# that doesn't have framework/core/implementation_queue.py yet.
def queue_recs(*, source_agent, site, run_ts, rec_ids,
               subject_tag="", action="implement",
               source="auto-queue", storage=None):
    if not rec_ids:
        return ""
    s = storage or get_storage()
    if not subject_tag:
        for p in source_agent.split("-"):
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


REC_EMITTING_AGENTS = (
    "aisleprompt-progressive-improvement-agent",
    "specpicks-progressive-improvement-agent",
    "aisleprompt-seo-opportunity-agent",
    "specpicks-seo-opportunity-agent",
    "aisleprompt-catalog-audit-agent",
    "specpicks-catalog-audit-agent",
    "aisleprompt-article-author-agent",
    "specpicks-article-author-agent",
)


def _normalize_type(rec: dict) -> str:
    t = (rec.get("type") or "").strip()
    if t:
        return t
    c = (rec.get("category") or "").strip()
    if c:
        rec["type"] = c
        return c
    rec["type"] = "unclassified"
    return "unclassified"


def _norm_title(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return s[:80]


def _primary_url(rec: dict) -> str:
    url = rec.get("affected_url") or rec.get("page_url") or rec.get("url") or ""
    if not url:
        evid = rec.get("evidence") or []
        if isinstance(evid, list) and evid and isinstance(evid[0], dict):
            url = evid[0].get("url") or ""
    if url:
        url = url.split("#", 1)[0].split("?", 1)[0]
    return url


def _site_for_agent(agent_id: str) -> str:
    if agent_id.startswith("aisleprompt-"):
        return "aisleprompt"
    if agent_id.startswith("specpicks-"):
        return "specpicks"
    return "site"


def _canonical_key(agent_id: str, rec: dict) -> str:
    t = _normalize_type(rec)
    title = _norm_title(rec.get("title") or "")
    url = _primary_url(rec)
    return f"{agent_id}|{t}|{url}|{title}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--agent", help="restrict to one agent id")
    ap.add_argument("--source-tag", default="requeue-deferred-backfill",
                    help="trace tag stamped on each auto-queue file")
    args = ap.parse_args()

    s = get_storage()
    agents = (args.agent,) if args.agent else REC_EMITTING_AGENTS

    # (canonical_key) -> latest rec entry: (run_ts, agent_id, rec_id, src_rec_dict, allowlist_path)
    by_key: dict[str, tuple[str, str, str, dict, str]] = {}

    # Track per-allowlist file the rec_ids we'll be requeueing
    requeue_per_file: dict[str, list[str]] = defaultdict(list)
    files_already_requeued = 0
    recs_seen_total = 0
    recs_with_missing_source = 0
    recs_typeless_before = 0

    # Per-(agent, run_ts) collection of which files we read source recs from,
    # so we can mutate recommendations.json once at end (only if needed).
    recs_files_to_persist: dict[str, dict] = {}

    for agent_id in agents:
        keys = s.list_prefix(f"agents/{agent_id}/runs/")
        allow_keys = sorted([k for k in keys if k.endswith("/deferred-by-allowlist.json")])
        for ak in allow_keys:
            try:
                ad = s.read_json(ak) or {}
            except Exception:
                continue
            if not isinstance(ad, dict):
                continue
            if ad.get("requeued_at"):
                files_already_requeued += 1
                continue

            run_dir = ak.rsplit("/", 1)[0]
            run_ts = run_dir.rsplit("/", 1)[-1]
            recs_path = run_dir + "/recommendations.json"

            recs_doc = recs_files_to_persist.get(recs_path)
            if recs_doc is None:
                try:
                    recs_doc = s.read_json(recs_path) or {}
                except Exception:
                    recs_doc = {}
            if not isinstance(recs_doc, dict):
                continue
            recs = recs_doc.get("recommendations") or []
            recs_by_id = {(r.get("id") or r.get("rec_id")): r for r in recs
                          if isinstance(r, dict)}

            for entry in (ad.get("deferred") or []):
                if not isinstance(entry, dict):
                    continue
                rid = entry.get("rec_id") or entry.get("id")
                if not rid:
                    continue
                src = recs_by_id.get(rid)
                recs_seen_total += 1
                if src is None:
                    recs_with_missing_source += 1
                    continue
                # Backfill type in source rec.
                if not src.get("type"):
                    recs_typeless_before += 1
                    _normalize_type(src)
                    recs_files_to_persist[recs_path] = recs_doc

                key = _canonical_key(agent_id, src)
                # Keep newest run_ts per key.
                prev = by_key.get(key)
                if prev is None or run_ts > prev[0]:
                    by_key[key] = (run_ts, agent_id, rid, src, ak)

    # Persist mutated recs files (additive: only added type field if missing).
    if not args.dry_run:
        for path, doc in recs_files_to_persist.items():
            try:
                s.write_json(path, doc)
            except Exception as e:
                print(f"WARN: failed to write back {path}: {e}", file=sys.stderr)

    # Group dedup'd recs by (agent, run_ts) for queue_recs.
    by_dispatch: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for key, (run_ts, agent_id, rid, src, ak) in by_key.items():
        by_dispatch[(agent_id, run_ts)].append((rid, ak))

    # Type breakdown for the report.
    type_counts: dict[str, int] = defaultdict(int)
    for _, (_, _, _, src, _) in by_key.items():
        type_counts[src.get("type") or "unclassified"] += 1

    # Issue queue_recs calls.
    queued_summary: list[dict] = []
    for (agent_id, run_ts), rec_pairs in sorted(by_dispatch.items()):
        rec_ids = [rid for rid, _ in rec_pairs]
        if args.dry_run:
            request_id = f"DRYRUN-r-{run_ts}-requeue-{_site_for_agent(agent_id)}"
        else:
            # subject_tag='requeue' to avoid colliding with original auto-queue
            # request_id (which would be subject="seo"|"audit"|"improve").
            request_id = queue_recs(
                source_agent=agent_id,
                site=_site_for_agent(agent_id),
                run_ts=run_ts,
                rec_ids=rec_ids,
                subject_tag="requeue",
                action="implement",
                source=args.source_tag,
                storage=s,
            )
        queued_summary.append({
            "agent": agent_id, "run_ts": run_ts,
            "rec_count": len(rec_ids), "request_id": request_id,
        })
        # Stamp each touched allowlist file as requeued.
        if not args.dry_run:
            files_to_stamp: set[str] = set()
            for _, ak in rec_pairs:
                files_to_stamp.add(ak)
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for ak in files_to_stamp:
                try:
                    ad = s.read_json(ak) or {}
                    ad["requeued_at"] = now_iso
                    ad["requeue_request_id"] = request_id
                    ad["requeue_source_tag"] = args.source_tag
                    s.write_json(ak, ad)
                except Exception as e:
                    print(f"WARN: failed to stamp {ak}: {e}", file=sys.stderr)

    # Report.
    print("=" * 64)
    print("REQUEUE-DEFERRED REPORT" + (" (DRY RUN)" if args.dry_run else ""))
    print("=" * 64)
    print(f"  files already-requeued (skipped): {files_already_requeued}")
    print(f"  total deferred entries seen:      {recs_seen_total}")
    print(f"  source rec missing (skipped):     {recs_with_missing_source}")
    print(f"  type-backfilled in source recs:   {recs_typeless_before}")
    print(f"  unique recs after dedup:          {len(by_key)}")
    print(f"  dedup ratio:                      {(1 - len(by_key)/max(1, recs_seen_total))*100:.1f}% saved")
    print(f"  recs files mutated (persist):     {len(recs_files_to_persist)}")
    print(f"  auto-queue files written:         {len(queued_summary)}")
    print()
    print("--- post-dedup type distribution ---")
    for t, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  [{n:>4}] {t}")
    print()
    print("--- queued dispatches ---")
    for q in queued_summary[:20]:
        print(f"  {q['agent']} @ {q['run_ts']}: {q['rec_count']} rec(s) -> {q['request_id']}")
    if len(queued_summary) > 20:
        print(f"  … and {len(queued_summary)-20} more")


if __name__ == "__main__":
    main()
