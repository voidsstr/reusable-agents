#!/usr/bin/env python3
"""Reconcile shipped:true / shipped_at on article-author recs by querying
the live editorial_articles table for matching slugs.

This catches the gap where the wrapper's article-INSERT step succeeded
(DB row exists, /blog/<slug> is live) but the rundir's recommendations.json
was never updated with shipped:true. Future implementer runs use the JSON
file as authority for "is this rec shipped" so the gap silently grows.
"""
import json, os, sys, glob, datetime
import psycopg2

DSN_BY_SITE = {
    "aisleprompt": os.environ.get("DATABASE_URL_AISLEPROMPT") or os.environ.get("AISLEPROMPT_DSN"),
    "specpicks":   os.environ.get("DATABASE_URL_SPECPICKS")   or os.environ.get("SPECPICKS_DSN"),
}

def shipped_slugs_for_site(site):
    dsn = DSN_BY_SITE.get(site)
    if not dsn:
        return {}
    conn = psycopg2.connect(dsn, connect_timeout=10)
    cur = conn.cursor()
    cur.execute("""SELECT slug, id, written_by, created_at, published_at
                   FROM editorial_articles""")
    out = {}
    for slug, row_id, written_by, created_at, published_at in cur.fetchall():
        out[slug] = {
            "row_id": row_id,
            "written_by": written_by or "",
            "created_at": (created_at.isoformat() if created_at else None),
            "published_at": (published_at.isoformat() if published_at else None),
        }
    conn.close()
    return out

def reconcile_recs_file(path, slugs_index):
    raw = json.loads(open(path).read())
    recs = raw.get("recommendations") if isinstance(raw, dict) else raw
    if not isinstance(recs, list):
        return 0, 0
    flipped = 0
    seen_articles = 0
    for r in recs:
        proposal = r.get("proposal") or r.get("article_proposal") or {}
        slug = proposal.get("slug") or r.get("slug")
        if not slug or slug not in slugs_index:
            continue
        seen_articles += 1
        if r.get("shipped"):
            continue  # already marked
        info = slugs_index[slug]
        r["shipped"] = True
        r["shipped_at"] = info.get("published_at") or info.get("created_at")
        r["shipped_via"] = "framework-article-insert"
        r["shipped_table_row_id"] = info["row_id"]
        r["shipped_written_by"] = info["written_by"]
        # Mirror implemented:true if not already set
        if not r.get("implemented"):
            r["implemented"] = True
            r["implemented_at"] = r["shipped_at"]
            r["implemented_via"] = "framework-article-insert"
        flipped += 1
    if flipped:
        if isinstance(raw, dict):
            raw["recommendations"] = recs
        else:
            raw = recs
        with open(path, "w") as f:
            json.dump(raw, f, indent=2)
    return flipped, seen_articles

def main():
    slug_idx = {site: shipped_slugs_for_site(site) for site in DSN_BY_SITE}
    for site, idx in slug_idx.items():
        print(f"=== {site}: {len(idx)} live articles in editorial_articles")
    total_flipped = 0
    total_seen = 0
    files_touched = 0
    for site in slug_idx:
        if not slug_idx[site]:
            continue
        # Walk both local rundirs and the data/agents storage path
        patterns = [
            f"/home/voidsstr/.reusable-agents/data/agents/{site}-article-author-agent/runs/*/recommendations.json",
            f"/home/voidsstr/.reusable-agents/{site}-article-author/runs/{site}/*/recommendations.json",
            f"/home/voidsstr/.reusable-agents/{site}-article-author-agent/runs/*/recommendations.json",
            f"/home/voidsstr/.reusable-agents/{site}-article-author/runs/{site}/*/recommendations.json",
            f"/home/voidsstr/.reusable-agents/data/agents/{site}-article-author-agent/runs/*/recommendations.json",
            f"/tmp/rundir-{site}-article-author-agent-*/recommendations.json",
        ]
        for pat in patterns:
            for path in glob.glob(pat):
                f, s = reconcile_recs_file(path, slug_idx[site])
                if s:
                    print(f"  [{site}] {path}: {f}/{s} flipped")
                    total_flipped += f
                    total_seen += s
                    if f: files_touched += 1
    print(f"\nTOTAL: flipped {total_flipped} shipped flags across {files_touched} files "
          f"({total_seen} article recs total seen)")

if __name__ == "__main__":
    main()
