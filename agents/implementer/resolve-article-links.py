#!/usr/bin/env python3
"""Post-step utility: resolve invented `/recipes/<slug>` links in
article body_md to real recipe URLs by fuzzy-matching the slug
against actual recipe titles in the live DB.

Why this exists:
  Article-author LLMs often hallucinate recipe slugs like
  `/recipes/greek-salad-with-feta` because the proposal carries bare
  `expected_recipe_slugs` (no IDs). The actual public URL on
  aisleprompt.com is `/recipes/<slugified-title>-<id>`. Without the
  ID, the link 404s.

  This resolver:
    1. Scans body_md for `/recipes/<slug>` patterns missing a trailing
       `-<digits>` (the broken pattern).
    2. Slugifies every recipe title in `recipe_catalog` and finds the
       best fuzzy match using normalized token overlap.
    3. If match score >= threshold, replaces the URL with
       `/recipes/<actual-slug>-<id>`.
    4. If no match (score below threshold), strips the markdown link
       wrapper and keeps just the link text — so the article still
       reads naturally, no broken anchor.

Usage:
  python3 resolve-article-links.py [--apply] [--article-id N]
    --apply            Actually UPDATE the rows (default: dry-run shows diffs)
    --article-id N     Limit to one article (default: all framework-written ones)

Env: DATABASE_URL must be set.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

_LINK_RE = re.compile(r"\]\(https?://[^/]*/recipes/([a-z0-9][a-z0-9-]*)\)")
_BARE_RE = re.compile(r"\bhttps?://[^/\s]*/recipes/([a-z0-9][a-z0-9-]*)\b")


def slugify(title: str) -> str:
    """Lossy title → slug: lowercase, [^a-z0-9-] → '-', collapse '-+'."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def tokens(slug: str) -> set[str]:
    return {t for t in slug.split("-") if t and t not in
            {"the", "a", "an", "and", "with", "for", "in", "on", "of", "to"}}


def score_match(invented: str, real: str) -> float:
    """Jaccard-ish overlap of token sets, weighted by overlap of order."""
    a, b = tokens(invented), tokens(real)
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    return overlap / max(len(a), len(b))


def load_recipe_index(cur) -> list[tuple[int, str, str]]:
    """Return [(id, title, slug)] for all active recipes."""
    cur.execute(
        "SELECT id, title FROM recipe_catalog "
        "WHERE COALESCE(is_active, TRUE) = TRUE")
    return [(rid, title, slugify(title)) for rid, title in cur.fetchall()]


def best_match(invented_slug: str, index, threshold: float = 0.55):
    best = (None, 0.0)
    for rid, title, real_slug in index:
        s = score_match(invented_slug, real_slug)
        if s > best[1]:
            best = ((rid, title, real_slug), s)
    if best[1] >= threshold:
        return best
    return (None, best[1])


def resolve_body(body_md: str, index) -> tuple[str, dict]:
    """Returns (new_body, stats)."""
    stats = {"matched": 0, "stripped": 0, "kept": 0, "scanned": 0}
    fixed = body_md

    def _link_sub(m: re.Match) -> str:
        slug = m.group(1)
        stats["scanned"] += 1
        # Already has trailing -NNNN — leave it alone.
        if re.search(r"-\d+$", slug):
            stats["kept"] += 1
            return m.group(0)
        match, score = best_match(slug, index)
        if match:
            rid, title, real_slug = match
            stats["matched"] += 1
            return f"](https://aisleprompt.com/recipes/{real_slug}-{rid})"
        stats["stripped"] += 1
        # Markdown link form: [text](url) — strip the link, keep text.
        # We're matching just the ](url) tail here; locate the [text]
        # in the surrounding context by walking back to the matching `[`.
        # Simpler: return a placeholder we'll post-process below.
        return "]__STRIP_LINK__"

    fixed = _LINK_RE.sub(_link_sub, fixed)

    # Now collapse `[text]__STRIP_LINK__` back into bare `text`.
    fixed = re.sub(r"\[([^\]]+)\]__STRIP_LINK__", r"\1", fixed)

    # Bare URLs (no markdown wrapper) — same logic but no link to strip.
    def _bare_sub(m: re.Match) -> str:
        slug = m.group(1)
        stats["scanned"] += 1
        if re.search(r"-\d+$", slug):
            stats["kept"] += 1
            return m.group(0)
        match, _ = best_match(slug, index)
        if match:
            rid, _, real_slug = match
            stats["matched"] += 1
            return f"https://aisleprompt.com/recipes/{real_slug}-{rid}"
        stats["stripped"] += 1
        # Replace the URL with the slugified text inside.
        return slug.replace("-", " ")

    fixed = _BARE_RE.sub(_bare_sub, fixed)
    return fixed, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--article-id", type=int, default=0)
    ap.add_argument("--threshold", type=float, default=0.55)
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    import psycopg2
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    print("[link-resolver] indexing recipe_catalog...", file=sys.stderr)
    index = load_recipe_index(cur)
    print(f"[link-resolver] {len(index)} recipes indexed", file=sys.stderr)

    if args.article_id:
        cur.execute(
            "SELECT id, slug, body_md FROM editorial_articles "
            "WHERE id = %s", (args.article_id,))
    else:
        cur.execute(
            "SELECT id, slug, body_md FROM editorial_articles "
            "WHERE (written_by LIKE 'framework-%%' OR written_by = 'aider-cli') "
            "ORDER BY id")
    articles = cur.fetchall()

    totals = Counter()
    for aid, slug, body in articles:
        if not body:
            continue
        new_body, stats = resolve_body(body, index)
        totals.update(stats)
        changed = new_body != body
        print(f"  id={aid} slug={slug}: scanned={stats['scanned']} "
              f"matched={stats['matched']} stripped={stats['stripped']} "
              f"kept={stats['kept']} {'(changed)' if changed else '(unchanged)'}",
              file=sys.stderr)
        if args.apply and changed:
            with conn.cursor() as upd:
                upd.execute(
                    "UPDATE editorial_articles SET body_md = %s, "
                    "updated_at = now() WHERE id = %s", (new_body, aid))
                conn.commit()

    print(f"\n[link-resolver] totals: scanned={totals['scanned']} "
          f"matched={totals['matched']} stripped={totals['stripped']} "
          f"kept={totals['kept']}", file=sys.stderr)
    if not args.apply:
        print("(dry-run — pass --apply to write)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
