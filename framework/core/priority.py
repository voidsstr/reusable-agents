"""Framework-level agent priority tiers.

Used everywhere the framework needs to ask "of these N agents/dispatches,
which should run FIRST?" — host-worker queue scan, responder
auto-queue drain, dashboard display order.

The tier-to-agent mapping is **deployment-configurable**, not hardcoded.
Each instance of the reusable-agents framework decides its own
priorities by writing/editing `config/priority-config.json` in framework
storage. Per-agent overrides may be set in the agent's manifest via
`priority_tier`.

Tier resolution order (lowest = highest priority):

  1. `manifest.priority_tier` (explicit, highest authority)
  2. First matching pattern in the storage config's tiers
  3. `default_tier` from the storage config
  4. Hard-coded fallback `DEFAULT_TIER` (5)

Patterns may be:
  - exact agent_id      — `"agent-doctor"`
  - prefix glob         — `"aisleprompt-*"`  (matches anything starting with)
  - suffix glob         — `"*-seo-opportunity-agent"`
  - mid-string glob     — `"specpicks-*-agent"`
  - wildcard `"*"`       — matches every agent (only useful as a
                            catch-all in default_tier section)

Lower tier numbers run first. By convention:
  1 = critical revenue / ranking drivers
  2 = high-priority content
  3 = secondary content
  4 = research / hygiene
  5 = ops / housekeeping
  6+ = anything we never want to block higher-priority work

Example config:
    {
      "schema_version": "1",
      "default_tier": 5,
      "tiers": {
        "1": {
          "label": "SEO + ranking signals",
          "agents": ["*-seo-opportunity-agent",
                     "*-progressive-improvement-agent",
                     "*-competitor-research-agent",
                     "seo-implementer", "seo-analyzer"]
        },
        "2": {
          "label": "AislePrompt content",
          "agents": ["aisleprompt-article-proposal-agent",
                     "aisleprompt-head-to-head-agent"]
        },
        "3": {
          "label": "SpecPicks content",
          "agents": ["specpicks-article-proposal-agent",
                     "specpicks-head-to-head-agent"]
        },
        ...
      }
    }
"""
from __future__ import annotations

import fnmatch
from typing import Optional

from .storage import StorageBackend, get_storage


CONFIG_KEY = "config/priority-config.json"
DEFAULT_TIER = 5


# Sensible default config for fresh installs (matches the user's spec):
#   T1 = SEO + ranking
#   T2 = AislePrompt content
#   T3 = SpecPicks content
#   T4 = research / catalog hygiene
#   T5 = ops / housekeeping
DEFAULT_CONFIG: dict = {
    "schema_version": "1",
    "default_tier": 5,
    "tiers": {
        "1": {
            "label": "SEO + ranking signals (highest impact)",
            "agents": [
                "*-seo-opportunity-agent",
                "seo-opportunity-agent",
                "*-progressive-improvement-agent",
                "progressive-improvement-agent",
                "*-competitor-research-agent",
                "competitor-research-agent",
                "seo-implementer",
                "seo-analyzer",
            ],
        },
        "2": {
            "label": "AislePrompt content production",
            "agents": [
                "aisleprompt-article-proposal-agent",
                "aisleprompt-head-to-head-agent",
            ],
        },
        "3": {
            "label": "SpecPicks content production",
            "agents": [
                "specpicks-article-proposal-agent",
                "specpicks-head-to-head-agent",
            ],
        },
        "4": {
            "label": "Research / catalog hygiene",
            "agents": [
                "*-catalog-audit-agent",
                "*-product-hydration-agent",
                "*-benchmark-research-agent",
                "*-ebay-product-sync-agent",
                "*-user-growth-strategist",
                "*-kitchen-scraper",
            ],
        },
        "5": {
            "label": "Ops / housekeeping",
            "agents": [
                "agent-doctor",
                "digest-rollup-agent",
                "responder-agent",
                "indexnow-submitter",
                "*-scraper-watchdog",
            ],
        },
    },
}


def load_priority_config(storage: Optional[StorageBackend] = None) -> dict:
    """Read priority config from framework storage. Returns DEFAULT_CONFIG
    on first run / missing key. Caller may pass storage=... for tests."""
    s = storage or get_storage()
    try:
        cfg = s.read_json(CONFIG_KEY)
    except Exception:
        cfg = None
    if not isinstance(cfg, dict):
        return dict(DEFAULT_CONFIG)
    cfg.setdefault("default_tier", DEFAULT_TIER)
    cfg.setdefault("tiers", {})
    return cfg


def write_priority_config(cfg: dict, storage: Optional[StorageBackend] = None) -> None:
    """Persist a priority config. Validates basic shape but doesn't
    enforce semantic constraints — the caller (dashboard / cron / hand
    edit) is responsible for sensible tier ordering."""
    if not isinstance(cfg, dict):
        raise ValueError("priority config must be a dict")
    cfg.setdefault("schema_version", "1")
    cfg.setdefault("default_tier", DEFAULT_TIER)
    cfg.setdefault("tiers", {})
    s = storage or get_storage()
    s.write_json(CONFIG_KEY, cfg)


def ensure_default_config_exists(storage: Optional[StorageBackend] = None) -> bool:
    """Write the default config IFF the storage key is missing. Returns
    True if a write occurred. Idempotent — safe to call on every boot."""
    s = storage or get_storage()
    try:
        existing = s.read_json(CONFIG_KEY)
        if isinstance(existing, dict) and existing.get("tiers"):
            return False
    except Exception:
        pass
    s.write_json(CONFIG_KEY, DEFAULT_CONFIG)
    return True


def tier_for_agent(
    agent_id: str,
    *,
    manifest: Optional[dict] = None,
    config: Optional[dict] = None,
    storage: Optional[StorageBackend] = None,
    read_manifest_from_storage: bool = True,
) -> int:
    """Return the priority tier (lower = run first) for one agent.

    Args:
      agent_id: kebab-case agent id (e.g. "specpicks-seo-opportunity-agent").
      manifest: optional pre-loaded manifest dict; if it has
                `priority_tier` set (>0), that wins.
      config: pre-loaded priority config; reads from storage if omitted.
      storage: pass-through for tests.
      read_manifest_from_storage: when True (default) AND `manifest` is
                None, fall back to reading the agent's manifest from
                storage so the manifest's `priority_tier` override is
                honoured even when the caller didn't pre-load it. Set
                False in hot paths to skip the round-trip.

    Resolution order: manifest → first matching pattern in config →
    config.default_tier → DEFAULT_TIER (5).
    """
    if not agent_id:
        return DEFAULT_TIER

    # 1. manifest override (caller-passed first, storage fallback second)
    if isinstance(manifest, dict):
        mt = manifest.get("priority_tier")
        if isinstance(mt, int) and mt > 0:
            return mt
    elif read_manifest_from_storage:
        try:
            s = storage or get_storage()
            man = s.read_json(f"agents/{agent_id}/manifest.json")
            if isinstance(man, dict):
                mt = man.get("priority_tier")
                if isinstance(mt, int) and mt > 0:
                    return mt
        except Exception:
            pass  # storage may be down — fall through to config patterns

    cfg = config or load_priority_config(storage=storage)
    tiers = cfg.get("tiers") or {}

    # 2. pattern match — walk tiers in NUMERIC order so an agent matching
    #    multiple patterns gets the LOWEST (most-urgent) tier.
    try:
        sorted_keys = sorted(tiers.keys(), key=lambda k: int(k))
    except Exception:
        sorted_keys = list(tiers.keys())
    for k in sorted_keys:
        try:
            tier = int(k)
        except Exception:
            continue
        block = tiers[k]
        if not isinstance(block, dict):
            continue
        for pat in (block.get("agents") or []):
            if not isinstance(pat, str) or not pat:
                continue
            if pat == agent_id or fnmatch.fnmatchcase(agent_id, pat):
                return tier

    # 3. default
    try:
        return int(cfg.get("default_tier", DEFAULT_TIER))
    except Exception:
        return DEFAULT_TIER


def sort_by_tier(
    items: list,
    *,
    agent_id_fn,
    secondary_fn=None,
    storage: Optional[StorageBackend] = None,
) -> list:
    """Generic helper: return items sorted by (tier, secondary_key).

    Args:
      items: list of arbitrary objects (dicts, file paths, dispatch envelopes).
      agent_id_fn: callable(item) → agent_id string.
      secondary_fn: optional callable(item) → secondary sort key (e.g. mtime,
                    run_ts) so items in the same tier go FIFO. Defaults to "".

    Loads priority config once and re-uses it across all items.
    """
    cfg = load_priority_config(storage=storage)
    secondary_fn = secondary_fn or (lambda _: "")
    return sorted(
        items,
        key=lambda it: (tier_for_agent(agent_id_fn(it), config=cfg),
                        secondary_fn(it)),
    )


# ── Pool-aware tier skip (2026-06-09) ─────────────────────────────────────
# When the claude-pool has no opus headroom (every authenticated profile
# is rate-limited until later than NOW + some grace window), any drained
# rec that REQUIRES opus is going to immediately defer. To stop opus-
# required recs from blocking non-opus T4/T5 work behind them in the
# tier sort, the drainer can call `effective_tier_with_pool_pressure()`
# which DEMOTES opus-required recs to tier 9 when opus is unreachable.
#
# Schedule:
#   - When opus IS reachable in <15min: no demotion (normal priority)
#   - When opus next-reset is >15min away: demote opus-required to tier 9
#
# This lets SEO + non-opus content recs ship continuously while opus is
# exhausted, instead of having T2/T3 article recs sit at the head of
# the queue blocking everything.

POOL_OPUS_GRACE_S = 900  # 15 min — how soon opus needs to be back to not demote


def _pool_opus_reachable_within(grace_s: int = POOL_OPUS_GRACE_S) -> bool:
    """True iff at least one authenticated claude-pool profile has opus
    available NOW or within `grace_s` seconds. Defensive — returns True
    on any read error so we don't accidentally demote when state.json
    is just missing or transiently locked.
    """
    import os
    from datetime import datetime, timezone
    try:
        # State path defaults to ~/.reusable-agents/claude-pool/state.json
        # (see framework.cli.claude_pool.ROOT). The drainer runs on the
        # host that owns the pool, so this file is readable.
        path = os.environ.get(
            "CLAUDE_POOL_ROOT",
            os.path.expanduser("~/.reusable-agents/claude-pool"),
        )
        state_file = os.path.join(path, "state.json")
        if not os.path.exists(state_file):
            return True
        import json as _j
        with open(state_file) as f:
            state = _j.load(f)
        now = datetime.now(timezone.utc)
        for pid, info in state.items():
            if pid.startswith("__") or not isinstance(info, dict):
                continue
            if not info.get("authenticated"):
                continue
            limits = info.get("limit_resets_at") or {}
            opus_reset = limits.get("opus", "")
            if not opus_reset:
                # No recorded opus limit → assume reachable
                return True
            try:
                t = datetime.fromisoformat(opus_reset)
            except Exception:
                return True
            if (t - now).total_seconds() <= grace_s:
                return True
        return False
    except Exception:
        return True


# Tier number a demoted opus-required rec gets relegated to. Picked larger
# than any default tier so it sorts behind everything else.
OPUS_DEMOTED_TIER = 9


def effective_tier_with_pool_pressure(
    base_tier: int,
    required_model: str = "",
    *,
    grace_s: int = POOL_OPUS_GRACE_S,
) -> int:
    """If the rec needs opus and opus isn't coming back within `grace_s`,
    demote to OPUS_DEMOTED_TIER. Otherwise return base_tier unchanged.
    """
    if not required_model:
        return base_tier
    rm = required_model.lower()
    needs_opus = "opus" in rm
    if not needs_opus:
        return base_tier
    if _pool_opus_reachable_within(grace_s):
        return base_tier
    return max(base_tier, OPUS_DEMOTED_TIER)


# ── Per-site starvation rebalance (2026-06-09) ────────────────────────────
# When one site's content-production pipeline has stalled (capacity caps,
# stuck recs, transient infra failures) while the other site is shipping
# normally, the priority sort doesn't notice — it just sees the tier
# numbers. Result: the starved site stays starved as long as the healthy
# site keeps emitting recs.
#
# This helper queries `editorial_articles` per site over the last 7 days
# and returns a per-site tier ADJUSTMENT to apply during drain:
#   - Site shipped 0–1 articles in 7d and the other shipped >10 → boost -2
#   - Site shipped 2–5 and the other shipped >20 → boost -1
#   - Otherwise → boost 0 (no change)
#
# A negative boost makes the starved site sort EARLIER (lower tier
# number). The drainer caps the final tier at 1 so a starved T3 won't
# leapfrog a non-starved T1 (SEO/PI keep top priority always).
#
# Cached for 5 min so we don't hammer the DB on every drain tick.

import time as _time
_STARVATION_CACHE: dict = {"computed_at": 0, "boosts": {}}
_STARVATION_TTL_S = 300  # 5 min

# DB envs to consult — one per site. The site_id label is the bit between
# "DATABASE_URL_" and the end (lowercased). Add new sites by exporting
# DATABASE_URL_<UPPERCASE_SITEID>.
_STARVATION_SITES = (
    "aisleprompt",
    "specpicks",
)


def _read_articles_7d(site_id: str) -> Optional[int]:
    """Return count of editorial_articles rows created in the last 7d for
    this site, or None on any error (don't poison the boost calc)."""
    import os
    dsn = os.environ.get(f"DATABASE_URL_{site_id.upper()}")
    if not dsn:
        return None
    try:
        import psycopg2
    except ImportError:
        return None
    try:
        with psycopg2.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM editorial_articles "
                    "WHERE created_at > now() - interval '7 days'"
                )
                return int(cur.fetchone()[0])
    except Exception:
        return None


def site_starvation_boost(
    *,
    storage: Optional[StorageBackend] = None,
    sites: tuple = _STARVATION_SITES,
    now: Optional[float] = None,
) -> dict:
    """Returns {site_id: tier_delta} where tier_delta is negative for
    starved sites. Cached for `_STARVATION_TTL_S` seconds. Safe to call
    on every drain tick.
    """
    now = now or _time.time()
    if now - _STARVATION_CACHE["computed_at"] < _STARVATION_TTL_S:
        return _STARVATION_CACHE["boosts"]
    counts: dict = {}
    for site in sites:
        n = _read_articles_7d(site)
        if n is not None:
            counts[site] = n
    if len(counts) < 2:
        # Can't decide who's starved without at least two sites' data.
        _STARVATION_CACHE.update({"computed_at": now, "boosts": {}})
        return {}
    boosts: dict = {}
    others_max = lambda this: max(c for s, c in counts.items() if s != this)
    for site, n in counts.items():
        peer_n = others_max(site)
        if n <= 1 and peer_n > 10:
            boosts[site] = -2  # severe starvation
        elif n <= 5 and peer_n > 20:
            boosts[site] = -1  # mild starvation
    _STARVATION_CACHE.update({"computed_at": now, "boosts": boosts})
    return boosts
