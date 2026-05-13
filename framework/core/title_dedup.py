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
