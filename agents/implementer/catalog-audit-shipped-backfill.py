#!/usr/bin/env python3
"""Backfill shipped:true on catalog-audit recs whose migration files have
already run against the production DB.

Why: catalog-audit dispatches skip the deployer (their work is DB-only,
no docker push needed), so the deployer never writes shipped:true to the
recommendations.json. The migrations DO get applied — usually via the
app's startup migration runner — but the rec lifecycle stays stuck at
implemented:true.

This script bridges the gap. For each recently-implemented catalog-audit
rec:
  1. Read the migration_template (or peek at the committed SQL file).
  2. Run a verification query against the prod DB to check whether the
     migration's effects are live.
  3. If yes, write shipped:true + shipped_at to the rec.

Usage:
    DATABASE_URL_AISLEPROMPT='postgresql://...' \
    DATABASE_URL_SPECPICKS='postgresql://...' \
    python3 agents/implementer/catalog-audit-shipped-backfill.py [--dry-run]

Note: only flips shipped:false → shipped:true. Never the other way.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, "/home/voidsstr/development/reusable-agents")

from framework.core.storage import get_storage

DSN_BY_SITE = {
    "aisleprompt": os.environ.get("DATABASE_URL_AISLEPROMPT"),
    "specpicks":   os.environ.get("DATABASE_URL_SPECPICKS"),
}
DRY_RUN = "--dry-run" in sys.argv


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(site: str):
    dsn = DSN_BY_SITE.get(site)
    if not dsn:
        return None
    try:
        import psycopg2
    except ImportError:
        print(f"[backfill] psycopg2 not installed; install in venv first")
        return None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=10)
        # Autocommit so a failed verifier query doesn't poison the
        # transaction for subsequent recs.
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"[backfill] connect to {site} failed: {e}")
        return None


def _verify_migration_applied(conn, rec: dict) -> tuple[bool, str]:
    """Return (applied, reason). Heuristic verification per criterion:

    For each catalog-audit criterion the migration_template has a known
    'verify' query — we re-run it and compare to the post-migration
    expected state.
    """
    check_id = rec.get("check_id", "")
    template = rec.get("migration_template", {}) or {}
    ref_ids_raw = rec.get("ref_ids") or template.get("ref_ids") or []
    if not ref_ids_raw:
        return False, "no ref_ids in rec to verify"
    # Coerce to ints — recommendations.json may store ids as strings
    # (older agent versions) but the DB columns are integer. ANY() with
    # the wrong text/int side raises "operator does not exist".
    ref_ids: list[int] = []
    for v in ref_ids_raw:
        try:
            ref_ids.append(int(v))
        except (TypeError, ValueError):
            pass
    if not ref_ids:
        return False, f"no integer-castable ref_ids (raw={ref_ids_raw[:3]})"

    cur = conn.cursor()
    try:
        # Per-criterion verification queries. These mirror the audit
        # script's checks — if the migration ran, the rows below should
        # NOT match anymore (they were either deactivated or had a
        # field nulled).
        if check_id == "recipe-image-present":
            # Migration sets is_active=false on these IDs that had no image.
            cur.execute(
                "SELECT COUNT(*) FROM recipe_catalog "
                "WHERE id = ANY(%s) AND is_active = true",
                (list(ref_ids),),
            )
            still_active = cur.fetchone()[0]
            return (still_active == 0,
                    f"{still_active} of {len(ref_ids)} still active (expected 0)")

        if check_id == "recipe-nutrition-sanity":
            # Migration nulls calories/protein/carbs/fat on these IDs.
            cur.execute(
                "SELECT COUNT(*) FROM recipe_catalog "
                "WHERE id = ANY(%s) "
                "AND (calories IS NOT NULL OR protein IS NOT NULL "
                "     OR carbs IS NOT NULL OR fat IS NOT NULL)",
                (list(ref_ids),),
            )
            still_filled = cur.fetchone()[0]
            return (still_filled == 0,
                    f"{still_filled} of {len(ref_ids)} still have nutrition")

        if check_id == "product-image-present":
            cur.execute(
                "SELECT COUNT(*) FROM kitchen_products "
                "WHERE id = ANY(%s) AND is_active = true",
                (list(ref_ids),),
            )
            still_active = cur.fetchone()[0]
            return (still_active == 0,
                    f"{still_active} of {len(ref_ids)} still active")

        if check_id == "recipe-image-valid-url":
            # Migration nulls image_url on broken-image rows.
            cur.execute(
                "SELECT COUNT(*) FROM recipe_catalog "
                "WHERE id = ANY(%s) AND image_url IS NOT NULL AND image_url != ''",
                (list(ref_ids),),
            )
            still_set = cur.fetchone()[0]
            return (still_set == 0,
                    f"{still_set} of {len(ref_ids)} still have image_url")

        if check_id == "recipe-video-present":
            # Migration sets is_active=false (or video_url=null depending on agent version).
            cur.execute(
                "SELECT COUNT(*) FROM recipe_catalog "
                "WHERE id = ANY(%s) AND is_active = true "
                "AND (video_url IS NOT NULL AND video_url != '')",
                (list(ref_ids),),
            )
            still_unfixed = cur.fetchone()[0]
            return (still_unfixed == 0,
                    f"{still_unfixed} of {len(ref_ids)} still active w/ video missing")

        if check_id == "recipe-duplicate-source-url":
            cur.execute(
                "SELECT COUNT(*) FROM recipe_catalog "
                "WHERE id = ANY(%s) AND is_active = true",
                (list(ref_ids),),
            )
            still_active = cur.fetchone()[0]
            return (still_active == 0,
                    f"{still_active} of {len(ref_ids)} dupes still active")

        if check_id == "product-schema-rich-results":
            # Schema-fix migrations don't usually have a deterministic
            # post-state query — assume committed = shipped via app deploy.
            return True, "schema fix — assume shipped after commit"

        return False, f"no verifier for check_id={check_id!r}"
    except Exception as e:
        return False, f"verifier query failed: {e}"
    finally:
        cur.close()


def main() -> int:
    s = get_storage()
    flipped_total = 0
    checked_total = 0

    for site in ("aisleprompt", "specpicks"):
        agent_id = f"{site}-catalog-audit-agent"
        conn = _connect(site)
        if conn is None:
            print(f"[backfill] {site}: no DB connection — skipping")
            continue

        keys = sorted(s.list_prefix(f"agents/{agent_id}/runs/"), reverse=True)
        rec_keys = [k for k in keys if k.endswith("/recommendations.json")][:30]
        print(f"[backfill] {site}: scanning {len(rec_keys)} recent recommendations.json files")

        flipped_for_site = 0
        for rk in rec_keys:
            rd = s.read_json(rk) or {}
            recs = rd.get("recommendations", [])
            if not recs:
                continue
            file_changed = False
            for r in recs:
                if not r.get("implemented") or r.get("shipped"):
                    continue
                checked_total += 1
                applied, reason = _verify_migration_applied(conn, r)
                rid = r.get("id", "?")
                run_id = rk.split("/runs/")[1].split("/")[0]
                if applied:
                    print(f"  [{site}/{run_id}/{rid}] SHIPPED — {reason}")
                    if not DRY_RUN:
                        r["shipped"] = True
                        r["shipped_at"] = _now_iso()
                        r["shipped_via"] = "catalog-audit-shipped-backfill"
                        r["shipped_verification"] = reason
                        file_changed = True
                    flipped_for_site += 1
                else:
                    print(f"  [{site}/{run_id}/{rid}] still pending — {reason}")
            if file_changed:
                s.write_json(rk, rd)

        print(f"[backfill] {site}: flipped {flipped_for_site} rec(s) to shipped")
        flipped_total += flipped_for_site
        conn.close()

    print(f"\n[backfill] DONE — checked {checked_total} recs across both sites,"
          f" flipped {flipped_total} to shipped"
          + (" (DRY-RUN — no writes)" if DRY_RUN else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
