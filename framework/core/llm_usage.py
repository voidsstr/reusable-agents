"""LLM call usage tracking — counts + token estimates + cost estimates
for every call made through ai_providers.AiClient.

Why a new module:
  - llm_stream already captures full request/response text per run, but
    it's not aggregated. The dashboard needs "total calls + tokens +
    estimated $ this month per (provider, model)" which would require
    walking every run dir's JSONL each time.
  - Now: every call hooks AiClient.chat() → this module appends one row
    to a monthly partition + bumps a fast aggregate cache.
  - For agents that ran BEFORE this module shipped: backfill_from_streams()
    walks the existing llm-output.jsonl files, counts request/response
    pairs, and builds the same aggregates.

Storage layout (under the framework storage backend):
  framework/llm-usage/<YYYY-MM>.jsonl   — append-only, one row per call
  framework/llm-usage-summary.json      — fast aggregate cache (last 90d)

Each JSONL row:
  {
    "ts":           "2026-05-04T17:30:00+00:00",
    "agent_id":     "specpicks-seo-opportunity-agent",
    "run_ts":       "20260504T173000Z",
    "provider":     "claude-cli",            // friendly provider name
    "kind_provider":"claude-cli",            // backend kind
    "model":        "claude-sonnet-4-6",
    "input_chars":  12500,
    "output_chars": 3400,
    "input_tokens": 3125,                    // est: chars/4
    "output_tokens": 850,                    // est
    "duration_s":   42.3,
    "est_cost_usd": 0.0123,                  // est: tokens * pricing[model][in|out]
    "is_error":     false
  }

CLI:
  python -m framework.core.llm_usage backfill   — walk every llm-output.jsonl
  python -m framework.core.llm_usage summary    — print top providers + costs
  python -m framework.core.llm_usage rebuild-cache
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .storage import StorageBackend, get_storage


# ---------------------------------------------------------------------------
# Pricing table — per 1M tokens (USD)
# ---------------------------------------------------------------------------
# Add models here as we use them. Marginal-zero plans (Claude Max, Copilot
# subscription, local Ollama) priced as zero so totals don't double-bill —
# the user pays a flat subscription instead.
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic API (per-token billing)
    "claude-opus-4-7":    (5.00, 25.00),
    "claude-opus-4-6":    (5.00, 25.00),
    "claude-sonnet-4-6":  (3.00, 15.00),
    "claude-sonnet-4-5":  (3.00, 15.00),
    "claude-haiku-4-5":   (0.80,  4.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    # Azure OpenAI
    "gpt-4o":             (2.50, 10.00),
    "gpt-4o-mini":        (0.15,  0.60),
    "gpt-4-turbo":        (10.00, 30.00),
    "gpt-4":              (30.00, 60.00),
    "o3-mini":            (1.10,  4.40),
    "o4-mini":            (1.10,  4.40),
    # Subscription / local — NO per-call cost
    "_subscription_model_zero": (0.0, 0.0),
}


def _zero_billed_provider(provider: str, kind_provider: str) -> bool:
    """True for providers that are flat-fee / local, so per-call cost is $0."""
    if not provider and not kind_provider:
        return False
    p = (provider or "").lower()
    k = (kind_provider or "").lower()
    return any(x in p for x in ("claude-cli", "claude-max", "copilot", "ollama")) \
        or any(x in k for x in ("claude-cli", "claude-max", "copilot", "ollama"))


def _pricing_for(provider: str, kind_provider: str, model: str) -> tuple[float, float]:
    """Return (input_per_1m, output_per_1m) USD. Returns (0, 0) for
    subscription/local providers — they don't bill per-token even if
    they expose a paid model name like claude-opus-4-7."""
    if _zero_billed_provider(provider, kind_provider):
        return (0.0, 0.0)
    return PRICING.get(model, (0.0, 0.0))


def estimate_tokens(text: Optional[str]) -> int:
    """Rough token estimate: 4 chars per token. Reasonable for English
    prose; over-estimates for code by ~10%, under-estimates for CJK by
    ~3x. Good enough for cost estimates on aggregate."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_cost(input_tokens: int, output_tokens: int,
                  provider: str, kind_provider: str, model: str) -> float:
    """USD estimate. Returns 0 for subscription / local providers."""
    in_per_1m, out_per_1m = _pricing_for(provider, kind_provider, model)
    return round(
        (input_tokens / 1_000_000) * in_per_1m
        + (output_tokens / 1_000_000) * out_per_1m,
        6,
    )


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

def _month_key(ts: str) -> str:
    """Pick a YYYY-MM bucket for a JSONL row."""
    return ts[:7]  # "2026-05-04T..." → "2026-05"


def _usage_jsonl_key(ts_iso: str) -> str:
    return f"framework/llm-usage/{_month_key(ts_iso)}.jsonl"


SUMMARY_KEY = "framework/llm-usage-summary.json"


# ---------------------------------------------------------------------------
# Public API: record_call (called from AiClient.chat() wrapper)
# ---------------------------------------------------------------------------

def record_call(
    *,
    agent_id: str,
    run_ts: str = "",
    provider: str,
    kind_provider: str = "",
    model: str,
    input_text: str = "",
    output_text: str = "",
    duration_s: float = 0.0,
    is_error: bool = False,
    storage: Optional[StorageBackend] = None,
) -> None:
    """Record one LLM call. Called from AiClient.chat() after the
    stream.response() / stream.error() hook so we capture every call
    regardless of provider. Best-effort — never raises into caller."""
    try:
        s = storage or get_storage()
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        in_t = estimate_tokens(input_text)
        out_t = estimate_tokens(output_text)
        cost = estimate_cost(in_t, out_t, provider, kind_provider, model)
        row = {
            "ts": ts,
            "agent_id": agent_id,
            "run_ts": run_ts,
            "provider": provider,
            "kind_provider": kind_provider or provider,
            "model": model,
            "input_chars": len(input_text or ""),
            "output_chars": len(output_text or ""),
            "input_tokens": in_t,
            "output_tokens": out_t,
            "duration_s": round(float(duration_s or 0), 2),
            "est_cost_usd": cost,
            "is_error": bool(is_error),
        }
        s.append_jsonl(_usage_jsonl_key(ts), row)
    except Exception as e:
        # Last-resort: log but don't break the agent.
        print(f"[llm_usage] record_call failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Aggregation — built once and cached
# ---------------------------------------------------------------------------

def aggregate_usage(
    *, days: Optional[int] = None,
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Build the aggregate summary by walking every monthly JSONL.
    If `days` is set, only include rows newer than now-days.

    Returns:
      {
        "as_of": "2026-05-04T18:00:00Z",
        "by_provider_model": [
            {"provider","kind_provider","model",
             "calls","input_tokens","output_tokens","est_cost_usd",
             "agents":[...], "first_ts","last_ts","is_zero_billed"}
        ],
        "by_agent": [
            {"agent_id","calls","input_tokens","output_tokens","est_cost_usd"}
        ],
        "by_day": [
            {"date","calls","est_cost_usd","input_tokens","output_tokens"}
        ],
        "totals": {"calls","input_tokens","output_tokens","est_cost_usd"}
      }
    """
    s = storage or get_storage()
    cutoff_ts = ""
    if days is not None:
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    # Walk every monthly partition
    rows: list[dict] = []
    try:
        keys = list(s.list_prefix("framework/llm-usage/", limit=240))
    except Exception:
        keys = []
    for k in sorted(keys):
        if not k.endswith(".jsonl"):
            continue
        try:
            chunk = s.read_jsonl(k) or []
        except Exception:
            continue
        for r in chunk:
            if cutoff_ts and r.get("ts", "") < cutoff_ts:
                continue
            rows.append(r)

    # Aggregate by (provider, model)
    pm_idx: dict[tuple, dict] = {}
    ag_idx: dict[str, dict] = {}
    day_idx: dict[str, dict] = {}
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0}

    for r in rows:
        prov = r.get("provider", "") or ""
        kp = r.get("kind_provider", "") or prov
        mdl = r.get("model", "") or ""
        ag = r.get("agent_id", "") or ""
        day = (r.get("ts", "") or "")[:10]
        in_t = int(r.get("input_tokens") or 0)
        out_t = int(r.get("output_tokens") or 0)
        cost = float(r.get("est_cost_usd") or 0.0)

        # by provider+model
        key = (prov, kp, mdl)
        ent = pm_idx.setdefault(key, {
            "provider": prov, "kind_provider": kp, "model": mdl,
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "est_cost_usd": 0.0, "agents": set(),
            "first_ts": r.get("ts", ""), "last_ts": r.get("ts", ""),
            "is_zero_billed": _zero_billed_provider(prov, kp),
        })
        ent["calls"] += 1
        ent["input_tokens"] += in_t
        ent["output_tokens"] += out_t
        ent["est_cost_usd"] += cost
        if ag:
            ent["agents"].add(ag)
        if r.get("ts") and r["ts"] < ent["first_ts"]:
            ent["first_ts"] = r["ts"]
        if r.get("ts") and r["ts"] > ent["last_ts"]:
            ent["last_ts"] = r["ts"]

        # by agent
        ae = ag_idx.setdefault(ag, {
            "agent_id": ag, "calls": 0, "input_tokens": 0,
            "output_tokens": 0, "est_cost_usd": 0.0,
        })
        ae["calls"] += 1
        ae["input_tokens"] += in_t
        ae["output_tokens"] += out_t
        ae["est_cost_usd"] += cost

        # by day
        de = day_idx.setdefault(day, {
            "date": day, "calls": 0, "input_tokens": 0,
            "output_tokens": 0, "est_cost_usd": 0.0,
        })
        de["calls"] += 1
        de["input_tokens"] += in_t
        de["output_tokens"] += out_t
        de["est_cost_usd"] += cost

        # totals
        totals["calls"] += 1
        totals["input_tokens"] += in_t
        totals["output_tokens"] += out_t
        totals["est_cost_usd"] += cost

    by_pm = []
    for ent in pm_idx.values():
        ent["agents"] = sorted(ent["agents"])
        ent["est_cost_usd"] = round(ent["est_cost_usd"], 4)
        by_pm.append(ent)
    by_pm.sort(key=lambda x: -x["calls"])

    by_agent = sorted(ag_idx.values(), key=lambda x: -x["calls"])
    for x in by_agent:
        x["est_cost_usd"] = round(x["est_cost_usd"], 4)

    by_day = sorted(day_idx.values(), key=lambda x: x["date"])
    for x in by_day:
        x["est_cost_usd"] = round(x["est_cost_usd"], 4)

    totals["est_cost_usd"] = round(totals["est_cost_usd"], 4)

    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filtered_days": days,
        "by_provider_model": by_pm,
        "by_agent": by_agent,
        "by_day": by_day,
        "totals": totals,
    }


def rebuild_cache(*, storage: Optional[StorageBackend] = None) -> dict:
    """Compute the 90-day summary and write it to SUMMARY_KEY for fast reads."""
    s = storage or get_storage()
    summary = aggregate_usage(days=90, storage=s)
    s.write_json(SUMMARY_KEY, summary)
    return summary


def read_cached_summary(storage: Optional[StorageBackend] = None) -> dict:
    s = storage or get_storage()
    return s.read_json(SUMMARY_KEY) or {"by_provider_model": [], "by_agent": [], "by_day": [], "totals": {}}


# ---------------------------------------------------------------------------
# Backfill from existing llm-output.jsonl
# ---------------------------------------------------------------------------

def backfill_from_streams(
    *, since_days: Optional[int] = None,
    storage: Optional[StorageBackend] = None,
    write_jsonl: bool = True,
) -> dict:
    """Walk every agent's llm-output.jsonl and emit usage records for
    every (request → response) or (request → error) pair found.

    Why pairs: each call is logged as a request, then 0+ chunks, then a
    response (or error). We pair adjacent request+response by their
    (agent_id, run_ts) — there's typically one per file, but if multiple,
    we walk in order.

    Idempotent — keeps a per-source-file cursor in
    framework/llm-usage-backfill-cursor.json so re-runs don't double-count.
    """
    s = storage or get_storage()
    cursor_key = "framework/llm-usage-backfill-cursor.json"
    cursor = s.read_json(cursor_key) or {"processed": []}
    processed = set(cursor.get("processed", []))

    # Find every agent's run dirs
    agents_prefix = "agents/"
    try:
        all_keys = list(s.list_prefix(agents_prefix, limit=20000))
    except Exception as e:
        print(f"[llm_usage] backfill: list failed: {e}", file=sys.stderr)
        return {"backfilled": 0, "skipped": 0, "errors": 0}

    # Filter to llm-output.jsonl files
    stream_keys = [k for k in all_keys if k.endswith("/llm-output.jsonl")]
    print(f"[llm_usage] backfill: {len(stream_keys)} stream files found", file=sys.stderr)

    cutoff = ""
    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(timespec="seconds")

    n_backfilled = 0
    n_skipped_already = 0
    n_errors = 0

    # Buffer for batch writes: month → list[row]
    buffer: dict[str, list[dict]] = {}

    for key in stream_keys:
        if key in processed:
            n_skipped_already += 1
            continue
        try:
            text = s.read_text(key) or ""
        except Exception:
            n_errors += 1
            continue
        # Parse pairs
        pending_request: Optional[dict] = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            kind = r.get("kind", "")
            ts = r.get("ts", "")
            if cutoff and ts < cutoff:
                continue
            if kind == "request":
                pending_request = r
                continue
            if kind in ("response", "error") and pending_request:
                req = pending_request
                pending_request = None
                meta = (req.get("meta") or {})
                resp_meta = (r.get("meta") or {})
                provider = resp_meta.get("provider") or meta.get("provider") or ""
                kp = resp_meta.get("kind_provider") or meta.get("kind_provider") or provider
                model = (resp_meta.get("model") or req.get("model") or "")
                input_text = (req.get("system") or "") + "\n" + (req.get("user") or "")
                output_text = r.get("text") or ""
                in_t = estimate_tokens(input_text)
                out_t = estimate_tokens(output_text)
                cost = estimate_cost(in_t, out_t, provider, kp, model)
                row = {
                    "ts": ts or req.get("ts", ""),
                    "agent_id": r.get("agent_id") or req.get("agent_id", ""),
                    "run_ts": r.get("run_ts") or req.get("run_ts", ""),
                    "provider": provider,
                    "kind_provider": kp,
                    "model": model,
                    "input_chars": len(input_text),
                    "output_chars": len(output_text),
                    "input_tokens": in_t,
                    "output_tokens": out_t,
                    "duration_s": round(float(r.get("duration_s") or r.get("elapsed_s") or 0), 2),
                    "est_cost_usd": cost,
                    "is_error": kind == "error",
                    "_backfill_source": key,
                }
                month = _month_key(row["ts"]) or _month_key(datetime.now(timezone.utc).isoformat())
                buffer.setdefault(month, []).append(row)
                n_backfilled += 1
        processed.add(key)

    # Write all rows in batched per-month appends
    if write_jsonl:
        for month, rows in buffer.items():
            try:
                k = f"framework/llm-usage/{month}.jsonl"
                # append in one call
                for row in rows:
                    s.append_jsonl(k, row)
            except Exception as e:
                print(f"[llm_usage] write {month} failed: {e}", file=sys.stderr)
                n_errors += len(rows)

    # Persist cursor
    try:
        s.write_json(cursor_key, {"processed": sorted(processed),
                                   "last_backfill_ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    except Exception:
        pass

    return {
        "backfilled": n_backfilled,
        "skipped_already_done": n_skipped_already,
        "errors": n_errors,
        "stream_files_total": len(stream_keys),
        "months_written": list(buffer.keys()),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    p = argparse.ArgumentParser(prog="llm_usage")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("backfill", help="walk every llm-output.jsonl and build usage rows")
    bf = sub.add_parser("backfill-since", help="backfill only rows newer than --days days")
    bf.add_argument("--days", type=int, default=90)
    sub.add_parser("rebuild-cache", help="recompute framework/llm-usage-summary.json")
    s = sub.add_parser("summary", help="print top-line totals")
    s.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    if args.cmd == "backfill":
        out = backfill_from_streams()
    elif args.cmd == "backfill-since":
        out = backfill_from_streams(since_days=args.days)
    elif args.cmd == "rebuild-cache":
        out = rebuild_cache()
    elif args.cmd == "summary":
        out = aggregate_usage(days=args.days)
    print(json.dumps(out, indent=2, default=str)[:4000])


if __name__ == "__main__":
    _main()
