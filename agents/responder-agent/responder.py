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
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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
    for k in ("host", "port"):
        if not cfg["imap"].get(k):
            raise SystemExit(f"config.imap.{k} is required")
    auth_method = cfg["imap"].get("auth_method", "password")
    if auth_method == "oauth2":
        # username can be inherited from the oauth file's username_hint
        if not cfg["imap"].get("oauth_file"):
            raise SystemExit("config.imap.oauth_file is required when auth_method=oauth2")
    elif auth_method == "password":
        if not cfg["imap"].get("username"):
            raise SystemExit("config.imap.username is required when auth_method=password")
        if not cfg["imap"].get("password_env"):
            raise SystemExit("config.imap.password_env is required when auth_method=password")
    else:
        raise SystemExit(f"unknown imap.auth_method: {auth_method!r} (expected 'oauth2' or 'password')")
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
    use_tls = imap_cfg.get("use_tls", True)

    auth_method = imap_cfg.get("auth_method", "password")  # 'password' | 'oauth2'

    conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port) if use_tls else imaplib.IMAP4(host, port)

    if auth_method == "oauth2":
        # Use XOAUTH2 — refresh token lives at imap.oauth_file
        from pathlib import Path as _P
        oauth_file = _P(os.path.expanduser(imap_cfg.get(
            "oauth_file", "~/.reusable-agents/responder/.oauth.json"
        )))
        # Defer import so the rest of the module loads without mint-token.py
        sys.path.insert(0, os.path.dirname(__file__))
        from importlib import import_module
        mint = import_module("mint-token")  # noqa
        access_token, oauth_user, _ = mint.mint_access_token(oauth_file)
        # Prefer the username from oauth file if not explicitly set in config
        effective_user = username or oauth_user
        if not effective_user:
            raise SystemExit(
                "IMAP username not set. Either set imap.username in config or "
                "pass --username during oauth-bootstrap."
            )
        sasl_bytes = mint.build_xoauth2_string(effective_user, access_token)
        conn.authenticate("XOAUTH2", lambda _: sasl_bytes)
    else:
        password = os.getenv(imap_cfg.get("password_env", ""), "")
        if not password:
            raise SystemExit(
                f"IMAP password not in env (var {imap_cfg.get('password_env')!r}). "
                "Set it, or switch imap.auth_method to 'oauth2'."
            )
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


_HTML_QUOTE_BOUNDARIES = [
    # Outlook for Web / Office 365: appended at the start of the quoted block
    re.compile(r'<div id=["\']?(appendonsend|divRplyFwdMsg|_originalContent|OutlookMessageHeader)["\']?', re.I),
    re.compile(r'<hr[^>]*\bid=["\']?stopSpelling["\']?', re.I),
    # Gmail
    re.compile(r'<div class=["\']?gmail_quote["\']?', re.I),
    re.compile(r'<div class=["\']?gmail_attr["\']?', re.I),
    # Apple Mail / generic blockquotes (most replies)
    re.compile(r'<blockquote\b', re.I),
    # Microsoft Outlook desktop "________________________________" separator block
    re.compile(r'<div[^>]*>\s*<font[^>]*><span[^>]*>_{30,}', re.I),
]


def _html_to_text(html: str) -> str:
    """Strip HTML to plain text. Also collapses runs of whitespace and decodes
    common entities. Goal: turn an Outlook HTML reply body into the same text
    the user actually typed, without leaking tag-internals like <b>rec-001</b>."""
    import html as html_mod
    # Drop <style> and <script> blocks entirely
    s = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    # Replace block-level closers with newlines so structure survives
    s = re.sub(r"</(p|div|li|tr|h[1-6]|br)\s*[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?\s*>", "\n", s, flags=re.I)
    # Strip remaining tags
    s = re.sub(r"<[^>]+>", "", s)
    # Decode entities
    s = html_mod.unescape(s)
    # Collapse whitespace per line, drop empties
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def get_body_text(msg: Message) -> str:
    """Return the user-typed body of a reply.

    Strategy:
      1. Prefer text/plain (most mail clients include it alongside HTML).
         For text/plain, strip the quoted reply via plain-text markers.
      2. Fall back to text/html. Cut at the first HTML reply-boundary marker
         BEFORE stripping tags (so `<b>rec-001</b>` in the quoted body never
         gets a chance to look like a command).
    """
    plain_part = None
    html_part = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = part.get("Content-Disposition", "") or ""
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and plain_part is None:
                plain_part = part
            elif ctype == "text/html" and html_part is None:
                html_part = part
    else:
        if msg.get_content_type() == "text/plain":
            plain_part = msg
        elif msg.get_content_type() == "text/html":
            html_part = msg

    if plain_part is not None:
        payload = plain_part.get_payload(decode=True) or b""
        text = payload.decode(plain_part.get_content_charset() or "utf-8", errors="replace")
        return _strip_quoted_text(text)

    if html_part is not None:
        payload = html_part.get_payload(decode=True) or b""
        html = payload.decode(html_part.get_content_charset() or "utf-8", errors="replace")
        # Cut HTML at the earliest reply-boundary marker
        cut = len(html)
        for pat in _HTML_QUOTE_BOUNDARIES:
            m = pat.search(html)
            if m and m.start() < cut:
                cut = m.start()
        return _strip_quoted_text(_html_to_text(html[:cut]))

    return ""


def _strip_quoted_text(body: str) -> str:
    """Strip plain-text quoted-reply markers ('On <date> ... wrote:', leading >)
    and return only the new content."""
    markers = [
        re.compile(r"^On .+wrote:\s*$", re.M),
        re.compile(r"^From: .+@", re.M),
        re.compile(r"^Sent from .+", re.M),
        re.compile(r"^>", re.M),
        re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.M | re.I),
        re.compile(r"^_{20,}\s*$", re.M),  # Outlook desktop separator line
    ]
    cut = len(body)
    for pat in markers:
        m = pat.search(body)
        if m and m.start() < cut:
            cut = m.start()
    return body[:cut].strip()


# Lines that are >50% non-alphanumeric / suspicious are HTML residue, not user text
_HTML_RESIDUE_RE = re.compile(r'[<>{}=/\\]')


_BULK_FILTERS = ("all", "auto", "review", "experimental",
                 "critical", "high", "medium", "low")


def parse_actions(body: str, default_action: str = "implement") -> list[dict]:
    """Parse user reply body into a list of actions.

    Each action: {action, rec_ids, filters, notes, prefix_agent, prefix_site}.

    Two selection modes:
      1. Explicit rec ids:    'implement rec-001 rec-005'
      2. Bulk filters (NEW):  'implement all'
                              'implement high and critical'
                              'implement auto'
                              'skip experimental'
         Filter keywords: all | auto | review | experimental |
                          critical | high | medium | low
         Multiple filters combine as a UNION. The downstream agent
         expands filters against its recommendations.json.

    Both modes can coexist on one line:
      'implement rec-001 and all high'  → rec_ids=[rec-001], filters=[high]

    The body must already have quoted-reply content stripped; lines that
    smell like HTML residue (`<`, `>`, `{`, `=`, etc.) are rejected.
    """
    actions: list[dict] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Reject lines that smell like HTML residue
        if _HTML_RESIDUE_RE.search(line):
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
        line_lower = line.lower()
        for cmd in ("implement", "skip", "merge", "modify"):
            if line_lower.startswith(cmd) and (
                len(line) == len(cmd) or not line[len(cmd)].isalnum()
            ):
                verb = cmd
                line = line[len(cmd):].strip()
                break
        rec_ids = _REC_PATTERN.findall(line)
        # Look for bulk-filter keywords on the same line.
        # Only meaningful for implement/skip — modify/merge need explicit ids.
        line_lower = line.lower()
        filters: list[str] = []
        if verb in ("implement", "skip"):
            for kw in _BULK_FILTERS:
                if re.search(rf"\b{kw}\b", line_lower):
                    filters.append(kw)
        if not rec_ids and not filters:
            continue
        actions.append({
            "action": verb,
            "rec_ids": rec_ids,
            "filters": filters,
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


_RUN_TS_RE = re.compile(r"\b(\d{8}T\d{6}Z)\b")
_SITE_TAG_RE = re.compile(r"\[(?:(?P<agent>[a-z0-9-]+):)?(?P<site>[a-z0-9-]+)\]", re.I)


def extract_agent_from_subject(subject: str) -> Optional[str]:
    """Pull the agent id from a '[<agent>:<site>]' subject tag (lower-case)."""
    if not subject:
        return None
    m = _SITE_TAG_RE.search(subject)
    if m and m.group("agent"):
        return m.group("agent").lower()
    return None


def extract_run_ts_from_subject(subject: str) -> tuple[Optional[str], Optional[str]]:
    """Pull the (site, run_ts) pair from any subject containing them.

    Handles all of:
      Re: [SEO:aisleprompt] run 20260426T031318Z — 12 recs
      Re: SEO agent run — aisleprompt — 20260426T031318Z
      Fwd: [SEO:my-site] run 20260101T000000Z any extra
    Falls back to (None, None) if either signal is missing.
    """
    if not subject:
        return None, None
    run_ts_m = _RUN_TS_RE.search(subject)
    run_ts = run_ts_m.group(1) if run_ts_m else None
    # Prefer [agent:site] / [site] bracket-tag form
    tag_m = _SITE_TAG_RE.search(subject)
    site = tag_m.group("site") if tag_m else None
    # Fallback: legacy 'Re: ... — <site> — <run-ts>' layout
    if site is None:
        s = re.sub(r"^(Re:|Fwd:|Fw:)\s*", "", subject, flags=re.I).strip()
        parts = re.split(r"\s+[—–-]\s+", s)
        if len(parts) >= 3:
            site = parts[1].strip() or None
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

def _decode_header(value: str) -> str:
    """RFC 2047 decode an email header. Returns the original string if decoding fails."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def process_message(cfg: dict, msg: Message, runs_roots: list[Path]) -> int:
    """Process a single email. Returns # of recorded actions."""
    # All headers may be RFC-2047 encoded — decode before any text parsing.
    subject = _decode_header(msg.get("Subject", ""))
    sender = _decode_header(msg.get("From", ""))
    msg_id = msg.get("Message-ID", "")
    in_reply_to = msg.get("In-Reply-To", "")
    references = msg.get("References", "")

    # Identify which agent ecosystem this reply is for. Multiple signals:
    #   1. X-Reusable-Agent header (most reliable, but mail clients sometimes
    #      strip custom X-headers from replies).
    #   2. In-Reply-To / References pointing at a Message-ID we sent (durable;
    #      survives any subject mangling — but only matches if we tracked the
    #      original Message-ID, which the reporter doesn't persist yet).
    #   3. Subject pattern: 'Re: ... — <site> ... <run-ts>' (most fragile but
    #      survives almost all reply round-trips).
    x_agent = msg.get("X-Reusable-Agent", "") or ""
    site_hint, run_ts_hint = extract_run_ts_from_subject(subject)
    agent_hint = extract_agent_from_subject(subject)  # e.g. 'seo' from '[SEO:aisleprompt]'

    print(f"[responder] msg from={sender} subject={subject[:120]!r} "
          f"x-agent={x_agent!r} site={site_hint} run_ts={run_ts_hint}",
          file=sys.stderr)

    # If the message is not from a recognized X- header AND the subject doesn't look
    # like one of our reports, skip it (avoid acting on unrelated mail).
    if not (x_agent or run_ts_hint):
        print("  [skip] not a recognized agent reply (no X-header, no run-ts in subject)",
              file=sys.stderr)
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

        # Expand bulk filters (e.g., 'implement all', 'implement high') against
        # the run's recommendations.json. Filters union with explicit rec_ids.
        rec_ids = list(action_obj.get("rec_ids") or [])
        filters = action_obj.get("filters") or []
        if filters:
            recs_path = run_dir / "recommendations.json"
            if recs_path.is_file():
                try:
                    doc = json.loads(recs_path.read_text())
                    expanded: set[str] = set(rec_ids)
                    for r in doc.get("recommendations", []):
                        if "all" in filters:
                            expanded.add(r["id"]); continue
                        if r.get("tier") in filters or r.get("severity") in filters:
                            expanded.add(r["id"])
                    rec_ids = sorted(expanded)
                    print(f"  [bulk] filters={filters} expanded to {len(rec_ids)} recs",
                          file=sys.stderr)
                except Exception as e:
                    print(f"  [bulk] failed to expand filters: {e}", file=sys.stderr)
            else:
                print(f"  [bulk] no recommendations.json at {recs_path} — skip expansion",
                      file=sys.stderr)

        if not rec_ids:
            print(f"  [skip] action {action_obj['raw_line']!r} resolved to 0 recs",
                  file=sys.stderr)
            continue

        for rec_id in rec_ids:
            record_action(runs_roots, site, run_ts, run_dir, rec_id,
                          action_obj["action"], notes=action_obj["raw_line"])
            print(f"  [recorded] {site}/{run_ts} {rec_id} → {action_obj['action']}", file=sys.stderr)
            recorded += 1
        # Persist the resolved set on the action so dispatch sees the expanded list
        action_obj["rec_ids"] = rec_ids

        # Dispatch (one call per route per email, batching all rec_ids).
        # Match strategies, in priority order:
        #   1. X-Reusable-Agent header (only present when the original
        #      outbound message-id chain is preserved AND the mail client
        #      didn't strip our X-* header — Outlook usually does strip).
        #   2. [<agent>:<site>] subject tag (durable; survives reply round-trip
        #      because mail clients prepend 'Re: ' but keep the rest).
        #   3. action_obj's own '[<agent>:<site>]' line prefix (user typed
        #      it explicitly).
        if action_obj["action"] in ("implement", "merge"):
            matched_route = None
            for route in cfg.get("routes", []):
                match = route.get("match", {})
                if match.get("header") == "X-Reusable-Agent" and x_agent \
                        and match.get("equals") == x_agent:
                    matched_route = route; break
                if match.get("agent_prefix") and action_obj.get("prefix_agent") \
                        and action_obj["prefix_agent"].lower() == match["agent_prefix"].lower():
                    matched_route = route; break
                if match.get("agent_subject_tag") and agent_hint \
                        and agent_hint.lower() == match["agent_subject_tag"].lower():
                    matched_route = route; break
            if matched_route:
                trigger_dispatcher(matched_route, action_obj["action"],
                                   action_obj["rec_ids"], site, run_ts, run_dir)
            else:
                print(f"  [no-route] no dispatcher matched (x_agent={x_agent!r}, "
                      f"subject_agent={agent_hint!r}, prefix_agent={action_obj.get('prefix_agent')!r})",
                      file=sys.stderr)

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
