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
          "agents": ["aisleprompt-article-author-agent",
                     "aisleprompt-head-to-head-agent"]
        },
        "3": {
          "label": "SpecPicks content",
          "agents": ["specpicks-article-author-agent",
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
                "aisleprompt-article-author-agent",
                "aisleprompt-head-to-head-agent",
            ],
        },
        "3": {
            "label": "SpecPicks content production",
            "agents": [
                "specpicks-article-author-agent",
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
