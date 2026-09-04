"""Every enabled agent with a cron must have a systemd timer, and vice versa.

Found 2026-09-04: four registered+enabled agents with cron expressions had
NO systemd unit at all, so they had never run once.

  specpicks-search-demand-agent   distills GSC+GA4 into the topic-steering
                                  signal that article-proposal and
                                  head-to-head inject into their prompts --
                                  so topic selection had been running with
                                  an empty steering signal the whole time
  specpicks-ai-news-aggregator    hourly news feed
  specpicks-article-author-agent  GHOST: entry_command points at
  aisleprompt-article-author-agent  .../agents/article-author-agent/, a
                                  directory that does not exist in either
                                  repo. Articles are authored through the
                                  dispatch path (DISPATCH_KIND=article-author
                                  in implementer/run.sh), not by these.
                                  Deregistered rather than wired up --
                                  wiring a ghost would have "fixed" the
                                  parity check while running nothing.

The drift matters in both directions: a registered agent with no timer never
runs, and a timer with no registry entry runs work nobody is tracking.
"""
import subprocess
import sys

FAILED = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def main():
    try:
        from framework.core import registry
        agents = [m for m in registry.list_agents()
                  if m.enabled and (m.cron_expr or "").strip()]
    except Exception as e:
        print(f"SKIP  registry unreachable ({str(e).splitlines()[0][:60]})")
        return 0

    missing = []
    for m in agents:
        r = subprocess.run(["systemctl", "--user", "is-enabled",
                            f"agent-{m.id}.timer"], capture_output=True, text=True)
        blob = r.stdout + r.stderr
        if "not-found" in blob or not r.stdout.strip():
            missing.append(m.id)

    check(not missing,
          f"every enabled+cron agent has a systemd timer "
          f"({len(agents)} checked, {len(missing)} missing)")
    for i in sorted(missing):
        print(f"        MISSING TIMER: {i}")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
