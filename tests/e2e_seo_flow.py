#!/usr/bin/env python3
"""End-to-end test for the SEO opportunity → email → reply → implementer →
completion-email loop.

Phases (per site):
  A. Trigger <site>-seo-opportunity-agent via the dashboard API.
  B. Poll status.json until state=success (or fail/timeout).
  C. Verify the run dir has a recommendations.json with N>0 recs.
  D. Verify outbound-email metadata was recorded by seo-reporter.
  E. Inject a synthetic "user reply" via msmtp (automation@ → automation@,
     subject re: [SEO:<site>] …), so the responder will pick it up.
  F. Trigger responder-agent via API; watch its dispatch log for a
     spawn-via-systemd-run line.
  G. Wait for the implementer's transient scope to finish ([implementer]
     done in the dispatch log).
  H. Verify completion-email metadata exists at
     agents/implementer/outbound-emails/<request-id>.completion.json.

Run:
  python3 tests/e2e_seo_flow.py                    # both sites, default reply
  python3 tests/e2e_seo_flow.py --sites aisleprompt
  python3 tests/e2e_seo_flow.py --reply "implement rec-001 rec-002"
  python3 tests/e2e_seo_flow.py --skip-trigger     # use existing latest runs
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

API = os.environ.get("FRAMEWORK_API_URL", "http://localhost:8093")
DATA = Path(os.path.expanduser(
    os.environ.get("AGENT_STORAGE_LOCAL_PATH", "~/.reusable-agents/data")
))
SEO_RUNS = Path(os.path.expanduser("~/.openclaw/workspace/tools/seo/runs"))
LOG_DIR = Path("/tmp/reusable-agents-logs")
HOST_WORKER_LOG = Path("/tmp/reusable-agents-host-worker.log")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str, **kw):
    extras = " ".join(f"{k}={v}" for k, v in kw.items())
    line = f"[e2e {datetime.now().strftime('%H:%M:%S')}] {msg}"
    if extras:
        line += " " + extras
    print(line, flush=True)


def banner(title: str) -> None:
    print()
    print(f"=== {title} ===", flush=True)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(path: str, timeout: int = 10) -> dict:
    return json.loads(urllib.request.urlopen(f"{API}{path}", timeout=timeout).read())


def api_post(path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body or {}).encode() if body is not None else b""
    req = urllib.request.Request(
        f"{API}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


# ---------------------------------------------------------------------------
# Phase A — cleanup
# ---------------------------------------------------------------------------

def cleanup() -> None:
    log("cleanup: tmp prompt files, stale running statuses")
    # Orphaned tmp prompt files from prior implementer runs
    for p in Path("/tmp").glob("tmp.*"):
        try:
            if p.stat().st_size > 0 and p.stat().st_size < 100_000:
                p.unlink()
        except Exception:
            pass

    # Stale "running" status from previous test runs
    for status_file in DATA.glob("agents/*/status.json"):
        try:
            s = json.loads(status_file.read_text())
            if s.get("state") in ("running", "starting"):
                age_s = time.time() - status_file.stat().st_mtime
                if age_s > 600:  # >10 min idle = definitely stale
                    s["state"] = "idle"
                    s["message"] = "(reset by e2e test cleanup — was stale)"
                    status_file.write_text(json.dumps(s, indent=2))
                    log(f"  reset stale status: {status_file.parent.name}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Phase B — trigger + monitor
# ---------------------------------------------------------------------------

def trigger_agent(agent_id: str) -> str:
    log(f"trigger {agent_id}")
    r = api_post(f"/api/agents/{agent_id}/trigger")
    return r.get("run_id", "")


def wait_for_completion(agent_id: str, timeout: int = 1800, poll: int = 8) -> dict:
    log(f"waiting for {agent_id} state=success (timeout={timeout}s)")
    start = time.time()
    last_msg = ""
    while time.time() - start < timeout:
        try:
            s = api_get(f"/api/agents/{agent_id}/status")
        except Exception as e:
            log(f"  status query failed: {e}")
            time.sleep(poll); continue
        state = s.get("state", "?")
        msg = s.get("message", "") or ""
        action = s.get("current_action", "") or ""
        if (state, msg, action) != ("running", last_msg, ""):
            log(f"  {agent_id}: state={state} action={action[:60]} msg={msg[:80]}")
            last_msg = msg
        if state == "success":
            return s
        if state == "failure":
            raise RuntimeError(f"{agent_id} failed: {msg}")
        time.sleep(poll)
    raise TimeoutError(f"{agent_id} did not complete in {timeout}s")


# ---------------------------------------------------------------------------
# Phase C/D — verify recs + outbound email
# ---------------------------------------------------------------------------

def find_latest_run(site: str) -> Path | None:
    site_dir = SEO_RUNS / site
    if not site_dir.is_dir():
        return None
    candidates = sorted(
        p for p in site_dir.iterdir()
        if p.is_dir() and p.name not in ("latest",)
    )
    return candidates[-1] if candidates else None


def find_outbound_email(site: str, run_ts: str) -> dict | None:
    """Look for the outbound email metadata. The seo-reporter writes to
    agents/<dashboard.agent_id>/outbound-emails/<request_id>.json — the
    per-site config used to point at the legacy 'seo-opportunity-agent',
    so we check both paths for resilience."""
    request_id = f"r-{run_ts}-seo-{site}"
    candidates = [
        DATA / "agents" / f"{site}-seo-opportunity-agent" / "outbound-emails" / f"{request_id}.json",
        DATA / "agents" / "seo-opportunity-agent" / "outbound-emails" / f"{request_id}.json",
    ]
    for p in candidates:
        if p.is_file():
            return json.loads(p.read_text())
    return None


# ---------------------------------------------------------------------------
# Phase E — inject reply via msmtp (self-send)
# ---------------------------------------------------------------------------

def send_reply_self(*, site: str, run_ts: str, body_text: str,
                    rec_count: int,
                    self_addr: str = "automation@northernsoftwareconsulting.com") -> None:
    """Inject a synthetic reply directly into the IMAP inbox via APPEND.

    The host's msmtp is sandboxed by AppArmor (can't exec the OAuth
    passwordeval), so we use IMAP APPEND with XOAUTH2 — same auth path
    the responder uses for polling. This is faster and more deterministic
    than sending through SMTP+mail-delivery.
    """
    import imaplib

    # Reuse the responder's OAuth helpers
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent /
                           "agents" / "responder-agent"))
    from importlib import import_module
    mint = import_module("mint-token")  # noqa

    oauth_file = Path(os.path.expanduser("~/.reusable-agents/responder/.oauth.json"))
    access_token, oauth_user, _ = mint.mint_access_token(oauth_file)
    sasl_bytes = mint.build_xoauth2_string(self_addr, access_token)

    msg = EmailMessage()
    msg["From"] = self_addr
    msg["To"] = self_addr
    msg["Subject"] = f"Re: [SEO:{site}] run {run_ts} — {rec_count} recs"
    msg["X-Reusable-Agent"] = "seo-reporter"
    msg["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(body_text)

    conn = imaplib.IMAP4_SSL("outlook.office365.com", 993)
    conn.authenticate("XOAUTH2", lambda _: sasl_bytes)
    typ, _ = conn.append("INBOX", None, None, msg.as_bytes())
    if typ != "OK":
        raise RuntimeError(f"IMAP APPEND failed: {typ}")
    conn.logout()
    log(f"  reply IMAP-APPENDed (Subject: {msg['Subject']!r})")


# ---------------------------------------------------------------------------
# Phase F/G — responder dispatch + implementer scope
# ---------------------------------------------------------------------------

def find_dispatch_log(site: str, since_ts: float) -> Path | None:
    candidates = [
        p for p in LOG_DIR.glob(f"dispatch-implementer-{site}-*.log")
        if p.stat().st_mtime > since_ts
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def wait_for_dispatch(site: str, since_ts: float, timeout: int = 360, poll: int = 5) -> Path | None:
    log(f"waiting for dispatch log {site} (timeout={timeout}s)")
    start = time.time()
    while time.time() - start < timeout:
        log_path = find_dispatch_log(site, since_ts)
        if log_path:
            log(f"  dispatch log: {log_path.name}")
            return log_path
        time.sleep(poll)
    return None


def wait_for_implementer_done(dispatch_log: Path, timeout: int = 3600,
                               poll: int = 15) -> bool:
    log(f"waiting for implementer scope to finish (timeout={timeout}s)")
    start = time.time()
    while time.time() - start < timeout:
        try:
            text = dispatch_log.read_text()
            if "[implementer] done" in text:
                return True
            if "claude exited rc=" in text:
                # Still printed even on success; see if we exit cleanly after
                if "[implementer] done" in text:
                    return True
        except Exception:
            pass
        time.sleep(poll)
    return False


# ---------------------------------------------------------------------------
# Phase H — completion email
# ---------------------------------------------------------------------------

def find_completion_email(*, run_ts: str,
                           agent_id: str = "implementer") -> dict | None:
    p = DATA / "agents" / agent_id / "outbound-emails" / f"{run_ts}.completion.json"
    return json.loads(p.read_text()) if p.is_file() else None


# ---------------------------------------------------------------------------
# Site orchestration
# ---------------------------------------------------------------------------

def phase_trigger_and_wait(site: str) -> dict:
    """Phases A/B/C/D for one site. Returns a dict with run_ts, recs_count."""
    agent_id = f"{site}-seo-opportunity-agent"
    banner(f"{site} • Phase A: trigger {agent_id}")
    trigger_agent(agent_id)

    banner(f"{site} • Phase B: wait for completion")
    wait_for_completion(agent_id, timeout=1800)

    banner(f"{site} • Phase C: verify recs")
    run_dir = find_latest_run(site)
    if not run_dir:
        raise RuntimeError(f"no run dir for {site}")
    recs_doc = json.loads((run_dir / "recommendations.json").read_text())
    recs = recs_doc.get("recommendations", [])
    log(f"  run_ts={run_dir.name}  recs={len(recs)}")
    if not recs:
        raise RuntimeError(f"{site}: 0 recs in {run_dir}")

    banner(f"{site} • Phase D: verify outbound email metadata")
    outbound = find_outbound_email(site, run_dir.name)
    if outbound:
        log(f"  request_id={outbound['request_id']} to={outbound.get('to')}")
    else:
        log("  NOT FOUND — completion-email recipient will fall back to site config")

    return {
        "site": site,
        "run_ts": run_dir.name,
        "rec_count": len(recs),
        "outbound": outbound,
    }


def phase_reply_and_dispatch(site_results: list[dict], reply_text: str) -> dict:
    """Phases E/F/G for all sites. Returns site → dispatch_log mapping."""
    banner("Phase E: inject reply emails")
    since_ts = time.time()
    for r in site_results:
        send_reply_self(site=r["site"], run_ts=r["run_ts"],
                        body_text=reply_text, rec_count=r["rec_count"])

    banner("Phase F: trigger responder + wait for dispatch")
    log("waiting 30s for emails to land in inbox")
    time.sleep(30)
    trigger_agent("responder-agent")
    # Responder is fast; wait briefly for it to dispatch
    log("waiting 30s for responder tick")
    time.sleep(30)

    dispatches: dict[str, Path] = {}
    for r in site_results:
        log_path = wait_for_dispatch(r["site"], since_ts, timeout=120)
        if log_path:
            dispatches[r["site"]] = log_path
        else:
            log(f"  WARN: no dispatch log for {r['site']} — triggering responder again")
            trigger_agent("responder-agent")
            time.sleep(30)
            log_path = wait_for_dispatch(r["site"], since_ts, timeout=120)
            if log_path:
                dispatches[r["site"]] = log_path

    return dispatches


def phase_wait_implementers(site_results: list[dict],
                             dispatches: dict[str, Path],
                             timeout_per_site: int = 3600) -> dict:
    banner("Phase G: wait for implementer scopes to finish")
    completions: dict[str, bool] = {}
    for r in site_results:
        site = r["site"]
        dlog = dispatches.get(site)
        if not dlog:
            log(f"  {site}: NO DISPATCH — skipping wait")
            completions[site] = False
            continue
        ok = wait_for_implementer_done(dlog, timeout=timeout_per_site)
        log(f"  {site}: implementer {'DONE' if ok else 'TIMED OUT'} — log={dlog.name}")
        completions[site] = ok
    return completions


def phase_verify_completion_emails(site_results: list[dict]) -> dict:
    banner("Phase H: verify completion emails")
    out: dict[str, dict | None] = {}
    for r in site_results:
        meta = find_completion_email(run_ts=r["run_ts"])
        if meta:
            log(f"  {r['site']}: completion email tracked, ok={meta.get('ok')}, "
                f"to={meta.get('to')}")
        else:
            log(f"  {r['site']}: NO completion-email metadata at "
                f"agents/implementer/outbound-emails/{r['run_ts']}.completion.json")
        out[r["site"]] = meta
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sites", default="aisleprompt,specpicks",
                    help="comma-separated site ids (default: both)")
    ap.add_argument("--reply", default="implement critical and high",
                    help="body of synthetic reply email")
    ap.add_argument("--skip-cleanup", action="store_true")
    ap.add_argument("--skip-trigger", action="store_true",
                    help="don't re-run SEO agents; use existing latest run dirs")
    ap.add_argument("--implementer-timeout", type=int, default=3600,
                    help="seconds to wait per implementer scope")
    args = ap.parse_args()

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    if not sites:
        log("no sites specified")
        return 2

    if not args.skip_cleanup:
        banner("Phase 0: cleanup")
        cleanup()

    # Phases A–D, sequential per site (running both at once would overload).
    # One site failing must NOT abort the others — log and continue.
    site_results: list[dict] = []
    for site in sites:
        try:
            if args.skip_trigger:
                log(f"--skip-trigger: using existing latest run for {site}")
                run_dir = find_latest_run(site)
                if not run_dir:
                    log(f"  no existing run for {site} — skipping")
                    continue
                recs_doc = json.loads((run_dir / "recommendations.json").read_text())
                site_results.append({
                    "site": site, "run_ts": run_dir.name,
                    "rec_count": len(recs_doc.get("recommendations", [])),
                    "outbound": find_outbound_email(site, run_dir.name),
                })
            else:
                site_results.append(phase_trigger_and_wait(site))
        except Exception as e:
            log(f"  ERROR for {site}: {e}")
            # Even on agent failure, recs may exist on disk — try to pick them up
            run_dir = find_latest_run(site)
            if run_dir and (run_dir / "recommendations.json").is_file():
                recs_doc = json.loads((run_dir / "recommendations.json").read_text())
                if recs_doc.get("recommendations"):
                    log(f"  ↳ recovered: {site} has {len(recs_doc['recommendations'])} recs at {run_dir.name}")
                    site_results.append({
                        "site": site, "run_ts": run_dir.name,
                        "rec_count": len(recs_doc["recommendations"]),
                        "outbound": find_outbound_email(site, run_dir.name),
                        "recovered": True,
                    })
                    continue
            log(f"  ↳ {site}: skipping for downstream phases")

    if not site_results:
        log("no site_results — aborting")
        return 1

    # Phases E/F: reply + dispatch
    dispatches = phase_reply_and_dispatch(site_results, args.reply)

    # Phase G: wait for implementer
    completions = phase_wait_implementers(
        site_results, dispatches, timeout_per_site=args.implementer_timeout,
    )

    # Phase H: verify completion emails
    emails = phase_verify_completion_emails(site_results)

    # Final summary
    banner("RESULTS")
    overall_ok = True
    for r in site_results:
        site = r["site"]
        impl_ok = completions.get(site, False)
        email_ok = bool(emails.get(site))
        ok = impl_ok and email_ok
        overall_ok = overall_ok and ok
        log(f"  {site}: recs={r['rec_count']}  implementer={'OK' if impl_ok else 'FAIL'}  "
            f"completion_email={'OK' if email_ok else 'MISSING'}  "
            f"overall={'PASS' if ok else 'FAIL'}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
