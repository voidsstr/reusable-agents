#!/usr/bin/env python3
"""seo-deployer — reads deployer.* from site config, runs test→build→push→
deploy→smoke. Writes deploy.json. Hard-gates on test failure.

Usage:
  SEO_AGENT_CONFIG=my-site.yaml python3 deployer.py --run-dir <path>
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.site_config import load_config_from_env  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Tiered test scope (smoke per-batch, full weekly)
# ---------------------------------------------------------------------------

def _last_full_test_state_path(site_id: str) -> Path:
    state_dir = Path(__file__).resolve().parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{site_id}-last-full-test.json"


def _record_last_full_test(site_id: str) -> None:
    p = _last_full_test_state_path(site_id)
    p.write_text(json.dumps({"site": site_id, "ts": _now_iso()}))


def _seconds_since_last_full_test(site_id: str) -> float | None:
    p = _last_full_test_state_path(site_id)
    if not p.is_file():
        return None
    try:
        ts = json.loads(p.read_text()).get("ts", "")
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _select_test_scope(test_block: dict, site_id: str,
                       run_dir: Path) -> tuple[str, str, str | None]:
    """Pick which test command to run.

    Returns (scope, cmd, cwd) where scope is one of:
      "legacy"  — single `test.cmd` form, run every time (backward compat)
      "smoke"   — `test.smoke.cmd` (run per-batch deploy)
      "full"    — `test.full.cmd` (run weekly, or when DEPLOYER_TEST_SCOPE=full)

    Selection priority:
      1. Explicit DEPLOYER_TEST_SCOPE env var ("smoke" / "full" / "legacy")
      2. site.yaml deployer.test_scope override
      3. Auto: full when last full run > full_interval_days old (default 7),
         smoke otherwise
      4. Legacy fallback when only `test.cmd` is configured (no smoke/full
         sub-blocks).
    """
    smoke = test_block.get("smoke") or {}
    full = test_block.get("full") or {}
    legacy_cmd = test_block.get("cmd")
    legacy_cwd = test_block.get("cwd")

    # Legacy single-command form — no tier, run as-is.
    if legacy_cmd and not smoke and not full:
        return "legacy", legacy_cmd, legacy_cwd

    forced = (os.environ.get("DEPLOYER_TEST_SCOPE", "")
              or test_block.get("test_scope", "")).lower()
    if forced in ("smoke", "full", "legacy"):
        scope = forced
    else:
        full_interval_days = float(test_block.get("full_interval_days", 7))
        last = _seconds_since_last_full_test(site_id)
        if last is None or last > full_interval_days * 86400:
            scope = "full"
        else:
            scope = "smoke"

    if scope == "full":
        cmd = full.get("cmd") or legacy_cmd
        cwd = full.get("cwd") or legacy_cwd
        return "full", cmd, cwd
    if scope == "smoke":
        cmd = smoke.get("cmd") or legacy_cmd
        cwd = smoke.get("cwd") or legacy_cwd
        return "smoke", cmd, cwd
    # Fallthrough — legacy
    return "legacy", legacy_cmd, legacy_cwd


def _expand(cmd: str, vars_: dict, tag: str, image: str) -> str:
    """Substitute {tag}, {image}, {app}, {rg}, etc. into a command template."""
    if not cmd:
        return cmd
    out = cmd
    out = out.replace("{tag}", tag)
    out = out.replace("{image}", image)
    for k, v in vars_.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# ---------------------------------------------------------------------------
# Local dev server (per-site Docker container backing the test step)
# ---------------------------------------------------------------------------

def _ensure_local_dev(local_dev_block: dict, repo_root: str | None) -> tuple[bool, str]:
    """Ensure the per-site local dev server is reachable before running tests.

    Each site can declare:
      local_dev:
        port: 4001                                 # convenience for {local_dev_port}
        url:  http://localhost:4001                # convenience for {local_dev_url}
        health_url: http://localhost:4001/api/health
        ensure_running:
          cwd: .
          cmd: docker compose up -d frontend backend
        health_timeout_s: 60
        health_interval_s: 2

    Behavior:
      1. GET health_url. If 200/2xx → already up, return (True, '').
      2. Otherwise run ensure_running.cmd (idempotent — Compose's
         `up -d` is no-op when containers are already healthy).
      3. Poll health_url every health_interval_s until it returns 2xx
         or health_timeout_s elapses.

    Returns (ok, detail). On failure, deploy SHOULD NOT proceed — tests
    will hammer a non-existent server and 404, blocking the pipeline.
    """
    if not local_dev_block:
        return True, "no local_dev configured"
    health_url = local_dev_block.get("health_url")
    if not health_url:
        return True, "no health_url — assuming caller manages the dev server"

    timeout_s = float(local_dev_block.get("health_timeout_s", 60))
    interval_s = float(local_dev_block.get("health_interval_s", 2))

    def _probe() -> bool:
        try:
            req = urllib.request.Request(
                health_url,
                headers={"User-Agent": "deployer-local-dev-probe/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return 200 <= resp.status < 400
        except Exception:
            return False

    if _probe():
        return True, "already healthy"

    ensure = local_dev_block.get("ensure_running") or {}
    cmd = ensure.get("cmd")
    if not cmd:
        return False, (
            f"local dev server at {health_url} not reachable and "
            "no ensure_running.cmd to bring it up"
        )

    print(f"[deployer:local-dev] {health_url} not reachable, running "
          f"ensure_running.cmd", file=sys.stderr)
    rc, _stdout, stderr = run_step(
        "local-dev:ensure", ensure.get("cwd"), cmd, repo_root=repo_root,
        timeout=int(local_dev_block.get("ensure_timeout_s", 300)),
    )
    if rc != 0:
        return False, f"ensure_running.cmd rc={rc} stderr={stderr[-400:]}"

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _probe():
            return True, "brought up + healthy"
        time.sleep(interval_s)
    return False, (
        f"ensure_running.cmd succeeded but {health_url} did not become "
        f"healthy within {timeout_s}s"
    )


def _expand_local_dev(cmd: str | None, local_dev_block: dict) -> str | None:
    """Substitute {local_dev_port} and {local_dev_url} in test commands."""
    if not cmd or not local_dev_block:
        return cmd
    port = local_dev_block.get("port")
    url = local_dev_block.get("url") or (
        f"http://localhost:{port}" if port else None
    )
    out = cmd
    if port is not None:
        out = out.replace("{local_dev_port}", str(port))
    if url:
        out = out.replace("{local_dev_url}", str(url))
    return out


def run_step(name: str, cwd: str | None, cmd: str, env: dict | None = None,
             timeout: int = 1800,
             repo_root: str | None = None) -> tuple[int, str, str]:
    """Run a shell command. `cwd` from site.yaml is interpreted RELATIVE
    TO `repo_root` (typically `cfg.implementer.repo_path`) when the
    deployer config was authored against a checked-out repo, e.g.

        deployer:
          build:
            cwd: .                           # → repo_root
            cmd: docker build -f Dockerfile.azure ...
          test:
            cwd: tests                       # → repo_root/tests

    Absolute paths in `cwd` bypass that resolution.
    """
    if not cmd:
        return 0, "", "(no command configured)"
    resolved_cwd = cwd
    if cwd and repo_root and not os.path.isabs(cwd):
        resolved_cwd = os.path.join(repo_root, cwd)
    elif not cwd and repo_root:
        resolved_cwd = repo_root
    print(f"[deployer:{name}] cwd={resolved_cwd or '.'} "
          f"cmd={cmd[:200]}", file=sys.stderr)
    proc = subprocess.run(
        ["bash", "-c", cmd],
        cwd=resolved_cwd or None,
        env={**os.environ, **(env or {})},
        capture_output=True, text=True, timeout=timeout,
    )
    print(f"[deployer:{name}] rc={proc.returncode}", file=sys.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def smoke_check(base_url: str, paths: list[str], timeout: int = 30) -> tuple[bool, list[dict]]:
    results: list[dict] = []
    base = base_url.rstrip("/")
    for path in paths:
        url = base + (path if path.startswith("/") else "/" + path)
        ok = False
        code = 0
        err = ""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "seo-deployer/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.status
                ok = 200 <= code < 400
        except urllib.error.HTTPError as e:
            code = e.code; ok = False
        except Exception as e:
            err = str(e); ok = False
        results.append({"url": url, "code": code, "ok": ok, "error": err})
    return all(r["ok"] for r in results), results


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--skip-test", action="store_true",
                   help="Skip the test step (DANGEROUS — for manual rollback / re-deploy)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands but don't execute")
    args = p.parse_args()

    cfg = load_config_from_env()
    deployer = cfg.get("deployer") or {}
    if not deployer:
        print("[deployer] no deployer block in config — nothing to do", file=sys.stderr)
        return

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        raise SystemExit(f"run dir not found: {run_dir}")

    # Resolve repo_root from cfg.implementer.repo_path so site.yaml's
    # `cwd: .` and `cwd: tests` are interpreted relative to the
    # checked-out repo, not wherever python was invoked from.
    implementer_block = cfg.get("implementer") or {}
    repo_root = implementer_block.get("repo_path") or None
    if repo_root and not Path(repo_root).is_dir():
        print(f"[deployer] WARN implementer.repo_path={repo_root} not a "
              f"directory; falling back to caller cwd", file=sys.stderr)
        repo_root = None

    tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    deploy_vars = (deployer.get("deploy") or {}).get("vars", {}) or {}
    image = deploy_vars.get("image", "")
    deploy_meta = {
        "schema_version": "1",
        "site": cfg.site_id,
        "tag": tag,
        "image": image,
        "started_at": _now_iso(),
        "ended_at": None,
        "test": {"rc": None, "skipped": args.skip_test},
        "build": {"rc": None, "skipped": False},
        "push": {"rc": None, "skipped": False},
        "deploy": {"rc": None, "skipped": False},
        "smoke": {"ok": None, "results": []},
        "status": "running",
        "rollback_cmd": "",
    }

    def _save():
        deploy_meta["ended_at"] = _now_iso()
        (run_dir / "deploy.json").write_text(json.dumps(deploy_meta, indent=2))

    try:
        # ---- 1. Test (hard gate) ----
        # Tiered test scope:
        #   - deployer.test     : full suite (legacy / weekly cadence)
        #   - deployer.test.smoke : fast subset (per-batch default)
        #   - deployer.test.full  : explicit full-suite slot (alias for deployer.test)
        # The framework picks `smoke` by default and bumps to `full` when
        # the last full run is older than `full_interval_days` (default 7).
        # Last-full-run state is persisted at
        #   agents/deployer/state/<site>-last-full-test.json
        # The whole tier is opt-in: a deployer.test block with only `cmd`
        # works exactly as before (single command, run every time).
        if not args.skip_test:
            t = deployer.get("test", {}) or {}
            scope, test_cmd, test_cwd = _select_test_scope(
                t, cfg.site_id, run_dir,
            )
            # Bring up the per-site local dev server (if configured) BEFORE
            # running the test command. Without this, multiple sites
            # collide on a hardcoded TEST_URL and tests 404 against the
            # wrong site's API. The local_dev block is opt-in — sites
            # that don't declare one keep current behavior.
            local_dev = cfg.get("local_dev") or {}
            test_cmd = _expand_local_dev(test_cmd, local_dev)
            if local_dev:
                ok, detail = _ensure_local_dev(local_dev, repo_root)
                if not ok:
                    deploy_meta["status"] = "blocked"
                    deploy_meta["test"] = {
                        "rc": 79, "skipped": False, "scope": scope,
                        "stderr_tail": f"local_dev unhealthy: {detail}",
                    }
                    _save()
                    print(f"[deployer] LOCAL DEV UNAVAILABLE: {detail}",
                          file=sys.stderr)
                    sys.exit(1)
                else:
                    print(f"[deployer:local-dev] ready ({detail})",
                          file=sys.stderr)
            if test_cmd:
                rc, stdout, stderr = run_step(
                    f"test:{scope}", test_cwd, test_cmd, repo_root=repo_root)
                # Sentinel — playwright/jest/etc. exit 0 when their grep
                # filter matches no tests. That would be a silent deploy
                # of untested code. Treat "no tests found" as a hard
                # failure regardless of exit code.
                combined = (stdout or "") + "\n" + (stderr or "")
                if "No tests found" in combined or "0 tests found" in combined:
                    print(f"[deployer] TEST SCOPE EMPTY scope={scope} — "
                          f"refusing to deploy untested code. Tag at least "
                          f"one test with @smoke (or set test_scope=full).",
                          file=sys.stderr)
                    rc = 78  # distinguishable from real failures
                deploy_meta["test"] = {
                    "rc": rc, "skipped": False, "scope": scope,
                    "stderr_tail": stderr[-1000:],
                }
                if rc != 0:
                    deploy_meta["status"] = "blocked"
                    # Capture failure context for the LLM-fix loop
                    # (consumed by an upcoming auto-fix dispatcher; for now
                    # it's just persisted alongside deploy.json so an
                    # operator or follow-up agent can act on it).
                    fail_ctx = {
                        "scope": scope, "rc": rc, "cmd": test_cmd,
                        "cwd": test_cwd, "stderr_tail": stderr[-4000:],
                        "captured_at": _now_iso(),
                    }
                    (run_dir / "test-failure-context.json").write_text(
                        json.dumps(fail_ctx, indent=2))
                    _save()
                    print(f"[deployer] TEST FAILED scope={scope} rc={rc}; "
                          f"aborting deploy (context: "
                          f"{run_dir}/test-failure-context.json)",
                          file=sys.stderr)
                    sys.exit(1)
                if scope == "full":
                    _record_last_full_test(cfg.site_id)
            else:
                deploy_meta["test"]["skipped"] = True

        if args.dry_run:
            deploy_meta["status"] = "dry-run"; _save()
            return

        # ---- 2. Build ----
        b = deployer.get("build", {})
        if b.get("cmd"):
            rc, _, stderr = run_step("build", b.get("cwd"), repo_root=repo_root, cmd=
                                     _expand(b["cmd"], deploy_vars, tag, image))
            deploy_meta["build"] = {"rc": rc, "skipped": False, "stderr_tail": stderr[-1000:]}
            if rc != 0:
                deploy_meta["status"] = "failure"; _save()
                sys.exit(1)
        else:
            deploy_meta["build"]["skipped"] = True

        # ---- 3. Push ----
        ps = deployer.get("push", {})
        if ps.get("cmd"):
            rc, _, stderr = run_step("push", ps.get("cwd"), repo_root=repo_root, cmd=
                                     _expand(ps["cmd"], deploy_vars, tag, image))
            deploy_meta["push"] = {"rc": rc, "skipped": False, "stderr_tail": stderr[-1000:]}
            if rc != 0:
                deploy_meta["status"] = "failure"; _save()
                sys.exit(1)
        else:
            deploy_meta["push"]["skipped"] = True

        # ---- 4. Deploy ----
        # Azure Container Apps + similar managed-control-plane services
        # serialize provisioning operations per resource. When two
        # dispatches deploy the same site near-simultaneously (e.g.
        # SEO opportunity + competitor-research both ship recs against
        # the same container app), the second one's `az containerapp
        # update` returns:
        #   ERROR: (ContainerAppOperationInProgress) Cannot modify a
        #   container app '<name>' because there is an active
        #   provisioning operation in progress.
        # The first deploy's revision swap typically completes within
        # 60-180s, so a brief backoff-and-retry resolves it without
        # losing the deploy. We try up to 3 attempts (60s/180s/360s)
        # before giving up.
        d = deployer.get("deploy", {})
        if d.get("cmd"):
            deploy_cmd = _expand(d["cmd"], deploy_vars, tag, image)
            rc, _, stderr = 1, "", ""
            BUSY_PATTERNS = (
                "ContainerAppOperationInProgress",
                "another operation is in progress",
                "ResourceGroupOperationInProgress",
                "AnotherOperationInProgress",
            )
            backoffs = [60, 180, 360]
            for attempt in range(len(backoffs) + 1):
                rc, _, stderr = run_step("deploy", d.get("cwd"),
                                          repo_root=repo_root, cmd=deploy_cmd)
                if rc == 0:
                    break
                # Only retry on the recognized busy-state error pattern.
                # Other errors (auth, bad image tag, quota) won't get
                # better with a wait, so we fail fast and surface them.
                if not any(pat in (stderr or "") for pat in BUSY_PATTERNS):
                    break
                if attempt >= len(backoffs):
                    break
                wait_s = backoffs[attempt]
                print(f"[deployer] Azure busy (attempt {attempt+1}/{len(backoffs)+1}); "
                      f"sleeping {wait_s}s before retry. stderr_tail={stderr[-200:]!r}",
                      file=sys.stderr)
                time.sleep(wait_s)
            # Post-flight active-revision check. Even when `az containerapp
            # update` returns rc!=0 — most often a CLI timeout that fired
            # AFTER Azure already accepted the revision swap — the new
            # image may still be live. Query the active revision; if its
            # image tag matches what we just tried to deploy, treat the
            # deploy as a success despite the CLI error code. This stops
            # the "5 implemented, 0 deployed" UI artifact when the recs
            # are actually live but Azure timed out the response.
            verified_via_revision = False
            if rc != 0:
                app = deploy_vars.get("app", "")
                rg = deploy_vars.get("rg", "")
                if app and rg:
                    target_tag_suffix = f":{tag}"
                    try:
                        check = subprocess.run(
                            ["az", "containerapp", "revision", "list",
                             "-g", rg, "-n", app,
                             "--query",
                             "[?properties.active]|[0].properties.template.containers[0].image",
                             "-o", "tsv"],
                            capture_output=True, text=True, timeout=60,
                        )
                        active_image = (check.stdout or "").strip()
                        if active_image and active_image.endswith(target_tag_suffix):
                            print(
                                f"[deployer] az returned rc={rc} but active "
                                f"revision image is {active_image!r} matching "
                                f"target tag {tag} — treating as success",
                                file=sys.stderr,
                            )
                            verified_via_revision = True
                    except Exception as _e:
                        print(f"[deployer] post-flight revision check failed: {_e}",
                              file=sys.stderr)
            deploy_meta["deploy"] = {
                "rc": rc, "skipped": False, "stderr_tail": stderr[-1000:],
                "verified_via_revision": verified_via_revision,
            }
            if rc != 0 and not verified_via_revision:
                deploy_meta["status"] = "failure"; _save()
                sys.exit(1)
        else:
            deploy_meta["deploy"]["skipped"] = True

        # ---- 5. Smoke check ----
        sc = deployer.get("smoke_check", {})
        if sc.get("base_url") and sc.get("paths"):
            time.sleep(20)  # let the deploy settle
            ok, results = smoke_check(
                sc["base_url"], sc["paths"], int(sc.get("timeout_seconds", 30)),
            )
            deploy_meta["smoke"] = {"ok": ok, "results": results}
            if not ok:
                deploy_meta["status"] = "failure"; _save()
                # Best-effort: print rollback hint if we have a prior tag
                print(f"[deployer] SMOKE FAILED — manual rollback needed", file=sys.stderr)
                sys.exit(1)

        deploy_meta["status"] = "success"
        deploy_meta["rollback_cmd"] = _expand(d.get("cmd", ""), deploy_vars, "<PRIOR_TAG>", image)
        _save()
        print(f"[deployer] deployed {image}:{tag}", file=sys.stderr)

        # ── Mark recs as shipped ────────────────────────────────────────
        # The implementer earlier marked recs `implemented: true` when it
        # committed/applied them. We now know those changes are live in
        # the deployed container — flip `shipped: true`. The dashboard
        # surfaces shipped vs implemented-but-not-shipped per rec.
        recs_path = run_dir / "recommendations.json"
        if recs_path.is_file():
            try:
                rd = json.loads(recs_path.read_text())
                shipped_at = _now_iso()
                shipped_n = 0
                for r in rd.get("recommendations", []):
                    if r.get("implemented") is True and not r.get("shipped"):
                        r["shipped"] = True
                        r["shipped_at"] = shipped_at
                        r["shipped_tag"] = tag
                        r["shipped_image"] = image
                        shipped_n += 1
                if shipped_n:
                    recs_path.write_text(json.dumps(rd, indent=2))
                    print(f"[deployer] marked {shipped_n} rec(s) as shipped tag={tag}",
                          file=sys.stderr)
                deploy_meta["shipped_rec_count"] = shipped_n
                _save()
            except Exception as e:
                print(f"[deployer] warn: shipped-marker write failed: {e}",
                      file=sys.stderr)

    except subprocess.TimeoutExpired:
        deploy_meta["status"] = "failure"
        deploy_meta["error"] = "step timed out"
        _save()
        sys.exit(2)
    except Exception as e:
        deploy_meta["status"] = "failure"
        deploy_meta["error"] = str(e)
        _save()
        raise


if __name__ == "__main__":
    main()
