"""LLM usage aggregation API.

GET /api/llm/usage           — full aggregate (cached + fresh blend)
GET /api/llm/usage?days=30   — limit window
GET /api/llm/usage/refresh   — force rebuild_cache then return it
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from framework.core import llm_usage

from ..auth import require_token


router = APIRouter(prefix="/api/llm", tags=["llm-usage"], dependencies=[Depends(require_token)])


@router.get("/usage")
def get_usage(days: Optional[int] = Query(default=None, ge=1, le=365)):
    """Aggregate LLM call counts + token + cost estimates.

    Default: read the cached 90-day summary (fast — ONE blob read).
    With `?days=N`: re-aggregate from the JSONL partitions for that
    window. Slower but accurate for arbitrary windows.
    """
    if days is None:
        return llm_usage.read_cached_summary()
    return llm_usage.aggregate_usage(days=days)


@router.post("/usage/refresh")
def refresh():
    """Force a cache rebuild from JSONL partitions."""
    return llm_usage.rebuild_cache()


@router.post("/usage/backfill")
def backfill(since_days: Optional[int] = Query(default=None, ge=1, le=365)):
    """Walk every llm-output.jsonl in storage and emit usage rows.
    Idempotent — keeps a per-source-file cursor."""
    out = llm_usage.backfill_from_streams(since_days=since_days)
    # Update cache after backfill
    llm_usage.rebuild_cache()
    return out
