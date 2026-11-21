#!/usr/bin/env python3
"""recall-gate-agent: daily CPSC recall + product-safety-warning sync for
every subscribed site. One instance serves all sites.

What one run does
-----------------
1. fetch   the SaferProducts recall API (a LastPublishDate window), the CPSC
           recall CSV and the CPSC product-safety-warning CSV
           (framework.core.recall_gate.SourceFetcher; the CSVs use
           If-Modified-Since against a local cache). The API reads its long
           history (initial_lookback_days) until EVERY subscribed site has
           committed a sync of it (per-site ``api_backfilled_at``), so a site
           subscribed later, or one whose first sync failed, still gets it.
2. match   per subscribed site (config by_site.<site>.enabled): the site's
           catalog (its ``catalog_sql``) against the fetched records plus
           every recall the site already stores, deterministically
           (recall_gate.Matcher). NO LLM: the AgentBase LLM helpers raise.
3. write   recalls, recall_matches and one recall_sync_log row into the
           site's database, in one transaction under an advisory lock.
           Human review decisions are never overwritten; the gate never
           lifts, clears or deletes a match by itself (it requests a review).
4. notify  one email per new match, buy-button flip, newly confirmed match or
           review request, to the fleet operator pair; more than
           ``email.max_per_run`` go out as one summary email. Sent state lives
           in the match row (notified_key), so a re-run sends nothing twice.
           No recipient configured: the run FAILS (it never goes quiet). Nor
           does a broken mail path: a run that hands mail to the transport
           and gets none delivered (msmtp missing, no credential, non-zero
           exit) is a warning, and ``email.fail_after_runs`` (2) such runs in
           a row FAIL the run (state ``email_failure_streak``; any delivered
           email resets it, a run with nothing to send leaves it).
5. verify  0 recalled SKUs with a live buy button: the site's
           ``buy_button.sql`` and/or an HTTP probe of the blocked products'
           live pages (a product that left catalog_sql is probed at the page
           stored with its match). A blocked SKU nobody could check counts as
           live (``recalled_buy_buttons_unverified``), including every blocked
           SKU of a site with no check configured
           (``buy_button_unchecked_sites``); one that left catalog_sql with no
           page to check is reported apart (``recalled_blocked_unlisted``):
           by the catalog_sql contract it renders no page. A violation emails
           an alert (once per distinct set; a failed send counts in
           ``emails_failed`` and is retried next run).
6. report  the cross-site manual queue to agents/<id>/manual-queue.json,
           metrics for the goals (recall_sync_age_h,
           recalled_live_buy_buttons, manual_queue_oldest_h, site_errors).

Fails closed: a storage config that exists but cannot be read, or a config
that lost a site this agent has synced before, ends the run as a failure
with the previous state kept (never "no subscribed sites: nothing to do").
Source state (the API window anchor, CSV validators) advances only when at
least one site committed the data.

Env overrides (operator / tests)
--------------------------------
  RECALL_GATE_DRY_RUN=1          match and report; roll back every write,
                                 send no email, probe nothing, keep the state;
                                 goal metrics are reported as dry_run_<name>
  RECALL_GATE_SITES=a,b          only these subscribed sites
  RECALL_GATE_NOW=<ISO datetime> evaluate as of that instant
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402
from framework.core import recall_gate as rg  # noqa: E402

QUEUE_KEY_TMPL = "agents/{agent_id}/manual-queue.json"
# The metrics goals.json binds to. A dry run reports them as dry_run_<name>,
# so AgentBase's goal auto-tracking never records a rolled-back run.
GOAL_METRICS = ("recall_sync_age_h", "recalled_live_buy_buttons", "manual_queue_oldest_h",
                "site_errors")


class RecallGateLLMForbidden(RuntimeError):
    """The recall gate is deterministic by design; an LLM call is a bug."""


def _truthy(v: Optional[str]) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes")


def default_send_email(*, subject: str, body_html: str, to: list[str], sender: str,
                       msmtp_account: str, bypass_digest: bool) -> tuple[bool, str]:
    """The framework's outbound mail helper (Graph first, msmtp fallback).
    (delivered, detail); a message dropped by DIGEST_DISABLED is not delivered."""
    from shared.site_quality import send_via_msmtp
    ok, detail = send_via_msmtp(subject=subject, body_html=body_html, to=to, sender=sender,
                                msmtp_account=msmtp_account, bypass_digest=bypass_digest,
                                extra_headers={"X-Reusable-Agent": "recall-gate-agent"})
    delivered = bool(ok) and not str(detail).startswith("digest disabled")
    return delivered, str(detail)[:200]


def _hours_since(ts: Any, now: _dt.datetime) -> Optional[float]:
    t = rg._parse_ts(ts)
    return None if t is None else round((now - t).total_seconds() / 3600.0, 2)


class RecallGateAgent(AgentBase):
    agent_id = "recall-gate-agent"
    name = "Recall Gate Agent"
    description = ("Daily CPSC recall and product-safety-warning sync: deterministic "
                   "brand/model matching against every subscribed site's catalog, tiered "
                   "results (stop-use, repair-hold, review) written to the site database, "
                   "one operator email per new match. Never calls an LLM.")
    category = "ops"
    capabilities = [
        declare("fetch_cpsc_sources",
                "GET the SaferProducts recall API and the CPSC recall and warning CSVs "
                "(public, read-only)", risk_level="low"),
        declare("write_site_recalls",
                "Upsert recalls / recall_matches / recall_sync_log in each subscribed site's "
                "database; the site hides the buy button from them",
                risk_level="medium", affects=["site-database"]),
        declare("email_operator", "One email per new recall match to the fleet operator pair",
                risk_level="low", affects=["email"]),
        declare("probe_live_pages",
                "GET recalled products' live pages on the site's own host to confirm no buy "
                "button renders (never follows a redirect off the site)",
                risk_level="low"),
    ]

    # Injectable seams: tests replace them; None means the production default.
    http_get: Optional[Callable[..., rg.HttpResponse]] = None
    db_connect: Optional[Callable[[str, rg.SiteSettings], Any]] = None
    send_email: Optional[Callable[..., tuple[bool, str]]] = None
    cache_dir: Optional[str] = None
    # This run's sends as the transport saw them (every _send: match emails,
    # the overflow summary, the buy-button alert); run() starts it.
    _mail: Optional[dict[str, Any]] = None

    # ---- never an LLM ---------------------------------------------------
    def ai_chat(self, *args: Any, **kwargs: Any) -> str:  # noqa: D401
        raise RecallGateLLMForbidden("recall-gate-agent never calls an LLM")

    def ai_chat_resolved(self, *args: Any, **kwargs: Any) -> Any:
        raise RecallGateLLMForbidden("recall-gate-agent never calls an LLM")

    def ai_client(self, *args: Any, **kwargs: Any) -> Any:
        raise RecallGateLLMForbidden("recall-gate-agent never calls an LLM")

    def signals(self) -> dict | None:
        # No auto short-circuit: the run's job includes proving freshness
        # (recall_sync_age_h) and every input is remote. The CSVs already skip
        # re-downloading via If-Modified-Since, and the site writes are no-ops
        # when nothing changed.
        return None

    # ---- helpers ----------------------------------------------------------
    def _now(self) -> _dt.datetime:
        raw = os.environ.get("RECALL_GATE_NOW", "").strip()
        if raw:
            ts = rg._parse_ts(raw)
            if ts is not None:
                return ts
        return _dt.datetime.now(_dt.timezone.utc)

    def _connect(self, dsn: str, settings: rg.SiteSettings) -> Any:
        fn = self.db_connect or rg.connect_site
        return fn(dsn, settings)

    def _send(self, *, subject: str, body: str, to: list[str], sender: str,
              email_cfg: Mapping[str, Any]) -> tuple[bool, str]:
        fn = self.send_email or default_send_email
        try:
            ok, detail = fn(subject=subject, body_html=body, to=to, sender=sender,
                            msmtp_account=str(email_cfg.get("msmtp_account") or "automation"),
                            bypass_digest=bool(email_cfg.get("bypass_digest", True)))
        except Exception as e:  # noqa: BLE001 - a mail failure must not fail the sync
            ok, detail = False, f"{type(e).__name__}: {e}"[:200]
        if self._mail is not None:
            self._mail["attempts"] += 1
            if ok:
                self._mail["delivered"] += 1
            else:
                self._mail["detail"] = str(detail)[:200]
        return ok, detail

    def _email_streak(self, state: Mapping[str, Any], email_cfg: Mapping[str, Any],
                      now: _dt.datetime) -> tuple[dict[str, Any], str, str]:
        """(next state keys, run failure text, summary line) for the mail path.

        The streak counts runs in a row that handed mail to the transport and
        got none delivered. A run that delivered anything resets it. A run
        that sent nothing (nothing pending, or no recipient, which fails the
        run on its own) leaves it and does not fail on it, as no mail went
        undelivered: the streak waits for the next run with mail to send,
        which fails at once if none of it gets through."""
        tally = self._mail or {}
        attempts, delivered = int(tally.get("attempts") or 0), int(tally.get("delivered") or 0)
        streak = int(state.get("email_failure_streak") or 0)
        since = str(state.get("email_failing_since") or "")
        detail = str(state.get("email_last_error") or "")
        if delivered:
            streak, since, detail = 0, "", ""
        elif attempts:
            since = since if streak > 0 and since else rg._iso(now)
            streak += 1
            detail = str(tally.get("detail") or "")
        keys = {"email_failure_streak": streak, "email_failing_since": since,
                "email_last_error": detail}
        if not streak:
            return keys, "", ""
        runs = f"{streak} run{'' if streak == 1 else 's'}"
        if not attempts:
            return keys, "", (f"email delivery: nothing to send this run; the last {runs} that "
                              f"sent mail delivered none (since {since}): {detail}")
        try:
            limit = max(1, int(email_cfg.get("fail_after_runs", 2)))
        except (TypeError, ValueError):
            limit = 2
        self.decide("warning", f"email delivery: {attempts} send(s), none delivered, {runs} in "
                    f"a row (the run fails at {limit}): {detail}")
        if streak < limit:
            return keys, "", (f"email delivery failed this run ({runs} in a row; the run "
                              f"fails at {limit}): {detail}")
        return (keys, f"email delivery failing since {since} for {runs}: {detail}",
                f"EMAIL DELIVERY FAILING since {since} for {runs}: {detail}")

    @staticmethod
    def _sync_age(sources: Mapping[str, Any], enabled: list[str],
                  site_states: Mapping[str, Any], sites: list[str],
                  now: _dt.datetime) -> Optional[float]:
        """Hours since the recall data was last both fetched (stalest enabled
        source) and committed to each subscribed site's database (a site that
        never committed counts as 9999)."""
        ages: list[float] = []
        src = rg.sync_age_hours(sources, enabled, now)
        if src is not None:
            ages.append(src)
        for site in sites:
            h = _hours_since((site_states.get(site) or {}).get("last_success_at"), now)
            ages.append(9999.0 if h is None else h)
        return max(ages) if ages else None

    @staticmethod
    def _dry_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
        for k in GOAL_METRICS:
            if k in metrics:
                metrics[f"dry_run_{k}"] = metrics.pop(k)
        return metrics

    def _halt(self, why: str, state: Mapping[str, Any], metrics: dict[str, Any],
              now: _dt.datetime, dry_run: bool) -> RunResult:
        """Fail closed and loud: keep the previous state, report the gate as
        down (sync age from the stored per-site successes, every known site
        as an error)."""
        self.decide("warning", f"recall gate halted: {why}")
        sources = dict(state.get("sources") or {})
        known = sorted(s for s, st in (state.get("sites") or {}).items()
                       if isinstance(st, Mapping) and st.get("last_success_at"))
        age = self._sync_age(sources, sorted(sources), state.get("sites") or {}, known, now)
        if age is not None:
            metrics["recall_sync_age_h"] = age
        metrics["site_errors"] = max(1, len(known))
        if dry_run:
            self._dry_metrics(metrics)
        return RunResult(status="failure", summary=f"recall gate halted: {why}"[:300],
                         metrics=metrics, next_state=dict(state), error_text=why[:2000])

    # ---- run ----------------------------------------------------------------
    def run(self) -> RunResult:
        now = self._now()
        dry_run = _truthy(os.environ.get("RECALL_GATE_DRY_RUN"))
        if dry_run:
            # A supervised dry run must not enqueue an agent-doctor run.
            self.auto_recovery_enabled = False
        self._mail = {"attempts": 0, "delivered": 0, "detail": ""}
        state = dict(self.state or {})
        metrics: dict[str, Any] = {
            "sites_subscribed": 0, "sites_synced": 0, "site_errors": 0, "source_errors": 0,
            "recalls_fetched": 0, "recalls_relevant": 0, "matches_total": 0, "matches_new": 0,
            "matches_blocking": 0, "matches_stale": 0, "manual_queue_size": 0,
            "manual_queue_oldest_h": 0.0, "review_requested": 0, "emails_sent": 0,
            "emails_failed": 0, "emails_pending": 0, "llm_calls": 0,
        }
        try:
            cfg = rg.resolve_config(storage=self.storage)
        except rg.RecallGateConfigError as e:
            return self._halt(f"config unreadable: {e}", state, metrics, now, dry_run)
        only = {s.strip() for s in os.environ.get("RECALL_GATE_SITES", "").split(",")
                if s.strip()}
        site_states: dict[str, Any] = {k: dict(v) for k, v in (state.get("sites") or {}).items()
                                       if isinstance(v, Mapping)}
        by_site = cfg.get("by_site") or {}
        # A site this agent has synced before that has no by_site entry at all
        # means the config lost it (an unreadable or truncated storage copy, a
        # bad edit): stop instead of silently protecting nothing. Unsubscribe
        # on purpose with by_site.<site>.enabled = false.
        lost = sorted(s for s, st in site_states.items()
                      if st.get("last_success_at") and s not in by_site)
        if lost and not only:
            return self._halt(
                f"config has no by_site entry for {', '.join(lost)}, which this agent synced "
                "before (storage config lost or truncated?). Restore it, or set "
                "by_site.<site>.enabled=false to unsubscribe on purpose", state, metrics, now,
                dry_run)
        subscribed = rg.subscribed_sites(cfg)
        for s in [s for s in site_states if s in by_site and s not in subscribed]:
            site_states.pop(s)  # unsubscribed on purpose: forget it
        if not cfg.get("enabled", True):
            self.decide("skip", "recall gate disabled in config (enabled=false)")
            known = sorted(s for s, st in site_states.items() if st.get("last_success_at"))
            age = self._sync_age(state.get("sources") or {}, sorted(state.get("sources") or {}),
                                 site_states, known, now)
            if age is not None:
                metrics["recall_sync_age_h"] = age  # an off gate goes stale on the goal
            return RunResult(status="success", summary="recall gate disabled in config",
                             metrics=self._dry_metrics(metrics) if dry_run else metrics,
                             next_state=state)
        sites = [s for s in subscribed if not only or s in only]
        metrics["sites_subscribed"] = len(sites)
        if not sites:
            self.decide("skip", "no subscribed site (by_site.<site>.enabled)")
            return RunResult(status="success", summary="no subscribed sites: nothing to do",
                             metrics=self._dry_metrics(metrics) if dry_run else metrics,
                             next_state=state if dry_run else {**state, "sites": site_states})

        # 1. fetch
        self.status("fetching CPSC sources", progress=0.1, current_action="fetch")
        prior_sources: dict[str, Any] = dict(state.get("sources") or {})
        fetcher = rg.SourceFetcher(cfg, http_get=self.http_get, now=now,
                                   cache_dir=self.cache_dir)
        enabled = fetcher.enabled()
        api_on = rg.SOURCE_API in enabled
        backfill_for = [s for s in sites if not (site_states.get(s) or {}).get("api_backfilled_at")]
        if api_on and backfill_for:
            self.decide("observation", "SaferProducts API: reading the full history window for "
                        + ", ".join(backfill_for))
        results = fetcher.fetch_all(prior_sources, backfill=api_on and bool(backfill_for))
        for name, r in results.items():
            if r.ok:
                self.decide("observation", f"{name}: {len(r.records)} records"
                            + (" (not modified)" if r.not_modified else ""))
            else:
                self.decide("warning", f"{name} failed: {r.error}")
        metrics["source_errors"] = sum(1 for r in results.values() if not r.ok)
        records = rg.merge_sources(results, cfg.get("remedy_options"))
        views = {rid: rg.RecallView(rec) for rid, rec in records.items()}
        metrics["recalls_fetched"] = len(records)
        api_res = results.get(rg.SOURCE_API)
        api_backfilled = bool(api_res and api_res.ok and api_res.state.get("backfill"))
        source_summary = {name: {"ok": r.ok, "records": len(r.records),
                                 "not_modified": r.not_modified, "error": r.error}
                          for name, r in results.items()}

        # 2-5. per site
        alerts: dict[str, Any] = dict(state.get("buy_button_alerts") or {})
        queue_doc: dict[str, Any] = {"generated_at": rg._iso(now), "sites": {}}
        committed: list[str] = []
        email_problems: list[str] = []
        live_total = unverified_total = unchecked_sites = unlisted_total = 0
        lines: list[str] = []
        for i, site in enumerate(sites):
            self.status(f"syncing {site}", progress=0.2 + 0.7 * i / max(1, len(sites)),
                        current_action=f"sync:{site}")
            out = self._sync_one(site, cfg, views, now=now, dry_run=dry_run,
                                 source_summary=source_summary, metrics=metrics,
                                 alerts=alerts)
            prev = dict(site_states.get(site) or {})
            prev["last_attempt_at"] = rg._iso(now)
            if out.get("error"):
                metrics["site_errors"] += 1
                prev["last_error"] = out["error"]
                lines.append(f"{site}: ERROR {out['error']}")
            else:
                metrics["sites_synced"] += 1
                if not dry_run:
                    committed.append(site)
                    prev.update({"last_success_at": rg._iso(now), "last_error": "",
                                 **out.get("summary", {})})
                    if not prev.get("api_backfilled_at") and (not api_on or api_backfilled):
                        prev["api_backfilled_at"] = rg._iso(now)
                queue_doc["sites"][site] = out.get("queue", [])
                bb = out.get("buy_button")
                if bb is not None:
                    live_total += int(bb["live"])
                    unverified_total += int(bb["unverified"])
                    unlisted_total += int(bb.get("unlisted") or 0)
                    unchecked_sites += 0 if bb["configured"] else 1
                if out.get("email_problem"):
                    email_problems.append(f"{site}: {out['email_problem']}")
                lines.append(out.get("line", site))
            site_states[site] = prev
        if committed:
            metrics["recalled_live_buy_buttons"] = live_total
            metrics["recalled_buy_buttons_unverified"] = unverified_total
            metrics["buy_button_unchecked_sites"] = unchecked_sites
            metrics["recalled_blocked_unlisted"] = unlisted_total
        # The API window anchor and the CSV validators only move once a site
        # has committed the data (a dry run or an all-sites failure must not
        # use up the backfill or the window).
        advance = bool(committed)
        new_sources: dict[str, Any] = {}
        for name in enabled:
            got = results.get(name)
            new_sources[name] = rg.next_source_state(
                prior_sources.get(name),
                None if (got is not None and got.ok and not advance) else got, now)
        age = self._sync_age(new_sources, enabled, site_states, sites, now)
        if age is not None:
            metrics["recall_sync_age_h"] = age
        queue_rows = [r for rows in queue_doc["sites"].values() for r in rows]
        metrics["manual_queue_size"] = len(queue_rows)
        metrics["review_requested"] = sum(1 for r in queue_rows if r.get("review_requested_at"))
        metrics["manual_queue_oldest_h"] = max(
            [float(r.get("age_h") or 0.0) for r in queue_rows], default=0.0)
        queue_doc.update({"size": metrics["manual_queue_size"],
                          "oldest_h": metrics["manual_queue_oldest_h"]})
        if not dry_run:
            try:
                self.storage.write_json(QUEUE_KEY_TMPL.format(agent_id=self.agent_id), queue_doc)
            except Exception as e:  # noqa: BLE001 - the site DB is the queue of record
                self.decide("warning", f"manual-queue mirror not written: {e}")

        # 6. report
        # A dry run sends nothing, so it neither reads nor moves the streak.
        mail_state, delivery_problem, mail_line = (
            ({}, "", "") if dry_run else self._email_streak(state, cfg.get("email") or {}, now))
        all_sources_failed = bool(results) and all(not r.ok for r in results.values())
        failed = (all_sources_failed or metrics["site_errors"] > 0 or bool(email_problems)
                  or bool(delivery_problem))
        src_line = ", ".join(f"{n} {'ok' if r.ok else 'FAILED'}" for n, r in results.items())
        if failed:
            summary = (f"recall sync problems: {metrics['source_errors']} source error(s), "
                       f"{metrics['site_errors']} site error(s)"
                       + (f", {len(email_problems)} email config problem(s)"
                          if email_problems else "")
                       + (f"; {delivery_problem}" if delivery_problem else ""))
        elif metrics["matches_new"]:
            summary = (f"{metrics['matches_new']} new recall match(es) across "
                       f"{metrics['sites_synced']} site(s); {metrics['matches_blocking']} "
                       f"blocking, {metrics['manual_queue_size']} in review")
        else:
            summary = (f"no new recall matches; {metrics['sites_synced']} site(s) synced, "
                       f"{metrics['matches_blocking']} blocking, "
                       f"{metrics['manual_queue_size']} in review")
        if dry_run:
            summary = "[dry run] " + summary
        summary_md = "\n".join([
            f"# Recall gate run {self.run_ts}", "",
            f"- sources: {src_line or 'none'}",
            f"- records fetched: {metrics['recalls_fetched']}; sync age "
            f"{metrics.get('recall_sync_age_h')} h "
            f"(goal <= {(cfg.get('goals') or {}).get('sync_age_max_h', 26)} h)",
            *[f"- {line}" for line in lines],
            f"- manual queue: {metrics['manual_queue_size']} (oldest "
            f"{metrics['manual_queue_oldest_h']} h; {metrics['review_requested']} review "
            "request(s) from the gate)",
            f"- emails: {metrics['emails_sent']} sent, {metrics['emails_failed']} failed, "
            f"{metrics['emails_pending']} pending",
            *[f"- EMAIL NOT CONFIGURED: {p}" for p in email_problems],
            *([f"- {mail_line}"] if mail_line else []),
            "- LLM calls: 0 (deterministic matcher)",
        ])
        errors = ([r.error for r in results.values() if r.error] + email_problems
                  + ([delivery_problem] if delivery_problem else []))
        if dry_run:
            # A dry run leaves no trace: same state, goal metrics renamed.
            return RunResult(status="failure" if failed else "success", summary=summary,
                             summary_md=summary_md, metrics=self._dry_metrics(metrics),
                             next_state=state,
                             error_text="; ".join(errors) if failed else "")
        next_state = {**state, "sources": new_sources, "sites": site_states,
                      "buy_button_alerts": alerts, **mail_state}
        return RunResult(status="failure" if failed else "success", summary=summary,
                         summary_md=summary_md, metrics=metrics, next_state=next_state,
                         error_text="; ".join(errors) if failed else "")

    def _sync_one(self, site: str, cfg: Mapping[str, Any],
                  views: Mapping[str, rg.RecallView], *, now: _dt.datetime, dry_run: bool,
                  source_summary: Mapping[str, Any], metrics: dict[str, Any],
                  alerts: dict[str, Any]) -> dict[str, Any]:
        try:
            settings, problems = rg.site_settings(cfg, site)
        except rg.RecallGateConfigError as e:
            return {"error": str(e)}
        if problems:
            self.decide("warning", f"{site}: config problems: {'; '.join(problems)}")
            return {"error": "config: " + "; ".join(problems)}
        dsn, names = rg.resolve_dsn(settings, storage=self.storage)
        if not dsn:
            return {"error": f"no DSN: none of {names or ['<no site registry entry>']} is set"}
        try:
            conn = self._connect(dsn, settings)
        except Exception as e:  # noqa: BLE001 - message only, never the DSN
            return {"error": f"connect failed: {type(e).__name__}"}
        sent = failed = pending = 0
        email_problem = ""
        bb: Optional[dict[str, Any]] = None
        try:
            res = rg.sync_site(conn, settings, views, now=now, dry_run=dry_run,
                               source_summary=source_summary)
            for p in res.catalog_problems[:20]:
                self.decide("warning", f"{site}: {p}")
            if not dry_run:  # a dry run sends nothing and probes nothing
                sent, failed, pending, email_problem = self._notify(conn, settings, cfg, now)
                bb = self._check_buy_buttons(conn, settings, cfg, res, alerts)
        except rg.SiteLockedError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            self.decide("warning", f"{site}: sync failed: {type(e).__name__}: {e}"[:400])
            return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        metrics["recalls_relevant"] += res.recalls_relevant
        metrics["matches_total"] += len(res.matches)
        metrics["matches_new"] += res.matches_new
        metrics["matches_blocking"] += res.blocking
        metrics["matches_stale"] += res.matches_stale
        metrics["emails_sent"] += sent
        metrics["emails_failed"] += failed + (bb["alert_failed"] if bb else 0)
        metrics["emails_pending"] += pending
        self.decide("result", f"{site}: {len(res.matches)} matches ({res.matches_new} new, "
                    f"{res.matches_updated} updated, {res.matches_stale} stale); "
                    f"{res.blocking} blocking; {res.manual_review} in review "
                    f"({res.review_requested} requested by the gate); "
                    f"{res.recalls_relevant} relevant recalls",
                    evidence=res.summary())
        line = (f"{site}: {len(res.matches)} matches ({res.matches_new} new), "
                f"{res.blocking} blocking, {res.manual_review} in review, "
                f"{res.recalls_relevant} relevant recalls"
                + ("" if bb is None else
                   f", {bb['live']} live or unverified buy button(s)"
                   + ("" if bb["configured"] else " (no buy-button check configured)")
                   + (f", {bb['unlisted']} blocked SKU(s) left the catalog with no page "
                      "to check" if bb.get("unlisted") else "")))
        return {"summary": res.summary(), "queue": res.queue, "buy_button": bb,
                "line": line, "email_problem": email_problem}

    def _notify(self, conn: Any, settings: rg.SiteSettings, cfg: Mapping[str, Any],
                now: _dt.datetime) -> tuple[int, int, int, str]:
        """(sent, failed, pending, config problem). One email per new match,
        buy-button flip, newly confirmed match or gate review request."""
        email_cfg = cfg.get("email") or {}
        rows = rg.pending_notifications(conn, settings)
        if not rows:
            return 0, 0, 0, ""
        today = now.date()
        to_send = [r for r in rows if rg.needs_email(r, today)]
        quiet = [r for r in rows if not rg.needs_email(r, today)]
        if quiet:
            rg.mark_notified(conn, settings, quiet, now)
        if not to_send:
            return 0, 0, 0, ""
        if not email_cfg.get("enabled", True) or not settings.email_enabled:
            return 0, 0, len(to_send), ""
        to, sender = rg.email_addresses(email_cfg)
        if not to or not sender:
            problem = (f"{len(to_send)} recall email(s) not sent: no recipient/sender; set "
                       f"one of {email_cfg.get('to_env')} and one of {email_cfg.get('from_env')}")
            self.decide("warning", f"{settings.site}: {problem}")
            return 0, len(to_send), 0, problem
        prefix = str(email_cfg.get("subject_prefix") or "[recall-gate]")
        cap = max(0, int(email_cfg.get("max_per_run", 25)))
        sent = failed = 0
        for row in to_send[:cap]:
            subject, body = rg.render_match_email(settings.site, row, prefix=prefix,
                                                  today=today)
            ok, detail = self._send(subject=subject, body=body, to=to, sender=sender,
                                    email_cfg=email_cfg)
            if not ok:
                failed += 1
                self.decide("warning", f"email for {row['match_key']} failed: {detail}")
                continue
            rg.mark_notified(conn, settings, [row], now)
            sent += 1
            self._record(settings.site, row["match_key"], row, subject, body, to)
        overflow = to_send[cap:]
        if overflow:
            subject, body = rg.render_summary_email(settings.site, overflow, prefix=prefix)
            ok, detail = self._send(subject=subject, body=body, to=to, sender=sender,
                                    email_cfg=email_cfg)
            if ok:
                rg.mark_notified(conn, settings, overflow, now)
                sent += 1
                self._record(settings.site, f"summary:{len(overflow)}", overflow[0], subject,
                             body, to)
            else:
                failed += len(overflow)
                self.decide("warning", f"summary email for {len(overflow)} matches failed: "
                            f"{detail}")
        return sent, failed, 0, ""

    def _record(self, site: str, key: str, row: Mapping[str, Any], subject: str, body: str,
                to: list[str]) -> None:
        rid = hashlib.sha1(f"{site}|{key}|{rg.notification_key(row)}".encode()).hexdigest()[:12]
        try:
            self.record_outbound(request_id=f"recall-{rid}", subject=subject,
                                 body_hash=hashlib.sha256(body.encode()).hexdigest(),
                                 body_excerpt=f"{site}: {row.get('item_label')} / "
                                              f"{row.get('number')} {row.get('title')}",
                                 to=to, expects_response=False)
        except Exception as e:  # noqa: BLE001 - dashboard bookkeeping only
            self.decide("warning", f"outbound record failed: {e}")

    def _check_buy_buttons(self, conn: Any, settings: rg.SiteSettings, cfg: Mapping[str, Any],
                           res: rg.SiteSyncResult, alerts: dict[str, Any]) -> dict[str, Any]:
        """{"configured", "live", "unverified", "unlisted", "violations",
        "alert_failed"} for one site.

        ``live`` counts blocked SKUs that still show a buy button PLUS blocked
        SKUs nobody could verify: the site's ``buy_button.sql`` (when set)
        covers every blocked SKU; otherwise the HTTP probe covers what it
        could fetch, a blocked product that left catalog_sql included (at the
        page stored with its match: a 404 or 410 proves it has no page); with
        no check configured nothing is verified. A blocked product that left
        catalog_sql and that no check can reach (no stored page, or no probe)
        is ``unlisted``: by the catalog_sql contract it renders no page, so it
        is reported (``recalled_blocked_unlisted``), never counted as live.
        Alerts once per distinct set; a failed alert send is ``alert_failed``
        (1) and is retried next run."""
        sql_rows = rg.buy_button_sql_violations(conn, settings)
        hcfg = (settings.buy_button or {}).get("http") or {}
        template = str(hcfg.get("url_template") or "")
        unlisted = set(res.blocked_unlisted)
        if sql_rows is not None:
            unreachable: set[str] = set()
        elif hcfg.get("enabled"):
            unreachable = {it.key for it in res.blocked_items
                           if it.key in unlisted and not rg._item_url(it, template)}
        else:
            unreachable = unlisted
        probe = rg.probe_buy_buttons(
            [it for it in res.blocked_items if it.key not in unreachable], settings,
            http_get=self.http_get,
            user_agent=str((cfg.get("http") or {}).get("user_agent") or ""),
            allowed_hosts=rg.site_hosts(settings, storage=self.storage))
        configured = sql_rows is not None or probe is not None
        blocked = {it.key: it for it in res.blocked_items if it.key not in unreachable}
        if unreachable:
            self.decide("observation",
                        f"{settings.site}: {len(unreachable)} blocked SKU(s) left catalog_sql and "
                        "have no stored page to check; not counted as live buy buttons (the "
                        "catalog_sql contract: it returns every product that still renders a "
                        "page): " + ", ".join(sorted(unreachable)[:10]))
        violations: dict[str, dict[str, Any]] = {}
        for row in sql_rows or []:
            key = rg.item_key(row.get("model_id"), row.get("product_id"))
            violations[key] = {"key": key, "label": row.get("label") or (
                blocked[key].display if key in blocked else key),
                "url": row.get("url") or "", "source": "buy_button.sql"}
        for v in (probe or {}).get("violations") or []:
            violations[v["key"]] = {**v, "source": "live page"}
        why: dict[str, str] = {}
        for err in (probe or {}).get("errors") or []:
            self.decide("warning", f"{settings.site}: buy-button probe: {err}")
            head, _, rest = str(err).partition(": ")
            if head in blocked and rest:
                why.setdefault(head, rest)
        if sql_rows is not None:
            unverified_keys: list[str] = []
        elif probe is not None:
            unverified_keys = [k for k in probe.get("unverified") or [] if k not in violations]
        else:
            unverified_keys = sorted(blocked)
        unverified = [{"key": k, "label": blocked[k].display if k in blocked else k,
                       "why": why.get(k) or ("no buy-button check configured" if not configured
                                             else "the probe could not check it")}
                      for k in unverified_keys]
        found = sorted(violations.values(), key=lambda v: v["key"])
        out = {"configured": configured, "violations": len(found),
               "unverified": len(unverified), "live": len(found) + len(unverified),
               "unlisted": len(unreachable), "alert_failed": 0}
        if not found and not unverified:
            alerts.pop(settings.site, None)
            return out
        digest = hashlib.sha1(("|".join(v["key"] for v in found) + "||"
                               + "|".join(u["key"] for u in unverified)).encode()).hexdigest()[:16]
        if found:
            self.decide("warning", f"{settings.site}: {len(found)} recalled SKU(s) with a live "
                        "buy button: " + ", ".join(v["key"] for v in found[:10]))
        if unverified:
            self.decide("warning", f"{settings.site}: {len(unverified)} blocked SKU(s) not "
                        "verified (counted as live): "
                        + ", ".join(u["key"] for u in unverified[:10]))
        email_cfg = cfg.get("email") or {}
        if (email_cfg.get("enabled", True) and email_cfg.get("alert_on_live_buy_button", True)
                and alerts.get(settings.site) != digest):
            to, sender = rg.email_addresses(email_cfg)
            if to and sender:
                subject, body = rg.render_buy_button_alert(
                    settings.site, found, unverified=unverified,
                    prefix=str(email_cfg.get("subject_prefix") or "[recall-gate]"))
                ok, detail = self._send(subject=subject, body=body, to=to, sender=sender,
                                        email_cfg=email_cfg)
                if ok:
                    alerts[settings.site] = digest
                else:
                    out["alert_failed"] = 1
                    self.decide("warning", f"{settings.site}: buy-button alert email failed "
                                f"(retried next run): {detail}")
        return out


if __name__ == "__main__":
    _result = RecallGateAgent().run_once()
    print(_result.summary_md or _result.summary)
