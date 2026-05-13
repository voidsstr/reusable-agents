"""Reusable short-circuit primitives for agents that fire on a cron but
shouldn't actually do LLM work when their inputs haven't changed.

Three idioms covered:

  1. **Signal-hash short-circuit** — for batch/proposal agents (article
     authors, content researchers, etc.) where the LLM call is driven by
     a set of input "signals" (DB queries, GSC results, queue states). If
     the signal vector hashes the same as last run, skip the LLM.

       sig = signal_hash({"queue": rows, "queries": top_queries})
       if should_skip(self.state, "last_signal_hash", sig):
           return RunResult(...)
       ...
       return RunResult(next_state={"last_signal_hash": sig, ...})

  2. **Snapshot-hash short-circuit** — for analyzer-style agents that
     build a deterministic "snapshot" of their inputs and produce
     recommendations from it. Same idea, different name (since the input
     is a structured snapshot rather than a flat signals dict).

       sig = snapshot_hash(snap_dict)
       if signatures_match(prior_snap, snap_dict):
           replay_prior_recs()
           return

  3. **Per-item-hash short-circuit** — for crawl/audit agents that
     analyze N items per run. Maintain a `{key: hash}` map of "seen"
     items in carried state; skip items whose hash hasn't changed AND
     replay their prior findings (kept in a parallel `{hash: findings}`
     map) so the recommendation pipeline still sees them.

       fresh, cached = partition_by_hash(items, prior_hashes, key=...)
       # ... LLM only fresh ...
       new_state = update_item_hash_cache(...)

All hashing routes through `stable_hash` which excludes per-run timestamp
fields and rounds floats so floating-point flap doesn't bust the cache.

Usage from an agent:
    from framework.core.short_circuit import (
        stable_hash, signal_hash, should_skip,
        partition_by_hash, merge_findings_cache,
    )
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Optional


# Field names that are timestamp-ish or run-specific — excluded from any
# hash so they don't bust the cache on every run.
DEFAULT_EXCLUDE_KEYS: frozenset[str] = frozenset({
    "as_of", "fetched_at", "generated_at", "run_ts", "snapshot_at",
    "ts", "timestamp", "schema_version", "updated_at", "created_at",
    "started_at", "ended_at", "fired_at", "triggered_at",
    "run_id", "request_id", "iteration",
    "captured_at", "collected_at", "pulled_at", "synced_at",
})


def stable_hash(
    obj: Any,
    *,
    exclude_keys: Iterable[str] = DEFAULT_EXCLUDE_KEYS,
    float_precision: int = 4,
    digest_chars: int = 24,
) -> str:
    """Deterministic SHA-256 of an object's MATERIAL content.

    Two objects with the same `stable_hash` are functionally equivalent
    inputs to a downstream LLM call. Excludes per-run timestamp/version
    fields, rounds floats to `float_precision` decimals (so floating-point
    flap doesn't break the cache), and serializes with sorted keys.

    Returns a 24-char hex prefix by default — enough collision resistance
    for ~16M agents at <1% birthday-collision probability.
    """
    excluded = frozenset(exclude_keys or ())

    def _normalize(o: Any) -> Any:
        if isinstance(o, dict):
            return {
                k: _normalize(v)
                for k, v in o.items()
                if k not in excluded
            }
        if isinstance(o, (list, tuple)):
            return [_normalize(x) for x in o]
        if isinstance(o, set):
            # Sets serialize unstably — sort
            return sorted(_normalize(x) for x in o)
        if isinstance(o, float):
            return round(o, float_precision)
        return o

    norm = _normalize(obj)
    blob = json.dumps(norm, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:digest_chars]


# Type-named convenience aliases — readers grok intent at the call site.
def signal_hash(signals: Any, **kwargs) -> str:
    """Hash an agent's input-signal bundle (e.g. {gsc, queue, recent})."""
    return stable_hash(signals, **kwargs)


def snapshot_hash(snap: Any, **kwargs) -> str:
    """Hash an analyzer's snapshot (e.g. SEO analyzer's snapshot.json)."""
    return stable_hash(snap, **kwargs)


# Per-field quantization rules for `lossy_snapshot_hash`. Anything matching
# one of these key suffixes (or substrings, marked) gets rounded into wide
# buckets so insignificant real-world drift (GSC delivering one extra
# impression, GA4 reporting one more session in a 28-day window) doesn't
# bust the cache.
_QUANTIZE_RULES: tuple[tuple[tuple[str, ...], int], ...] = (
    # (suffixes_matched_against_dict_keys, bucket_size). Larger buckets
    # for cardinality counters (num_queries goes 3841→3894 across two
    # ticks 4h apart) since 100 was too tight on the boundary.
    (("num_queries", "num_pages_indexed", "num_pages",
      "queries", "pages_indexed", "pages"), 250),
    (("impressions", "page_views", "screenpageviews", "screenPageViews",
      "newusers", "totalusers", "sessions", "users",
      "pos21_50", "pos51plus", "pos1_3", "pos4_10", "pos11_20"), 100),
    (("clicks", "conversions", "purchases"), 5),
    (("count", "events"), 25),
)
# Position metrics rounded to nearest INTEGER — sub-1.0 position drift
# (rank 64.1 → 64.4) doesn't change recommendations. CTRs to 3 decimals.
_POSITION_KEYS: frozenset[str] = frozenset({"position", "avg_position",
                                            "average_position"})
# Duration / latency keys — round to nearest second.
_DURATION_KEYS: frozenset[str] = frozenset({"averagesessionduration",
                                            "avgsessionduration",
                                            "session_duration",
                                            "duration_seconds"})
_CTR_KEYS: frozenset[str] = frozenset({"ctr", "click_through_rate"})
# Drop per-page / per-query map entries below this impression floor — new
# low-traffic discoveries appear constantly on a busy GSC property and
# don't materially change recommendations.
_LOW_TRAFFIC_FLOOR_IMPRESSIONS: int = 10


def _coerce_number(value: Any) -> Optional[float]:
    """GA4 + some GSC payloads ship metrics as strings ('59847'). Coerce
    safely to float for quantization."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _quantize_value(key: str, value: Any) -> Any:
    """Round numeric metrics into wide buckets keyed by field name."""
    num = _coerce_number(value)
    if num is None:
        return value
    key_l = key.lower()
    for suffix_set, bucket in _QUANTIZE_RULES:
        if any(key_l.endswith(s) or key_l == s for s in suffix_set):
            return int(round(num / bucket) * bucket)
    if any(key_l == s or key_l.endswith("." + s) for s in _POSITION_KEYS):
        return int(round(num))
    if key_l in _DURATION_KEYS:
        return int(round(num))
    if any(key_l == s or key_l.endswith("." + s) for s in _CTR_KEYS):
        return round(num, 3)
    # Original was string and not a quantize-target — preserve original
    # so we don't accidentally string-vs-float compare across runs.
    if isinstance(value, str):
        return value
    return value


_METRIC_MAP_FIELDS: frozenset[str] = frozenset({
    "impressions", "position", "clicks", "ctr",
    "page_views", "sessions",
})


def _looks_like_metric_map(node: dict) -> bool:
    """Detect a per-URL or per-query metric map (keys are URLs/queries,
    values are dicts with impressions/clicks/position fields). The
    low-traffic floor in `_quantize_node` then drops entries that won't
    materially affect downstream recommendations.
    """
    if len(node) < 5:
        return False
    sample_keys = list(node.keys())[:10]
    sample_vals = list(node.values())[:10]
    if not all(isinstance(k, str) for k in sample_keys):
        return False
    # Need MOST values (not all — guards against one bad entry) to be
    # dicts with at least one metric field. URLs and queries both qualify.
    metric_dict_count = sum(
        1 for v in sample_vals
        if isinstance(v, dict)
        and any(f in v for f in _METRIC_MAP_FIELDS)
    )
    return metric_dict_count >= max(3, int(len(sample_vals) * 0.6))


# Per-entry fields to STRIP from metric-map values (per-URL, per-query
# maps). Per-page rank position drifts ±0.5 every tick on active sites
# but doesn't change which page should be the rank target — that's
# captured in aggregate avg_position. CTR per-entry is similarly noisy.
_METRIC_MAP_DROP_FIELDS: frozenset[str] = frozenset({"position", "ctr"})


def _quantize_node(node: Any, parent_key: str = "") -> Any:
    """Recurse through a snapshot, quantizing leaf metrics + dropping
    low-traffic per-page/per-query map entries + stripping per-entry
    fields that drift every tick without changing recommendations."""
    if isinstance(node, dict):
        is_metric_map = _looks_like_metric_map(node)
        out = {}
        for k, v in node.items():
            if is_metric_map and isinstance(v, dict):
                impr_raw = v.get("impressions") or v.get("page_views") or 0
                impr = _coerce_number(impr_raw) or 0
                if impr < _LOW_TRAFFIC_FLOOR_IMPRESSIONS:
                    continue
                # Strip noisy per-entry fields BEFORE quantizing the
                # remaining metrics. Aggregate equivalents survive.
                v = {kk: vv for kk, vv in v.items()
                     if kk not in _METRIC_MAP_DROP_FIELDS}
            if isinstance(v, (dict, list)):
                out[k] = _quantize_node(v, parent_key=k)
            else:
                out[k] = _quantize_value(k, v)
        return out
    if isinstance(node, list):
        return [_quantize_node(x, parent_key=parent_key) for x in node]
    return _quantize_value(parent_key, node)


def too_soon_to_rerun(
    prior_run_iso: Optional[str],
    *,
    min_interval_hours: float = 6.0,
    now_iso: Optional[str] = None,
) -> tuple[bool, float]:
    """Time-based short-circuit gate.

    Some agents fire on a 2h cron but their upstream data (GSC, GA4,
    DB-stats) refreshes ~daily. Re-analyzing every 2h is wasted LLM
    cost. This helper returns (True, hours_since_last) when the prior
    successful run was less than `min_interval_hours` ago — caller
    short-circuits and replays prior output.

    Returns (False, 0) when prior_run_iso is missing/unparseable so a
    failed timestamp parse never blocks legitimate runs.
    """
    from datetime import datetime, timezone
    if not prior_run_iso:
        return False, 0.0
    try:
        if prior_run_iso.endswith("Z"):
            prior = datetime.fromisoformat(prior_run_iso[:-1]).replace(
                tzinfo=timezone.utc)
        else:
            prior = datetime.fromisoformat(prior_run_iso)
            if prior.tzinfo is None:
                prior = prior.replace(tzinfo=timezone.utc)
    except Exception:
        return False, 0.0
    if now_iso:
        try:
            now = (datetime.fromisoformat(now_iso[:-1]).replace(
                tzinfo=timezone.utc) if now_iso.endswith("Z")
                else datetime.fromisoformat(now_iso))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except Exception:
            now = datetime.now(timezone.utc)
    else:
        now = datetime.now(timezone.utc)
    delta_h = (now - prior).total_seconds() / 3600.0
    return (delta_h < min_interval_hours), delta_h


def lossy_snapshot_hash(snap: Any, **kwargs) -> str:
    """Hash a snapshot AFTER quantizing GSC/GA4-style metrics into wide
    buckets and dropping low-traffic per-URL entries.

    Use this when the snapshot's downstream LLM audit would produce
    materially-identical recommendations even though raw metrics drift
    every tick (1 extra session, 5 extra impressions, etc.). Aggressive
    enough that two runs ~hours apart on a quiet site hash identically;
    sensitive enough that a real content/structure change still busts
    it (new high-traffic page appears, total impressions jumps 10%+).

    Delegates to `stable_hash` after quantization so the same exclude_keys
    / float_precision / digest_chars knobs apply.
    """
    return stable_hash(_quantize_node(snap), **kwargs)


def should_skip(
    state: Optional[dict],
    state_key: str,
    current_hash: str,
) -> bool:
    """Return True if `current_hash` matches `state[state_key]` — meaning
    the agent has already run with this exact input and its prior output
    is still applicable.

    Returns False if state is None, the key is missing, or hashes differ.
    Always False when `current_hash` is empty (defensive — never
    short-circuit on a hash failure).
    """
    if not current_hash:
        return False
    if not isinstance(state, dict):
        return False
    return state.get(state_key) == current_hash


# ---------------------------------------------------------------------------
# Per-item short-circuit (for crawl/audit agents)
# ---------------------------------------------------------------------------

def partition_by_hash(
    items: list,
    prior_hashes: dict[str, str],
    *,
    key_fn,
    hash_fn,
    revisit_counter: Optional[dict[str, int]] = None,
    revisit_after_runs: int = 12,
) -> tuple[list, list, dict[str, int]]:
    """Split `items` into (fresh, cached) based on hash equality with the
    prior run, and return an updated revisit-counter.

    Args:
      items: the things to analyze (pages, products, hardware rows, etc.)
      prior_hashes: {key: hash} from carried state
      key_fn: function(item) → str  — stable identity (e.g. URL, product id)
      hash_fn: function(item) → str — content hash that detects material change
      revisit_counter: optional {key: int} from carried state — how many
        consecutive runs we've short-circuited this item. Forces a re-analyze
        every `revisit_after_runs` ticks even if the hash is unchanged.
      revisit_after_runs: ticks between forced revisits (default 12 ≈ 24h
        at 2h cadence)

    Returns: (fresh_items, cached_items, new_revisit_counter)
    """
    revisit_counter = revisit_counter or {}
    fresh: list = []
    cached: list = []
    new_counter: dict[str, int] = {}
    for it in items:
        try:
            k = key_fn(it)
            h = hash_fn(it)
        except Exception:
            fresh.append(it)
            continue
        if not k or not h:
            fresh.append(it)
            continue
        seen_count = int(revisit_counter.get(k, 0))
        if prior_hashes.get(k) == h and seen_count < revisit_after_runs:
            cached.append(it)
            new_counter[k] = seen_count + 1
        else:
            fresh.append(it)
            new_counter[k] = 0  # reset on fresh analysis
    return fresh, cached, new_counter


def merge_findings_cache(
    prior: dict[str, list[dict]],
    fresh: dict[str, list[dict]],
    active_hashes: dict[str, str],
) -> dict[str, list[dict]]:
    """Combine the prior-run findings cache with this run's fresh
    findings, dropping entries for hashes no longer present in the
    crawl (`active_hashes` values). Caps the cache from growing
    unboundedly across thousands of runs.
    """
    active = set(active_hashes.values())
    merged: dict[str, list[dict]] = {
        h: list(rows) for h, rows in (prior or {}).items()
        if h in active and isinstance(rows, list)
    }
    for h, rows in (fresh or {}).items():
        if not h or not isinstance(rows, list):
            continue
        merged[h] = list(rows)  # fresh wins on conflict
    return merged
