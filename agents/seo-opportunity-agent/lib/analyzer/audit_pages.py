"""Which pages the LLM audit looks at — and when it should stop looking.

WHY THIS EXISTS
---------------
Without a page-type inventory (pages-by-type.jsonl), the LLM audit fell back
to a 20-page BFS crawl seeded with the homepage plus the top-10 GSC pages by
clicks. On a site whose search traffic is ~zero, "top-10 by clicks" is a list
of 1-click pages, and the rest of the slots went to /about, /privacy and
/contact from the homepage's links. Every run re-audited the same handful of
product pages under a Google-rank objective and emitted a dozen recs, so one
site's SEO agent spent ~90 commits in three weeks hand-tuning 9 PDPs that
had 0 AI-assistant referrals — while the pages assistants actually send
people to (articles, other PDPs) were never audited.

This module holds the pure, testable parts of the fix. The analyzer wires
them in; nothing here touches the network or the DB.

  seed_rows(...)           pages named by `analyzer.audit_seed_queries`
                           (db-stats.json blocks whose rows carry `path`),
                           e.g. the collector's `ai_landed_pages`.
  prioritize(...)          put seed pages first and annotate them, for runs
                           whose pages come from an inventory file.
  annotate(...)            copy AI-landing counts onto page records so the
                           prompt shows why a page matters.
  url_rec_counts(...)      LLM-audit recs per URL over prior runs.
  cooldown_filter(...)     drop pages / recs for URLs that already had their
                           share of recs in the window, unless exempt.
  slow_ai_landed_rec(...)  one live-state rec when AI-landed pages are slow.

CONFIG (site.yaml `analyzer:` block — schema in site-config.schema.json)
  audit_seed_queries:       [ai_landed_pages, ...]
  audit_url_cooldown:       {window_days: 30, max_recs_per_url: 2,
                             max_recs_per_exempt_url: 6,
                             exempt_queries: [ai_landed_pages, ...]}
  ai_landed_ttfb_budget_ms: 3000     (0 disables the latency rec)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

DEFAULT_COOLDOWN = {
    "window_days": 30,
    "max_recs_per_url": 2,
    "max_recs_per_exempt_url": 6,
    "exempt_queries": [],
}
DEFAULT_TTFB_BUDGET_MS = 3000


def _host(netloc: str) -> str:
    h = (netloc or "").lower().split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def norm_url(url: str) -> str:
    """Comparable form: no query/fragment, no trailing slash (root keeps '/'),
    host lowercased without www."""
    p = urlparse(str(url or ""))
    if not p.scheme or not p.netloc:
        return ""
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return f"{p.scheme.lower()}://{_host(p.netloc)}{path}"


def abs_url(base_url: str, path_or_url: str) -> str:
    """Absolute URL on base_url's host for a path or a same-site URL; ""
    for another host or an empty value."""
    v = str(path_or_url or "").strip()
    if not v:
        return ""
    base = urlparse(base_url)
    if v.startswith(("http://", "https://")):
        p = urlparse(v)
        if _host(p.netloc) != _host(base.netloc):
            return ""
        v = p.path or "/"
    if not v.startswith("/"):
        v = "/" + v
    v = v.split("#", 1)[0].split("?", 1)[0]
    if len(v) > 1:
        v = v.rstrip("/") or "/"
    return f"{base.scheme}://{base.netloc}{v}"


def seed_rows(db_stats: Mapping, query_names: Iterable[str], base_url: str,
              *, limit: int = 0) -> list[dict]:
    """Rows from the named db-stats.json blocks as
    ``[{url, referrals, fetches, seed_query}]``, in query then row order,
    de-duplicated by URL. A block may be a list of rows or one row (dict).
    Rows need a `path` (or `url`) on this site; others are skipped."""
    out: list[dict] = []
    seen: set[str] = set()
    for name in query_names or []:
        rows = (db_stats or {}).get(name)
        if isinstance(rows, dict):
            rows = [rows]
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            url = abs_url(base_url, r.get("path") or r.get("url") or "")
            key = norm_url(url)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "url": url,
                "referrals": _int(r.get("referrals")),
                "fetches": _int(r.get("fetches")),
                "seed_query": name,
            })
            if limit and len(out) >= limit:
                return out
    return out


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def annotate(page: dict, seeds_by_url: Mapping[str, dict]) -> dict:
    """Copy AI-landing counts onto a page record (by URL, or by the URL the
    crawler was redirected from). Returns the page."""
    s = (seeds_by_url.get(norm_url(page.get("url") or ""))
         or seeds_by_url.get(norm_url(page.get("redirected_from") or "")))
    if s:
        page["audit_seed"] = s.get("seed_query") or True
        if s.get("referrals"):
            page["ai_referrals"] = s["referrals"]
        if s.get("fetches"):
            page["ai_live_fetches"] = s["fetches"]
    return page


def seeds_index(seeds: Iterable[dict]) -> dict[str, dict]:
    return {norm_url(s["url"]): s for s in seeds or [] if s.get("url")}


def prioritize(pages: list[dict], seeds: list[dict]) -> list[dict]:
    """Seed pages first (in seed order), then the rest in their original
    order. Annotates seed pages. Stable, never drops a page."""
    idx = seeds_index(seeds)
    order = {k: i for i, k in enumerate(idx)}
    for p in pages:
        annotate(p, idx)

    def key(p: dict) -> tuple[int, int]:
        k = norm_url(p.get("url") or "")
        if k in order:
            return (0, order[k])
        k2 = norm_url(p.get("redirected_from") or "")
        if k2 in order:
            return (0, order[k2])
        return (1, 0)

    return sorted(pages, key=key)


# ---------------------------------------------------------------------------
# Per-URL cooldown
# ---------------------------------------------------------------------------

def run_ts_datetime(run_ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(run_ts)[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def url_rec_counts(runs: Iterable[tuple[str, Mapping]], *, now: datetime,
                   window_days: int) -> dict[str, int]:
    """LLM-audit recs per normalized URL across ``(run_ts, recommendations
    doc)`` pairs inside the window. Only recs carrying `llm_check_id` count:
    rule-pass recs (coverage, GEO, top-5) have their own dedupe."""
    cutoff = now - timedelta(days=max(1, int(window_days)))
    counts: dict[str, int] = {}
    for run_ts, doc in runs:
        dt = run_ts_datetime(run_ts)
        if dt is None or dt < cutoff or not isinstance(doc, Mapping):
            continue
        for r in doc.get("recommendations") or []:
            if not isinstance(r, Mapping) or not r.get("llm_check_id"):
                continue
            refs = r.get("data_refs") or []
            key = norm_url(refs[0] if refs else r.get("url") or "")
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def cooldown_config(analyzer_cfg: Mapping) -> dict:
    c = dict(DEFAULT_COOLDOWN)
    c.update((analyzer_cfg or {}).get("audit_url_cooldown") or {})
    return c


def exempt_urls(db_stats: Mapping, query_names: Iterable[str],
                base_url: str) -> set[str]:
    """Normalized URLs of the rows in the exempt db-stats blocks."""
    return {norm_url(r["url"]) for r in seed_rows(db_stats, query_names, base_url)}


def _cap(url_key: str, cfg: Mapping, exempt: set[str]) -> int:
    if url_key in exempt:
        return int(cfg.get("max_recs_per_exempt_url") or 0)
    return int(cfg.get("max_recs_per_url") or 0)


def cooldown_filter_pages(pages: list[dict], counts: Mapping[str, int],
                          cfg: Mapping, exempt: set[str]) -> tuple[list[dict], list[str]]:
    """Drop pages whose URL already reached its rec cap in the window.
    A cap of 0 means unlimited. Returns (kept, dropped_urls)."""
    kept, dropped = [], []
    for p in pages:
        k = norm_url(p.get("url") or "")
        cap = _cap(k, cfg, exempt)
        if cap > 0 and counts.get(k, 0) >= cap:
            dropped.append(p.get("url") or "")
            continue
        kept.append(p)
    return kept, dropped


def cooldown_filter_recs(recs: list[dict], counts: Mapping[str, int],
                         cfg: Mapping, exempt: set[str]) -> tuple[list[dict], int]:
    """Keep at most (cap - prior count) LLM recs per URL in this run, in
    order. Recs without a URL pass. Returns (kept, n_dropped)."""
    used: dict[str, int] = {}
    kept: list[dict] = []
    dropped = 0
    for r in recs:
        refs = r.get("data_refs") or []
        k = norm_url(refs[0] if refs else "")
        cap = _cap(k, cfg, exempt) if k else 0
        if k and cap > 0:
            if counts.get(k, 0) + used.get(k, 0) >= cap:
                dropped += 1
                continue
            used[k] = used.get(k, 0) + 1
        kept.append(r)
    return kept, dropped


# ---------------------------------------------------------------------------
# Latency on AI-landed pages
# ---------------------------------------------------------------------------

def slow_ai_landed_rec(pages: Iterable[dict], *, budget_ms: int,
                       rec_id: str, data_ref: str = "data/pages.jsonl") -> Optional[dict]:
    """One `cwv-ttfb-slow` rec (a live-state type: re-measured every crawl,
    never deduped) listing AI-landed pages whose time to first byte exceeded
    `budget_ms`, slowest first. None when there are none or budget_ms <= 0.

    Assistants fetch a page while the user waits (ChatGPT-User,
    Perplexity-User) and give up after a few seconds; a slow page is a page
    they cannot quote or link."""
    if not budget_ms or budget_ms <= 0:
        return None
    slow = []
    for p in pages or []:
        if not p.get("audit_seed"):
            continue
        if not (p.get("ai_referrals") or p.get("ai_live_fetches")):
            continue
        ms = _int(p.get("ttfb_ms")) or _int(p.get("fetch_ms"))
        if ms > budget_ms:
            slow.append((ms, p))
    if not slow:
        return None
    slow.sort(key=lambda t: -t[0])
    sample = ", ".join(
        f"{p.get('url')} ({ms / 1000:.1f}s; {p.get('ai_referrals', 0)} AI referrals, "
        f"{p.get('ai_live_fetches', 0)} assistant fetches)"
        for ms, p in slow[:5])
    return {
        "id": rec_id,
        "type": "cwv-ttfb-slow",
        "priority": "high",
        "title": (f"{len(slow)} AI-landed page(s) take over {budget_ms / 1000:.0f}s "
                  f"to first byte"),
        "rationale": (
            "These pages are the ones AI assistants send people to or fetch at "
            "answer time, and the audit crawl measured each one's server "
            f"response above the {budget_ms}ms budget. An assistant that "
            "fetches live gives up after a few seconds, so a slow page is one "
            "it cannot quote or link. Slowest: " + sample
        ),
        "expected_impact": {"metric": "ai_referral_landings_30d", "horizon_weeks": 2},
        "data_refs": [data_ref],
        "sample_urls": [p.get("url") for _, p in slow[:10]],
        "implementation_outline": {
            "approach": (
                "Profile the server render of each listed route. Serve it from "
                "a cache or precomputed data, put a hard per-query timeout on "
                "optional modules so a slow rail degrades that module instead "
                "of the page, and never add a per-request catalog scan. A "
                "failed optional module must not turn the page into an empty "
                "or noindex response."
            ),
        },
        "implemented": False,
    }
