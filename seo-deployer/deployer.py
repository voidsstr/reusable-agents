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

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.site_config import load_config_from_env  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def run_step(name: str, cwd: str | None, cmd: str, env: dict | None = None,
             timeout: int = 1800) -> tuple[int, str, str]:
    """Run a shell command. Returns (rc, stdout, stderr)."""
    if not cmd:
        return 0, "", "(no command configured)"
    print(f"[deployer:{name}] cwd={cwd or '.'} cmd={cmd[:200]}", file=sys.stderr)
    proc = subprocess.run(
        ["bash", "-c", cmd],
        cwd=cwd or None, env={**os.environ, **(env or {})},
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
        if not args.skip_test:
            t = deployer.get("test", {})
            if t.get("cmd"):
                rc, _, stderr = run_step("test", t.get("cwd"), t["cmd"])
                deploy_meta["test"] = {"rc": rc, "skipped": False, "stderr_tail": stderr[-1000:]}
                if rc != 0:
                    deploy_meta["status"] = "blocked"
                    _save()
                    print(f"[deployer] TEST FAILED rc={rc}; aborting deploy", file=sys.stderr)
                    sys.exit(1)
            else:
                deploy_meta["test"]["skipped"] = True

        if args.dry_run:
            deploy_meta["status"] = "dry-run"; _save()
            return

        # ---- 2. Build ----
        b = deployer.get("build", {})
        if b.get("cmd"):
            rc, _, stderr = run_step("build", b.get("cwd"),
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
            rc, _, stderr = run_step("push", ps.get("cwd"),
                                     _expand(ps["cmd"], deploy_vars, tag, image))
            deploy_meta["push"] = {"rc": rc, "skipped": False, "stderr_tail": stderr[-1000:]}
            if rc != 0:
                deploy_meta["status"] = "failure"; _save()
                sys.exit(1)
        else:
            deploy_meta["push"]["skipped"] = True

        # ---- 4. Deploy ----
        d = deployer.get("deploy", {})
        if d.get("cmd"):
            rc, _, stderr = run_step("deploy", d.get("cwd"),
                                     _expand(d["cmd"], deploy_vars, tag, image))
            deploy_meta["deploy"] = {"rc": rc, "skipped": False, "stderr_tail": stderr[-1000:]}
            if rc != 0:
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
