#!/usr/bin/env python3
"""site-feedback-notifier: email the fleet operator new reader feedback.

Sites store reader feedback (the FeedbackWidget's POST /api/feedback) in a
table but have no mail transport of their own: the web containers have no
MTA. This agent runs on the fleet host, where the framework mailer works, and
reports what readers sent. One instance serves every subscribed site.

What one run does
-----------------
For each subscribed site (config ``by_site.<site>.enabled``):

1. read    rows newer than the site's cursor from its feedback table (the
           vendored feedback package's schema: "Feedback" with "appKey" and
           "createdAt"), in a READ-ONLY session. The cursor is the newest
           ``createdAt`` already reported plus the ids reported at exactly that
           instant, so rows sharing a timestamp are neither skipped nor
           repeated.
2. notify  ONE email per site per run listing the new rows (at most
           ``email.max_items_per_email``, then "and N more"). Reader text is
           HTML-escaped and labelled as untrusted.
3. advance the site's cursor ONLY after the email is delivered. A failed send
           keeps it, so the next run sends the same rows again (at least once,
           never silently dropped).

A site seen for the first time starts at ``by_site.<site>.start_from`` (an
ISO timestamp), or at the run's start without one: it never mails a backlog
by surprise.

Fails closed: a storage config that exists but cannot be read fails the run
(never "no subscribed sites"). A site whose read fails two runs in a row
fails the run; one failed read is a warning (Azure Postgres blips happen).
An undelivered email fails the run.

Metrics (goals.json binds the first three):
  feedback_undelivered     rows found this run whose email was not delivered
  feedback_notify_lag_min  minutes from the oldest row emailed this run being
                           submitted to its email going out (0 when none)
  site_errors              subscribed sites whose read failed this run
  feedback_new             rows found this run
  sites_checked            subscribed sites read this run

Env overrides (operator / tests)
--------------------------------
  FEEDBACK_NOTIFIER_DRY_RUN=1   read and report; send nothing; keep the state
  FEEDBACK_NOTIFIER_SITES=a,b   only these subscribed sites
  FEEDBACK_NOTIFIER_NOW=<ISO>   evaluate as of that instant

Never calls an LLM.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

AGENT_ID = "site-feedback-notifier"
CONFIG_KEY = "config/site-feedback-notifier-config.json"
REPO_CONFIG_PATH = _REPO_ROOT / "config" / "site-feedback-notifier-config.json"

DEFAULTS: dict[str, Any] = {
    "email": {
        "enabled": True,
        # The fleet-wide operator pair (CLAUDE.md "Outbound-email recipient
        # policy") comes from the env vars the fleet host already sets for
        # operator alerts, in the same order recall-gate and resilience use.
        # Literal "to"/"from" values override them.
        "to": [],
        "from": "",
        "to_env": ["FRAMEWORK_OPERATOR_EMAIL", "OPERATOR_EMAIL"],
        "from_env": ["IMPLEMENTER_FROM", "OPERATOR_FROM_EMAIL"],
        "msmtp_account": "automation",
        # Reader feedback is operator mail, not digest traffic.
        "bypass_digest": True,
        "max_items_per_email": 25,
        "max_message_chars": 1500,
        "subject_prefix": "",
    },
    "site_defaults": {
        "enabled": False,
        "label": "",
        "dsn_env": "",
        "app_key": "",
        "admin_url": "",
        "table": "Feedback",
        "start_from": "",
        # Rows read per run; more than this waits for the next run.
        "max_rows_per_run": 200,
    },
    # Runs in a row a site's read may fail before the run FAILS.
    "fail_after_read_errors": 2,
    "by_site": {},
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FeedbackNotifierConfigError(RuntimeError):
    """The config cannot be trusted; the run must fail, not go quiet."""


class FeedbackNotifierLLMForbidden(RuntimeError):
    """This agent is deterministic; an LLM call is a bug."""


# ── config ────────────────────────────────────────────────────────────────────

def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(base))
    for k, v in (over or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_file_config(storage: Any) -> dict[str, Any]:
    """The storage copy when present (it replaces the repo default wholesale),
    else the repo default, else {}. A storage copy that exists but cannot be
    read raises: falling back to the repo default (no sites) would turn the
    notifier off without anyone noticing."""
    if storage is not None:
        try:
            doc = storage.read_json(CONFIG_KEY)
        except Exception as e:  # noqa: BLE001
            raise FeedbackNotifierConfigError(
                f"storage copy of {CONFIG_KEY} could not be read ({type(e).__name__})") from None
        if isinstance(doc, dict) and doc:
            return doc
        if doc is not None and not isinstance(doc, dict):
            raise FeedbackNotifierConfigError(f"storage copy of {CONFIG_KEY} is not a JSON object")
        if doc is None:
            try:
                exists = bool(storage.exists(CONFIG_KEY))
            except Exception:  # noqa: BLE001 - unknown is not proof of absence
                exists = True
            if exists:
                raise FeedbackNotifierConfigError(
                    f"storage copy of {CONFIG_KEY} exists but could not be read "
                    "(archived blob or invalid JSON)")
    try:
        doc = json.loads(REPO_CONFIG_PATH.read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_config(doc: Mapping[str, Any]) -> dict[str, Any]:
    """DEFAULTS <- the file config; each by_site entry merged over site_defaults."""
    cfg = _merge(DEFAULTS, doc or {})
    sites: dict[str, dict[str, Any]] = {}
    for name, entry in (cfg.get("by_site") or {}).items():
        if str(name).startswith("_") or not isinstance(entry, Mapping):
            continue
        sites[str(name)] = _merge(cfg["site_defaults"], entry)
    cfg["by_site"] = sites
    return cfg


def site_problems(name: str, s: Mapping[str, Any]) -> list[str]:
    """What stops an enabled site from being read (empty = readable)."""
    problems = []
    if not s.get("dsn_env"):
        problems.append(f"by_site.{name}.dsn_env is empty")
    if not s.get("app_key"):
        problems.append(f"by_site.{name}.app_key is empty")
    if not _IDENT.match(str(s.get("table") or "")):
        problems.append(f"by_site.{name}.table {s.get('table')!r} is not a plain identifier")
    return problems


def resolve_email_pair(email_cfg: Mapping[str, Any],
                       environ: Optional[Mapping[str, str]] = None) -> tuple[list[str], str]:
    """(to, from): literal values when set, else the first non-empty env var
    named in to_env / from_env."""
    env = os.environ if environ is None else environ
    to = [str(a).strip() for a in (email_cfg.get("to") or []) if str(a).strip()]
    if not to:
        for name in email_cfg.get("to_env") or []:
            value = str(env.get(str(name)) or "").strip()
            if value:
                to = [a.strip() for a in value.split(",") if a.strip()]
                break
    sender = str(email_cfg.get("from") or "").strip()
    if not sender:
        for name in email_cfg.get("from_env") or []:
            value = str(env.get(str(name)) or "").strip()
            if value:
                sender = value
                break
    return to, sender


# ── time + cursor ─────────────────────────────────────────────────────────────

def parse_ts(v: Any) -> Optional[_dt.datetime]:
    if isinstance(v, _dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
    s = str(v or "").strip()
    if not s:
        return None
    try:
        t = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def new_rows(rows: list[Mapping[str, Any]], cursor_ts: Optional[_dt.datetime],
             cursor_ids: list[str]) -> list[dict[str, Any]]:
    """Rows not yet reported: strictly after the cursor, or AT the cursor
    instant with an id not reported then. Oldest first."""
    seen = set(str(i) for i in (cursor_ids or []))
    out = []
    for r in rows:
        t = parse_ts(r.get("createdAt"))
        if t is None:
            continue
        if cursor_ts is not None and (t < cursor_ts or (t == cursor_ts and str(r.get("id")) in seen)):
            continue
        out.append({**r, "createdAt": t})
    out.sort(key=lambda r: (r["createdAt"], str(r.get("id"))))
    return out


def advance_cursor(rows: list[Mapping[str, Any]], prev_ts: Optional[_dt.datetime] = None,
                   prev_ids: Optional[list[str]] = None) -> tuple[str, list[str]]:
    """(cursor_ts iso, ids reported at that instant) after reporting ``rows``
    (non-empty). When the newest row shares the previous cursor's instant, the
    ids reported then are kept too; otherwise the next read would treat them
    as new and send them again."""
    last = max(r["createdAt"] for r in rows)
    ids = {str(r.get("id")) for r in rows if r["createdAt"] == last}
    if prev_ts is not None and last == prev_ts:
        ids |= {str(i) for i in (prev_ids or [])}
    return last.isoformat(), sorted(ids)


# ── email ─────────────────────────────────────────────────────────────────────

def render_email(site: str, s: Mapping[str, Any], rows: list[Mapping[str, Any]],
                 email_cfg: Mapping[str, Any]) -> tuple[str, str]:
    """(subject, html body) for one site's new rows."""
    label = str(s.get("label") or site)
    cap = max(1, int(email_cfg.get("max_items_per_email") or 25))
    max_chars = max(100, int(email_cfg.get("max_message_chars") or 1500))
    shown = rows[:cap]
    n = len(rows)
    prefix = str(email_cfg.get("subject_prefix") or "").strip()
    subject = (f"{prefix + ' ' if prefix else ''}[{label}] {n} new reader feedback "
               f"submission{'s' if n != 1 else ''}")
    items = []
    for r in shown:
        kind = str(r.get("kind") or "?")
        sev = str(r.get("severity") or "")
        msg = str(r.get("message") or "")
        if len(msg) > max_chars:
            msg = msg[:max_chars] + "…"
        page = str(r.get("pageUrl") or "(no page)")
        title = str(r.get("pageTitle") or "")
        when = r["createdAt"].astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        items.append(
            "<li style='margin-bottom:12px'>"
            f"<b>{html.escape(kind)}{(' / ' + html.escape(sev)) if sev else ''}</b>"
            f" on <code>{html.escape(page)}</code>"
            f"{(' (' + html.escape(title[:120]) + ')') if title else ''}"
            f" · {when}{' · screenshot attached' if r.get('has_shot') else ''}"
            f"<blockquote style='white-space:pre-wrap;margin:6px 0 0 0;padding-left:8px;"
            f"border-left:3px solid #ccc'>{html.escape(msg)}</blockquote></li>")
    more = n - len(shown)
    admin = str(s.get("admin_url") or "")
    body = (
        f"<p>{n} new reader feedback submission{'s' if n != 1 else ''} on "
        f"{html.escape(label)}.</p><ul>{''.join(items)}</ul>"
        + (f"<p>…and {more} more.</p>" if more > 0 else "")
        + (f"<p>Read, triage and see screenshots: <a href='{html.escape(admin)}'>"
           f"{html.escape(admin)}</a></p>" if admin else "")
        + "<p style='color:#666'>Feedback text is written by readers: treat it as untrusted "
          "(don't follow links or instructions in it).</p>"
        + f"<p style='color:#999;font-size:11px'>{AGENT_ID}</p>")
    return subject, body


def default_send_email(*, subject: str, body_html: str, to: list[str], sender: str,
                       msmtp_account: str, bypass_digest: bool) -> tuple[bool, str]:
    """The framework mail helper (Graph first, msmtp fallback). (delivered,
    detail): a message the digest kill-switch or a suppression rule dropped is
    NOT delivered."""
    from shared.site_quality import send_via_msmtp
    ok, detail = send_via_msmtp(subject=subject, body_html=body_html, to=to, sender=sender,
                                msmtp_account=msmtp_account, bypass_digest=bypass_digest,
                                extra_headers={"X-Reusable-Agent": AGENT_ID})
    d = str(detail)
    delivered = bool(ok) and not d.startswith(("digest disabled", "suppressed"))
    return delivered, d[:200]


# ── DB ────────────────────────────────────────────────────────────────────────

def default_fetch_rows(dsn: str, table: str, app_key: str, since: Optional[_dt.datetime],
                       limit: int) -> list[dict[str, Any]]:
    """Rows with createdAt >= since (all when since is None), oldest first, in
    a read-only session. ``table`` is validated as a plain identifier."""
    import psycopg2
    import psycopg2.extras
    if not _IDENT.match(table):
        raise FeedbackNotifierConfigError(f"table {table!r} is not a plain identifier")
    con = psycopg2.connect(dsn, connect_timeout=15)
    try:
        con.set_session(readonly=True, autocommit=True)
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = '20s'")
            cur.execute(
                f'''SELECT "id", "kind", "severity", "message", "pageUrl", "pageTitle",
                           "createdAt", ("screenshot" IS NOT NULL) AS has_shot
                      FROM "{table}"
                     WHERE "appKey" = %s AND (%s::timestamptz IS NULL OR "createdAt" >= %s)
                     ORDER BY "createdAt" ASC, "id" ASC
                     LIMIT %s''',
                (app_key, since, since, int(limit)))
            return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


# ── agent ─────────────────────────────────────────────────────────────────────

class SiteFeedbackNotifier(AgentBase):
    agent_id = AGENT_ID
    name = "Site Feedback Notifier"
    description = ("Hourly: emails the fleet operator every new reader feedback submission "
                   "each subscribed site stored. One instance serves all sites; sites "
                   "subscribe in config/site-feedback-notifier-config.json by_site. "
                   "Read-only on site databases. Never calls an LLM.")
    category = "ops"
    capabilities = [
        declare("read_site_feedback",
                "SELECT new rows from each subscribed site's feedback table (read-only session)",
                risk_level="low"),
        declare("email_operator",
                "One email per site per run listing new reader feedback, to the fleet "
                "operator pair", risk_level="low", affects=["email"]),
    ]

    # Injectable seams (tests replace them; None = production default).
    fetch_rows: Optional[Callable[..., list[dict[str, Any]]]] = None
    send_email: Optional[Callable[..., tuple[bool, str]]] = None

    # ---- never an LLM ---------------------------------------------------
    def ai_chat(self, *args: Any, **kwargs: Any) -> str:  # noqa: D401
        raise FeedbackNotifierLLMForbidden(f"{AGENT_ID} never calls an LLM")

    def ai_chat_resolved(self, *args: Any, **kwargs: Any) -> Any:
        raise FeedbackNotifierLLMForbidden(f"{AGENT_ID} never calls an LLM")

    def ai_client(self, *args: Any, **kwargs: Any) -> Any:
        raise FeedbackNotifierLLMForbidden(f"{AGENT_ID} never calls an LLM")

    def signals(self) -> dict | None:
        # No short-circuit: every input is a remote table, and the read IS the
        # cheap check (one indexed SELECT per site). No-op runs send no
        # run-summary email anyway ("no new ..." is a no-op summary).
        return None

    def _now(self) -> _dt.datetime:
        t = parse_ts(os.environ.get("FEEDBACK_NOTIFIER_NOW", ""))
        return t or _dt.datetime.now(_dt.timezone.utc)

    def run(self) -> RunResult:
        now = self._now()
        dry = str(os.environ.get("FEEDBACK_NOTIFIER_DRY_RUN", "")).strip().lower() in ("1", "true", "yes")
        only = {s.strip() for s in os.environ.get("FEEDBACK_NOTIFIER_SITES", "").split(",") if s.strip()}
        prev_sites: dict[str, Any] = dict((self.state or {}).get("sites") or {})
        next_sites: dict[str, Any] = copy.deepcopy(prev_sites)

        self.status("loading config", progress=0.05)
        try:
            cfg = resolve_config(load_file_config(self.storage))
        except FeedbackNotifierConfigError as e:
            return RunResult(status="failure", summary=f"config unreadable: {e}",
                             error_text=str(e), next_state=dict(self.state or {}),
                             metrics={"site_errors": 1, "feedback_undelivered": 0,
                                      "feedback_notify_lag_min": 0})
        email_cfg = cfg["email"]
        fetch = self.fetch_rows or default_fetch_rows
        send = self.send_email or default_send_email
        to, sender = resolve_email_pair(email_cfg)

        subscribed = {n: s for n, s in cfg["by_site"].items() if s.get("enabled")}
        if only:
            subscribed = {n: s for n, s in subscribed.items() if n in only}
        # A site this agent tracked before that vanished from the config is a
        # config accident until proven otherwise: say so, keep its cursor.
        lost = sorted(n for n in prev_sites if n not in cfg["by_site"] and not only)

        found_total = undelivered = site_errors = checked = 0
        lag_min = 0.0
        notes: list[str] = []
        failures: list[str] = []
        fail_after = max(1, int(cfg.get("fail_after_read_errors") or 2))

        for i, (site, s) in enumerate(sorted(subscribed.items())):
            self.status(f"checking {site}", progress=0.1 + 0.8 * i / max(1, len(subscribed)))
            st = dict(next_sites.get(site) or {})
            problems = site_problems(site, s)
            dsn = os.environ.get(str(s.get("dsn_env") or ""), "") if not problems else ""
            if not problems and not dsn:
                problems.append(f"env {s.get('dsn_env')} is not set")
            if problems:
                site_errors += 1
                failures.append(f"{site}: " + "; ".join(problems))
                next_sites[site] = st
                continue

            cursor_ts = parse_ts(st.get("cursor_ts"))
            cursor_ids = list(st.get("cursor_ids") or [])
            if cursor_ts is None:
                # First sight: start at start_from, else now. Never a backlog.
                cursor_ts = parse_ts(s.get("start_from")) or now
                cursor_ids = []
                st.update({"cursor_ts": cursor_ts.isoformat(), "cursor_ids": [],
                           "initialized_at": now.isoformat()})
            try:
                rows = fetch(dsn, str(s["table"]), str(s["app_key"]), cursor_ts,
                             int(s.get("max_rows_per_run") or 200) + len(cursor_ids))
            except Exception as e:  # noqa: BLE001 - one site must not stop the others
                site_errors += 1
                streak = int(st.get("read_error_streak") or 0) + 1
                st["read_error_streak"] = streak
                st["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                msg = f"{site}: read failed ({type(e).__name__}), {streak} run(s) in a row"
                self.decide("warning", msg)
                (failures if streak >= fail_after else notes).append(msg)
                next_sites[site] = st
                continue
            checked += 1
            st["read_error_streak"] = 0
            st.pop("last_error", None)
            fresh = new_rows(rows, cursor_ts, cursor_ids)
            found_total += len(fresh)
            if not fresh:
                st["last_checked_at"] = now.isoformat()
                next_sites[site] = st
                continue

            subject, body = render_email(site, s, fresh, email_cfg)
            if dry:
                notes.append(f"{site}: DRY RUN, would email {len(fresh)}")
                continue  # keep the previous state
            if not email_cfg.get("enabled", True):
                notes.append(f"{site}: {len(fresh)} new, email disabled in config")
                undelivered += len(fresh)
                next_sites[site] = st
                continue
            if not to or not sender:
                undelivered += len(fresh)
                failures.append(f"{site}: {len(fresh)} new but no recipient/sender "
                                "(set OPERATOR_EMAIL / OPERATOR_FROM_EMAIL)")
                next_sites[site] = st
                continue
            try:
                ok, detail = send(subject=subject, body_html=body, to=to, sender=sender,
                                  msmtp_account=str(email_cfg.get("msmtp_account") or "automation"),
                                  bypass_digest=bool(email_cfg.get("bypass_digest", True)))
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{type(e).__name__}: {e}"[:200]
            if not ok:
                undelivered += len(fresh)
                failures.append(f"{site}: email for {len(fresh)} not delivered ({detail[:80]})")
                next_sites[site] = st  # cursor stays: the next run retries
                continue
            cur_ts, cur_ids = advance_cursor(fresh, cursor_ts, cursor_ids)
            st.update({"cursor_ts": cur_ts, "cursor_ids": cur_ids,
                       "last_emailed_at": now.isoformat(), "last_count": len(fresh),
                       "last_checked_at": now.isoformat()})
            next_sites[site] = st
            oldest = min(r["createdAt"] for r in fresh)
            lag_min = max(lag_min, (now - oldest).total_seconds() / 60.0)
            notes.append(f"{site} {len(fresh)}")
            self._record_outbound(site, fresh, subject, body, to)

        if lost:
            failures.append("sites tracked before are missing from the config: " + ", ".join(lost))

        metrics = {
            "feedback_undelivered": undelivered,
            "feedback_notify_lag_min": round(lag_min, 1),
            "site_errors": site_errors,
            "feedback_new": found_total,
            "sites_checked": checked,
        }
        if dry:
            metrics = {f"dry_run_{k}": v for k, v in metrics.items()}
        next_state = {"sites": next_sites} if not dry else dict(self.state or {})

        if failures:
            return RunResult(status="failure",
                             summary="FAILED: " + " | ".join(failures)[:400],
                             error_text="\n".join(failures), metrics=metrics,
                             next_state=next_state)
        if not subscribed:
            summary = "nothing to do: no subscribed sites in " + CONFIG_KEY
        elif found_total == 0:
            summary = f"no new feedback on {checked} site(s)" + (f"; {'; '.join(notes)}" if notes else "")
        elif dry:
            summary = "; ".join(notes)
        else:
            summary = f"emailed {found_total} new feedback submission(s): " + ", ".join(notes)
        return RunResult(status="success", summary=summary, metrics=metrics,
                         next_state=next_state)

    def _record_outbound(self, site: str, rows: list[Mapping[str, Any]], subject: str,
                         body: str, to: list[str]) -> None:
        rid = hashlib.sha1(f"{site}|{rows[0].get('id')}|{rows[-1].get('id')}".encode()).hexdigest()[:12]
        try:
            self.record_outbound(request_id=f"feedback-{rid}", subject=subject,
                                 body_hash=hashlib.sha256(body.encode()).hexdigest(),
                                 body_excerpt=f"{site}: {len(rows)} new feedback",
                                 to=to, expects_response=False)
        except Exception as e:  # noqa: BLE001 - dashboard bookkeeping only
            self.decide("warning", f"outbound record failed: {e}")


if __name__ == "__main__":
    SiteFeedbackNotifier().run_once()
