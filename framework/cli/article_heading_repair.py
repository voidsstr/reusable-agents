#!/usr/bin/env python3
"""Repair outline-label headings in already-written article bodies.

Why this exists
---------------
`framework.core.article_metadata_guard.has_leaky_heading` gates the
implementer's *wrapper-driven* article INSERT — the path that stamps
`written_by='framework-<backend>'`. But per `agents/implementer/
ARTICLE_AUTHOR.md` the article-author LLM writes most rows itself
(`written_by='claude-cli'`, its own `INSERT INTO editorial_articles`),
and those bodies never pass through the wrapper. Result: the guard
shipped 2026-07-03 and 84 further articles still reached production
with `## Editorial intro: …` / `## 5-column spec-delta table` headings
— prompt scaffolding rendered as a section title, which reads as
machine-generated boilerplate to readers and to Google's
scaled-content classifier.

This CLI closes that gap from the other side: it sweeps rows that are
already in the table, repairs the heading text in place via
`repair_leaky_headings`, and leaves everything else in the body
untouched. It is safe to run repeatedly — a clean body is a no-op.

Two callers:
  * `agents/implementer/run.sh`, right after the article-author
    dispatch's INSERT step (alongside `resolve-article-links.py`), so
    freshly-written articles are cleaned before anyone reads them;
  * an operator, to sweep the backlog of already-published rows.

Usage
-----
    # dry run — show every heading that would change, touch nothing
    python3 -m framework.cli.article_heading_repair --site specpicks

    # repair one article
    python3 -m framework.cli.article_heading_repair --site specpicks \\
        --slug noctua-nh-u12s-vs-deepcool-ak620-2026 --commit

    # repair everything published in the last day (the run.sh hook)
    python3 -m framework.cli.article_heading_repair --site specpicks \\
        --since-hours 24 --commit

The table/column names come from `config/article-heading-repair-config.json`
(storage copy overrides the repo default) keyed by `--site`; individual
flags override the config. The leak patterns themselves are logic and
live in the primitive, not here.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from framework.core.article_metadata_guard import (  # noqa: E402
    has_leaky_heading,
    repair_leaky_headings,
)

_CONFIG_KEY = "config/article-heading-repair-config.json"
_REPO_CONFIG = pathlib.Path(__file__).resolve().parents[2] / _CONFIG_KEY

_FALLBACK = {
    "dsn_env": "DATABASE_URL",
    "table": "editorial_articles",
    "body_column": "body_md",
    "slug_column": "slug",
    "status_column": "status",
    "status_value": "published",
    "updated_column": "updated_at",
}


def resolve_settings(site_hint: str, storage=None) -> dict:
    """Return the table/column mapping for `site_hint`.

    Matching is by substring against the config's `sites` keys, so a
    per-site agent id ("specpicks-article-proposal-agent") resolves the
    same as the bare site name.
    """
    cfg = None
    if storage is not None:
        try:
            cfg = storage.read_json(_CONFIG_KEY) or None
        except Exception:
            cfg = None
    if cfg is None:
        try:
            cfg = json.loads(_REPO_CONFIG.read_text())
        except Exception as e:  # config missing → still usable defaults
            sys.stderr.write(
                f"[heading-repair] config unreadable ({e}); using built-in defaults\n")
            cfg = {}
    vals = dict(_FALLBACK)
    vals.update(cfg.get("default") or {})
    hint = (site_hint or "").lower()
    for key, over in (cfg.get("sites") or {}).items():
        if key.lower() in hint:
            vals.update(over or {})
            break
    return vals


def _connect(dsn: str):
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        # claude-pool remaps HOME, so the user site-packages dir falls off
        # sys.path even though psycopg2 is installed there. Probe both the
        # env HOME and the passwd home (the latter survives the remap).
        import glob
        import pwd
        homes = {os.path.expanduser("~"), pwd.getpwuid(os.getuid()).pw_dir}
        for home in filter(None, homes):
            for cand in glob.glob(f"{home}/.local/lib/python3.*/site-packages"):
                if cand not in sys.path:
                    sys.path.append(cand)
    import psycopg2
    return psycopg2.connect(dsn)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Repair outline-label headings in article bodies")
    ap.add_argument("--site", default="",
                    help="site hint (site name or per-site agent id) used to "
                         "resolve the table/column mapping from config")
    ap.add_argument("--dsn", default="", help="explicit DSN (overrides --dsn-env)")
    ap.add_argument("--dsn-env", default="",
                    help="env var holding the DSN (default: from config)")
    ap.add_argument("--table", default="")
    ap.add_argument("--body-column", default="")
    ap.add_argument("--slug-column", default="")
    ap.add_argument("--status", default="",
                    help="status value to filter on; '' = every row")
    ap.add_argument("--slug", action="append", default=[],
                    help="limit to these slugs (repeatable)")
    ap.add_argument("--since-hours", type=float, default=0.0,
                    help="only rows whose updated/published timestamp is "
                         "within this many hours (0 = no time filter)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit", action="store_true",
                    help="actually UPDATE (default: dry run)")
    ap.add_argument("--json", action="store_true",
                    help="emit a machine-readable report on stdout")
    args = ap.parse_args(argv)

    cfg = resolve_settings(args.site)
    table = args.table or cfg["table"]
    body_col = args.body_column or cfg["body_column"]
    slug_col = args.slug_column or cfg["slug_column"]
    status_col = cfg.get("status_column") or "status"
    status_val = args.status or cfg.get("status_value") or ""
    updated_col = cfg.get("updated_column") or ""

    dsn = args.dsn
    if not dsn:
        for env_name in filter(None, [args.dsn_env, cfg.get("dsn_env"), "DATABASE_URL"]):
            dsn = os.environ.get(env_name) or ""
            if dsn:
                break
    if not dsn:
        sys.stderr.write(
            "[heading-repair] no DSN — set --dsn or the configured DSN env var\n")
        return 2

    where, params = [], []
    if status_val:
        where.append(f"{status_col} = %s")
        params.append(status_val)
    if args.slug:
        where.append(f"{slug_col} = ANY(%s)")
        params.append(list(args.slug))
    if args.since_hours > 0:
        ts_col = updated_col or "published_at"
        where.append(
            f"COALESCE({ts_col}, published_at) > NOW() - make_interval(hours => %s)")
        params.append(args.since_hours)
    sql = (f"SELECT {slug_col}, {body_col} FROM {table}"
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY {slug_col}"
           + (f" LIMIT {int(args.limit)}" if args.limit else ""))

    conn = _connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()

    report = {"scanned": len(rows), "repaired": [], "committed": bool(args.commit)}
    for slug, body in rows:
        if not has_leaky_heading(body or ""):
            continue
        new_body, changes = repair_leaky_headings(body)
        if not changes:
            continue
        report["repaired"].append({
            "slug": slug,
            "headings": [{"before": b, "after": a} for b, a in changes],
        })
        if args.commit:
            set_clause = f"{body_col} = %s"
            upd_params: list = [new_body]
            if updated_col:
                set_clause += f", {updated_col} = NOW()"
            upd_params.append(slug)
            cur.execute(
                f"UPDATE {table} SET {set_clause} WHERE {slug_col} = %s",
                upd_params)

    if args.commit:
        conn.commit()
    else:
        conn.rollback()
    cur.close()
    conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        verb = "repaired" if args.commit else "would repair"
        print(f"[heading-repair] scanned={report['scanned']} "
              f"{verb}={len(report['repaired'])}")
        for item in report["repaired"]:
            print(f"  {item['slug']}")
            for h in item["headings"]:
                after = h["after"] or "<heading dropped>"
                print(f"    {h['before']!r} -> {after!r}")
        if not args.commit and report["repaired"]:
            print("[heading-repair] dry run — re-run with --commit to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
