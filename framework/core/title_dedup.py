"""Title-based near-duplicate detection for rec / proposal sets.

The pattern showed up in multiple producers: same logical rec (e.g.
"publish 50 new comparison page(s) — coverage gap") emitted every run
because the underlying state hasn't changed between ticks. Each
emission becomes a new (run_ts, rec_id) entry. The implementer ends up
dispatching 67 near-identical batches over 14 days — pure waste.

Two consumption patterns:

  1. **Producer-side**: before persisting `recommendations.json`, drop
     proposals whose title closely matches a title in the agent's own
     historical run-dirs OR accumulator. The "won't re-emit a topic
     we've already proposed" rule. See `filter_against_history()`.

  2. **Consumer-side**: backlog-dispatcher dedups by `(producer,
     normalized_title)` so the same logical rec is only dispatched
     once even if the producer emitted it 67×. See its `queued_ids`
     cache (handled inline in agent.py, not here).

Similarity: containment-based — share ≥50% of the smaller token set
AND ≥3 shared tokens (excluding stopwords). Validated against real
queue titles 2026-05-12:

    "Sound Blaster Audigy FX install (WinXP)"
    "Sound Blaster Audigy FX install (Win98)"
    → 3 shared tokens, 0.50 containment → MATCH ✓ (intentional)

    "Best Gaming Monitor for 1440p"
    "Best Gaming Mouse for FPS Esports"
    → 2 shared tokens ("gaming", "under"), under min_shared → NO MATCH ✓
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

# Filler words that don't differentiate titles. Common SEO/buyer-guide
# vocabulary that's reused everywhere.
DEFAULT_STOPWORDS = frozenset({
    "the", "a", "an", "for", "of", "in", "on", "to", "and", "or", "vs",
    "with", "best", "top", "complete", "guide", "tips", "tricks", "how",
    "what", "why", "review", "reviews", "ultimate", "easy", "quick",
    "simple", "your", "you", "from", "by", "is", "are", "be", "this",
    "that", "all", "new", "free", "page", "pages", "rec", "recs",
    "2022", "2023", "2024", "2025", "2026", "2027",
    # SEO-jargon shared across coverage-gap class recs. The recs follow
    # a template ("publish N new X page(s) — coverage gap") where the
    # only differentiator is the page-type token (comparison /
    # troubleshooting / buying-guide / brand / use-case). Without these
    # in stopwords, every coverage-gap rec is a "near-dup" of every
    # other one because they share "publish coverage gap".
    "publish", "coverage", "gap", "create", "add",
    # Analytics filler — every funnel rec has these
    "impressions", "clicks", "last", "30d", "7d", "investigate",
})


def title_signature(title: str,
                    *,
                    stopwords: frozenset = DEFAULT_STOPWORDS) -> frozenset:
    """Bag of meaningful tokens for dedup. Lowercase, ≥3 chars, no stopwords.

    Also drops pure-numeric tokens — "publish 50 X" and "publish 25 X"
    should look identical (the count is variable, not the topic).
    """
    tokens = re.findall(r"[a-z0-9]+", (title or "").lower())
    return frozenset(t for t in tokens
                     if len(t) >= 3
                     and t not in stopwords
                     and not t.isdigit())


def is_similar_title(a: frozenset, b: frozenset,
                     *,
                     threshold: float = 0.50,
                     min_shared: int = 3) -> bool:
    """Containment-based similarity over the smaller token set.

    Returns True iff EITHER:
      (a) `a == b` exactly — short titles like coverage-gap recs collapse
          to 1-2 differentiator tokens after stopword stripping, and
          min_shared=3 would miss them. Exact-match catches those.
      (b) both sets non-empty AND |a ∩ b| ≥ min_shared AND
          |a ∩ b| / min(|a|, |b|) ≥ threshold

    See module docstring for the calibration rationale.
    """
    if not a or not b:
        return False
    # Exact-match fast path — covers terse recs (e.g.
    # "publish N comparison page(s) — coverage gap" → both reduce to
    # {comparison} after stopwords + digits stripped). Without this,
    # min_shared=3 misses the most common dup class.
    if a == b:
        return True
    inter = a & b
    if len(inter) < min_shared:
        return False
    min_size = min(len(a), len(b))
    return (len(inter) / min_size) >= threshold if min_size else False


def filter_against_history(
    new_recs: list[dict],
    historical_titles: Iterable[str],
    *,
    title_key: str = "title",
    threshold: float = 0.50,
    min_shared: int = 3,
    on_skip: Callable[[str, str], None] | None = None,
) -> list[dict]:
    """Drop proposals whose title is a near-duplicate of any historical title
    OR of an earlier proposal in this same batch (intra-batch dedup).

    Args:
      new_recs:    proposals about to be persisted.
      historical_titles: iterable of strings — typically combined from
                   the agent's recent run-dirs + accumulator open + DB.
      title_key:   field on each rec that holds the title (default "title";
                   article-author uses "title" too).
      threshold / min_shared: see `is_similar_title`.
      on_skip:     callback `(dropped_title, reason)` for logging.

    Returns: kept-list (same order, minus duplicates).
    """
    existing_sigs = [title_signature(t) for t in historical_titles]
    existing_sigs = [s for s in existing_sigs if s]
    seen_sigs = list(existing_sigs)
    kept: list[dict] = []
    for r in new_recs:
        title = (r.get(title_key) or "").strip() if isinstance(r, dict) else ""
        sig = title_signature(title)
        if not sig:
            kept.append(r)
            continue
        match_idx = next((i for i, s in enumerate(seen_sigs)
                          if is_similar_title(sig, s,
                                              threshold=threshold,
                                              min_shared=min_shared)),
                         None)
        if match_idx is not None:
            if on_skip:
                src = "history" if match_idx < len(existing_sigs) else "intra-batch"
                on_skip(title, f"near-dup of existing ({src})")
            continue
        seen_sigs.append(sig)
        kept.append(r)
    return kept


# ── Published-catalog keyword collisions (2026-10-01 AI-reach audit) ──────
# `filter_against_history` above compares a producer's titles with its OWN
# past emissions. It never looks at what the site has already PUBLISHED, so
# a proposer could ship a second article on a keyword the site already owns:
# one site had 248 primary_keyword groups with more than one article (718 of
# 3,074 published), "best gpu for gpt-oss 20b" was the primary of two
# articles, and a sale hub listed a sibling spoke's primary keyword as its
# own secondary. `filter_keyword_collisions` is the published-catalog check
# every article proposer runs before it queues a proposal (the site's
# proposer supplies the published rows; this module never queries a DB).
#
# Rules, all on normalized keywords (lowercase, punctuation and year tokens
# dropped, simple plurals folded so "llm" == "llms"):
#   1. primary_keyword equals a published article's primary  -> reject
#   2. a secondary keyword equals a published primary        -> strip that
#      secondary (default) or reject the proposal (secondary_action="reject")
#   3. the title's token set is near-identical to a published title
#      (Jaccard >= title_jaccard) -> reject. Holiday-tagged proposals are
#      exempt: seasonal titles share their roundup suffix with evergreen
#      pieces (framework CLAUDE.md dedup rule).
#   4. within one `holiday:<id>`: a primary keyword or a top pick (first
#      entry of the first non-empty `pick_fields` list) that a published or
#      earlier-in-batch sibling with the same tag already uses -> reject.
#      Distinct category spokes of one sale event stay allowed.
#   5. intra-batch: a later proposal with the same normalized primary as an
#      earlier one -> reject.

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


# -s words that are not plurals; folding them would only matter if one side
# used a different form, but keep them readable in logs.
_NOT_PLURAL = frozenset({
    "asus", "bonus", "status", "virus", "campus", "cactus", "focus", "plus",
    "nexus", "corpus", "hummus", "citrus", "asparagus", "octopus", "census",
    "series", "species", "news", "lens", "chassis", "analysis", "basis",
    "tennis", "this", "thus", "always", "various",
})


def _fold_plural(tok: str) -> str:
    if (len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "ous"))
            and tok not in _NOT_PLURAL):
        return tok[:-1]
    return tok


def normalize_keyword(kw: str) -> str:
    """Comparable form of a keyword: lowercase, alphanumeric tokens only,
    year tokens dropped, simple plurals folded. "Best GPUs for LLMs (2026)"
    -> "best gpu for llm"."""
    toks = re.findall(r"[a-z0-9]+", str(kw or "").lower())
    return " ".join(_fold_plural(t) for t in toks if not _YEAR_RE.match(t))


def _norm_title_tokens(title: str) -> frozenset:
    return frozenset(_fold_plural(t) for t in title_signature(title)
                     if not _YEAR_RE.match(t))


def holiday_ids(tags: Iterable[str] | None, prefix: str = "holiday:") -> set[str]:
    """The occasion ids a row is tagged for (`holiday:<id>` tags)."""
    out: set[str] = set()
    for t in tags or []:
        t = str(t or "").strip().lower()
        if t.startswith(prefix) and t[len(prefix):] not in ("", "none", "null", "n/a"):
            out.add(t[len(prefix):])
    return out


def _top_pick(row: dict, pick_fields: Iterable[str]) -> str:
    for f in pick_fields:
        vals = row.get(f) if isinstance(row, dict) else None
        if isinstance(vals, str):
            vals = [vals]
        for v in vals or []:
            if isinstance(v, dict):
                v = v.get("asin") or v.get("slug") or ""
            v = str(v or "").strip().lower()
            if v:
                return v
    return ""


def filter_keyword_collisions(
    proposals: list[dict],
    published: Iterable[dict],
    *,
    secondary_action: str = "strip",
    title_jaccard: float = 0.8,
    pick_fields: Iterable[str] = ("related_product_asins",),
    holiday_prefix: str = "holiday:",
    on_drop: Callable[[dict, str], None] | None = None,
    on_strip: Callable[[dict, str, str], None] | None = None,
) -> list[dict]:
    """Drop (or trim) proposals that collide with the published catalog.

    `published`: rows with `slug`, `title`, `primary_keyword`,
    `secondary_keywords`, `tags` and optionally the `pick_fields` lists.
    `secondary_action`: "strip" removes the colliding secondary keyword from
    the proposal (the audit's own remedy for a hub that lists a spoke's
    primary); "reject" drops the proposal; "off" skips rule 2.
    `title_jaccard`: 0 disables rule 3.
    Returns the kept proposals (copies when a secondary was stripped), in order.
    `on_drop(proposal, reason)` / `on_strip(proposal, keyword, owner_slug)`
    are logging hooks.
    """
    pick_fields = tuple(pick_fields or ())
    primaries: dict[str, str] = {}
    titles: list[tuple[frozenset, str]] = []
    by_holiday: dict[str, dict[str, dict[str, str]]] = {}

    def _index(row: dict, label: str) -> None:
        pk = normalize_keyword(row.get("primary_keyword") or "")
        if pk:
            primaries.setdefault(pk, label)
        for hid in holiday_ids(row.get("tags"), holiday_prefix):
            h = by_holiday.setdefault(hid, {"primary": {}, "pick": {}})
            if pk:
                h["primary"].setdefault(pk, label)
            tp = _top_pick(row, pick_fields)
            if tp:
                h["pick"].setdefault(tp, label)

    for row in published or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("slug") or row.get("title") or "?")
        _index(row, label)
        sig = _norm_title_tokens(row.get("title") or "")
        if sig:
            titles.append((sig, label))

    kept: list[dict] = []
    for p in proposals or []:
        if not isinstance(p, dict):
            continue
        label = f"this batch: {p.get('slug') or p.get('title') or p.get('id') or '?'}"
        pk = normalize_keyword(p.get("primary_keyword") or "")
        hids = holiday_ids(p.get("tags"), holiday_prefix)
        reason = ""
        if pk and pk in primaries:
            reason = f"primary keyword {pk!r} is already the primary of {primaries[pk]}"
        if not reason:
            for hid in sorted(hids):
                h = by_holiday.get(hid) or {}
                if pk and pk in (h.get("primary") or {}):
                    reason = f"{holiday_prefix}{hid}: primary {pk!r} duplicates {h['primary'][pk]}"
                    break
                tp = _top_pick(p, pick_fields)
                if tp and tp in (h.get("pick") or {}):
                    reason = (f"{holiday_prefix}{hid}: top pick {tp} is already the top pick "
                              f"of {h['pick'][tp]}")
                    break
        if not reason and title_jaccard and not hids:
            sig = _norm_title_tokens(p.get("title") or "")
            if sig:
                for s, owner in titles:
                    union = sig | s
                    if union and len(sig & s) / len(union) >= title_jaccard:
                        reason = f"title is near-identical to published {owner}"
                        break
        if not reason and secondary_action == "reject":
            for kw in p.get("secondary_keywords") or []:
                nk = normalize_keyword(kw)
                if nk and nk in primaries and nk != pk:
                    reason = f"secondary keyword {nk!r} is the primary of {primaries[nk]}"
                    break
        if reason:
            if on_drop:
                on_drop(p, reason)
            continue
        if secondary_action == "strip" and p.get("secondary_keywords"):
            keep_kw = []
            for kw in p.get("secondary_keywords") or []:
                nk = normalize_keyword(kw)
                if nk and nk in primaries and nk != pk:
                    if on_strip:
                        on_strip(p, str(kw), primaries[nk])
                    continue
                keep_kw.append(kw)
            if len(keep_kw) != len(p.get("secondary_keywords") or []):
                p = {**p, "secondary_keywords": keep_kw}
        kept.append(p)
        _index(p, label)
        sig = _norm_title_tokens(p.get("title") or "")
        if sig and not hids:
            titles.append((sig, label))
    return kept
