#!/usr/bin/env python3
"""Check article bodies against the inline-link contract
(framework.core.article_link_guard): recipe / kitchen / product minima and the
bucket's direct-buy-link minimum, all from config/article-link-guard-config.json.

The implementer's wrapper INSERT runs the same check, but the article-author
LLM writes its own `INSERT INTO editorial_articles` (written_by='claude-cli';
none of the 54 articles published 2026-10-13..11-12 went through the wrapper
INSERT), so the wrapper never saw those bodies and seven guides shipped with
zero buy links. Two subcommands close that gap:

    # pre-INSERT self-check (ARTICLE_AUTHOR.md): exit 1 + the exact gap
    python3 framework/cli/article_link_guard.py check \\
        --run-dir <run dir> --rec-id art-001 --body changes/<slug>.body.md

    # report rows already in the table (run.sh hook after an article-author
    # dispatch, or a backlog audit). Read-only; never edits a body.
    python3 framework/cli/article_link_guard.py sweep --site <site> --since-hours 6

`check` reads the proposal (bucket, title, expected slugs, agent id) from the
run dir's recommendations.json; `--site/--bucket/--title` override or replace
it. `sweep` takes table/column names from config/article-heading-repair-config
(the same per-site mapping the heading-repair and claims sweeps use).
Exit codes: check → 0 pass / 1 fail / 2 bad input; sweep → 0 unless
--fail-on-reject and a row fails.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from framework.core.article_link_guard import (  # noqa: E402
    min_buy_links_for,
    render_failure_addendum,
    render_link_directive,
    resolve_minima,
    verify_body_with_config,
)


def _storage():
    """The storage backend (its copy of the guard config is laid over the
    repo default), or None — the repo default then applies."""
    try:
        from framework.core.storage import get_storage
        return get_storage()
    except (Exception, SystemExit):  # unknown STORAGE_BACKEND raises SystemExit
        return None


def _load_rec(run_dir: str, rec_id: str) -> dict:
    p = pathlib.Path(run_dir) / "recommendations.json"
    raw = json.loads(p.read_text())
    recs = raw.get("recommendations", []) if isinstance(raw, dict) else raw
    for r in recs or []:
        if isinstance(r, dict) and r.get("id") == rec_id:
            return r
    raise KeyError(f"rec {rec_id!r} not in {p}")


def _summary(a) -> str:
    return (f"recipes={a.recipe_links}/{a.min_recipes} kitchen={a.kitchen_links}/{a.min_kits} "
            f"products={a.product_links}/{a.min_products} buy={a.buy_links}/{a.min_buy_links}")


def cmd_check(args) -> int:
    proposal: dict = {}
    site = args.site or ""
    if args.run_dir and args.rec_id:
        try:
            rec = _load_rec(args.run_dir, args.rec_id)
        except Exception as e:  # noqa: BLE001
            print(f"[link-guard] cannot read the proposal: {e}", file=sys.stderr)
            return 2
        proposal = dict(rec.get("proposal") or rec.get("article_proposal") or {})
        site = site or rec.get("agent_id") or proposal.get("site") or ""
    site = site or os.environ.get("RESPONDER_SITE", "")
    if not site:
        print("[link-guard] no site hint: pass --site or --run-dir/--rec-id", file=sys.stderr)
        return 2
    if args.bucket is not None:
        proposal["bucket"], proposal["category"] = args.bucket, ""
    if args.title is not None:
        proposal["title"] = args.title
    try:
        body = sys.stdin.read() if args.body == "-" else pathlib.Path(args.body).read_text()
    except Exception as e:  # noqa: BLE001
        print(f"[link-guard] cannot read the body: {e}", file=sys.stderr)
        return 2
    cfg = resolve_minima(site, None if args.no_storage else _storage())
    audit = verify_body_with_config(body, proposal, cfg)
    if audit.passes:
        print(f"PASS {_summary(audit)}")
        return 0
    print(f"FAIL {audit.failure_reason()}")
    print(f"     {_summary(audit)}")
    print("")
    print(render_failure_addendum(audit))
    if audit.buy_links < audit.min_buy_links:
        # Re-state the buy-link block with the proposal's own slugs, so the
        # fix is a copy-paste, not a guess.
        directive = render_link_directive(
            proposal, min_recipes=audit.min_recipes, min_kits=audit.min_kits,
            min_products=audit.min_products, site_root=cfg.get("site_root") or "",
            link_cfg=cfg, min_buy_links=audit.min_buy_links)
        block, keep = [], False
        for line in directive.splitlines():
            if line.lstrip().startswith("DIRECT BUY LINKS"):
                keep = True
            elif keep and not line.strip():
                break
            if keep:
                block.append(line)
        if block:
            print("")
            print("\n".join(block))
    return 1


def cmd_sweep(args) -> int:
    from framework.cli.article_heading_repair import resolve_settings

    st = resolve_settings(args.site)
    dsn = os.environ.get(st.get("dsn_env") or "DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not dsn:
        print(f"[link-guard] no DSN in ${st.get('dsn_env')} / $DATABASE_URL — nothing to do",
              file=sys.stderr)
        return 0
    import psycopg2  # local import: the primitive itself has no DB dependency

    t, body_c, slug_c = st["table"], st["body_column"], st["slug_column"]
    status_c, status_v, upd = st["status_column"], st["status_value"], st["updated_column"]
    where, params = [f"{status_c} = %s"], [status_v]
    if args.slug:
        where.append(f"{slug_c} = %s"); params.append(args.slug)
    if args.since_hours:
        where.append(f"{upd} >= now() - (%s || ' hours')::interval"); params.append(str(args.since_hours))
    # Read-only session: this sweep reports, it never edits a row.
    conn = psycopg2.connect(dsn, options="-c default_transaction_read_only=on -c statement_timeout=30000")
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (t,))
            cols = {r[0] for r in cur.fetchall()}
            extra = [c for c in ("title", "bucket", "category") if c in cols]
            cur.execute(
                f"SELECT {slug_c}, {body_c}{''.join(', ' + c for c in extra)} FROM {t} "
                f"WHERE {' AND '.join(where)} ORDER BY {upd} DESC NULLS LAST LIMIT %s",
                params + [args.limit])
            rows = cur.fetchall()
    finally:
        conn.close()

    cfg = resolve_minima(args.site, None if args.no_storage else _storage())
    failed = 0
    for row in rows:
        rec = dict(zip([slug_c, body_c] + extra, row))
        buckets = [str(rec.get("bucket") or ""), str(rec.get("category") or "")]
        title = str(rec.get("title") or "")
        audit = verify_body_with_config(rec.get(body_c) or "", {}, cfg, bucket=buckets, title=title)
        if audit.passes:
            continue
        failed += 1
        if args.json:
            print(json.dumps({"slug": rec[slug_c], "buckets": [b for b in buckets if b],
                              "title": title, "reason": audit.failure_reason(),
                              "recipe_links": audit.recipe_links, "kitchen_links": audit.kitchen_links,
                              "product_links": audit.product_links, "buy_links": audit.buy_links,
                              "min_buy_links": audit.min_buy_links}, ensure_ascii=False))
        else:
            print(f"{rec[slug_c]}: {audit.failure_reason()} ({_summary(audit)})")
    print(f"[link-guard] {args.site}: {failed}/{len(rows)} row(s) fail the link contract",
          file=sys.stderr)
    return 1 if (failed and args.fail_on_reject) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check one body before it is INSERTed")
    c.add_argument("--body", required=True, help="the article body markdown file ('-' = stdin)")
    c.add_argument("--run-dir", help="dispatch run dir holding recommendations.json")
    c.add_argument("--rec-id", help="the rec whose proposal the body answers")
    c.add_argument("--site", help="site hint (agent id or site name); default: the rec's agent_id")
    c.add_argument("--bucket", help="override the proposal's bucket")
    c.add_argument("--title", help="override the proposal's title")
    c.add_argument("--no-storage", action="store_true", help="repo default config only")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("sweep", help="report published rows that fail the contract (read-only)")
    s.add_argument("--site", required=True, help="site hint (matches config keys by substring)")
    s.add_argument("--since-hours", type=float, default=0, help="only rows updated in the last N hours (0 = all)")
    s.add_argument("--slug", help="one article")
    s.add_argument("--json", action="store_true", help="one JSON object per failing row")
    s.add_argument("--fail-on-reject", action="store_true")
    s.add_argument("--limit", type=int, default=2000)
    s.add_argument("--no-storage", action="store_true", help="repo default config only")
    s.set_defaults(func=cmd_sweep)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
