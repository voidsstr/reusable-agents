#!/usr/bin/env python3
"""Affiliate Article Engine: one data-backed Opus guide per run, for an
affiliate site whose pages are rendered from its own tables.

A generic AgentBase engine (no site names; every table, SQL string, rule and
threshold is the site's ``article_engine:`` YAML block, see
site.example.yaml). One run = one run_ts / progress.json:

  window -> topic (calendar, in code) -> output gate -> pool / model guards
  -> facts pack (SQL; no prices, no Amazon text) -> ONE model call (strict
  JSON; one re-prompt) -> in-process checks -> INSERT as the queue status
  -> framework.cli.article_publish_gate --commit --no-requeue -> record

Modules: article_settings.py (config), article_store.py (site DB),
article_prompt.py (prompts, JSON parser, canned fake model),
article_checks.py (checks), article_engine.py (the phases). Framework
primitives: authoring_capacity, editorial_calendar, facts_pack,
guide_claims_guard (all new), article_output_gate, content_policy,
article_link_classes, article_claims_guard, required_model, ai_providers,
seasonal_calendar, featured_rotation (read and called, never edited).

CLI
---
  python3 agent.py                       AgentBase run (cron / Run now)
  python3 agent.py --config              resolved settings (no DB, no storage)
  python3 agent.py --dry-run --out DIR   everything up to and including the checks with a
                                         canned model; writes draft.json, article.md,
                                         checks.json, facts.json, prompt.txt, report.json
                                         under DIR; no INSERT, no storage write, no state
      [--live-model]                     the real model (window + pool guards apply)
      [--entry ID]                       force one calendar entry (readiness still checked)
      [--fake-response FILE]             the canned model returns FILE's JSON verbatim
      [--adversarial FILE]               append FILE's text to the user message (red team)
      [--no-storage]                     read no framework storage (repo-default configs)
Options: --site-yaml PATH[#key] (default $AFFILIATE_ARTICLE_CONFIG), --agent-id ID.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

_HERE = Path(__file__).resolve().parent
_FW = _HERE.parents[1]
for _p in (str(_HERE), str(_FW)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402
from framework.core.storage import StorageBackend  # noqa: E402

import article_engine as engine_mod  # noqa: E402
import article_settings as settings_mod  # noqa: E402
import article_store as store_mod  # noqa: E402

ENGINE_ID = "affiliate-article-agent"


class ReadOnlyStorage(StorageBackend):
    """Reads pass through; every write raises (dry runs). `refused` lists the
    keys something tried to write."""
    name = "read-only"

    def __init__(self, inner: Optional[StorageBackend]):
        self.inner = inner
        self.refused: list[str] = []

    def read_bytes(self, key):
        return self.inner.read_bytes(key) if self.inner is not None else None

    def exists(self, key):
        return bool(self.inner is not None and self.inner.exists(key))

    def list_prefix(self, prefix, limit=10000):
        return self.inner.list_prefix(prefix, limit) if self.inner is not None else []

    def list_child_prefixes(self, prefix):
        return self.inner.list_child_prefixes(prefix) if self.inner is not None else []

    def _refuse(self, key):
        self.refused.append(str(key))
        raise PermissionError(f"dry run: storage write refused ({key})")

    def write_bytes(self, key, data, cache_control=None):
        self._refuse(key)

    def delete(self, key):
        self._refuse(key)

    def append_bytes(self, key, data):
        self._refuse(key)


def default_model_factory(storage, settings) -> Callable[[str, str], Any]:
    def make(agent_id: str, model: str):
        from framework.core.ai_providers import ai_client_for
        return ai_client_for(agent_id, override_provider=(settings.model.get("provider") or None),
                             override_model=model, storage=storage)
    return make


def store_factory(settings, *, readonly_only: bool, environ: Optional[Mapping[str, str]] = None):
    def ro():
        return store_mod.connect(settings.dsn_env, readonly=True, statement_timeout_ms=settings.statement_timeout_ms,
                                 application_name=settings.agent_id, environ=environ)

    def rw():
        return store_mod.connect(settings.dsn_env, readonly=False,
                                 statement_timeout_ms=settings.statement_timeout_ms,
                                 application_name=settings.agent_id, environ=environ)

    def make():
        return store_mod.SiteStore(settings, ro(), rw_factory=None if readonly_only else rw, ro_factory=ro)
    return make


class AffiliateArticleAgent(AgentBase):
    agent_id = ENGINE_ID
    name = "Affiliate Article Engine"
    description = ("One data-backed Opus guide per run: topic from the site's calendar, facts from its SQL, "
                   "checks in process, INSERT as pending, promotion only through the publish gate.")
    category = "seo"
    send_run_summary_email = False
    capabilities = [
        declare("read_site_data", "Read the site's views (facts pack, readiness, indexable pages)", risk_level="low"),
        declare("call_opus", "One Opus call (plus one re-prompt) through ai_client_for, only when the output gate, "
                "the blackout windows and the pool guards allow it", risk_level="medium", affects=["claude-pool"]),
        declare("insert_pending_article", "INSERT one draft as the site's queue status (the publish guard forces "
                "it); never publishes itself", risk_level="medium", affects=["site-database"]),
        declare("run_publish_gate", "Run framework.cli.article_publish_gate --commit --no-requeue for the draft",
                risk_level="medium", affects=["site-database", "production"]),
    ]

    def __init__(self, *, settings=None, config_spec: Optional[str] = None, deps_overrides: Optional[dict] = None,
                 environ: Optional[Mapping[str, str]] = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._settings = settings
        self._config_spec = config_spec
        self._overrides = dict(deps_overrides or {})
        self._environ = environ

    def load_settings(self):
        if self._settings is None:
            self._settings = settings_mod.load_settings(self._config_spec, agent_id=self.agent_id,
                                                        environ=self._environ)
        return self._settings

    def signals(self) -> dict | None:
        """Inputs only: calendar digest, data timestamps, written slugs, active
        occasions, the output-gate ledger's date and count. None (no
        short-circuit) when any of them cannot be read."""
        try:
            s = self.load_settings()
            from framework.core import article_output_gate as og
            from framework.core import editorial_calendar as ecal
            cal = ecal.load_calendar(s.calendar_path)
            store = self._overrides.get("store_factory", store_factory(s, readonly_only=True,
                                                                       environ=self._environ))()
            try:
                data = store.data_signal()
                slugs = store.done_slugs()
            finally:
                store.close()
            now = self._overrides.get("now", lambda: _dt.datetime.now(_dt.timezone.utc))()
            acts = engine_mod.active_occasions(s, now.date(), self.storage)
            ledger = og.read_ledger(self.storage, self.agent_id)
            today = now.date().isoformat()
            return {"calendar": cal.digest, "calendar_version": cal.version, "data": data, "slugs": slugs,
                    "occasions": sorted(a.occasion.id for a in acts), "ledger_date": today,
                    "ledger_today": sum(int(b.get("count") or 0) for b in ledger.get("batches") or []
                                        if str(b.get("at", "")).startswith(today)),
                    "flags": dict(s.flags)}
        except Exception as e:  # noqa: BLE001 - no hash = no short-circuit (the run decides)
            self.decisions.observe(f"signals() unavailable, running in full: {type(e).__name__}: {e}"[:300])
            return None

    def run(self) -> RunResult:
        self.status("loading config", progress=0.02)
        try:
            s = self.load_settings()
        except settings_mod.SettingsError as e:
            return RunResult(status="failure", summary=f"config invalid — nothing done: {e}"[:500],
                             error_text=str(e)[:4000], next_state=self._state_without_hash())
        for w in s.warnings:
            self.decide("warning", w)
        deps = engine_mod.Deps(
            settings=s, storage=self.storage,
            store_factory=self._overrides.get("store_factory") or store_factory(s, readonly_only=False,
                                                                                environ=self._environ),
            model_factory=self._overrides.get("model_factory") or default_model_factory(self.storage, s),
            gate_fn=self._overrides.get("gate_fn") or engine_mod.og.evaluate,
            pool_fn=self._overrides.get("pool_fn") or engine_mod.cap.pool_decision,
            ready_fn=self._overrides.get("ready_fn"), publish_fn=self._overrides.get("publish_fn"),
            now=self._overrides.get("now") or (lambda: _dt.datetime.now(_dt.timezone.utc)),
            say=lambda msg, progress=0.0: self.status(msg, progress=progress),
            decide=lambda cat, msg, **kw: self.decide(cat, msg),
            environ=self._environ, ledger=self._overrides.get("ledger"))
        runner = engine_mod.GuideRun(deps, engine_mod.RunOptions(run_ts=self.run_ts), agent_id=self.agent_id,
                                     prior_state=self.state)
        try:
            out = runner.run()
        except Exception as e:  # noqa: BLE001
            # the day's Opus minutes spent before the error survive (state and the ledger)
            return RunResult(status="failure", summary=f"article engine failed: {type(e).__name__}: {e}"[:500],
                             error_text=traceback.format_exc()[-4000:], metrics=dict(runner.out.metrics),
                             next_state={**self._state_without_hash(), **runner.out.state,
                                         "last_outcome": "error"})
        try:
            self.storage.write_json(f"agents/{self.agent_id}/runs/{self.run_ts}/engine-report.json",
                                    {"outcome": out.outcome, "summary": out.summary, "metrics": out.metrics,
                                     **out.report})
        except Exception:  # noqa: BLE001
            pass
        state = {**(self.state if out.persist_signals else self._state_without_hash()), **out.state,
                 "last_outcome": out.outcome}
        return RunResult(status=out.status, summary=out.summary or out.outcome,
                         summary_md=f"**{out.outcome}** — {out.summary}", metrics=dict(out.metrics),
                         next_state=state, error_text=out.error_text,
                         short_circuited=out.outcome in ("blackout", "not-before", "gate-closed", "deferred"))

    def _state_without_hash(self) -> dict:
        return {k: v for k, v in (self.state or {}).items() if k != self.SIGNALS_STATE_KEY}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def dry_run(args, *, environ: Optional[Mapping[str, str]] = None, storage=None, deps_overrides=None) -> dict:
    """The --dry-run path (no AgentBase lifecycle, so no status / run record
    writes): storage is read-only, the DB session is read-only, nothing is
    inserted and no state advances."""
    env = dict(os.environ if environ is None else environ)
    env.pop("AGENT_RUN_ID", None)
    env.pop("RUN_ID", None)
    agent_id = args.agent_id or env.get("AGENT_ID") or ENGINE_ID
    s = settings_mod.load_settings(args.site_yaml or None, agent_id=agent_id, environ=env)
    if storage is None and not args.no_storage:
        from framework.core.storage import get_storage, register_backend, reset_default_backend
        inner = get_storage()
        ro = ReadOnlyStorage(inner)
        # every framework module that calls get_storage() (LLM usage logs) gets the read-only view
        register_backend("article-engine-dry-run", lambda: ro)
        # Child processes (the claude-pool shim) cannot resolve this in-process
        # backend; isolated_cli() hands them the parent's real one.
        os.environ.setdefault("AAE_PARENT_STORAGE_BACKEND", os.environ.get("STORAGE_BACKEND", ""))
        os.environ["STORAGE_BACKEND"] = "article-engine-dry-run"
        reset_default_backend()
        storage = ro
    elif storage is None:
        storage = ReadOnlyStorage(None)
    for k in ("AGENT_RUN_ID", "RUN_ID"):
        os.environ.pop(k, None)
    ov = dict(deps_overrides or {})
    fake = None
    if args.fake_response:
        fake = Path(args.fake_response).read_text()
    adversarial = Path(args.adversarial).read_text() if args.adversarial else ""
    run_ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    deps = engine_mod.Deps(
        settings=s, storage=storage,
        store_factory=ov.get("store_factory") or store_factory(s, readonly_only=True, environ=env),
        model_factory=ov.get("model_factory") or default_model_factory(storage, s),
        gate_fn=ov.get("gate_fn") or engine_mod.og.evaluate, pool_fn=ov.get("pool_fn") or engine_mod.cap.pool_decision,
        ready_fn=ov.get("ready_fn"), publish_fn=lambda slug: {"promoted": False, "held": True,
                                                              "reasons": ["dry run never publishes"]},
        now=ov.get("now") or (lambda: _dt.datetime.now(_dt.timezone.utc)),
        say=(lambda msg, progress=0.0: print(f"[dry-run] {msg}", file=sys.stderr)), environ=env,
        ledger=ov.get("ledger"))
    out_dir = args.out or str(Path.cwd() / "article-engine-dry-run")
    opts = engine_mod.RunOptions(dry_run=True, live_model=bool(args.live_model), out_dir=out_dir,
                                 entry_id=args.entry or "", fake_response=fake, adversarial=adversarial,
                                 run_ts=run_ts)
    out = engine_mod.GuideRun(deps, opts, agent_id=agent_id, prior_state={}).run()
    return {"outcome": out.outcome, "status": out.status, "summary": out.summary, "metrics": out.metrics,
            "artifacts": out.artifacts_dir,
            "storage_writes_refused": list(getattr(storage, "refused", []))}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="affiliate-article-agent", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", action="store_true", help="print the resolved settings (no DB, no storage)")
    ap.add_argument("--dry-run", action="store_true", help="draft + checks only; files under --out; no writes")
    ap.add_argument("--live-model", action="store_true", help="with --dry-run: call the real model")
    ap.add_argument("--out", default="", help="with --dry-run: output directory")
    ap.add_argument("--entry", default="", help="with --dry-run: force this calendar entry id")
    ap.add_argument("--fake-response", default="", help="with --dry-run: canned model reply (JSON file)")
    ap.add_argument("--adversarial", default="", help="with --dry-run: text appended to the user message")
    ap.add_argument("--no-storage", action="store_true", help="with --dry-run: read no framework storage")
    ap.add_argument("--site-yaml", default="", help="config PATH[#dotted.key] (default $AFFILIATE_ARTICLE_CONFIG)")
    ap.add_argument("--agent-id", default="", help="instance id (default $AGENT_ID)")
    args = ap.parse_args(argv)
    if args.config:
        agent_id = args.agent_id or os.environ.get("AGENT_ID") or ENGINE_ID
        try:
            s = settings_mod.load_settings(args.site_yaml or None, agent_id=agent_id)
        except settings_mod.SettingsError as e:
            print(f"config invalid: {e}", file=sys.stderr)
            return 2
        print(json.dumps(s.describe(), indent=2, default=str))
        return 0
    if args.dry_run:
        if args.live_model is False and (args.adversarial and not args.fake_response):
            print("[dry-run] note: --adversarial without --live-model goes to the canned model", file=sys.stderr)
        try:
            res = dry_run(args)
        except settings_mod.SettingsError as e:
            print(f"config invalid: {e}", file=sys.stderr)
            return 2
        except store_mod.SiteDBError as e:
            print(f"site database: {e}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=2, default=str))
        return 0 if res["status"] == "success" else 1
    if any((args.live_model, args.out, args.entry, args.fake_response, args.adversarial)):
        print("--live-model/--out/--entry/--fake-response/--adversarial need --dry-run", file=sys.stderr)
        return 2
    AffiliateArticleAgent(config_spec=args.site_yaml or None, agent_id=args.agent_id or None).run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
