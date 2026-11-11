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
     prefixed string like `holiday:memorial-day`. Tags are read through
     `seasonal_calendar.canonicalizer()`, so an alias such as
     `holiday:black-friday-cyber-monday` counts for `black-friday`.

  2. This module runs each article-author tick (or via the
     featured-rotation-agent on a 1h timer) and:
       - Reads active occasions from seasonal_calendar
       - For each NOW/IMMINENT occasion, queries `editorial_articles`
         for rows tagged `holiday:<id>`, status='published'
       - Picks up to N winners. With a score map (verified affiliate
         clicks, see `score_source`), candidates are ranked by clicks,
         then recency (newest first), then slug, across every active
         occasion; scores below `min_score` count as zero. Without one, a
         stable hour-of-day cursor rotates through each occasion's
         candidates by slug, NOW occasions first.
       - UPDATEs `featured = true` for winners, `featured = false` for
         articles whose holiday tags all point to OUT-OF-WINDOW occasions
         (so January's Christmas piece doesn't stay featured in May).
         This demotion also runs on days with no active occasion
         (`demote_when_idle`, default on): until 2026-11-11 the function
         returned early on such days, and two Halloween pieces stayed
         featured for 11 days. `demote_when_idle=False` keeps stale
         holiday features on idle days, as before.

  3. Articles with no holiday tags AND `featured = true` are
     untouched — operator-pinned manual features keep their slot. The one
     exception is an article this module itself promoted through
     `evergreen_fallback`; it remembers those in agent storage and
     demotes them when they stop winning or the fallback is switched off.

`score_source` (optional, per site): {table, spec, match, group_col,
window_days (30), path_prefixes, min_score (1)} — the same shape as the
output gate's `traffic_gate.affiliate_clicks`. Clicks per article page come
from framework.core.human_clicks; pages map to slugs under `path_prefixes`.

`evergreen_fallback` (optional): {enabled, max_features, min_score}. When
the holiday candidates leave slots empty (or no occasion is active), the
best-selling articles that carry no holiday tag fill them, up to
`max_features` features from this module in total. Needs a score map plus
`storage` and `profile` (the agent id), so the module can remember what it
promoted (agents/<profile>/state/featured-rotation.json); without them it
is skipped. Ownership rules:
  - the claim is saved BEFORE the promoting UPDATE; if that save fails
    the new picks are not promoted this run (nothing unowned goes up);
  - the state is read whenever storage + profile are given, so switching
    the fallback off (or removing its score source) releases its picks;
  - when the scores are temporarily unreadable its picks are held as they
    are (no promotion, no release) until the next readable run;
  - a recorded slug that is no longer featured, or now carries a holiday
    tag, is dropped from the state, so a stale record never captures an
    article an operator features later.

Sites consume via:

    from framework.core.featured_rotation import cycle_from_config
    cycle_from_config(dsn, site_cfg["featured_rotation"], audience="food",
                      storage=self.storage, profile=self.agent_id)

or the keyword form, cycle_homepage_features(dsn, audience="food",
max_features=6, ...).

Idempotent + safe to call every run.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Mapping, Optional

from .seasonal_calendar import (ActiveOccasion, active_signal,  # noqa: F401
                                canonicalizer, load_occasions)


HOLIDAY_TAG_PREFIX = "holiday:"
STATE_KEY_TMPL = "agents/{profile}/state/featured-rotation.json"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

log = logging.getLogger(__name__)


def _ident(name: str) -> str:
    if not _IDENT_RE.match(str(name or "")):
        raise ValueError(f"featured_rotation: unsafe SQL identifier {name!r}")
    return name


def holiday_ids_from_tags(tags, canon=None) -> set[str]:
    """`holiday:<id>` tags -> occasion ids. With `canon` (a
    seasonal_calendar.canonicalizer) aliases map to the calendar id. An id
    the calendar does not know is kept as written: the article is still a
    holiday piece (never in window, so demoted as stale), not an
    operator pin."""
    out: set[str] = set()
    for t in tags or ():
        t = str(t)
        if not t.startswith(HOLIDAY_TAG_PREFIX):
            continue
        raw = t[len(HOLIDAY_TAG_PREFIX):]
        out.add((canon(raw) if canon is not None else None) or raw)
    return out


def _ts(row: Mapping) -> float:
    v = row.get("published_at")
    try:
        return float(v.timestamp()) if v is not None else 0.0
    except Exception:
        return 0.0


def _read_owned(storage, profile: str) -> list[str]:
    """Slugs this module promoted through evergreen_fallback ([] when none
    or unreadable). An unreadable state can only make the module treat its
    own picks as operator pins for a run, never demote a real pin."""
    try:
        doc = storage.read_json(STATE_KEY_TMPL.format(profile=profile))
    except Exception:
        doc = None
    if not isinstance(doc, dict):
        return []
    return list(dict.fromkeys(str(s) for s in doc.get("evergreen") or [] if s))


def _write_owned(storage, profile: str, slugs: list[str]) -> None:
    storage.write_json(STATE_KEY_TMPL.format(profile=profile), {
        "schema_version": "1",
        "evergreen": list(slugs),
        "at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds"),
    })


def cycle_homepage_features(dsn: str,
                            *,
                            audience: str = "food",
                            max_features: int = 6,
                            rotate_every_hours: int = 6,
                            articles_table: str = "editorial_articles",
                            tags_column: str = "tags",
                            featured_column: str = "featured",
                            published_column: str = "published_at",
                            dry_run: bool = False,
                            today: datetime.date | None = None,
                            demote_when_idle: bool = True,
                            scores: Optional[Mapping[str, float]] = None,
                            score_source: Optional[Mapping] = None,
                            evergreen_fallback: Optional[Mapping] = None,
                            storage=None,
                            profile: str = "",
                            conn=None) -> dict:
    """Update `<articles_table>.<featured_column>` to reflect the
    current rotation. Returns a stats dict.

    `tags_column` is expected to be a TEXT[] (array of strings).
    Articles tagged `holiday:<occasion-id>` (or an alias of it) are
    candidates for that occasion.

    `scores` ({slug: verified clicks}) or `score_source` (computed here,
    see the module docstring) order the candidates by what sells, then by
    recency. Without either, the old slug-ordered hour rotation applies.
    `storage` applies config/seasonal-calendar.json and, with `profile`,
    holds the evergreen ownership state. `demote_when_idle` only governs
    stale holiday features on a day with no active occasion; evergreen
    picks follow their own rules. `conn` lets the caller pass an open
    connection (left open).
    """
    today = today or datetime.date.today()
    occasions = load_occasions(storage)
    active = active_signal(today, audience=audience, occasions=occasions)
    # UPCOMING is too soft to flip the homepage; only NOW + IMMINENT trigger
    # active-feature status. UPCOMING gets surfaced through the article
    # proposer instead. NOW first, then soonest (active_signal order).
    active_order: list[str] = []
    for a in active:
        if a.window in ("now", "imminent") and a.occasion.id not in active_order:
            active_order.append(a.occasion.id)
    active_ids = set(active_order)
    canon = canonicalizer(audience, occasions=occasions, warn=False)
    eg = dict(evergreen_fallback or {})
    eg_on = bool(eg.get("enabled"))
    src = dict(score_source or {})
    src_on = bool(src) and src.get("enabled", True) is not False

    stats: dict[str, Any] = {
        "active_occasions": sorted(active_ids),
        "candidates_by_occasion": {},
        "promoted": [],
        "demoted": [],
        "untouched_pinned": 0,
        "rotate_hour_cursor": _hour_cursor(rotate_every_hours),
        "dry_run": dry_run,
    }

    # What evergreen_fallback promoted on earlier runs. Read whenever it can
    # be, so those picks are released even once the fallback is off.
    track = storage is not None and bool(profile)
    stored = _read_owned(storage, profile) if track else []

    if not active_ids and not demote_when_idle and not eg_on and not stored:
        log.info("no active occasions today — leaving featured flags alone")
        return stats

    table = _ident(articles_table)
    tags_col = _ident(tags_column)
    feat_col = _ident(featured_column)
    pub_col = _ident(published_column)

    try:
        import psycopg2
        import psycopg2.extras
        dict_cursor = {"cursor_factory": psycopg2.extras.RealDictCursor}
    except ImportError:
        if conn is None:
            log.warning("psycopg2 not installed; skipping featured rotation")
            return {"skipped": True, "reason": "psycopg2 missing"}
        psycopg2 = None
        dict_cursor = {}

    own_conn = conn is None
    if own_conn:
        conn = psycopg2.connect(dsn, connect_timeout=15)
        conn.autocommit = False
    try:
        # Fetch all currently-featured articles + any candidate articles
        # tagged with our active occasions.
        with conn.cursor(**dict_cursor) as cur:
            cur.execute(
                f"SELECT id, slug, {tags_col} AS tags, "
                f"{feat_col} AS featured, {pub_col} AS published_at "
                f"FROM {table} "
                f"WHERE status = 'published'"
            )
            rows = cur.fetchall()

        score_map: dict[str, float] = {}
        # True when the score map can be trusted, even if empty (given, or
        # read OK); False when there are none or the read failed.
        scores_ok = False
        min_score = 1.0
        if scores:
            score_map = {str(k).strip().lower(): float(v)
                         for k, v in scores.items() if v}
            scores_ok = True
            stats["scores"] = f"given: {len(score_map)} slug(s)"
        elif src_on:
            try:
                from .article_output_gate import affiliate_clicks_by_slug
                score_map = {k: float(v) for k, v in affiliate_clicks_by_slug(
                    conn, src, days=int(src.get("window_days", 30)),
                    prefixes=list(src.get("path_prefixes") or ()),
                    profile=profile, storage=storage).items()}
                scores_ok = True
                stats["scores"] = f"ok: {len(score_map)} article(s) with clicks"
            except Exception as e:  # rotation still runs, by slug
                score_map = {}
                stats["scores"] = f"unavailable: {type(e).__name__}: {e}"[:200]
                try:
                    conn.rollback()
                except Exception:
                    pass
        elif scores is not None:
            scores_ok = True            # an explicit empty map: nothing sells
            stats["scores"] = "given: 0 slug(s)"
        if src_on:
            try:
                min_score = float(src.get("min_score", 1))
            except (TypeError, ValueError):
                min_score = 1.0

        def score(r: Mapping) -> float:
            return score_map.get(str(r["slug"]).lower(), 0.0)

        candidates_by_occ: dict[str, list[dict]] = {oid: [] for oid in active_order}
        currently_featured: list[dict] = []
        untagged: list[dict] = []
        for r in rows:
            holiday_ids = holiday_ids_from_tags(r.get("tags"), canon)
            if r.get("featured"):
                currently_featured.append({**r, "holiday_ids": holiday_ids})
            if not holiday_ids:
                untagged.append(r)
            for oid in active_order:
                if oid in holiday_ids:
                    candidates_by_occ[oid].append(r)

        targeted: list[str] = []
        cursor = _hour_cursor(rotate_every_hours)
        for oid in active_order:
            stats["candidates_by_occasion"][oid] = len(candidates_by_occ[oid])
        if score_map:
            # What sells first, then the newest, across every active
            # occasion. Below min_score a click count is noise: it ranks
            # as zero, by recency.
            def ranked_score(r: Mapping) -> float:
                s = score(r)
                return s if s >= min_score else 0.0

            seen: set[str] = set()
            pool: list[dict] = []
            for oid in active_order:
                for r in candidates_by_occ[oid]:
                    if r["slug"] not in seen:
                        seen.add(r["slug"])
                        pool.append(r)
            pool.sort(key=lambda r: (-ranked_score(r), -_ts(r), r["slug"]))
            targeted = [r["slug"] for r in pool[:max(0, int(max_features))]]
            stats["ranked_by"] = "verified clicks"
        else:
            # Rotate-by-hour through each occasion's candidates (stable slug
            # order), NOW occasions first.
            for oid in active_order:
                if len(targeted) >= max_features:
                    break
                cands = sorted((c for c in candidates_by_occ[oid]
                                if c["slug"] not in targeted),
                               key=lambda c: c["slug"])
                if not cands:
                    continue
                n = len(cands)
                for i in range(min(max_features - len(targeted), n)):
                    targeted.append(cands[(cursor + i) % n]["slug"])

        # Evergreen ownership. `owned`: recorded picks that are still
        # featured with no holiday tag (anything else is no longer ours to
        # manage). `keep`: owned picks that stay featured this run.
        featured_untagged = {f["slug"] for f in currently_featured
                             if not f["holiday_ids"]}
        owned = [s for s in stored if s in featured_untagged]
        eg_picks: list[str] = []
        keep: set[str] = set()
        eg_active = False
        if eg_on and not scores_ok and not src_on:
            stats["evergreen"] = "skipped: no verified-click scores"
        elif eg_on and not track:
            stats["evergreen"] = ("skipped: needs storage + profile to "
                                  "remember what it promoted")
        elif eg_on and not scores_ok:
            # Transient: the score source failed. Neither promote nor
            # release on a missing signal; next readable run decides.
            keep = set(owned)
            stats["evergreen"] = (f"held: verified-click scores unavailable; "
                                  f"{len(owned)} pick(s) kept")
        elif eg_on:
            eg_active = True
            try:
                eg_max = min(int(max_features),
                             int(eg.get("max_features", max_features)))
                eg_min = float(eg.get("min_score", min_score))
            except (TypeError, ValueError):
                eg_max, eg_min = int(max_features), min_score
            ranked = sorted((r for r in untagged
                             if r["slug"] not in targeted and score(r) >= eg_min),
                            key=lambda r: (-score(r), -_ts(r), r["slug"]))
            for r in ranked:
                if len(targeted) + len(eg_picks) >= eg_max:
                    break
                eg_picks.append(r["slug"])
            keep = set(eg_picks)
            stats["evergreen"] = eg_picks
        elif owned:
            stats["evergreen"] = f"off: releasing {len(owned)} pick(s) it promoted"

        # Demote: any currently-featured article whose holiday tags ALL
        # point to OUT-OF-WINDOW occasions (i.e. seasonal stale), unless it
        # is an idle day and demote_when_idle is off; and any evergreen pick
        # of ours that is not kept. Operator-pinned articles (featured=true
        # with NO holiday tags, not ours) are not touched.
        demote_stale = bool(active_ids) or demote_when_idle
        to_demote: list[str] = []
        for f in currently_featured:
            if f["holiday_ids"]:
                if f["holiday_ids"] & active_ids:
                    continue  # still on-window
                if demote_stale:
                    to_demote.append(f["slug"])
            elif f["slug"] in owned:
                if f["slug"] not in keep:
                    to_demote.append(f["slug"])  # our evergreen pick, slot lost
            else:
                stats["untouched_pinned"] += 1  # operator-pinned, leave alone

        # Promote: any candidate slug not currently featured
        currently_featured_slugs = {f["slug"] for f in currently_featured}
        to_promote = [s for s in targeted + eg_picks
                      if s not in currently_featured_slugs]
        # Evergreen slugs this module owns after this run: the ones it
        # promotes now plus the owned ones it keeps. An article an operator
        # had featured is never claimed, so it is never demoted by this
        # module.
        if eg_active:
            owned_new = [s for s in eg_picks if s in to_promote or s in owned]
        else:
            owned_new = [s for s in owned if s in keep]
        if score_map:
            stats["scores_of_picks"] = {s: score_map.get(s.lower(), 0.0)
                                        for s in targeted + eg_picks}

        stats["promoted"] = to_promote
        stats["demoted"] = to_demote

        if dry_run:
            return stats

        # Claim before promoting: if the claim cannot be saved, the new
        # evergreen picks stay down this run rather than go up unowned
        # (an unowned feature looks like an operator pin forever).
        new_claims = [s for s in owned_new if s not in stored]
        if track and new_claims:
            claim = list(dict.fromkeys(stored + owned_new))
            try:
                _write_owned(storage, profile, claim)
                stored = claim
            except Exception as e:
                log.warning("featured rotation: evergreen claim not saved "
                            "(%s); not promoting %s", e, new_claims)
                to_promote = [s for s in to_promote if s not in new_claims]
                owned_new = [s for s in owned_new if s not in new_claims]
                stats["promoted"] = to_promote
                stats["evergreen_state"] = (
                    f"claim not saved ({type(e).__name__}); new evergreen "
                    "picks not promoted")

        if to_promote or to_demote:
            with conn.cursor() as cur:
                if to_promote:
                    cur.execute(
                        f"UPDATE {table} SET {feat_col} = TRUE "
                        f"WHERE slug = ANY(%s) AND status = 'published'",
                        (to_promote,),
                    )
                if to_demote:
                    cur.execute(
                        f"UPDATE {table} SET {feat_col} = FALSE "
                        f"WHERE slug = ANY(%s)",
                        (to_demote,),
                    )
            conn.commit()
        # Trim the record to what is ours now (order is not meaningful). If
        # this write fails the record keeps a superset; those slugs are no
        # longer featured, so the next run drops them (see `owned`).
        if track and set(stored) != set(owned_new):
            try:
                _write_owned(storage, profile, owned_new)
            except Exception as e:
                log.warning("featured rotation: evergreen state not trimmed "
                            "(%s); next run retries", e)
                stats.setdefault("evergreen_state",
                                 f"state not trimmed ({type(e).__name__}); "
                                 "next run retries")
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
    return stats


# site.yaml `featured_rotation` keys passed through by cycle_from_config().
CONFIG_KEYS = ("max_features", "rotate_every_hours", "articles_table",
               "tags_column", "featured_column", "published_column",
               "demote_when_idle", "score_source", "evergreen_fallback")


def cycle_from_config(dsn: str, cfg: Optional[Mapping], *, audience: str,
                      dry_run: bool = False, storage=None, profile: str = "",
                      today: datetime.date | None = None,
                      conn=None) -> Optional[dict]:
    """cycle_homepage_features() driven by a site.yaml `featured_rotation`
    block. Returns None when the block is missing or `enabled` is false.
    New knobs reach every consumer without a code change."""
    cfg = dict(cfg or {})
    if not cfg.get("enabled"):
        return None
    kw = {k: cfg[k] for k in CONFIG_KEYS if k in cfg and cfg[k] is not None}
    if "max_features" in kw:
        kw["max_features"] = int(kw["max_features"])
    if "rotate_every_hours" in kw:
        kw["rotate_every_hours"] = int(kw["rotate_every_hours"])
    return cycle_homepage_features(
        dsn, audience=audience, dry_run=bool(dry_run or cfg.get("dry_run")),
        storage=storage, profile=profile, today=today, conn=conn, **kw)


def _hour_cursor(rotate_every_hours: int) -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.toordinal() * 24 + now.hour) // max(1, rotate_every_hours)
