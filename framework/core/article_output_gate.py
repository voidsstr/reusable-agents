"""Article output gate — cap editorial volume per site, gated on traffic.

WHY THIS EXISTS
---------------
On 2026-09-24 one site had published 298 articles in 30 days and those
articles had earned 0 Google impressions. Each article is an Opus authoring
run. Throttling the proposer's per-run ``daily_cap`` did not help, because
the proposer runs several times a day, and nothing checked whether the
previous batch had done anything before the next one was written.

This module is the framework-level answer. Every article proposer asks it
two questions before spending an Opus call:

1. **Daily cap.** How many proposals has this agent already queued today?
   The default is 1 per calendar day, recorded in a small ledger in agent
   storage.
2. **Traffic gate.** Did at least one article from the previous batch earn
   *any* signal? The previous batch is the articles published in the
   ``lookback_days`` window, excluding anything younger than
   ``min_age_days``. The signal is a GSC impression (per-page GSC export
   from the site's SEO collector) or an AI-assistant/search referral visit
   (the site's AI-traffic log table). If none did, the proposer skips the
   run and the site stops shipping into a void.

When the data to evaluate the traffic gate is missing (no fresh GSC
export AND the AI-traffic table is unreadable), the gate falls back to
**cap-only** mode and says so in ``decision.mode`` and ``decision.reason``.
It never blocks on missing data. When the prior window holds no articles
at all, the gate lets one batch through as a probe. Without that probe,
a quiet site would stay blocked forever.

**Seasonal override.** When a seasonal occasion is NOW or IMMINENT
(``seasonal_calendar``), a batch the traffic gate would block is still
allowed, but only for proposals tagged ``holiday:<occasion-id>``. The
daily cap still applies. Timely deal content is the exception the
operator asked for. It does not open the gate for evergreen topics.

CONFIG (primitive + config + extension point)
---------------------------------------------
Resolution order (later wins, dicts deep-merge):

1. ``DEFAULTS`` below (ships sensible values out of the box)
2. repo ``config/article-output-gate-config.json`` → ``defaults``
   (replaced wholesale by the storage copy at the same key when present)
3. the proposer's ``site.yaml`` → ``proposals.output_gate`` (per-site values)
4. storage config → ``by_agent_id[<agent_id>]`` (operator override, e.g.
   ``{"enabled": false}`` to lift the gate without a commit)

Site-specific VALUES (the SEO collector agent id, table names, article URL
prefixes) belong in site.yaml. The framework only carries templates such as
``{site_id}-seo-opportunity-agent``.

Consumer contract::

    from framework.core import article_output_gate as gate
    decision = gate.evaluate(storage=self.storage, agent_id=self.agent_id,
                             site_id=site_id, dsn=dsn,
                             site_cfg=proposals_cfg.get("output_gate"),
                             audience="tech")
    if not decision.allowed:
        return RunResult(status="success", short_circuited=True, ...)
    user_prompt += decision.prompt_block()
    ...
    kept, dropped = gate.select_for_queue(proposals, decision)
    ... queue `kept` ...
    gate.record_batch(self.storage, self.agent_id,
                      slugs=[p["slug"] for p in kept], run_ts=self.run_ts)

CLI: ``python3 -m framework.cli.article_output_gate --help``.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse

CONFIG_KEY = "config/article-output-gate-config.json"
_REPO_CONFIG = (Path(__file__).resolve().parents[2]
                / "config" / "article-output-gate-config.json")
LEDGER_KEY_TMPL = "agents/{agent_id}/state/output-gate-ledger.json"

UNLIMITED = 10 ** 6

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Proposals this agent may queue per window. 1 = about one article/day.
    "max_proposals_per_day": 1,
    # "calendar-day" (in `timezone`) or "rolling-24h". Calendar-day is the
    # default: an 8-hourly cron that queued at 00:45 yesterday must be able
    # to queue again at 00:45 today. A rolling window at 23h58m blocks it,
    # so the real cadence drifts to one batch every 32h.
    "window": "calendar-day",
    "timezone": "UTC",
    "ledger_keep_days": 60,
    "traffic_gate": {
        "enabled": True,
        "lookback_days": 14,
        # GSC lags 2-3 days, and a fresh URL needs a few days to be crawled.
        # Articles younger than this are not judged yet.
        "min_age_days": 3,
        "min_impressions": 1,
        "min_ai_visits": 1,
        "articles_table": "editorial_articles",
        # Only count GSC / AI-visit paths under these prefixes. Empty = any
        # path whose last segment equals the article slug.
        "article_path_prefixes": [],
        "gsc_source_agent": "{site_id}-seo-opportunity-agent",
        "gsc_pages_files": ["data/gsc-pages-28d.json",
                            "data/gsc-pages-90d.json"],
        "gsc_max_age_hours": 72,
        "gsc_scan_runs": 15,
        "ai_visits_table": "ai_traffic_log",
        "ai_visit_kinds": ["referral"],
    },
    "seasonal_override": {
        "enabled": True,
        "windows": ["now", "imminent"],
    },
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, over: Optional[dict]) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_file_config(storage) -> dict:
    """Storage copy wins over the repo default (whole-file replace)."""
    if storage is not None:
        try:
            doc = storage.read_json(CONFIG_KEY)
            if isinstance(doc, dict) and doc:
                return doc
        except Exception:
            pass
    try:
        return json.loads(_REPO_CONFIG.read_text())
    except Exception:
        return {}


def resolve_config(storage, agent_id: str,
                   site_cfg: Optional[dict] = None) -> dict:
    file_cfg = _load_file_config(storage)
    cfg = _deep_merge(DEFAULTS, file_cfg.get("defaults") or {})
    cfg = _deep_merge(cfg, site_cfg or {})
    cfg = _deep_merge(cfg, (file_cfg.get("by_agent_id") or {}).get(agent_id) or {})
    return cfg


# ---------------------------------------------------------------------------
# Ledger (what this agent queued, when)
# ---------------------------------------------------------------------------

def _now(now: Optional[_dt.datetime] = None) -> _dt.datetime:
    n = now or _dt.datetime.now(_dt.timezone.utc)
    return n if n.tzinfo else n.replace(tzinfo=_dt.timezone.utc)


def _parse_iso(s: str) -> Optional[_dt.datetime]:
    try:
        d = _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def read_ledger(storage, agent_id: str) -> dict:
    try:
        doc = storage.read_json(LEDGER_KEY_TMPL.format(agent_id=agent_id))
    except Exception:
        doc = None
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("schema_version", "1")
    doc.setdefault("batches", [])
    return doc


def record_batch(storage, agent_id: str, *, slugs: Sequence[str],
                 run_ts: str = "", count: Optional[int] = None,
                 keep_days: int = DEFAULTS["ledger_keep_days"],
                 now: Optional[_dt.datetime] = None) -> dict:
    """Append one queued batch to the agent's ledger. Call AFTER the recs
    were actually queued/dispatched. A batch that never left the building
    must not eat tomorrow's slot."""
    n = _now(now)
    doc = read_ledger(storage, agent_id)
    cutoff = n - _dt.timedelta(days=max(1, int(keep_days)))
    batches = [b for b in doc["batches"]
               if (_parse_iso(b.get("at", "")) or n) >= cutoff]
    slugs = [str(s) for s in slugs if s]
    batches.append({
        "at": n.isoformat(timespec="seconds"),
        "run_ts": run_ts,
        "count": int(count if count is not None else len(slugs)),
        "slugs": slugs,
    })
    doc["batches"] = batches
    storage.write_json(LEDGER_KEY_TMPL.format(agent_id=agent_id), doc)
    return doc


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name or "UTC")
    except Exception:
        return _dt.timezone.utc


def queued_in_window(ledger: dict, cfg: dict,
                     now: Optional[_dt.datetime] = None) -> int:
    n = _now(now)
    total = 0
    if cfg.get("window") == "rolling-24h":
        cutoff = n - _dt.timedelta(hours=24)
        for b in ledger.get("batches") or []:
            at = _parse_iso(b.get("at", ""))
            if at and at > cutoff:
                total += int(b.get("count") or 0)
        return total
    tz = _tz(cfg.get("timezone") or "UTC")
    today = n.astimezone(tz).date()
    for b in ledger.get("batches") or []:
        at = _parse_iso(b.get("at", ""))
        if at and at.astimezone(tz).date() == today:
            total += int(b.get("count") or 0)
    return total


# ---------------------------------------------------------------------------
# Traffic evidence
# ---------------------------------------------------------------------------

def slug_from_path(url_or_path: str,
                   prefixes: Sequence[str] = ()) -> str:
    """Last path segment, lowercased. '' when the path is outside every
    configured prefix."""
    p = str(url_or_path or "")
    if "://" in p:
        p = urlparse(p).path
    p = p.split("?", 1)[0].split("#", 1)[0]
    if prefixes and not any(p.startswith(pre) for pre in prefixes):
        return ""
    p = p.rstrip("/")
    return p.rsplit("/", 1)[-1].strip().lower() if p else ""


def _run_ts_to_dt(run_ts: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(run_ts[:16], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def load_gsc_pages(storage, source_agent: str, *,
                   files: Sequence[str], max_age_hours: float,
                   scan_runs: int = 15,
                   now: Optional[_dt.datetime] = None
                   ) -> tuple[Optional[list[dict]], str]:
    """Newest per-page GSC export from `source_agent`'s run dirs.

    Returns (rows, status). rows is None when unavailable. Walks the
    run-index (`recent[]`, newest first). Listing `runs/` directly is
    capped at 10K keys and returns the OLDEST runs."""
    if storage is None or not source_agent:
        return None, "unavailable: no storage/source agent"
    try:
        idx = storage.read_json(f"agents/{source_agent}/run-index.json") or {}
    except Exception as e:
        return None, f"unavailable: run-index read failed ({e})"
    recent = idx.get("recent") if isinstance(idx, dict) else None
    if not recent:
        return None, f"unavailable: {source_agent} has no run-index"
    n = _now(now)
    for entry in recent[:max(1, int(scan_runs))]:
        rts = str(entry.get("run_ts") or "")
        if not rts:
            continue
        for rel in files:
            key = f"agents/{source_agent}/runs/{rts}/{rel}"
            try:
                if hasattr(storage, "exists") and not storage.exists(key):
                    continue
                doc = storage.read_json(key)
            except Exception:
                continue
            rows = (doc.get("rows") if isinstance(doc, dict) else doc) or None
            if rows is None:
                continue
            when = _run_ts_to_dt(rts)
            if when and (n - when).total_seconds() > max_age_hours * 3600:
                return None, (f"unavailable: newest GSC export {rts} is older "
                              f"than {max_age_hours}h")
            return list(rows), f"ok: {source_agent}/{rts}/{rel} ({len(rows)} pages)"
    return None, f"unavailable: no {', '.join(files)} in last {scan_runs} runs"


def _connect(dsn: str):
    import psycopg2
    return psycopg2.connect(dsn, connect_timeout=15,
                            options="-c statement_timeout=60000")


def _ident(name: str) -> str:
    if not _IDENT_RE.match(str(name or "")):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def read_prior_batch(conn, *, table: str, lookback_days: int,
                     min_age_days: int) -> list[dict]:
    """Articles published in [now - lookback, now - min_age]. Future-dated
    (scheduled) rows are excluded by the upper bound."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT slug, published_at FROM {_ident(table)} "
            " WHERE status = 'published' AND slug IS NOT NULL "
            "   AND published_at >= NOW() - make_interval(days => %s) "
            "   AND published_at <= NOW() - make_interval(days => %s) "
            " ORDER BY published_at DESC LIMIT 1000",
            (int(lookback_days), int(min_age_days)))
        out = []
        for r in cur.fetchall():
            slug = r["slug"] if isinstance(r, dict) else r[0]
            pub = r["published_at"] if isinstance(r, dict) else r[1]
            out.append({"slug": str(slug).strip().lower(),
                        "published_at": pub.isoformat() if pub else None})
        return out


def read_ai_visits(conn, *, table: str, kinds: Sequence[str],
                   days: int) -> dict[str, int]:
    """path → visit count for AI-assistant / search referrals."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT path, COUNT(*) AS n FROM {_ident(table)} "
            " WHERE kind = ANY(%s) AND ts >= NOW() - make_interval(days => %s) "
            " GROUP BY path",
            (list(kinds), int(days)))
        out: dict[str, int] = {}
        for r in cur.fetchall():
            path = r["path"] if isinstance(r, dict) else r[0]
            n = r["n"] if isinstance(r, dict) else r[1]
            out[str(path or "")] = int(n or 0)
        return out


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass
class GateDecision:
    allowed: bool
    slots: int
    reason: str
    mode: str                 # disabled | cap+traffic | cap-only | seasonal-override
    cap: int = UNLIMITED
    queued_today: int = 0
    batch_size: int = 0
    batch_hits: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    require_holiday_ids: list = field(default_factory=list)
    priority_holiday_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def metrics(self) -> dict:
        """Flat numeric metrics for RunResult.metrics (goal auto-tracking
        resolves flat keys only)."""
        return {
            "output_gate_allowed": 1.0 if self.allowed else 0.0,
            "output_gate_slots": float(min(self.slots, 999)),
            "output_gate_queued_today": float(self.queued_today),
            "output_gate_prior_batch": float(self.batch_size),
            "output_gate_prior_batch_hits": float(len(self.batch_hits)),
        }

    def prompt_block(self) -> str:
        """Tell the proposer LLM how many proposals will actually be
        written, so it ranks best-first instead of padding the list."""
        if self.mode == "disabled" or self.slots >= UNLIMITED:
            return ""
        lines = [
            "OUTPUT GATE — READ BEFORE RANKING.",
            f"Only the FIRST {self.slots} proposal(s) in your list will be "
            "written today. Everything after that is discarded. Order the "
            "list best-first: the first proposal must be the single article "
            "most likely to earn search impressions, an AI-assistant "
            "citation, and a real Amazon purchase.",
        ]
        if self.batch_hits:
            winners = ", ".join(h["slug"] for h in self.batch_hits[:8])
            lines.append(
                "Recent articles that DID earn impressions or AI visits (build "
                f"on these topics and formats): {winners}")
        if self.require_holiday_ids:
            lines.append(
                "The traffic gate is closed for evergreen topics. Only a "
                "proposal tagged holiday:<id> for one of these occasions will "
                f"be written: {', '.join(self.require_holiday_ids)}. Put it "
                "first and carry the tag in `tags`.")
        elif self.priority_holiday_ids:
            lines.append(
                "A seasonal deal occasion is NOW/IMMINENT "
                f"({', '.join(self.priority_holiday_ids)}). If none of the "
                "RECENTLY PUBLISHED articles already covers it, the first "
                "proposal must target it and carry `holiday:<id>` in `tags`.")
        return "\n".join(lines) + "\n"


def active_holiday_ids(audience: str, windows: Iterable[str],
                       today: Optional[_dt.date] = None) -> list[str]:
    try:
        from . import seasonal_calendar
    except Exception:
        return []
    wanted = set(windows or ())
    return [a.occasion.id
            for a in seasonal_calendar.active_signal(today, audience=audience)
            if a.window in wanted]


def evaluate(*, storage, agent_id: str, site_id: str,
             dsn: Optional[str] = None,
             site_cfg: Optional[dict] = None,
             audience: Optional[str] = None,
             now: Optional[_dt.datetime] = None,
             today: Optional[_dt.date] = None,
             conn=None) -> GateDecision:
    """Decide whether this run may queue proposals, and how many."""
    cfg = resolve_config(storage, agent_id, site_cfg)
    if not cfg.get("enabled", True):
        return GateDecision(True, UNLIMITED, "output gate disabled", "disabled")

    cap = max(0, int(cfg.get("max_proposals_per_day", 1)))
    ledger = read_ledger(storage, agent_id) if storage is not None else {}
    already = queued_in_window(ledger, cfg, now)
    slots = max(0, cap - already)

    so = cfg.get("seasonal_override") or {}
    holiday_ids: list[str] = []
    if audience and so.get("enabled", True):
        holiday_ids = active_holiday_ids(audience, so.get("windows") or (), today)

    base = dict(cap=cap, queued_today=already,
                priority_holiday_ids=list(holiday_ids))
    if slots == 0:
        return GateDecision(
            False, 0,
            f"daily cap reached: {already}/{cap} proposal(s) already queued "
            f"this {cfg.get('window')}", "cap-only", **base)

    tg = cfg.get("traffic_gate") or {}
    if not tg.get("enabled", True):
        return GateDecision(True, slots, "traffic gate disabled; cap only",
                            "cap-only", **base)

    sources: dict[str, str] = {}
    prefixes = list(tg.get("article_path_prefixes") or [])
    own_conn = False
    batch: Optional[list[dict]] = None
    ai_by_slug: Optional[dict[str, int]] = None
    try:
        if conn is None and dsn:
            conn = _connect(dsn)
            own_conn = True
        if conn is not None:
            try:
                batch = read_prior_batch(
                    conn, table=tg.get("articles_table", "editorial_articles"),
                    lookback_days=int(tg.get("lookback_days", 14)),
                    min_age_days=int(tg.get("min_age_days", 3)))
                sources["prior_batch"] = f"ok: {len(batch)} article(s)"
            except Exception as e:
                sources["prior_batch"] = f"unavailable: {type(e).__name__}: {e}"[:200]
                try: conn.rollback()
                except Exception: pass
            try:
                visits = read_ai_visits(
                    conn, table=tg.get("ai_visits_table", "ai_traffic_log"),
                    kinds=tg.get("ai_visit_kinds") or ["referral"],
                    days=int(tg.get("lookback_days", 14)) + 1)
                ai_by_slug = {}
                for path, n in visits.items():
                    sl = slug_from_path(path, prefixes)
                    if sl:
                        ai_by_slug[sl] = ai_by_slug.get(sl, 0) + n
                sources["ai_visits"] = f"ok: {len(visits)} path(s)"
            except Exception as e:
                sources["ai_visits"] = f"unavailable: {type(e).__name__}: {e}"[:200]
                try: conn.rollback()
                except Exception: pass
        else:
            sources["prior_batch"] = "unavailable: no DSN"
            sources["ai_visits"] = "unavailable: no DSN"
    except Exception as e:
        sources.setdefault("prior_batch", f"unavailable: connect failed: {e}"[:200])
        sources.setdefault("ai_visits", f"unavailable: connect failed: {e}"[:200])
    finally:
        if own_conn and conn is not None:
            try: conn.close()
            except Exception: pass

    gsc_agent = str(tg.get("gsc_source_agent") or "").format(site_id=site_id)
    rows, gsc_status = load_gsc_pages(
        storage, gsc_agent,
        files=tg.get("gsc_pages_files") or DEFAULTS["traffic_gate"]["gsc_pages_files"],
        max_age_hours=float(tg.get("gsc_max_age_hours", 72)),
        scan_runs=int(tg.get("gsc_scan_runs", 15)), now=now)
    sources["gsc"] = gsc_status
    gsc_by_slug: Optional[dict[str, int]] = None
    if rows is not None:
        gsc_by_slug = {}
        for r in rows:
            keys = r.get("keys") or [r.get("page") or r.get("url") or ""]
            sl = slug_from_path(keys[0] if keys else "", prefixes)
            if sl:
                gsc_by_slug[sl] = gsc_by_slug.get(sl, 0) + int(r.get("impressions") or 0)

    base["sources"] = sources
    if batch is None:
        return GateDecision(True, slots,
                            "traffic data unavailable (prior batch unreadable); "
                            "cap only", "cap-only", **base)
    base["batch_size"] = len(batch)
    if not batch:
        return GateDecision(True, slots,
                            f"no articles published {tg.get('min_age_days')}-"
                            f"{tg.get('lookback_days')}d ago; probe batch allowed",
                            "cap+traffic", **base)
    if gsc_by_slug is None and ai_by_slug is None:
        return GateDecision(True, slots,
                            "traffic data unavailable (no fresh GSC export, "
                            "AI-visit table unreadable); cap only",
                            "cap-only", **base)

    min_impr = int(tg.get("min_impressions", 1))
    min_ai = int(tg.get("min_ai_visits", 1))
    hits = []
    for a in batch:
        impr = (gsc_by_slug or {}).get(a["slug"], 0)
        ai = (ai_by_slug or {}).get(a["slug"], 0)
        if (gsc_by_slug is not None and impr >= min_impr) or \
                (ai_by_slug is not None and ai >= min_ai):
            hits.append({"slug": a["slug"], "impressions": impr,
                         "ai_visits": ai})
    base["batch_hits"] = hits
    if hits:
        return GateDecision(True, slots,
                            f"traffic gate open: {len(hits)}/{len(batch)} prior "
                            "article(s) earned impressions or AI visits",
                            "cap+traffic", **base)
    if holiday_ids:
        base["require_holiday_ids"] = list(holiday_ids)
        return GateDecision(True, slots,
                            f"traffic gate closed (0/{len(batch)} prior articles "
                            "earned a signal) — seasonal override for "
                            f"{', '.join(holiday_ids)} only",
                            "seasonal-override", **base)
    return GateDecision(False, 0,
                        f"traffic gate closed: 0/{len(batch)} articles published "
                        f"{tg.get('min_age_days')}-{tg.get('lookback_days')}d ago "
                        "earned a GSC impression or an AI/search visit",
                        "cap+traffic", **base)


# ---------------------------------------------------------------------------
# Post-LLM selection
# ---------------------------------------------------------------------------

def proposal_holiday_ids(p: dict) -> set[str]:
    out: set[str] = set()
    for t in p.get("tags") or []:
        t = str(t).strip().lower()
        if t.startswith("holiday:"):
            out.add(t.split(":", 1)[1])
    for k in ("holiday", "occasion_id", "holiday_id"):
        v = p.get(k)
        if v:
            out.add(str(v).strip().lower().removeprefix("holiday:"))
    return out


def select_for_queue(proposals: Sequence[dict], decision: GateDecision
                     ) -> tuple[list[dict], list[dict]]:
    """Order + trim proposals to the gate's slots. Returns (kept, dropped).

    Proposals tagged for a NOW/IMMINENT occasion move to the front, keeping
    LLM order otherwise. Under a seasonal override only tagged proposals
    are eligible."""
    if not decision.allowed:
        return [], list(proposals or [])
    props = list(proposals or [])
    want = set(decision.require_holiday_ids or decision.priority_holiday_ids or [])
    if want:
        tagged = [p for p in props if proposal_holiday_ids(p) & want]
        rest = [p for p in props if not (proposal_holiday_ids(p) & want)]
        props = tagged if decision.require_holiday_ids else tagged + rest
    kept = props[:decision.slots]
    kept_ids = {id(p) for p in kept}
    dropped = [p for p in proposals or [] if id(p) not in kept_ids]
    return kept, dropped
