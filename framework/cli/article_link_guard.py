#!/usr/bin/env python3
"""Check article bodies against the inline-link contract
(framework.core.article_link_guard): recipe / kitchen / product minima and the
direct-buy-link minimum (bucket, title, products named through their pages),
all from config/article-link-guard-config.json.

The implementer's wrapper INSERT runs the same check, but the article-author
LLM writes its own `INSERT INTO editorial_articles` (written_by='claude-cli';
none of the 54 articles published 2026-10-13..11-12 went through the wrapper
INSERT), so the wrapper never saw those bodies and seven articles shipped with
zero buy links. Two subcommands close that gap:

    # pre-INSERT / pre-UPDATE self-check (ARTICLE_AUTHOR.md, AGENT.md):
    # exit 1 + the exact gap
    python3 framework/cli/article_link_guard.py check \\
        --run-dir <run dir> --rec-id art-001 --body changes/<slug>.body.md

    # rows already in the table. Report only by default (read-only session).
    python3 framework/cli/article_link_guard.py sweep --site <site> --since-hours 6
    # deterministic repairs: a buy link after each product's first link, and
    # bracket-free link text. Dry run unless --commit (run.sh passes --commit
    # after an article-author / article-refresh dispatch).
    python3 framework/cli/article_link_guard.py sweep --site <site> --since-hours 6 \\
        --repair-buy-links --repair-anchor-brackets [--commit] [--report-json <file>]

`check` reads the proposal (bucket, title, expected slugs, agent id) from the
run dir's recommendations.json; `--site/--bucket/--title` override or replace
it. An article-refresh rec carries no proposal: `check` then reads the row's
title, bucket and category by `--slug` (default: the rec's `article_slug`).
With a DSN in `--dsn-env` (default DATABASE_URL, which the implementer sets
for the author session) a buy link counts only when its slug is an active
catalog product (`kitchen_slug_check.product_table`): a /k/<category> link
redirects to the category page and is not a buy link. `check` also fails link
text that holds brackets ("[FLUX.2 [klein] 4B …](url)" rendered as raw
markdown on 2026-11-12); write "FLUX.2 (klein) 4B" instead.

`sweep` takes table/column names and the DSN env from
config/article-heading-repair-config (the per-site mapping the heading-repair
and claims sweeps use). Exit codes: check → 0 pass / 1 fail / 2 bad input;
sweep → 0 unless --fail-on-reject and a row still fails.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from framework.core.article_link_guard import (  # noqa: E402
    find_bracketed_anchors,
    product_slug_checker_from_db,
    render_failure_addendum,
    render_link_directive,
    repair_anchor_brackets,
    repair_buy_links,
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


def _settings(site: str) -> dict:
    """Table / column / DSN-env mapping (config/article-heading-repair-config)."""
    from framework.cli.article_heading_repair import resolve_settings
    return resolve_settings(site)


def _connect(dsn: str, readonly: bool = True):
    """A psycopg2 connection. article_heading_repair._connect finds psycopg2
    even under claude-pool's HOME remap. Read-only (autocommit) unless the
    caller is about to commit repairs; 30 s statement timeout either way."""
    from framework.cli.article_heading_repair import _connect as _pg_connect
    conn = _pg_connect(dsn)
    if readonly:
        conn.set_session(readonly=True, autocommit=True)
    else:
        conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 30000")
    return conn


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
            f"products={a.product_links}/{a.min_products} buy={a.buy_links}/{a.min_buy_links} "
            f"product_pages={a.product_page_links}")


def _columns(cur, table: str) -> set[str]:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {r[0] for r in cur.fetchall()}


def _row_meta(conn, st: dict, slug: str) -> dict:
    """title / bucket / category of the published row `slug` (article-refresh
    recs carry no proposal), {} when there is no such row."""
    t, slug_c = st["table"], st["slug_column"]
    with conn.cursor() as cur:
        cols = [c for c in ("title", "bucket", "category") if c in _columns(cur, t)]
        if not cols:
            return {}
        cur.execute(f"SELECT {', '.join(cols)} FROM {t} WHERE {slug_c} = %s LIMIT 1", (slug,))
        row = cur.fetchone()
    return dict(zip(cols, row)) if row else {}


def _buy_block(proposal: dict, cfg: dict, audit) -> list[str]:
    """The directive's DIRECT BUY LINKS block, filled with the proposal's own
    product slugs, so the fix is a copy-paste, not a guess."""
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
    return block


def cmd_check(args) -> int:
    proposal: dict = {}
    rec: dict = {}
    site = args.site or ""
    if args.run_dir and args.rec_id:
        try:
            rec = _load_rec(args.run_dir, args.rec_id)
        except Exception as e:  # noqa: BLE001
            print(f"[link-guard] cannot read the proposal: {e}", file=sys.stderr)
            return 2
        proposal = dict(rec.get("proposal") or rec.get("article_proposal") or {})
        site = site or rec.get("agent_id") or proposal.get("site") or rec.get("site") or ""
    site = site or os.environ.get("RESPONDER_SITE", "")
    if not site:
        print("[link-guard] no site hint: pass --site or --run-dir/--rec-id", file=sys.stderr)
        return 2
    try:
        body = sys.stdin.read() if args.body == "-" else pathlib.Path(args.body).read_text()
    except Exception as e:  # noqa: BLE001
        print(f"[link-guard] cannot read the body: {e}", file=sys.stderr)
        return 2
    cfg = resolve_minima(site, None if args.no_storage else _storage())
    slug = args.slug or proposal.get("slug") or rec.get("article_slug") or ""

    # Catalog check (and, for a refresh, the row's own bucket/title) from the
    # session's DB. Optional: without it the category slugs the proposal and
    # config name are still dropped.
    slug_ok, row = None, {}
    dsn = "" if args.no_db else os.environ.get(args.dsn_env or "DATABASE_URL", "")
    if dsn:
        try:
            conn = _connect(dsn)
            try:
                slug_ok = product_slug_checker_from_db(conn, body, cfg)
                if slug and not (proposal.get("title") or proposal.get("bucket")):
                    row = _row_meta(conn, _settings(site), slug)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"[link-guard] catalog check skipped ({type(e).__name__}: {str(e)[:160]})",
                  file=sys.stderr)
    elif not args.no_db:
        print(f"[link-guard] no ${args.dsn_env or 'DATABASE_URL'}: buy links are not checked "
              f"against the catalog (a /k/<slug> to an inactive product still counts)",
              file=sys.stderr)

    buckets = list(args.bucket) if args.bucket else [
        str(proposal.get("bucket") or row.get("bucket") or ""),
        str(proposal.get("category") or row.get("category") or "")]
    title = args.title if args.title is not None else str(proposal.get("title") or row.get("title") or "")
    audit = verify_body_with_config(body, proposal, cfg, bucket=buckets, title=title, slug_ok=slug_ok)
    brackets = find_bracketed_anchors(body)
    if audit.passes and not brackets:
        print(f"PASS {_summary(audit)}")
        return 0

    if not audit.passes:
        print(f"FAIL {audit.failure_reason()}")
        print(f"     {_summary(audit)}")
        print("")
        print(render_failure_addendum(audit))
        if audit.buy_links < audit.min_buy_links:
            block = _buy_block(proposal, cfg, audit)
            if block:
                print("")
                print("\n".join(block))
    else:
        print(f"FAIL link text holds brackets ({len(brackets)} link(s))")
        print(f"     {_summary(audit)}")
    if brackets:
        print("")
        print("LINK TEXT WITH BRACKETS — the site renders these as raw markdown. Use "
              "parentheses inside link text (\"FLUX.2 (klein) 4B\", not \"FLUX.2 [klein] 4B\") "
              "and never put a link inside another link's text:")
        for t in brackets[:10]:
            print(f"    [{t}](…)")
    return 1


def _dsn_for(site: str, dsn_env: str = "") -> str:
    st = _settings(site)
    for name in filter(None, [dsn_env, st.get("dsn_env"), "DATABASE_URL"]):
        v = os.environ.get(name) or ""
        if v:
            return v
    return ""


def cmd_sweep(args) -> int:
    st = _settings(args.site)
    dsn = _dsn_for(args.site, args.dsn_env or "")
    if not dsn:
        print(f"[link-guard] no DSN in ${st.get('dsn_env')} / $DATABASE_URL — nothing to do",
              file=sys.stderr)
        return 0
    repairing = bool(args.repair_buy_links or args.repair_anchor_brackets)
    commit = bool(args.commit and repairing)

    t, body_c, slug_c = st["table"], st["body_column"], st["slug_column"]
    status_c, status_v, upd = st["status_column"], st["status_value"], st["updated_column"]
    where, params = [f"{status_c} = %s"], [status_v]
    if args.slug:
        where.append(f"{slug_c} = ANY(%s)"); params.append(list(args.slug))
    if args.since_hours:
        where.append(f"{upd} >= now() - (%s || ' hours')::interval"); params.append(str(args.since_hours))

    cfg = resolve_minima(args.site, None if args.no_storage else _storage())
    report: dict = {"site": args.site, "since_hours": args.since_hours, "committed": commit,
                    "catalog_check": False, "rows": 0, "failed": [], "repaired": []}
    # Read-only session unless repairs are being committed.
    conn = _connect(dsn, readonly=not commit)
    try:
        with conn.cursor() as cur:
            extra = [c for c in ("title", "bucket", "category") if c in _columns(cur, t)]
            cur.execute(
                f"SELECT {slug_c}, {body_c}{''.join(', ' + c for c in extra)} FROM {t} "
                f"WHERE {' AND '.join(where)} ORDER BY {upd} DESC NULLS LAST LIMIT %s",
                params + [args.limit])
            rows = cur.fetchall()
        report["rows"] = len(rows)
        slug_ok = None
        try:
            slug_ok = product_slug_checker_from_db(conn, [r[1] or "" for r in rows], cfg)
        except Exception as e:  # noqa: BLE001
            print(f"[link-guard] catalog check failed ({type(e).__name__}: {str(e)[:160]}); "
                  f"buy links are counted by shape only and not repaired", file=sys.stderr)
        report["catalog_check"] = slug_ok is not None

        for row in rows:
            rec = dict(zip([slug_c, body_c] + extra, row))
            body = rec.get(body_c) or ""
            buckets = [str(rec.get("bucket") or ""), str(rec.get("category") or "")]
            title = str(rec.get("title") or "")
            new_body, notes = body, []
            if args.repair_anchor_brackets:
                new_body, n1 = repair_anchor_brackets(new_body)
                notes += n1
            audit = verify_body_with_config(new_body, {}, cfg, bucket=buckets, title=title,
                                            slug_ok=slug_ok)
            if args.repair_buy_links and audit.buy_links < audit.min_buy_links and slug_ok is not None:
                # Only with the catalog check: a buy link for a product that
                # is not active would lead nowhere.
                new_body, n2 = repair_buy_links(new_body, cfg, product_ok=slug_ok)
                if n2:
                    notes += n2
                    audit = verify_body_with_config(new_body, {}, cfg, bucket=buckets, title=title,
                                                    slug_ok=slug_ok)
            if new_body != body:
                item = {"slug": rec[slug_c], "changes": notes, "passes_after": audit.passes}
                if commit:
                    # Only if the body is still the one we read: a concurrent
                    # edit is never overwritten (the next sweep sees it).
                    set_clause = f"{body_c} = %s" + (f", {upd} = NOW()" if upd else "")
                    with conn.cursor() as cur:
                        cur.execute(f"UPDATE {t} SET {set_clause} WHERE {slug_c} = %s AND {body_c} = %s",
                                    (new_body, rec[slug_c], body))
                        if getattr(cur, "rowcount", 1) == 0:
                            item["skipped"] = "body changed since it was read"
                report["repaired"].append(item)
            if audit.passes:
                continue
            item = {"slug": rec[slug_c], "buckets": [b for b in buckets if b], "title": title,
                    "reason": audit.failure_reason(), "recipe_links": audit.recipe_links,
                    "kitchen_links": audit.kitchen_links, "product_links": audit.product_links,
                    "buy_links": audit.buy_links, "min_buy_links": audit.min_buy_links,
                    "product_page_links": audit.product_page_links,
                    "addendum": render_failure_addendum(audit)}
            report["failed"].append(item)
            if args.json:
                print(json.dumps({k: v for k, v in item.items() if k != "addendum"}, ensure_ascii=False))
            else:
                print(f"{rec[slug_c]}: {audit.failure_reason()} ({_summary(audit)})")
        if commit:
            conn.commit()
    finally:
        conn.close()

    verb = "repaired" if commit else "would repair"
    for r in report["repaired"]:
        if not args.json:
            if r.get("skipped"):
                print(f"{r['slug']}: not repaired — {r['skipped']}")
                continue
            print(f"{r['slug']}: {verb} — " + "; ".join(r["changes"][:6])
                  + ("" if r["passes_after"] else " (still short of the contract)"))
    if repairing and not commit and report["repaired"]:
        print("[link-guard] dry run — re-run with --commit to apply", file=sys.stderr)
    if args.report_json:
        try:
            p = pathlib.Path(args.report_json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        except Exception as e:  # noqa: BLE001
            print(f"[link-guard] report not written: {e}", file=sys.stderr)
    print(f"[link-guard] {args.site}: {len(report['failed'])}/{len(rows)} row(s) fail the link "
          f"contract" + (f"; {verb} {len(report['repaired'])}" if repairing else ""),
          file=sys.stderr)
    return 1 if (report["failed"] and args.fail_on_reject) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check one body before it is INSERTed or UPDATEd")
    c.add_argument("--body", required=True, help="the article body markdown file ('-' = stdin)")
    c.add_argument("--run-dir", help="dispatch run dir holding recommendations.json")
    c.add_argument("--rec-id", help="the rec whose proposal the body answers")
    c.add_argument("--site", help="site hint (agent id or site name); default: the rec's agent_id / site")
    c.add_argument("--slug", help="the article's slug (refresh: read its bucket/title from the row); "
                                  "default: the proposal's slug or the rec's article_slug")
    c.add_argument("--bucket", action="append",
                   help="override the bucket (repeatable: a row's bucket and category)")
    c.add_argument("--title", help="override the proposal's title")
    c.add_argument("--dsn-env", default="DATABASE_URL",
                   help="env var with the site DSN for the catalog check (default DATABASE_URL)")
    c.add_argument("--no-db", action="store_true", help="skip the catalog check")
    c.add_argument("--no-storage", action="store_true", help="repo default config only")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("sweep", help="report (and optionally repair) published rows that fail the contract")
    s.add_argument("--site", required=True, help="site hint (matches config keys by substring)")
    s.add_argument("--since-hours", type=float, default=0, help="only rows updated in the last N hours (0 = all)")
    s.add_argument("--slug", action="append", default=[], help="limit to these slugs (repeatable)")
    s.add_argument("--json", action="store_true", help="one JSON object per failing row")
    s.add_argument("--fail-on-reject", action="store_true")
    s.add_argument("--limit", type=int, default=2000)
    s.add_argument("--dsn-env", default="", help="env var holding the DSN (default: from config)")
    s.add_argument("--repair-buy-links", action="store_true",
                   help="add a buy link after the first link to each active product that has none, "
                        "on rows short of their buy-link minimum")
    s.add_argument("--repair-anchor-brackets", action="store_true",
                   help="turn brackets inside link text into parentheses")
    s.add_argument("--commit", action="store_true", help="write the repairs (default: dry run)")
    s.add_argument("--report-json", help="write the full report (failures with their addendum, repairs) here")
    s.add_argument("--no-storage", action="store_true", help="repo default config only")
    s.set_defaults(func=cmd_sweep)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
