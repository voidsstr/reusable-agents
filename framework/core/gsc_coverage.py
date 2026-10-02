"""Search Console URL Inspection coverage: one definition of "indexed", the
sampler that picks which URLs to inspect, and per-section rollups.

Why (2026-10-02): one site's auditor reported indexed=0.03% of 5,904 URLs.
The figure was true of what it measured, but it measured the wrong thing.
Never-inspected URLs all tie at the empty watermark, so they were taken in
alphabetical order: 150 a day walked /blog, then /kitchen, and would have
reached /recipes (140k URLs, the site's core content) years later. A URL
inspected once came round again only after every never-inspected URL, so
the blog's first snapshot would never refresh. And only "Submitted and
indexed" counted as indexed, so a page Google indexed outside the sitemap
read as missing -- the state every URL lands in when a sitemap is pruned.

The sampler gives every section of a site an equal share of each run, and
the rollup reports indexed % per section, so "is Google indexing the pages
we care about?" has an answer per section instead of one blended number.
A site can name extra sections (e.g. a curated sitemap) in its config.

Stdlib only, and nothing that imports `inspect`: the auditor's own script
is named inspect.py and imports this module.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

# Google's coverageState strings for a URL that is in the index.
INDEXED_STATES = frozenset({
    "Submitted and indexed",
    "Indexed, not submitted in sitemap",
})
UNKNOWN_STATE = "URL is unknown to Google"
CRAWLED_NOT_INDEXED_STATES = frozenset({
    "Crawled - currently not indexed",
    "Crawled — currently not indexed",
})


def is_indexed(coverage_state: str | None, verdict: str | None = None) -> bool:
    """True when Google reports the URL as indexed. verdict PASS is the API's
    own "valid" flag; the state strings cover rows recorded without it."""
    return verdict == "PASS" or (coverage_state or "") in INDEXED_STATES


def section_of(url: str, section_urls: Mapping[str, str] | None = None) -> str:
    """The section a URL is sampled and reported in: the label the site
    config assigns it, else its first path segment ('/recipes/x-1' ->
    'recipes', '/' -> 'root')."""
    if section_urls:
        label = section_urls.get(url)
        if label:
            return label
    seg = (urlsplit(url).path or "/").strip("/").split("/", 1)[0]
    return seg or "root"


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pick_targets(
    universe: Iterable[str],
    last_inspected: Mapping[str, str],
    limit: int,
    *,
    now: datetime | None = None,
    min_age_days: float = 14.0,
    section_urls: Mapping[str, str] | None = None,
) -> list[str]:
    """Choose up to `limit` URLs to inspect this run.

    Sections take turns, one URL each per round, so a 140k-URL catalog and a
    500-post blog are both sampled every run. Within a section, URLs never
    inspected come first in a stable hash order (a fair sample, where
    alphabetical order walked one slug prefix at a time), then the ones
    unchecked longest. A URL inspected within `min_age_days` sits out, so a
    one-page section does not spend quota every run.
    """
    if limit <= 0:
        return []
    cutoff = _utc_stamp((now or datetime.now(timezone.utc)) - timedelta(days=min_age_days))
    buckets: dict[str, list[tuple[str, str, str]]] = {}
    for url in universe:
        last = last_inspected.get(url) or ""
        if last and last > cutoff:
            continue
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        buckets.setdefault(section_of(url, section_urls), []).append((last, key, url))
    queues = [sorted(buckets[name]) for name in sorted(buckets)]
    out: list[str] = []
    depth = 0
    while len(out) < limit:
        took = False
        for q in queues:
            if depth < len(q):
                out.append(q[depth][2])
                took = True
                if len(out) >= limit:
                    break
        if not took:
            break
        depth += 1
    return out


def latest_by_url(lines: Iterable[str]) -> dict[str, dict]:
    """Newest inspection per URL from coverage JSONL lines:
    {url: {"state", "verdict", "inspected_at", "section"}}."""
    latest: dict[str, dict] = {}
    for raw in lines:
        try:
            row = json.loads(raw)
        except Exception:
            continue
        url = row.get("url")
        ts = row.get("inspected_at") or ""
        if not url:
            continue
        prev = latest.get(url)
        if prev is None or ts > prev["inspected_at"]:
            latest[url] = {
                "state": row.get("coverageState") or "",
                "verdict": row.get("verdict"),
                "inspected_at": ts,
                "section": row.get("section") or "",
            }
    return latest


def section_summary(latest: Mapping[str, Mapping]) -> dict[str, dict[str, int]]:
    """{section: {"inspected", "indexed", "unknown", "crawled_not_indexed"}}.
    A row's recorded section (set at inspection time from the site config)
    wins over the URL's path segment."""
    out: dict[str, dict[str, int]] = {}
    for url, row in latest.items():
        name = row.get("section") or section_of(url)
        s = out.setdefault(name, {"inspected": 0, "indexed": 0, "unknown": 0, "crawled_not_indexed": 0})
        s["inspected"] += 1
        state = row.get("state") or ""
        if is_indexed(state, row.get("verdict")):
            s["indexed"] += 1
        elif state == UNKNOWN_STATE:
            s["unknown"] += 1
        elif state in CRAWLED_NOT_INDEXED_STATES:
            s["crawled_not_indexed"] += 1
    return out


def metric_key(section: str) -> str:
    """Metric-safe section name: 'recipes-priority' -> 'recipes_priority'."""
    return "".join(c if c.isalnum() else "_" for c in section.lower()).strip("_") or "root"
