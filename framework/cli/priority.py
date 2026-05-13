"""CLI entry: print the priority tier for an agent.

Used by host-worker.sh + any other shell-side caller that needs to
order a queue without re-implementing the tier logic. Output is a
single integer, suitable for piping into `sort -n`.

Usage:
    python3 -m framework.cli.priority <agent-id>           # → "1"
    python3 -m framework.cli.priority <agent-id> --json    # → {"agent_id":..., "tier":1, "label":"..."}
    python3 -m framework.cli.priority --init               # write default config if missing
    python3 -m framework.cli.priority --dump               # print full config
"""
from __future__ import annotations

import argparse
import json
import sys

from framework.core.priority import (
    DEFAULT_TIER,
    ensure_default_config_exists,
    load_priority_config,
    tier_for_agent,
)


def main() -> None:
    p = argparse.ArgumentParser(description="Resolve an agent's priority tier")
    p.add_argument("agent_id", nargs="?", help="agent id to resolve")
    p.add_argument("--json", action="store_true",
                   help="emit JSON {agent_id, tier, label}")
    p.add_argument("--init", action="store_true",
                   help="write the default priority config if storage key is missing")
    p.add_argument("--dump", action="store_true",
                   help="print the active priority config")
    args = p.parse_args()

    if args.init:
        wrote = ensure_default_config_exists()
        print(json.dumps({"wrote_default": wrote}))
        return

    cfg = load_priority_config()
    if args.dump:
        print(json.dumps(cfg, indent=2))
        return

    if not args.agent_id:
        p.print_help()
        sys.exit(2)

    tier = tier_for_agent(args.agent_id, config=cfg)
    if args.json:
        # Find the matching tier's label (best-effort)
        label = ""
        block = (cfg.get("tiers") or {}).get(str(tier))
        if isinstance(block, dict):
            label = block.get("label", "")
        print(json.dumps({
            "agent_id": args.agent_id,
            "tier": tier,
            "label": label,
        }))
    else:
        # Plain int — bash-callable
        print(tier)


if __name__ == "__main__":
    main()
