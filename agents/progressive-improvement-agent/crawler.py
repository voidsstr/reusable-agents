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
) -> Iterator[Page]:
    """BFS crawl. Yields Page objects in fetch order. Caller is responsible for
    persisting results."""
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

        t0 = time.monotonic()
        try:
            r = requests.get(
                url, headers={"User-Agent": user_agent},
                timeout=request_timeout_s, allow_redirects=True,
            )
            ms = int((time.monotonic() - t0) * 1000)
            page = Page(
                url=url, status_code=r.status_code, fetch_ms=ms, depth=depth,
                content_type=(r.headers.get("Content-Type") or "").split(";")[0].strip(),
            )
            if "html" not in page.content_type.lower() and "xml" not in page.content_type.lower():
                # Non-HTML: record + don't extract
                yield page
                fetched += 1
                continue
            extracted = _extract(r.text, url)
            page.title = extracted["title"]
            page.description = extracted["description"]
            page.canonical = extracted["canonical"]
            page.h1 = extracted["h1"]
            page.body_text = extracted["body_text"]
            page.links = extracted["links"]
            import hashlib as _h
            page.body_hash = _h.sha1(page.body_text.encode("utf-8")).hexdigest()[:16]
        except Exception as e:
            ms = int((time.monotonic() - t0) * 1000)
            page = Page(url=url, status_code=0, fetch_ms=ms, depth=depth,
                        error=str(e)[:300])
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
