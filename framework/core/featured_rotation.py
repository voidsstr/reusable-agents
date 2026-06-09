"""Cycle the `featured` flag on editorial_articles by active holiday.

The site's homepage rail shows articles where `featured = true`. The
problem we solve here: that flag is static. Without a cycler, the
homepage either always shows the SAME picks (boring, low CTR) or shows
nothing relevant on a key cultural moment (Memorial Day weekend should
feature grilling articles, not a generic "20 Sunday meal prep recipes"
left over from January).

Strategy:

  1. The article-author tags every proposal with `holidays: [<id>, ...]`
     drawn from the seasonal_calendar primitive (NOW or IMMINENT
     occasions). Tag is persisted to `editorial_articles.tags` as a
     prefixed string like `holiday:memorial-day`.

  2. This module runs each article-author tick (or via the
     featured-rotation-agent on a 1h timer) and:
       - Reads active occasions from seasonal_calendar
       - For each NOW/IMMINENT occasion, queries `editorial_articles`
         for rows tagged `holiday:<id>`, status='published'
       - Picks up to N rotation winners using a stable hour-of-day cursor
       - UPDATEs `featured = true` for winners, `featured = false` for
         articles whose only holiday tag matches an OUT-OF-WINDOW
         occasion (so January's Christmas piece doesn't stay featured
         in May)

  3. Articles with no holiday tags AND `featured = true` are
     untouched — operator-pinned manual features keep their slot.

Sites consume via:

    from framework.core.featured_rotation import cycle_homepage_features
    cycle_homepage_features(dsn, audience="food", max_features=6)

Idempotent + safe to call every run.
"""
from __future__ import annotations

import datetime
import logging
import sys

from .seasonal_calendar import active_signal, ActiveOccasion


HOLIDAY_TAG_PREFIX = "holiday:"

log = logging.getLogger(__name__)


def cycle_homepage_features(dsn: str,
                            *,
                            audience: str = "food",
                            max_features: int = 6,
                            rotate_every_hours: int = 6,
                            articles_table: str = "editorial_articles",
                            tags_column: str = "tags",
                            featured_column: str = "featured",
                            dry_run: bool = False,
                            today: datetime.date | None = None) -> dict:
    """Update `<articles_table>.<featured_column>` to reflect the
    current rotation. Returns a stats dict.

    `tags_column` is expected to be a TEXT[] (array of strings).
    Articles tagged `holiday:<occasion-id>` are considered candidates
    for that occasion.
    """
    today = today or datetime.date.today()
    active = active_signal(today, audience=audience)
    active_ids = {a.occasion.id for a in active if a.window in ("now", "imminent")}
    # UPCOMING is too soft to flip the homepage; only NOW + IMMINENT trigger
    # active-feature status. UPCOMING gets surfaced through the article
    # proposer instead.

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        log.warning("psycopg2 not installed; skipping featured rotation")
        return {"skipped": True, "reason": "psycopg2 missing"}

    stats = {
        "active_occasions": sorted(active_ids),
        "candidates_by_occasion": {},
        "promoted": [],
        "demoted": [],
        "untouched_pinned": 0,
        "rotate_hour_cursor": _hour_cursor(rotate_every_hours),
        "dry_run": dry_run,
    }

    if not active_ids:
        log.info("no active occasions today — leaving featured flags alone")
        return stats

    conn = psycopg2.connect(dsn, connect_timeout=15)
    try:
        conn.autocommit = False
        # Fetch all currently-featured articles + any candidate articles
        # tagged with our active occasions.
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT id, slug, {tags_column} AS tags, "
                f"{featured_column} AS featured "
                f"FROM {articles_table} "
                f"WHERE status = 'published'"
            )
            rows = cur.fetchall()

        candidates_by_occ: dict[str, list[dict]] = {oid: [] for oid in active_ids}
        currently_featured: list[dict] = []
        for r in rows:
            tags = [t for t in (r.get("tags") or [])]
            holiday_ids = {t[len(HOLIDAY_TAG_PREFIX):]
                           for t in tags if t.startswith(HOLIDAY_TAG_PREFIX)}
            if r.get("featured"):
                currently_featured.append({**r, "holiday_ids": holiday_ids})
            for oid in (holiday_ids & active_ids):
                candidates_by_occ[oid].append(r)

        # Pick rotation winners per occasion
        cursor = _hour_cursor(rotate_every_hours)
        targeted_slugs: set[str] = set()
        for oid, cands in candidates_by_occ.items():
            stats["candidates_by_occasion"][oid] = len(cands)
            if not cands:
                continue
            # Stable order for repeatable rotation
            cands.sort(key=lambda c: c["slug"])
            n = len(cands)
            take = min(max_features, n)
            # Rotate-by-hour to cycle different slugs across the day
            picks = [cands[(cursor + i) % n] for i in range(take)]
            for p in picks:
                targeted_slugs.add(p["slug"])
                if len(targeted_slugs) >= max_features:
                    break
            if len(targeted_slugs) >= max_features:
                break

        # Demote: any currently-featured article whose holiday tags ALL
        # point to OUT-OF-WINDOW occasions (i.e. seasonal stale).
        # Operator-pinned articles (featured=true with NO holiday tags)
        # are not touched.
        to_demote: list[str] = []
        for f in currently_featured:
            if not f["holiday_ids"]:
                stats["untouched_pinned"] += 1
                continue  # operator-pinned, leave alone
            if f["holiday_ids"] & active_ids:
                continue  # still on-window
            to_demote.append(f["slug"])

        # Promote: any candidate slug not currently featured
        to_promote: list[str] = []
        currently_featured_slugs = {f["slug"] for f in currently_featured}
        for s in targeted_slugs:
            if s not in currently_featured_slugs:
                to_promote.append(s)

        stats["promoted"] = to_promote
        stats["demoted"] = to_demote

        if not (to_promote or to_demote):
            return stats

        if dry_run:
            return stats

        with conn.cursor() as cur:
            if to_promote:
                cur.execute(
                    f"UPDATE {articles_table} SET {featured_column} = TRUE "
                    f"WHERE slug = ANY(%s) AND status = 'published'",
                    (to_promote,),
                )
            if to_demote:
                cur.execute(
                    f"UPDATE {articles_table} SET {featured_column} = FALSE "
                    f"WHERE slug = ANY(%s)",
                    (to_demote,),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return stats


def _hour_cursor(rotate_every_hours: int) -> int:
    now = datetime.datetime.utcnow()
    return (now.toordinal() * 24 + now.hour) // max(1, rotate_every_hours)
