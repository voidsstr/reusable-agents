"""One authoring run, as phases (one run_ts, one progress.json in live mode):

  window      blackout windows and the hard not-before date (no Opus inside)
  pending     re-run the publish gate for this engine's own held drafts (live)
  gate        article_output_gate.evaluate (once)
  topic+facts the first calendar entry that is ready, admitted by the gate
              (select_for_queue on its tags), whose SQL facts pack builds (no
              prices, no Amazon text: asserted) with enough live pages to
              link; an entry that fails is recorded and the next one tried
  pool/model  Opus only, the day's Opus minutes (a local ledger shared with
              live-model dry runs) and dry-run call caps, claude_pool
              readiness (>= min, > CLAUDE_POOL_RESERVE), no recent
              production-author defers (fail closed), a provider that can
              serve the model, claude-cli isolation
  draft       ONE model call returning strict JSON (one re-prompt on failure)
  checks      in-process checks (article_checks) before any INSERT
  insert      INSERT as the site's queue status (live only)
  promote     framework.cli.article_publish_gate --commit --no-requeue (live only)
  record      output-gate ledger, goal metrics, featured rotation (opt-in)

A dry run stops after the checks and writes the draft, the rendered markdown
and the check report to a local directory: no INSERT, no storage write, no
state advance. Its model is a canned fake unless --live-model is given (then
the window and pool guards apply as in a live run, the output gate is only
reported).
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping, Optional

from framework.core import article_output_gate as og
from framework.core import authoring_capacity as cap
from framework.core import content_policy
from framework.core import editorial_calendar as ecal
from framework.core import facts_pack as fp
from framework.core import required_model as rm

import article_checks as checks_mod
import article_prompt as prompt_mod

FW_ROOT = Path(__file__).resolve().parents[2]
METRIC_KEYS = ("guides_published", "gate_closed", "deferred_pool", "checks_failed", "opus_minutes", "blackout",
               "topic_ready", "model_calls", "held", "gate_held", "facts_rows")


@dataclasses.dataclass
class Outcome:
    status: str = "success"            # success | failure
    outcome: str = ""                  # blackout | not-before | no-topic | gate-closed | deferred | held | ...
    summary: str = ""
    metrics: dict = dataclasses.field(default_factory=lambda: {k: 0.0 for k in METRIC_KEYS})
    persist_signals: bool = False      # True only when the result depends on signal inputs alone
    state: dict = dataclasses.field(default_factory=dict)
    report: dict = dataclasses.field(default_factory=dict)
    error_text: str = ""
    artifacts_dir: str = ""

    def stop(self, outcome: str, summary: str, *, persist: bool = False, status: str = "success") -> "Outcome":
        self.outcome, self.summary, self.persist_signals, self.status = outcome, summary, persist, status
        return self


@dataclasses.dataclass
class Deps:
    settings: Any
    storage: Any                                  # read in every mode; written only in live mode
    store_factory: Callable[[], Any]              # -> article_store.SiteStore
    model_factory: Optional[Callable[[str, str], Any]] = None   # (agent_id, model) -> AIClient-like
    gate_fn: Callable[..., Any] = og.evaluate
    pool_fn: Callable[..., cap.Decision] = cap.pool_decision
    ready_fn: Optional[Callable[[str], Any]] = None
    publish_fn: Optional[Callable[[str], dict]] = None
    now: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc)
    say: Callable[..., None] = lambda *a, **k: None
    decide: Callable[..., None] = lambda *a, **k: None
    environ: Optional[Mapping[str, str]] = None
    ledger: Any = None                            # authoring_capacity.UsageLedger (default: ledger_path(agent))


@dataclasses.dataclass
class RunOptions:
    dry_run: bool = False
    live_model: bool = False            # dry run with the real model
    out_dir: str = ""
    entry_id: str = ""                  # dry run: force one calendar entry
    fake_response: Any = None           # dry run: the canned model returns this verbatim
    adversarial: str = ""               # dry run: appended to the user message (red team)
    run_ts: str = ""


def _env(deps: Deps) -> Mapping[str, str]:
    return os.environ if deps.environ is None else deps.environ


def active_occasions(settings, today: _dt.date, storage=None) -> list:
    """NOW / IMMINENT occasions for the site's audience: the site's own list
    (occasions.source: site) or the framework calendar (source: calendar)."""
    from framework.core import seasonal_calendar as sc
    occ = settings.occasions or {}
    if occ.get("source") == "calendar":
        acts = sc.active_signal(today, audience=settings.audience, storage=storage)
    else:
        acts = sc.active_signal(today, occasions=site_occasions(settings), audience=settings.audience)
    return [a for a in acts if a.window in ("now", "imminent")]


def site_occasions(settings) -> list:
    """The site's own occasions (``occasions.list``) as seasonal_calendar.Occasion."""
    from framework.core import seasonal_calendar as sc
    known = {f.name: f for f in dataclasses.fields(sc.Occasion)}
    out = []
    for d in (settings.occasions or {}).get("list") or []:
        if not isinstance(d, Mapping) or not d.get("id") or not d.get("month_day"):
            continue
        kw = {}
        for k, v in d.items():
            if k not in known or k == "guidance":       # never an occasion's authoring guidance text
                continue
            if k in ("recipe_keywords", "bucket_hints", "link_categories", "aliases"):
                v = tuple(v) if isinstance(v, (list, tuple)) else ((v,) if v else ())
            elif k == "year_dates":
                v = tuple((str(y), str(md)) for y, md in (v.items() if isinstance(v, Mapping) else v or ()))
            kw[k] = v
        kw.setdefault("label", kw["id"])
        try:
            out.append(sc.Occasion(**kw))
        except TypeError:
            continue
    return out


def required_tags(settings, entry, occasion: str) -> list[str]:
    tags = []
    if entry.lane:
        tags.append(f"lane:{entry.lane}")
    tags += [f"lane:{l}" for l in settings.gate_lanes]
    if occasion:
        tags.append(f"holiday:{occasion}")
    return list(dict.fromkeys(tags))


def merge_tags(model_tags, required) -> list[str]:
    """Required tags first; model tags kept only when they are not holiday:/lane:
    (the code decides those) and look like tags."""
    out = list(required)
    for t in model_tags or []:
        t = str(t).strip().lower()
        if not t or t.startswith(("holiday:", "lane:")) or len(t) > 60 or not all(c.isalnum() or c in "-:" for c in t):
            continue
        out.append(t)
    return list(dict.fromkeys(out))[:20]


def resolve_model(settings, storage, agent_id: str) -> tuple[str, str]:
    """(tier, model id): the storage required-models config for the dispatch
    kind / agent, else the site's required tier. Never a downgrade."""
    kind = str(settings.model.get("dispatch_kind") or "article-author")
    tier = rm.required_tier_for_rec({}, dispatch_kind=kind, source_agent_id=agent_id, storage=storage)
    tier = tier or str(settings.model.get("required_tier") or "opus")
    model = rm.enforce_writer_model(agent_id=agent_id, dispatch_kind=kind, storage=storage,
                                    fallback_model=rm.MODEL_FOR_TIER.get(tier, "claude-opus-5-5"))
    return tier, model


# ---------------------------------------------------------------------------
# claude-cli isolation. framework.core.ai_providers runs `claude --print` with
# tools on, --dangerously-skip-permissions and the inherited working directory
# (so the project's CLAUDE.md and memory load). Until a no-tools mode lands in
# ai_providers (post-12-02 follow-up, WIN1), every claude-cli call this engine
# makes goes through bin/claude-isolated (tools off, no MCP servers) from a
# fresh empty working directory, and a live call is refused unless the
# operator has verified the wrapper against the installed claude
# (model.claude_cli.verified).
# ---------------------------------------------------------------------------

WRAPPER = Path(__file__).resolve().parent / "bin" / "claude-isolated"
REAL_CLAUDE_ENV = "AFFILIATE_ARTICLE_REAL_CLAUDE"


def cli_wrapper(settings) -> str:
    return str((settings.model.get("claude_cli") or {}).get("wrapper") or WRAPPER)


def isolation_problem(settings) -> str:
    """'' when claude-cli calls may run, else why not."""
    cli = dict(settings.model.get("claude_cli") or {})
    w = Path(cli_wrapper(settings))
    if not (w.is_file() and os.access(w, os.X_OK)):
        return f"the isolating wrapper {w} is missing or not executable"
    if not cli.get("verified"):
        return ("model.claude_cli.verified is false: the operator has not yet checked bin/claude-isolated's flags "
                "against the installed claude (go-live checklist); the claude-cli path would run with tools and "
                "project memory")
    return ""


@contextlib.contextmanager
def isolated_cli(settings) -> Iterator[str]:
    """For one claude-cli call: CLAUDE_CLI_CMD -> the wrapper (the previous
    binary in AFFILIATE_ARTICLE_REAL_CLAUDE) and the working directory -> a
    fresh empty temp dir (no project CLAUDE.md, no project memory)."""
    prev_cmd = os.environ.get("CLAUDE_CLI_CMD")
    prev_real = os.environ.get(REAL_CLAUDE_ENV)
    prev_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="aae-cli-") as td:
        try:
            if prev_cmd and Path(prev_cmd).resolve() != Path(cli_wrapper(settings)).resolve():
                os.environ[REAL_CLAUDE_ENV] = prev_cmd
            os.environ["CLAUDE_CLI_CMD"] = cli_wrapper(settings)
            os.chdir(td)
            yield td
        finally:
            os.chdir(prev_cwd)
            for k, v in (("CLAUDE_CLI_CMD", prev_cmd), (REAL_CLAUDE_ENV, prev_real)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


@contextlib.contextmanager
def _nullctx() -> Iterator[None]:
    yield None


def scenario_facts(entry) -> fp.NumericFacts:
    nf = fp.NumericFacts()
    for unit, vals in (entry.scenario or {}).items():
        for v in vals:
            nf.add(unit, v)
    return nf


def default_publish_fn(settings, environ: Optional[Mapping[str, str]] = None) -> Callable[[str], dict]:
    def run(slug: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="aae-gate-") as td:
            rep = os.path.join(td, "report.json")
            cmd = [sys.executable, "-m", "framework.cli.article_publish_gate", "--site", settings.site, "--commit",
                   "--no-requeue", "--slug", slug, "--report-json", rep,
                   *[str(a) for a in (settings.publish.get("extra_args") or [])]]
            env = dict(os.environ if environ is None else environ)
            env["PYTHONPATH"] = str(FW_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            try:
                r = subprocess.run(cmd, cwd=str(FW_ROOT), env=env, capture_output=True, text=True,
                                   timeout=int(settings.publish.get("timeout_s") or 300))
            except subprocess.TimeoutExpired:
                return {"promoted": False, "held": True, "exit": -1, "reasons": ["publish gate timed out"],
                        "errors": ["timeout"]}
            try:
                report = json.loads(Path(rep).read_text())
            except (OSError, ValueError):
                report = {}
        return parse_gate_report(report, slug, r.returncode, r.stderr)
    return run


def parse_gate_report(report: Mapping[str, Any], slug: str, code: int, stderr: str = "") -> dict:
    promoted = slug in (report.get("promoted") or [])
    reasons = []
    for d in report.get("drafts") or []:
        if d.get("slug") == slug:
            for c in d.get("checks") or []:
                if c.get("passes"):
                    continue
                det = c.get("addendum") or (json.dumps(c.get("details"), default=str)[:600] if c.get("details")
                                            else "")
                reasons.append(f"{c.get('name')}: {c.get('reason') or ''}".strip() + (f"\n      {det}" if det else ""))
    errors = list(report.get("errors") or [])
    if not report and stderr:
        errors.append(stderr.strip().splitlines()[-1][:300] if stderr.strip() else "no report")
    return {"promoted": promoted, "held": not promoted, "exit": code, "reasons": reasons, "errors": errors,
            "note": report.get("note", "")}


class GuideRun:
    def __init__(self, deps: Deps, opts: RunOptions, *, agent_id: str, prior_state: Optional[dict] = None):
        self.d, self.o, self.agent_id = deps, opts, agent_id
        self.s = deps.settings
        self.state = dict(prior_state or {})
        self.out = Outcome()
        self.m = self.out.metrics
        self.rep = self.out.report
        self.store = None
        self.model_secs = 0.0

    # ------------------------------------------------------------------
    def run(self) -> Outcome:
        """The run's Outcome. Any exception other than a facts violation is
        re-raised with ``self.out`` (state included: the day's Opus minutes)
        still readable by the caller."""
        try:
            return self._run()
        except fp.FactsPackViolation as e:
            return self.out.stop("facts-violation", f"facts pack refused (fail closed): {e}"[:400],
                                 status="failure")
        finally:
            if self.store is not None:
                self.store.close()
            if self.o.dry_run and self.o.out_dir:
                self._write_artifacts()

    def _uses_live_model(self) -> bool:
        return (not self.o.dry_run) or self.o.live_model

    def _run(self) -> Outcome:
        s, d, o = self.s, self.d, self.o
        now = d.now()
        today = now.date()
        self.rep["now"] = now.isoformat()
        self.rep["mode"] = ("dry-run" + (" (live model)" if o.live_model else " (canned model)")) if o.dry_run else "live"

        # 1. window -----------------------------------------------------------
        if self._uses_live_model():
            w = cap.window_decision(s.schedule, now=now, live=not o.dry_run)
            self.rep["window"] = w.to_dict()
            if not w.allowed:
                self.m["blackout"] = 1.0
                return self.out.stop(w.kind, f"no Opus call: {w.reason}")

        cal = ecal.load_calendar(s.calendar_path)
        self.store = d.store_factory()

        # 1b. this engine's own drafts the publish gate held: re-run the gate
        # (no model call) before writing anything new
        if not o.dry_run and s.publish.get("enabled", True):
            got = self._retry_own_pending(now)
            if got is not None:
                return got

        # 2. output gate (once per run) ---------------------------------------
        d.say("output gate", progress=0.15)
        dsn = str(_env(d).get(s.dsn_env, "") or "") or None
        decision = d.gate_fn(storage=d.storage, agent_id=self.agent_id, site_id=s.site, dsn=dsn,
                             site_cfg=dict(s.output_gate), audience=s.audience, now=now, today=today)
        self.rep["gate"] = {"allowed": bool(decision.allowed), "reason": decision.reason, "mode": decision.mode,
                            "slots": decision.slots, "lane_ids": list(decision.lane_ids or []),
                            "require_holiday_ids": list(decision.require_holiday_ids or [])}
        self.m.update({k: float(v) for k, v in decision.metrics().items()})
        if not decision.allowed:
            self.m["gate_closed"] = 1.0
            if not o.dry_run:
                return self.out.stop("gate-closed", f"output gate closed: {decision.reason}")
            self.rep["gate"]["dry_run_note"] = "a live run would stop here; the dry run continues"

        # 3. topic + facts: the first calendar entry that is ready, admitted by
        # the gate, whose facts pack builds and offers enough live links. An
        # entry that cannot be used is recorded and the next one is tried, so
        # one bad entry never wedges the calendar.
        d.say("choosing the topic", progress=0.2)
        done = () if (o.dry_run and o.entry_id) else self.store.done_slugs()
        acts = active_occasions(s, today, d.storage)
        self.rep["active_occasions"] = [{"id": a.occasion.id, "window": a.window, "when": a.when.isoformat()}
                                        for a in acts]
        tried: dict[str, str] = {}
        violations: list[str] = []
        lane_refused: list[str] = []
        self.rep["topic_attempts"] = attempts_log = []
        entry = facts = choice = None
        tags: list[str] = []
        for _ in range(int(s.max_entries_per_run)):
            choice = ecal.choose(cal, done_slugs=done, today=today, active_occasions=[a.occasion.id for a in acts],
                                 count_fn=self.store.count, flags=s.flags, only=o.entry_id, exclude=tried)
            if choice.entry is None:
                break
            e = choice.entry
            etags = required_tags(s, e, choice.occasion)
            kept, _dropped = og.select_for_queue([{"slug": e.slug, "tags": etags}], decision)
            if decision.allowed and not kept:
                why = (f"the gate is open only for {decision.require_holiday_ids or []} / lanes "
                       f"{decision.lane_ids or []} and the tags {etags} do not qualify")
                attempts_log.append({"entry": e.id, "skipped": why})
                lane_refused.append(e.id)
                if not o.dry_run:
                    tried[e.id] = "output gate lane"
                    if o.entry_id:
                        break
                    continue
                self.rep["gate"]["dry_run_note"] = "a live run would skip this entry; the dry run continues"
            d.say(f"facts pack for {e.id}", progress=0.3)
            params = {**dict(s.fact_params), **dict(e.scope)}
            try:
                pack = self.store.facts(params)
            except fp.FactsPackViolation as ex:
                msg = f"facts pack refused (fail closed): {ex}"[:400]
                attempts_log.append({"entry": e.id, "skipped": msg})
                violations.append(f"{e.id}: {msg}")
                d.decide("warning", f"{e.id}: {msg}")
                tried[e.id] = "facts pack refused"
                if o.entry_id:
                    break
                continue
            if not pack.ready:
                attempts_log.append({"entry": e.id, "skipped": f"facts pack empty: {pack.empty_required}"})
                tried[e.id] = "facts pack empty"
                if o.entry_id:
                    break
                continue
            live_supply = self.store.indexable_paths(pack.paths)
            short = []
            for c in s.link_classes:
                have = {p for p in live_supply if any(p.startswith(r) for r in c.roots)}
                if len(have) < c.min:
                    short.append(f"{c.name} {len(have)}/{c.min}")
            self.rep["link_supply"] = {"entry": e.id, "live_paths": len(live_supply), "short": short}
            if short:
                attempts_log.append({"entry": e.id, "skipped": f"too few live pages to link ({', '.join(short)})"})
                tried[e.id] = "too few live links"
                if o.entry_id:
                    break
                continue
            entry, facts, tags = e, pack, etags
            break
        self.rep["topic"] = choice.to_dict() if choice is not None else {}
        self.rep["topic"]["tried"] = dict(tried)
        if entry is None:
            if violations:
                self.out.error_text = "\n".join(violations)[:4000]
                return self.out.stop("facts-violation", "no usable calendar entry; facts pack refused for "
                                     + "; ".join(violations)[:380], status="failure")
            if lane_refused and not o.dry_run:
                self.m["gate_closed"] = 1.0
                return self.out.stop("gate-closed", f"output gate admits none of the ready entries "
                                     f"({', '.join(lane_refused[:6])}): {decision.reason}")
            skipped = (choice.skipped if choice is not None else [])
            return self.out.stop("no-topic", ("no calendar entry is usable: " if tried else
                                              "no calendar entry is ready: ") + "; ".join(
                [f"{k}: {v}" for k, v in tried.items()] + [f"{x['id']}: {x['reason']}" for x in skipped[:6]
                                                           if x["id"] not in tried])[:480], persist=not tried)
        self.facts = facts
        self.m["topic_ready"] = 1.0
        self.m["facts_rows"] = float(sum(len(v) for v in facts.rows.values()))
        occ = next((a for a in acts if a.occasion.id == choice.occasion), None) if choice.occasion else None
        self.rep["required_tags"] = tags
        self.rep["topic"]["chosen"] = entry.id
        d.decide("plan", f"topic {entry.id} ({entry.slug}); tags {tags}")

        # 4. pool and model ---------------------------------------------------
        tier, model = resolve_model(s, d.storage, self.agent_id)
        self.rep["model"] = {"tier": tier, "model": model}
        if self._uses_live_model():
            if tier != "opus" or model != rm.MODEL_FOR_TIER.get("opus"):
                return self.out.stop("deferred", f"required model resolves to {tier!r}/{model!r}, not Opus "
                                     f"{rm.MODEL_FOR_TIER.get('opus')!r}: deferring (never a downgrade)")
            day = today.isoformat()
            try:
                ledger = self._ledger()
                used_today = ledger.day(day)
            except cap.LedgerError as e:
                self.m["deferred_pool"] = 1.0
                return self.out.stop("deferred", f"the day's Opus ledger is unreadable ({e}); deferring")
            used_state = float(self.state.get("opus_minutes_day") or 0.0) if self.state.get("opus_day") == day else 0.0
            used = max(used_today["opus_minutes"], used_state)
            self.rep["budget"] = {"day": day, "opus_minutes_used": used, **used_today}
            cap_min = float(s.model.get("max_minutes_per_day") or 0)
            if cap_min and used >= cap_min:
                self.m["deferred_pool"] = 1.0
                return self.out.stop("deferred", f"Opus minutes cap reached today ({used:.1f}/{cap_min:g} min)")
            max_calls = int(s.model.get("max_calls_per_run") or 2)
            if o.dry_run:
                dry_cap = int(s.model.get("max_dry_run_calls_per_day") or 0)
                if used_today["dry_run_calls"] + max_calls > dry_cap:
                    self.m["deferred_pool"] = 1.0
                    return self.out.stop("deferred", f"live-model dry-run calls cap reached today "
                                         f"({used_today['dry_run_calls']}/{dry_cap})")
            watch = dict(s.pool.get("peer_defer_watch") or {})
            pool = d.pool_fn(model=model, min_ready=int(s.pool.get("min_ready_profiles") or 2), storage=d.storage,
                             peer_patterns=list(watch.get("agent_patterns") or []),
                             peer_reasons=list(watch.get("reasons") or []),
                             peer_hours=float(watch.get("lookback_hours") or 6), self_agent=self.agent_id,
                             ready_fn=d.ready_fn, environ=d.environ, now=now)
            self.rep["pool"] = pool.to_dict()
            if not pool.allowed:
                self.m["deferred_pool"] = 1.0
                return self.out.stop("deferred", f"deferred, no model call: {pool.reason}")
            if d.model_factory is None:
                return self.out.stop("deferred", "no model client factory", status="failure")
            try:
                client = d.model_factory(self.agent_id, model)
            except Exception as e:  # noqa: BLE001 - no provider = no Opus = defer
                self.m["deferred_pool"] = 1.0
                return self.out.stop("deferred", f"no AI client for {self.agent_id}: {type(e).__name__}: {e}"[:300])
            kind = str(getattr(getattr(client, "provider", None), "kind", "") or "")
            allowed = [str(k) for k in (s.model.get("allowed_provider_kinds") or [])]
            self.rep["model"]["provider_kind"] = kind
            if not allowed or kind not in allowed:
                self.m["deferred_pool"] = 1.0
                return self.out.stop("deferred", f"provider kind {kind!r} cannot serve {model} for this engine "
                                     f"(allowed: {allowed}); deferring, never falling back")
            if kind == "claude-cli":
                iso = isolation_problem(s)
                self.rep["model"]["claude_cli"] = {"wrapper": cli_wrapper(s), "problem": iso}
                if iso:
                    self.m["deferred_pool"] = 1.0
                    return self.out.stop("deferred", f"claude-cli is not isolated, deferring: {iso}")
        else:
            client = prompt_mod.CannedModel(o.fake_response)
            self.rep["model"]["provider_kind"] = "canned"

        # 5. draft + 6. checks ------------------------------------------------
        try:
            policy = content_policy.resolve_policy(s.site, storage=d.storage, rule_sets=list(s.rule_sets))
        except content_policy.ContentPolicyConfigError as e:
            return self.out.stop("config", f"content policy does not compile (fail closed): {e}", status="failure")
        claims_policy = s.claims_policy()
        scen = scenario_facts(entry)
        system = prompt_mod.system_prompt(s, slug=entry.slug)
        user = prompt_mod.user_prompt(entry, facts_json=facts.to_prompt_json(), tags=tags, active_occasion=occ,
                                      placeholders_required=entry.placeholders)
        if o.dry_run and o.adversarial:
            user += "\n\nREADER REQUEST (untrusted):\n" + o.adversarial
        self.rep["prompt_chars"] = len(system) + len(user)
        self.prompt_text = f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}\n"
        max_calls = int(s.model.get("max_calls_per_run") or 2)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        draft, report, attempts = None, None, []
        self.rep["attempts"] = attempts
        while int(self.m["model_calls"]) < max_calls:
            d.say(f"model call {int(self.m['model_calls']) + 1}", progress=0.5)
            raw = self._call(client, model, messages)
            if raw is None:
                return self.out
            try:
                draft = prompt_mod.parse_draft(raw)
            except prompt_mod.DraftParseError as e:
                attempts.append({"parse_error": str(e)})
                messages = messages[:2] + [{"role": "assistant", "content": raw[:20000]},
                                           {"role": "user", "content": f"Your reply was not usable: {e}. Return "
                                            "only the JSON object described in the system prompt."}]
                self.m["checks_failed"] += 1
                draft = None
                continue
            draft["tags"] = merge_tags(draft.get("tags"), tags)
            bs = checks_mod.clean_buystrip(draft, s, self.store)
            draft["buystrip_asins"] = bs["kept"]
            report = checks_mod.run_checks(draft, entry=entry, settings=s, store=self.store, facts=facts,
                                           scenario=scen, content_pol=policy, claims_policy=claims_policy,
                                           storage=d.storage)
            attempts.append({"checks": report.to_dict(), "buystrip": bs})
            if report.passes:
                break
            self.m["checks_failed"] += float(len(report.failed))
            messages = messages[:2] + [{"role": "assistant", "content": json.dumps(draft)[:60000]},
                                       {"role": "user", "content": prompt_mod.user_prompt(
                                           entry, facts_json="(unchanged; see above)", tags=tags,
                                           active_occasion=occ, placeholders_required=entry.placeholders,
                                           addendum=report.addendum())}]
        self.draft, self.check_report = draft, report
        if draft is None or report is None or not report.passes:
            self.m["held"] = 1.0
            self.out.state["last_held"] = {"slug": entry.slug, "at": now.isoformat(),
                                           "failed": report.failed if report else ["parse"]}
            if not o.dry_run:
                self._store_held(entry, draft, report)
            return self.out.stop("held", f"draft for {entry.slug} held after {int(self.m['model_calls'])} model "
                                 f"call(s): {report.summary() if report else 'no parseable draft'}"[:500])

        if o.dry_run:
            return self.out.stop("dry-run-pass", f"dry run: {entry.slug} passes every in-process check "
                                 f"(gate {'open' if self.rep['gate']['allowed'] else 'closed'}; nothing written)")

        # 7. insert -----------------------------------------------------------
        d.say("insert pending row", progress=0.8)
        row = self._row(entry, draft, facts)
        if not self.store.insert_article(row):
            return self.out.stop("exists", f"a row with slug {entry.slug} already exists; nothing written")
        self.rep["inserted"] = entry.slug
        og.record_batch(d.storage, self.agent_id, slugs=[entry.slug], run_ts=o.run_ts,
                        holiday_ids=og.holiday_ids_for([draft], audience=s.audience,
                                                       occasions=getattr(decision, "_occasions", None)),
                        lane_ids=og.lane_ids_for([{"tags": [t for t in draft["tags"]
                                                            if t[5:] in s.gate_lanes]}]), now=now)

        # 8. promote ----------------------------------------------------------
        if not s.publish.get("enabled", True):
            return self.out.stop("inserted", f"{entry.slug} inserted as {s.articles['insert_status']} (publish off)")
        publish = d.publish_fn or default_publish_fn(s, d.environ)
        d.say("publish gate", progress=0.9)
        res = publish(entry.slug)
        self.rep["publish"] = [res]
        if not res.get("promoted") and int(self.m["model_calls"]) < max_calls and res.get("reasons"):
            fix = ("THE SITE'S PUBLISH GATE HELD THE DRAFT (its checks and their details):\n"
                   + "\n".join(f"  - {r}" for r in res["reasons"]))
            messages = messages[:2] + [{"role": "assistant", "content": json.dumps(draft)[:60000]},
                                       {"role": "user", "content": prompt_mod.user_prompt(
                                           entry, facts_json="(unchanged; see above)", tags=tags,
                                           active_occasion=occ, placeholders_required=entry.placeholders,
                                           addendum=fix)}]
            raw = self._call(client, model, messages)
            if raw is not None:
                try:
                    d2 = prompt_mod.parse_draft(raw)
                    d2["tags"] = merge_tags(d2.get("tags"), tags)
                    d2["buystrip_asins"] = checks_mod.clean_buystrip(d2, s, self.store)["kept"]
                    r2 = checks_mod.run_checks(d2, entry=entry, settings=s, store=self.store, facts=facts,
                                               scenario=scen, content_pol=policy, claims_policy=claims_policy,
                                               storage=d.storage)
                    self.rep["attempts"].append({"after_gate": r2.to_dict()})
                    if r2.passes and self.store.update_article(entry.slug, self._row(entry, d2, facts)):
                        res = publish(entry.slug)
                        self.rep["publish"].append(res)
                    else:
                        self.m["checks_failed"] += float(len(r2.failed))
                except prompt_mod.DraftParseError as e:
                    self.rep["attempts"].append({"after_gate_parse_error": str(e)})
        if not res.get("promoted"):
            self.m["gate_held"] = 1.0
            self.m["held"] = 1.0
            return self.out.stop("held", f"{entry.slug} inserted but held by the publish gate (re-checked, no "
                                 "model call, at the start of later runs): "
                                 + "; ".join((res.get("reasons") or res.get("errors") or ["no reason"])[:3])[:400])
        self.m["guides_published"] = 1.0
        self._post_publish(entry, now)
        return self.out.stop("published", f"published {entry.slug} ({', '.join(draft['tags'])})")

    # ------------------------------------------------------------------
    def _ledger(self):
        if self.d.ledger is None:
            self.d.ledger = cap.UsageLedger(cap.ledger_path(self.agent_id, self.d.environ))
        return self.d.ledger

    def _retry_own_pending(self, now) -> Optional[Outcome]:
        """Re-run the publish gate (no model call) for up to
        ``publish.retry_pending`` of this engine's own rows still in the queue
        status. A promotion is this run's guide."""
        lister = getattr(self.store, "own_pending_slugs", None)
        limit = int(self.s.publish.get("retry_pending", 2) or 0)
        if lister is None or limit <= 0:
            return None
        try:
            slugs = list(lister(self.agent_id, limit))
        except Exception as e:  # noqa: BLE001 - reported; a new guide may still be written
            self.rep["pending_retry_error"] = f"{type(e).__name__}: {e}"[:200]
            return None
        if not slugs:
            return None
        publish = self.d.publish_fn or default_publish_fn(self.s, self.d.environ)
        results = []
        self.rep["pending_retry"] = results
        for slug in slugs:
            self.d.say(f"publish gate for held draft {slug}", progress=0.1)
            res = publish(slug)
            results.append({"slug": slug, **{k: res.get(k) for k in ("promoted", "reasons", "errors")}})
            if res.get("promoted"):
                self.m["guides_published"] = 1.0
                self.rep["publish"] = [res]
                fr_entry = SimpleNamespace(slug=slug)
                self._post_publish(fr_entry, now)
                return self.out.stop("published", f"published the held draft {slug} on a later gate run "
                                     "(no model call)")
        return None

    def _call(self, client, model: str, messages) -> Optional[str]:
        t0 = time.monotonic()
        self.m["model_calls"] += 1
        live = self._uses_live_model()
        kind = str(getattr(getattr(client, "provider", None), "kind", "") or "")
        try:
            with (isolated_cli(self.s) if (live and kind == "claude-cli") else _nullctx()):
                raw = client.chat(messages, model=model, max_tokens=int(self.s.model.get("max_tokens") or 16000),
                                  temperature=float(self.s.model.get("temperature") or 0.3))
        except Exception as e:  # noqa: BLE001 - classify: capacity -> defer, anything else -> failure
            text = f"{type(e).__name__}: {e}"
            if any(k in text.lower() for k in ("rate limit", "rate-limit", "usage limit", "reserve", "exit 75",
                                                 "status 75", "overloaded", "quota", "429", "529")):
                self.m["deferred_pool"] = 1.0
                self.out.stop("deferred", f"model unavailable, deferring (no fallback): {text}"[:400])
            else:
                self.out.stop("model-error", f"model call failed: {text}"[:400], status="failure")
            return None
        finally:
            secs = time.monotonic() - t0
            if live:
                self.model_secs += secs
                self.m["opus_minutes"] = round(self.model_secs / 60.0, 3)
                day = self.d.now().date().isoformat()
                prev = float(self.state.get("opus_minutes_day") or 0.0) if self.state.get("opus_day") == day else 0.0
                self.out.state["opus_day"] = day
                self.out.state["opus_minutes_day"] = round(prev + secs / 60.0, 3)
                try:
                    self._ledger().add(day, minutes=secs / 60.0, live_calls=0 if self.o.dry_run else 1,
                                       dry_run_calls=1 if self.o.dry_run else 0)
                except cap.LedgerError as e:
                    self.rep["ledger_error"] = str(e)[:200]
        return str(raw or "")

    def _row(self, entry, draft: Mapping[str, Any], facts) -> dict:
        cites = sorted({v for rows in facts.rows.values() for r in rows for k, v in r.items()
                        if isinstance(v, str) and k.endswith("source_url") and v.startswith("https://")})[:12]
        words = len(str(draft.get("body_md") or "").split())
        return {"slug": entry.slug, "title": draft["title"], "subtitle": draft.get("subtitle") or None,
                "excerpt": draft["excerpt"], "body_md": draft["body_md"], "tags": list(draft["tags"]),
                "asins": list(draft.get("buystrip_asins") or []), "faqs": draft.get("faqs") or [],
                "citations": cites or None, "written_by": self.agent_id[:100],
                "primary_keyword": (entry.working_title or draft["title"])[:200],
                "read_time": max(1, int(math.ceil(words / 230)))}

    def _store_held(self, entry, draft, report) -> None:
        try:
            self.d.storage.write_json(f"agents/{self.agent_id}/runs/{self.o.run_ts}/held-draft.json",
                                      {"entry": entry.id, "slug": entry.slug, "draft": draft,
                                       "checks": report.to_dict() if report else None})
        except Exception:  # noqa: BLE001 - the run record still says why it held
            pass

    def _post_publish(self, entry, now) -> None:
        fr = dict((self.s.post_publish or {}).get("featured_rotation") or {})
        if not fr.get("enabled"):
            return
        try:
            from framework.core import featured_rotation
            featured_rotation.cycle_homepage_features(str(_env(self.d).get(self.s.dsn_env) or ""),
                                                      audience=self.s.audience,
                                                      max_features=int(fr.get("max_features") or 4),
                                                      storage=self.d.storage)
        except Exception as e:  # noqa: BLE001 - a rotation failure never unpublishes
            self.rep["featured_rotation_error"] = f"{type(e).__name__}: {e}"[:200]

    def _write_artifacts(self) -> None:
        topic = (self.rep.get("topic") or {}).get("slug") or "no-topic"
        out = Path(self.o.out_dir) / f"{self.o.run_ts or 'dry-run'}-{topic}"
        out.mkdir(parents=True, exist_ok=True)
        self.out.artifacts_dir = str(out)
        draft = getattr(self, "draft", None)
        report = getattr(self, "check_report", None)
        (out / "report.json").write_text(json.dumps({"outcome": self.out.outcome, "summary": self.out.summary,
                                                     "metrics": self.m, **self.rep}, indent=2, default=str))
        if getattr(self, "facts", None) is not None:
            (out / "facts.json").write_text(json.dumps(self.facts.to_dict(), indent=2, default=str))
        if getattr(self, "prompt_text", None):
            (out / "prompt.txt").write_text(self.prompt_text)
        if draft is not None:
            (out / "draft.json").write_text(json.dumps(draft, indent=2, ensure_ascii=False))
            md = [f"# {draft.get('title', '')}", ""]
            if draft.get("subtitle"):
                md += [f"_{draft['subtitle']}_", ""]
            md += [f"> {draft.get('excerpt', '')}", "", draft.get("body_md", ""), "", "## FAQ", ""]
            for f in draft.get("faqs") or []:
                md += [f"**{f.get('question', '')}**", "", f.get("answer", ""), ""]
            md += [f"<!-- tags: {', '.join(draft.get('tags') or [])}; buystrip: "
                   f"{', '.join(draft.get('buystrip_asins') or [])} -->", ""]
            (out / "article.md").write_text("\n".join(md))
        if report is not None:
            (out / "checks.json").write_text(json.dumps(report.to_dict(), indent=2, default=str))
