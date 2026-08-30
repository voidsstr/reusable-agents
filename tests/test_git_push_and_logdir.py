"""Regressions for two silent-failure bugs found 2026-08-30.

1. Agent commits never reached origin. The implementer's inline push only
   covered its OWN `git commit`; the claude code-editor agent commits from
   inside its own bash sandbox with a different message format, so 12 such
   commits sat unpushed with the inline push already in place, and earlier
   the two site repos had drifted 180-265 commits ahead of origin -- code
   live in production that existed on one machine only.

2. Every agent unit died at boot. The unit log dir lives under /tmp, which
   is wiped on reboot; systemd will not create the parent of an `append:`
   StandardOutput target, so 55 units failed at `209/STDOUT` in the minutes
   after the 2026-08-30 reboot, before running a line of agent code.
"""
import re
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
    pusher = ROOT / "install" / "push-unpushed.sh"
    check(pusher.is_file(), "install/push-unpushed.sh exists")
    if pusher.is_file():
        src = pusher.read_text()
        # It must refuse a diverged branch rather than force -- a diverged
        # site branch is a normal state here and needs a human merge.
        check("behind" in src and "NOT pushing" in src,
              "push-unpushed.sh refuses to push a diverged branch")
        check("--force" not in src and "-f " not in src,
              "push-unpushed.sh never force-pushes")
        check(src.rstrip().endswith("exit 0"),
              "push-unpushed.sh always exits 0 (a failed push must not fail the caller)")
        check(subprocess.run(["bash", "-n", str(pusher)]).returncode == 0,
              "push-unpushed.sh is syntactically valid")
        # A non-repo argument is a no-op, not a crash.
        r = subprocess.run(["bash", str(pusher), "/nonexistent-path-xyz"],
                           capture_output=True, text=True)
        check(r.returncode == 0, "push-unpushed.sh exits 0 on a bad path")

    run_sh = (ROOT / "agents" / "implementer" / "run.sh").read_text()
    # The push must be in the EXIT trap, not only inline after `git commit`:
    # that is precisely what let the code-editor's own commits strand.
    trap = [l for l in run_sh.splitlines() if l.startswith("trap ") and "EXIT" in l]
    check(bool(trap) and any("_push_implementer_repo" in l for l in trap),
          "implementer pushes from the EXIT trap (catches every committer + early exit)")
    check("push-unpushed.sh" in run_sh,
          "implementer uses the shared push-unpushed.sh")

    dep = (ROOT / "agents" / "deployer" / "deployer.py").read_text()
    check("push-unpushed.sh" in dep, "deployer uses the shared push-unpushed.sh")
    check("tag-release.sh" in dep, "deployer still tags each release")

    sched = (ROOT / "framework" / "core" / "scheduler.py").read_text()
    check("ExecStartPre=-/bin/mkdir -p {log_dir}" in sched,
          "generated units create their log dir before ExecStart")
    check(re.search(r"log_dir=log_dir", sched) is not None,
          "scheduler passes log_dir into the unit template")

    standup = (ROOT / "install" / "standup-fleet-host.sh").read_text()
    check(standup.count("ExecStartPre=-/bin/mkdir -p $LOG_DIR") == 2,
          "standup-fleet-host units (api + drainer) create their log dir")

    drift = ROOT / "install" / "deploy-drift.sh"
    check(drift.is_file(), "install/deploy-drift.sh exists")
    if drift.is_file():
        check(subprocess.run(["bash", "-n", str(drift)]).returncode == 0,
              "deploy-drift.sh is syntactically valid")
        d = drift.read_text()
        check("rev-list --count" in d and "release/" in d,
              "deploy-drift.sh measures HEAD against the newest release tag")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
