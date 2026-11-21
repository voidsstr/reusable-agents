#!/usr/bin/env python3
"""Spec Extractor: manufacturer specs -> provenance rows, typed specs,
compatibility edges and runtime estimates, for any catalog niche.

A thin AgentBase engine over two framework primitives:

  framework.core.spec_sources   polite fetching, documents, deterministic
                                per-brand extractors, a batched cheap-model
                                fallback, consolidation with provenance and
                                an adjudication queue (never Amazon text);
  framework.core.compat_graph   versioned compatibility rules.

Phases of one run (spec_extractor_pipeline.py): sources -> extract ->
validate -> compat -> runtime. Each phase short-circuits on unchanged input:
a source whose content hash and extraction fingerprint are unchanged is not
parsed again, a model whose inputs hash is unchanged is not consolidated or
written (short_circuit.partition_by_hash, revisited every N runs), compat
rebuilds only pairs touching changed models, and runtime rows are written
only when they differ.

Every table and column comes from the site's site.yaml block
($SPEC_EXTRACTOR_CONFIG, spec_extractor_settings.py); the niche (fields,
extractors, brands, rules) is a profile in the primitives' storage configs.
Nothing here names a site. The TABLE CONTRACT is in AGENT.md.

CLI
---
  python3 agent.py                        AgentBase run (cron / Run now)
  python3 agent.py --config               resolved settings and per-kind field sets (no DB, no HTTP)
  python3 agent.py --print-sql            the contract SQL this engine runs (no DB, no HTTP)
  python3 agent.py --plan [--limit N]     read-only DB: which sources are due and why (no HTTP)
  python3 agent.py --dry-run --agent-id <instance id> [--model-id ID ...] [--limit N]
                                          LIVE polite fetch + extract + consolidate for a few
                                          models; NO DB, queue or LLM writes, no run record.
                                          Holds the instance's run lock like a cron run (one
                                          fetching process per instance): exit 3 while a run
                                          holds it, exit 2 without the instance id ($AGENT_ID
                                          or database.lock_key also name the lock)
Options: --site-yaml PATH[#key] (default $SPEC_EXTRACTOR_CONFIG), --agent-id ID,
         --spec-config-file / --compat-config-file (local JSON instead of the storage configs).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

_HERE = Path(__file__).resolve().parent
_FW = _HERE.parents[1]  # this engine ships inside the framework repo
for _p in (str(_HERE), str(_FW)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

import spec_extractor_pipeline as pipeline  # noqa: E402
import spec_extractor_settings as settings_mod  # noqa: E402
import spec_extractor_store as store_mod  # noqa: E402

ENGINE_ID = "spec-extractor-agent"


# ---------------------------------------------------------------------------
# Site DB connection: the DSN never reaches a run record, a log or the CLI.
# (pick-builder-agent/agent.py carries the same helpers; one framework copy
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


def default_connect(settings: settings_mod.Settings, *,
                    environ: Optional[Mapping[str, str]] = None, readonly: bool = False):
    """A keepalive Postgres connection to the site's DB. The DSN is read from
    the env var the settings name (a validated variable NAME) and never
    leaves this function: a failure raises :class:`SiteDBConnectError` with
    :func:`connect_error_text` and no chained driver exception (whose text
    can echo the DSN). ``readonly`` (--plan, --dry-run): the session refuses
    every write."""
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


class SpecExtractorAgent(AgentBase):
    agent_id = ENGINE_ID
    name = "Spec Extractor"
    description = ("Fetches manufacturer pages and manuals politely, extracts specs "
                   "deterministically (cheap-model fallback, strong-model adjudication within a "
                   "daily budget), writes provenance rows and typed specs, then compatibility "
                   "edges and runtime estimates. Never parses Amazon text.")
    category = "research"
    send_run_summary_email = False
    capabilities = [
        declare("fetch_manufacturer_sources", "Polite GETs of manufacturer pages and manuals "
                "(robots.txt, per-host rate limit, conditional requests; Amazon refused)",
                risk_level="low", affects=["third-party-sites"]),
        declare("llm_extract", "Batched cheap-model extraction for fields deterministic "
                "extractors missed (only when the site's llm.enabled; claude-pool reserve held)",
                risk_level="medium", affects=["claude-pool"]),
        declare("adjudicate_conflicts", "Strong-model choice among sourced candidates, within "
                "0.1 h/day (only when adjudication.enabled)", risk_level="medium",
                affects=["claude-pool"]),
        declare("write_spec_tables", "Rewrite the site's spec_values rows, typed spec rows and "
                "model status per changed model (operator rows never deleted)",
                risk_level="medium", affects=["site-database"]),
        declare("write_compat_edges", "Upsert / delete compat_edges through compat_graph "
                "(manual rows protected)", risk_level="medium", affects=["site-database"]),
        declare("write_runtime_estimates", "Replace changed runtime estimate rows",
                risk_level="low", affects=["site-database"]),
    ]

    def __init__(self, *, settings: Optional[settings_mod.Settings] = None,
                 config_spec: Optional[str] = None, connect: Optional[Callable] = None,
                 collaborators: Optional[pipeline.Collaborators] = None,
                 environ: Optional[Mapping[str, str]] = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._settings = settings
        self._config_spec = config_spec
        self._connect = connect or default_connect
        self._collab = collaborators or pipeline.Collaborators()
        self._environ = environ

    def signals(self) -> dict | None:
        """No run-level short-circuit: elapsed time is an input (sources fall
        due after refetch_hours, failed ones after their backoff). Every
        phase short-circuits inside run() instead: unchanged source content
        is not parsed, unchanged models are not rewritten, compat skips when
        no node changed. A run with nothing to do reports short_circuited."""
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
            return RunResult(status="failure",
                             summary=f"spec-extractor config invalid — nothing done: {exc}"[:500],
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
        store = store_mod.SpecStore(conn, settings,
                                    reconnect=lambda: self._connect(settings, environ=self._environ))
        try:
            if not store.try_lock(settings.lock_key):
                return RunResult(status="success",
                                 summary=(f"Skipped: another {self.agent_id} run holds the lock "
                                          "(nothing to do this tick)."),
                                 metrics={"skipped_locked": 1}, next_state=dict(self.state),
                                 short_circuited=True)
            run = pipeline.SpecExtractorRun(
                settings, store, storage=self.storage, collab=self._collab,
                prior_state=self.state,
                say=lambda msg, progress=0.0: self.status(msg, progress=progress),
                decide=lambda category, message, evidence=None: self.decide(
                    category, message, evidence=evidence))
            try:
                rep = run.run()
            except Exception as exc:  # noqa: BLE001 - keep the state (AgentBase stores {} on a raise:
                # every model would be re-consolidated and compat fully rebuilt next run)
                store.end_read()
                # a reconnect between phases raises SiteDBConnectError (already
                # redacted); anything else is scrubbed of the password as well
                dsn = self._dsn(settings)
                head = (str(exc) if isinstance(exc, SiteDBConnectError)
                        else f"{type(exc).__name__}: {exc}")
                return RunResult(status="failure",
                                 summary=redact_dsn(f"spec-extractor failed: {head}", dsn)[:500],
                                 error_text=redact_dsn(traceback.format_exc(), dsn)[-4000:],
                                 metrics={k: (round(v, 4) if isinstance(v, float) else v)
                                          for k, v in sorted(run.metrics.items())},
                                 next_state={**self.state, **run.state})
            finally:
                store.unlock(settings.lock_key)
        finally:
            try:
                store.conn.close()
            except Exception:  # noqa: BLE001
                pass
        return RunResult(status=rep.status, summary=rep.summary, summary_md=rep.summary_md,
                         metrics=rep.metrics, error_text=rep.error_text,
                         next_state={**self.state, **rep.state},
                         short_circuited=rep.short_circuited)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _collab_from_args(args) -> pipeline.Collaborators:
    c = pipeline.Collaborators()
    if args.spec_config_file:
        c.spec_file_cfg = json.loads(Path(args.spec_config_file).read_text())
    if args.compat_config_file:
        c.compat_file_cfg = json.loads(Path(args.compat_config_file).read_text())
    return c


def _sql_preview(settings: settings_mod.Settings) -> dict:
    st = store_mod.SpecStore(None, settings)
    out = {"models": st.models_sql(), "sources (light)": st.sources_sql(),
           "operator values predicate": st._operator_pred()}
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="spec-extractor-agent", description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--config", action="store_true",
                      help="print the resolved settings and per-kind field sets (no DB, no HTTP)")
    mode.add_argument("--print-sql", action="store_true", help="print the contract SQL (no DB)")
    mode.add_argument("--plan", action="store_true",
                      help="read-only DB: due sources and why (no HTTP)")
    mode.add_argument("--dry-run", action="store_true",
                      help="LIVE polite fetch + extract + consolidate; no DB / queue / LLM writes; "
                           "holds the instance's run lock (needs the instance id)")
    ap.add_argument("--site-yaml", default="", help="config PATH[#dotted.key] (default $SPEC_EXTRACTOR_CONFIG)")
    ap.add_argument("--agent-id", default="", help="instance id (default $AGENT_ID)")
    ap.add_argument("--spec-config-file", default="", help="spec-sources config JSON (instead of storage)")
    ap.add_argument("--compat-config-file", default="", help="compat-graph config JSON (instead of storage)")
    ap.add_argument("--model-id", action="append", default=[], help="with --dry-run: model id(s)")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    if not (args.config or args.print_sql or args.plan or args.dry_run):
        SpecExtractorAgent(config_spec=args.site_yaml or None,
                           agent_id=args.agent_id or None).run_once()
        return 0
    agent_id = args.agent_id or os.environ.get("AGENT_ID") or ENGINE_ID
    try:
        settings = settings_mod.load_settings(args.site_yaml or None, agent_id=agent_id, storage=None)
    except settings_mod.SettingsError as exc:
        print(f"config invalid: {exc}", file=sys.stderr)
        return 2
    if args.dry_run and agent_id == ENGINE_ID and not settings.database.get("lock_key"):
        # the lock would be the engine's, not the instance's cron run's
        print("--dry-run fetches live and must hold the instance's run lock: pass --agent-id "
              "<instance id> (the cron unit's AGENT_ID) or set database.lock_key", file=sys.stderr)
        return 2
    collab = _collab_from_args(args)
    if args.print_sql:
        for name, sql in _sql_preview(settings).items():
            print(f"-- {name}\n{sql};\n")
        return 0
    storage = None
    if collab.spec_file_cfg is None:
        from framework.core.storage import get_storage
        storage = get_storage()
    engines, common, _f, problems = pipeline.build_engines(settings, storage=storage, collab=collab)
    if args.config:
        _print({"settings": settings.describe(), "problems": problems,
                "kinds": {k: {"fields": sorted(e.cfg.get("fields") or {}), "required": list(e.required),
                              "llm_enabled": e.llm_enabled, "brands": sorted(e.cfg.get("brands") or {})}
                          for k, e in engines.items()},
                "fetch": common.get("fetch"), "adjudication": common.get("adjudication")})
        return 0 if not problems else 1
    if problems:
        print("config problems: " + "; ".join(problems), file=sys.stderr)
        return 1
    try:
        conn = default_connect(settings, readonly=True)
    except SiteDBConnectError as exc:      # redacted text, no traceback
        print(str(exc), file=sys.stderr)
        return 1
    store = store_mod.SpecStore(conn, settings)
    locked = False
    try:
        if args.plan:
            models = store.load_models(engines)
            sources = store.load_sources([m.id for m in models])
            store.end_read()
            by_key = {m.key: m for m in models}
            now = collab.clock()
            rows = []
            for src in sources:
                m = by_key.get(src.model_key)
                if m is None or src.kind not in (settings.fetching.get("fetch_kinds") or []):
                    continue
                eng = engines[m.kind]
                slug = eng.ss.extractors.resolve_brand(m.brand, src.url)
                reasons = pipeline.due_reasons(src, now=now, refetch_hours=float(settings.fetching["refetch_hours"]),
                                               fingerprint=eng.ss.extractors.fingerprint(slug))
                if reasons:
                    rows.append({"source_id": src.id, "model_id": src.model_id, "kind": src.kind,
                                 "url": src.url, "reasons": reasons,
                                 "status": src.state.get("status"),
                                 "next_attempt_at": src.state.get("next_attempt_at")})
            _print({"models": len(models), "sources": len(sources), "due": len(rows),
                    "first": rows[:max(1, args.limit)]})
            return 0
        # --dry-run fetches live, so it holds the instance's run lock as a cron
        # run does: PoliteFetcher keeps the per-host interval, Crawl-delay and
        # 429/503 backoff in one process's memory (pg_try_advisory_lock writes
        # nothing, so the read-only session can take it)
        if not store.try_lock(settings.lock_key):
            print(f"another {agent_id} run holds the lock ({settings.lock_key}); "
                  "not dry-running alongside it", file=sys.stderr)
            return 3
        locked = True
        only = args.model_id or None
        if only is None:
            models = store.load_models(engines)[:max(1, args.limit)]
            only = [m.key for m in models]
            store.end_read()
        from framework.core.storage import get_storage
        rep = pipeline.SpecExtractorRun(settings, store, storage=storage or get_storage(), collab=collab,
                                        prior_state={}, say=lambda *a, **k: None,
                                        decide=lambda *a, **k: None, dry_run=True,
                                        only_models=only).run()
        print(rep.summary)
        print(rep.summary_md)
        return 0 if rep.status == "success" else 1
    finally:
        if locked:
            store.unlock(settings.lock_key)
        store.conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
