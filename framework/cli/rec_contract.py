"""CLI for framework/core/rec_contract.py: hold recs written under an older
producer contract.

  filter   The implementer's gate. It reads <run-dir>/recommendations.json,
           defers the held recs in the batch (changes/<id>.summary.md) and
           marks them review_required + contract_hold in that file. stdout
           is "KEEP:<ids>" when anything was held (empty after the colon
           when everything was), and nothing otherwise. Diagnostics go to
           stderr. It never fails the caller: on an error it prints the
           error to stderr and nothing to stdout.

  sweep    The operator's one-off. It finds every open held rec in the
           named producers' run-dirs and, with --apply, marks it held.
           Without --apply it only reads.

Usage:
    python3 -m framework.cli.rec_contract filter --run-dir DIR --rec-ids a,b \\
        [--producer AGENT_ID] [--no-mark]
    python3 -m framework.cli.rec_contract sweep --agents '*-catalog-audit-agent' \\
        [--agent-id ID ...] [--apply] [--json]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys

from framework.core import rec_contract as rc


def _filter(a) -> int:
    ids = [x.strip() for x in (a.rec_ids or "").split(",") if x.strip()]
    if not ids:
        return 0
    try:
        keep, held = rc.filter_run_dir(a.run_dir, ids, producer=a.producer or "",
                                       mark=not a.no_mark)
    except Exception as e:  # noqa: BLE001 -- never fail the implementer
        print("[rec-contract] filter failed, batch unchanged: %s" % str(e)[:300], file=sys.stderr)
        return 0
    for rid, hold in held:
        print("[rec-contract] held %s (rule %s): %s" % (rid, hold["rule"], hold["why"]),
              file=sys.stderr)
    if held:
        print("KEEP:" + ",".join(keep))
    return 0


def _agent_ids(a, storage) -> list[str]:
    out = list(a.agent_id or [])
    if a.agents:
        from framework.core import registry
        known = sorted(m.id for m in registry.list_agents(storage=storage))
        for g in a.agents:
            out += [aid for aid in known if fnmatch.fnmatchcase(aid, g)]
    return sorted(set(out))


def _sweep(a) -> int:
    from framework.core.storage import get_storage
    s = get_storage()
    agents = _agent_ids(a, s)
    if not agents:
        print("rec-contract sweep: no agents matched %s" % (a.agents or a.agent_id))
        return 1
    rep = rc.sweep(s, agents, apply=a.apply)
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
        return 0
    mode = "APPLIED" if a.apply else "dry run (nothing written)"
    print("rec-contract sweep, %s" % mode)
    for aid, st in rep["agents"].items():
        print("  %-40s runs=%-4d runs_with_held=%-3d held_dispatchable=%-4d held_review=%-4d "
              "marked=%d" % (aid, st["runs_scanned"], st["runs_with_held"],
                             st["held_dispatchable"], st["held_review"], st["marked"]))
    t = rep["totals"]
    print("  total: held_dispatchable=%d held_review=%d marked=%d"
          % (t["held_dispatchable"], t["held_review"], t["marked"]))
    for e in rep["errors"][:20]:
        print("  error: %s" % e)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Hold recs written under an older producer contract")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("filter", help="implementer gate over one dispatched batch")
    f.add_argument("--run-dir", required=True)
    f.add_argument("--rec-ids", required=True)
    f.add_argument("--producer", default="")
    f.add_argument("--no-mark", action="store_true",
                   help="defer held recs but do not mark them in recommendations.json")
    w = sub.add_parser("sweep", help="mark every open held rec in producers' run-dirs")
    w.add_argument("--agents", action="append", default=[],
                   help="fnmatch glob over registered agent ids (repeatable)")
    w.add_argument("--agent-id", action="append", default=[], help="an agent id (repeatable)")
    w.add_argument("--apply", action="store_true", help="write the marks (default: dry run)")
    w.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    return _filter(a) if a.cmd == "filter" else _sweep(a)


if __name__ == "__main__":
    sys.exit(main())
