#!/usr/bin/env python3
"""event-refresh-agent: post-event refresh for holiday-tagged articles.

Problem (audit issue seasonal-no-post-event-state, 2026-10-01)
---------------------------------------------------------------
Deal pages are written before a sale: "buy now", "worth buying on
October 6-7", "(Prime Big Deal Days Edition)" in the H1. Nothing touched
them after the sale ended, so from the morning after, every one of them
told readers and AI assistants to buy in a sale that was over.
featured_rotation only drops the homepage flag.

What it does
------------
Daily. When a seasonal_calendar occasion of a configured kind (default
`retail-deal`) ended 1..`lookback_days` days ago, for every configured
site it selects the published articles tagged `holiday:<occasion-id>` and
queues one `article-refresh` rec per article through the implementation
queue (responder auto-queue -> implementer). Each rec asks for an
in-place, Opus-written refresh:

  - the event section and FAQ in the past tense, with absolute dates;
  - re-angled to the next sale for the same audience
    (seasonal_calendar.next_occasion, e.g. Black Friday Nov 27, 2026);
  - the event dropped from the title (H1 / headline / meta title) while
    the slug stays;
  - updated_at (dateModified) bumped only in the same write as that
    content change.

Idempotent: a ledger keyed site + occasion + event start + slug means an
article is queued once per event, however many times the agent runs.

Per-site values (DSN env var, article URL template, table/columns) live
in config/event-refresh-config.json: the storage copy replaces the repo
default wholesale. No site name appears in this file.

Env overrides (operator / test):
  EVENT_REFRESH_TODAY=YYYY-MM-DD   evaluate as of that date
  EVENT_REFRESH_DRY_RUN=1          report what would be queued; write nothing
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402
from framework.core import seasonal_calendar as sc  # noqa: E402


CONFIG_KEY = "config/event-refresh-config.json"
_REPO_CONFIG = _REPO_ROOT / "config" / "event-refresh-config.json"
LEDGER_KEY_TMPL = "agents/{agent_id}/state/event-refresh-ledger.json"

REC_TYPE = "article-refresh"
HOLIDAY_TAG_PREFIX = "holiday:"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Occasion kinds whose end triggers a refresh. Holiday recipe pages
    # (Halloween, July 4th) stay framed for next year's holiday, so only
    # sales with a hard end are on by default.
    "kinds": [sc.RETAIL_DEAL_KIND],
    # A run that finds an event ended 1..N days ago still queues it (host
    # down on the day after). The ledger stops double-queueing.
    "lookback_days": 7,
    # Stale-page metric horizon: published articles still tagged with a
    # sale that ended within this many days.
    "metric_horizon_days": 60,
    "max_recs_per_site": 25,
    "ledger_keep_days": 400,
    # Re-angle target: the next occasion of the same kind for the ended
    # event's audience, starting within this many days.
    "next_occasion_horizon_days": 120,
    "default": {
        "dsn_env": "DATABASE_URL",
        "table": "editorial_articles",
        "slug_column": "slug",
        "title_column": "title",
        "tags_column": "tags",
        "status_column": "status",
        "status_value": "published",
        "published_column": "published_at",
        "article_url_template": "",
    },
    # {"<site-id>": {"dsn_env": "...", "article_url_template": "https://.../{slug}"}}
    # Empty -> discover sites from DATABASE_URL_<SITE> env vars.
    "sites": {},
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "sites":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(storage=None, *, repo_config: Path = _REPO_CONFIG) -> dict:
    """DEFAULTS <- repo config/event-refresh-config.json, or the storage
    copy at the same key when present (it replaces the repo file)."""
    doc: Any = None
    if storage is not None:
        try:
            doc = storage.read_json(CONFIG_KEY)
        except Exception:
            doc = None
    if not isinstance(doc, dict):
        try:
            doc = json.loads(Path(repo_config).read_text())
        except Exception:
            doc = {}
    return _merge(DEFAULTS, doc if isinstance(doc, dict) else {})


def resolve_sites(cfg: dict, environ: Optional[dict] = None) -> dict[str, dict]:
    """Per-site settings (default block merged under each site). With no
    `sites` configured, every DATABASE_URL_<SITE> env var is a site."""
    env = os.environ if environ is None else environ
    base = cfg.get("default") or {}
    sites = cfg.get("sites") or {}
    if not sites:
        sites = {}
        for k in sorted(env):
            m = re.match(r"^DATABASE_URL_([A-Z0-9_]+)$", k)
            if m and env.get(k):
                sites[m.group(1).lower().replace("_", "-")] = {"dsn_env": k}
    out: dict[str, dict] = {}
    for site, sc_cfg in sites.items():
        merged = _merge(base, sc_cfg if isinstance(sc_cfg, dict) else {})
        if merged.get("enabled", True) is False:
            continue
        out[str(site)] = merged
    return out


# ---------------------------------------------------------------------------
# Rec construction (pure)
# ---------------------------------------------------------------------------

def ledger_key(site: str, event: sc.OccasionWindow, slug: str) -> str:
    return f"{site}|{event.occasion.id}|{event.start.isoformat()}|{slug}"


def _short_label(occ: sc.Occasion) -> str:
    """'Amazon Prime Big Deal Days (October Prime Day, tech deals)' ->
    'Amazon Prime Big Deal Days'. Labels carry an audience qualifier in
    parentheses that never belongs in reader-facing copy."""
    return re.split(r"\s*\(", occ.label, maxsplit=1)[0].strip() or occ.label


def build_refresh_rec(rec_id: str, *, site: str, article: dict,
                      event: sc.OccasionWindow,
                      next_event: Optional[sc.OccasionWindow],
                      article_url: str = "") -> dict:
    """One Opus article-refresh rec for one article tagged with an ended
    event. The instructions are the contract the implementer follows."""
    slug = article["slug"]
    title = article.get("title") or slug
    ev_name = _short_label(event.occasion)
    ev_dates = event.date_range_label()
    old_tag = HOLIDAY_TAG_PREFIX + event.occasion.id
    if next_event is not None:
        nx_name = _short_label(next_event.occasion)
        nx_dates = next_event.date_range_label()
        reangle = (
            f"Re-angle the page to the next sale for this audience: {nx_name} "
            f"({nx_dates}). Frame the picks as what to watch for then. Do not "
            f"predict or invent {nx_name} prices or discounts; say prices change "
            f"and link to the live listing. In the tags column, replace "
            f"`{old_tag}` with `{HOLIDAY_TAG_PREFIX}{next_event.occasion.id}` so "
            f"homepage rotation and the next post-event refresh follow the new "
            f"framing."
        )
        nxt = {"id": next_event.occasion.id, "label": nx_name,
               "start": next_event.start.isoformat(),
               "end": next_event.end.isoformat(), "dates": nx_dates}
    else:
        reangle = (
            "No next sale is on the calendar for this audience: re-angle to "
            "evergreen buying advice (what a good price is, who should wait). "
            f"Remove `{old_tag}` from the tags column."
        )
        nxt = None
    steps = (
        f"{ev_name} ran {ev_dates} and is over. Refresh this published "
        f"article IN PLACE (same row, same slug, same URL; never insert a new "
        f"article) so it stops telling readers to buy in a sale that has ended. "
        f"Use Claude Opus for every word of prose (required_model_tier=opus; "
        f"defer rather than fall back). Steps: "
        f"(1) Rewrite the {ev_name} section and every FAQ answer about it in the "
        f"past tense with absolute dates ('{ev_name} ran {ev_dates}'). Update the "
        f"FAQ wherever this site stores it (body_md FAQ section and the faqs "
        f"column if the table has one). Keep {ev_name} copy inside one bounded H2 "
        f"section plus the FAQ. "
        f"(2) Remove every relative countdown or present-tense urgency line "
        f"('buy now', 'nine days to decide', 'under two weeks', 'worth buying on "
        f"<date>', 'this week'), in body_md, subtitle and excerpt. "
        f"(3) {reangle} "
        f"(4) Drop the event name ('{ev_name}' and any short form of it, plus "
        f"event qualifiers such as '... Edition' or 'Before ...') from the title "
        f"column, which renders the H1, the "
        f"BlogPosting/Article headline and the meta <title>; also from the "
        f"subtitle/excerpt/meta description if they carry it. Keep the slug "
        f"exactly as is: no new URL, no redirect. "
        f"(5) Keep every factual claim honest: no invented discounts, deal "
        f"prices, ratings, testing or stock claims; a price shown as current "
        f"must come from fresh catalog data, otherwise remove it or label it "
        f"with the date it was observed. Follow the site's article voice rules "
        f"(CLAUDE.md). "
        f"(6) Bump updated_at (drives dateModified) in the SAME UPDATE that "
        f"changes title/body_md; never bump it on its own. "
        f"(7) DB-only change: commit the apply script/migration you used and "
        f"write applied-recs.json (implementer runbook, DB-only recs), then "
        f"send an IndexNow ping for the URL if the site has one wired."
    )
    return {
        "id": rec_id,
        "type": REC_TYPE,
        "rec_type": REC_TYPE,
        "kind": REC_TYPE,
        "dispatch_kind": REC_TYPE,
        "trigger": "event-ended",
        "required_model_tier": "opus",
        "category": "freshness",
        "priority": "high",
        "severity": "medium",
        "tier": "auto-implement",
        "confidence": 0.9,
        "site": site,
        "title": (f"Post-event refresh ({ev_name} ended {event.end.isoformat()}): "
                  f"{title}"),
        "rationale": steps,
        "affected_url": article_url,
        "article_slug": slug,
        "article_id": article.get("id"),
        "event": {"id": event.occasion.id, "label": ev_name,
                  "start": event.start.isoformat(),
                  "end": event.end.isoformat(), "dates": ev_dates,
                  "tag": old_tag},
        "next_occasion": nxt,
        "expected_metric": "ended_event_articles_live",
        "target_files": [],  # body_md/title DB update; commit the apply script
        "audit_issue": "seasonal-no-post-event-state",
    }


# ---------------------------------------------------------------------------
# DB (read-only SELECT)
# ---------------------------------------------------------------------------

def fetch_tagged_articles(dsn: str, tags: list[str], site_cfg: dict,
                          *, limit: int = 500) -> list[dict]:
    """Published articles carrying any of `tags`. Read-only."""
    cols = {k: site_cfg[k] for k in ("table", "slug_column", "title_column",
                                     "tags_column", "status_column",
                                     "published_column")}
    for k, v in cols.items():
        if not _IDENT_RE.match(str(v)):
            raise ValueError(f"event-refresh config: bad identifier {k}={v!r}")
    import psycopg2  # type: ignore
    sql = (
        f"SELECT id, {cols['slug_column']} AS slug, {cols['title_column']} AS title, "
        f"{cols['tags_column']} AS tags, {cols['published_column']} AS published_at "
        f"FROM {cols['table']} "
        f"WHERE {cols['status_column']} = %s AND {cols['tags_column']} && %s::text[] "
        f"ORDER BY {cols['published_column']} DESC NULLS LAST, id LIMIT %s"
    )
    conn = psycopg2.connect(dsn, connect_timeout=15)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(sql, (site_cfg.get("status_value", "published"),
                              list(tags), int(limit)))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

def _today() -> _dt.date:
    v = os.environ.get("EVENT_REFRESH_TODAY", "").strip()
    if v:
        return _dt.date.fromisoformat(v)
    return _dt.date.today()


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes")


class EventRefreshAgent(AgentBase):
    agent_id = "event-refresh-agent"
    name = "Event Refresh Agent"
    description = ("Day after a retail sale ends, queues one Opus article-refresh "
                   "rec per published article tagged holiday:<id>.")
    category = "seo"

    capabilities = [
        declare("read_articles", "SELECT holiday-tagged published articles",
                confirmation_required=False, risk_level="low"),
        declare("queue_refresh_recs",
                "Queue article-refresh recs to the implementation queue",
                confirmation_required=False, risk_level="low",
                affects=["implementer-queue"]),
    ]

    # Injected by tests; production uses fetch_tagged_articles.
    fetch_articles = staticmethod(fetch_tagged_articles)

    def _ledger_key(self) -> str:
        return LEDGER_KEY_TMPL.format(agent_id=self.agent_id)

    def _cfg(self) -> dict:
        return load_config(self.storage)

    def _occasions(self):
        return sc.load_occasions(self.storage)

    def signals(self) -> dict | None:
        # Nothing ended within the metric horizon -> constant hash, the
        # framework skips the run with no DB traffic. Otherwise run every
        # tick: it is one SELECT per site and keeps the stale-page metric
        # current and retries a site that failed.
        cfg = self._cfg()
        ended = sc.recently_ended(
            _today(), lookback_days=int(cfg["metric_horizon_days"]),
            kinds=cfg.get("kinds"), occasions=self._occasions())
        if ended:
            return None
        return {"ended_events": [], "enabled": bool(cfg.get("enabled", True))}

    def run(self) -> RunResult:
        cfg = self._cfg()
        today = _today()
        dry_run = _truthy(os.environ.get("EVENT_REFRESH_DRY_RUN", ""))
        metrics = {"events_ended": 0, "articles_tagged": 0, "recs_queued": 0,
                   "already_queued": 0, "unqueued_ended_event_articles": 0,
                   "ended_event_articles_live": 0, "site_errors": 0}
        if not cfg.get("enabled", True):
            self.decide("skip", "event-refresh disabled in config")
            return RunResult(status="success", summary="disabled in config",
                             metrics=metrics, next_state=dict(self.state or {}))

        occasions = self._occasions()
        kinds = cfg.get("kinds")
        horizon = sc.recently_ended(today, lookback_days=int(cfg["metric_horizon_days"]),
                                    kinds=kinds, occasions=occasions)
        lookback = int(cfg["lookback_days"])
        actionable = [w for w in horizon if (today - w.end).days <= lookback]
        metrics["events_ended"] = len(actionable)
        if not horizon:
            return RunResult(status="success",
                             summary=f"No {','.join(kinds or ['any'])} event ended "
                                     f"in the last {cfg['metric_horizon_days']} days.",
                             metrics=metrics, next_state=dict(self.state or {}))
        self.decide("plan", "ended events: " + ", ".join(
            f"{w.occasion.id} ({w.date_range_label()})" for w in horizon)
            + f"; actionable (<= {lookback}d): "
            + (", ".join(w.occasion.id for w in actionable) or "none"))

        next_by_event: dict[str, Optional[sc.OccasionWindow]] = {}
        for w in actionable:
            next_by_event[w.occasion.id] = sc.next_occasion(
                w.occasion.audience, today, kind=w.occasion.kind or None,
                exclude_ids=(w.occasion.id,),
                horizon_days=int(cfg["next_occasion_horizon_days"]),
                occasions=occasions)

        ledger: dict[str, str] = {}
        try:
            ledger = dict(self.storage.read_json(self._ledger_key()) or {})
        except Exception:
            ledger = {}

        sites = resolve_sites(cfg)
        recs: list[dict] = []
        rec_ids_by_site: dict[str, list[str]] = {}
        keys_by_site: dict[str, list[str]] = {}
        cap = int(cfg["max_recs_per_site"])
        for site, scfg in sites.items():
            dsn = os.environ.get(str(scfg.get("dsn_env") or ""), "")
            if not dsn:
                self.decide("skip", f"{site}: env {scfg.get('dsn_env')} not set")
                continue
            tag_to_event = {HOLIDAY_TAG_PREFIX + w.occasion.id: w for w in horizon}
            try:
                rows = self.fetch_articles(dsn, sorted(tag_to_event), scfg)
            except Exception as e:  # one site down must not block the other
                metrics["site_errors"] += 1
                self.decide("warning", f"{site}: article select failed: "
                                       f"{type(e).__name__}: {e}")
                continue
            for art in rows:
                tags = set(art.get("tags") or [])
                for tag, w in tag_to_event.items():
                    if tag not in tags:
                        continue
                    metrics["ended_event_articles_live"] += 1
                    if w not in actionable:
                        continue
                    metrics["articles_tagged"] += 1
                    lk = ledger_key(site, w, art["slug"])
                    if lk in ledger:
                        metrics["already_queued"] += 1
                        continue
                    if len(rec_ids_by_site.get(site, [])) >= cap:
                        metrics["unqueued_ended_event_articles"] += 1
                        continue
                    tmpl = str(scfg.get("article_url_template") or "")
                    url = tmpl.format(slug=art["slug"]) if tmpl else ""
                    rid = f"evt-{len(recs) + 1:03d}"
                    recs.append(build_refresh_rec(
                        rid, site=site, article=art, event=w,
                        next_event=next_by_event.get(w.occasion.id),
                        article_url=url))
                    rec_ids_by_site.setdefault(site, []).append(rid)
                    keys_by_site.setdefault(site, []).append(lk)

        if not recs:
            summary = (f"{len(actionable)} ended event(s); nothing new to queue "
                       f"({metrics['already_queued']} already queued, "
                       f"{metrics['ended_event_articles_live']} tagged page(s) live).")
            return self._finish(summary, metrics)

        if dry_run:
            for r in recs:
                self.decide("observation", f"[dry-run] would queue {r['site']} "
                                           f"{r['article_slug']} ({r['event']['id']})")
            metrics["unqueued_ended_event_articles"] += len(recs)
            return self._finish(f"[dry-run] would queue {len(recs)} refresh rec(s)",
                                metrics)

        self.storage.write_json(
            f"agents/{self.agent_id}/runs/{self.run_ts}/recommendations.json", {
                "schema_version": "1",
                "agent_id": self.agent_id,
                "run_ts": self.run_ts,
                "mode": REC_TYPE,
                "sites": sorted(rec_ids_by_site),
                "summary": f"{len(recs)} post-event article refresh(es).",
                "recommendations": recs,
                "run_metadata": {"today": today.isoformat(),
                                 "events": [w.to_dict() for w in actionable]},
            })
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        for site, ids in rec_ids_by_site.items():
            try:
                request_id = self.queue_recs(site=site, rec_ids=ids,
                                             subject_tag="refresh",
                                             source="event-refresh-agent")
            except Exception as e:
                metrics["site_errors"] += 1
                metrics["unqueued_ended_event_articles"] += len(ids)
                self.decide("warning", f"{site}: queue_recs failed: {e}")
                continue
            metrics["recs_queued"] += len(ids)
            for k in keys_by_site[site]:
                ledger[k] = now_iso
            self.decide("result", f"{site}: queued {len(ids)} refresh rec(s) "
                                  f"request_id={request_id}")
            # Persist per site so a crash after one site can't re-queue it.
            self._write_ledger(ledger, today, int(cfg["ledger_keep_days"]))

        return self._finish(
            f"Queued {metrics['recs_queued']} post-event refresh rec(s) for "
            + ", ".join(w.occasion.id for w in actionable), metrics)

    def _write_ledger(self, ledger: dict, today: _dt.date, keep_days: int) -> None:
        cutoff = (today - _dt.timedelta(days=keep_days)).isoformat()
        kept = {k: v for k, v in ledger.items() if str(v)[:10] >= cutoff}
        self.storage.write_json(self._ledger_key(), kept)

    def _finish(self, summary: str, metrics: dict) -> RunResult:
        next_state = dict(self.state or {})
        if metrics.get("site_errors"):
            # Don't let the short-circuit hash mark a partial run as done.
            next_state.pop(self.SIGNALS_STATE_KEY, None)
        return RunResult(status="success", summary=summary, metrics=metrics,
                         next_state=next_state)


if __name__ == "__main__":
    EventRefreshAgent().run_once()
