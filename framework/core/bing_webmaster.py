"""Bing Webmaster Tools API client — the index behind ChatGPT search.

Bing's index feeds ChatGPT search, Copilot, DuckDuckGo, Yahoo and Ecosia;
for aisleprompt ~96% of human search arrivals come through it (GA4 30d,
2026-09-25). The fleet pushed URLs into it (IndexNow) but read nothing back:
how many pages Bing holds, what it fails to crawl, what it sends. This is
the one reader, lifted from aisleprompt's user-growth-strategist so every
site and agent shares it.

Auth: BING_WEBMASTER_API_KEY (Bing Webmaster Tools → Settings → API access;
one key covers every site verified under that account). Quotes around the
value are tolerated (secrets.env single-quotes values). Without a key every
call returns {"available": False, "error": ...} — never raises — so a
collector can call it unconditionally.

Per-site config: site.yaml `data_sources.bing.site_url` (the property URL as
registered in Bing, e.g. "https://example.com/"). No site names in code.

    from framework.core import bing_webmaster as bwt
    raw = bwt.collect("https://example.com/")
    raw["metrics"]  # {"in_index": ..., "clicks_28d": ..., "crawl_errors_1d": ...}
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

API_BASE = "https://ssl.bing.com/webmaster/api.svc/json"
_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def api_key() -> str:
    """The API key from the environment, with shell-style quotes stripped."""
    k = (os.environ.get("BING_WEBMASTER_API_KEY") or "").strip()
    if len(k) >= 2 and k[0] == k[-1] and k[0] in "'\"":
        k = k[1:-1].strip()
    return k


def _base() -> str:
    return (os.environ.get("BING_WEBMASTER_API_BASE") or API_BASE).rstrip("/")


def parse_date(v: Any) -> Optional[datetime]:
    """Bing's JSON dates look like "/Date(1695081600000-0700)/"."""
    if isinstance(v, str):
        m = _DATE_RE.search(v)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def call(method: str, site_url: str, *, key: Optional[str] = None,
         params: Optional[dict] = None, timeout: float = 60.0) -> Any:
    """One GET against the JSON endpoint. Returns the `d` payload, or
    {"error": "..."} on any failure (never raises)."""
    k = key if key is not None else api_key()
    if not k:
        return {"error": "BING_WEBMASTER_API_KEY not set"}
    q = {"siteUrl": site_url, "apikey": k, **(params or {})}
    url = f"{_base()}/{method}?{urllib.parse.urlencode(q)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return {"error": f"HTTP {e.code} {detail}".strip()}
    except Exception as e:
        # Never echo the URL: it carries the API key.
        return {"error": f"{type(e).__name__}: {str(e)[:160]}".replace(k, "***")}
    return body.get("d") if isinstance(body, dict) and "d" in body else body


def _rows(v: Any) -> list[dict]:
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def summarize(raw: dict, *, now: Optional[datetime] = None) -> dict[str, float]:
    """Flat numeric metrics from a `collect()` payload (goal-bindable)."""
    m: dict[str, float] = {}
    crawl = sorted(_rows(raw.get("crawl_stats")),
                   key=lambda r: parse_date(r.get("Date")) or datetime.min.replace(tzinfo=timezone.utc))
    if crawl:
        last = crawl[-1]
        for src, dst in (("InIndex", "in_index"), ("CrawledPages", "crawled_pages_1d"),
                         ("CrawlErrors", "crawl_errors_1d"), ("Code4xx", "code_4xx_1d"),
                         ("Code5xx", "code_5xx_1d"), ("BlockedByRobotsTxt", "blocked_by_robots_1d"),
                         ("InLinks", "in_links")):
            if isinstance(last.get(src), (int, float)):
                m[dst] = float(last[src])
    traffic = _rows(raw.get("rank_and_traffic"))
    dated = [(parse_date(r.get("Date")), r) for r in traffic]
    dated = [(d, r) for d, r in dated if d]
    if dated:
        latest = max(d for d, _ in dated)
        window = [r for d, r in dated if (latest - d).days < 28]
        m["clicks_28d"] = float(sum(r.get("Clicks") or 0 for r in window))
        m["impressions_28d"] = float(sum(r.get("Impressions") or 0 for r in window))
    if isinstance(raw.get("crawl_issues"), list):
        m["crawl_issues"] = float(len(raw["crawl_issues"]))
    quota = raw.get("url_submission_quota")
    if isinstance(quota, dict) and isinstance(quota.get("DailyQuota"), (int, float)):
        m["url_submission_quota_daily"] = float(quota["DailyQuota"])
    feeds = _rows(raw.get("sitemaps"))
    if feeds:
        m["sitemaps"] = float(len(feeds))
        m["sitemap_urls"] = float(sum(f.get("UrlCount") or 0 for f in feeds))
    return m


def collect(site_url: str, *, key: Optional[str] = None, timeout: float = 60.0,
            top_queries: int = 75, top_pages: int = 50) -> dict:
    """Everything the fleet reads from BWT for one property. Always returns a
    dict: {"available": bool, "site": ..., <sections>..., "metrics": {...}}."""
    k = key if key is not None else api_key()
    out: dict[str, Any] = {"site": site_url, "available": bool(k)}
    if not k:
        out["error"] = ("BING_WEBMASTER_API_KEY not set — generate it in Bing Webmaster Tools "
                        "(Settings → API access) and add it to ~/.reusable-agents/secrets.env")
        out["metrics"] = {}
        return out

    def top(v, n):
        rows = _rows(v)
        if rows and "Impressions" in rows[0]:
            rows = sorted(rows, key=lambda x: x.get("Impressions", 0), reverse=True)
        return rows[:n] if rows or isinstance(v, list) else v

    out["crawl_stats"] = call("GetCrawlStats", site_url, key=k, timeout=timeout)
    out["rank_and_traffic"] = call("GetRankAndTrafficStats", site_url, key=k, timeout=timeout)
    out["top_queries"] = top(call("GetQueryStats", site_url, key=k, timeout=timeout), top_queries)
    out["top_pages"] = top(call("GetPageStats", site_url, key=k, timeout=timeout), top_pages)
    out["sitemaps"] = call("GetFeeds", site_url, key=k, timeout=timeout)
    out["crawl_issues"] = call("GetCrawlIssues", site_url, key=k, timeout=timeout)
    out["url_submission_quota"] = call("GetUrlSubmissionQuota", site_url, key=k, timeout=timeout)
    errors = {s: v["error"] for s, v in out.items() if isinstance(v, dict) and "error" in v}
    if errors:
        out["errors"] = errors
        if len(errors) >= 7:          # every call failed (bad key / unverified site)
            out["available"] = False
    out["metrics"] = summarize(out)
    return out


def url_info(site_url: str, url: str, *, key: Optional[str] = None, timeout: float = 60.0) -> Any:
    """Bing's view of one URL (HTTP status seen, last crawl, discovery)."""
    return call("GetUrlInfo", site_url, key=key, params={"url": url}, timeout=timeout)
