"""Pausing the agents must be reversible, and must not resurrect the dead.

Written 2026-08-30 when the fleet was paused to stop it spending Claude
credits during the day. The hazard is the resume: a blanket
`systemctl --user enable agent-*.timer` would switch on units that are
disabled ON PURPOSE -- specpicks-scraper-watchdog is `enabled: false` in
its manifest and in the registry, and had been firing every five minutes
and failing every time until a9ca035 stopped registration re-enabling it.
So resume replays a recorded list, and a unit that was off stays off.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def main():
    pause = ROOT / "install" / "agents-pause.sh"
    resume = ROOT / "install" / "agents-resume.sh"
    at = ROOT / "install" / "agents-resume-at.sh"

    for f in (pause, resume, at):
        check(f.is_file(), f"install/{f.name} exists")
        if f.is_file():
            check(subprocess.run(["bash", "-n", str(f)]).returncode == 0,
                  f"{f.name} is syntactically valid")

    if pause.is_file():
        p = pause.read_text()
        check("list-unit-files" in p and "enabled" in p,
              "pause records which timers were ENABLED, not merely present")
        check("paused-timers.txt" in p, "pause writes a state file")
        check("claude_pool" in p,
              "pause also stops in-flight work (a running agent keeps spending)")

    if resume.is_file():
        r = resume.read_text()
        check("paused-timers.txt" in r, "resume replays the recorded list")
        # The critical property: no blanket enable of every agent timer.
        check("agent-*" not in r and "agent-*.timer" not in r,
              "resume never globs agent timers (would resurrect disabled ones)")
        check("mkdir -p /tmp/reusable-agents-logs" in r,
              "resume recreates the log dir /tmp wipes on reboot")

    if at.is_file():
        a = at.read_text()
        check("--on-calendar" in a, "scheduler uses a systemd calendar timer")
        check("Persistent=true" in a,
              "scheduled resume survives a reboot before it fires")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
