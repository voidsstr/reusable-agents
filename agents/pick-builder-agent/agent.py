#!/usr/bin/env python3
"""Pick Builder: deterministic ladder slots, best lists and substitutes from
one eligibility view, plus the pick-ASIN list the price refresh reads.

A thin AgentBase engine (no LLM, no external API):

  pick_builder_store.py    the table contract in SQL (eligibility pool,
                           definitions, outputs; group-wise writes)
  pick_builder_engine.py   selectors, ladders, lists, substitutes (pure)
  pick_builder_run.py      one run: snapshot -> short-circuit -> fill ->
                           write changed groups -> publish -> verify

Every slot is filled from the site's eligibility view or deliberately left
empty with the reason; no filter is ever loosened to fill it. Every value
(tables, columns, plans, selectors, substitute rules, publish key) comes
from the site's site.yaml block ($PICK_BUILDER_CONFIG,
pick_builder_settings.py); nothing here names a site. The TABLE CONTRACT is
in AGENT.md.

CLI
---
  python3 agent.py                      AgentBase run (cron / Run now)
  python3 agent.py --config             resolved settings (no DB)
  python3 agent.py --print-sql          the contract SQL (no DB)
  python3 agent.py --plan [--json]      read-only DB: what a run would write, slot by slot,
                                        with the reason for every empty slot (no writes, no publish)
Options: --site-yaml PATH[#key] (default $PICK_BUILDER_CONFIG), --agent-id ID.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

_HERE = Path(__file__).resolve().parent
_FW = _HERE.parents[1]
for _p in (str(_HERE), str(_FW)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

import pick_builder_run as run_mod  # noqa: E402
import pick_builder_settings as settings_mod  # noqa: E402
import pick_builder_store as store_mod  # noqa: E402

ENGINE_ID = "pick-builder-agent"


# ---------------------------------------------------------------------------
# Site DB connection: the DSN never reaches a run record, a log or the CLI.
# (spec-extractor-agent/agent.py carries the same helpers; one framework copy
# in framework.core.pg_conn is the post-freeze cleanup.)
# ---------------------------------------------------------------------------

class SiteDBConnectError(RuntimeError):
    """The site DB could not be reached. The text is safe to store and show:
    it never carries the DSN or any part of its password."""


#: libpq quotes every piece of the connection string it echoes ("..."; other
#: marks under a translated client locale).
_QUOTED_RE = re.compile("\"[^\"]*\"|'[^']*'|“[^”]*”|„[^“”]*[“”]"
                        "|«[^»]*»|‘[^’]*’")
#: A parse error of the connection string itself: its detail IS the DSN.
_INVALID_DSN_RE = re.compile(r"invalid dsn|connection info string|\bURI\b")
_PASSWORD_KW_RE = re.compile(r"password\s*=\s*('[^']*'?|\S+)", re.I)
_URI_RE = re.compile(r"^\s*[A-Za-z][A-Za-z0-9+.-]*://(.*)$", re.S)
_PIECES_RE = re.compile(r"[\s@:/?#&=,;\"'%]+")
_MIN_FRAGMENT = 4
REDACTED = "<redacted>"


def password_fragments(dsn: str) -> list[str]:
    """What a driver message could echo of the DSN's password, read
    leniently: ``password=...`` (keyword form or URI query) and the URI user
    info up to the LAST '@' (a password holding an unencoded '@', '/', '?' or
    '#' breaks the URI but is still found); raw and percent-decoded, whole
    and in pieces of at least 4 characters, longest first."""
    dsn = str(dsn or "")
    found = {m.group(1).strip("'") for m in _PASSWORD_KW_RE.finditer(dsn)}
    m = _URI_RE.match(dsn)
    if m and "@" in m.group(1):
        userinfo = m.group(1)[:m.group(1).rfind("@")]
        if ":" in userinfo:
            found.add(userinfo.split(":", 1)[1])
    out: set[str] = set()
    for f in found:
        for v in (f, urllib.parse.unquote(f)):
            out.add(v)
            out.update(_PIECES_RE.split(v))
    return sorted((x for x in out if len(x) >= _MIN_FRAGMENT), key=lambda x: (-len(x), x))


def redact_dsn(text: str, dsn: str) -> str:
    """``text`` with every run of at least 4 characters found inside a
    password fragment of ``dsn`` replaced by ``<redacted>``."""
    frags = password_fragments(dsn)
    if not frags or not text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        best = 0
        if i + _MIN_FRAGMENT <= n:
            window = text[i:i + _MIN_FRAGMENT]
            for f in frags:
                if window in f:
                    k = _MIN_FRAGMENT
                    while i + k < n and text[i:i + k + 1] in f:
                        k += 1
                    best = max(best, k)
        if best:
            out.append(REDACTED)
            i += best
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def connect_error_text(exc: BaseException, *, dsn_env: str, dsn: str = "") -> str:
    """A connect failure as text that is safe to store and show. libpq echoes
    pieces of a malformed connection string (a mis-encoded password
    included) inside quotes, so a parse error of the DSN keeps no driver text
    at all, any other message loses every quoted segment, and what is left
    loses every fragment of the password (:func:`redact_dsn`)."""
    kind = type(exc).__name__
    raw = " ".join(str(exc).split())
    if kind == "ProgrammingError" or _INVALID_DSN_RE.search(raw):
        detail = ("not a valid connection string or URI (the driver's message is withheld: it "
                  "echoes the DSN). In a URI, percent-encode special characters of the password "
                  "(% as %25, @ as %40, / as %2F, # as %23, ? as %3F, a space as %20).")
    else:
        detail = _QUOTED_RE.sub(f'"{REDACTED}"', raw)
    return f"cannot connect to the site DB (${dsn_env}): {kind}: {redact_dsn(detail, dsn)[:400]}"


def connect_failure_text(exc: BaseException, *, dsn_env: str, dsn: str) -> str:
    """The text of any exception a connect callable raised, safe to store."""
    if isinstance(exc, SiteDBConnectError):
        return redact_dsn(str(exc), dsn)
    return connect_error_text(exc, dsn_env=dsn_env, dsn=dsn)


def default_connect(settings: settings_mod.Settings, *, environ: Optional[Mapping[str, str]] = None,
                    readonly: bool = False):
    """A keepalive Postgres connection to the site's DB. The DSN is read from
    the env var the settings name (a validated variable NAME) and never
    leaves this function: a failure raises :class:`SiteDBConnectError` with
    :func:`connect_error_text` and no chained driver exception (whose text
    can echo the DSN). ``readonly`` (--plan): the session refuses every
    write."""
    env = os.environ if environ is None else environ
    dsn = str(env.get(settings.dsn_env, "") or "")
    if not dsn:
        raise SiteDBConnectError(f"cannot connect to the site DB: ${settings.dsn_env} is not set "
                                 "(the site's Postgres DSN)")
    from framework.core import pg_conn
    db = settings.database
    kw: dict[str, Any] = {"application_name": (db.get("application_name") or settings.agent_id)[:63],
                          "connect_timeout": int(db.get("connect_timeout_s") or 15)}
    if db.get("statement_timeout_ms"):
        kw["options"] = f"-c statement_timeout={int(db['statement_timeout_ms'])}"
    conn = None
    failure = ""
    try:
        conn = pg_conn.connect(dsn, **kw)
    except Exception as exc:  # noqa: BLE001 - re-raised below without the driver's text
        failure = connect_error_text(exc, dsn_env=settings.dsn_env, dsn=dsn)
    if conn is None:
        # raised outside the except block: no __context__ carries the
        # driver's exception (and its echo of the DSN) along
        raise SiteDBConnectError(failure or f"cannot connect to the site DB (${settings.dsn_env})")
    if readonly:
        conn.set_session(readonly=True)
    return conn


class PickBuilderAgent(AgentBase):
    agent_id = ENGINE_ID
    name = "Pick Builder"
    description = ("Fills ladder slots and best lists from the site's eligibility view (an "
                   "unfillable slot stays empty with its reason), builds the substitutes map, and "
                   "publishes the pick-ASIN list the price refresh prioritises. Deterministic; no LLM.")
    category = "seo"
    send_run_summary_email = False
    capabilities = [
        declare("read_eligibility", "Read the site's eligibility view, definitions and compat edges",
                risk_level="low"),
        declare("write_picks", "Rewrite changed ladder slots, list items and substitutes (one "
                "transaction, picks only from the eligibility view)", risk_level="medium",
                affects=["site-database"]),
        declare("publish_pick_asins", "Write the pick-ASIN list to framework storage for "
                "price_refresh_priority", risk_level="low", affects=["framework-storage"]),
    ]

    def __init__(self, *, settings: Optional[settings_mod.Settings] = None,
                 config_spec: Optional[str] = None, connect: Optional[Callable] = None,
                 environ: Optional[Mapping[str, str]] = None,
                 clock: Optional[Callable[[], datetime]] = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._settings = settings
        self._config_spec = config_spec
        self._connect = connect or default_connect
        self._environ = environ
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def signals(self) -> dict | None:
        """No pre-run hash: the eligibility view's contents ARE the input and
        change with time (prices age out). run() loads them in one snapshot,
        hashes inputs and outputs, and skips every write when both match the
        last run (short_circuit.should_skip)."""
        return None

    def load_settings(self) -> settings_mod.Settings:
        if self._settings is not None:
            return self._settings
        return settings_mod.load_settings(self._config_spec, agent_id=self.agent_id,
                                          storage=self.storage, environ=self._environ)

    def _dsn(self, settings: settings_mod.Settings) -> str:
        """The DSN value, only to redact it from failure text."""
        env = os.environ if self._environ is None else self._environ
        return str(env.get(settings.dsn_env, "") or "")

    def run(self) -> RunResult:
        self.status("loading config", progress=0.01)
        try:
            settings = self.load_settings()
        except settings_mod.SettingsError as exc:
            return RunResult(status="failure", summary=f"pick-builder config invalid — nothing done: {exc}"[:500],
                             error_text=str(exc)[:4000], metrics={"config_errors": len(exc.problems)},
                             next_state=dict(self.state))
        for w in settings.warnings:
            self.decide("warning", w)
        try:
            conn = self._connect(settings, environ=self._environ)
        except Exception as exc:  # noqa: BLE001 - only the redacted text is kept (no traceback)
            text = connect_failure_text(exc, dsn_env=settings.dsn_env, dsn=self._dsn(settings))
            return RunResult(status="failure", summary=text[:300], error_text=text[:2000],
                             next_state=dict(self.state))
        store = store_mod.PickStore(conn, settings)
        try:
            if not store.try_lock(settings.lock_key):
                return RunResult(status="success",
                                 summary=f"Skipped: another {self.agent_id} run holds the lock.",
                                 metrics={"skipped_locked": 1}, next_state=dict(self.state),
                                 short_circuited=True)
            build = run_mod.PickBuild(
                settings, store, storage=self.storage, prior_state=self.state,
                say=lambda msg, progress=0.0: self.status(msg, progress=progress),
                decide=lambda category, message, evidence=None: self.decide(
                    category, message, evidence=evidence),
                now=self._clock(), run_ts=self.run_ts)
            try:
                rep = build.run()
            except Exception as exc:  # noqa: BLE001 - keep the state: an empty one would forget
                # the published list and the dropped picks (AgentBase stores {} on a raise)
                store.rollback()
                dsn = self._dsn(settings)      # scrubbed of the password as a second line
                return RunResult(status="failure",
                                 summary=redact_dsn(f"pick-builder failed: {type(exc).__name__}: {exc}",
                                                    dsn)[:500],
                                 error_text=redact_dsn(traceback.format_exc(), dsn)[-4000:],
                                 metrics={k: (round(v, 4) if isinstance(v, float) else v)
                                          for k, v in sorted(build.m.items())},
                                 next_state={**self.state, **build.state})
            finally:
                store.unlock(settings.lock_key)
        finally:
            try:
                store.conn.close()
            except Exception:  # noqa: BLE001
                pass
        return RunResult(status=rep.status, summary=rep.summary, summary_md=rep.summary_md,
                         metrics=rep.metrics, error_text=rep.error_text,
                         next_state={**self.state, **rep.state}, short_circuited=rep.short_circuited)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pick-builder-agent", description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--config", action="store_true", help="print the resolved settings (no DB)")
    mode.add_argument("--print-sql", action="store_true", help="print the contract SQL (no DB)")
    mode.add_argument("--plan", action="store_true",
                      help="read-only DB: what a run would write, with every empty slot's reason")
    ap.add_argument("--site-yaml", default="", help="config PATH[#dotted.key] (default $PICK_BUILDER_CONFIG)")
    ap.add_argument("--agent-id", default="", help="instance id (default $AGENT_ID)")
    ap.add_argument("--json", action="store_true", help="with --plan: the full plan as JSON")
    args = ap.parse_args(argv)
    if not (args.config or args.print_sql or args.plan):
        PickBuilderAgent(config_spec=args.site_yaml or None, agent_id=args.agent_id or None).run_once()
        return 0
    agent_id = args.agent_id or os.environ.get("AGENT_ID") or ENGINE_ID
    try:
        settings = settings_mod.load_settings(args.site_yaml or None, agent_id=agent_id, storage=None)
    except settings_mod.SettingsError as exc:
        print(f"config invalid: {exc}", file=sys.stderr)
        return 2
    if args.config:
        _print(settings.describe())
        return 0
    if args.print_sql:
        st = store_mod.PickStore(None, settings)
        print(f"-- eligibility pool\n{st.pool_sql()};\n")
        return 0
    try:
        conn = default_connect(settings, readonly=True)
    except SiteDBConnectError as exc:      # redacted text, no traceback
        print(str(exc), file=sys.stderr)
        return 1
    try:
        store = store_mod.PickStore(conn, settings)

        class _NoStorage:
            def read_json(self, _k):
                return None

            def write_json(self, *_a, **_k):
                raise RuntimeError("--plan never publishes")

        rep = run_mod.PickBuild(settings, store, storage=_NoStorage(), prior_state={},
                                say=lambda *a, **k: None, decide=lambda *a, **k: None,
                                dry_run=True).run()
    finally:
        conn.close()
    print(rep.summary)
    if args.json:
        _print(rep.plan)
        return 0
    for line in plan_lines(rep.plan):
        print(line)
    return 0


def plan_lines(plan: Mapping[str, Any]) -> list[str]:
    """--plan text: every slot (an empty one with its reason and the counted
    trace of the filters), then every list (a short one with why)."""
    out = []
    traces = plan.get("slot_traces") or {}
    for tier, rows in sorted((plan.get("slots") or {}).items()):
        tt = traces.get(tier) or []
        for i, r in enumerate(rows):
            status = (r.get("eligibility") or {}).get("status") if isinstance(r.get("eligibility"), Mapping) else None
            what = r["product_id"] if r["product_id"] is not None else (
                "NOT NEEDED" if status == "not-needed" else "EMPTY")
            line = f"tier {tier} {r['role']}#{r['rank']}: {what} — {r['reason']}"
            if r["product_id"] is None and i < len(tt) and tt[i]:
                line += f"  [{tt[i]}]"
            out.append(line)
    notes = plan.get("lists") or {}
    for lst, rows in sorted((plan.get("items") or {}).items()):
        line = f"list {lst}: {[r['product_id'] for r in rows]}"
        n = notes.get(lst)
        if n:
            line += f" — {n['filled']}/{n['size']} filled: {n['reason']}"
            if n.get("trace"):
                line += f"  [{n['trace']}]"
        out.append(line)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
