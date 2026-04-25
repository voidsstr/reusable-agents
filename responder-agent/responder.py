#!/usr/bin/env python3
"""responder-agent — poll IMAP inbox, parse replies to agent emails,
dispatch actions to the configured downstream agent.

Designed to run as a 1-minute cron job:
    * * * * * cd /home/voidsstr/development/reusable-agents/responder-agent && python3 responder.py

Or as a long-running daemon: `python3 responder.py --daemon --interval 60`.

Configuration: ~/.reusable-agents/responder/config.yaml — see README.md.
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared import run_files  # noqa: E402


DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.reusable-agents/responder/config.yaml"))
STATE_PATH = Path(os.path.expanduser("~/.reusable-agents/responder/state.json"))


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(
            f"No responder config at {path}.\n"
            f"Copy responder-agent/config.example.yaml and edit, then re-run."
        )
    with path.open() as f:
        cfg = yaml.safe_load(f)
    # Validate minimum required fields
    if not cfg.get("imap"):
        raise SystemExit("config.imap is required")
    for k in ("host", "port", "username", "password_env"):
        if not cfg["imap"].get(k):
            raise SystemExit(f"config.imap.{k} is required")
    return cfg


def load_state() -> dict:
    if not STATE_PATH.is_file():
        return {"last_uid": 0, "processed_message_ids": []}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def connect_imap(imap_cfg: dict) -> imaplib.IMAP4_SSL:
    host = imap_cfg["host"]
    port = int(imap_cfg.get("port", 993))
    username = imap_cfg["username"]
    password = os.getenv(imap_cfg["password_env"], "")
    if not password:
        raise SystemExit(
            f"IMAP password not in env (var {imap_cfg['password_env']!r}). "
            "Set it before running the responder."
        )
    use_tls = imap_cfg.get("use_tls", True)
    if use_tls:
        conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)
    conn.login(username, password)
    conn.select(imap_cfg.get("mailbox", "INBOX"))
    return conn


def fetch_unseen(conn: imaplib.IMAP4_SSL) -> list[tuple[bytes, Message]]:
    typ, data = conn.search(None, "UNSEEN")
    if typ != "OK":
        return []
    out = []
    for uid in data[0].split():
        typ, msg_data = conn.fetch(uid, "(RFC822)")
        if typ != "OK":
            continue
        for part in msg_data:
            if isinstance(part, tuple) and len(part) == 2:
                msg = email.message_from_bytes(part[1])
                out.append((uid, msg))
                break
    return out


def mark_seen(conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
    conn.store(uid, "+FLAGS", "\\Seen")


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

_REC_PATTERN = re.compile(r"\brec-\d{3}\b")
_PREFIX_PATTERN = re.compile(r"^\s*\[(?P<agent>[a-z0-9-]+)(?::(?P<site>[a-z0-9-]+))?\]\s*", re.I)


def get_body_text(msg: Message) -> str:
    """Return the plain-text body of an email, falling back to HTML stripped."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = part.get("Content-Disposition", "")
            if ctype == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        # No text/plain — strip HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
    else:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def strip_quoted_reply(body: str) -> str:
    """Return only the new content above the first quoted-reply marker.
    Email clients quote the original message — we don't want to parse those rec ids."""
    # Common reply markers, in priority order
    markers = [
        re.compile(r"^On .+ wrote:\s*$", re.M),
        re.compile(r"^From: .+@", re.M),
        re.compile(r"^>.+", re.M),
        re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.M | re.I),
    ]
    cut = len(body)
    for pat in markers:
        m = pat.search(body)
        if m and m.start() < cut:
            cut = m.start()
    return body[:cut].strip()


def parse_actions(body: str, default_action: str = "implement") -> list[dict]:
    """Parse user reply body into a list of actions.

    Each action: {action: 'implement'|'skip'|'merge', rec_ids: [...], notes: str,
                  prefix_agent: str, prefix_site: str}
    """
    body = strip_quoted_reply(body)
    actions = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        agent_prefix = None
        site_prefix = None
        m = _PREFIX_PATTERN.match(line)
        if m:
            agent_prefix = m.group("agent")
            site_prefix = m.group("site")
            line = line[m.end():].strip()
        # Determine action verb
        verb = default_action
        for cmd in ("implement", "skip", "merge", "modify"):
            if line.lower().startswith(cmd):
                verb = cmd
                line = line[len(cmd):].strip()
                break
        # Find rec ids
        rec_ids = _REC_PATTERN.findall(line)
        if not rec_ids:
            continue
        actions.append({
            "action": verb,
            "rec_ids": rec_ids,
            "prefix_agent": agent_prefix,
            "prefix_site": site_prefix,
            "raw_line": raw_line.strip(),
        })
    return actions


# ---------------------------------------------------------------------------
# Routing — find the right run dir for a given (agent, site) hint
# ---------------------------------------------------------------------------

def find_run_dir_for_site(runs_roots: list[Path], site: str, hint_run_ts: Optional[str] = None) -> Optional[Path]:
    """Find the latest run dir for `site` across all configured runs_roots.
    If hint_run_ts is given, prefer that exact run."""
    for root in runs_roots:
        site_dir = root / site
        if not site_dir.is_dir():
            continue
        if hint_run_ts:
            specific = site_dir / hint_run_ts
            if specific.is_dir():
                return specific
        # Latest symlink (preferred) or alphabetically max non-`latest` subdir
        latest = site_dir / "latest"
        if latest.is_symlink() or latest.exists():
            try: return latest.resolve()
            except OSError: pass
        candidates = sorted(p for p in site_dir.iterdir()
                            if p.is_dir() and p.name not in ("latest", "_queue"))
        if candidates:
            return candidates[-1]
    return None


def extract_run_ts_from_subject(subject: str) -> tuple[Optional[str], Optional[str]]:
    """Parse 'Re: SEO agent run — <site> — <run-ts>' style subjects."""
    if not subject: return None, None
    s = re.sub(r"^(Re:|Fwd:|Fw:)\s*", "", subject, flags=re.I).strip()
    parts = re.split(r"\s+[—–-]\s+", s)
    site = parts[1].strip() if len(parts) > 1 else None
    run_ts = parts[-1].strip() if len(parts) > 2 else None
    if run_ts and not re.match(r"^\d{8}T\d{6}Z$", run_ts):
        run_ts = None
    return site, run_ts


# ---------------------------------------------------------------------------
# Dispatching
# ---------------------------------------------------------------------------

def record_action(runs_roots: list[Path], site: str, run_ts: str, run_dir: Path,
                  rec_id: str, action: str, notes: str = "") -> None:
    """Append to the run's responses.json + the global queue."""
    run_files.append_response(
        run_dir,
        site=site, from_run=run_ts, rec_id=rec_id, action=action,
        source="email-reply", notes=notes,
    )
    # Global queue
    for root in runs_roots:
        if str(run_dir).startswith(str(root)):
            run_files.append_to_global_response_queue(root, {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "site": site, "from_run": run_ts, "rec_id": rec_id,
                "action": action, "source": "email-reply", "notes": notes,
            })
            break


def trigger_dispatcher(route: dict, action: str, rec_ids: list[str], site: str,
                       run_ts: str, run_dir: Path) -> None:
    """Call the configured downstream agent (or just log + exit if --no-dispatch)."""
    dispatcher = route.get("dispatcher", {})
    typ = dispatcher.get("type", "")
    script = dispatcher.get("script", "")
    if not script or not Path(script).is_file():
        print(f"  [dispatch] no script configured for type={typ}, action={action} — recorded only", file=sys.stderr)
        return
    env = os.environ.copy()
    env["RESPONDER_ACTION"] = action
    env["RESPONDER_REC_IDS"] = ",".join(rec_ids)
    env["RESPONDER_SITE"] = site
    env["RESPONDER_RUN_TS"] = run_ts
    env["RESPONDER_RUN_DIR"] = str(run_dir)
    try:
        proc = subprocess.Popen(
            ["bash", script],
            env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"  [dispatch] spawned {script} pid={proc.pid} action={action} recs={rec_ids}", file=sys.stderr)
    except Exception as e:
        print(f"  [dispatch] failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def process_message(cfg: dict, msg: Message, runs_roots: list[Path]) -> int:
    """Process a single email. Returns # of recorded actions."""
    subject = msg.get("Subject", "")
    sender = msg.get("From", "")
    msg_id = msg.get("Message-ID", "")
    in_reply_to = msg.get("In-Reply-To", "")

    # Identify which agent ecosystem this reply is for via X-* headers OR subject
    site_hint, run_ts_hint = extract_run_ts_from_subject(subject)
    x_agent = msg.get("X-Reusable-Agent", "") or ""

    print(f"[responder] msg from={sender} subject={subject!r} x-agent={x_agent!r} site={site_hint} run_ts={run_ts_hint}", file=sys.stderr)

    # If the message is not from a recognized X- header AND the subject doesn't look
    # like one of our reports, skip it (avoid acting on unrelated mail).
    if not (x_agent or run_ts_hint):
        print("  [skip] not a recognized agent reply (no X-header, no run-ts in subject)", file=sys.stderr)
        return 0

    body = get_body_text(msg)
    actions = parse_actions(body)
    if not actions:
        print("  [skip] no actionable rec ids found in body", file=sys.stderr)
        return 0

    recorded = 0
    for action_obj in actions:
        site = action_obj.get("prefix_site") or site_hint
        if not site:
            print(f"  [skip] no site for action {action_obj['raw_line']!r}", file=sys.stderr)
            continue
        run_dir = find_run_dir_for_site(runs_roots, site, hint_run_ts=run_ts_hint)
        if not run_dir:
            print(f"  [skip] no run dir found for site={site}", file=sys.stderr)
            continue
        run_ts = run_dir.name
        for rec_id in action_obj["rec_ids"]:
            record_action(runs_roots, site, run_ts, run_dir, rec_id,
                          action_obj["action"], notes=action_obj["raw_line"])
            print(f"  [recorded] {site}/{run_ts} {rec_id} → {action_obj['action']}", file=sys.stderr)
            recorded += 1

        # Dispatch (one call per route per email, batching all rec_ids)
        if action_obj["action"] in ("implement", "merge"):
            for route in cfg.get("routes", []):
                match = route.get("match", {})
                # Match by X-Reusable-Agent header (most precise)
                if match.get("header") == "X-Reusable-Agent" and x_agent and match.get("equals") == x_agent:
                    trigger_dispatcher(route, action_obj["action"], action_obj["rec_ids"],
                                        site, run_ts, run_dir)
                    break
                # Or match by agent prefix the user wrote in the email
                if match.get("agent_prefix") and action_obj.get("prefix_agent") == match["agent_prefix"]:
                    trigger_dispatcher(route, action_obj["action"], action_obj["rec_ids"],
                                        site, run_ts, run_dir)
                    break

    return recorded


def tick(cfg: dict, state: dict) -> dict:
    """One poll cycle. Returns updated state."""
    runs_roots = [Path(os.path.expanduser(r)) for r in cfg.get("runs_roots", [])]
    if not runs_roots:
        runs_roots = [Path(os.path.expanduser("~/.reusable-agents/seo/runs"))]

    try:
        conn = connect_imap(cfg["imap"])
    except Exception as e:
        print(f"[responder] IMAP connect failed: {e}", file=sys.stderr)
        return state

    try:
        msgs = fetch_unseen(conn)
        print(f"[responder] tick: {len(msgs)} unseen messages", file=sys.stderr)
        processed_ids = set(state.get("processed_message_ids", []))
        for uid, msg in msgs:
            msg_id = msg.get("Message-ID", "")
            if msg_id and msg_id in processed_ids:
                # already processed (we mark seen, but this is belt-and-braces)
                mark_seen(conn, uid)
                continue
            try:
                process_message(cfg, msg, runs_roots)
            except Exception as e:
                print(f"[responder] error processing message: {e}", file=sys.stderr)
            mark_seen(conn, uid)
            if msg_id:
                processed_ids.add(msg_id)
        # Cap state size
        state["processed_message_ids"] = list(processed_ids)[-1000:]
    finally:
        try: conn.logout()
        except Exception: pass

    return state


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--once", action="store_true", default=True,
                   help="Run one tick and exit (default; suitable for cron).")
    args = p.parse_args()

    cfg = load_config(Path(args.config))
    state = load_state()

    if args.daemon:
        print(f"[responder] daemon mode, interval={args.interval}s", file=sys.stderr)
        while True:
            state = tick(cfg, state)
            save_state(state)
            time.sleep(args.interval)
    else:
        state = tick(cfg, state)
        save_state(state)


if __name__ == "__main__":
    main()
