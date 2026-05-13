"""Mark recs as shipped in their producer's accumulator immediately
after the implementer ships them.

Why this exists
---------------
Without this step, a rec's lifecycle was:
  1. Producer emits → accumulator entry state=open
  2. Implementer ships → writes implemented:true to producer's
     recommendations.json AND _ship_status.json in the dispatch run-dir
  3. Producer's NEXT cron run reads its own recommendations.json and
     transitions accumulator entry open → implemented (via persist_run)
  4. backlog-dispatcher sees state!=open, skips

The breakage: between step 2 and step 3 (sometimes hours, sometimes
days if the producer is paused) the backlog-dispatcher re-queued the
same rec because accumulator still said state=open. The implementer
re-shipped — wasted tokens, potential regression-flag noise.

The fix: this CLI runs RIGHT AFTER the implementer's git commit. It
takes the rec_ids that just shipped, looks them up in the producer's
recommendations.json to get titles, finds matching accumulator entries
by normalized title, and flips them to state=implemented.

The producer's eventual re-run will see the same state and no-op.

Usage (from implementer/run.sh after a successful commit):
    python3 -m framework.cli.mark_shipped_in_accumulator \
        --source-agent specpicks-progressive-improvement-agent \
        --source-run-ts 20260511T123001Z \
        --rec-ids rec-001,rec-002,rec-005 \
        --implementation-sha $SHA
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core import rec_memory  # noqa: E402
from framework.core.storage import get_storage  # noqa: E402


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _rec_ids_from(arg: str) -> list[str]:
    return [r.strip() for r in (arg or "").split(",") if r.strip()]


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mark_shipped_in_accumulator")
    p.add_argument("--source-agent", required=True,
                   help="producer agent id (e.g. specpicks-progressive-improvement-agent)")
    p.add_argument("--source-run-ts", required=True,
                   help="run_ts whose recommendations.json holds these recs")
    p.add_argument("--rec-ids", required=True,
                   help="comma-separated rec_ids that just shipped")
    p.add_argument("--implementation-sha", default="",
                   help="git commit SHA the implementer wrote")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)

    rec_ids = set(_rec_ids_from(args.rec_ids))
    if not rec_ids:
        return 0

    s = get_storage()
    recs_key = f"agents/{args.source_agent}/runs/{args.source_run_ts}/recommendations.json"
    try:
        doc = s.read_json(recs_key) or {}
    except Exception as e:
        print(f"[mark-shipped] can't read {recs_key}: {e}", file=sys.stderr)
        return 0  # non-fatal — don't fail the implementer run

    recs = doc if isinstance(doc, list) else (doc.get("recommendations") or [])
    if not isinstance(recs, list):
        return 0

    # Build {rec_id: normalized_title} for the ones we just shipped.
    shipped_titles: dict[str, str] = {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("rec_id") or r.get("rec_uid")
        if rid and rid in rec_ids:
            shipped_titles[rid] = _norm_title(r.get("title") or "")

    if not shipped_titles:
        if args.verbose:
            print(f"[mark-shipped] no matching recs in {recs_key}", file=sys.stderr)
        return 0

    # Load the producer's accumulator + flip matching entries.
    accum = rec_memory.load_active(s, args.source_agent)
    flipped = 0
    titles_to_match = set(shipped_titles.values())
    for prop in accum.get("proposals", []):
        if prop.get("state") != "open":
            continue
        if _norm_title(prop.get("title") or "") in titles_to_match:
            ok = rec_memory.transition(
                accum, prop["id"],
                to_state="implemented",
                run_ts=args.source_run_ts,
                implementation_sha=args.implementation_sha or None,
            )
            if ok:
                flipped += 1

    if flipped:
        rec_memory.save_active(s, args.source_agent, accum)
        print(f"[mark-shipped] flipped {flipped} accumulator entry(ies) "
              f"to implemented for {args.source_agent} sha={args.implementation_sha[:8] if args.implementation_sha else '?'}")
    elif args.verbose:
        print(f"[mark-shipped] no accumulator entries matched (already shipped?) for {args.source_agent}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
