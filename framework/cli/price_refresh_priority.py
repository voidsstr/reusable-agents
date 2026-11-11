"""CLI: inspect the demand-ranked price refresh (framework/core/price_refresh_priority.py).

Read-only: the DB session is opened read-only and nothing is written to
storage. Use it to answer "why wasn't this product re-priced?" or to check a
config change before the next tick.

Usage:
    python3 -m framework.cli.price_refresh_priority config  --agent-id <id> [--site <site>] [--site-cfg <src>]
    python3 -m framework.cli.price_refresh_priority tiers   --agent-id <id> [--site <site>] [--site-cfg <src>] [--dsn-env ENV]
    python3 -m framework.cli.price_refresh_priority rank    --agent-id <id> ... [--limit 50]
    python3 -m framework.cli.price_refresh_priority explain --agent-id <id> ... <ASIN>

--site-cfg names where the consumer's site config lives:
    path/to/site.yaml#price_refresh.demand_priority   a YAML/JSON file (+ dotted key)
    path/to/agent.py:DEMAND_SITE_CFG                  a module attribute (dict, or a
                                                      zero-argument function returning one)
Without it only the storage/repo config applies (enough for consumers whose
site values live in storage by_site / by_agent_id).

`tiers` prints per-tier membership and price freshness (fresh = checked within
display_hours); `rank` prints the first --limit due candidates; `explain`
prints every group holding the ASIN, its gate, age and queue position.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from framework.core import price_refresh_priority as prp


def _dig(doc: Any, dotted: str) -> Any:
    for part in [p for p in (dotted or "").split(".") if p]:
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


def load_site_cfg(src: str | None) -> dict:
    """Load a consumer's site config: FILE[#dotted.key] or FILE.py:ATTR."""
    if not src:
        return {}
    if ".py:" in src:
        path, attr = src.rsplit(":", 1)
        spec = importlib.util.spec_from_file_location("prp_site_cfg_module", path)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        val = getattr(mod, attr)
        val = val() if callable(val) else val
        if not isinstance(val, dict):
            raise SystemExit(f"{src} is not a dict")
        return val
    path, _, key = src.partition("#")
    text = Path(path).read_text()
    if path.endswith((".yaml", ".yml")):
        import yaml
        doc = yaml.safe_load(text) or {}
    else:
        doc = json.loads(text)
    val = _dig(doc, key) if key else doc
    if not isinstance(val, dict):
        raise SystemExit(f"{src}: no config object there")
    return val


def _cfg(args) -> dict:
    return prp.resolve_config(agent_id=args.agent_id, site=args.site or "",
                              site_cfg=load_site_cfg(args.site_cfg))


def _connect(dsn_env: str):
    import psycopg2  # type: ignore
    dsn = os.environ.get(dsn_env, "")
    if not dsn:
        raise SystemExit(f"{dsn_env} is not set (set -a; . ~/.reusable-agents/secrets.env; set +a)")
    conn = psycopg2.connect(dsn)
    conn.set_session(readonly=True)
    return conn


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="price_refresh_priority",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["config", "tiers", "rank", "explain"])
    ap.add_argument("asin", nargs="?", default="")
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--site", default="")
    ap.add_argument("--site-cfg", default="")
    ap.add_argument("--dsn-env", default="DATABASE_URL")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args(argv)

    cfg = _cfg(args)
    if args.cmd == "config":
        _print(cfg)
        return 0
    if args.cmd == "explain" and not args.asin:
        ap.error("explain needs an ASIN")
    if not cfg.get("enabled"):
        print(f"note: demand ranking is DISABLED for {args.agent_id} "
              f"(enabled=false in the resolved config); showing what it would do",
              file=sys.stderr)
    conn = _connect(args.dsn_env)
    try:
        tiers = prp.collect_tiers(conn, cfg)
        if args.cmd == "tiers":
            out: dict[str, Any] = {"signals": tiers.summary()}
            if (cfg.get("catalog") or {}).get("table"):
                out["freshness"] = prp.tier_freshness(conn, cfg, tiers)
            _print(out)
        elif args.cmd == "rank":
            cands = prp.rank_refresh_candidates(conn, cfg, limit=args.limit, tiers=tiers)
            _print({"signals": tiers.summary(), "by_tier": prp.tier_counts(cands),
                    "candidates": [c._asdict() for c in cands]})
        else:
            _print(prp.explain(conn, cfg, args.asin, tiers=tiers))
    finally:
        conn.rollback()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
