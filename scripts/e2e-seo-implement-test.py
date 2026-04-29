#!/usr/bin/env python3
"""End-to-end test for the SEO recommend → reply → implement chain.

What this script does:

  1. Detects the most recent SEO outbound-email record in Azure for a
     given site (default: aisleprompt). Polls until found, since the
     SEO agent run typically takes 15-30 minutes.

  2. Once the email is recorded as outbound, sends a synthetic REPLY
     via Microsoft Graph /sendMail using the responder's OAuth refresh
     token (the same automation@nsc identity that the agents use).
     Subject is `Re: <original subject>`, body is `implement all
     changes`. To/From: automation@nsc (same inbox the responder polls,
     so the message lands in IMAP within seconds).

  3. Watches the responder's log for `[recorded]` markers as it
     processes the reply.

  4. Watches the responses-archive in Azure for the responded-to
     marker (proves the dashboard's Confirmations page will mark
     this email as "responded").

  5. Watches dispatch-batches.json + responses-queue + implementer
     dispatch logs. Reports per-batch status (running / paused /
     completed) and any deployer/test failures.

  6. Exits when the chain finishes (all batches reach a terminal
     state) or after `--timeout-min` minutes.

Usage:

  python3 scripts/e2e-seo-implement-test.py --site aisleprompt
  python3 scripts/e2e-seo-implement-test.py --site aisleprompt --reply-only
  python3 scripts/e2e-seo-implement-test.py --site aisleprompt --watch-only

Env / files used:

  ~/.reusable-agents/responder/.oauth.json — Graph OAuth refresh token
  AZURE_STORAGE_CONNECTION_STRING in env or in reusable-agents/.env
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Bootstrap path so framework + agents modules import.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Load AZURE_STORAGE_CONNECTION_STRING from .env if not in env.
def _load_env():
    if os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        return
    env = REPO / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            if k and v and k not in os.environ:
                os.environ[k] = v.strip()

_load_env()

from framework.core.storage import get_storage  # noqa: E402

# Mint helper from responder. The file is hyphenated (`mint-token.py`)
# so we import it via importlib.
sys.path.insert(0, str(REPO / "agents" / "responder-agent"))
from importlib import import_module
mint = import_module("mint-token")  # type: ignore  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str, level: str = "info") -> None:
    icon = {"info": "·", "ok": "✓", "warn": "!", "err": "✗", "step": "▶"}.get(level, "·")
    print(f"[{stamp()}] {icon} {msg}", flush=True)


# ── Step 1: find latest SEO outbound email ────────────────────────────────

def find_seo_email(site: str, since_iso: str | None = None,
                    poll_sec: int = 30, max_wait_min: int = 45) -> dict | None:
    """Wait for the most recent SEO outbound-email record for the site."""
    s = get_storage()
    agent_id = f"{site}-seo-opportunity-agent"
    prefix = f"agents/{agent_id}/outbound-emails/"
    deadline = time.time() + max_wait_min * 60
    last_seen_count = -1
    while time.time() < deadline:
        keys = list(s.list_prefix(prefix))
        # Filter out responses-archive (which lives at responses-archive/, but
        # belt-and-braces) and only look at email-recommendations records.
        candidates: list[dict] = []
        for k in keys:
            if not k.endswith(".json"):
                continue
            d = s.read_json(k)
            if not isinstance(d, dict):
                continue
            if d.get("kind") != "email-recommendations":
                continue
            if since_iso and d.get("sent_at", "") < since_iso:
                continue
            d["_key"] = k
            candidates.append(d)
        if candidates:
            candidates.sort(key=lambda x: x.get("sent_at", ""), reverse=True)
            return candidates[0]
        if last_seen_count != len(keys):
            log(f"waiting for new SEO email (current {len(keys)} records, none after {since_iso})", "info")
            last_seen_count = len(keys)
        time.sleep(poll_sec)
    log(f"timed out waiting for SEO email after {max_wait_min} min", "err")
    return None


# ── Step 2: send the synthetic reply via Microsoft Graph ─────────────────

def send_reply(*, original: dict, body_text: str, oauth_file: Path) -> tuple[bool, str]:
    """Send `Re: <original subject>` with body_text via Graph /sendMail.

    From + To are both automation@nsc (the inbox the responder polls).
    The responder doesn't filter by sender, so a self-addressed reply
    works as well as a real user reply for end-to-end testing.

    Returns (ok, detail).
    """
    # Mint a token with the Graph audience (default IMAP audience would
    # fail with "Invalid audience" against /sendMail).
    access_token, oauth_user, _ = mint.mint_access_token(
        oauth_file,
        scope_override="offline_access https://graph.microsoft.com/Mail.Send",
    )
    sender = oauth_user or "automation@northernsoftwareconsulting.com"

    orig_subj = original.get("subject", "") or ""
    if not orig_subj.lower().startswith("re:"):
        reply_subj = f"Re: {orig_subj}"
    else:
        reply_subj = orig_subj

    # Plain-text body. Body must include the actual command verb the
    # responder parses (e.g. "implement all changes").
    payload = {
        "message": {
            "subject": reply_subj,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": sender}}],
            # X-header so the responder can route via X-Reusable-Agent
            # if the subject is mangled. Optional but useful.
            "internetMessageHeaders": [
                {"name": "x-reusable-agent",
                 "value": original.get("agent_id", "")[:255]},
            ],
        },
        "saveToSentItems": True,
    }
    url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status not in (200, 202):
                return False, f"HTTP {r.status} {r.read()[:200]!r}"
        return True, f"sent: graph:as_self subject={reply_subj!r}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:400]
        return False, f"HTTP {e.code} {body}"
    except Exception as e:
        return False, f"send failed: {e}"


# ── Step 3-5: monitor responder + chain ──────────────────────────────────

def poll_archive_marker(agent_id: str, request_id: str, timeout_min: int = 5) -> bool:
    s = get_storage()
    key = f"agents/{agent_id}/responses-archive/{request_id}.json"
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        if s.read_json(key):
            return True
        time.sleep(5)
    return False


def poll_queue_for_run(run_ts: str, timeout_min: int = 5) -> int:
    """Wait for responses-queue items tagged with `from_run == run_ts`."""
    s = get_storage()
    prefix = "agents/implementer/responses-queue/"
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        n = 0
        for k in s.list_prefix(prefix):
            if run_ts in k:
                n += 1
        if n > 0:
            return n
        time.sleep(5)
    return 0


def find_dispatch_manifest(source_agent: str, source_run_ts: str) -> tuple[str, dict] | tuple[None, None]:
    """Find the dispatch-batches.json key for this run (it's keyed under
    the implementer's tempdir basename, not the source run_ts)."""
    s = get_storage()
    for k in s.list_prefix(f"agents/{source_agent}/runs/"):
        if not k.endswith("/dispatch-batches.json"):
            continue
        d = s.read_json(k)
        if isinstance(d, dict) and d.get("source_run_ts") == source_run_ts:
            return k, d
    return None, None


def watch_chain(source_agent: str, source_run_ts: str, timeout_min: int = 90) -> dict:
    """Poll the dispatch-batches.json until all batches are terminal or
    the timeout expires. Print one update per state change."""
    s = get_storage()
    deadline = time.time() + timeout_min * 60
    last_status: dict[int, str] = {}
    manifest_key: str | None = None
    while time.time() < deadline:
        if not manifest_key:
            manifest_key, _ = find_dispatch_manifest(source_agent, source_run_ts)
            if not manifest_key:
                log("waiting for batch manifest…", "info")
                time.sleep(15)
                continue
            log(f"manifest at {manifest_key}", "ok")
        m = s.read_json(manifest_key) or {}
        for b in m.get("batches", []):
            idx = b.get("index")
            cs = b.get("completion_status") or b.get("status", "")
            if last_status.get(idx) != cs:
                last_status[idx] = cs
                rc = b.get("rec_count", 0)
                ps = b.get("priority_summary", "")
                log(f"batch {idx}: {cs} ({rc} recs · {ps})", "step")
        # Terminal: no batch is pending or running
        statuses = {b.get("completion_status") or b.get("status", "") for b in m.get("batches", [])}
        if "pending" not in statuses and "running" not in statuses:
            log("chain reached terminal state", "ok")
            return {
                "manifest_key": manifest_key,
                "batches": m.get("batches", []),
                "final_statuses": dict(last_status),
            }
        time.sleep(15)
    log(f"chain watch timed out after {timeout_min}m", "warn")
    return {
        "manifest_key": manifest_key,
        "batches": (s.read_json(manifest_key) or {}).get("batches", []) if manifest_key else [],
        "final_statuses": dict(last_status),
    }


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", default="aisleprompt", help="site id (e.g. aisleprompt)")
    p.add_argument("--since", default="",
                   help="only consider outbound emails sent after this ISO timestamp")
    p.add_argument("--reply-body", default="implement all changes",
                   help="text body of the synthetic reply")
    p.add_argument("--reply-only", action="store_true",
                   help="find the latest SEO email and send a reply, then exit")
    p.add_argument("--watch-only", action="store_true",
                   help="skip sending — only watch existing chain progress")
    p.add_argument("--email-wait-min", type=int, default=45,
                   help="how long to wait for the SEO email to arrive")
    p.add_argument("--chain-wait-min", type=int, default=90,
                   help="how long to watch the implementer chain")
    p.add_argument("--oauth-file", default=os.path.expanduser("~/.reusable-agents/responder/.oauth.json"))
    args = p.parse_args()

    log(f"E2E SEO test — site={args.site}", "step")
    started_at = now_iso()
    if not args.since:
        args.since = started_at  # default: only emails sent after the test starts

    if not args.watch_only:
        # Step 1
        log(f"step 1/5: waiting for SEO outbound email (after {args.since})", "step")
        email = find_seo_email(args.site, since_iso=args.since,
                                max_wait_min=args.email_wait_min)
        if not email:
            return 2
        log(f"found email: {email['subject']!r} request_id={email.get('request_id')}", "ok")
        log(f"  recs={email.get('rec_count')} run_ts={email.get('run_ts')}", "info")

        # Step 2
        log("step 2/5: sending synthetic reply via Graph", "step")
        ok, detail = send_reply(
            original=email,
            body_text=args.reply_body,
            oauth_file=Path(args.oauth_file),
        )
        if not ok:
            log(f"reply send failed: {detail}", "err")
            return 3
        log(f"reply sent: {detail}", "ok")

        if args.reply_only:
            return 0

        # Step 3: archive marker (proves dashboard "responded" flag)
        log("step 3/5: waiting for responder to archive the reply", "step")
        agent_id = email.get("agent_id", "")
        request_id = email.get("request_id", "")
        if poll_archive_marker(agent_id, request_id, timeout_min=5):
            log(f"archive marker written: agents/{agent_id}/responses-archive/{request_id}.json", "ok")
        else:
            log("no archive marker after 5 min — dashboard 'responded' flag won't update yet", "warn")

        # Step 4: queue items
        log("step 4/5: waiting for responder to populate responses-queue", "step")
        n = poll_queue_for_run(email.get("run_ts", ""), timeout_min=5)
        log(f"queued {n} items for implementer", "ok" if n > 0 else "warn")

    # Step 5: watch the chain
    log("step 5/5: watching dispatch chain", "step")
    # source_agent + source_run_ts: derive from the latest SEO email
    s = get_storage()
    src_agent = f"{args.site}-seo-opportunity-agent"
    if args.watch_only:
        # Find the most recent run with a manifest
        latest_ts = ""
        for k in s.list_prefix(f"agents/{src_agent}/runs/"):
            if not k.endswith("/dispatch-batches.json"):
                continue
            d = s.read_json(k)
            if isinstance(d, dict):
                ts = d.get("source_run_ts") or ""
                if ts > latest_ts:
                    latest_ts = ts
        if not latest_ts:
            log("no dispatch manifest found — nothing to watch", "err")
            return 4
        result = watch_chain(src_agent, latest_ts, timeout_min=args.chain_wait_min)
    else:
        result = watch_chain(src_agent, email.get("run_ts", ""),
                              timeout_min=args.chain_wait_min)

    # Final summary
    log("=" * 60, "info")
    log("FINAL CHAIN STATUS", "step")
    for b in result.get("batches", []):
        idx = b.get("index")
        cs = b.get("completion_status") or b.get("status", "")
        log(f"  batch {idx}: {cs} ({b.get('rec_count', 0)} recs)", "info")
    statuses = [b.get("completion_status") or b.get("status", "") for b in result.get("batches", [])]
    if statuses and all(s in ("completed", "paused") for s in statuses):
        log("chain finished — see dashboard for per-rec details", "ok")
        return 0
    log("chain incomplete (timed out)", "warn")
    return 1


if __name__ == "__main__":
    sys.exit(main())
