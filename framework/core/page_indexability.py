"""Is a live page indexable, and how fast is it? — one generic answer.

Used by the deployer's post-deploy gate (agents/deployer/deployer.py) and
anything else that must tell "the page is up" from "the page is up AND a
search/AI crawler may index it". A 200 is not enough: in September 2026 a
PI change noindexed 99.8% of aisleprompt's recipes and shipped through 39
releases because the smoke check only looked at status codes.

A page is indexable when it:
  - returns 200 without a redirect,
  - carries no `noindex`/`none` in X-Robots-Tag (unscoped, or scoped to
    bingbot/robots/*) nor in <meta name="robots"|"bingbot">,
  - declares a self-canonical (or none),
  - optionally, renders at least `min_text_chars` of visible text (an
    empty SSR shell with `index,follow` is still a dead page).

Mirrors `classifyPage` in agents/indexnow-submitter/submit.ts so the
submitter and the deploy gate agree on what "indexable" means.

Stdlib only (urllib), so any agent can import it.
"""
from __future__ import annotations

import html as _html
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Optional

DEFAULT_UA = "Mozilla/5.0 (compatible; IndexabilityCheck/1.0; +https://github.com/voidsstr/reusable-agents)"

_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_NOINDEX_RE = re.compile(r"(^|[\s,])(noindex|none)([\s,]|$)", re.I)
_KNOWN_DIRECTIVES = {"unavailable_after", "max-snippet", "max-image-preview", "max-video-preview"}


def _attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag):
        out[m.group(1).lower()] = (m.group(3) or m.group(4) or m.group(5) or "").strip()
    return out


def normalize_url(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u)
        host = (p.hostname or "").lower()
        port = p.port
        netloc = host if not port or (p.scheme, port) in (("https", 443), ("http", 80)) else f"{host}:{port}"
        path = p.path or "/"
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        return urllib.parse.urlunsplit((p.scheme, netloc, path, p.query, ""))
    except Exception:
        return u


def header_noindex(x_robots_tag: Optional[str]) -> bool:
    """`noindex, follow` → True; `googlebot: noindex` → False (scoped to
    another crawler); `bingbot: noindex` → True."""
    if not x_robots_tag:
        return False
    scope: Optional[str] = None
    for raw in x_robots_tag.lower().split(","):
        tok = raw.strip()
        m = re.match(r"^([a-z0-9_*-]+)\s*:\s*(.*)$", tok)
        if m and m.group(1) not in _KNOWN_DIRECTIVES:
            scope, tok = m.group(1), m.group(2)
        if _NOINDEX_RE.search(tok) and scope in (None, "bingbot", "robots", "*"):
            return True
    return False


def visible_text_len(html: str) -> int:
    t = re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S)
    t = re.sub(r"<style\b.*?</style>", " ", t, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return len(re.sub(r"\s+", " ", _html.unescape(t)).strip())


def classify_page(url: str, status: int, x_robots_tag: Optional[str], html: str,
                  min_text_chars: int = 0) -> tuple[bool, str]:
    """(indexable, reason). Reasons: ok, redirect-3xx, http-<code>,
    noindex-header, noindex-meta, canonical-elsewhere, thin-body."""
    if 300 <= status < 400:
        return False, f"redirect-{status}"
    if status != 200:
        return False, f"http-{status or 'error'}"
    if header_noindex(x_robots_tag):
        return False, "noindex-header"
    head = html[:400_000]
    for m in _META_RE.finditer(head):
        a = _attrs(m.group(0))
        if a.get("name", "").lower() in ("robots", "bingbot") and _NOINDEX_RE.search(a.get("content", "")):
            return False, "noindex-meta"
    for m in _LINK_RE.finditer(head):
        a = _attrs(m.group(0))
        if "canonical" not in a.get("rel", "").lower().split() or not a.get("href"):
            continue
        canon = urllib.parse.urljoin(url, _html.unescape(a["href"]))
        if normalize_url(canon) != normalize_url(url):
            return False, "canonical-elsewhere"
        break
    if min_text_chars and visible_text_len(html) < min_text_chars:
        return False, "thin-body"
    return True, "ok"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 — keep the 3xx
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class PageCheck:
    url: str
    ua: str
    status: int = 0
    ttfb_s: Optional[float] = None
    total_s: Optional[float] = None
    indexable: bool = False
    reason: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def fetch_page(url: str, *, ua: str = DEFAULT_UA, timeout: float = 30.0,
               max_bytes: int = 2_000_000) -> tuple[int, Optional[str], str, Optional[float], Optional[float], str]:
    """(status, x_robots_tag, html, ttfb_s, total_s, error). Redirects are
    NOT followed. TTFB = time until response headers arrived."""
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    t0 = time.monotonic()
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            ttfb = time.monotonic() - t0
            body = resp.read(max_bytes).decode("utf-8", "replace")
            return resp.status, resp.headers.get("X-Robots-Tag"), body, ttfb, time.monotonic() - t0, ""
    except urllib.error.HTTPError as e:
        ttfb = time.monotonic() - t0
        try:
            body = e.read(max_bytes).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, e.headers.get("X-Robots-Tag") if e.headers else None, body, ttfb, time.monotonic() - t0, ""
    except Exception as e:  # timeout, DNS, TLS
        return 0, None, "", None, time.monotonic() - t0, f"{type(e).__name__}: {e}"[:200]


def check_page(url: str, *, ua: str = DEFAULT_UA, timeout: float = 30.0,
               min_text_chars: int = 0) -> PageCheck:
    status, xrt, body, ttfb, total, err = fetch_page(url, ua=ua, timeout=timeout)
    pc = PageCheck(url=url, ua=ua, status=status, ttfb_s=None if ttfb is None else round(ttfb, 3),
                   total_s=None if total is None else round(total, 3), error=err)
    if err:
        pc.reason = "timeout" if "timed out" in err.lower() or "Timeout" in err else "fetch-error"
        return pc
    pc.indexable, pc.reason = classify_page(url, status, xrt, body, min_text_chars)
    return pc


# ── Sitemaps ───────────────────────────────────────────────────────────────

_URL_BLOCK = re.compile(r"<url\b[^>]*>(.*?)</url>", re.I | re.S)
_SM_BLOCK = re.compile(r"<sitemap\b[^>]*>(.*?)</sitemap>", re.I | re.S)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)


_RSS_ITEM = re.compile(r"<item\b[^>]*>(.*?)</item>", re.I | re.S)
_RSS_LINK = re.compile(r"<link>\s*(.*?)\s*</link>", re.I | re.S)
_ATOM_ENTRY = re.compile(r"<entry\b[^>]*>(.*?)</entry>", re.I | re.S)
_ATOM_LINK = re.compile(r"""<link\b[^>]*href=["']([^"']+)["']""", re.I)

# Sitemaps may be up to 50 MB uncompressed (sitemaps.org); a 45k-URL child is
# ~6 MB, so the page-sized read cap would silently truncate it.
SITEMAP_MAX_BYTES = 60_000_000


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """(page_urls, child_sitemap_urls). Accepts <urlset>, <sitemapindex>, and
    the RSS 2.0 / Atom feeds search engines also take as sitemaps."""
    xml = xml or ""
    is_index = re.search(r"<sitemapindex\b", xml, re.I) is not None
    blocks = (_SM_BLOCK if is_index else _URL_BLOCK).findall(xml)
    locs = []
    for b in blocks:
        m = _LOC.search(b)
        if m:
            locs.append(_html.unescape(m.group(1).strip()))
    if not is_index and not locs:
        for b in _RSS_ITEM.findall(xml):
            m = _RSS_LINK.search(b)
            if m:
                locs.append(_html.unescape(m.group(1).strip()))
        for b in _ATOM_ENTRY.findall(xml):
            m = _ATOM_LINK.search(b)
            if m:
                locs.append(_html.unescape(m.group(1).strip()))
    return ([], locs) if is_index else (locs, [])


@dataclass
class SitemapReport:
    index_url: str
    index_status: int = 0
    children: list[dict] = field(default_factory=list)
    sample: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    #: every page URL read from the children (for callers that sample later)
    pool: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {"index_url": self.index_url, "index_status": self.index_status, "ok": self.ok,
                "children": self.children, "sample": self.sample, "failures": self.failures}


def check_sitemaps(index_url: str, *, ua: str = DEFAULT_UA, timeout: float = 60.0,
                   allow_empty: Iterable[str] = (), sample_n: int = 0,
                   max_bad_ratio: float = 0.25, min_text_chars: int = 0,
                   rng: Optional[random.Random] = None) -> SitemapReport:
    """Every child listed by the index must return 200 with ≥1 <url>
    (unless its URL or path is in `allow_empty`). Optionally sample
    `sample_n` page URLs across the children and fail when more than
    `max_bad_ratio` of them are not indexable."""
    rep = SitemapReport(index_url=index_url)
    allow = {a for a in allow_empty}
    status, _x, body, _t, _tt, err = fetch_page(index_url, ua=ua, timeout=timeout,
                                                max_bytes=SITEMAP_MAX_BYTES)
    rep.index_status = status
    if status != 200:
        rep.failures.append(f"sitemap index {index_url} → {status or err}")
        return rep
    pages, children = parse_sitemap(body)
    if not children:
        children = [index_url] if pages else []
    pool: list[str] = list(pages)
    for child in children:
        if child == index_url:
            rep.children.append({"url": child, "status": status, "urls": len(pages)})
            continue
        st, _x, xml, ttfb, _tt, e = fetch_page(child, ua=ua, timeout=timeout,
                                               max_bytes=SITEMAP_MAX_BYTES)
        urls, _sub = parse_sitemap(xml) if st == 200 else ([], [])
        rep.children.append({"url": child, "status": st, "urls": len(urls),
                             "ttfb_s": None if ttfb is None else round(ttfb, 2), "error": e})
        path = urllib.parse.urlsplit(child).path
        if st != 200:
            rep.failures.append(f"sitemap {child} → HTTP {st or e}")
        elif not urls and child not in allow and path not in allow:
            rep.failures.append(f"sitemap {child} is empty")
        pool.extend(urls)
    rep.pool = pool
    if sample_n and pool:
        r = rng or random.Random()
        picks = r.sample(pool, min(sample_n, len(pool)))
        bad = 0
        for u in picks:
            pc = check_page(u, ua=ua, timeout=timeout, min_text_chars=min_text_chars)
            rep.sample.append({"url": u, "status": pc.status, "reason": pc.reason})
            bad += 0 if pc.indexable else 1
        if picks and bad / len(picks) > max_bad_ratio:
            rep.failures.append(f"sitemap sample: {bad}/{len(picks)} URLs not indexable "
                                f"(> {max_bad_ratio:.0%})")
    return rep
