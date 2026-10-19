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
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.site_config import load_config_from_env  # noqa: E402
from framework.core import deploy_gate  # noqa: E402
from framework.core import deploy_repair  # noqa: E402
from framework.core.implementer_scope import ScopePolicy  # noqa: E402
from framework.core.locks import site_deploy_lock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_runner as gate  # noqa: E402


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


def _infer_content_check(rec: dict, base_url: str) -> Optional[dict]:
    """Heuristic: derive a {url, contains} content-check spec from a rec
    when the producer didn't specify one explicitly.

    Returns None when we can't tell what to verify (then the deploy
    skips content-verify for that rec — better than false-positive).

    The mapping is per rec_type; new types fall through silently.
    """
    if not isinstance(rec, dict) or not base_url:
        return None
    rec_type = (rec.get("type") or rec.get("category") or "").lower()
    base = base_url.rstrip("/")

    # Affected URL — many producers attach it
    affected = rec.get("affected_url") or rec.get("page_url") or rec.get("url") or ""
    if affected.startswith("/"):
        affected = base + affected
    if not affected:
        # Fall back to evidence[0].url
        evid = rec.get("evidence") or []
        if isinstance(evid, list) and evid and isinstance(evid[0], dict):
            u = evid[0].get("url") or ""
            if u.startswith("/"): u = base + u
            affected = u

    # Type → marker. Add cases as we encounter them; missing maps return None.
    if rec_type in ("schema-markup", "schema-product-missing",
                    "schema-article-missing", "schema-faqpage-missing",
                    "schema-howto-missing", "schema-breadcrumblist-missing",
                    "schema-organization-missing"):
        return {"url": affected or base, "contains": "application/ld+json"}
    if rec_type == "ux-improvement" and "json-ld" in (rec.get("title") or "").lower():
        return {"url": affected or base, "contains": "application/ld+json"}
    if rec_type in ("onpage-canonical-missing", "onpage-canonical-issue"):
        return {"url": affected or base, "contains": '<link rel="canonical"', "regex": False}
    if rec_type == "onpage-twitter-card-missing":
        return {"url": affected or base, "contains": 'name="twitter:card"'}
    if rec_type in ("onpage-og-incomplete",):
        return {"url": affected or base, "contains": 'property="og:'}
    if rec_type == "h1-missing":
        return {"url": affected or base, "contains": "<h1", "regex": False}
    if rec_type == "indexing-sitemap-404":
        return {"url": base + "/sitemap.xml", "contains": "<urlset"}
    # Article-author proposals: verify the new article slug renders
    if rec_type == "article-author-proposal":
        slug = ((rec.get("article_proposal") or {}).get("slug")
                or (rec.get("proposal") or {}).get("slug") or "")
        if slug:
            # Heuristic: most article-author destinations are under /articles/
            # or /reviews/ or /buying-guide/. Try the most likely one.
            for prefix in ("/articles/", "/reviews/", "/buying-guide/", "/blog/"):
                return {"url": base + prefix + slug, "contains": slug}
    return None


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


_IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:@]*$")


def _capture_serving_ref(rb: dict, deploy_vars: dict, tag: str, image: str,
                        repo_root: str | None, *, what: str) -> str:
    """Run deployer.rollback.capture_cmd (e.g. `az containerapp show ...
    --query <image> -o tsv`) and return what the site is serving now (an
    image ref, or e.g. an ECS task-definition ARN). '' when not configured
    or unusable."""
    cmd = (rb or {}).get("capture_cmd")
    if not cmd:
        return ""
    rc, out, err = run_step("rollback-capture", None, _expand(cmd, deploy_vars, tag, image),
                            timeout=120, repo_root=repo_root)
    ref = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
    if rc != 0 or not _IMAGE_REF_RE.match(ref or "-"):
        print(f"[deployer] rollback capture ({what}) unusable (rc={rc}, out={ref[:120]!r})",
              file=sys.stderr)
        return ""
    return ref


def _capture_prior_image(rb: dict, deploy_vars: dict, tag: str, image: str,
                         repo_root: str | None) -> str:
    """The capture taken BEFORE the deploy — the rollback target."""
    ref = _capture_serving_ref(rb, deploy_vars, tag, image, repo_root, what="before deploy")
    if not ref and (rb or {}).get("capture_cmd"):
        print("[deployer] no rollback target; a failed gate will need a manual rollback",
              file=sys.stderr)
    return ref


def _rollback(rb: dict, deploy_vars: dict, tag: str, image: str, prior_image: str,
              repo_root: str | None, deploy_meta: dict, reason: str, *,
              deployed_ref: str = "") -> bool:
    """Run deployer.rollback.cmd with {prior_image}. True when it ran clean.

    `deployed_ref` is what capture_cmd returned right after THIS deploy. The
    rollback only replaces that: if the site now serves something else (a
    newer deploy, an operator's manual deploy, or a rollback that already
    happened), redeploying our prior image would clobber it, so we don't."""
    cmd = (rb or {}).get("cmd")
    if not (cmd and prior_image):
        deploy_meta["rollback"] = {"attempted": False, "reason": reason,
                                   "why_not": "no rollback.cmd" if not cmd else "prior image unknown"}
        return False
    if deployed_ref:
        now_ref = _capture_serving_ref(rb, deploy_vars, tag, image, repo_root, what="before rollback")
        if now_ref and now_ref != deployed_ref:
            deploy_meta["rollback"] = {
                "attempted": False, "reason": reason,
                "why_not": (f"site now serves {now_ref}, not this deploy's {deployed_ref} — "
                            f"a newer deploy or a manual rollback happened; not overwriting it"),
            }
            print(f"[deployer] NOT rolling back: {deploy_meta['rollback']['why_not']}", file=sys.stderr)
            return False
    expanded = _expand(cmd, {**deploy_vars, "prior_image": prior_image}, tag, image)
    print(f"[deployer] ROLLING BACK to {prior_image}: {reason}", file=sys.stderr)
    rc, _o, err = run_step("rollback", None, expanded, timeout=900, repo_root=repo_root)
    deploy_meta["rollback"] = {"attempted": True, "reason": reason, "prior_image": prior_image,
                               "rc": rc, "stderr_tail": (err or "")[-800:]}
    return rc == 0


def _gate_summary(snap: dict) -> dict:
    """Compact view of a deploy_gate snapshot for deploy.json."""
    pages = snap.get("pages", {})
    sample = snap.get("sample", {})
    return {
        "pages": pages,
        "sitemaps": snap.get("sitemaps", {}),
        "sample_bad": sum(1 for v in sample.values() if not v.get("indexable")),
        "sample_n": len(sample),
        "sample_rejects": {u: v.get("reason") for u, v in sample.items() if not v.get("indexable")},
    }


def _storage_or_none():
    try:
        from framework.core.storage import get_storage
        return get_storage()
    except Exception:
        return None


def _mark_shipped(run_dir: Path, deploy_meta: dict, tag: str, image: str, state) -> None:
    """The implementer marked the batch's recs `implemented`; their code is now
    live in this (or, for already-live, an earlier) deploy. Flip `shipped`."""
    recs_path = run_dir / "recommendations.json"
    if not recs_path.is_file():
        return
    try:
        rd = json.loads(recs_path.read_text())
        # recommendations.json ships in two shapes across producers: a bare
        # list, or {"recommendations": [...]}. Handle both.
        if isinstance(rd, list):
            rec_list = rd
        elif isinstance(rd, dict):
            rec_list = rd.get("recommendations", [])
            if not isinstance(rec_list, list):
                rec_list = []
        else:
            rec_list = []
        shipped_at = _now_iso()
        shipped_n = 0
        for r in rec_list:
            if not isinstance(r, dict):
                continue
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
    except Exception as e:
        print(f"[deployer] warn: shipped-marker write failed: {e}",
              file=sys.stderr)


def _mail(cfg, subject: str, body_html: str) -> bool:
    """Send an operator alert. True only when the mail actually went out."""
    em = (cfg.get("reporter") or {}).get("email") or {}
    to = em.get("to") or []
    sender = em.get("from") or ""
    m = re.search(r"<([^>]+)>", sender)
    sender = m.group(1) if m else sender
    if not to or not sender:
        print("[deployer] no reporter.email in the site config; not alerting", file=sys.stderr)
        return False
    try:
        from shared.site_quality import send_via_msmtp
        ok, detail = send_via_msmtp(subject=subject, body_html=body_html, to=list(to),
                                    sender=sender, msmtp_account=em.get("msmtp_account") or "automation",
                                    bypass_digest=True)
        delivered = bool(ok) and not str(detail).startswith(("digest disabled", "suppressed:"))
        print(f"[deployer] alert {'sent' if delivered else 'NOT sent'}: {detail}", file=sys.stderr)
        return delivered
    except Exception as e:
        print(f"[deployer] alert failed: {e}", file=sys.stderr)
        return False


def _alert_if_due(cfg, state, signature: str, outcome, repair_cfg: dict, run_dir: Path) -> None:
    """Email the operator about a red gate, de-duplicated per failure
    signature (deploy_repair.GateState.should_alert): a code failure once a
    repair cycle is spent, any other red once the site has been red for
    alert_red_for_s. Recorded only when the mail really went out."""
    if not signature or not state.should_alert(signature, repair_cfg):
        return
    import html as _html
    entry = state.entry(signature)
    rows = "".join(
        f"<li><b>{_html.escape(f['title'])}</b> <code>{_html.escape(f.get('file') or '')}"
        f":{f.get('line') or ''}</code>{' (did not run)' if f.get('did_not_run') else ''}"
        f"<pre>{_html.escape((f.get('error') or '')[:800])}</pre></li>"
        for f in (outcome.summary().get("failures") or [])[:10])
    reps = "".join(f"<li>{_html.escape(r['note'])}</li>" for r in outcome.repairs)
    body = (
        f"<p>The pre-deploy test gate for <b>{_html.escape(cfg.site_id)}</b> is red, and the "
        f"deployer could not get it green. Nothing deploys for this site until it is.</p>"
        f"<p><b>Why:</b> {_html.escape(outcome.status_reason)}<br>"
        f"<b>Commit:</b> {_html.escape(outcome.sha[:12])} · <b>signature:</b> {_html.escape(signature)} "
        f"({_html.escape(entry.get('kind') or '')}, {entry.get('cycles') or 0} spent cycle(s)) · "
        f"<b>red since:</b> {_html.escape(state.red_since)} · <b>next repair attempt:</b> "
        f"{_html.escape(entry.get('next_repair_at') or 'next deploy')}</p>"
        f"<p><b>Failing tests</b></p><ul>{rows or '<li>(no test-level failures)</li>'}</ul>"
        f"<p><b>Repair attempts this run</b></p><ul>{reps or '<li>none</li>'}</ul>"
        f"<p>Context: <code>{_html.escape(str(run_dir))}/test-failure-context.json</code>; gate state: "
        f"<code>{_html.escape(str(deploy_repair.state_dir()))}/{_html.escape(cfg.site_id)}.json</code>. "
        f"Turn repair off with storage <code>{deploy_repair.CONFIG_KEY}</code> "
        f"<code>{{\"enabled\": false}}</code>.</p>")
    if _mail(cfg, f"[deploy-gate:{cfg.site_id}] RED — {outcome.status_reason[:90]}", body):
        state.record_alert(signature)


def _alert_landing(cfg, state, signature: str, repair_cfg: dict, ref: str, detail: str,
                   run_dir: Path) -> None:
    """A green repair that could not be landed needs a human: alert at once,
    then at most every realert_s for the same failure."""
    import html as _html
    if not state.should_alert(signature, repair_cfg):
        return
    if _mail(cfg, f"[deploy-gate:{cfg.site_id}] repair green but not landed",
             f"<p>The deploy gate for <b>{_html.escape(cfg.site_id)}</b> was repaired and is green, "
             f"but the repair commits could not be put on the live branch, so nothing was deployed.</p>"
             f"<p>The repair is kept at <code>{_html.escape(ref)}</code>; cherry-pick it by hand.</p>"
             f"<pre>{_html.escape(detail[:2000])}</pre><p>Run dir: <code>{_html.escape(str(run_dir))}</code></p>"):
        state.record_alert(signature)


def _on_sigterm(signum, frame):  # noqa: ARG001
    # A kill from the dispatcher or systemd must still release the lock, remove
    # the worktree and save the gate state (the finally blocks).
    raise SystemExit(143)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--skip-test", action="store_true",
                   help="Skip the test step (DANGEROUS — for manual rollback / re-deploy)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print commands but don't execute")
    args = p.parse_args()
    signal.signal(signal.SIGTERM, _on_sigterm)

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

    deploy_lock = None
    wt_cleanup = lambda: None  # noqa: E731
    try:
        # ---- 0. One gate + deploy per target at a time ----
        # Held from the first test through smoke/rollback (released in the
        # outer finally). Taken BEFORE the tests since 2026-10-19: same-site
        # runs used to test in parallel on one fixed container and kill each
        # other's suite (rc=4, six blocked deploys in four days), and two
        # overlapping deploys of one app fail each other's smoke and roll each
        # other back (site_deploy_lock).
        target = str(deploy_vars.get("app") or cfg.site_id)
        lock_wait_s = int(os.environ.get("DEPLOYER_LOCK_WAIT_S", "9000"))
        deploy_lock = site_deploy_lock(target, timeout_s=lock_wait_s)
        try:
            gate.acquire_lock(deploy_lock, lock_wait_s,
                              "another gate or deploy of this target is in flight")
        except TimeoutError as e:
            deploy_meta["status"] = "failure"
            deploy_meta["error"] = f"deploy lock: {e}"
            _save()
            print(f"[deployer] {e}; not deploying alongside it", file=sys.stderr)
            sys.exit(1)

        # The image tag is stamped once the lock is ours: a run that waited
        # must not reuse the minute it started in (that is another run's tag).
        tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        deploy_meta["tag"] = tag

        state = deploy_repair.GateState.load(cfg.site_id)
        b = deployer.get("build", {}) or {}
        # Another run may have tested and shipped this very commit while we
        # waited for the lock. Its image is live, so are our commits.
        head_now = gate.rev(repo_root) if repo_root else ""
        if (head_now and head_now == state.last_deployed_sha and not args.dry_run
                and not args.skip_test and os.environ.get("DEPLOYER_FORCE") != "1"):
            live_tag = state.last_deployed_tag or tag
            deploy_meta.update({"status": "already-live", "sha": head_now, "tag": live_tag})
            print(f"[deployer] {head_now[:12]} is already live ({image}:{live_tag}, deployed by an "
                  f"earlier run); nothing to build", file=sys.stderr)
            _mark_shipped(run_dir, deploy_meta, live_tag, image, state)
            _save()
            return

        # ---- 0b. The commit to test and ship ----
        # One clean checkout of HEAD serves the gate AND the build, so what is
        # tested is exactly what ships: never another agent's uncommitted edit
        # in the live tree (2026-09-25 shipped one), never a commit that landed
        # after the tests passed.
        gate.sweep_stale_worktrees(repo_root, cfg.site_id)
        wt, ship_sha, wt_cleanup, isolated = gate.checkout(
            repo_root, cfg.site_id, enabled=b.get("from_clean_checkout", True))
        wt = wt or repo_root or os.getcwd()
        gate_base = ship_sha
        reverted: list = []
        deploy_meta["sha"] = ship_sha
        tested_image = ""

        # ---- 1. Test -> repair -> retest (hard gate) ----
        # Tiered scope: deployer.test.cmd runs every deploy (legacy); a
        # smoke/full pair picks per DEPLOYER_TEST_SCOPE / test_scope /
        # full_interval_days (_select_test_scope). A red gate is classified
        # first. Infra failures are retried. Code failures get Opus repair
        # attempts (agents/deployer/REPAIR.md) in the checkout, each vetted by
        # deploy_repair.check_repair and re-tested. Only a green gate continues.
        if args.skip_test:
            print("[deployer] WARNING: --skip-test — deploying WITHOUT the test gate", file=sys.stderr)
        else:
            t = deployer.get("test", {}) or {}
            scope, test_cmd, test_cwd = _select_test_scope(
                t, cfg.site_id, run_dir,
            )
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
                repair_cfg = deploy_repair.load_config(
                    cfg.site_id, deployer.get("repair"), storage=_storage_or_none())
                runner = gate.GateRunner(
                    site_id=cfg.site_id, tag=tag, test_cmd=test_cmd, test_cwd=test_cwd,
                    worktree=wt, repo_root=repo_root or wt,
                    branch=implementer_block.get("branch") or "",
                    scope=ScopePolicy.from_site_config({"implementer": implementer_block},
                                                       dispatch_kind="deploy-repair"),
                    cfg=repair_cfg, state=state, run_dir=run_dir,
                    test_timeout_s=t.get("timeout_s"), isolated=isolated,
                    # A dry run only reports, unless asked to repair too.
                    allow_repair=(not args.dry_run) or os.environ.get("DEPLOY_REPAIR_IN_DRY_RUN") == "1")
                outcome = runner.run()
                summary = outcome.summary()
                (run_dir / "gate.json").write_text(json.dumps(summary, indent=2))
                deploy_meta["test"] = {
                    "rc": 0 if outcome.ok else (summary.get("rc") if summary.get("rc") is not None else 1),
                    "skipped": False, "scope": scope, "gate": summary,
                }
                if not outcome.ok:
                    deploy_meta["status"] = "blocked"
                    fail_ctx = {
                        "scope": scope, "rc": deploy_meta["test"]["rc"], "cmd": test_cmd,
                        "cwd": test_cwd, "kind": summary.get("kind"), "reason": outcome.status_reason,
                        "signature": outcome.signature, "failures": summary.get("failures"),
                        "artifacts": [r.get("artifacts") for r in outcome.runs],
                        "repairs": outcome.repairs, "captured_at": _now_iso(),
                    }
                    (run_dir / "test-failure-context.json").write_text(json.dumps(fail_ctx, indent=2))
                    _alert_if_due(cfg, state, outcome.signature, outcome, repair_cfg, run_dir)
                    state.save()
                    _save()
                    print(f"[deployer] TEST GATE RED scope={scope}: {outcome.status_reason}; "
                          f"not deploying (context: {run_dir}/test-failure-context.json)",
                          file=sys.stderr)
                    sys.exit(1)
                if outcome.flaky:
                    print(f"[deployer] gate green with {len(outcome.flaky)} flaky test(s): "
                          f"{outcome.flaky[:5]}", file=sys.stderr)
                if not outcome.repaired:
                    state.record_green(outcome.sha, repaired=False)
                elif args.dry_run and os.environ.get("DEPLOYER_DRY_RUN_LAND") != "1":
                    # A dry run never moves the live branch.
                    ref = f"refs/deploy-repair/{cfg.site_id}/{tag}"
                    subprocess.run(["git", "-C", repo_root or wt, "update-ref", ref, outcome.sha],
                                   capture_output=True)
                    deploy_meta["test"]["gate"]["landed"] = f"dry run: kept at {ref}, not landed"
                    print(f"[deployer] dry run: repair kept at {ref}, not landed", file=sys.stderr)
                else:
                    landed_ok, landed = gate.land(repo_root or wt, implementer_block.get("branch") or "",
                                                  gate_base, outcome.sha)
                    deploy_meta["test"]["gate"]["landed"] = landed
                    if not landed_ok:
                        ref = f"refs/deploy-repair/{cfg.site_id}/{tag}"
                        subprocess.run(["git", "-C", repo_root or wt, "update-ref", ref, outcome.sha],
                                       capture_output=True)
                        deploy_meta["status"] = "blocked"
                        deploy_meta["error"] = f"repair is green but could not land on the branch: {landed}"
                        # The code failure keeps its backoff (no fresh Opus repair
                        # every dispatch), and the landing problem alerts once.
                        code_c = outcome.classification or deploy_repair.Classification("code", "", 1)
                        land_c = deploy_repair.Classification(
                            "landing", f"repair kept at {ref}; could not land: {landed[:200]}", 1)
                        land_sig = "landing-" + (outcome.signature or deploy_repair.failure_signature(land_c))
                        if outcome.signature:
                            state.record_red(code_c, outcome.signature, attempts=0, exhausted=True,
                                             cfg=repair_cfg)
                        state.record_red(land_c, land_sig, attempts=0, exhausted=True, cfg=repair_cfg)
                        _alert_landing(cfg, state, land_sig, repair_cfg, ref, landed, run_dir)
                        state.save()
                        _save()
                        print(f"[deployer] {deploy_meta['error']}; kept at {ref}", file=sys.stderr)
                        sys.exit(1)
                    state.record_green(outcome.sha, repaired=True)
                    reverted = gate.reverted_batch_files(repo_root or wt,
                                                         os.environ.get("DEPLOYER_BATCH_BASE_SHA", ""),
                                                         gate_base, outcome.sha)
                    if reverted:
                        deploy_meta["test"]["gate"]["reverted_batch_files"] = reverted
                        print(f"[deployer] the repair restored {len(reverted)} file(s) the batch had "
                              f"changed ({reverted[:5]}); this batch's recs are NOT marked shipped",
                              file=sys.stderr)
                    print(f"[deployer] gate repaired and green; repair {landed}", file=sys.stderr)
                state.save()
                ship_sha = outcome.sha
                deploy_meta["sha"] = ship_sha
                tested_image = outcome.image_id
                if scope == "full":
                    _record_last_full_test(cfg.site_id)
            else:
                deploy_meta["test"]["skipped"] = True

        if args.dry_run:
            deploy_meta["status"] = "dry-run"; _save()
            return

        # ---- 2. Build ----
        # The image the gate tested is the image that ships: tag it rather than
        # rebuild (a second build used a different builder and cache, and its
        # npm layers differed). Without a tested image (--skip-test, no test
        # command, a gate that is not run-local-suite) build from the checkout.
        if tested_image and b.get("cmd") and b.get("reuse_tested_image", True):
            rc, _, stderr = run_step("build", None, repo_root=wt,
                                     cmd=f"docker tag {shlex.quote(tested_image)} {shlex.quote(image + ':' + tag)}")
            deploy_meta["build"] = {"rc": rc, "skipped": False, "stderr_tail": stderr[-1000:],
                                    "from_clean_checkout": wt != repo_root,
                                    "reused_tested_image": tested_image}
            if rc != 0:
                deploy_meta["status"] = "failure"; _save()
                sys.exit(1)
        elif b.get("cmd"):
            rc, _, stderr = run_step("build", b.get("cwd"), repo_root=wt, cmd=
                                     _expand(b["cmd"], deploy_vars, tag, image))
            deploy_meta["build"] = {"rc": rc, "skipped": False, "stderr_tail": stderr[-1000:],
                                    "from_clean_checkout": wt != repo_root}
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
        # ---- 3b. Pre-deploy baseline + rollback target ----
        # The post-deploy gate fails only on REGRESSIONS against this
        # baseline, so a page/sitemap that is already broken never blocks
        # the deploy that may fix it (framework/core/deploy_gate.py).
        sc = deployer.get("smoke_check", {}) or {}
        gate_cfg = sc.get("gate") or None
        rb = deployer.get("rollback") or {}
        gate_pre = None
        if gate_cfg and sc.get("base_url") and d.get("cmd") and gate_cfg.get("baseline", True):
            try:
                gate_pre = deploy_gate.measure(sc["base_url"], gate_cfg)
                deploy_meta["gate"] = {"pre": _gate_summary(gate_pre)}
            except Exception as e:
                print(f"[deployer] gate baseline failed ({e}); the post-deploy gate "
                      f"will report but not roll back (no baseline to compare)", file=sys.stderr)
        prior_image = _capture_prior_image(rb, deploy_vars, tag, image, repo_root) if d.get("cmd") else ""
        deployed_ref = ""
        if prior_image:
            deploy_meta["prior_image"] = prior_image
            if rb.get("cmd"):
                deploy_meta["rollback_cmd"] = _expand(rb["cmd"], {**deploy_vars, "prior_image": prior_image}, tag, image)
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
            # What this deploy put in place — a later rollback replaces only
            # that, never a newer deploy that landed meanwhile.
            if prior_image and rb.get("cmd"):
                deployed_ref = _capture_serving_ref(rb, deploy_vars, tag, image, repo_root,
                                                    what="after deploy")
                if deployed_ref:
                    deploy_meta["deployed_ref"] = deployed_ref
        else:
            deploy_meta["deploy"]["skipped"] = True

        # ---- 5. Smoke check (URL-level + content-level) ----
        sc = deployer.get("smoke_check", {})
        if sc.get("base_url") and sc.get("paths"):
            # Cold-start grace period — Container Apps takes 30-60s to
            # warm up after a revision swap. Increased from 20s after
            # repeated false-positive smoke failures.
            settle_s = int(sc.get("settle_seconds", 60))
            print(f"[deployer] settling for {settle_s}s before smoke", file=sys.stderr)
            time.sleep(settle_s)
            ok, results = smoke_check(
                sc["base_url"], sc["paths"], int(sc.get("timeout_seconds", 30)),
            )
            deploy_meta["smoke"] = {"ok": ok, "results": results}
            if not ok:
                # Retry once with a longer timeout — this is the cold-start
                # window for Azure Container Apps. Many false positives
                # historically came from this race.
                print(f"[deployer] smoke failed first attempt, retrying once after 30s", file=sys.stderr)
                time.sleep(30)
                ok, results = smoke_check(
                    sc["base_url"], sc["paths"], int(sc.get("timeout_seconds", 60)),
                )
                deploy_meta["smoke"] = {"ok": ok, "results": results, "retried": True}
            if not ok:
                bad = [r["url"] for r in results if not r["ok"]]
                rolled = rb.get("on_smoke_failure", True) and _rollback(
                    rb, deploy_vars, tag, image, prior_image, repo_root, deploy_meta,
                    f"smoke check failed: {bad}", deployed_ref=deployed_ref)
                deploy_meta["status"] = "rolled-back" if rolled else "failure"; _save()
                print(f"[deployer] SMOKE FAILED — "
                      f"{'rolled back to ' + prior_image if rolled else 'manual rollback needed'}",
                      file=sys.stderr)
                sys.exit(1)

            # ── Indexability + latency gate (regressions only) ──────
            # A 200 is not "working": the 99.8%-noindex release and the
            # shop-sitemap 500 both passed the URL smoke above.
            if gate_cfg:
                try:
                    post = deploy_gate.measure(sc["base_url"], gate_cfg,
                                               sample_urls=(gate_pre or {}).get("sample_urls"))
                    failures, warnings = deploy_gate.compare(gate_pre, post, gate_cfg)
                except Exception as e:
                    # A broken gate must not fail (or roll back) a good deploy.
                    print(f"[deployer] gate errored ({e}); not gating this deploy", file=sys.stderr)
                    post, failures, warnings = {}, [], [f"gate errored: {e}"]
                if failures and gate_pre is None and gate_cfg.get("baseline", True) is not False:
                    # The baseline was meant to be taken but wasn't (it errored,
                    # or there was no deploy step). Without it an already-broken
                    # page or sitemap is indistinguishable from a regression, so
                    # report — never roll back a deploy on that.
                    warnings = [f"(no baseline — not gating) {f}" for f in failures] + warnings
                    failures = []
                if failures:
                    # Re-measure before acting: a cold revision and a transient
                    # 5xx must fail again. A latency-only failure gets three
                    # re-measures over ~5 minutes. A new revision's cache
                    # warmers share its one node process, and /buying-guides
                    # read 4.3 s and 9.1 s warm a minute after two 2026-10-19
                    # deploys (0.12 s once warm), so both rolled back. A page
                    # still over budget after that is a regression.
                    latency_only = all(re.search(r"\bTTFB\b", f) for f in failures)
                    for wait_s in ([45, 90, 180] if latency_only else [45]):
                        print(f"[deployer] gate failed ({len(failures)}): "
                              f"{failures[:5]}; re-measuring in {wait_s}s", file=sys.stderr)
                        time.sleep(wait_s)
                        try:
                            post = deploy_gate.measure(sc["base_url"], gate_cfg,
                                                       sample_urls=(gate_pre or {}).get("sample_urls"))
                            failures, warnings = deploy_gate.compare(gate_pre, post, gate_cfg)
                        except Exception as e:
                            print(f"[deployer] gate re-measure errored ({e}); keeping the previous result",
                                  file=sys.stderr)
                        if not failures:
                            print("[deployer] gate passed on re-measure", file=sys.stderr)
                            break
                deploy_meta.setdefault("gate", {})
                deploy_meta["gate"].update({"post": _gate_summary(post), "failures": failures,
                                            "warnings": warnings, "ok": not failures})
                for w in warnings:
                    print(f"[deployer] gate warning: {w}", file=sys.stderr)
                if failures:
                    rolled = rb.get("on_gate_failure", True) and _rollback(
                        rb, deploy_vars, tag, image, prior_image, repo_root, deploy_meta,
                        f"indexability/latency gate: {failures[:5]}", deployed_ref=deployed_ref)
                    deploy_meta["status"] = "rolled-back" if rolled else "failure"; _save()
                    print(f"[deployer] INDEXABILITY GATE FAILED — {failures}", file=sys.stderr)
                    print(f"[deployer] "
                          f"{'rolled back to ' + prior_image if rolled else 'manual rollback needed'}",
                          file=sys.stderr)
                    sys.exit(1)

            # ── Content-level verification (per-rec) ────────────────
            # Previous smoke just checked URLs respond 200. That passes
            # even when the rec's *intent* didn't actually ship (e.g.
            # the LLM created a JSON-LD component but didn't import it,
            # so /buying-guide/* still emits zero ld+json tags).
            #
            # For each rec we just shipped, look at its `content_check`
            # block (set by the producer agent or inferred from rec.type)
            # and curl + grep the affected URL for the expected marker.
            # If grep fails, we know the LLM produced orphan code or
            # missed the integration step — fail the deploy so the
            # dispatch retries with stronger Rule 8 prompting.
            #
            # Disabled by default per-deployment if you want the old
            # behavior: set RESPONDER_SKIP_CONTENT_VERIFY=1.
            if os.environ.get("RESPONDER_SKIP_CONTENT_VERIFY") != "1":
                cv_results = []
                cv_failed = []
                try:
                    recs_doc = (run_dir / "recommendations.json")
                    if recs_doc.is_file():
                        recs_data = json.loads(recs_doc.read_text())
                        recs_list = (recs_data.get("recommendations")
                                     if isinstance(recs_data, dict) else recs_data) or []
                        for r in recs_list:
                            if not isinstance(r, dict): continue
                            if not r.get("implemented"): continue
                            # Skip if this rec doesn't carry a content_check spec
                            cc = r.get("content_check") or _infer_content_check(r, sc.get("base_url",""))
                            if not cc: continue
                            url = cc.get("url"); needle = cc.get("contains")
                            if not (url and needle): continue
                            try:
                                req = urllib.request.Request(url,
                                    headers={"User-Agent":"seo-deployer-content-check/1.0"})
                                with urllib.request.urlopen(req, timeout=30) as resp:
                                    body = resp.read().decode("utf-8","replace")
                                ok_cv = needle.lower() in body.lower() if not cc.get("regex") \
                                        else bool(re.search(needle, body, re.I))
                            except Exception as e:
                                body = ""; ok_cv = False
                            cv_results.append({
                                "rec_id": r.get("id"), "url": url, "needle": needle,
                                "ok": ok_cv,
                            })
                            if not ok_cv:
                                cv_failed.append(r.get("id"))
                except Exception as e:
                    print(f"[deployer] content-verify skipped: {e}", file=sys.stderr)

                deploy_meta["content_verify"] = {
                    "results": cv_results,
                    "failed_rec_ids": cv_failed,
                }
                if cv_failed:
                    deploy_meta["status"] = "failure"; _save()
                    print(f"[deployer] CONTENT VERIFY FAILED for recs: {cv_failed}", file=sys.stderr)
                    print(f"[deployer] The deploy succeeded (URLs return 200) but the"
                          f" expected content is NOT visible. Likely orphan code or missed integration.", file=sys.stderr)
                    sys.exit(1)

        deploy_meta["status"] = "success"
        if not deploy_meta.get("rollback_cmd"):
            deploy_meta["rollback_cmd"] = _expand(d.get("cmd", ""), deploy_vars, "<PRIOR_TAG>", image)
        _save()
        print(f"[deployer] deployed {image}:{tag}", file=sys.stderr)

        # ── Push the deployed commits, then tag the release ──────────────
        # The implementer commits locally and nothing pushed, so both site
        # repos silently accumulated 180-265 unpushed commits: work that was
        # live in production but existed on exactly one machine. Push at the
        # moment we know the code is deployed, then stamp an incrementing
        # release/<site>/NNNN tag so `git tag` alone answers "what went live,
        # when, and how many releases ago".
        #
        # Neither step may fail a deploy that already succeeded: a diverged
        # branch is a normal state here, and the tag still records the exact
        # deployed commit even when the branch cannot fast-forward.
        if repo_root:
            try:
                _pusher = Path(__file__).resolve().parents[2] / "install" / "push-unpushed.sh"
                if _pusher.is_file():
                    _pr = subprocess.run(
                        ["bash", str(_pusher), repo_root],
                        capture_output=True, text=True, timeout=300)
                    for _line in (_pr.stderr or "").splitlines():
                        if _line.strip():
                            print(f"[deployer] {_line.strip()}", file=sys.stderr)
                else:
                    print(f"[deployer] WARNING: push-unpushed.sh not found at {_pusher}", file=sys.stderr)
            except Exception as e:
                print(f"[deployer] WARNING: push skipped ({e})", file=sys.stderr)

            try:
                _tagger = Path(__file__).resolve().parents[2] / "install" / "tag-release.sh"
                if _tagger.is_file():
                    _tr = subprocess.run(
                        ["bash", str(_tagger), cfg.site_id, tag],
                        cwd=repo_root, capture_output=True, text=True, timeout=180,
                        env={**os.environ, "RELEASE_SHA": deploy_meta.get("sha") or ""})
                    for _line in (_tr.stdout or "").splitlines() + (_tr.stderr or "").splitlines():
                        if _line.strip():
                            print(f"[deployer] {_line.strip()}", file=sys.stderr)
                else:
                    print(f"[deployer] WARNING: tag-release.sh not found at {_tagger}", file=sys.stderr)
            except Exception as e:
                print(f"[deployer] WARNING: release tag skipped ({e})", file=sys.stderr)

        if deploy_meta.get("sha"):
            state.record_deployed(deploy_meta["sha"], tag)
            state.save()
        if not reverted:
            _mark_shipped(run_dir, deploy_meta, tag, image, state)
        _save()

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
    finally:
        wt_cleanup()
        if deploy_lock is not None:
            deploy_lock.release()


if __name__ == "__main__":
    main()
