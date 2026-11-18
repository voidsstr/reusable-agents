#!/usr/bin/env python3
"""Amazon Price Refresh: the price and display-cache refresh engine for cache-model sites.

A thin AgentBase wrapper over two framework primitives:

  framework.core.price_refresh_priority   WHICH ASINs to re-check, in what order
                                          (demand tiers by gate, then the long
                                          tail, oldest first)
  framework.core.amazon_price_refresh     refresh_prices(): getItems, 10 ASINs a
                                          call, inside the budget; classifies
                                          every answer

plus the one thing a cache-model site adds: price_refresh_cache.py writes each
answer into the site's Amazon cache table and purges Amazon data before it can
be stored for 24h. Every value (catalog view, groups and gates, Creators
consumer, partner tag, resources, table and columns) comes from the site's
site.yaml block (price_refresh_settings.py); nothing here names a site.

ONE RUN
-------
 1. Load and validate the site's block ($PRICE_REFRESH_CONFIG).
 2. Size the run: min(max_calls_per_run, the consumer's remaining daily
    Creators share) calls x 10 ASINs.
 3. Rank due ASINs with price_refresh_priority over the site's catalog view.
    Its demand-ranking off switch (storage by_agent_id.<id>.enabled=false)
    falls back to plain oldest-first over the same view; it does not stop the
    refresh or the purge. The ranking's read transaction is then closed, so
    the session never sits idle in a transaction across a Creators call.
 4. Sweep with refresh_prices(). Each answered batch is upserted at once (one
    statement, one commit, deadlocks retried; an answer whose values cannot
    be stored is skipped and counted instead of stopping the sweep), so a run
    killed mid-way keeps its work. not_found answers, which NULL a whole row,
    are held to the end.
 5. If the sweep succeeded (Creators answered, nothing raised, no abort, no
    throttle) write the held not_found rows, unless they look like an
    integration fault (the not_found guard), then purge every row older than
    the purge horizon (24h minus the cron period and a run, so nothing is
    stored for 24h). The purge spares the rows this run asked about and could
    not refresh (a failed batch, an ASIN the answer left out, an answer that
    could not be stored) while a visitor can still see them (the quotable
    window): a failed batch is a failed run for its rows, and the next run
    re-asks them. Otherwise write nothing more: the previous values stay,
    nothing is blanked, and the purge waits for the next successful run.
 6. Read-only metrics: fresh-priced share of indexable pages (inside the
    quotable window), per-tier freshness, the backlog still due, due rows
    the purge NULLed while still displayable (no run answered them before
    the horizon), rows still holding Amazon data past the purge horizon and
    past 24h, today's Creators usage against the share.
 7. Status: failure when ASINs were due and Creators answered no call (every
    batch rejected, or a 429 from the first call), when the sweep or a write
    failed, when the not_found guard tripped, or when the purge was skipped
    while rows already hold Amazon data past 24h.

CLI
---
  python3 agent.py                       AgentBase run (cron / Run now)
  python3 agent.py --config              resolved settings + demand config (no DB, no API)
  python3 agent.py --print-sql           the cache upsert / purge / metric SQL (no DB, no API)
  python3 agent.py --plan [--limit N]    read-only DB: budget, tiers, first N candidates (no API)
  python3 agent.py --dry-run [--limit N] [--asins A,B]
                                         LIVE Creators getItems, NO DB writes, no run record;
                                         exits 1 when any getItems call failed
Options: --site-yaml PATH[#key] (default $PRICE_REFRESH_CONFIG), --agent-id ID.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

_HERE = Path(__file__).resolve().parent
_FW = _HERE.parents[1]  # this engine ships inside the framework repo
for _p in (str(_HERE), str(_FW)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from framework.core import price_refresh_priority as prp  # noqa: E402
from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.amazon_creators import (  # noqa: E402
    MAX_ITEMS_PER_CALL, CreatorsRequestError, CreatorsUnavailable,
)
from framework.core.amazon_price_refresh import PriceOutcome, refresh_prices  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

import price_refresh_cache as cache_mod  # noqa: E402
import price_refresh_settings as settings_mod  # noqa: E402

ENGINE_ID = "amazon-price-refresh-agent"

Say = Callable[..., None]
Decide = Callable[..., None]


# ---------------------------------------------------------------------------
# Default collaborators (tests inject fakes; nothing here runs on import)
# ---------------------------------------------------------------------------

def default_client_factory(settings: settings_mod.Settings, storage, partner_tag: str):
    """A Creators client charged to the site's consumer, or None when the
    credentials (AMAZON_CREATORS_CLIENT_ID / _SECRET) are not in the env."""
    from framework.core.amazon_creators import AmazonCreatorsClient, CreatorsConfig
    cfg = CreatorsConfig.from_env(partner_tag=partner_tag, marketplace=settings.marketplace)
    if cfg is None:
        return None
    return AmazonCreatorsClient(cfg, min_interval_s=settings.min_interval_s,
                                consumer=settings.consumer, storage=storage)


def default_connect(settings: settings_mod.Settings, *, readonly: bool = False,
                    environ: Optional[Mapping[str, str]] = None):
    """A keepalive Postgres connection to the site's DB. The DSN is read from
    the env var the settings name and is never logged."""
    env = os.environ if environ is None else environ
    dsn = env.get(settings.dsn_env, "")
    if not dsn:
        raise RuntimeError(f"${settings.dsn_env} is not set (the site's Postgres DSN)")
    from framework.core import pg_conn
    kw: dict[str, Any] = {"application_name": (settings.application_name or settings.agent_id)[:63],
                          "connect_timeout": settings.connect_timeout_s}
    if settings.statement_timeout_ms:
        # A session default, so a rolled-back write cannot undo it.
        kw["options"] = f"-c statement_timeout={int(settings.statement_timeout_ms)}"
    conn = pg_conn.connect(dsn, **kw)
    if readonly:
        conn.set_session(readonly=True)
    return conn


def default_lock_factory(agent_id: str):
    """One run per instance at a time (a manual run must not overlap the cron)."""
    from framework.core.locks import FileLock
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent_id)
    return FileLock(f"amazon-price-refresh-{safe}", timeout_s=60)


# ---------------------------------------------------------------------------
# Raw capture
# ---------------------------------------------------------------------------

def _loggable(text: Any, limit: int = 300) -> str:
    """Text safe to log and store: no NUL, no lone surrogate, bounded."""
    return cache_mod._UNSTORABLE_RE.sub("", str(text))[:limit]


class RawCapture:
    """Delegating Creators client that keeps each answered batch's raw items.

    refresh_prices() hands its on_batch callback only the classified
    PriceOutcomes; the cache also needs the title and image of the same
    answer. It also remembers the ASINs and the error of every failed batch,
    which refresh_prices() counts but does not hand to on_batch: the purge
    must spare those rows (a failed batch is a failed run for them), and a
    run Creators never answered must say why. Everything else (budget,
    throttled_remainder, track_usage) passes through to the real client."""

    def __init__(self, client):
        self._client = client
        self.raw: dict[str, dict] = {}
        self.answered_calls = 0
        self.failed_calls = 0
        self.failed_asins: list[str] = []
        self.last_error = ""

    def get_items(self, asins, *, resources=None):
        try:
            items, errors = self._client.get_items(asins, resources=resources)
        except CreatorsRequestError as exc:
            self.failed_calls += 1
            self.failed_asins.extend(str(a) for a in asins)
            self.last_error = _loggable(f"{type(exc).__name__}: {exc}")
            raise
        if not getattr(self._client, "throttled_remainder", None):
            self.answered_calls += 1
        for asin, item in (items or {}).items():
            self.raw[asin] = item
        return items, errors

    def __getattr__(self, name):
        if name.startswith("__") or name == "_client":
            raise AttributeError(name)  # never recurse before __init__ has run
        return getattr(self._client, name)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@dataclass
class RefreshReport:
    status: str = "success"
    summary: str = ""
    summary_md: str = ""
    error_text: str = ""
    metrics: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)


@dataclass
class _Sweep:
    """What the sweep and the steps after it found (for status and summaries)."""
    n_cands: int = 0
    by_tier: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    sweep_error: Optional[tuple[str, BaseException]] = None
    no_answer: bool = False
    creators_why: str = ""
    guard_tripped: bool = False
    purge_reason: str = ""
    #: ASINs this run asked Amazon about and could not refresh (failed
    #: batch, silently omitted, answer not storable): the purge spares them.
    unrefreshed: list = field(default_factory=list)
    #: The quotable window (hours): the spare window, and what "still
    #: displayable" means for the blanking counts.
    quotable_h: float = 0.0
    purge: Optional[cache_mod.PurgeResult] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hours_since(iso: Any) -> Optional[float]:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 3600.0)


def _metric_key(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(name).lower()).strip("_")


def priority_config(settings: settings_mod.Settings, *, storage=None) -> dict:
    """The resolved price_refresh_priority config for this instance (storage
    defaults / by_site / by_agent_id layered over the site's block)."""
    return prp.resolve_config(agent_id=settings.agent_id, site=settings.site,
                              site_cfg=dict(settings.demand_priority), storage=storage)


def fallback_config(cfg: Mapping[str, Any]) -> dict:
    """Plain oldest-first over the same catalog: no demand groups, the long
    tail on (the demand-ranking off switch's path; it reads no click or AI
    signal, and still refreshes and purges)."""
    out = dict(cfg)
    out["groups"] = []
    out["long_tail"] = {**dict(cfg.get("long_tail") or {}), "enabled": True}
    return out


def creators_usage_metrics(consumer: str, storage) -> dict:
    """Today's ledger for ``consumer`` against its share (read-only)."""
    from framework.core import amazon_creators as ac
    cfg = ac.load_budget_config(storage)
    usage = ac.usage_today(storage)
    name = ac.normalize_consumer(consumer)
    rec = usage.get("consumers", {}).get(name) or {}
    total = max(0, int(cfg.get("total_calls_per_day") or 0))
    share = float((cfg.get("shares") or {}).get(name, cfg.get("default_share", 0)) or 0)
    share_calls = int(total * min(max(share, 0.0), 1.0))
    calls = int(rec.get("calls") or 0)
    out = {"creators_calls_today": calls,
           "creators_throttled_today": int(rec.get("throttled") or 0),
           "creators_share_calls": share_calls,
           "creators_calls_all_consumers_today": int(usage.get("total") or 0)}
    if share_calls > 0:
        out["creators_calls_vs_share"] = round(calls / share_calls, 3)
    return out


def run_refresh(settings: settings_mod.Settings, *, storage,
                client_factory: Optional[Callable] = None,
                connect: Optional[Callable] = None,
                say: Optional[Say] = None, decide: Optional[Decide] = None,
                dry_run: bool = False, explicit_asins: Optional[Sequence[str]] = None,
                limit_asins: Optional[int] = None, prior_state: Optional[Mapping] = None,
                environ: Optional[Mapping[str, str]] = None) -> RefreshReport:
    """One refresh pass (see the module docstring). Never raises for an
    expected failure: the report's status says what happened.

    ``client_factory`` and ``connect`` default to the module's
    ``default_client_factory`` / ``default_connect``, looked up at call time."""
    client_factory = client_factory or default_client_factory
    connect = connect or default_connect
    say = say or (lambda msg, progress=0.0: print(f"[price-refresh] {msg}", flush=True))
    decide = decide or (lambda category, message, evidence=None: None)
    if explicit_asins and not dry_run:
        raise ValueError("explicit ASINs are a dry-run (operator) feature")
    started = time.time()
    prior_state = dict(prior_state or {})
    rep = RefreshReport()
    m = rep.metrics
    m.update({"dry_run": 1 if dry_run else 0, "max_calls_per_run": settings.max_calls_per_run,
              "api_calls": 0, "refreshed": 0, "written_rows": 0, "purged_rows": 0,
              "purge_skipped": 0, "not_found_held": 0, "blanked_on_failed_run": 0,
              "poison_rows": 0, "purge_horizon_hours": settings.cache.max_age_hours})

    def finish(status: str, summary: str, *, error_text: str = "") -> RefreshReport:
        rep.status = status
        rep.summary = summary[:1000]
        rep.error_text = error_text[:4000]
        m["elapsed_s"] = round(time.time() - started, 1)
        return rep

    # -- 1. Creators client --------------------------------------------------
    tag = settings.partner_tag_value(environ)
    if not tag:
        return finish("failure", f"Partner tag not configured (${settings.partner_tag_env} is empty) "
                                 "— nothing refreshed, nothing purged.",
                      error_text="creators.partner_tag_env names an unset env var")
    client = client_factory(settings, storage, tag)
    if client is None:
        return finish("failure", "Amazon Creators API not configured (AMAZON_CREATORS_CLIENT_ID / "
                                 "_SECRET) — nothing refreshed, nothing purged.",
                      error_text="client_from_env returned None")

    # -- 2. Size the run -----------------------------------------------------
    budget_left: Optional[int] = None
    if getattr(client, "track_usage", False):
        try:
            budget_left = int(client.remaining_budget())
        except Exception as exc:  # noqa: BLE001 - the budget is advisory
            decide("warning", f"Creators budget unreadable, using max_calls_per_run: {exc}"[:300])
    max_calls = settings.max_calls_per_run
    if budget_left is not None:
        max_calls = min(max_calls, max(0, budget_left))
        m["budget_left_at_start"] = budget_left
    if limit_asins:
        max_calls = min(max_calls, -(-int(limit_asins) // MAX_ITEMS_PER_CALL))
    limit = int(limit_asins) if limit_asins else max_calls * MAX_ITEMS_PER_CALL
    m["budget_calls"] = max_calls

    # -- 3. Candidates -------------------------------------------------------
    conn = None
    if not (dry_run and explicit_asins):
        try:
            # A dry run only reads (it ranks candidates): read-only session.
            conn = connect(settings, environ=environ, readonly=dry_run)
        except Exception as exc:  # noqa: BLE001
            return finish("failure", f"Cannot reach the site DB (${settings.dsn_env}): "
                                     f"{type(exc).__name__} — nothing refreshed, nothing purged.",
                          error_text=f"{type(exc).__name__}: {exc}")
    try:
        return _run_with_conn(settings, conn=conn, client=client, storage=storage, rep=rep,
                              say=say, decide=decide, dry_run=dry_run,
                              explicit_asins=explicit_asins, max_calls=max_calls, limit=limit,
                              prior_state=prior_state, finish=finish, started=started)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _run_with_conn(settings, *, conn, client, storage, rep, say, decide, dry_run,
                   explicit_asins, max_calls, limit, prior_state, finish,
                   started) -> RefreshReport:
    m = rep.metrics
    spec = settings.cache
    pcfg: dict = {}
    tiers = prp.Tiers()
    rank_cfg: dict = {}
    if conn is not None:
        pcfg = priority_config(settings, storage=storage)
        for p in pcfg.get("config_problems") or []:
            decide("warning", f"demand config: {p}"[:300])
        demand_on = bool(pcfg.get("enabled"))
        m["demand_ranking"] = 1 if demand_on else 0
        if demand_on:
            tiers = prp.collect_tiers(conn, pcfg, storage=storage)
            rank_cfg = pcfg
        else:
            decide("observation", "demand ranking is switched off for this instance "
                                  "(enabled=false); ranking oldest-first over the catalog")
            rank_cfg = fallback_config(pcfg)
        m["demand_members"] = len(tiers.members)
        m["demand_signal_errors"] = len(tiers.errors)
        for name, err in tiers.errors.items():
            decide("warning", f"demand group {name} empty this run: {err}"[:300])
        gate_issues = settings.gate_problems(rank_cfg)
        m["gate_buffer_violations"] = len(gate_issues)
        for issue in gate_issues:
            decide("warning", f"gate buffer: {issue}")

    cands: list = []
    if explicit_asins:
        asins = list(dict.fromkeys(a.strip() for a in explicit_asins if a and a.strip()))[:limit]
    elif max_calls > 0:
        try:
            cands = prp.rank_refresh_candidates(conn, rank_cfg, limit=limit, tiers=tiers,
                                                storage=storage)
        except Exception as exc:  # noqa: BLE001
            return finish("failure", f"Catalog ranking query failed ({type(exc).__name__}) — no "
                                     "Creators call made, nothing written, nothing purged.",
                          error_text=f"{type(exc).__name__}: {exc}")
        asins = [c.asin for c in cands]
    else:
        asins = []
    sw = _Sweep(n_cands=len(asins), by_tier=prp.tier_counts(cands),
                quotable_h=settings.quotable_hours((rank_cfg or pcfg).get("display_hours")))
    m["candidates"] = len(asins)
    for tier, n in sw.by_tier.items():
        m[f"candidates_{_metric_key(tier)}"] = n
    say(f"{len(asins)} ASIN(s) to refresh ({', '.join(f'{t} {n}' for t, n in sw.by_tier.items()) or 'none due'})"
        f" in at most {max_calls} call(s){' (DRY RUN: no DB writes)' if dry_run else ''}", 0.1)
    decide("plan", f"refresh {len(asins)} ASIN(s) within {max_calls} Creators call(s)",
           evidence={"by_tier": sw.by_tier, "consumer": settings.consumer,
                     "budget_left": m.get("budget_left_at_start")})

    # The ranking's savepoints leave its read transaction open. Close it now,
    # so the session is not idle in a transaction across the first Creators
    # call (the LWA token alone can take minutes) and a connection that died
    # meanwhile is found before any call is spent.
    if conn is not None:
        try:
            conn.rollback()
        except Exception as exc:  # noqa: BLE001
            return finish("failure", f"The site DB connection failed before the sweep "
                                     f"({type(exc).__name__}) — no Creators call made, nothing "
                                     "written, nothing purged.",
                          error_text=f"{type(exc).__name__}: {exc}")

    # -- 4. Sweep ------------------------------------------------------------
    capture = RawCapture(client)
    writer = None if (dry_run or conn is None) else cache_mod.CacheWriter(
        conn, spec, consumer=settings.consumer, retries=settings.write_retries,
        backoff_s=settings.write_retry_backoff_s)
    held_not_found: list[PriceOutcome] = []
    # Outcomes of every batch fully handled (written, or printed in a dry
    # run). Counted here rather than from refresh_prices()' return value,
    # which is lost when the sweep raises part-way.
    processed: list[PriceOutcome] = []
    write_errors: list[BaseException] = []

    def on_batch(batch: list[PriceOutcome]) -> None:
        raws = {o.asin: capture.raw.pop(o.asin, None) for o in batch}
        rows = [(o.asin, o.status, cache_mod.row_values(o, raws.get(o.asin),
                                                        max_text_chars=spec.max_text_chars))
                for o in batch if o.status in cache_mod.CARRIES_ITEM]
        if dry_run:
            for o in batch:
                vals = next((r[2] for r in rows if r[0] == o.asin), {})
                extra = (f"${o.price:.2f} list={o.original_price}" if o.status == "priced"
                         else (o.error or o.availability or ""))
                print(f"  DRY {o.asin} {o.status:<11} {_loggable(extra, 200)} "
                      f"title={'yes' if vals.get('title') else 'no'} "
                      f"image={'yes' if vals.get('image_url') else 'no'} "
                      f"[{o.availability_type or '-'} / {o.condition or '-'}]", flush=True)
        elif rows and writer is not None:
            try:
                writer.upsert(rows)
            except Exception as exc:
                write_errors.append(exc)
                raise
        held_not_found.extend(o for o in batch if o.status == "not_found")
        processed.extend(batch)
        if len(processed) % (MAX_ITEMS_PER_CALL * 20) == 0:
            say(f"refreshed {len(processed)}/{len(asins)}",
                0.1 + 0.7 * len(processed) / max(1, len(asins)))

    stats: dict = {"api_calls": 0, "throttled": 0, "errors": 0, "untried": [], "aborted": ""}
    try:
        if asins and max_calls > 0:
            _, stats = refresh_prices(
                capture, asins, max_calls=max_calls,
                expected_currency=settings.expected_currency, resources=settings.resources,
                on_batch=on_batch, max_consecutive_errors=settings.max_consecutive_errors)
    except CreatorsUnavailable as exc:
        sw.sweep_error = ("creators", exc)
    except Exception as exc:  # noqa: BLE001 - a write failure or an unexpected answer
        sw.sweep_error = ("write" if write_errors else "sweep", exc)
    finally:
        capture.raw.clear()
    sw.stats = stats

    aborted = bool(stats.get("aborted"))
    throttled = bool(stats.get("throttled"))
    api_calls = int(stats.get("api_calls") or capture.answered_calls)
    batch_errors = int(stats.get("errors") or capture.failed_calls)
    # Creators was asked and answered nothing (every batch rejected, or a
    # persistent 429 from the first call). refresh_prices() returns normally
    # then, but this is not a successful run: nothing was refreshed, so the
    # purge must not run and the run must escalate.
    sw.no_answer = sw.sweep_error is None and api_calls == 0 and (batch_errors > 0 or throttled)
    sw.creators_why = (capture.last_error or _loggable(stats.get("aborted") or "")
                       or ("a persistent HTTP 429" if throttled else ""))
    sweep_ok = sw.sweep_error is None and not aborted and not throttled and not sw.no_answer
    answered = [o for o in processed if o.status in cache_mod.ANSWERED]
    m.update({
        "api_calls": api_calls,
        "refreshed": sum(1 for o in processed if o.status == "priced"),
        "unavailable": sum(1 for o in processed if o.marks_unavailable),
        "out_of_stock": sum(1 for o in processed if o.status == "unavailable"),
        "no_offer": sum(1 for o in processed if o.status == "no_offer"),
        "not_found": len(held_not_found),
        "non_usd": sum(1 for o in processed if o.status == "non_usd"),
        "map_hidden": sum(1 for o in processed if o.map_hidden),
        "missing": sum(1 for o in processed if o.status == "missing"),
        "batch_errors": batch_errors,
        "throttled": 1 if throttled else 0,
        "aborted": 1 if aborted else 0,
        # Not answered this run (budget, abort, throttle, failed batches, or the
        # rest of a sweep that raised): their rows keep their previous values.
        "untried": (len(stats.get("untried") or []) if sw.sweep_error is None
                    else max(0, len(asins) - len(processed))),
        "written_rows": writer.rows_written if writer else 0,
        "write_retries": writer.retried if writer else 0,
    })

    # -- 5. Held not_found rows, then the purge ------------------------------
    post_error: Optional[tuple[str, BaseException]] = None
    nf = len(held_not_found)
    sw.guard_tripped = bool(
        sweep_ok and nf and len(answered) >= settings.not_found_min_answered
        and nf / max(1, len(answered)) > settings.not_found_max_share)
    nf_written = 0
    if nf and not dry_run and writer is not None and sweep_ok and not sw.guard_tripped:
        try:
            for i in range(0, nf, MAX_ITEMS_PER_CALL):
                chunk = held_not_found[i:i + MAX_ITEMS_PER_CALL]
                nf_written += writer.upsert([(o.asin, o.status, cache_mod.row_values(o, None))
                                             for o in chunk])
        except Exception as exc:  # noqa: BLE001
            post_error = ("write", exc)
    m["not_found_written"] = nf_written
    m["not_found_held"] = nf - nf_written
    m["written_rows"] = writer.rows_written if writer else 0
    poison = list(writer.poison) if writer else []
    m["poison_rows"] = len(poison)
    if poison:
        shown = ", ".join(f"{a} ({k})" for a, k in poison[:20])
        decide("warning", f"{len(poison)} answer(s) could not be stored and were skipped; those "
                          f"rows keep their previous values: {shown}"
                          f"{' ...' if len(poison) > 20 else ''}"[:1000])
    # Asked and not refreshed: a failed run for these rows. The purge spares
    # them while they are still displayable (see price_refresh_cache's THE
    # SPARE); the next run re-asks them.
    sw.unrefreshed = list(dict.fromkeys(
        capture.failed_asins + [o.asin for o in processed if o.status == "missing"]
        + [a for a, _ in poison]))
    m["failed_batch_asins"] = len(set(capture.failed_asins))
    m["unrefreshed_asins"] = len(sw.unrefreshed)

    purged = 0
    purge_error: Optional[BaseException] = None
    if dry_run or writer is None:
        sw.purge_reason = "dry run" if dry_run else "no database"
    elif sw.sweep_error is not None:
        sw.purge_reason = f"the sweep failed ({sw.sweep_error[0]})"
    elif sw.no_answer:
        sw.purge_reason = "Creators answered no call"
    elif aborted:
        sw.purge_reason = "the sweep stopped early"
    elif throttled:
        sw.purge_reason = "Creators throttled the sweep"
    elif sw.guard_tripped:
        sw.purge_reason = "the not_found guard tripped"
    elif post_error is not None:
        sw.purge_reason = "a not_found write failed"
    if sw.purge_reason:
        m["purge_skipped"] = 1
    else:
        offset = time.time() - started
        m["purge_offset_s"] = round(offset, 1)
        if offset > settings.max_run_minutes * 60.0:
            decide("warning", f"the purge ran {offset / 60.0:.0f} min into the run, past "
                              f"schedule.max_run_minutes ({settings.max_run_minutes:g}); the "
                              f"{spec.max_age_hours:g}h purge horizon assumes shorter runs: "
                              "raise max_run_minutes (it lowers the horizon) or shorten the run")
        try:
            sw.purge = writer.purge_report(spare=sw.unrefreshed,
                                           spare_max_age_hours=sw.quotable_h)
            purged = sw.purge.count
            rep.state["last_purge_at"] = _now_iso()
        except Exception as exc:  # noqa: BLE001
            purge_error = exc
            m["purge_skipped"] = 1
            sw.purge_reason = f"the purge failed ({type(exc).__name__})"
    m["purged_rows"] = purged
    unrefreshed = set(sw.unrefreshed)
    purged_unrefreshed = ([(k, age) for k, age in sw.purge.purged if k in unrefreshed]
                          if sw.purge else [])
    # Rows this run could not refresh that its purge NULLed anyway: only rows
    # already past the quotable window (no visitor sees them any more).
    m["purged_unrefreshed_rows"] = len(purged_unrefreshed)
    m["spared_rows"] = len(sw.purge.spared) if sw.purge else 0
    # The invariant the goal tracks: no Amazon value a visitor can still see
    # is NULLed by a run that failed, or by the failed part of a run (rows it
    # asked about and could not refresh). Counted from what actually ran (the
    # purge's RETURNING ages), so a regression in the gating or the spare
    # shows up here instead of being 0 by definition.
    blanked_unrefreshed = [k for k, age in purged_unrefreshed
                           if age is not None and age <= sw.quotable_h]
    m["blanked_on_failed_run"] = ((purged + nf_written) if not sweep_ok else 0) \
        + len(blanked_unrefreshed)
    if blanked_unrefreshed:
        decide("warning", f"the purge NULLed {len(blanked_unrefreshed)} row(s) this run could not "
                          "refresh while they were still displayable (an engine regression): "
                          f"{', '.join(blanked_unrefreshed[:20])}"[:1000])
    if sweep_ok and not dry_run:
        rep.state["last_successful_sweep_at"] = _now_iso()

    # -- 6. Metrics (read-only; a failure here never fails the run) ----------
    if conn is not None and not dry_run:
        _collect_metrics(settings, conn=conn, pcfg=rank_cfg or pcfg, tiers=tiers, rep=rep,
                         storage=storage, decide=decide, sw=sw)
    last_purge = rep.state.get("last_purge_at") or prior_state.get("last_purge_at")
    hrs = _hours_since(last_purge)
    if hrs is not None:
        m["hours_since_last_purge"] = round(hrs, 2)

    # -- 7. Status + summary -------------------------------------------------
    status = "success"
    err = ""
    if sw.sweep_error is not None:
        status = "failure"
        err = f"{type(sw.sweep_error[1]).__name__}: {sw.sweep_error[1]}"
    elif sw.no_answer:
        status = "failure"
        err = (f"Creators answered none of the getItems calls for {len(asins)} due ASIN(s) "
               f"({batch_errors} failed{', then a persistent 429' if throttled else ''}): "
               f"{sw.creators_why}")
    elif sw.guard_tripped or post_error is not None or purge_error is not None:
        status = "failure"
        exc = (post_error[1] if post_error else purge_error)
        err = f"{type(exc).__name__}: {exc}" if exc else (
            f"{nf} of {len(answered)} answers were not_found (guard max_share "
            f"{settings.not_found_max_share:g})")
    elif dry_run and batch_errors:
        # The dry run exists to prove the credential, the tag and the resource
        # set: any rejected call means the live run would lose batches too.
        status = "failure"
        err = f"{batch_errors} of {batch_errors + api_calls} getItems call(s) failed: {sw.creators_why}"
    elif sw.purge_reason and int(m.get("api_rows_past_24h") or 0) > 0:
        # A partial run may skip the purge once; rows already past 24h mean
        # purges have been skipped for longer than the horizon allows.
        status = "failure"
        err = (f"{m['api_rows_past_24h']} cache row(s) hold Amazon data older than 24h and this "
               f"run skipped the purge ({sw.purge_reason}); last purge: "
               f"{last_purge or 'none recorded'}")
    rep.summary_md = _summary_md(settings, m, sw)
    summary = _summary(settings, m, sw, dry_run)
    decide("result", summary[:500])
    return finish(status, summary, error_text=err)


def _collect_metrics(settings, *, conn, pcfg, tiers, rep, storage, decide,
                     sw: Optional[_Sweep] = None) -> None:
    m = rep.metrics
    spec = settings.cache
    sw = sw or _Sweep()
    quotable_h = settings.quotable_hours(pcfg.get("display_hours"))
    m["quotable_hours"] = quotable_h
    catalog = pcfg.get("catalog") or {}
    try:
        sql, params = cache_mod.build_freshness_sql(
            spec, catalog, display_hours=quotable_h,
            min_price=settings.min_quotable_price, indexable_sql=settings.indexable_sql)
        f = cache_mod.read_one(conn, sql, params, ("indexable", "fresh_checked", "fresh_priced"))
        m["indexable_pages"] = f["indexable"]
        m["fresh_checked_pages"] = f["fresh_checked"]
        m["fresh_priced_pages"] = f["fresh_priced"]
        if f["indexable"]:
            m["fresh_checked_pct"] = round(100.0 * f["fresh_checked"] / f["indexable"], 1)
            m["fresh_priced_pct"] = round(100.0 * f["fresh_priced"] / f["indexable"], 1)
    except Exception as exc:  # noqa: BLE001
        decide("warning", f"freshness metric unavailable: {type(exc).__name__}: {exc}"[:300])
    try:
        # The rows this run's purge spared are deliberately kept: not "should
        # have been NULLed" (spared_rows counts them). Without a purge there
        # was no spare.
        spare = sw.unrefreshed if sw.purge is not None else ()
        sql, params = cache_mod.build_stale_counts_sql(spec, spare=spare,
                                                       spare_max_age_hours=sw.quotable_h)
        m.update(cache_mod.read_one(conn, sql, params, cache_mod.STALE_COUNT_NAMES))
    except Exception as exc:  # noqa: BLE001
        decide("warning", f"stale-row metric unavailable: {type(exc).__name__}: {exc}"[:300])
    # Due catalog rows this run's purge NULLed while a visitor could still see
    # them: rows no run answered between falling due and the purge horizon
    # (the budget ran out before them, or a gate leaves no run of slack).
    # Rows of products the catalog no longer lists are hygiene, not blanking.
    displayable = sw.purge.within(quotable_h) if sw.purge is not None else []
    if not displayable:
        m["blanked_due_rows"] = 0
    else:
        try:
            sql, params = cache_mod.build_catalog_count_sql(catalog, displayable)
            m["blanked_due_rows"] = cache_mod.read_one(conn, sql, params, ("catalog_rows",))[
                "catalog_rows"]
        except Exception as exc:  # noqa: BLE001
            decide("warning", f"blanked-row metric unavailable: {type(exc).__name__}: {exc}"[:300])
    if m.get("blanked_due_rows"):
        decide("warning", f"the purge NULLed {m['blanked_due_rows']} due catalog row(s) a visitor "
                          f"could still see: no run answered them before the "
                          f"{spec.max_age_hours:g}h purge horizon. Check gate_buffer_violations "
                          "(a gate with no run of slack) and budget_calls against the due backlog")
    if settings.tier_freshness and catalog.get("table"):
        try:
            rows = prp.tier_freshness(conn, {**pcfg, "display_hours": quotable_h}, tiers)
            fresh = members = due = 0
            for r in rows:
                if r.get("fresh_pct") is not None:
                    m[f"fresh_pct_{_metric_key(r['tier'])}"] = r["fresh_pct"]
                m[f"due_backlog_{_metric_key(r['tier'])}"] = int(r.get("due") or 0)
                due += int(r.get("due") or 0)
                if r["tier"] != prp.LONG_TAIL:
                    fresh += int(r.get("fresh") or 0)
                    members += int(r.get("in_catalog") or 0)
            # Rows still due after this run (never checked, or past their
            # gate): the queue the next run starts from, including due rows
            # past this run's ranking LIMIT (which "untried" cannot see).
            m["due_backlog"] = due
            if members:
                m["demand_fresh_pct"] = round(100.0 * fresh / members, 1)
        except Exception as exc:  # noqa: BLE001
            decide("warning", f"tier freshness unavailable: {type(exc).__name__}: {exc}"[:300])
        finally:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
    try:
        m.update(creators_usage_metrics(settings.consumer, storage))
    except Exception as exc:  # noqa: BLE001
        decide("warning", f"Creators usage ledger unreadable: {type(exc).__name__}: {exc}"[:300])


def _summary(settings, m, sw: _Sweep, dry_run: bool) -> str:
    head = "[DRY RUN] " if dry_run else ""
    err = sw.sweep_error
    if err is not None and err[0] == "creators" and not m.get("written_rows"):
        head += ("Creators unavailable before any answer — nothing written, every cached "
                 "value kept, purge skipped")
        return f"{head}: {type(err[1]).__name__}: {err[1]}"[:1000]
    if sw.no_answer:
        kept = "nothing written, every cached value kept" + ("" if dry_run else ", purge skipped")
        return (f"{head}Creators answered none of the getItems calls for {sw.n_cands} due "
                f"ASIN(s) ({m.get('batch_errors', 0)} failed"
                f"{', then a persistent 429' if m.get('throttled') else ''}) — {kept}: "
                f"{sw.creators_why}")[:1000]
    if m.get("budget_calls") == 0:
        # Nothing was ranked, so "of 0 due" would contradict the backlog below.
        parts = [f"{head}No Creators call: the daily budget of consumer {settings.consumer} is "
                 "spent, nothing ranked or refreshed"]
    else:
        parts = [f"{head}Refreshed {m.get('refreshed', 0)} of {sw.n_cands} due ASIN(s) via "
                 f"Creators in {m.get('api_calls', 0)} call(s) (consumer {settings.consumer})"]
    parts.append(f"unavailable {m.get('unavailable', 0)} (out_of_stock {m.get('out_of_stock', 0)}, "
                 f"no_offer {m.get('no_offer', 0)}, not_found {m.get('not_found', 0)}, "
                 f"non_usd {m.get('non_usd', 0)}); missing {m.get('missing', 0)}")
    if m.get("batch_errors"):
        parts.append(f"{m['batch_errors']} failed batch(es): no answer for "
                     f"{m.get('failed_batch_asins', 0)} ASIN(s), nothing written for them"
                     f"{f' (last: {sw.creators_why[:200]})' if sw.creators_why else ''}")
    if err is not None:
        parts.append(f"sweep FAILED ({err[0]}: {type(err[1]).__name__}); "
                     f"{m.get('written_rows', 0)} row(s) written before it, the rest keep their values")
    elif sw.stats.get("aborted"):
        parts.append(f"stopped early: {_loggable(sw.stats['aborted'], 160)}")
    elif m.get("throttled"):
        parts.append("hit a persistent Creators 429 and stopped early")
    if m.get("poison_rows"):
        parts.append(f"{m['poison_rows']} answer(s) could not be stored and were skipped")
    if sw.guard_tripped:
        parts.append(f"not_found guard tripped: {m.get('not_found_held', 0)} not_found answer(s) "
                     "held back as a likely integration fault")
    elif m.get("not_found_held") and not dry_run:
        parts.append(f"{m['not_found_held']} not_found answer(s) held back")
    if not dry_run:
        parts.append(f"purge skipped ({sw.purge_reason})" if sw.purge_reason else
                     _purge_phrase(settings, m, sw))
    if m.get("due_backlog"):
        parts.append(f"{m['due_backlog']} row(s) still due")
    if m.get("api_rows_past_24h"):
        parts.append(f"{m['api_rows_past_24h']} row(s) hold Amazon data past 24h")
    elif m.get("stale_api_rows"):
        parts.append(f"{m['stale_api_rows']} row(s) past the {settings.cache.max_age_hours:g}h "
                     "purge horizon await the next purge")
    if "fresh_priced_pct" in m:
        parts.append(f"fresh-priced {m['fresh_priced_pct']}% of {m.get('indexable_pages', 0)} "
                     "indexable page(s)")
    if "demand_fresh_pct" in m:
        parts.append(f"demand tiers {m['demand_fresh_pct']}% fresh")
    if "creators_calls_today" in m:
        parts.append(f"Creators today {m['creators_calls_today']}/{m.get('creators_share_calls', 0)} "
                     f"call(s), {m.get('creators_throttled_today', 0)} throttle(s)")
    return ("; ".join(parts) + ".")[:1000]


def _purge_phrase(settings, m, sw: _Sweep) -> str:
    """What a purge that ran did, in words that match the metrics: rows the
    run could not refresh are 'kept' only when the spare kept them."""
    out = (f"purge NULLed {m.get('purged_rows', 0)} row(s) older than "
           f"{settings.cache.max_age_hours:g}h")
    if m.get("blanked_due_rows"):
        out += (f" ({m['blanked_due_rows']} of them due catalog rows still displayable: no run "
                "answered them before the horizon)")
    if m.get("spared_rows"):
        out += (f"; spared {m['spared_rows']} row(s) this run could not refresh (inside the "
                f"{sw.quotable_h:g}h quotable window; the next run re-asks them)")
    if m.get("purged_unrefreshed_rows"):
        out += (f"; {m['purged_unrefreshed_rows']} row(s) it could not refresh were already "
                "past that window and were purged")
    return out


def _summary_md(settings, m, sw: _Sweep) -> str:
    lines = [f"## Price refresh: {settings.site or settings.agent_id}", "",
             f"- Consumer `{settings.consumer}`, budget {m.get('budget_calls', 0)} call(s) this run "
             f"(left at start: {m.get('budget_left_at_start', 'n/a')})",
             f"- Candidates by tier: " + (", ".join(f"{t} {n}" for t, n in sw.by_tier.items())
                                          or "none due"),
             f"- Outcomes: priced {m.get('refreshed', 0)}, out_of_stock {m.get('out_of_stock', 0)}, "
             f"no_offer {m.get('no_offer', 0)}, not_found {m.get('not_found', 0)}, "
             f"non_usd {m.get('non_usd', 0)}, missing {m.get('missing', 0)}, "
             f"failed batches {m.get('batch_errors', 0)}",
             f"- Rows written {m.get('written_rows', 0)} (not_found written "
             f"{m.get('not_found_written', 0)}, held {m.get('not_found_held', 0)}; could not be "
             f"stored {m.get('poison_rows', 0)})",
             f"- Purge (horizon {settings.cache.max_age_hours:g}h): "
             + (f"skipped ({sw.purge_reason})" if sw.purge_reason
                else f"{m.get('purged_rows', 0)} row(s) NULLed, "
                     f"{m.get('blanked_due_rows', 'n/a')} of them due catalog rows still "
                     f"displayable; {m.get('spared_rows', 0)} unrefreshed row(s) spared, "
                     f"{m.get('purged_unrefreshed_rows', 0)} past the quotable window purged"),
             f"- Asked but not refreshed: {m.get('unrefreshed_asins', 0)} ASIN(s) "
             f"({m.get('failed_batch_asins', 0)} in failed batches, {m.get('missing', 0)} omitted, "
             f"{m.get('poison_rows', 0)} not storable)",
             ]
    if sw.no_answer:
        lines.append(f"- Creators answered no call: `{sw.creators_why}`; nothing written, "
                     "every cached value kept")
    elif m.get("batch_errors") and sw.creators_why:
        lines.append(f"- Last failed batch: `{sw.creators_why}`")
    if sw.sweep_error is not None:
        lines.append(f"- Sweep failed ({sw.sweep_error[0]}): `{type(sw.sweep_error[1]).__name__}`; "
                     "rows not yet answered keep their previous values")
    if sw.stats.get("aborted"):
        lines.append(f"- Stopped early: {_loggable(sw.stats['aborted'], 200)}")
    if sw.guard_tripped:
        lines.append("- not_found guard tripped: not_found answers held back (integration fault?)")
    for k in ("fresh_priced_pct", "fresh_checked_pct", "demand_fresh_pct", "quotable_hours",
              "due_backlog", "blanked_due_rows", "blanked_on_failed_run",
              "stale_api_rows", "stale_price_rows", "api_rows_past_24h", "creators_calls_today",
              "creators_calls_vs_share", "creators_throttled_today", "gate_buffer_violations"):
        if k in m:
            lines.append(f"- {k}: {m[k]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# AgentBase wrapper
# ---------------------------------------------------------------------------

class AmazonPriceRefreshAgent(AgentBase):
    agent_id = ENGINE_ID
    name = "Amazon Price Refresh"
    description = ("Demand-ranked Amazon price and display-cache refresh for a cache-model "
                   "site; never blanks data on a failed run, or on the rows a partial run could "
                   "not refresh; purges Amazon data before it can be stored for 24h.")
    category = "seo"
    capabilities = [
        declare("read_catalog", "Read the site's refresh catalog view and demand signals",
                risk_level="low"),
        declare("creators_get_items", "Amazon Creators getItems inside the consumer's daily share",
                risk_level="medium", affects=["amazon-creators-budget"]),
        declare("upsert_item_cache", "Write fresh Amazon answers into the site's cache table",
                risk_level="medium", affects=["site-database"]),
        declare("purge_item_cache", "NULL Amazon-sourced columns past the purge horizon (under "
                                    "24h) after a successful run", risk_level="medium",
                affects=["site-database"]),
    ]

    def __init__(self, *, settings: Optional[settings_mod.Settings] = None,
                 config_spec: Optional[str] = None, client_factory: Optional[Callable] = None,
                 connect: Optional[Callable] = None, lock_factory: Optional[Callable] = None,
                 environ: Optional[Mapping[str, str]] = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._settings = settings
        self._config_spec = config_spec
        self._client_factory = client_factory or default_client_factory
        self._connect = connect or default_connect
        self._lock_factory = lock_factory or default_lock_factory
        self._environ = environ

    def signals(self) -> dict | None:
        # Prices drift on Amazon's side and the purge must run every
        # successful tick: elapsed time IS the input, so never short-circuit.
        return None

    def load_settings(self) -> settings_mod.Settings:
        if self._settings is not None:
            return self._settings
        return settings_mod.load_settings(self._config_spec, agent_id=self.agent_id,
                                          storage=self.storage, environ=self._environ)

    def run(self) -> RunResult:
        self.status("loading config", progress=0.02)
        try:
            settings = self.load_settings()
        except settings_mod.SettingsError as exc:
            return RunResult(status="failure",
                             summary=f"price-refresh config invalid — nothing done: {exc}"[:500],
                             error_text=str(exc)[:4000],
                             metrics={"config_errors": len(exc.problems)},
                             next_state=dict(self.state))
        for w in settings.warnings:
            self.decide("warning", w)
        lock = self._lock_factory(self.agent_id)
        if not lock.try_acquire():
            return RunResult(status="success",
                             summary=(f"Skipped: another {self.agent_id} run is still in "
                                      "progress (nothing to do this tick)."),
                             metrics={"skipped_locked": 1}, next_state=dict(self.state))
        try:
            rep = run_refresh(
                settings, storage=self.storage, client_factory=self._client_factory,
                connect=self._connect, prior_state=self.state, environ=self._environ,
                say=lambda msg, progress=0.0: self.status(msg, progress=progress),
                decide=lambda category, message, evidence=None: self.decide(
                    category, message, evidence=evidence))
        finally:
            lock.release()
        return RunResult(status=rep.status, summary=rep.summary, summary_md=rep.summary_md,
                         metrics=rep.metrics, error_text=rep.error_text,
                         next_state={**self.state, **rep.state})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _cli_settings(args) -> settings_mod.Settings:
    agent_id = args.agent_id or os.environ.get("AGENT_ID") or ENGINE_ID
    return settings_mod.load_settings(args.site_yaml or None, agent_id=agent_id, storage=None)


def _sql_preview(settings: settings_mod.Settings) -> dict:
    spec = settings.cache
    quotable = settings.quotable_hours(settings.demand_priority.get("display_hours"))
    example = ["B000000000"]
    purge, _ = cache_mod.build_purge_sql(spec)
    spared_purge, _ = cache_mod.build_purge_sql(spec, spare=example, spare_max_age_hours=quotable)
    spared, _ = cache_mod.build_spared_sql(spec, example, quotable)
    stale, _ = cache_mod.build_stale_counts_sql(spec)
    catalog = settings.demand_priority.get("catalog") or {}
    fresh, _ = cache_mod.build_freshness_sql(
        spec, catalog, display_hours=quotable,
        min_price=settings.min_quotable_price, indexable_sql=settings.indexable_sql)
    blanked, _ = cache_mod.build_catalog_count_sql(catalog, example)
    return {"upsert (one row)": cache_mod.build_upsert_sql(spec, 1),
            f"purge (horizon {spec.max_age_hours:g}h)": purge,
            f"purge when the run could not refresh some rows (spared inside the {quotable:g}h "
            "quotable window)": spared_purge,
            "spared rows (same transaction)": spared,
            "stale_counts": stale, "freshness": fresh,
            "blanked due rows (purged keys still in the catalog)": blanked}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="amazon-price-refresh-agent",
                                 description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--config", action="store_true",
                      help="print the resolved settings and demand config (no DB, no API)")
    mode.add_argument("--print-sql", action="store_true",
                      help="print the cache upsert, purge and metric SQL (no DB, no API)")
    mode.add_argument("--plan", action="store_true",
                      help="read-only DB: budget, tiers and the first --limit candidates (no API)")
    mode.add_argument("--dry-run", action="store_true",
                      help="LIVE Creators getItems for --limit candidates or --asins; NO DB "
                           "writes; exits 1 if any call failed")
    ap.add_argument("--site-yaml", default="", help="config PATH[#dotted.key] "
                                                    "(default $PRICE_REFRESH_CONFIG)")
    ap.add_argument("--agent-id", default="", help="instance id (default $AGENT_ID)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--asins", default="", help="comma-separated ASINs (with --dry-run)")
    args = ap.parse_args(argv)

    if not (args.config or args.print_sql or args.plan or args.dry_run):
        AmazonPriceRefreshAgent(config_spec=args.site_yaml or None,
                                agent_id=args.agent_id or None).run_once()
        return 0
    try:
        settings = _cli_settings(args)
    except settings_mod.SettingsError as exc:
        print(f"config invalid: {exc}", file=sys.stderr)
        return 2
    if args.config:
        _print({"settings": settings.describe(), "demand_priority": priority_config(settings)})
        return 0
    if args.print_sql:
        for name, sql in _sql_preview(settings).items():
            print(f"-- {name}\n{sql};\n")
        return 0
    if args.plan:
        from framework.core import amazon_creators as ac
        pcfg = priority_config(settings)
        rank_cfg = pcfg if pcfg.get("enabled") else fallback_config(pcfg)
        conn = default_connect(settings, readonly=True)
        try:
            tiers = prp.collect_tiers(conn, pcfg) if pcfg.get("enabled") else prp.Tiers()
            cands = prp.rank_refresh_candidates(conn, rank_cfg, limit=max(1, args.limit),
                                                tiers=tiers)
            quotable = settings.quotable_hours(rank_cfg.get("display_hours"))
            fresh = prp.tier_freshness(conn, {**rank_cfg, "display_hours": quotable}, tiers)
        finally:
            conn.close()
        _print({"consumer": settings.consumer,
                "budget_left_today": ac.remaining_budget(settings.consumer),
                "demand_ranking": bool(pcfg.get("enabled")), "signals": tiers.summary(),
                "quotable_hours": quotable,
                "purge_horizon_hours": settings.cache.max_age_hours,
                "freshness": fresh,
                "gate_buffer": settings.gate_problems(rank_cfg),
                "candidates": [c._asdict() for c in cands]})
        return 0
    # --dry-run: live API, no writes, no run record.
    asins = [a.strip() for a in args.asins.split(",") if a.strip()]
    rep = run_refresh(settings, storage=None, dry_run=True, explicit_asins=asins or None,
                      limit_asins=max(1, args.limit))
    print(rep.summary)
    _print(rep.metrics)
    if rep.status != "success":
        print(f"dry run FAILED: {rep.error_text}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
