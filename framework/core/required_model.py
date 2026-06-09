"""Required-model policy — declares which Claude model is REQUIRED for a
rec, with NO fallback when that model is rate-limited.

Distinguishes from `implementer_safety.recommended_model_tier`:
  - `recommended_tier` — soft hint. Implementer starts there but falls
    through to sonnet/haiku if the preferred tier is rate-limited or
    times out. Used for cost-optimizing dispatch.
  - `required_tier` — HARD requirement. If the model is unavailable, the
    implementer DEFERS the rec rather than fall back. Used when quality
    matters more than throughput (e.g. article-author should produce
    opus-quality content; sonnet-quality is worse than waiting).

Resolution order (first match wins):
  1. rec.required_model_tier — per-rec explicit override
  2. storage config/required-models.json by_dispatch_kind[<kind>]
  3. storage config/required-models.json by_agent_id[<source_agent>]
  4. None — no requirement, fall through to recommended_tier defaults

Config shape (config/required-models.json in storage):
    {
      "_doc": "Hard model requirements per agent / dispatch_kind",
      "by_dispatch_kind": {
        "article-author": "opus"
      },
      "by_agent_id": {
        "specpicks-article-proposal-agent": "opus",
        "aisleprompt-article-proposal-agent": "opus"
      }
    }

Tier values match `implementer_safety.MODEL_TIER_*`:
  "opus" | "sonnet" | "haiku"
"""
from __future__ import annotations

from typing import Any, Optional

# Reuse the tier-to-model mapping from implementer_safety so it stays
# consistent with the recommended-tier system.
try:
    from .implementer_safety import MODEL_FOR_TIER, MODEL_TIER_OPUS, MODEL_TIER_SONNET, MODEL_TIER_HAIKU
    VALID_TIERS = (MODEL_TIER_OPUS, MODEL_TIER_SONNET, MODEL_TIER_HAIKU)
except Exception:  # pragma: no cover
    MODEL_FOR_TIER = {"opus": "claude-opus-4-7",
                      "sonnet": "claude-sonnet-4-6",
                      "haiku": "claude-haiku-4-5"}
    VALID_TIERS = ("opus", "sonnet", "haiku")

CONFIG_KEY = "config/required-models.json"


def _normalize_tier(t: Optional[str]) -> Optional[str]:
    """Accept 'opus' / 'claude-opus-4-7' / variants. Returns canonical
    tier string or None if unrecognized."""
    if not t:
        return None
    t = str(t).strip().lower()
    if t in VALID_TIERS:
        return t
    # Allow specifying the model id directly
    for tier, model in MODEL_FOR_TIER.items():
        if t == model.lower():
            return tier
    # Accept partial matches like "claude-opus" → "opus"
    if "opus" in t:
        return "opus"
    if "sonnet" in t:
        return "sonnet"
    if "haiku" in t:
        return "haiku"
    return None


def required_tier_for_rec(
    rec: dict,
    *,
    dispatch_kind: str = "",
    source_agent_id: str = "",
    storage: Any = None,
) -> Optional[str]:
    """Return the required tier for this rec, or None if no requirement.

    See module docstring for resolution order.
    """
    # Priority 1: per-rec explicit
    if isinstance(rec, dict):
        t = _normalize_tier(rec.get("required_model_tier")
                            or rec.get("required_model"))
        if t:
            return t

    # Priority 2/3: storage config
    cfg = _load_config(storage)
    if cfg:
        kind_map = cfg.get("by_dispatch_kind") or {}
        if dispatch_kind:
            t = _normalize_tier(kind_map.get(dispatch_kind))
            if t:
                return t
        agent_map = cfg.get("by_agent_id") or {}
        if source_agent_id:
            t = _normalize_tier(agent_map.get(source_agent_id))
            if t:
                return t

    return None


def required_model_for_batch(
    recs: list[dict],
    *,
    dispatch_kind: str = "",
    source_agent_id: str = "",
    storage: Any = None,
) -> Optional[tuple[str, str]]:
    """For a multi-rec batch, return (tier, model_id) if a required tier
    applies to ANY rec in the batch — picks the most-expensive required
    tier (since the implementer runs one LLM session over the batch).

    Returns None if no rec has a required tier (caller falls back to
    `implementer_safety.recommended_model_for_batch`).
    """
    if not recs:
        return None
    rank = {"opus": 2, "sonnet": 1, "haiku": 0}
    best: Optional[str] = None
    for r in recs:
        t = required_tier_for_rec(
            r, dispatch_kind=dispatch_kind,
            source_agent_id=source_agent_id, storage=storage,
        )
        if t and (best is None or rank.get(t, -1) > rank.get(best, -1)):
            best = t
    if best is None:
        return None
    return best, MODEL_FOR_TIER[best]


def _load_config(storage: Any) -> Optional[dict]:
    """Read config/required-models.json from storage. Returns None if
    storage is unset or the config doesn't exist."""
    if storage is None:
        try:
            from .storage import get_storage
            storage = get_storage()
        except Exception:
            return None
    try:
        return storage.read_json(CONFIG_KEY)
    except Exception:
        return None


def enforce_writer_model(*, agent_id: str = "", dispatch_kind: str = "",
                          storage: Any = None,
                          fallback_model: str = "claude-opus-4-7") -> str:
    """Return the model id required for a writer (article/news) agent.

    One-call helper for ad-hoc scripts that produce article-shaped
    output and want the same opus-only policy the implementer obeys.

    Returns the canonical model id (e.g. 'claude-opus-4-7'). If neither
    `agent_id` nor `dispatch_kind` resolves to a required tier, returns
    `fallback_model` so callers always have a model to invoke.

    Usage:
        from framework.core.required_model import enforce_writer_model
        model = enforce_writer_model(dispatch_kind="news-rewrite")
        # → "claude-opus-4-7"

    Raises ValueError ONLY when a config entry resolves to an invalid
    tier — never silently downgrades.
    """
    tier = None
    cfg = _load_config(storage)
    if cfg:
        kind_map = cfg.get("by_dispatch_kind") or {}
        if dispatch_kind:
            tier = _normalize_tier(kind_map.get(dispatch_kind))
        if not tier:
            agent_map = cfg.get("by_agent_id") or {}
            tier = _normalize_tier(agent_map.get(agent_id))
    if not tier:
        # No explicit requirement → fall back to the caller's default
        # (we'd rather honor the script's intent than guess wrong).
        return fallback_model
    if tier not in MODEL_FOR_TIER:
        raise ValueError(f"required_model config: unknown tier '{tier}' for agent={agent_id} kind={dispatch_kind}")
    return MODEL_FOR_TIER[tier]


__all__ = [
    "required_tier_for_rec",
    "required_model_for_batch",
    "enforce_writer_model",
    "MODEL_FOR_TIER",
    "CONFIG_KEY",
]
