#!/usr/bin/env python3
"""End-to-end test for site-quality-recommender agents (PI / CR).

Same flow as e2e_seo_flow.py but for agents whose run dirs live in framework
storage (agents/<id>/runs/<run_ts>/) and whose subject uses the
[<agent-id>:<request-id>] tag form.

Run:
  python3 tests/e2e_site_quality_flow.py --agent aisleprompt-progressive-improvement-agent
  python3 tests/e2e_site_quality_flow.py --agent specpicks-competitor-research-agent --skip-trigger
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

API = os.environ.get("FRAMEWORK_API_URL", "http://localhost:8093")
DATA = Path(os.path.expanduser(
    os.environ.get("AGENT_STORAGE_LOCAL_PATH", "~/.reusable-agents/data")
))
LOG_DIR = Path("/tmp/reusable-agents-logs")
SELF_ADDR = "automation@northernsoftwareconsulting.com"


def log(msg: str, **kw):
    extras = " ".join(f"{k}={v}" for k, v in kw.items())
    line = f"[e2e-sq {datetime.now().strftime('%H:%M:%S')}] {msg}"
    if extras:
        line += " " + extras
    print(line, flush=True)


def banner(t: str) -> None:
    print()
    print(f"=== {t} ===", flush=True)


def api_get(path: str, timeout: int = 10) -> dict:
    return json.loads(urllib.request.urlopen(f"{API}{path}", timeout=timeout).read())


def api_post(path: str, body: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body or {}).encode() if body is not None else b""
    req = urllib.request.Request(f"{API}{path}", data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


# ---------------------------------------------------------------------------

def cleanup_stale(agent_id: str) -> None:
    for p in Path("/tmp").glob("tmp.*"):
        try:
            if 0 < p.stat().st_size < 100_000:
                p.unlink()
        except Exception:
            pass
    sf = DATA / "agents" / agent_id / "status.json"
    if sf.is_file():
        try:
            s = json.loads(sf.read_text())
            if s.get("state") in ("running", "starting"):
                age_s = time.time() - sf.stat().st_mtime
                if age_s > 600:
                    s["state"] = "idle"
                    sf.write_text(json.dumps(s, indent=2))
                    log(f"  reset stale status for {agent_id}")
        except Exception:
            pass


# ---------------------------------------------------------------------------

def trigger(agent_id: str) -> str:
    log(f"trigger {agent_id}")
    r = api_post(f"/api/agents/{agent_id}/trigger")
    return r.get("run_id", "")


def wait_for_completion(agent_id: str, timeout: int = 1800, poll: int = 8) -> dict:
    log(f"waiting for {agent_id} state=success (timeout={timeout}s)")
    start = time.time()
    last_state = ""
    while time.time() - start < timeout:
        try:
            s = api_get(f"/api/agents/{agent_id}/status")
        except Exception as e:
            log(f"  status query failed: {e}")
            time.sleep(poll); continue
        state = s.get("state", "?")
        if state != last_state:
            log(f"  state={state} action={s.get('current_action','')[:60]} "
                f"msg={(s.get('message') or '')[:80]}")
            last_state = state
        if state == "success":
            return s
        if state == "failure":
            raise RuntimeError(f"{agent_id} failed: {s.get('message')}")
        time.sleep(poll)
    raise TimeoutError(f"{agent_id} did not complete in {timeout}s")


# ---------------------------------------------------------------------------

def find_latest_run_dir(agent_id: str) -> Path | None:
    base = DATA / "agents" / agent_id / "runs"
    if not base.is_dir():
        return None
    dirs = sorted(p for p in base.iterdir() if p.is_dir())
    return dirs[-1] if dirs else None


def find_outbound_email(agent_id: str) -> dict | None:
    base = DATA / "agents" / agent_id / "outbound-emails"
    if not base.is_dir():
        return None
    files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        try:
            d = json.loads(p.read_text())
            if d.get("kind", "email-recommendations") == "email-recommendations":
                return d
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------

def imap_inject_reply(*, agent_id: str, subject: str, body_text: str,
                      x_reusable_agent: str = "") -> None:
    """IMAP APPEND a synthetic reply that the responder will pick up."""
    import imaplib

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent /
                           "agents" / "responder-agent"))
    from importlib import import_module
    mint = import_module("mint-token")  # noqa

    oauth_file = Path(os.path.expanduser("~/.reusable-agents/responder/.oauth.json"))
    access_token, _, _ = mint.mint_access_token(oauth_file)
    sasl = mint.build_xoauth2_string(SELF_ADDR, access_token)

    msg = EmailMessage()
    msg["From"] = SELF_ADDR
    msg["To"] = SELF_ADDR
    msg["Subject"] = f"Re: {subject}"
    if x_reusable_agent or agent_id:
        msg["X-Reusable-Agent"] = x_reusable_agent or agent_id
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(body_text)

    conn = imaplib.IMAP4_SSL("outlook.office365.com", 993)
    conn.authenticate("XOAUTH2", lambda _: sasl)
    typ, _ = conn.append("INBOX", None, None, msg.as_bytes())
    if typ != "OK":
        raise RuntimeError(f"IMAP APPEND failed: {typ}")
    conn.logout()
    log(f"  reply IMAP-APPENDed (Subject: {msg['Subject']!r})")


# ---------------------------------------------------------------------------

def find_dispatch_log(agent_subject_tag: str, since_ts: float) -> Path | None:
    pat = f"dispatch-seo-implementer-*-*.log"
    candidates = [
        p for p in LOG_DIR.glob(pat)
        if p.stat().st_mtime > since_ts and agent_subject_tag in p.read_text(errors="ignore")[:200]
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def wait_for_implementer_done(dlog: Path, timeout: int = 3600, poll: int = 15) -> bool:
    log(f"waiting for implementer scope to finish (timeout={timeout}s) — {dlog.name}")
    start = time.time()
    while time.time() - start < timeout:
        try:
            text = dlog.read_text()
            if "[implementer] done" in text:
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


# ---------------------------------------------------------------------------

def find_completion_email(*, request_id: str,
                           implementer_id: str = "seo-implementer") -> dict | None:
    p = DATA / "agents" / implementer_id / "outbound-emails" / f"{request_id}.completion.json"
    return json.loads(p.read_text()) if p.is_file() else None


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", required=True,
                    help="agent id, e.g. aisleprompt-progressive-improvement-agent")
    ap.add_argument("--reply", default="implement critical and high")
    ap.add_argument("--skip-cleanup", action="store_true")
    ap.add_argument("--skip-trigger", action="store_true")
    ap.add_argument("--implementer-timeout", type=int, default=3600)
    args = ap.parse_args()

    agent_id = args.agent

    if not args.skip_cleanup:
        banner("Phase 0: cleanup")
        cleanup_stale(agent_id)

    if not args.skip_trigger:
        banner(f"Phase A: trigger {agent_id}")
        trigger(agent_id)
        banner("Phase B: wait for completion")
        wait_for_completion(agent_id, timeout=1800)

    banner("Phase C: verify recs")
    run_dir = find_latest_run_dir(agent_id)
    if not run_dir:
        log(f"  no run dir for {agent_id}")
        return 1
    recs_path = run_dir / "recommendations.json"
    if not recs_path.is_file():
        log(f"  no recommendations.json at {recs_path}")
        return 1
    recs = json.loads(recs_path.read_text()).get("recommendations", [])
    log(f"  run_ts={run_dir.name}  recs={len(recs)}")
    if not recs:
        log("  0 recs — aborting")
        return 1

    banner("Phase D: outbound email metadata")
    outbound = find_outbound_email(agent_id)
    if outbound:
        log(f"  request_id={outbound.get('request_id')} subject={outbound.get('subject')!r} "
            f"to={outbound.get('to')}")
    else:
        log("  NOT FOUND — completion-email recipient will fall back to site config")
        return 1

    since_ts = time.time()
    banner("Phase E: inject reply")
    imap_inject_reply(
        agent_id=agent_id, subject=outbound["subject"],
        body_text=args.reply,
    )

    banner("Phase F: trigger responder + wait for dispatch")
    log("waiting 30s for email to land")
    time.sleep(30)
    trigger("responder-agent")
    log("waiting 30s for responder tick")
    time.sleep(30)

    # The dispatch log filename pattern uses {site} from RESPONDER_SITE.
    # For PI/CR agents the site is derived by the responder from agent id
    # (e.g., aisleprompt). Fallback: scan all recent dispatches for our run_ts.
    log_path = None
    deadline = time.time() + 180
    while time.time() < deadline:
        candidates = sorted(LOG_DIR.glob("dispatch-seo-implementer-*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for p in candidates:
            if p.stat().st_mtime <= since_ts:
                continue
            try:
                head = p.read_text(errors="ignore")[:2000]
                if outbound["request_id"] in head or run_dir.name in head:
                    log_path = p; break
            except Exception:
                continue
        if log_path:
            break
        time.sleep(10)
    if not log_path:
        log("  NO DISPATCH LOG MATCHED — failing")
        # Show all recent dispatch logs for debugging
        for p in sorted(LOG_DIR.glob("dispatch-seo-implementer-*.log"), key=lambda x: x.stat().st_mtime)[-3:]:
            log(f"    recent: {p.name} (mtime {p.stat().st_mtime})")
        return 1
    log(f"  dispatch log: {log_path.name}")

    banner("Phase G: wait for implementer scope")
    ok = wait_for_implementer_done(log_path, timeout=args.implementer_timeout)
    if not ok:
        log("  implementer did NOT finish in time")
        return 1
    log("  implementer DONE")

    banner("Phase H: verify completion email")
    meta = find_completion_email(request_id=outbound["request_id"])
    if meta:
        log(f"  completion email tracked: ok={meta.get('ok')} to={meta.get('to')}")
        return 0
    else:
        # try with run_ts as request_id (legacy) — completion email module
        # uses RESPONDER_REQUEST_ID env which the responder sets to run_ts
        meta = find_completion_email(request_id=run_dir.name)
        if meta:
            log(f"  completion email (by run_ts) tracked: ok={meta.get('ok')} to={meta.get('to')}")
            return 0
    log("  NO completion-email metadata found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
