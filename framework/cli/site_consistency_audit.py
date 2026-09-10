#!/usr/bin/env python3
"""Site consistency auditor — a broad, automated "does this site still look
professional?" crawl.

WHY THIS EXISTS
---------------
A fleet of autonomous agents writes to these sites continuously: they publish
articles, swap hero images, rewrite titles, add schema, deactivate products and
ship code. Each agent validates its own narrow change. Nothing checks the
RESULT the way a visitor sees it.

That gap is not theoretical. On 2026-09-10 the article "Ah Yes, 'Gaming'
Laptops: The Hype vs. Reality" was found running a gaming MOUSE product photo
as its hero. It had been wrong for months. Three separate guards should have
caught it and none did, because each was scoped to its own change.

This auditor is the outside check: it crawls the live site like a reader,
breadth-first to a configurable depth, and reports everything that looks wrong
regardless of which agent caused it. It is deliberately read-only — it reports,
it never repairs, so it can never itself become a source of damage.

DESIGN NOTES
------------
* Read-only. No DB writes, no repairs. Findings go to JSON + stdout.
* Same-origin only. Never follows off-site links.
* Bounded: max_pages, depth and a per-request timeout, so a scheduled run
  cannot melt the origin (which matters — these sites SSR every request and
  are not CDN-cached today).
* Every check is independent and additive; one failing check never aborts the
  crawl.
* Severity is honest: `error` means a visitor sees something broken or wrong,
  `warn` means it is sloppy, `info` is a hygiene note.

Usage:
    python3 -m framework.cli.site_consistency_audit \\
        --base https://specpicks.com --depth 5 --max-pages 400 \\
        --out /tmp/specpicks-audit.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (compatible; SiteConsistencyAudit/1.0; +ops)"

# Text that should never reach a reader. Each of these has actually shipped to
# production on one of these sites at some point.
# NOTE on calibration: the first version of this list included bare
# "undefined", which fired on the perfectly good sentence "perf-per-dollar is
# undefined". On a tech-review site that word is ordinary English. Only
# code-shaped forms survive here.
PLACEHOLDER = re.compile(
    r"(lorem ipsum|TODO:|FIXME:|\[object Object\]|"
    r"\bundefined\s*(?:px|%|,|\)|</)|=\s*undefined\b|"
    r"\bNaN\b|null null|\{\{[a-z_]+\}\}|<no title>|\bUntitled\b)", re.I)

# Generator exhaust that leaks the prompt into user-visible copy. The catalog
# has a long history of these ("Phantom Battery Chocolate Mousse", "Multiple
# Inheritance Miso-Glazed Eggplant Donburi", "Interactive Digital Candy
# Popcorn Clusters"). Phrase-form only — bare words false-positive constantly.
# CALIBRATION: bare "prompt" was in this list and matched 52 pages of
# legitimate copy ("both scale linearly with prompt length") — on an AI-hardware
# site that word is a topic, not a leak. Same for "language model". Only
# assistant-voice phrases and the known descriptor-bank leaks remain, which is
# what the file's own "phrase-form only" rule demanded in the first place.
PROMPT_LEAK = re.compile(
    r"(\binteractive digital\b|\binteractive web\b|\bweb-themed\b|"
    r"\bhidden object\b|\bas an ai (language )?model\b|"
    r"\bi'm sorry,? (but )?i (can'?t|cannot)\b|\bi cannot fulfill\b|"
    r"\bplaceholder text\b|\bhere is the (article|rewritten)\b)", re.I)

# A money value that is definitionally wrong to display.
# $0 or $0.00 ONLY. The trailing guard must reject "$0.94/Task", which an
# earlier version matched because it only looked for a following DIGIT and
# the next char was ".".
# CALIBRATION: "$0" alone is legitimate in a pricing table ("Free $0 — enable
# the console's CRT filter"). Only flag a zero price sitting next to BUY
# language, which is where a $0 is actually a broken buy box.
BAD_PRICE = re.compile(
    r"(?<![\d.])\$0(?:\.00)?(?![\d.])"
    r"(?=[^.]{0,60}\b(buy|shop|add to cart|view (price|deal)|on amazon|check price)\b)", re.I)

TAG = re.compile(r"<[^>]+>")


def _get(url: str, timeout: int, method: str = "GET"):
    """Fetch a URL. Returns (status, headers, body_text, elapsed_s)."""
    t0 = time.time()
    try:
        rq = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            body = b"" if method == "HEAD" else r.read(800_000)
            return r.status, dict(r.headers), body.decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(getattr(e, "headers", {}) or {}), "", time.time() - t0
    except Exception as e:
        return 0, {"_err": type(e).__name__}, "", time.time() - t0


def _text_of(html: str) -> str:
    no_script = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", TAG.sub(" ", no_script)).strip()


def _attr_all(html: str, tag: str, attr: str) -> list[str]:
    out = []
    for m in re.finditer(rf"<{tag}\b[^>]*>", html, re.I):
        a = re.search(rf'{attr}\s*=\s*["\']([^"\']+)["\']', m.group(0), re.I)
        if a:
            out.append(a.group(1))
    return out


def audit_page(url: str, html: str, status: int, headers: dict, elapsed: float,
               origin: str, cfg: dict) -> list[dict]:
    """All page-level checks. Pure — takes a fetched page, returns findings."""
    f = []
    def add(sev, kind, msg, **extra):
        f.append(dict(severity=sev, kind=kind, url=url, message=msg, **extra))

    if status == 0:
        add("error", "unreachable", f"request failed ({headers.get('_err','?')})")
        return f
    if status >= 500:
        add("error", "http-5xx", f"HTTP {status}")
        return f
    if status in (404, 410):
        # Only a finding when something LINKED here; the caller records that.
        add("warn", "http-404", f"HTTP {status}")
        return f
    if status != 200:
        add("warn", "http-status", f"HTTP {status}")
        return f

    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "html" not in ctype:
        return f  # xml/json endpoints get status checks only

    text = _text_of(html)
    words = len(text.split())
    title = (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, ""])[1].strip()
    h1s = re.findall(r"<h1\b[^>]*>(.*?)</h1>", html, re.S | re.I)

    # ── Shell / soft-404 ────────────────────────────────────────────────
    # A 200 that renders almost nothing is worse than a 404: Google indexes
    # it, a reader sees an empty page, and no monitor notices because the
    # status code is fine.
    if words < cfg["min_words"]:
        add("error", "thin-or-shell", f"only {words} visible words (soft-404 / SSR shell?)", words=words)
    if not h1s:
        add("error", "no-h1", "page has no <h1>")
    elif len(h1s) > 1:
        add("warn", "multiple-h1", f"{len(h1s)} <h1> elements")

    # ── Title ───────────────────────────────────────────────────────────
    if not title:
        add("error", "no-title", "empty <title>")
    elif len(title) < 12:
        add("warn", "short-title", f"title is {len(title)} chars: {title!r}")

    # ── Canonical ───────────────────────────────────────────────────────
    canon = _attr_all(html, "link", "href")
    canon = [c for c in canon if 'canonical' in html[max(0, html.find(c) - 120):html.find(c)].lower()]
    if not canon:
        add("warn", "no-canonical", "no rel=canonical")

    # ── Placeholder / leak / bad money in visible copy ──────────────────
    for rx, kind, sev, label in (
        (PLACEHOLDER, "placeholder-text", "error", "placeholder/debug text"),
        (PROMPT_LEAK, "prompt-leak", "error", "generator/prompt exhaust"),
        (BAD_PRICE, "zero-price", "warn", "$0 price"),
    ):
        m = rx.search(text)
        if m:
            i = max(0, m.start() - 45)
            add(sev, kind, f"{label}: …{text[i:m.end() + 45]}…")

    # ── Images ──────────────────────────────────────────────────────────
    imgs = [urllib.parse.urljoin(url, s) for s in _attr_all(html, "img", "src") if s]
    if not imgs:
        # Author bios, policy pages and tag indexes legitimately have none.
        add("info", "no-images", "page renders no <img>")
    for i in _attr_all(html, "img", "src"):
        if i.startswith("data:") or not i.strip():
            continue
    missing_alt = len([1 for m in re.finditer(r"<img\b[^>]*>", html, re.I)
                       if not re.search(r'\balt\s*=', m.group(0), re.I)])
    if missing_alt:
        add("info", "img-missing-alt", f"{missing_alt} <img> without alt")

    # ── Structured data ─────────────────────────────────────────────────
    lds = re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I)
    for raw in lds:
        try:
            json.loads(raw.strip())
        except Exception as e:
            add("error", "invalid-jsonld", f"JSON-LD does not parse: {type(e).__name__}")
            break

    # ── Performance (a reader-visible defect at these magnitudes) ───────
    if elapsed > cfg["slow_s"]:
        add("warn", "slow-page", f"{elapsed:.1f}s to first byte+body", secs=round(elapsed, 2))

    return f, title, (h1s[0].strip() if h1s else ""), imgs


def crawl(base: str, depth: int, max_pages: int, timeout: int, workers: int,
          cfg: dict) -> dict:
    origin = urllib.parse.urlsplit(base)
    seen, queue, results = set(), collections.deque([(base.rstrip("/") + "/", 0)]), []
    titles, h1s, all_imgs, linked_from = {}, {}, set(), {}

    while queue and len(seen) < max_pages:
        batch = []
        while queue and len(batch) < workers and len(seen) + len(batch) < max_pages:
            u, d = queue.popleft()
            if u in seen:
                continue
            seen.add(u)
            batch.append((u, d))
        if not batch:
            break

        with ThreadPoolExecutor(workers) as ex:
            fetched = list(ex.map(lambda p: (p[0], p[1], *_get(p[0], timeout)), batch))

        for url, d, status, headers, html, elapsed in fetched:
            out = audit_page(url, html, status, headers, elapsed, base, cfg)
            if isinstance(out, list):
                results.extend(out)
                continue
            page_f, title, h1, imgs = out
            results.extend(page_f)
            if title:
                titles.setdefault(title, []).append(url)
            if h1:
                h1s.setdefault(h1, []).append(url)
            all_imgs.update(imgs[: cfg["imgs_per_page"]])

            if d < depth and status == 200:
                for href in _attr_all(html, "a", "href"):
                    nxt = urllib.parse.urljoin(url, href).split("#")[0].rstrip("/") or "/"
                    p = urllib.parse.urlsplit(nxt)
                    if p.netloc != origin.netloc or p.scheme not in ("http", "https"):
                        continue
                    if re.search(r"\.(jpg|jpeg|png|webp|gif|svg|css|js|ico|pdf|zip)$", p.path, re.I):
                        continue
                    # API and machine endpoints are not reader-facing pages.
                    # Crawling them produced 71 bogus "no-canonical" findings on
                    # /api/instacart/recipe-go/* affiliate redirects, which have
                    # no business carrying a canonical tag in the first place.
                    if re.match(r"^/(api|_next|static|assets|feed|rss)(/|$)", p.path, re.I):
                        continue
                    if nxt not in seen:
                        linked_from.setdefault(nxt, url)
                        queue.append((nxt, d + 1))

    # ── Cross-page checks (only possible once the crawl is done) ────────
    for t, urls in titles.items():
        if len(urls) > cfg["dupe_title_cap"]:
            results.append(dict(severity="warn", kind="duplicate-title", url=urls[0],
                                message=f"<title> {t[:60]!r} used on {len(urls)} pages",
                                count=len(urls), sample=urls[:4]))
    for t, urls in h1s.items():
        if len(urls) > cfg["dupe_title_cap"]:
            results.append(dict(severity="info", kind="duplicate-h1", url=urls[0],
                                message=f"<h1> {t[:60]!r} used on {len(urls)} pages",
                                count=len(urls), sample=urls[:4]))

    # ── Image reachability (the defect class that started all this) ─────
    imgs = list(all_imgs)[: cfg["max_images"]]
    if imgs:
        with ThreadPoolExecutor(workers) as ex:
            probes = list(ex.map(lambda i: (i, *_get(i, timeout, method="HEAD")), imgs))
        for iu, st, hd, _b, _e in probes:
            if st == 0 or st >= 400:
                results.append(dict(severity="error", kind="broken-image", url=iu,
                                    message=f"image returns {st or hd.get('_err','error')}"))
            else:
                n = hd.get("Content-Length") or hd.get("content-length")
                if n and n.isdigit() and int(n) < cfg["tiny_img_bytes"]:
                    results.append(dict(severity="warn", kind="tiny-image", url=iu,
                                        message=f"image is only {int(n)//1024}KB — renders soft in a lead card"))

    return {"base": base, "pages_crawled": len(seen), "images_checked": len(imgs),
            "findings": results}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--depth", type=int, default=5)
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-words", type=int, default=180)
    ap.add_argument("--slow-s", type=float, default=8.0)
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    cfg = dict(min_words=a.min_words, slow_s=a.slow_s, dupe_title_cap=3,
               imgs_per_page=6, max_images=500, tiny_img_bytes=12_000)

    rep = crawl(a.base, a.depth, a.max_pages, a.timeout, a.workers, cfg)
    by_kind = collections.Counter(f["kind"] for f in rep["findings"])
    by_sev = collections.Counter(f["severity"] for f in rep["findings"])
    rep["summary"] = {"by_kind": dict(by_kind), "by_severity": dict(by_sev)}

    print(f"\n{a.base}  —  {rep['pages_crawled']} pages, {rep['images_checked']} images, "
          f"{len(rep['findings'])} findings")
    print(f"  severity: {dict(by_sev)}")
    for kind, n in by_kind.most_common():
        sample = next(f for f in rep["findings"] if f["kind"] == kind)
        print(f"  {n:>5}  {kind:<20} e.g. {sample['url'][:66]}")
        print(f"         {sample['message'][:110]}")

    if a.out:
        with open(a.out, "w") as fh:
            json.dump(rep, fh, indent=1)
        print(f"\n  full report -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
