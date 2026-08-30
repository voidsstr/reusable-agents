"""The deploy gate must test the image it is about to deploy.

Both site gates were broken, in opposite directions, and both silently:

  * specpicks pinned TEST_URL=http://localhost:10001 -- the long-lived dev
    container from docker-compose.yml, built from an old commit. On
    2026-08-30 it was serving a build without the commit being deployed,
    and its green run approved that deploy anyway.
  * aisleprompt pinned a dev server on :10000 that nothing ever started
    (its dev container is on :4004). Every run failed, and the deployer
    hard-gates on test failure, so it shipped nothing for weeks -- 279
    commits deep by the time anyone looked.

Both now run tests/run-local-suite.sh, which BUILDS Dockerfile.azure from
the working tree, starts it against the local fixture Postgres on a port of
its own, and runs the suite against that.
"""
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SITES = ROOT / "examples" / "sites"
REPOS = {
    "specpicks": Path("/home/voidsstr/development/specpicks"),
    "aisleprompt": Path("/home/voidsstr/development/aisleprompt"),
}
FAILED = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def gate_cmds(cfg):
    """Every playwright-running command in a site's deployer.test block."""
    t = ((cfg or {}).get("deployer") or {}).get("test") or {}
    if "cmd" in t:
        return [t["cmd"]]
    return [v["cmd"] for v in t.values() if isinstance(v, dict) and "cmd" in v]


def main():
    for site in ("specpicks", "aisleprompt"):
        cfg = yaml.safe_load((SITES / f"{site}.yaml").read_text())
        cmds = gate_cmds(cfg)
        check(bool(cmds), f"{site}: deployer.test declares at least one command")
        for c in cmds:
            check("run-local-suite.sh" in c,
                  f"{site}: gate command runs run-local-suite.sh ({c[:60]})")
            # A pinned TEST_URL is what let both gates test the wrong thing.
            check("TEST_URL" not in c,
                  f"{site}: gate command does not pin TEST_URL ({c[:60]})")

        repo = REPOS[site]
        if not repo.is_dir():
            print(f"SKIP  {site}: repo not present at {repo}")
            continue

        runner = repo / "tests" / "run-local-suite.sh"
        check(runner.is_file(), f"{site}: tests/run-local-suite.sh exists")
        if runner.is_file():
            src = runner.read_text()
            check(subprocess.run(["bash", "-n", str(runner)]).returncode == 0,
                  f"{site}: run-local-suite.sh is syntactically valid")
            check("Dockerfile.azure" in src or "docker-compose.test.yml" in src,
                  f"{site}: runner builds from the production compose/Dockerfile")
            check("build backend-test" in src,
                  f"{site}: runner rebuilds the image every run")
            # A build failure IS a gate failure -- it means the image that
            # would be deployed does not build.
            check("BUILD FAILED" in src and "exit 3" in src,
                  f"{site}: a failed build fails the gate instead of being skipped")
            check("exit $rc" in src,
                  f"{site}: runner exits with Playwright's status")

        compose = repo / "docker-compose.test.yml"
        check(compose.is_file(), f"{site}: docker-compose.test.yml exists")
        if compose.is_file():
            c = yaml.safe_load(compose.read_text())
            svc = (c.get("services") or {}).get("backend-test")
            check(svc is not None, f"{site}: declares a backend-test service")
            if svc:
                check(svc.get("build", {}).get("dockerfile") == "Dockerfile.azure",
                      f"{site}: backend-test builds the PRODUCTION Dockerfile")
                ports = [str(p) for p in svc.get("ports", [])]
                # Its own port: sharing the dev container's is the whole bug.
                check(not any(p.startswith("10001:") or p.startswith("4004:") for p in ports),
                      f"{site}: backend-test does not reuse the dev container's port ({ports})")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
