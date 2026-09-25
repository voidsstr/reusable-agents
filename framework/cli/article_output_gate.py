"""CLI entry: evaluate an article proposer's output gate without running it.

Read-only. It never records a batch. Use it to answer "why didn't the
proposer write anything today?" or to check a config change before the
next tick.

Usage:
    python3 -m framework.cli.article_output_gate evaluate \\
        --agent-id <id> --site-id <site> --site-yaml <path> \\
        [--dsn-env DATABASE_URL_<SITE>] [--audience tech|food]
    python3 -m framework.cli.article_output_gate ledger --agent-id <id>
    python3 -m framework.cli.article_output_gate config --agent-id <id> [--site-yaml <path>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from framework.core import article_output_gate as gate


def _site_cfg(path: str | None) -> dict:
    if not path:
        return {}
    import yaml
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    return ((doc.get("proposals") or {}).get("output_gate")) or {}


def main() -> int:
    p = argparse.ArgumentParser(description="Article output gate inspector")
    sub = p.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="print the gate decision (read-only)")
    ev.add_argument("--agent-id", required=True)
    ev.add_argument("--site-id", required=True)
    ev.add_argument("--site-yaml", help="proposer site.yaml (reads proposals.output_gate)")
    ev.add_argument("--dsn-env", default="DATABASE_URL",
                    help="env var holding the site DB DSN")
    ev.add_argument("--audience", help="seasonal audience (tech|food|general)")
    lg = sub.add_parser("ledger", help="print the agent's queued-batch ledger")
    lg.add_argument("--agent-id", required=True)
    cf = sub.add_parser("config", help="print the resolved config")
    cf.add_argument("--agent-id", required=True)
    cf.add_argument("--site-yaml")
    args = p.parse_args()

    from framework.core.storage import get_storage
    storage = get_storage()

    if args.cmd == "ledger":
        print(json.dumps(gate.read_ledger(storage, args.agent_id), indent=2))
        return 0
    if args.cmd == "config":
        print(json.dumps(gate.resolve_config(storage, args.agent_id,
                                             _site_cfg(args.site_yaml)), indent=2))
        return 0
    decision = gate.evaluate(
        storage=storage, agent_id=args.agent_id, site_id=args.site_id,
        dsn=os.environ.get(args.dsn_env) or None,
        site_cfg=_site_cfg(args.site_yaml), audience=args.audience)
    print(json.dumps(decision.to_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
