"""Deterministic opportunity scoring for app-store candidates.

Companion to `app_store_clients.py`: the clients produce normalized app
dicts, the LLM analysis pass annotates them with `build_complexity` /
`revenue_potential`, and this module collapses all of it into one
`opportunity_score` in [0, 1] plus the per-signal breakdown that the
agent stores as `rank_signals` (so the operator email can explain WHY an
app ranked where it did).

Four signals, each normalized to [0, 1] and combined by operator-tunable
weights from `config.yaml -> scoring.weights`:

  decline_signal  low rating x many reviews x stale update. The core
                  thesis: a big, unhappy, neglected userbase is a
                  userbase you can take.
  install_base    review-count saturation curve (log-scaled popularity
                  proxy — review counts span 5 orders of magnitude, so a
                  linear scale would let one megahit dominate).
  monetization    IAP / subscription / ad signals mined from the store
                  description + price, i.e. "is there proven willingness
                  to pay in this niche?"
  simplicity      inverse of the LLM's build_complexity (1 trivial ..
                  5 huge) — a cheap build beats a marginally richer one.

Weights need not sum to 1.0; the result is normalized by their total, so
an operator can drop a signal to 0 without rescaling the rest.

This lives in `framework/core/` rather than the agent's own `lib/`
because the repo-root `.gitignore` carries the stock Python `lib/` rule,
which silently excluded the original module from version control — it
was lost when the host was rebuilt from a fresh clone. Keep new
shared-logic modules out of any directory named `lib/`.
"""
from __future__ import annotations

import math
import re
from typing import Any

__all__ = ["opportunity_score", "DEFAULT_WEIGHTS"]


DEFAULT_WEIGHTS: dict[str, float] = {
    "decline_signal": 0.30,
    "install_base": 0.30,
    "monetization": 0.20,
    "simplicity": 0.20,
}

# Review count at which `install_base` saturates toward 1.0. 100k reviews
# is "unambiguously mainstream" on both stores.
_INSTALL_BASE_SATURATION = 100_000

# Days-since-update at which staleness maxes out. 2 years unmaintained is
# as abandoned as it needs to be for the thesis to hold.
_STALE_SATURATION_DAYS = 730

# Ratings at/above this are "healthy" and contribute no decline signal.
_HEALTHY_RATING = 4.5

_REVENUE_POTENTIAL: dict[str, float] = {
    "low": 0.25,
    "medium": 0.50,
    "high": 0.80,
    "very_high": 1.00,
}

# Monetization tells, mined from the store description. Weighted because a
# named subscription tier is far stronger evidence than the word "premium".
_MONETIZATION_PATTERNS: list[tuple[str, float]] = [
    (r"\bsubscription\b|\bsubscribe\b|\bauto-renew", 0.40),
    (r"\bin-app purchase|\bIAP\b|\bunlock\b.{0,20}\bfull\b", 0.30),
    (r"\bpro\b.{0,12}\bversion\b|\bpremium\b|\bplus\b.{0,12}\bplan\b", 0.20),
    (r"\bper month\b|\bper year\b|/mo\b|monthly\b|annually\b|yearly\b", 0.20),
    (r"\bfree trial\b|\btrial\b", 0.15),
    (r"\bads?\b|\badvertis|\bad-free\b|\bremove ads\b", 0.15),
]


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _saturating(value: float, ceiling: float) -> float:
    """Log-saturating curve: 0 at value=0, ~1 at value>=ceiling.

    Log-scaled so a 2k-review app and a 20k-review app are meaningfully
    apart while a 200k and a 2M app are both simply "huge".
    """
    if value <= 0 or ceiling <= 0:
        return 0.0
    return _clamp01(math.log10(1.0 + value) / math.log10(1.0 + ceiling))


def _price_float(price: Any) -> float:
    """`price` is 'free' or a currency-ish string from either store."""
    if price is None:
        return 0.0
    s = str(price).strip().lower()
    if not s or s == "free":
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)", s.replace(",", ""))
    return float(m.group(1)) if m else 0.0


def _decline_signal(app: dict) -> float:
    """Low rating x large userbase x stale update, each in [0, 1].

    Multiplicative on purpose: an app must be unhappy AND used AND
    neglected. A beloved-but-stale app or an unhappy-but-tiny app should
    both score near zero, which an additive blend would not deliver.
    Uses cube-root so a strong showing on two of three factors still
    surfaces rather than being crushed toward 0.
    """
    rating = float(app.get("average_rating") or 0.0)
    reviews = int(app.get("review_count") or 0)
    stale_days = int(app.get("days_since_update") or 0)

    # Unrated apps carry no evidence of decline — don't reward the gap.
    if rating <= 0:
        unhappiness = 0.0
    else:
        unhappiness = _clamp01((_HEALTHY_RATING - rating) / (_HEALTHY_RATING - 1.0))

    reach = _saturating(reviews, _INSTALL_BASE_SATURATION)
    staleness = _clamp01(stale_days / _STALE_SATURATION_DAYS)

    return _clamp01((unhappiness * reach * staleness) ** (1.0 / 3.0))


def _install_base(app: dict) -> float:
    return _saturating(int(app.get("review_count") or 0), _INSTALL_BASE_SATURATION)


def _monetization(app: dict) -> float:
    """Blend the LLM's revenue read with hard evidence from the listing.

    `revenue_potential` is the LLM's judgement and dominates (it saw the
    whole listing); the description/price tells are the deterministic
    floor that keeps the score sane when the LLM pass is unavailable and
    every app defaults to "medium".
    """
    label = str(app.get("revenue_potential") or "").strip().lower()
    llm = _REVENUE_POTENTIAL.get(label, 0.0)

    blob = f"{app.get('description') or ''}".lower()
    evidence = 0.0
    for pattern, weight in _MONETIZATION_PATTERNS:
        if re.search(pattern, blob, re.IGNORECASE):
            evidence += weight
    # A paid app is itself proof of willingness to pay.
    if _price_float(app.get("price")) > 0:
        evidence += 0.30
    evidence = _clamp01(evidence)

    if label in _REVENUE_POTENTIAL:
        return _clamp01(0.65 * llm + 0.35 * evidence)
    return evidence


def _simplicity(app: dict) -> float:
    """Inverse build_complexity: 1 -> 1.0, 5 -> 0.0."""
    try:
        bc = int(app.get("build_complexity") or 3)
    except (TypeError, ValueError):
        bc = 3
    bc = max(1, min(5, bc))
    return (5 - bc) / 4.0


_SIGNALS = {
    "decline_signal": _decline_signal,
    "install_base": _install_base,
    "monetization": _monetization,
    "simplicity": _simplicity,
}


def opportunity_score(
    app: dict,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """Score one analyzed app.

    Args:
        app: normalized app dict from `app_store_clients`, optionally
            annotated by the LLM pass with `build_complexity` (1-5) and
            `revenue_potential` (low|medium|high|very_high).
        weights: per-signal weights, typically from
            `config.yaml -> scoring.weights`. Unknown keys are ignored;
            missing keys fall back to `DEFAULT_WEIGHTS`. Need not sum to
            1.0 — the total is normalized out.

    Returns:
        `(score, parts)` where `score` is in [0, 1] and `parts` maps each
        signal name to its raw [0, 1] value (stored as `rank_signals` so
        the ranking is explainable in the operator email).
    """
    w = dict(DEFAULT_WEIGHTS)
    for key, value in (weights or {}).items():
        if key in _SIGNALS:
            try:
                w[key] = float(value)
            except (TypeError, ValueError):
                continue

    parts = {name: _clamp01(fn(app)) for name, fn in _SIGNALS.items()}

    total_weight = sum(max(0.0, w.get(name, 0.0)) for name in _SIGNALS)
    if total_weight <= 0:
        return 0.0, parts

    score = sum(parts[name] * max(0.0, w.get(name, 0.0)) for name in _SIGNALS)
    return _clamp01(score / total_weight), parts
