#!/usr/bin/env python3
"""Backfill shipped:true into Azure-stored recommendations.json files.

The earlier reconciler (agents/implementer/reconcile-shipped.py) wrote
to LOCAL filesystem paths under ~/.reusable-agents/. The framework API
reads from Azure blob storage under agents/<id>/runs/<ts>/recommendations.json
— so the local writes never affected the lifetime-stats endpoint.

This script does the same reconciliation but writes back to Azure.
"""
import os, sys, json
sys.path.insert(0, '/home/voidsstr/development/reusable-agents')
from framework.core.storage import get_storage
import psycopg2

s = get_storage()

# Live editorial_articles slug → meta map per site
DSN_BY_SITE = {
    "aisleprompt": os.environ.get("DATABASE_URL_AISLEPROMPT"),
    "specpicks":   os.environ.get("DATABASE_URL_SPECPICKS"),
}

def shipped_slugs(site):
    dsn = DSN_BY_SITE.get(site)
    if not dsn: return {}
    conn = psycopg2.connect(dsn, connect_timeout=10)
    cur = conn.cursor()
    cur.execute("""SELECT slug, id, written_by, created_at, published_at
                   FROM editorial_articles""")
    out = {}
    for slug, rid, wb, ca, pa in cur.fetchall():
        out[slug] = {
            "row_id": rid, "written_by": wb or "",
            "created_at": ca.isoformat() if ca else None,
            "published_at": pa.isoformat() if pa else None,
        }
    conn.close()
    return out

# ── 1. Article-author shipped reconciliation ──────────────────────
for site, slug_idx in {"aisleprompt": shipped_slugs("aisleprompt"),
                       "specpicks":   shipped_slugs("specpicks")}.items():
    print(f"=== {site}: {len(slug_idx)} live articles ===")
    if not slug_idx: continue
    agent_id = f"{site}-article-author-agent"
    keys = s.list_prefix(f"agents/{agent_id}/runs/")
    recs_keys = [k for k in keys if k.endswith("/recommendations.json")]
    print(f"  scanning {len(recs_keys)} recommendations.json files")
    flipped_files = 0
    flipped_recs = 0
    for rk in recs_keys:
        rd = s.read_json(rk)
        if not isinstance(rd, dict): continue
        recs = rd.get("recommendations") or []
        if not isinstance(recs, list): continue
        changed = False
        for r in recs:
            if not isinstance(r, dict): continue
            prop = r.get("proposal") or r.get("article_proposal") or {}
            slug = prop.get("slug") or r.get("slug")
            if slug and slug in slug_idx and not r.get("shipped"):
                info = slug_idx[slug]
                r["shipped"] = True
                r["shipped_at"] = info.get("published_at") or info.get("created_at")
                r["shipped_via"] = "framework-article-insert"
                r["shipped_table_row_id"] = info["row_id"]
                if not r.get("implemented"):
                    r["implemented"] = True
                    r["implemented_at"] = r["shipped_at"]
                    r["implemented_via"] = "framework-article-insert"
                changed = True
                flipped_recs += 1
        if changed:
            rd["recommendations"] = recs
            s.write_json(rk, rd)
            flipped_files += 1
    print(f"  ✓ flipped {flipped_recs} shipped flags across {flipped_files} files")

# ── 2. SEO + progressive code-rec shipped reconciliation ──────────
# Code recs ship when a deploy.json is written for that run (deployer
# fired). Walk each agent's runs and for any run with deploy.json
# status=success, mark all that run's implemented recs as shipped too
# (since the deploy carried them).
for agent_id in ("aisleprompt-seo-opportunity-agent",
                 "specpicks-seo-opportunity-agent",
                 "aisleprompt-progressive-improvement-agent",
                 "specpicks-progressive-improvement-agent"):
    print(f"=== {agent_id}: reconciling code-rec shipped ===")
    keys = s.list_prefix(f"agents/{agent_id}/runs/")
    deploy_keys = [k for k in keys if k.endswith("/deploy.json")]
    print(f"  {len(deploy_keys)} deploy.json files found")
    flipped = 0
    for dk in deploy_keys:
        d = s.read_json(dk)
        if not isinstance(d, dict): continue
        if d.get("status") != "success": continue
        run_ts = dk.split("/")[3]
        recs_key = f"agents/{agent_id}/runs/{run_ts}/recommendations.json"
        rd = s.read_json(recs_key)
        if not isinstance(rd, dict): continue
        recs = rd.get("recommendations") or []
        changed = False
        for r in recs:
            if r.get("implemented") and not r.get("shipped"):
                r["shipped"] = True
                r["shipped_at"] = d.get("ended_at") or d.get("started_at")
                r["shipped_tag"] = d.get("tag", "")
                r["shipped_image"] = d.get("image", "")
                r["shipped_via"] = "deployer.py"
                changed = True
                flipped += 1
        if changed:
            rd["recommendations"] = recs
            s.write_json(recs_key, rd)
    print(f"  ✓ flipped {flipped} shipped flags from successful deploys")

print("\nbackfill complete")
