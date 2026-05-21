"""Article-author resume-from-where-it-left-off primitive.

When the implementer is dispatched with N rec_ids and crashes mid-batch
(scope killed, timeout, rate-limit defer), the typical recovery path
re-dispatches the SAME rec_id list. Without this primitive the new
scope would re-run already-completed articles, burning LLM tokens
producing duplicate UPSERTs.

This primitive filters the rec_id list to drop ones whose target slug
is already "done" in the DB — meaning the article row has body_md
substantially longer than the rewrite stub. The remaining rec_ids are
what the new scope actually needs to process.

Detection of "done" is by SLUG, not rec_id:
  - article-author UPSERTs by slug, so two re-author batches for the
    same slug should not both write
  - body_md > 5000 chars AND body_md != stub → considered done

Usage from run.sh (article-author dispatch_kind):

    REMAINING_REC_IDS=$(
        PYTHONPATH="$REPO_ROOT" \
        RESPONDER_RUN_DIR="$RESPONDER_RUN_DIR" \
        RESPONDER_REC_IDS="$RESPONDER_REC_IDS" \
        DATABASE_URL="$DATABASE_URL" \
        python3 -m framework.core.article_progress filter
    )
    if [ -n "$REMAINING_REC_IDS" ] && [ "$REMAINING_REC_IDS" != "$RESPONDER_REC_IDS" ]; then
        echo "[implementer] article-progress filter: $RESPONDER_REC_IDS → $REMAINING_REC_IDS"
        RESPONDER_REC_IDS="$REMAINING_REC_IDS"
    fi

If all rec_ids are already done, REMAINING_REC_IDS is empty and the
scope exits cleanly (nothing to do).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Length threshold above which we consider a body "real" content
# (vs the rewrite stub or a placeholder). 5000 chars is conservative —
# our rewrite stub is 56 chars, real articles run 10K-25K.
DONE_MIN_BODY_CHARS = 5000

# Exact stub marker emitted by audit_unpublish; never counts as "done"
# even if it somehow exceeds the threshold.
STUB_MARKERS = (
    "[NEEDS REWRITE - flagged by audit",
)


def is_article_done(body_md: str) -> bool:
    """Return True if body_md looks like a completed article (not a stub
    and not empty)."""
    if not body_md:
        return False
    for marker in STUB_MARKERS:
        if marker in body_md:
            return False
    return len(body_md) >= DONE_MIN_BODY_CHARS


def filter_done_rec_ids(
    rec_ids: list[str],
    recs_doc: dict,
    db_dsn: str | None = None,
) -> list[str]:
    """Filter rec_ids to drop ones whose target slug is already done in
    the DB.

    `recs_doc` is the parsed recommendations.json. Each rec is expected
    to have either:
      - rec['article_proposal']['slug'], or
      - rec['affected_url'] (path-stripped to slug)

    Returns the filtered list, preserving order. Recs without a
    resolvable slug are kept (skip is conservative).
    """
    if not rec_ids or not isinstance(recs_doc, dict):
        return rec_ids

    # Map id → slug from the recommendations doc
    id_to_slug: dict[str, str] = {}
    for r in recs_doc.get("recommendations") or []:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("rec_id")
        if not rid:
            continue
        slug = ""
        ap = r.get("article_proposal") or {}
        if isinstance(ap, dict):
            slug = ap.get("slug") or ""
        if not slug:
            url = r.get("affected_url") or ""
            if url:
                # Extract trailing path component; works for
                # https://site.com/articles/<slug> or .../reviews/<slug>
                slug = url.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            id_to_slug[str(rid)] = slug

    if not id_to_slug:
        return rec_ids

    # Connect to DB; if unavailable, return all (conservative — don't
    # accidentally skip work).
    db_dsn = db_dsn or os.environ.get("DATABASE_URL", "")
    if not db_dsn:
        return rec_ids
    try:
        import psycopg2  # type: ignore
        conn = psycopg2.connect(db_dsn)
    except Exception:
        return rec_ids

    done_slugs: set[str] = set()
    try:
        slugs_to_check = list({id_to_slug[r] for r in rec_ids if r in id_to_slug})
        if not slugs_to_check:
            return rec_ids
        with conn.cursor() as cur:
            cur.execute(
                "SELECT slug, body_md FROM editorial_articles WHERE slug = ANY(%s)",
                (slugs_to_check,),
            )
            for slug, body in cur.fetchall():
                if is_article_done(body):
                    done_slugs.add(slug)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not done_slugs:
        return rec_ids

    filtered = []
    for r in rec_ids:
        slug = id_to_slug.get(r)
        if slug and slug in done_slugs:
            continue
        filtered.append(r)
    return filtered


def _cli_filter() -> int:
    """CLI entry — read env, print the filtered rec-id list as comma-
    delimited stdout. Used by run.sh."""
    run_dir = os.environ.get("RESPONDER_RUN_DIR") or ""
    rec_ids_str = os.environ.get("RESPONDER_REC_IDS") or ""
    db_dsn = os.environ.get("DATABASE_URL") or ""
    if not run_dir or not rec_ids_str:
        # Nothing to filter; echo input.
        sys.stdout.write(rec_ids_str)
        return 0
    rec_ids = [r.strip() for r in rec_ids_str.split(",") if r.strip()]
    if not rec_ids:
        return 0
    recs_path = Path(run_dir) / "recommendations.json"
    if not recs_path.exists():
        sys.stdout.write(",".join(rec_ids))
        return 0
    try:
        doc = json.loads(recs_path.read_text())
    except Exception:
        sys.stdout.write(",".join(rec_ids))
        return 0
    filtered = filter_done_rec_ids(rec_ids, doc, db_dsn=db_dsn)
    sys.stdout.write(",".join(filtered))
    # Diagnostic to stderr — visible in dispatch logs
    if len(filtered) < len(rec_ids):
        skipped = [r for r in rec_ids if r not in set(filtered)]
        sys.stderr.write(
            f"[article-progress] skipped {len(skipped)} already-done rec(s): "
            f"{','.join(skipped)}\n"
        )
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "filter":
        print("usage: article_progress.py filter", file=sys.stderr)
        return 2
    return _cli_filter()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["filter_done_rec_ids", "is_article_done", "DONE_MIN_BODY_CHARS"]
