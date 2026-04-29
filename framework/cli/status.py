"""CLI: write a single status update for an agent.

Bash agents that don't subclass `AgentBase` use this to keep the dashboard's
"Agents" page in sync. Otherwise their status.json goes stale because the
host-worker only writes status when triggered via "Run now" — cron-fired
runs (systemd timers) bypass it entirely.

Usage:

    python3 -m framework.cli.status \
        --agent-id aisleprompt-seo-opportunity-agent \
        --run-ts 20260428T182952Z \
        --state running \
        --message "collector → analyzer → reporter" \
        --progress 0.33 \
        --current-action "pulling GSC + GA4"

Valid states: starting, running, success, failure, blocked, cancelled, idle.

Always force-writes (no throttle) since CLI invocations are coarse-grained.
"""
from __future__ import annotations

import argparse
import sys

from framework.core.status import StatusReporter


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent-id", required=True)
    p.add_argument("--run-ts", required=True)
    p.add_argument("--state", required=True,
                   choices=("starting", "running", "success", "failure",
                            "blocked", "cancelled", "idle"))
    p.add_argument("--message", default="")
    p.add_argument("--progress", type=float, default=0.0)
    p.add_argument("--current-action", default="")
    p.add_argument("--iteration-count", type=int, default=0)
    args = p.parse_args()

    reporter = StatusReporter(
        agent_id=args.agent_id,
        run_ts=args.run_ts,
        iteration_count=args.iteration_count,
    )
    reporter.update(
        state=args.state,
        message=args.message,
        progress=args.progress,
        current_action=args.current_action,
        force=True,
    )
    print(f"[status] {args.agent_id}: {args.state} — {args.message}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
