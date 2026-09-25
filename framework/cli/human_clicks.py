"""Operator CLI for framework.core.human_clicks — print the verified-human
breakdown for a first-party click table.

Example (per-site values are arguments, never framework defaults):

    set -a; source ~/.reusable-agents/secrets.env; set +a
    python3 -m framework.cli.human_clicks \\
        --dsn-env DATABASE_URL_<SITE> --table outbound_clicks \\
        --time-col clicked_at --referer-col source_page \\
        --country-col country --bot-flag-col is_bot \\
        --match target=amazon --days 30 --profile <site>-site-goals-tracker

`--profile` applies the storage override `by_profile[<profile>]` from
config/human-click-filter-config.json, exactly as the goal tracker does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from framework.core import human_clicks


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dsn-env", required=True,
                   help="env var holding the Postgres DSN")
    p.add_argument("--table", required=True)
    p.add_argument("--time-col", default=None)
    p.add_argument("--ua-col", default=None)
    p.add_argument("--ip-col", default=None)
    p.add_argument("--referer-col", default=None)
    p.add_argument("--country-col", default=None)
    p.add_argument("--bot-flag-col", default=None)
    p.add_argument("--match", action="append", default=[],
                   help="col=value (repeatable; comma-separate values for IN)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--profile", default="")
    p.add_argument("--no-storage", action="store_true",
                   help="ignore the storage override doc")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    dsn = os.environ.get(a.dsn_env)
    if not dsn:
        print(f"{a.dsn_env} is not set", file=sys.stderr)
        return 2
    spec_in = {k: v for k, v in {
        "time_col": a.time_col, "ua_col": a.ua_col, "ip_col": a.ip_col,
        "referer_col": a.referer_col, "country_col": a.country_col,
        "bot_flag_col": a.bot_flag_col,
    }.items() if v}
    match: dict = {}
    for m in a.match:
        col, _, val = m.partition("=")
        match[col] = val.split(",") if "," in val else val
    spec = human_clicks.resolve_spec(spec_in, profile=a.profile,
                                     config={} if a.no_storage else None)

    import psycopg2
    conn = psycopg2.connect(dsn, connect_timeout=15)
    try:
        bd = human_clicks.breakdown(conn, spec, table=a.table,
                                    window_days=a.days, match=match)
    finally:
        conn.close()
    if a.json:
        print(json.dumps({"table": a.table, "days": a.days, "match": match,
                          "stale_thresholds": human_clicks.stale_thresholds(spec),
                          "breakdown": bd}, indent=2))
    else:
        total = bd.get("_total", 0)
        print(f"{a.table} last {a.days}d {match or ''}: "
              f"{bd.get(human_clicks.VERDICT_HUMAN, 0)} verified human of {total}")
        for k, v in sorted(bd.items(), key=lambda kv: -kv[1]):
            if k != "_total":
                print(f"  {k:<18} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
