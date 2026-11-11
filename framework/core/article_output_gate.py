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
daily cap still applies, and so does a per-occasion cap
(``seasonal_override.max_per_occasion``, default 2 pieces per occasion per
site): once an occasion has that many tagged articles, the override closes
for it. Timely deal content is the exception the operator asked for. It
does not open the gate for evergreen topics, and it is not a daily pass.

**Format lanes.** ``format_lanes`` names evergreen formats that may also
ship while the traffic gate is closed, each with its own cap
(``{"x-or-y": {"max_per_window": 2, "window_days": 7}}``). Only proposals
tagged ``lane:<id>`` qualify, and they rank after holiday-tagged ones. A
lane is the operator's way of saying "this format earns citations
elsewhere on the site, keep a trickle of it going" without opening the
gate for everything. Counted like occasions: articles tagged ``lane:<id>``
created in the window (any status but archived) plus ledger batches that
recorded the lane. The daily cap still applies. A lane may carry a dated
``boost`` (``{"max_per_window": 3, "until": "2026-11-09"}``): through the
``until`` date (inclusive) the boost cap replaces ``max_per_window``, and the
day after, the lane falls back to its base cap with no config change.

**AI-visit evidence** counts distinct visitor IPs per article path and
drops our own traffic: private / loopback addresses, the
``framework.core.ai_traffic`` exclude list (``AI_TRAFFIC_EXCLUDE_IPS``),
rows whose ``ip_verified`` is false, and an optional user-agent regex for
internal refreshers.

**Affiliate-click evidence** (``traffic_gate.affiliate_clicks``, off by
default). A prior-batch article also counts as a hit when it earned at
least ``min_clicks`` verified-human affiliate clicks over
``lookback_days + 1`` days. The count comes from ``framework.core.
human_clicks`` (``resolve_spec(spec, profile=<agent id>)`` +
``human_counts_by``), the same definition of "a person clicked a buy link"
the goal trackers use, so an operator filter in config/human-click-filter-
config.json applies here too. Why (2026-11): one site's GSC gave the gate 0
hits on 14 straight days while its recent Black Friday roundups earned 9
verified Amazon clicks. When the source is on, ``batch_hits[]`` carry
``affiliate_clicks`` and the prompt block names the click winners first.

**Canonical holiday ids.** Every holiday id the gate reads (proposal tags,
ledger entries, ``holiday:<id>`` tags in the articles table,
``max_per_occasion_by_id`` keys) goes through
``seasonal_calendar.canonicalizer()``, so ``holiday:black-friday-cyber-
monday`` is Black Friday. Before 2026-11-11 the override matched ids
exactly and silently dropped three correctly targeted roundups. Unknown ids
are dropped with a warning. ``max_per_occasion_by_id`` sets one occasion's
cap (``{"black-friday": {"max_per_occasion": 20, "until": "2026-12-01"}}``),
so a deal season can be raised without raising every occasion.

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
                      slugs=[p["slug"] for p in kept], run_ts=self.run_ts,
                      holiday_ids=gate.holiday_ids_for(kept),
                      lane_ids=gate.lane_ids_for(kept))

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
        # Which AI-visit rows count (2026-10 audit: our own probes, the docker
        # bridge and a cache refresher were logged as "referrals").
        # `ai_traffic` is merged into framework.core.ai_traffic.config(), so
        # the exclude list is the one every AI-traffic reader shares:
        # `exclude_ips` + the AI_TRAFFIC_EXCLUDE_IPS env var (an entry ending
        # in "." or ":" is a prefix, e.g. an IPv6 /64), plus the ip / status /
        # ip_verified column auto-detection and `only_status_200`.
        "ai_traffic": {},
        # Loopback, RFC 1918, docker bridge, IPv6 ULA / link-local: never a
        # real visitor.
        "ai_visit_exclude_private_ips": True,
        # Case-insensitive POSIX regex on the user-agent column. Site-specific
        # internal refreshers go in site.yaml, e.g. "micro-cache-refresh".
        "ai_visit_exclude_user_agent_regex": "",
        "ai_visit_user_agent_column": "auto",
        # Count distinct visitor IPs per path, not rows: a refresher hitting
        # the page right after a real visitor must not turn 1 into 2.
        "ai_visit_distinct_ips": True,
        # Verified-human affiliate clicks as a third kind of hit (see
        # "Affiliate-click evidence" above). Per-site values in site.yaml:
        # `table` (click log), `spec` (framework.core.human_clicks column
        # spec), `match` (e.g. {"target": "amazon"}), `group_col` (the
        # column holding the page the click came from; a path or full URL,
        # mapped to a slug with article_path_prefixes). `label` names the
        # clicks in the prompt block and reasons.
        "affiliate_clicks": {
            "enabled": False,
            "table": None,
            "spec": {},
            "match": {},
            "group_col": None,
            "min_clicks": 1,
            "label": "Amazon clicks",
        },
    },
    "seasonal_override": {
        "enabled": True,
        "windows": ["now", "imminent"],
        # At most this many holiday:<id>-tagged pieces per occasion per site
        # (counted over `occasion_count_days`, articles not archived, plus
        # ledger batches that recorded holiday ids) may ship through the
        # override while the traffic gate is closed. None / negative =
        # unlimited. Before 2026-10 the override let one deal piece through
        # every day: 7 Prime Big Deal Days pieces per site into a domain
        # earning no impressions.
        "max_per_occasion": 2,
        # Per-occasion override of max_per_occasion, keyed by occasion id
        # (aliases accepted): {"black-friday": 20}, or dated
        # {"black-friday": {"max_per_occasion": 20, "until": "2026-12-01"}}
        # (applies through `until`, inclusive; the day after, the default
        # cap applies again with no config change). None / negative =
        # unlimited for that occasion. Malformed entries are ignored.
        "max_per_occasion_by_id": {},
        "occasion_count_days": 60,
        "tags_column": "tags",
    },
    # lane id -> {"max_per_window": int, "window_days": int (default 7),
    #             "boost": {"max_per_window": int, "until": "YYYY-MM-DD"}}.
    # Empty = no lanes; see "Format lanes" above.
    "format_lanes": {},
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
                 holiday_ids: Optional[Sequence[str]] = None,
                 lane_ids: Optional[Sequence[str]] = None,
                 keep_days: int = DEFAULTS["ledger_keep_days"],
                 now: Optional[_dt.datetime] = None) -> dict:
    """Append one queued batch to the agent's ledger. Call AFTER the recs
    were actually queued/dispatched. A batch that never left the building
    must not eat tomorrow's slot.

    `holiday_ids`: one entry per queued proposal that carries a
    holiday:<id> tag (see :func:`holiday_ids_for`). It lets the seasonal
    override's per-occasion cap count pieces that are queued but not yet
    written to the articles table. `lane_ids` does the same for
    ``format_lanes`` (see :func:`lane_ids_for`)."""
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
        **({"holiday_ids": [str(h).strip().lower() for h in holiday_ids if h]}
           if holiday_ids else {}),
        **({"lane_ids": [str(h).strip().lower() for h in lane_ids if h]}
           if lane_ids else {}),
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


_PRIVATE_IP_RE = (r"^(::ffff:)?(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)"
                  r"|^(::1$|f[cd][0-9a-f]{2}:|fe80:)")


def _split_exclude_ips(entries: Iterable[str]) -> tuple[list[str], list[str]]:
    """Kept for callers of the old name; the rule lives in ai_traffic."""
    from . import ai_traffic
    return ai_traffic.split_exclude_ips(entries)


def build_ai_visits_sql(tg: dict, atcfg: dict, *, table: str,
                        kinds: Sequence[str], days: int,
                        ua_column: str = "") -> tuple[str, dict]:
    """(sql, params) for :func:`read_ai_visits`. `atcfg` is an
    ``ai_traffic.config()`` with its columns resolved. Pure — no DB."""
    t = _ident(table)
    ts = _ident(atcfg.get("ts_column") or "ts")
    kind = _ident(atcfg.get("kind_column") or "kind")
    path = _ident(atcfg.get("path_column") or "path")
    ip = _ident(atcfg["ip_column"]) if atcfg.get("ip_column") else ""
    status = _ident(atcfg["status_column"]) if atcfg.get("status_column") else ""
    verified = _ident(atcfg["verified_column"]) if atcfg.get("verified_column") else ""
    ua = _ident(ua_column) if ua_column else ""

    params: dict[str, Any] = {"kinds": list(kinds), "days": int(days)}
    where = [f"l.{kind} = ANY(%(kinds)s)",
             f"l.{ts} >= NOW() - make_interval(days => %(days)s)"]
    if status and atcfg.get("only_status_200", True):
        where.append(f"(l.{status} IS NULL OR l.{status} = 200)")
    if verified:
        # Referral rows are human browsers and are usually never IP-checked
        # (NULL). Drop only rows the logger proved false.
        where.append(f"l.{verified} IS NOT FALSE")
    if ip:
        if tg.get("ai_visit_exclude_private_ips", True):
            params["private_ip_re"] = _PRIVATE_IP_RE
            where.append(f"(l.{ip} IS NULL OR l.{ip} !~* %(private_ip_re)s)")
        exact, prefixes = _split_exclude_ips(atcfg.get("exclude_ips") or [])
        if exact:
            params["exclude_ips"] = exact
            where.append(f"(l.{ip} IS NULL OR NOT (lower(l.{ip}) = ANY(%(exclude_ips)s)))")
        if prefixes:
            params["exclude_ip_prefixes"] = prefixes
            where.append(f"(l.{ip} IS NULL OR NOT (lower(l.{ip}) LIKE ANY(%(exclude_ip_prefixes)s)))")
    ua_re = str(tg.get("ai_visit_exclude_user_agent_regex") or "")
    if ua and ua_re:
        params["exclude_ua_re"] = ua_re
        where.append(f"(l.{ua} IS NULL OR l.{ua} !~* %(exclude_ua_re)s)")
    if ip and tg.get("ai_visit_distinct_ips", True):
        # A row with no IP still counts once on its own.
        n = (f"COUNT(DISTINCT l.{ip}) + COUNT(*) FILTER (WHERE l.{ip} IS NULL)")
    else:
        n = "COUNT(*)"
    sql = (f"SELECT l.{path} AS path, {n} AS n FROM {t} l\n WHERE "
           + "\n   AND ".join(where) + f"\n GROUP BY l.{path}")
    return sql, params


def read_ai_visits(conn, *, table: str, kinds: Sequence[str],
                   days: int, tg: Optional[dict] = None) -> dict[str, int]:
    """path → AI-assistant / search referral visitors (distinct IPs by
    default), with our own and private traffic excluded (see the
    ``traffic_gate.ai_*`` knobs)."""
    from . import ai_traffic
    tg = tg if tg is not None else DEFAULTS["traffic_gate"]
    atcfg = ai_traffic.config({**(tg.get("ai_traffic") or {}), "table": table})
    atcfg = ai_traffic.resolve_columns(conn, atcfg)
    ua_column = str(tg.get("ai_visit_user_agent_column") or "")
    if tg.get("ai_visit_exclude_user_agent_regex") and ua_column == "auto":
        try:
            cols = ai_traffic.table_columns(conn, table)
        except Exception:
            cols = set()
        ua_column = next((c for c in ("user_agent", "ua") if c in cols), "")
    elif ua_column == "auto":
        ua_column = ""
    sql, params = build_ai_visits_sql(tg, atcfg, table=table, kinds=kinds,
                                      days=days, ua_column=ua_column)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        out: dict[str, int] = {}
        for r in cur.fetchall():
            path = r["path"] if isinstance(r, dict) else r[0]
            n = r["n"] if isinstance(r, dict) else r[1]
            out[str(path or "")] = out.get(str(path or ""), 0) + int(n or 0)
        return out


def read_affiliate_clicks(conn, ac: dict, *, days: int, profile: str = "",
                          storage=None) -> dict[str, int]:
    """``group_col`` value (the page a click came from) → verified-human
    affiliate clicks over `days`. `ac` is a ``traffic_gate.affiliate_clicks``
    block: ``table``, ``spec`` (framework.core.human_clicks column spec),
    ``match``, ``group_col``. The spec resolves with ``profile`` (the
    consumer's agent id), so config/human-click-filter-config.json
    ``defaults`` and ``by_profile[<profile>]`` apply."""
    from . import human_clicks
    table = str(ac.get("table") or "")
    group_col = str(ac.get("group_col") or "")
    if not table or not group_col:
        raise ValueError("affiliate_clicks needs `table` and `group_col`")
    spec = human_clicks.resolve_spec(ac.get("spec") or {}, profile=profile,
                                     storage=storage)
    return human_clicks.human_counts_by(conn, spec, table=table,
                                        group_col=group_col,
                                        window_days=int(days),
                                        match=ac.get("match") or None)


def affiliate_clicks_by_slug(conn, ac: dict, *, days: int,
                             prefixes: Sequence[str] = (), profile: str = "",
                             storage=None) -> dict[str, int]:
    """Article slug → verified-human affiliate clicks from its page over
    `days` (see :func:`read_affiliate_clicks`). Pages outside `prefixes`
    are skipped; query strings and trailing slashes are ignored. Shared
    with framework.core.featured_rotation (``score_source``)."""
    out: dict[str, int] = {}
    for value, n in read_affiliate_clicks(conn, ac, days=days, profile=profile,
                                          storage=storage).items():
        sl = slug_from_path(value, prefixes)
        if sl and n:
            out[sl] = out.get(sl, 0) + int(n)
    return out


def read_occasion_counts(conn, *, table: str, tags_column: str,
                         holiday_ids: Sequence[str], days: int,
                         prefix: str = "holiday",
                         canonical=None) -> dict[str, int]:
    """holiday id → articles tagged ``holiday:<id>`` created in the last
    `days` days (any status but archived, so scheduled pieces count).
    ``prefix="lane"`` counts ``lane:<id>`` tags instead.

    `canonical` (a ``seasonal_calendar.canonicalizer()``) makes holiday
    counts alias-aware: every ``holiday:*`` tag in the window is mapped to
    its calendar id, so an article tagged ``holiday:black-friday-cyber-
    monday`` counts toward ``black-friday``. Without it, tags must match
    exactly."""
    ids = [str(h).strip().lower() for h in holiday_ids if h]
    if not ids:
        return {}
    col = _ident(tags_column)
    if canonical is not None and prefix == "holiday":
        return _read_canonical_occasion_counts(conn, table=table, col=col,
                                               ids=ids, days=days,
                                               canonical=canonical)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT lower(t) AS tag, COUNT(DISTINCT a.slug) AS n "
            f"  FROM {_ident(table)} a, unnest(a.{col}) AS t "
            " WHERE lower(t) = ANY(%s) "
            "   AND COALESCE(a.status, '') <> 'archived' "
            "   AND COALESCE(a.created_at, a.published_at) >= NOW() - make_interval(days => %s) "
            " GROUP BY 1",
            ([f"{prefix}:{h}" for h in ids], int(days)))
        out = {h: 0 for h in ids}
        for r in cur.fetchall():
            tag = r["tag"] if isinstance(r, dict) else r[0]
            n = r["n"] if isinstance(r, dict) else r[1]
            h = str(tag).split(":", 1)[-1]
            if h in out:
                out[h] = int(n or 0)
        return out


def _read_canonical_occasion_counts(conn, *, table: str, col: str,
                                    ids: list[str], days: int,
                                    canonical) -> dict[str, int]:
    """Alias-aware :func:`read_occasion_counts`. One row per distinct
    ``holiday:*`` tag in the window: (tag, distinct articles, their slugs).
    Slugs make the per-occasion count exact when one article carries two
    spellings of the same occasion; a row without them adds its count."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT lower(t) AS tag, COUNT(DISTINCT a.slug) AS n, "
            f"       array_agg(DISTINCT a.slug) AS slugs "
            f"  FROM {_ident(table)} a, unnest(a.{col}) AS t "
            " WHERE lower(t) LIKE %s "
            "   AND COALESCE(a.status, '') <> 'archived' "
            "   AND COALESCE(a.created_at, a.published_at) >= NOW() - make_interval(days => %s) "
            " GROUP BY 1",
            ("holiday:%", int(days)))
        slugs: dict[str, set] = {h: set() for h in ids}
        extra: dict[str, int] = {h: 0 for h in ids}
        for r in cur.fetchall():
            if isinstance(r, dict):
                tag, n, sl = r.get("tag"), r.get("n"), r.get("slugs")
            else:
                tag, n = r[0], r[1]
                sl = r[2] if len(r) > 2 else None
            h = canonical(str(tag).split(":", 1)[-1])
            if h not in slugs:
                continue
            if sl is None:
                extra[h] += int(n or 0)
            else:
                slugs[h].update(str(s) for s in sl if s)
        return {h: len(slugs[h]) + extra[h] for h in ids}


def ledger_occasion_counts(ledger: dict, holiday_ids: Sequence[str], days: int,
                           now: Optional[_dt.datetime] = None,
                           key: str = "holiday_ids",
                           canonical=None) -> dict[str, int]:
    """holiday id → proposals the ledger recorded for it in the last `days`.
    ``key="lane_ids"`` counts format lanes instead. `canonical` maps
    recorded holiday ids (older entries may hold an alias) to calendar ids."""
    n = _now(now)
    cutoff = n - _dt.timedelta(days=max(1, int(days)))
    ids = [str(h).strip().lower() for h in holiday_ids if h]
    out = {h: 0 for h in ids}
    for b in (ledger or {}).get("batches") or []:
        at = _parse_iso(b.get("at", ""))
        if not at or at < cutoff:
            continue
        for h in b.get(key) or []:
            h = str(h).strip().lower()
            if canonical is not None:
                h = canonical(h) or h
            if h in out:
                out[h] += 1
    return out


def occasion_caps(so: dict, holiday_ids: Sequence[str], *,
                  today: Optional[_dt.date] = None,
                  canonical=None) -> dict[str, Optional[int]]:
    """holiday id → seasonal-override cap for it (None = unlimited).

    ``max_per_occasion`` is the default; ``max_per_occasion_by_id`` replaces
    it per occasion, as an int or as ``{"max_per_occasion": N, "until":
    "YYYY-MM-DD"}`` (through `until`, inclusive). Keys may be aliases
    (`canonical`). A negative or null cap means unlimited. A malformed
    by-id entry is ignored; a malformed default raises, as it always has."""
    def _cap(v):
        if v is None:
            return None
        v = int(v)
        return None if v < 0 else v

    default = _cap(so.get("max_per_occasion"))
    today = today or _dt.date.today()
    by_id: dict[str, Optional[int]] = {}
    raw = so.get("max_per_occasion_by_id")
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        key = (canonical(k) if canonical is not None else None) or \
            str(k).strip().lower().removeprefix("holiday:")
        try:
            if isinstance(v, dict):
                if "max_per_occasion" not in v:
                    continue
                until = v.get("until")
                if until is not None and today > _dt.date.fromisoformat(str(until)):
                    continue
                by_id[key] = _cap(v.get("max_per_occasion"))
            else:
                by_id[key] = _cap(v)
        except (TypeError, ValueError):
            continue
    return {h: (by_id[h] if h in by_id else default) for h in holiday_ids}


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
    occasion_counts: dict = field(default_factory=dict)
    lane_ids: list = field(default_factory=list)       # open format lanes
    lane_room: dict = field(default_factory=dict)      # lane id -> slots left
    lane_counts: dict = field(default_factory=dict)
    # Seasonal audience the decision was made for; select_for_queue()
    # resolves proposal holiday ids against it.
    audience: str = ""
    # Set (to traffic_gate.affiliate_clicks.label) when verified affiliate
    # clicks were read; batch_hits[] then carry `affiliate_clicks`.
    click_label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def metrics(self) -> dict:
        """Flat numeric metrics for RunResult.metrics (goal auto-tracking
        resolves flat keys only)."""
        out = {
            "output_gate_allowed": 1.0 if self.allowed else 0.0,
            "output_gate_slots": float(min(self.slots, 999)),
            "output_gate_queued_today": float(self.queued_today),
            "output_gate_prior_batch": float(self.batch_size),
            "output_gate_prior_batch_hits": float(len(self.batch_hits)),
        }
        if self.click_label:
            out["output_gate_prior_batch_affiliate_clicks"] = float(
                sum(int(h.get("affiliate_clicks") or 0) for h in self.batch_hits))
        return out

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
        if self.batch_hits and self.click_label:
            winners = ", ".join(_hit_with_counts(h, self.click_label)
                                for h in self.batch_hits[:8])
            lines.append(
                "Recent articles that earned impressions, AI visits or "
                f"{self.click_label} (build on these topics and formats; "
                f"most {self.click_label} first): {winners}")
        elif self.batch_hits:
            winners = ", ".join(h["slug"] for h in self.batch_hits[:8])
            lines.append(
                "Recent articles that DID earn impressions or AI visits (build "
                f"on these topics and formats): {winners}")
        if self.require_holiday_ids or self.lane_ids:
            allowed = []
            if self.require_holiday_ids:
                allowed.append("a proposal tagged holiday:<id> for one of these "
                               f"occasions: {', '.join(self.require_holiday_ids)}")
            if self.lane_ids:
                allowed.append("a proposal tagged lane:<id> for one of these "
                               f"formats: {', '.join(self.lane_ids)}")
            lines.append(
                "The traffic gate is closed for evergreen topics. Only "
                + " or ".join(allowed) + " will be written. Holiday-tagged "
                "proposals go first. Carry the tag in `tags`.")
        elif self.priority_holiday_ids:
            lines.append(
                "A seasonal deal occasion is NOW/IMMINENT "
                f"({', '.join(self.priority_holiday_ids)}). If none of the "
                "RECENTLY PUBLISHED articles already covers it, the first "
                "proposal must target it and carry `holiday:<id>` in `tags`.")
        return "\n".join(lines) + "\n"


def _hit_with_counts(h: dict, click_label: str) -> str:
    """'slug (Amazon clicks: 7; AI visits: 1)' — non-zero counts only."""
    parts = [f"{label}: {int(h.get(k) or 0)}"
             for k, label in (("affiliate_clicks", click_label),
                              ("ai_visits", "AI visits"),
                              ("impressions", "impressions"))
             if int(h.get(k) or 0)]
    return f"{h['slug']} ({'; '.join(parts)})" if parts else str(h["slug"])


def _calendar(storage):
    """Occasions with the storage calendar config applied; the defaults when
    the calendar or the config is unreadable."""
    try:
        from . import seasonal_calendar
    except Exception:
        return None
    try:
        return seasonal_calendar.load_occasions(storage)
    except Exception:
        return seasonal_calendar.DEFAULT_OCCASIONS


def _canonicalizer(audience: Optional[str] = None, occasions=None,
                   warn: bool = True):
    """seasonal_calendar.canonicalizer(); a plain lowercase/strip when the
    calendar module is unavailable."""
    try:
        from . import seasonal_calendar
        return seasonal_calendar.canonicalizer(audience or None,
                                               occasions=occasions, warn=warn)
    except Exception:
        return lambda raw: (str(raw or "").strip().lower()
                            .removeprefix("holiday:") or None)


def _lane_config(cfg: dict,
                 today: Optional[_dt.date] = None) -> dict[str, dict]:
    """``format_lanes`` normalised to lane id -> {cap, days}; lanes with a
    cap below 1 are dropped. A ``boost`` whose ``until`` date is today or
    later replaces the cap; a past, missing or unparseable ``until`` leaves
    the base cap."""
    today = today or _dt.date.today()
    out: dict[str, dict] = {}
    for lid, lc in (cfg.get("format_lanes") or {}).items():
        lc = lc if isinstance(lc, dict) else {"max_per_window": lc}
        try:
            cap = int(lc.get("max_per_window", 0))
            days = max(1, int(lc.get("window_days", 7)))
        except (TypeError, ValueError):
            continue
        boost = lc.get("boost")
        if isinstance(boost, dict):
            try:
                until = _dt.date.fromisoformat(str(boost.get("until")))
                if today <= until:
                    cap = int(boost["max_per_window"])
            except (KeyError, TypeError, ValueError):
                pass
        lid = str(lid).strip().lower().removeprefix("lane:")
        if lid and cap >= 1:
            out[lid] = {"cap": cap, "days": days}
    return out


def active_holiday_ids(audience: str, windows: Iterable[str],
                       today: Optional[_dt.date] = None,
                       occasions=None) -> list[str]:
    """Calendar ids of the occasions in `windows` today. `occasions` (from
    seasonal_calendar.load_occasions(storage)) applies the storage calendar
    config; None uses the defaults."""
    try:
        from . import seasonal_calendar
    except Exception:
        return []
    wanted = set(windows or ())
    return [a.occasion.id
            for a in seasonal_calendar.active_signal(today, audience=audience,
                                                     occasions=occasions)
            if a.window in wanted]


def evaluate(*, storage, agent_id: str, site_id: str,
             dsn: Optional[str] = None,
             site_cfg: Optional[dict] = None,
             audience: Optional[str] = None,
             now: Optional[_dt.datetime] = None,
             today: Optional[_dt.date] = None,
             conn=None) -> GateDecision:
    """Decide whether this run may queue proposals, and how many."""
    occasions = _calendar(storage) if audience else None
    decision = _evaluate(storage=storage, agent_id=agent_id, site_id=site_id,
                         dsn=dsn, site_cfg=site_cfg, audience=audience,
                         now=now, today=today, conn=conn, occasions=occasions)
    decision.audience = audience or ""
    # Not a dataclass field (stays out of to_dict()): the calendar, with the
    # storage aliases, that select_for_queue() resolves proposal tags against.
    decision._occasions = occasions
    return decision


def _evaluate(*, storage, agent_id: str, site_id: str,
              dsn: Optional[str], site_cfg: Optional[dict],
              audience: Optional[str], now: Optional[_dt.datetime],
              today: Optional[_dt.date], conn, occasions) -> GateDecision:
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
        holiday_ids = active_holiday_ids(audience, so.get("windows") or (), today,
                                         occasions=occasions)
    # Aliases in DB tags / ledger entries / cap keys resolve to calendar ids.
    # Quiet: those tags are history, warned about when they were proposed.
    canon = _canonicalizer(audience, occasions, warn=False)

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
    occ_caps = occasion_caps(so, holiday_ids, today=today, canonical=canon)
    any_occ_cap = any(c is not None for c in occ_caps.values())
    occ_days = int(so.get("occasion_count_days") or 60)
    db_occ: Optional[dict[str, int]] = None
    lanes = _lane_config(cfg, today)
    db_lanes: Optional[dict[str, int]] = None
    own_conn = False
    batch: Optional[list[dict]] = None
    ai_by_slug: Optional[dict[str, int]] = None
    ac = tg.get("affiliate_clicks") or {}
    ac_on = bool(ac.get("enabled"))
    click_by_slug: Optional[dict[str, int]] = None
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
                    days=int(tg.get("lookback_days", 14)) + 1, tg=tg)
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
            if ac_on:
                try:
                    click_by_slug = affiliate_clicks_by_slug(
                        conn, ac, days=int(tg.get("lookback_days", 14)) + 1,
                        prefixes=prefixes, profile=agent_id, storage=storage)
                    sources["affiliate_clicks"] = (
                        f"ok: {sum(click_by_slug.values())} verified click(s) on "
                        f"{len(click_by_slug)} article page(s)")
                except Exception as e:
                    click_by_slug = None
                    sources["affiliate_clicks"] = f"unavailable: {type(e).__name__}: {e}"[:200]
                    try: conn.rollback()
                    except Exception: pass
            if lanes:
                try:
                    db_lanes = {}
                    for lid, lc in lanes.items():
                        db_lanes.update(read_occasion_counts(
                            conn, table=tg.get("articles_table", "editorial_articles"),
                            tags_column=str(so.get("tags_column") or "tags"),
                            holiday_ids=[lid], days=lc["days"], prefix="lane"))
                    sources["lane_counts"] = f"ok: {db_lanes}"
                except Exception as e:
                    db_lanes = None
                    sources["lane_counts"] = f"unavailable: {type(e).__name__}: {e}"[:200]
                    try: conn.rollback()
                    except Exception: pass
            if holiday_ids and any_occ_cap:
                try:
                    db_occ = read_occasion_counts(
                        conn, table=tg.get("articles_table", "editorial_articles"),
                        tags_column=str(so.get("tags_column") or "tags"),
                        holiday_ids=holiday_ids, days=occ_days, canonical=canon)
                    sources["occasion_counts"] = f"ok: {db_occ}"
                except Exception as e:
                    sources["occasion_counts"] = f"unavailable: {type(e).__name__}: {e}"[:200]
                    try: conn.rollback()
                    except Exception: pass
        else:
            sources["prior_batch"] = "unavailable: no DSN"
            sources["ai_visits"] = "unavailable: no DSN"
            if ac_on:
                sources["affiliate_clicks"] = "unavailable: no DSN"
    except Exception as e:
        sources.setdefault("prior_batch", f"unavailable: connect failed: {e}"[:200])
        sources.setdefault("ai_visits", f"unavailable: connect failed: {e}"[:200])
        if ac_on:
            sources.setdefault("affiliate_clicks", f"unavailable: connect failed: {e}"[:200])
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
    if gsc_by_slug is None and ai_by_slug is None and click_by_slug is None:
        return GateDecision(True, slots,
                            "traffic data unavailable (no fresh GSC export, "
                            "AI-visit table unreadable"
                            + (", affiliate clicks unreadable" if ac_on else "")
                            + "); cap only",
                            "cap-only", **base)

    min_impr = int(tg.get("min_impressions", 1))
    min_ai = int(tg.get("min_ai_visits", 1))
    # At least 1: "0 clicks" would make every prior article a hit.
    min_clicks = max(1, int(ac.get("min_clicks", 1) or 1))
    click_label = str(ac.get("label") or "affiliate clicks")
    if click_by_slug is not None:
        base["click_label"] = click_label
    hits = []
    for a in batch:
        impr = (gsc_by_slug or {}).get(a["slug"], 0)
        ai = (ai_by_slug or {}).get(a["slug"], 0)
        clicks = (click_by_slug or {}).get(a["slug"], 0)
        if (gsc_by_slug is not None and impr >= min_impr) or \
                (ai_by_slug is not None and ai >= min_ai) or \
                (click_by_slug is not None and clicks >= min_clicks):
            hit = {"slug": a["slug"], "impressions": impr, "ai_visits": ai}
            if click_by_slug is not None:
                hit["affiliate_clicks"] = clicks
            hits.append(hit)
    if click_by_slug is not None:
        # Click winners first (the prompt block shows the first 8); stable,
        # so ties keep newest-first.
        hits.sort(key=lambda h: (-h["affiliate_clicks"], -h["ai_visits"],
                                 -h["impressions"]))
    base["batch_hits"] = hits
    if hits:
        what = (f"impressions, AI visits or {click_label}"
                if click_by_slug is not None else "impressions or AI visits")
        return GateDecision(True, slots,
                            f"traffic gate open: {len(hits)}/{len(batch)} prior "
                            f"article(s) earned {what}",
                            "cap+traffic", **base)
    open_lanes: list[str] = []
    lane_room: dict[str, int] = {}
    if lanes:
        lane_counts = {}
        for lid, lc in lanes.items():
            led = ledger_occasion_counts(ledger, [lid], lc["days"], now,
                                         key="lane_ids").get(lid, 0)
            lane_counts[lid] = max((db_lanes or {}).get(lid, 0), led)
            if lane_counts[lid] < lc["cap"]:
                lane_room[lid] = lc["cap"] - lane_counts[lid]
        open_lanes = list(lane_room)
        base["lane_counts"] = lane_counts
    lane_slots = sum(lane_room.values())
    lane_note = (f"; format lane(s) open: "
                 + ", ".join(f"{l} {lane_counts[l]}/{lanes[l]['cap']}"
                             for l in open_lanes)) if open_lanes else ""
    if holiday_ids or open_lanes:
        open_ids, note = list(holiday_ids), ""
        holiday_room = UNLIMITED
        if holiday_ids and any_occ_cap:
            led = ledger_occasion_counts(ledger, holiday_ids, occ_days, now,
                                         canonical=canon)
            counts = {h: max((db_occ or {}).get(h, 0), led.get(h, 0))
                      for h in holiday_ids}
            base["occasion_counts"] = counts
            open_ids = [h for h in holiday_ids
                        if occ_caps[h] is None or counts[h] < occ_caps[h]]
            full = [f"{h} {counts[h]}/{occ_caps[h]}" for h in holiday_ids
                    if occ_caps[h] is not None and counts[h] >= occ_caps[h]]
            if full:
                note = f"; per-occasion cap reached for {', '.join(full)}"
            holiday_room = (UNLIMITED if any(occ_caps[h] is None for h in open_ids)
                            else sum(occ_caps[h] - counts[h] for h in open_ids))
        if not open_ids:
            holiday_room = 0
        if open_ids or open_lanes:
            base["require_holiday_ids"] = open_ids
            base["lane_ids"] = open_lanes
            base["lane_room"] = lane_room
            slots = min(slots, holiday_room + lane_slots)
            if open_ids:
                return GateDecision(True, slots,
                                    f"traffic gate closed (0/{len(batch)} prior articles "
                                    "earned a signal) — seasonal override for "
                                    f"{', '.join(open_ids)} only{note}{lane_note}",
                                    "seasonal-override", **base)
            return GateDecision(True, slots,
                                f"traffic gate closed (0/{len(batch)} prior articles "
                                "earned a signal) — format lanes only"
                                f"{note}{lane_note}",
                                "format-lane", **base)
        return GateDecision(False, 0,
                            f"traffic gate closed: 0/{len(batch)} prior articles "
                            "earned a signal, and the seasonal override is spent"
                            f"{note} (seasonal_override.max_per_occasion)",
                            "cap+traffic", **base)
    return GateDecision(False, 0,
                        f"traffic gate closed: 0/{len(batch)} articles published "
                        f"{tg.get('min_age_days')}-{tg.get('lookback_days')}d ago "
                        + (f"earned a GSC impression, an AI/search visit or "
                           f"{min_clicks}+ verified {click_label}"
                           if click_by_slug is not None else
                           "earned a GSC impression or an AI/search visit"),
                        "cap+traffic", **base)


# ---------------------------------------------------------------------------
# Post-LLM selection
# ---------------------------------------------------------------------------

def _raw_proposal_holiday_ids(p: dict) -> set[str]:
    """Holiday ids as written: ``holiday:<id>`` tags plus the ``holiday`` /
    ``occasion_id`` / ``holiday_id`` keys."""
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


def _canonical_holiday_ids(p: dict, canon) -> set[str]:
    return {c for c in (canon(r) for r in _raw_proposal_holiday_ids(p)) if c}


def proposal_holiday_ids(p: dict, *, audience: Optional[str] = None,
                         occasions=None) -> set[str]:
    """Calendar ids of the occasions a proposal targets. Aliases resolve
    (``holiday:black-friday-cyber-monday`` -> ``black-friday``, see
    seasonal_calendar.canonicalizer); ids the calendar does not know are
    dropped with a warning. `audience` limits the candidate occasions as
    active_signal() does; `occasions` applies a storage calendar."""
    return _canonical_holiday_ids(p, _canonicalizer(audience, occasions))


def proposal_lane_ids(p: dict) -> set[str]:
    out: set[str] = set()
    for t in p.get("tags") or []:
        t = str(t).strip().lower()
        if t.startswith("lane:"):
            out.add(t.split(":", 1)[1])
    v = p.get("lane")
    if v:
        out.add(str(v).strip().lower().removeprefix("lane:"))
    return out


def lane_ids_for(proposals: Iterable[dict]) -> list[str]:
    """One lane id per proposal that carries one — the ``lane_ids``
    value for :func:`record_batch`."""
    out = []
    for p in proposals or ():
        ids = sorted(proposal_lane_ids(p))
        if ids:
            out.append(ids[0])
    return out


def holiday_ids_for(proposals: Iterable[dict], *,
                    audience: Optional[str] = None,
                    occasions=None) -> list[str]:
    """One calendar id per proposal that carries a known one (the first
    when a proposal has several) — the ``holiday_ids`` value for
    :func:`record_batch`. Aliases resolve; unknown ids are left out, as
    they could never count toward an occasion's cap."""
    canon = _canonicalizer(audience, occasions)
    out = []
    for p in proposals or ():
        ids = sorted(_canonical_holiday_ids(p, canon))
        if ids:
            out.append(ids[0])
    return out


def select_for_queue(proposals: Sequence[dict], decision: GateDecision
                     ) -> tuple[list[dict], list[dict]]:
    """Order + trim proposals to the gate's slots. Returns (kept, dropped).

    Proposals tagged for a NOW/IMMINENT occasion move to the front, keeping
    LLM order otherwise. Under a seasonal override or format lane only
    tagged proposals are eligible: holiday-tagged first, then lane-tagged
    up to each lane's remaining room. Holiday tags are matched by calendar
    id, so an alias such as ``holiday:black-friday-cyber-monday`` qualifies
    for ``black-friday``."""
    if not decision.allowed:
        return [], list(proposals or [])
    props = list(proposals or [])
    want = set(decision.require_holiday_ids or decision.priority_holiday_ids or [])
    restricted = bool(decision.require_holiday_ids or decision.lane_ids)
    canon = _canonicalizer(getattr(decision, "audience", "") or None,
                           getattr(decision, "_occasions", None))
    hol = {id(p): _canonical_holiday_ids(p, canon) for p in props} if want else {}
    if restricted:
        tagged = [p for p in props if hol.get(id(p), set()) & want]
        room = dict(decision.lane_room or {})
        laned = []
        for p in props:
            if hol.get(id(p), set()) & want:
                continue
            hit = sorted(l for l in proposal_lane_ids(p) if room.get(l, 0) > 0)
            if hit:
                room[hit[0]] -= 1
                laned.append(p)
        props = tagged + laned
    elif want:
        tagged = [p for p in props if hol[id(p)] & want]
        rest = [p for p in props if not (hol[id(p)] & want)]
        props = tagged + rest
    kept = props[:decision.slots]
    kept_ids = {id(p) for p in kept}
    dropped = [p for p in proposals or [] if id(p) not in kept_ids]
    return kept, dropped
