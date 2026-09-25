#!/usr/bin/env python3
"""Report articles whose body carries invented testing, copied star ratings or
point prices (framework.core.article_claims_guard).

The implementer's wrapper INSERT runs the guard before writing, but the
article-author LLM often writes its own `INSERT INTO editorial_articles`
(written_by='claude-cli'), so those rows never pass through it. This sweep
reads what is already in the table and reports every row the site's policy
would reject. It never edits prose — the fix is an Opus rewrite or a reviewed
data migration — so it is safe to run on every article-author dispatch.

    # the run.sh hook: rows written in the last 6 hours
    python3 -m framework.cli.article_claims_guard --site <site> --since-hours 6

    # backlog audit, JSON for a rec batch
    python3 -m framework.cli.article_claims_guard --site <site> --json

Exit code is 0 unless --fail-on-reject is given and a row is rejected.
Table/column names follow config/article-heading-repair-config.json (same
per-site mapping the heading-repair sweep uses).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from framework.core.article_claims_guard import check, resolve_policy  # noqa: E402
from framework.cli.article_heading_repair import resolve_settings  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--site", required=True, help="site hint (matches config keys by substring)")
    ap.add_argument("--since-hours", type=float, default=0, help="only rows updated in the last N hours (0 = all)")
    ap.add_argument("--slug", help="one article")
    ap.add_argument("--json", action="store_true", help="print one JSON object per rejected row")
    ap.add_argument("--fail-on-reject", action="store_true")
    ap.add_argument("--limit", type=int, default=2000)
    args = ap.parse_args(argv)

    st = resolve_settings(args.site)
    dsn = os.environ.get(st.get("dsn_env") or "DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not dsn:
        print(f"[claims-guard] no DSN in ${st.get('dsn_env')} / $DATABASE_URL — nothing to do", file=sys.stderr)
        return 0
    import psycopg2  # local import: the primitive itself has no DB dependency

    t, body, slug_c = st["table"], st["body_column"], st["slug_column"]
    status_c, status_v, upd = st["status_column"], st["status_value"], st["updated_column"]
    where, params = [f"{status_c} = %s"], [status_v]
    if args.slug:
        where.append(f"{slug_c} = %s"); params.append(args.slug)
    if args.since_hours:
        where.append(f"{upd} >= now() - (%s || ' hours')::interval"); params.append(str(args.since_hours))
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '30s'")
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (t,))
            cols = {r[0] for r in cur.fetchall()}
            extra = [c for c in ("subtitle", "excerpt", "bucket", "category") if c in cols]
            cur.execute(
                f"SELECT {slug_c}, {body}{''.join(', ' + c for c in extra)} FROM {t} "
                f"WHERE {' AND '.join(where)} ORDER BY {upd} DESC NULLS LAST LIMIT %s",
                params + [args.limit])
            rows = cur.fetchall()
    finally:
        conn.close()

    rejected = 0
    for row in rows:
        rec = dict(zip([slug_c, body] + extra, row))
        bucket = rec.get("bucket") or rec.get("category") or ""
        audit = check(rec.get(body) or "", subtitle=rec.get("subtitle") or "",
                      excerpt=rec.get("excerpt") or "", bucket=bucket,
                      policy=resolve_policy(args.site, bucket))
        if audit.passes:
            continue
        rejected += 1
        if args.json:
            print(json.dumps({"slug": rec[slug_c], "bucket": bucket,
                              "first_hand": audit.first_hand + audit.meta_first_hand,
                              "star_ratings": audit.star_ratings,
                              "point_prices": audit.point_prices}, ensure_ascii=False))
        else:
            print(f"{rec[slug_c]}: {audit.failure_reason(max_examples=2)[:400]}")
    print(f"[claims-guard] {args.site}: {rejected}/{len(rows)} row(s) fail the policy", file=sys.stderr)
    return 1 if (rejected and args.fail_on_reject) else 0


if __name__ == "__main__":
    sys.exit(main())
