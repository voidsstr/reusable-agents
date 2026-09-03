"""Lightweight BFS crawler for progressive-improvement-agent.

Fetches HTML, extracts title/description/main-text + outgoing same-origin
links, respects max_depth + max_pages + path_excludes. Optional sitemap
discovery for seed-URL expansion.

JS-rendered SPA support is opt-in via crawler.render_js: true (requires
playwright). Default is plain requests + BeautifulSoup, which is fine for
SSR'd sites like AislePrompt and SpecPicks.
"""
from __future__ import annotations

import fnmatch
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator, Optional

import requests
from bs4 import BeautifulSoup


@dataclass
class Page:
    url: str
    status_code: int
    fetch_ms: int
    title: str = ""
    description: str = ""
    canonical: str = ""
    h1: str = ""
    body_text: str = ""           # cleaned visible text, capped
    body_hash: str = ""
    links: list[str] = field(default_factory=list)
    error: str = ""
    depth: int = 0
    content_type: str = ""
    # 2026-05-31: surface signals that lived only in <script>/<head> so
    # downstream LLM-fed extractors (competitor-research, PI analyzers)
    # don't conclude a page lacks schema/OG/etc when it has them — the
    # exact bug behind the "SpecPicks emits no JSON-LD" hallucination
    # the competitor-research-agent produced.
    jsonld_types: list[str] = field(default_factory=list)   # e.g. ['Product','Offer','FAQPage']
    jsonld_count: int = 0                                   # number of <script type="application/ld+json"> blocks
    og_type: str = ""                                       # og:type meta (article/website/product/etc)
    robots_meta: str = ""                                   # <meta name="robots"> directives
    twitter_card: str = ""                                  # twitter:card meta value
    # 2026-09-02: how many fetches it took to produce this Page. >1 means an
    # earlier attempt hit a transport error or a 5xx and we retried. Callers
    # that turn failures into "broken page" findings must report this — a
    # single dropped read is not a site defect.
    attempts: int = 1

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        # Cap body_text to keep pages.jsonl reasonable
        if len(d["body_text"]) > 8000:
            d["body_text"] = d["body_text"][:8000] + " […]"
        return d


def _normalize_url(base: str, href: str) -> Optional[str]:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    abs_url = urllib.parse.urljoin(base, href)
    parsed = urllib.parse.urlparse(abs_url)
    if parsed.scheme not in ("http", "https"):
        return None
    # Drop fragment
    return urllib.parse.urlunparse(parsed._replace(fragment=""))


def _same_origin(a: str, b: str) -> bool:
    pa = urllib.parse.urlparse(a)
    pb = urllib.parse.urlparse(b)
    return pa.netloc == pb.netloc


def _path_excluded(url: str, excludes: list[str]) -> bool:
    path = urllib.parse.urlparse(url).path
    return any(fnmatch.fnmatch(path, pat) for pat in excludes)


def _extract(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")[:300]
    desc = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        desc = md["content"].strip()[:500]
    canon = ""
    cl = soup.find("link", attrs={"rel": "canonical"})
    if cl and cl.get("href"):
        canon = cl["href"].strip()
    h1 = ""
    h1el = soup.find("h1")
    if h1el:
        h1 = h1el.get_text(" ", strip=True)[:300]

    # 2026-05-31: capture JSON-LD + other head signals BEFORE the script-
    # stripping below decomposes them. Previously the agent was blind to
    # all <script type="application/ld+json"> blocks (so the LLM
    # confidently recommended "add JSON-LD" on a site already emitting it).
    jsonld_types: list[str] = []
    jsonld_count = 0
    try:
        import json as _json
        for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
            txt = sc.string or sc.get_text() or ""
            if not txt.strip():
                continue
            jsonld_count += 1
            try:
                parsed = _json.loads(txt)
            except Exception:
                continue
            # @graph wrapper unrolls into its node list; otherwise a single
            # object or an array of objects. Collect every @type.
            def _collect(node):
                if isinstance(node, dict):
                    g = node.get("@graph")
                    if isinstance(g, list):
                        for x in g:
                            _collect(x)
                    t = node.get("@type")
                    if isinstance(t, str):
                        jsonld_types.append(t)
                    elif isinstance(t, list):
                        for tt in t:
                            if isinstance(tt, str):
                                jsonld_types.append(tt)
                    for v in node.values():
                        if isinstance(v, (dict, list)):
                            _collect(v)
                elif isinstance(node, list):
                    for x in node:
                        _collect(x)
            _collect(parsed)
    except Exception:
        pass
    # Dedup while preserving discovery order
    jsonld_types = list(dict.fromkeys(jsonld_types))[:40]

    # Other head signals worth surfacing
    og_type = ""
    ogt = soup.find("meta", attrs={"property": "og:type"})
    if ogt and ogt.get("content"):
        og_type = ogt["content"].strip()[:60]
    robots_meta = ""
    rm = soup.find("meta", attrs={"name": "robots"})
    if rm and rm.get("content"):
        robots_meta = rm["content"].strip()[:120]
    twitter_card = ""
    tc = soup.find("meta", attrs={"name": "twitter:card"})
    if tc and tc.get("content"):
        twitter_card = tc["content"].strip()[:60]

    # Strip nav/footer/script/style for cleaner body
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    body_text = " ".join(soup.get_text(" ", strip=True).split())
    links = []
    for a in soup.find_all("a", href=True):
        n = _normalize_url(url, a["href"])
        if n:
            links.append(n)
    return {
        "title": title, "description": desc, "canonical": canon, "h1": h1,
        "body_text": body_text, "links": list(dict.fromkeys(links)),
        "jsonld_types": jsonld_types, "jsonld_count": jsonld_count,
        "og_type": og_type, "robots_meta": robots_meta,
        "twitter_card": twitter_card,
    }


def _fetch_sitemap_urls(base_url: str, ua: str, timeout: int) -> list[str]:
    """Try /sitemap.xml. Return list of <loc> URLs (flat — sitemap-index URLs
    are also fetched recursively, capped at 200 entries)."""
    out: list[str] = []
    sitemap_url = urllib.parse.urljoin(base_url, "/sitemap.xml")
    try:
        r = requests.get(sitemap_url, headers={"User-Agent": ua}, timeout=timeout)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        # sitemap index?
        for sm in root.findall("sm:sitemap", ns):
            loc = sm.find("sm:loc", ns)
            if loc is not None and loc.text:
                try:
                    rr = requests.get(loc.text, headers={"User-Agent": ua}, timeout=timeout)
                    inner = ET.fromstring(rr.text)
                    for u in inner.findall("sm:url", ns):
                        l = u.find("sm:loc", ns)
                        if l is not None and l.text:
                            out.append(l.text.strip())
                            if len(out) >= 200:
                                return out
                except Exception:
                    pass
        # urlset?
        for u in root.findall("sm:url", ns):
            l = u.find("sm:loc", ns)
            if l is not None and l.text:
                out.append(l.text.strip())
                if len(out) >= 200:
                    return out
    except Exception:
        return []
    return out


def _fetch_with_retry(
    url: str, *, user_agent: str, timeout_s: int, retries: int, backoff_s: float,
) -> tuple[Optional[requests.Response], str, int, int]:
    """Fetch `url`, retrying transport errors and 5xx responses.

    Returns `(response | None, error_text, elapsed_ms, attempts)`. A 4xx is a
    real answer from the origin and is never retried; a read timeout, a reset
    connection, or a 502 from a replica that was cold when we knocked is not an
    answer at all, and gets one more chance before we call the page broken.
    """
    t0 = time.monotonic()
    total = max(0, int(retries)) + 1
    attempts = 0
    last_err = ""
    for i in range(total):
        attempts += 1
        last = i == total - 1
        try:
            r = requests.get(
                url, headers={"User-Agent": user_agent},
                timeout=timeout_s, allow_redirects=True,
            )
            if r.status_code < 500 or last:
                return r, "", int((time.monotonic() - t0) * 1000), attempts
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)[:240]
            if last:
                break
        time.sleep(max(0.0, backoff_s))
    err = last_err if attempts == 1 else f"{last_err} (after {attempts} attempts)"
    return None, err, int((time.monotonic() - t0) * 1000), attempts


def crawl(
    *,
    base_url: str,
    seed_urls: list[str],
    use_sitemap: bool = True,
    max_depth: int = 2,
    max_pages: int = 30,
    path_excludes: Optional[list[str]] = None,
    request_timeout_s: int = 15,
    user_agent: str = "reusable-agents-quality-crawler/1.0",
    throttle_ms: int = 500,
    retry_on_error: int = 1,
    retry_backoff_s: float = 1.5,
) -> Iterator[Page]:
    """BFS crawl. Yields Page objects in fetch order. Caller is responsible for
    persisting results.

    `retry_on_error` re-fetches a URL that raised a transport error (read
    timeout, connection reset, DNS blip) or answered 5xx, waiting
    `retry_backoff_s` between tries. Only the last attempt's outcome is
    yielded, with `Page.attempts` recording how many it took. Set to 0 to
    restore single-shot behaviour."""
    excludes = path_excludes or []
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()

    # Expand relative seeds against base_url
    for s in (seed_urls or ["/"]):
        if s.startswith(("http://", "https://")):
            queue.append((s, 0))
        else:
            queue.append((urllib.parse.urljoin(base_url, s), 0))

    if use_sitemap:
        for u in _fetch_sitemap_urls(base_url, user_agent, request_timeout_s)[:50]:
            if u not in seen:
                queue.append((u, 0))

    fetched = 0
    while queue and fetched < max_pages:
        url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if not _same_origin(url, base_url):
            continue
        if _path_excluded(url, excludes):
            continue

        r, fetch_err, ms, attempts = _fetch_with_retry(
            url, user_agent=user_agent, timeout_s=request_timeout_s,
            retries=retry_on_error, backoff_s=retry_backoff_s,
        )
        if r is None:
            page = Page(url=url, status_code=0, fetch_ms=ms, depth=depth,
                        error=fetch_err, attempts=attempts)
            yield page
            fetched += 1
            continue

        page = Page(
            url=url, status_code=r.status_code, fetch_ms=ms, depth=depth,
            attempts=attempts,
            content_type=(r.headers.get("Content-Type") or "").split(";")[0].strip(),
        )
        if "html" not in page.content_type.lower() and "xml" not in page.content_type.lower():
            # Non-HTML: record + don't extract
            yield page
            fetched += 1
            continue

        try:
            extracted = _extract(r.text, url)
            page.title = extracted["title"]
            page.description = extracted["description"]
            page.canonical = extracted["canonical"]
            page.h1 = extracted["h1"]
            page.body_text = extracted["body_text"]
            page.links = extracted["links"]
            page.jsonld_types = extracted.get("jsonld_types", []) or []
            page.jsonld_count = extracted.get("jsonld_count", 0) or 0
            page.og_type = extracted.get("og_type", "") or ""
            page.robots_meta = extracted.get("robots_meta", "") or ""
            page.twitter_card = extracted.get("twitter_card", "") or ""
            import hashlib as _h
            page.body_hash = _h.sha1(page.body_text.encode("utf-8")).hexdigest()[:16]
        except Exception as e:
            # Parse failure, not a fetch failure — keep the status code the
            # origin actually returned so downstream checks don't report a
            # 200 page as unreachable.
            page.error = f"extract failed: {e}"[:300]
            yield page
            fetched += 1
            continue

        yield page
        fetched += 1

        # Enqueue children (only if HTML 2xx + within depth)
        if 200 <= page.status_code < 300 and depth + 1 <= max_depth:
            for link in page.links:
                if link not in seen and _same_origin(link, base_url):
                    if _path_excluded(link, excludes):
                        continue
                    queue.append((link, depth + 1))

        if throttle_ms > 0:
            time.sleep(throttle_ms / 1000.0)
