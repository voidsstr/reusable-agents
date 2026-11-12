#!/usr/bin/env python3
"""Site consistency auditor — a broad, automated "is this site still good?" crawl.

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

The first version of this file only asked "is this page broken?". That is the
floor, not the goal. A site can be entirely unbroken and still bleed readers:
dead-end pages with nowhere to go, duplicate titles competing against each
other in search, missing meta descriptions nobody clicks, fabricated review
counts that destroy trust. So the checks are grouped by what they protect:

  INTEGRITY   — is it broken?        (soft-404s, dead images, invalid schema)
  RETENTION   — will they read on?   (dead ends, orphans, redirect hops, CTAs)
  ACQUISITION — will they arrive?    (meta descriptions, SERP titles, sitemap
                                      lies, stale year claims)
  TRUST       — does it look honest? (fabricated ratings, price parity,
                                      locality leaks, missing bylines)
  REGRESSION  — what changed since the agents last shipped?

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
  `warn` means it is sloppy or costing traffic, `info` is a hygiene note.
* Every finding names the page that CAUSED it wherever that is knowable
  (`on_page`, `linked_from`). A finding you cannot route to an owner is a
  finding nobody fixes.

Usage:
    python3 -m framework.cli.site_consistency_audit \\
        --base https://specpicks.com --depth 5 --max-pages 400 \\
        --out /tmp/specpicks-audit.json
"""
from __future__ import annotations

import argparse
import base64
import collections
import copy
import html as _html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (compatible; SiteConsistencyAudit/1.0; +ops)"
# Some CDNs (notably m.media-amazon.com) rate-limit or 403 an unfamiliar UA.
# Image re-confirmation uses a browser UA so we never report a CDN's opinion of
# our crawler as a broken image.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Text that should never reach a reader.
# CALIBRATION: the first version included bare "undefined", which fired on the
# perfectly good sentence "perf-per-dollar is undefined". On a tech-review site
# that word is ordinary English. Only code-shaped forms survive here.
# NaN stays case-sensitive: under re.I it matched the Burmese dish "Nan Gyi
# Thoke" in an aisleprompt recipe rail (2026-10-12).
PLACEHOLDER = re.compile(
    r"(lorem ipsum|TODO:|FIXME:|\[object Object\]|"
    r"\bundefined\s*(?:px|%|,|\)|</)|=\s*undefined\b|"
    r"(?-i:\bNaN\b)|null null|\{\{[a-z_]+\}\}|<no title>|\bUntitled\b)", re.I)

# Generator exhaust that leaks the prompt into user-visible copy.
# CALIBRATION: bare "prompt" was in this list and matched 52 pages of
# legitimate copy ("both scale linearly with prompt length") — on an
# AI-hardware site that word is a topic, not a leak. Same for "language model".
PROMPT_LEAK = re.compile(
    r"(\binteractive digital\b|\binteractive web\b|\bweb-themed\b|"
    r"\bhidden object\b|\bas an ai (language )?model\b|"
    r"\bi'm sorry,? (but )?i (can'?t|cannot)\b|\bi cannot fulfill\b|"
    r"\bplaceholder text\b|\bhere is the (article|rewritten)\b)", re.I)

# CALIBRATION: "$0" alone is legitimate in a pricing table ("Free $0 — enable
# the console's CRT filter"). Only flag a zero price sitting next to BUY
# language, which is where a $0 is actually a broken buy box.
BAD_PRICE = re.compile(
    r"(?<![\d.])\$0(?:\.00)?(?![\d.])"
    r"(?=[^.]{0,60}\b(buy|shop|add to cart|view (price|deal)|on amazon|check price)\b)", re.I)

# A US "<Place>, <State>" shape in a TITLE is almost always scenery leaking out
# of the generator's prompt context, not editorial intent. Real offenders have
# shipped here: "Zanesville", "Park Hill", "Mountain Home, Arkansas".
# NOTE: a BARE city name ("Zanesville") cannot be caught without a gazetteer —
# the complement is an open set of US suburbs. This catches the "City, State"
# shape; bare names go in --leak-token once a site knows them.
US_STATES = (r"Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|"
             r"Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|"
             r"Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
             r"New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
             r"Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|"
             r"Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming")
LOCALITY_LEAK = re.compile(rf"\b[A-Z][a-zA-Z]+,\s+({US_STATES})\b")

# Buy / affiliate paths. A monetized page with none of these is a dead end for
# both the reader and the revenue.
CTA = re.compile(
    r"(amzn\.to|amazon\.[a-z.]+/dp/|[?&]tag=|/api/instacart/|instacart\.com|"
    r"ebay\.[a-z.]+/itm|/go/|add to cart|shop on|view (price|deal)|check price|buy now)", re.I)

TAG = re.compile(r"<[^>]+>")
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)")

# Paths the auditor never requests, on any site: affiliate click-outs log a
# click and redirect to a retailer, and API routes are not pages. A site
# config can replace it (`never_fetch_path_re`).
DEFAULT_NEVER_FETCH = r"^/(?:k|r|go)/|^/api/"


def _never_fetch(path: str, cfg: dict) -> bool:
    rx = cfg.get("never_fetch_path_re", DEFAULT_NEVER_FETCH) if cfg else DEFAULT_NEVER_FETCH
    try:
        return bool(rx) and bool(re.search(rx, path or ""))
    except re.error:
        return bool(re.search(DEFAULT_NEVER_FETCH, path or ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib following redirects so a 3xx becomes observable.

    Redirects were invisible to the first version: urllib follows them
    silently, so an internal link pointing at a 301 looked identical to one
    pointing at the final URL. Those hops cost crawl budget, leak link equity
    and add latency for every reader who follows them.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


RETRY_PAUSE_S = 3


def _get(url: str, timeout: int, method: str = "GET", ua: str = UA, retries: int = 1,
         headers: dict | None = None):
    """Fetch one URL WITHOUT following redirects.

    Returns (status, headers, body_text, elapsed_s). A 3xx carries its
    Location header so the caller can record the hop.

    A failure with no HTTP response at all (connection reset, refused, timed
    out) is retried `retries` times before it is reported as status 0. The
    evening crawls of 2026-10-03 and 10-04 each flagged two specpicks
    benchmark pages "unreachable" on a single URLError while the pages served
    200 minutes later, and a new kind marks the whole run as a regression. An
    HTTP error status is a real answer from the server and is never retried.
    elapsed_s times the attempt that answered, so a retry cannot fake a slow page.

    `headers` are extra request headers — the site config's
    `request_headers`, e.g. a probe header that tells the site's analytics
    the request is our own.
    """
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            rq = urllib.request.Request(url, headers={"User-Agent": ua, **(headers or {})},
                                        method=method)
            with _OPENER.open(rq, timeout=timeout) as r:
                body = b"" if method == "HEAD" else r.read(800_000)
                return r.status, dict(r.headers), body.decode("utf-8", "replace"), time.time() - t0
        except urllib.error.HTTPError as e:
            hd = dict(getattr(e, "headers", {}) or {})
            return e.code, hd, "", time.time() - t0
        except Exception as e:
            err = e
            if attempt < retries:
                time.sleep(RETRY_PAUSE_S)
    reason = getattr(err, "reason", None) or err
    return 0, {"_err": f"{type(err).__name__}: {reason}"[:160],
               "_attempts": retries + 1}, "", time.time() - t0


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


def _meta(html: str, name: str) -> str:
    m = (re.search(rf'<meta[^>]+name\s*=\s*["\']{name}["\'][^>]*>', html, re.I)
         or re.search(rf'<meta[^>]+property\s*=\s*["\']{name}["\'][^>]*>', html, re.I))
    if not m:
        return ""
    # Match up to the SAME quote that opened the value. A `[^"']*` class stopped
    # at the first apostrophe, so "Peshawar's iconic flat kebab..." read as
    # "Peshawar" and 2026-10-07 reported false short-meta-description findings.
    c = re.search(r'content\s*=\s*(["\'])(.*?)\1', m.group(0), re.I | re.S)
    return c.group(2).strip() if c else ""


def _jsonld(html: str) -> list:
    out = []
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                          html, re.S | re.I):
        try:
            d = json.loads(raw.strip())
        except Exception:
            out.append("__INVALID__")
            continue
        out.extend(d if isinstance(d, list) else [d])
    return out


def _price_matches(want: float, visible: set, tol: float = 0.02) -> bool:
    """Is a schema price represented on the page, allowing for rounding?

    Display prices are frequently rounded ("$5,300" for 5299.99) and that is a
    presentation choice, not a mismatch. An exact string compare flagged every
    such page and would have made this check pure noise. A 2% band still
    catches what matters: a schema price left behind by a stale buy box, which
    is off by tens of percent, not cents.
    """
    return any(abs(v - want) <= max(tol * want, 0.01) for v in visible)


def _walk(node):
    """Yield every dict in a nested JSON-LD structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# ── Page-level checks ───────────────────────────────────────────────────────

def audit_page(url, html, status, headers, elapsed, cfg) -> dict:
    """All page-level checks. Pure: takes a fetched page, returns a record with
    its findings plus the facts the cross-page checks need afterwards."""
    findings: list[dict] = []
    rec = {"url": url, "status": status, "findings": findings, "title": "", "h1": "",
           "meta_desc": "", "links": [], "imgs": [], "redirect_to": "", "is_html": False,
           "canonical": ""}

    def add(sev, kind, msg, **extra):
        findings.append(dict(severity=sev, kind=kind, url=url, message=msg, **extra))

    if status == 0:
        add("error", "unreachable", f"request failed after {headers.get('_attempts', 1)} "
                                    f"attempt(s) ({headers.get('_err', '?')})")
        return rec
    if 300 <= status < 400:
        loc = headers.get("Location") or headers.get("location") or ""
        rec["redirect_to"] = urllib.parse.urljoin(url, loc)
        return rec
    if status >= 500:
        add("error", "http-5xx", f"HTTP {status}")
        return rec
    if status in (404, 410):
        add("warn", "http-404", f"HTTP {status}")
        return rec
    if status != 200:
        add("warn", "http-status", f"HTTP {status}")
        return rec

    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "html" not in ctype:
        return rec
    rec["is_html"] = True

    text = _text_of(html)
    words = len(text.split())
    title = (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, ""])[1].strip()
    h1s = re.findall(r"<h1\b[^>]*>(.*?)</h1>", html, re.S | re.I)
    desc = _meta(html, "description")
    path = urllib.parse.urlsplit(url).path
    rec.update(title=title, h1=(TAG.sub("", h1s[0]).strip() if h1s else ""), meta_desc=desc)

    # ── INTEGRITY ───────────────────────────────────────────────────────
    # A 200 that renders almost nothing is worse than a 404: Google indexes it,
    # a reader sees an empty page, and no status-code monitor notices.
    if words < cfg["min_words"]:
        add("error", "thin-or-shell", f"only {words} visible words (soft-404 / SSR shell?)", words=words)
    if not h1s:
        add("error", "no-h1", "page has no <h1>")
    elif len(h1s) > 1:
        add("warn", "multiple-h1", f"{len(h1s)} <h1> elements")
    if not title:
        add("error", "no-title", "empty <title>")
    elif len(title) < 12:
        add("warn", "short-title", f"title is {len(title)} chars: {title!r}")
    cm = re.search(r'<link[^>]+rel\s*=\s*["\']canonical["\'][^>]*>', html, re.I)
    if not cm:
        add("warn", "no-canonical", "no rel=canonical")
    else:
        ch = re.search(r'href\s*=\s*["\']([^"\']+)["\']', cm.group(0), re.I)
        if ch:
            rec["canonical"] = urllib.parse.urljoin(url, _html.unescape(ch.group(1))).rstrip("/") or "/"

    for rx, kind, sev, label in (
        (PLACEHOLDER, "placeholder-text", "error", "placeholder/debug text"),
        (PROMPT_LEAK, "prompt-leak", "error", "generator/prompt exhaust"),
        (BAD_PRICE, "zero-price", "warn", "$0 next to buy language"),
    ):
        m = rx.search(text)
        if m:
            i = max(0, m.start() - 45)
            add(sev, kind, f"{label}: …{text[i:m.end() + 45]}…")

    # ── ACQUISITION: what a searcher sees before deciding to click ──────
    # The meta description IS the SERP snippet. Missing it hands Google a
    # random paragraph; duplicating it makes two results look identical.
    if not desc:
        add("warn", "no-meta-description",
            "no meta description — Google invents the SERP snippet, costing click-through")
    elif len(desc) < 50:
        add("info", "short-meta-description", f"meta description is {len(desc)} chars (thin snippet)")
    elif len(desc) > 165:
        add("info", "long-meta-description", f"meta description is {len(desc)} chars — truncated in results")
    if title and len(title) > cfg["title_max"]:
        add("info", "long-title", f"title is {len(title)} chars — truncated in search results")

    lds = _jsonld(html)
    if "__INVALID__" in lds:
        add("error", "invalid-jsonld", "JSON-LD does not parse — rich results silently stop")
    nodes = [n for d in lds if isinstance(d, dict) for n in _walk(d)]

    # A "Best X of 2026" page last touched years ago is the single most
    # credibility-destroying thing a review site can publish.
    ym = re.search(r"\b(20\d{2})\b", title or "")
    if ym:
        mod = next((str(n.get("dateModified")) for n in nodes if n.get("dateModified")), "")
        my = re.match(r"(\d{4})", mod)
        if my and int(ym.group(1)) - int(my.group(1)) >= 1:
            add("warn", "stale-year-claim",
                f"title claims {ym.group(1)} but dateModified is {mod[:10]}")

    # ── TRUST ───────────────────────────────────────────────────────────
    # Keep full precision: display prices are often ROUNDED for readability
    # ("$5,300") while the schema carries the exact figure ("5299.99"). That is
    # a legitimate presentation choice, not a deceptive-markup problem, so the
    # comparison below tolerates it — see _price_matches.
    visible_money = {float(v.replace(",", "")) for v in MONEY.findall(text)}
    # Does the page corroborate a rating ANYWHERE a reader can see — as words,
    # as star glyphs, or as explicit rating markup?
    corroborates_rating = bool(
        re.search(r"\b(review|rating)s?\b", text, re.I)
        or re.search(r"[\u2605\u2606\u2b50]", html)
        or re.search(r'(aria-label|class)="[^"]*\b(star|rating)', html, re.I)
        or re.search(r"\b\d\.\d\s*(/|out of)\s*5\b", text, re.I))
    for node in nodes:
        # Fabricated ratings. Precedent: aisleprompt shipped
        # "rec-010: null 17 fabricated AggregateRating counts". Structured data
        # claiming reviews the page cannot show is a manual-action risk.
        ar = node.get("aggregateRating")
        if isinstance(ar, dict):
            n = int(re.sub(r"\D", "", str(ar.get("reviewCount") or ar.get("ratingCount") or "0")) or 0)
            # CALIBRATION: the first version fired on 403 aisleprompt pages,
            # because it demanded the words "review"/"rating" in the text — but
            # the site displays ratings as STAR GLYPHS with no accompanying
            # count, which is a normal design choice, not fraud. Corroboration
            # now counts stars and rating markup too, so this only fires when
            # the schema asserts ratings the page corroborates NOWHERE.
            #
            # It remains a SIGNAL, not proof. Whether the numbers are real is a
            # database question (do review rows exist?), which a crawler cannot
            # answer — that belongs to the catalog-audit / PI agents. Precedent
            # that it is worth asking: aisleprompt shipped "rec-010: null 17
            # fabricated AggregateRating counts".
            if n > 0 and not corroborates_rating:
                add("error", "uncorroborated-rating",
                    f"JSON-LD asserts {n} ratings but the page shows no stars, count or "
                    "review text anywhere — verify the underlying rows exist")
        # Price parity: schema price must match what the reader sees, or the
        # markup is deceptive and the buy box is probably stale.
        offers = node.get("offers")
        for off in (offers if isinstance(offers, list) else [offers]):
            if isinstance(off, dict) and str(off.get("price") or "").strip():
                try:
                    want = float(re.sub(r"[^\d.]", "", str(off["price"])) or 0)
                except ValueError:
                    continue
                if want and visible_money and not _price_matches(want, visible_money):
                    add("warn", "price-mismatch",
                        f"JSON-LD offer price {off['price']} does not match any price shown "
                        f"on the page (nearest {min(visible_money, key=lambda v: abs(v - want)):.2f})")
                    break

    if title and LOCALITY_LEAK.search(title):
        add("warn", "locality-leak-title",
            f"title carries a US place name — usually prompt scenery, not editorial: {title[:70]!r}")
    for tok in cfg["leak_tokens"]:
        if tok.lower() in (title or "").lower():
            add("warn", "known-leak-token", f"title contains known-bad token {tok!r}: {title[:70]!r}")

    # Editorial with no byline or visible date reads as machine output.
    if cfg["editorial_re"] and re.search(cfg["editorial_re"], path):
        if not re.search(r'\b(by|author)\b[^<]{0,40}<|itemprop=["\']author|"author"\s*:', html, re.I):
            add("info", "no-byline", "editorial page shows no author attribution (E-E-A-T)")
        if not re.search(r'\b20\d{2}-\d{2}-\d{2}\b|datetime=|"date(Published|Modified)"', html, re.I):
            add("info", "no-visible-date", "editorial page shows no published/updated date")

    # ── RETENTION ───────────────────────────────────────────────────────
    origin = urllib.parse.urlsplit(url)
    internal = []
    for href in _attr_all(html, "a", "href"):
        # Unescape first: an href written `?a=1&amp;b=2` in HTML is the URL
        # `?a=1&b=2`. Requesting the literal `&amp;` fetches a DIFFERENT url,
        # which made 40 tracking variants of one page look like 40 pages.
        nxt = urllib.parse.urljoin(url, _html.unescape(href)).split("#")[0]
        p = urllib.parse.urlsplit(nxt)
        if p.netloc == origin.netloc and p.scheme in ("http", "https"):
            internal.append(nxt.rstrip("/") or "/")
    rec["links"] = sorted(set(internal))

    # A page with nowhere to go ends the session. specpicks already runs an
    # internal-link-densifier; this tells it where to work.
    if len(rec["links"]) < cfg["min_internal_links"]:
        add("warn", "dead-end-page",
            f"only {len(rec['links'])} distinct internal links — the reader has nowhere to go next",
            links=len(rec["links"]))

    # A monetized page with no buy path earns nothing and frustrates intent.
    if cfg["monetized_re"] and re.search(cfg["monetized_re"], path) and not CTA.search(html):
        add("error", "no-cta-on-monetized-page",
            "product/buying-guide page renders no affiliate or buy link")

    # ── MEDIA ───────────────────────────────────────────────────────────
    rec["imgs"] = [urllib.parse.urljoin(url, s) for s in _attr_all(html, "img", "src")
                   if s and not s.startswith("data:")]
    if not rec["imgs"]:
        add("info", "no-images", "page renders no <img>")
    missing_alt = len([1 for m in re.finditer(r"<img\b[^>]*>", html, re.I)
                       if not re.search(r"\balt\s*=", m.group(0), re.I)])
    if missing_alt:
        add("info", "img-missing-alt", f"{missing_alt} <img> without alt")

    if elapsed > cfg["slow_s"]:
        add("warn", "slow-page", f"{elapsed:.1f}s to first byte+body", secs=round(elapsed, 2))

    return rec


# ── Vision check for text baked into hero images (optional, costs money) ────

def check_hero_text(img_urls: list[str], timeout: int) -> list[dict]:
    """Flag hero images with marketing text baked into the pixels.

    Article 125's replacement hero was topically correct — a laptop — but was
    still an Amazon listing graphic plastered with "15.6\"", "1080P FULL HD"
    and "24GB RAM" badges. Correct subject, unusable as editorial art.

    There is no OCR on this host (no tesseract, and no sudo to install it) and
    no usable GPU, so a pixel heuristic would be guesswork. This asks the same
    Azure `vision-mini` deployment the recipe-image-verifier already uses.

    OFF by default: it costs money per image and that deployment's TPM quota
    was deliberately cut to control spend. Enable with --vision-hero-check.
    """
    ep = (os.getenv("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
    key = os.getenv("AZURE_OPENAI_KEY") or ""
    dep = os.getenv("AZURE_OPENAI_VISION_DEPLOYMENT", "vision-mini")
    if not ep or not key:
        return [dict(severity="info", kind="vision-check-skipped", url="",
                     message="AZURE_OPENAI_ENDPOINT/KEY not set — hero text check skipped")]
    api = f"{ep}/openai/deployments/{dep}/chat/completions?api-version=2024-10-21"
    out = []
    for iu in img_urls:
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(iu, headers={"User-Agent": BROWSER_UA}),
                timeout=timeout).read()
            if len(raw) > 4_000_000:
                continue
            payload = json.dumps({
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text":
                     "Does this image have marketing text, spec badges, feature callouts or "
                     "watermarks baked into the pixels? Editorial hero images should be clean "
                     'photography. Answer strictly as JSON: {"has_text": true|false, '
                     '"what": "at most 12 words"}'},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()}}]}],
                "max_tokens": 90, "temperature": 0}).encode()
            rq = urllib.request.Request(api, data=payload, method="POST",
                                        headers={"Content-Type": "application/json", "api-key": key})
            r = json.loads(urllib.request.urlopen(rq, timeout=timeout).read())
            j = json.loads(re.search(r"\{.*\}", r["choices"][0]["message"]["content"], re.S).group(0))
            if j.get("has_text"):
                out.append(dict(severity="warn", kind="text-baked-into-hero", url=iu,
                                message=f"hero has baked-in marketing text: {j.get('what', '')}"))
        except Exception as e:
            out.append(dict(severity="info", kind="vision-check-error", url=iu,
                            message=type(e).__name__))
        time.sleep(0.5)  # respect the reduced TPM quota
    return out


# ── Sitemap reconciliation ──────────────────────────────────────────────────

def sitemap_urls(base: str, timeout: int, cap: int, headers: dict | None = None) -> list[str]:
    """Every page <loc> in the sitemap index and its children."""
    st, _h, body, _e = _get(urllib.parse.urljoin(base, "/sitemap.xml"), timeout, headers=headers)
    if st != 200 or not body:
        return []
    locs = re.findall(r"<loc>([^<]+)</loc>", body)
    pages = [u for u in locs if not u.endswith(".xml")]
    for child in [u for u in locs if u.endswith(".xml")][:20]:
        s2, _h2, b2, _e2 = _get(child, timeout, headers=headers)
        if s2 == 200 and b2:
            pages.extend(u for u in re.findall(r"<loc>([^<]+)</loc>", b2) if not u.endswith(".xml"))
        if len(pages) > cap:
            break
    return pages[:cap]


# ── Crawl ───────────────────────────────────────────────────────────────────

def crawl(base: str, depth: int, max_pages: int, timeout: int, workers: int, cfg: dict) -> dict:
    start = base.rstrip("/") + "/"
    seen, queue, results = set(), collections.deque([(start, 0)]), []
    pages: dict[str, dict] = {}
    linked_from: dict[str, str] = {}
    img_pages: dict[str, str] = {}

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
            fetched = list(ex.map(
                lambda p: (p[0], p[1], *_get(p[0], timeout, headers=cfg.get("request_headers"))),
                batch))

        for url, d, status, headers, html, elapsed in fetched:
            rec = audit_page(url, html, status, headers, elapsed, cfg)
            pages[url] = rec
            # Route every finding to whatever linked to it. A 404 nobody can
            # trace back to a referring page is a finding nobody fixes.
            for f in rec["findings"]:
                if url in linked_from:
                    f.setdefault("linked_from", linked_from[url])
            results.extend(rec["findings"])

            for iu in rec["imgs"][: cfg["imgs_per_page"]]:
                img_pages.setdefault(iu, url)

            if d < depth and status == 200:
                for nxt in rec["links"]:
                    p = urllib.parse.urlsplit(nxt)
                    if re.search(r"\.(jpg|jpeg|png|webp|gif|svg|css|js|ico|pdf|zip)$", p.path, re.I):
                        continue
                    # API and machine endpoints are not reader-facing pages.
                    if re.match(r"^/(api|_next|static|assets|feed|rss)(/|$)", p.path, re.I):
                        continue
                    # Affiliate click-outs (/k/<slug> and the like) are never
                    # fetched: each request is logged as a click and earns a
                    # redirect to the retailer, not a page.
                    if _never_fetch(p.path, cfg):
                        continue
                    if nxt not in seen:
                        linked_from.setdefault(nxt, url)
                        queue.append((nxt, d + 1))

    # ── RETENTION: link-graph checks (need the whole crawl) ─────────────
    inbound: dict[str, set] = collections.defaultdict(set)
    for u, rec in pages.items():
        for t in rec["links"]:
            inbound[t].add(u)

    for u, rec in pages.items():
        if u != start and rec["is_html"] and rec["status"] == 200 and not inbound.get(u):
            results.append(dict(severity="warn", kind="orphan-page", url=u,
                                message="no other crawled page links here — readers and crawlers "
                                        "can reach it only from the sitemap"))
        if rec["redirect_to"]:
            src = linked_from.get(u, "")
            results.append(dict(
                severity="warn", kind="internal-link-to-redirect", url=u,
                linked_from=src, redirect_to=rec["redirect_to"],
                message=f"internal link hits a {rec['status']} → {rec['redirect_to'][:70]}"
                        + (f" (linked from {src})" if src else "")))

    # ── ACQUISITION: cross-page cannibalisation ─────────────────────────
    for field, kind, sev in (("title", "duplicate-title", "warn"),
                             ("h1", "duplicate-h1", "info"),
                             ("meta_desc", "duplicate-meta-description", "warn")):
        groups: dict[str, list] = {}
        for u, rec in pages.items():
            v = (rec.get(field) or "").strip()
            # A page whose canonical points ELSEWHERE is not competing — it has
            # already told Google which URL to keep. /shoppinglist?source=… has
            # 40 tracking variants that all canonicalise to /shoppinglist, and
            # counting them as duplicates was pure noise.
            canon = (rec.get("canonical") or "").strip()
            if canon and canon != u.rstrip("/") and canon != u:
                continue
            if v:
                groups.setdefault(v, []).append(u)
        for v, urls in groups.items():
            if len(urls) > cfg["dupe_title_cap"]:
                results.append(dict(severity=sev, kind=kind, url=urls[0], count=len(urls),
                                    sample=urls[:4],
                                    message=f"{field} {v[:56]!r} used on {len(urls)} pages — "
                                            "they compete against each other in search"))

    # ── ACQUISITION: sitemap vs reality ────────────────────────────────
    # The sitemaps are agent-generated, so nobody is checking them against the
    # live site. A sitemap full of 404s burns crawl budget on every visit.
    if cfg["check_sitemap"]:
        sm = [u for u in sitemap_urls(base, timeout, cfg["sitemap_cap"], headers=cfg.get("request_headers"))
              if not _never_fetch(urllib.parse.urlsplit(u).path, cfg)]
        step = max(1, len(sm) // cfg["sitemap_sample"]) if sm else 1
        sample = sm[::step][: cfg["sitemap_sample"]]
        if sample:
            with ThreadPoolExecutor(workers) as ex:
                probes = list(ex.map(lambda u: (u, *_get(u, timeout, method="HEAD",
                                                         headers=cfg.get("request_headers"))), sample))
            for su, st, _hd, _b, _e in probes:
                if st in (404, 410):
                    results.append(dict(severity="error", kind="sitemap-url-404", url=su,
                                        message=f"advertised in the sitemap but returns {st} — "
                                                "burns crawl budget and signals a broken site"))
                elif 300 <= st < 400:
                    results.append(dict(severity="warn", kind="sitemap-url-redirects", url=su,
                                        message=f"sitemap advertises a {st} redirect, not a final URL"))

    # ── MEDIA: reachability, with attribution ──────────────────────────
    imgs = list(img_pages)[: cfg["max_images"]]
    confirmed: dict[str, object] = {}
    if imgs:
        with ThreadPoolExecutor(workers) as ex:
            probes = list(ex.map(lambda i: (i, *_get(i, timeout, method="HEAD")), imgs))
        # RE-CONFIRM before declaring an image broken. The first pass fires N
        # concurrent HEADs at m.media-amazon.com and Amazon rate-limits that: a
        # spot check of 6 "broken" images found only 4 genuinely 404. A checker
        # that is a third false positives trains people to ignore it.
        for iu, st, _hd, _b, _e in probes:
            if st == 0 or st >= 400:
                time.sleep(cfg["recheck_delay_s"])
                st2, hd2, _b2, _e2 = _get(iu, timeout, method="GET", ua=BROWSER_UA)
                if st2 == 0 or st2 >= 400:
                    confirmed[iu] = st2 or hd2.get("_err", "error")
        for iu, st, hd, _b, _e in probes:
            if iu in confirmed:
                results.append(dict(severity="error", kind="broken-image", url=iu,
                                    on_page=img_pages.get(iu, ""),
                                    message=f"image returns {confirmed[iu]} "
                                            f"(rendered on {img_pages.get(iu, '?')})"))
            else:
                n = hd.get("Content-Length") or hd.get("content-length")
                if n and str(n).isdigit() and int(n) < cfg["tiny_img_bytes"]:
                    results.append(dict(severity="warn", kind="tiny-image", url=iu,
                                        on_page=img_pages.get(iu, ""),
                                        message=f"image is only {int(n) // 1024}KB on "
                                                f"{img_pages.get(iu, '?')} — renders soft in a lead card"))

    if cfg["vision_hero_check"]:
        results.extend(check_hero_text([i for i in imgs if i not in confirmed][: cfg["vision_max"]],
                                       timeout))

    return {"base": base, "pages_crawled": len(seen), "images_checked": len(imgs),
            "findings": results, "crawled": sorted(u.rstrip("/") for u in pages)}


# ── Recent articles — audited every run, crawled or not ─────────────────────
#
# 2026-11-12 article review: of 54 articles published in 30 days, the crawl
# reached a handful, so it never saw what a reader of the new ones saw:
# 17 heroes of 344-496 px, one product photo heroing four different
# articles, two heroes hotlinked from strangers' servers, a citation
# rendered as raw markdown, seven gift / checklist guides with no buy
# button, 5000-px recipe photos on cards. The checks below run on EVERY
# article published in the window (from the site DB, or the sitemap's
# lastmod), whether or not the BFS reached it. Knobs live in
# config/site-consistency-audit-config.json (storage copy overlays it);
# every kind has a severity there.

_CONFIG_KEY = "config/site-consistency-audit-config.json"
_REPO_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", _CONFIG_KEY)

ARTICLE_SEVERITY = {
    "raw-markdown": "error",                     # a reader sees `](https://…)` / `**`
    "no-buy-link-on-monetized-article": "error",  # lost revenue on a buying page
    "small-hero": "warn",                         # soft lead card, weak og:image
    "hero-offsite-host": "warn",                  # hotlinked: can vanish or be pulled
    "duplicate-hero": "warn",                     # one photo standing in for N subjects
    "heavy-image": "warn",                        # multi-MB / 5000-px images on a card
    "recent-article-noindex": "warn",             # the day's article slot earns nothing
    "recent-article-redirects": "warn",           # its canonical URL is a hop
    "article-audit-unavailable": "warn",          # the audit itself could not run
}

ARTICLE_DEFAULTS: dict = {
    "enabled": True,
    "window_days": 30,
    "max_articles": 200,
    # where the recent articles come from: "db", "sitemap" or "auto"
    # (db when dsn_env is set in the environment, else the sitemap)
    "source": "auto",
    "dsn_env": "",
    # SELECT returning at least slug; optional title, category, bucket,
    # hero_image_url, published_at. Named params: %(days)s, %(limit)s.
    "query": "",
    "url_template": "",            # "https://<host>/<route>/{slug}"
    "sitemap_path_re": "",          # sitemap source: article URL paths
    # The article region of a page: first <article>…</article>, else <main>.
    "content_re": "",
    "imgs_per_article": 24,
    "image_probe_bytes": 65536,     # ranged GET: enough for any image header
    "hero_min_width": 1000,
    "hero_min_bytes": 40000,
    "allowed_image_hosts": [],      # hero hosts; empty switches the check off
    "duplicate_hero_min": 2,        # articles sharing one hero before it fires
    # path rewrites applied before comparing hero URLs, so CDN size variants
    # of one picture ("…/81ubl5FITQL._AC_SL1500_.jpg") compare equal
    "image_url_normalize": [[r"\._[A-Za-z0-9_,]+_(?=\.[A-Za-z0-9]+$)", ""]],
    "max_image_bytes": 1_500_000,   # the review's bar: 1.5-1.9 MB card images
    "max_image_width": 3000,
    # monetized = a listed bucket/category, a title match, or at least
    # monetized_min_product_links links to product pages
    "monetized_buckets": [],
    "monetized_title_re": "",
    "product_link_re": "",
    "monetized_min_product_links": 1,
    # a direct buy link: matched against the path of an own-host link, the
    # full URL of any other
    "buy_link_re": r"^/(?:k|go)/[A-Za-z0-9-]+|amazon\.[a-z.]+/(?:dp|gp/product)/|amzn\.to/|ebay\.[a-z.]+/itm/",
    "severity": dict(ARTICLE_SEVERITY),
}


def _deep_merge(base: dict, over: dict) -> dict:
    """Dicts merge key by key (recursively); anything else replaces."""
    out = dict(base or {})
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None, storage=None) -> dict:
    """The audit config: repo default, the storage copy laid over it, then an
    explicit --config file. An unreadable repo default or storage copy is
    skipped; an explicit --config that does not load is reported on stderr."""
    cfg: dict = {}
    try:
        with open(_REPO_CONFIG) as fh:
            cfg = json.load(fh)
    except Exception as e:
        print(f"[site-audit] repo config unreadable ({e}); built-in defaults only", file=sys.stderr)
    if storage is not None:
        try:
            cfg = _deep_merge(cfg, storage.read_json(_CONFIG_KEY) or {})
        except Exception:
            pass
    if path:
        try:
            with open(path) as fh:
                cfg = _deep_merge(cfg, json.load(fh))
        except Exception as e:
            print(f"[site-audit] --config {path} not loaded: {e}", file=sys.stderr)
    return cfg


def _bare_host(netloc: str) -> str:
    h = (netloc or "").lower().split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


def site_config(cfg: dict, base: str) -> dict:
    """Settings for the site at `base`: the config's `default` block with the
    `sites` block whose key is the base's host (www. ignored) laid over it.
    `article_audit` is filled from ARTICLE_DEFAULTS and is configured only
    when the site names a source (query + url_template, or sitemap_path_re)."""
    host = _bare_host(urllib.parse.urlsplit(base).netloc)
    site = copy.deepcopy((cfg or {}).get("default") or {})
    matched = ""
    for key, block in ((cfg or {}).get("sites") or {}).items():
        if _bare_host(key) == host:
            site = _deep_merge(site, copy.deepcopy(block or {}))
            matched = key
            break
    acfg = _deep_merge(copy.deepcopy(ARTICLE_DEFAULTS), site.get("article_audit") or {})
    acfg["configured"] = bool((acfg.get("query") and acfg.get("url_template"))
                              or acfg.get("sitemap_path_re"))
    site["article_audit"] = acfg
    site["matched_site"] = matched
    site.setdefault("request_headers", {})
    site.setdefault("never_fetch_path_re", DEFAULT_NEVER_FETCH)
    return site


def _art_finding(acfg: dict, kind: str, url: str, message: str, **extra) -> dict:
    sev = (acfg.get("severity") or {}).get(kind) or ARTICLE_SEVERITY.get(kind, "warn")
    return dict(severity=sev, kind=kind, url=url, message=message, recent_article=True, **extra)


# ── where the recent articles come from ────────────────────────────────────

def recent_articles_from_db(acfg: dict, connect=None) -> list[dict]:
    """Rows of the site's own `query`, in a READ-ONLY session (the audit
    never writes). `connect(dsn)` is injectable for tests."""
    dsn = os.environ.get(acfg.get("dsn_env") or "", "")
    if not dsn:
        raise RuntimeError(f"${acfg.get('dsn_env')} is not set")
    if connect is None:
        import psycopg2  # optional dependency, only for the db source

        def connect(d):
            c = psycopg2.connect(d, options="-c default_transaction_read_only=on "
                                            "-c statement_timeout=30000")
            c.set_session(readonly=True, autocommit=True)
            return c
    conn = connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(acfg["query"], {"days": int(acfg["window_days"]), "limit": int(acfg["max_articles"])})
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    out = []
    for r in rows:
        slug = str(r.get("slug") or "").strip()
        if not slug:
            continue
        out.append({"url": acfg["url_template"].replace("{slug}", slug), "slug": slug,
                    "title": r.get("title") or "",
                    "categories": [str(r.get(k) or "") for k in ("category", "bucket") if r.get(k)],
                    "db_hero": r.get("hero_image_url") or "",
                    "published_at": str(r.get("published_at") or "")[:19]})
    return out


def recent_articles_from_sitemap(base: str, acfg: dict, timeout: int, headers: dict | None = None,
                                 now: float | None = None) -> list[dict]:
    """Sitemap entries whose path matches `sitemap_path_re` and whose
    <lastmod> falls inside the window (lastmod is the last change, so a
    re-edited old article counts too — the db source is exact)."""
    rx = re.compile(acfg["sitemap_path_re"])
    cutoff = (now if now is not None else time.time()) - int(acfg["window_days"]) * 86400
    st, _h, body, _e = _get(urllib.parse.urljoin(base, "/sitemap.xml"), timeout, headers=headers)
    if st != 200 or not body:
        raise RuntimeError(f"sitemap.xml returned {st}")
    docs = [body]
    for child in re.findall(r"<sitemap>\s*<loc>([^<]+)</loc>", body)[:20]:
        s2, _h2, b2, _e2 = _get(child.strip(), timeout, headers=headers)
        if s2 == 200 and b2:
            docs.append(b2)
    out, seen = [], set()
    for doc in docs:
        for block in re.findall(r"<url>(.*?)</url>", doc, re.S):
            loc = (re.search(r"<loc>([^<]+)</loc>", block) or [None, ""])[1].strip()
            mod = (re.search(r"<lastmod>([^<]+)</lastmod>", block) or [None, ""])[1].strip()
            if not loc or loc in seen or not rx.search(urllib.parse.urlsplit(loc).path):
                continue
            try:
                ts = time.mktime(time.strptime(mod[:10], "%Y-%m-%d"))
            except ValueError:
                continue
            if ts >= cutoff:
                seen.add(loc)
                out.append({"url": loc, "slug": loc.rstrip("/").rsplit("/", 1)[-1], "title": "",
                            "categories": [], "db_hero": "", "published_at": mod[:19]})
    return out[: int(acfg["max_articles"])]


def recent_articles(base: str, acfg: dict, timeout: int, headers: dict | None = None,
                    connect=None) -> tuple[list[dict], str, str]:
    """(articles, source, error). `auto` prefers the DB when its DSN env is
    set and falls back to the sitemap when the DB source fails."""
    src = (acfg.get("source") or "auto").lower()
    errs = []
    if src in ("db", "auto") and acfg.get("query") and acfg.get("url_template") \
            and (src == "db" or os.environ.get(acfg.get("dsn_env") or "")):
        try:
            return recent_articles_from_db(acfg, connect=connect), "db", ""
        except Exception as e:  # noqa: BLE001
            errs.append(f"db: {type(e).__name__}: {str(e)[:120]}")
    if src in ("sitemap", "auto") and acfg.get("sitemap_path_re"):
        try:
            return recent_articles_from_sitemap(base, acfg, timeout, headers), "sitemap", "; ".join(errs)
        except Exception as e:  # noqa: BLE001
            errs.append(f"sitemap: {type(e).__name__}: {str(e)[:120]}")
    return [], "", "; ".join(errs) or "no usable source (dsn env unset and no sitemap_path_re)"


# ── what a page shows ───────────────────────────────────────────────────────

# Markdown the renderer should have turned into HTML, found in VISIBLE text
# (code blocks excluded). Calibrated on the 54 articles of the 2026-11-12
# review: the only hit was the 4475 citation whose link text held brackets.
RAW_MARKDOWN = (
    ("heading-hash", re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S", re.M)),
    ("bold-asterisks", re.compile(r"\*\*[^*\n]{2,80}\*\*")),
    ("md-link", re.compile(r"\]\((?:https?://|/)[^)\s]+\)")),
    ("table-pipes", re.compile(r"\|[ \t]*:?-{3,}")),
    ("code-fence", re.compile(r"```")),
)

_BLOCK_TAG = re.compile(
    r"<(?:/?(?:p|div|h[1-6]|li|ul|ol|tr|table|section|article|header|footer|blockquote|figure|"
    r"figcaption|dt|dd|nav|aside|main)\b[^>]*|br\s*/?)>", re.I)


def _content_region(html: str, content_re: str = "") -> str:
    """The article region: `content_re`'s first group, else the first
    <article>…</article>, else <main>, else the whole page."""
    pats = ([content_re] if content_re else []) + [r"<article\b[^>]*>(.*)</article>",
                                                    r"<main\b[^>]*>(.*)</main>"]
    for p in pats:
        try:
            m = re.search(p, html or "", re.S | re.I)
        except re.error:
            continue
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return html or ""


def visible_text_lines(html: str, drop_tables: bool = False) -> str:
    """Visible text with block elements on their own lines; scripts, styles
    and code (<pre>, <code>) removed, entities decoded. drop_tables removes
    rendered <table>s too (a "#" column header is not a markdown heading)."""
    h = re.sub(r"<(script|style|noscript|template|pre|code)\b[^>]*>.*?</\1\s*>", " ", html or "",
               flags=re.S | re.I)
    if drop_tables:
        h = re.sub(r"<table\b[^>]*>.*?</table\s*>", "\n", h, flags=re.S | re.I)
    h = _BLOCK_TAG.sub("\n", h)
    t = _html.unescape(TAG.sub(" ", h))
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    return "\n".join(line.strip() for line in t.split("\n") if line.strip())


def find_raw_markdown(text: str, heading_text: str | None = None) -> list[tuple[str, str]]:
    """[(kind, snippet)] of markdown syntax left in visible text.
    `heading_text` (the same text without rendered tables) is what the
    heading check reads, when given: a table whose first header cell is "#"
    starts a line with "# Recipe" (2026-11-12 Whole30 article)."""
    out = []
    for kind, rx in RAW_MARKDOWN:
        src = heading_text if (kind == "heading-hash" and heading_text is not None) else text
        m = rx.search(src or "")
        if m:
            i = max(0, m.start() - 50)
            out.append((kind, (src[i:m.end() + 30]).replace("\n", " ⏎ ")))
    return out


def _link_target(href: str, page_url: str) -> str:
    """An href as the buy/product patterns see it: the path (+query) when it
    is on the page's own host, else the absolute URL."""
    absu = urllib.parse.urljoin(page_url, _html.unescape(href or "").strip())
    p, own = urllib.parse.urlsplit(absu), urllib.parse.urlsplit(page_url)
    if p.scheme in ("http", "https") and _bare_host(p.netloc) == _bare_host(own.netloc):
        return p.path + (f"?{p.query}" if p.query else "")
    return absu


def _rx(pat: str):
    try:
        return re.compile(pat, re.I) if pat else None
    except re.error:
        return None


def article_page_facts(article: dict, url: str, html: str, acfg: dict, cfg: dict | None = None) -> dict:
    """Everything the article checks need from one fetched page. Pure."""
    region = _content_region(html, acfg.get("content_re") or "")
    buy_rx, prod_rx = _rx(acfg.get("buy_link_re") or ""), _rx(acfg.get("product_link_re") or "")
    buys, prods = set(), set()
    for href in _attr_all(region, "a", "href"):
        t = _link_target(href, url)
        key = re.split(r"[?#]", t, maxsplit=1)[0].rstrip("/").lower()
        if buy_rx and buy_rx.search(t):
            buys.add(key)
        elif prod_rx and prod_rx.search(t):
            prods.add(key)
    imgs = []
    for s in _attr_all(region, "img", "src"):
        if not s or s.startswith("data:"):
            continue
        u = urllib.parse.urljoin(url, _html.unescape(s))
        p = urllib.parse.urlsplit(u)
        if _bare_host(p.netloc) == _bare_host(urllib.parse.urlsplit(url).netloc) \
                and _never_fetch(p.path, cfg or {}):
            continue
        if u not in imgs:
            imgs.append(u)
    og = _meta(html, "og:image")
    title = article.get("title") or ""
    if not title:
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", html or "", re.S | re.I)
        title = _html.unescape(TAG.sub("", h1.group(1))).strip() if h1 else ""
    return {
        "url": url, "title": title, "categories": list(article.get("categories") or []),
        "hero": urllib.parse.urljoin(url, og) if og else (article.get("db_hero") or ""),
        "db_hero": article.get("db_hero") or "",
        "imgs": imgs[: int(acfg.get("imgs_per_article") or 24)],
        "raw_markdown": find_raw_markdown(visible_text_lines(region),
                                          heading_text=visible_text_lines(region, drop_tables=True)),
        "buy_links": len(buys), "product_links": len(prods),
        "noindex": "noindex" in _meta(html, "robots").lower(),
    }


# ── images ──────────────────────────────────────────────────────────────────

_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def image_dims(data: bytes):
    """(width, height) from an image's leading bytes — PNG, GIF, JPEG, WebP —
    or None. Falls back to PIL when it is installed (AVIF etc.)."""
    d = data or b""
    try:
        if d[:8] == b"\x89PNG\r\n\x1a\n" and len(d) >= 24:
            return int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")
        if d[:6] in (b"GIF87a", b"GIF89a") and len(d) >= 10:
            return int.from_bytes(d[6:8], "little"), int.from_bytes(d[8:10], "little")
        if d[:4] == b"RIFF" and d[8:12] == b"WEBP":
            chunk = d[12:16]
            if chunk == b"VP8 " and len(d) >= 30:
                return (int.from_bytes(d[26:28], "little") & 0x3FFF,
                        int.from_bytes(d[28:30], "little") & 0x3FFF)
            if chunk == b"VP8L" and len(d) >= 25:
                b = d[21:25]
                return (1 + (((b[1] & 0x3F) << 8) | b[0]),
                        1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6)))
            if chunk == b"VP8X" and len(d) >= 30:
                return 1 + int.from_bytes(d[24:27], "little"), 1 + int.from_bytes(d[27:30], "little")
        if d[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(d):
                if d[i] != 0xFF:
                    i += 1
                    continue
                m = d[i + 1]
                if m == 0xFF:                       # fill byte
                    i += 1
                    continue
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:   # no length field
                    i += 2
                    continue
                if m in (0xD9, 0xDA):               # end / start of scan: no SOF ahead
                    break
                if m in _JPEG_SOF:
                    return int.from_bytes(d[i + 7:i + 9], "big"), int.from_bytes(d[i + 5:i + 7], "big")
                i += 2 + int.from_bytes(d[i + 2:i + 4], "big")
    except Exception:
        pass
    try:
        import io
        from PIL import Image  # optional
        return Image.open(io.BytesIO(d)).size
    except Exception:
        return None


def probe_image(url: str, timeout: int, full: bool = False, probe_bytes: int = 65536,
                max_bytes: int = 25_000_000) -> dict:
    """{url, status, bytes, w, h[, sha1]} for one image, following redirects.

    full=False reads only the leading `probe_bytes` (a ranged GET) and takes
    the total size from Content-Range / Content-Length; full=True downloads
    the file so its bytes can be hashed (duplicate heroes at different URLs).
    A failed probe reports its status only — the crawl's broken-image check
    owns reachability, so an unknown size is never a finding here."""
    out: dict = {"url": url}
    hdrs = {"User-Agent": BROWSER_UA}
    if not full:
        hdrs["Range"] = f"bytes=0-{max(int(probe_bytes), 1024) - 1}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=timeout) as r:
            status = r.status
            hd = {k.lower(): v for k, v in r.headers.items()}
            data = r.read(max_bytes if full else max(int(probe_bytes), 1024))
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        return out
    except Exception as e:  # noqa: BLE001
        out.update(status=0, error=type(e).__name__)
        return out
    out["status"] = status
    total = None
    m = re.search(r"/(\d+)\s*$", hd.get("content-range", ""))
    if m:
        total = int(m.group(1))
    elif str(hd.get("content-length", "")).isdigit():
        total = int(hd["content-length"])
    if full and len(data) < max_bytes:
        total = len(data)
        import hashlib
        out["sha1"] = hashlib.sha1(data).hexdigest()
    out["bytes"] = total
    dims = image_dims(data)
    if dims is None and not full and total and total <= max_bytes:
        # A header past the probe window (a big EXIF block): read it all once.
        return probe_image(url, timeout, full=True, probe_bytes=probe_bytes, max_bytes=max_bytes)
    if dims:
        out["w"], out["h"] = dims
    return out


def _host_allowed(url: str, allowed: list) -> bool:
    host = (urllib.parse.urlsplit(url).netloc or "").lower().split(":")[0]
    for a in allowed or []:
        a = str(a).lower().lstrip("*").lstrip(".")
        if a and (host == a or host.endswith("." + a)):
            return True
    return False


def _norm_image_url(url: str, rules: list) -> str:
    p = urllib.parse.urlsplit(url)
    path = p.path
    for rule in rules or []:
        try:
            path = re.sub(rule[0], rule[1], path)
        except (re.error, IndexError, TypeError):
            continue
    return f"{(p.netloc or '').lower()}{path}"


def _kb(n) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{max(n // 1000, 0)} KB"


# ── the checks ──────────────────────────────────────────────────────────────

def evaluate_articles(facts: list[dict], probes: dict, acfg: dict) -> list[dict]:
    """Findings for the recent articles from their page facts and the image
    probes ({url: probe_image() result}). Pure."""
    out: list[dict] = []
    allowed = acfg.get("allowed_image_hosts") or []
    min_w, min_b = int(acfg.get("hero_min_width") or 0), int(acfg.get("hero_min_bytes") or 0)
    title_rx = _rx(acfg.get("monetized_title_re") or "")
    buckets = {str(b).lower() for b in acfg.get("monetized_buckets") or []}
    min_prod = int(acfg.get("monetized_min_product_links") or 1)

    for f in facts:
        u = f["url"]
        if f.get("raw_markdown"):
            kinds = ", ".join(k for k, _s in f["raw_markdown"])
            out.append(_art_finding(acfg, "raw-markdown", u,
                                    f"markdown the renderer did not convert ({kinds}): "
                                    f"…{f['raw_markdown'][0][1][:140]}…"))
        if f.get("noindex"):
            out.append(_art_finding(acfg, "recent-article-noindex", u,
                                    "published in the audit window but noindex — it cannot earn "
                                    "search traffic, so the proposer spent a daily article slot on it"))
        why = []
        cats = {c.lower() for c in f.get("categories") or [] if c}
        if cats & buckets:
            why.append("bucket " + "/".join(sorted(cats & buckets)))
        if title_rx and title_rx.search(f.get("title") or ""):
            why.append("title")
        if acfg.get("product_link_re") and f.get("product_links", 0) >= min_prod:
            why.append(f"{f['product_links']} product-page links")
        if why and f.get("buy_links", 0) == 0:
            out.append(_art_finding(acfg, "no-buy-link-on-monetized-article", u,
                                    f"monetized ({', '.join(why)}) but the article renders no direct "
                                    f"buy link — products are named only through their pages",
                                    product_links=f.get("product_links", 0)))
        hero = f.get("hero") or ""
        if hero:
            pr = probes.get(hero) or {}
            w, b = pr.get("w"), pr.get("bytes")
            small = (w is not None and w < min_w) or (b is not None and b < min_b)
            if small:
                dims = f"{w}x{pr.get('h')}" if w is not None else "?x?"
                out.append(_art_finding(acfg, "small-hero", u,
                                        f"hero {dims}, {_kb(b or 0)} (min {min_w} px wide, "
                                        f"{_kb(min_b)}) — renders soft as the lead card and og:image",
                                        hero=hero, width=w, bytes=b))
        if allowed:
            for h in dict.fromkeys(x for x in (hero, f.get("db_hero")) if x):
                if not _host_allowed(h, allowed):
                    out.append(_art_finding(acfg, "hero-offsite-host", u,
                                            f"hero is hotlinked from {urllib.parse.urlsplit(h).netloc} "
                                            "(not an allowed image host) — mirror or replace it",
                                            hero=h))

    # duplicate heroes: same normalized URL, or same bytes at different URLs
    min_dup = max(int(acfg.get("duplicate_hero_min") or 2), 2)
    rules = acfg.get("image_url_normalize") or []
    groups: dict = collections.defaultdict(set)
    for f in facts:
        hero = f.get("hero") or ""
        if not hero:
            continue
        groups[("url", _norm_image_url(hero, rules))].add(f["url"])
        sha = (probes.get(hero) or {}).get("sha1")
        if sha:
            groups[("sha1", sha)].add(f["url"])
    shared: dict[str, set] = collections.defaultdict(set)
    for members in groups.values():
        if len(members) >= min_dup:
            for m in members:
                shared[m] |= members - {m}
    hero_of = {f["url"]: f.get("hero") for f in facts}
    for u in sorted(shared):
        others = sorted(shared[u])
        out.append(_art_finding(acfg, "duplicate-hero", u,
                                f"hero is also the hero of {len(others)} other recent article(s): "
                                + ", ".join(o.rsplit("/", 1)[-1] for o in others[:4]),
                                hero=hero_of.get(u), shared_with=others))

    # heavy images: once per image, naming the pages that render it
    max_b, max_w = int(acfg.get("max_image_bytes") or 0), int(acfg.get("max_image_width") or 0)
    pages_of: dict[str, list] = collections.defaultdict(list)
    for f in facts:
        for iu in dict.fromkeys([f.get("hero") or ""] + list(f.get("imgs") or [])):
            if iu:
                pages_of[iu].append(f["url"])
    for iu, pages in pages_of.items():
        pr = probes.get(iu) or {}
        w, b = pr.get("w"), pr.get("bytes")
        if (max_b and b and b > max_b) or (max_w and w and w > max_w):
            dims = f"{w}x{pr.get('h')}" if w else "?x?"
            out.append(_art_finding(acfg, "heavy-image", iu,
                                    f"image {dims}, {_kb(b or 0)} (max {max_w} px / {_kb(max_b)}) "
                                    f"on {len(pages)} recent article(s), e.g. {pages[0]}",
                                    on_page=pages[0], pages=len(pages), width=w, bytes=b))
    return out


def audit_recent_articles(base: str, site: dict, cfg: dict, timeout: int, workers: int,
                          crawled: set | None = None, connect=None) -> tuple[list[dict], dict]:
    """Audit every article published in the window (crawled or not).

    Returns (findings, summary). A recent article the BFS did not reach also
    gets every page-level check (audit_page); one it did reach is not
    re-reported. Read-only: GETs and a read-only SELECT."""
    acfg = site["article_audit"]
    headers = site.get("request_headers") or {}
    summary = {"source": "", "window_days": acfg.get("window_days"), "articles": 0,
               "audited": 0, "not_in_crawl": 0, "error": ""}
    arts, source, err = recent_articles(base, acfg, timeout, headers=headers, connect=connect)
    summary.update(source=source, articles=len(arts), error=err)
    findings: list[dict] = []
    if not arts:
        if err:
            findings.append(_art_finding(acfg, "article-audit-unavailable", base,
                                         f"recent-article audit did not run: {err}"))
        return findings, summary

    crawled_norm = {c.rstrip("/") for c in (crawled or set())}
    todo = [a for a in arts if not _never_fetch(urllib.parse.urlsplit(a["url"]).path, site)]
    with ThreadPoolExecutor(max(workers, 1)) as ex:
        fetched = list(ex.map(lambda a: (a, *_get(a["url"], timeout, headers=headers)), todo))

    facts = []
    for a, status, hd, html, elapsed in fetched:
        url = a["url"]
        if url.rstrip("/") not in crawled_norm:
            summary["not_in_crawl"] += 1
            rec = audit_page(url, html, status, hd, elapsed, cfg)
            for f in rec["findings"]:
                f["recent_article"] = True
            findings.extend(rec["findings"])
        if 300 <= status < 400:
            loc = urllib.parse.urljoin(url, hd.get("Location") or hd.get("location") or "")
            findings.append(_art_finding(acfg, "recent-article-redirects", url,
                                         f"recently published article answers {status} → {loc[:90]} "
                                         "(its URL in the sitemap/canonical is a hop)", redirect_to=loc))
            continue
        if status != 200 or not html:
            continue
        facts.append(article_page_facts(a, url, html, acfg, site))
    summary["audited"] = len(facts)

    heroes = list(dict.fromkeys(f["hero"] for f in facts if f.get("hero")))
    hero_set = set(heroes)
    others = list(dict.fromkeys(i for f in facts for i in f["imgs"] if i not in hero_set))
    pb = int(acfg.get("image_probe_bytes") or 65536)
    probes: dict = {}
    with ThreadPoolExecutor(max(workers, 1)) as ex:
        for u, pr in zip(heroes, ex.map(lambda x: probe_image(x, timeout, full=True, probe_bytes=pb), heroes)):
            probes[u] = pr
        for u, pr in zip(others, ex.map(lambda x: probe_image(x, timeout, full=False, probe_bytes=pb), others)):
            probes[u] = pr
    summary["images_probed"] = len(probes)
    findings.extend(evaluate_articles(facts, probes, acfg))
    return findings, summary


# ── Run-over-run regression diff ────────────────────────────────────────────

def diff_against_last(rep: dict, state_dir: str, scope: str = "") -> dict:
    """Compare this run to the previous one for the same base URL.

    The absolute finding count is mostly noise — a site always has a tail of
    known warts. What matters is what CHANGED since the agents last shipped: a
    kind appearing for the first time, or a count that jumped, is the signal
    this tool exists to produce. Without this, every run reads the same and
    nobody notices the morning a deploy broke something.
    """
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, re.sub(r"\W+", "-", rep["base"]).strip("-") + ".last.json")

    now = collections.Counter(f["kind"] for f in rep["findings"])
    prev, prev_at, prev_scope = {}, None, None
    if os.path.exists(path):
        try:
            d = json.load(open(path))
            prev, prev_at, prev_scope = d.get("by_kind", {}), d.get("at"), d.get("scope")
        except Exception:
            pass

    # Only compare like-for-like. A depth-2/25-page smoke run against a
    # depth-5/240-page run reported "broken-image 1→52, price-mismatch 6→301"
    # as a regression when nothing had regressed at all — the second run simply
    # looked at ten times more of the site. Comparing across scopes would make
    # every parameter change look like an outage and train people to ignore the
    # one signal this tool exists to give.
    if prev and prev_scope and scope and prev_scope != scope:
        json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scope": scope,
                   "by_kind": dict(now), "total": len(rep["findings"])}, open(path, "w"), indent=1)
        return {"compared_to": None, "new_kinds": [], "resolved_kinds": [], "regressed": {},
                "improved": {}, "is_regression": False,
                "note": f"previous run had scope {prev_scope!r}, this one {scope!r} — "
                        "not comparable, baseline reset"}

    new_kinds = sorted(k for k in now if k not in prev)
    worse = sorted(k for k in now if prev.get(k, 0) and now[k] > prev[k])
    better = sorted(k for k in now if prev.get(k, 0) > now[k])
    diff = {"compared_to": prev_at,
            "new_kinds": new_kinds,
            "resolved_kinds": sorted(k for k in prev if k not in now),
            "regressed": {k: {"was": prev.get(k, 0), "now": now[k]} for k in worse},
            "improved": {k: {"was": prev[k], "now": now[k]} for k in better},
            "is_regression": bool(new_kinds or worse)}

    json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scope": scope,
               "by_kind": dict(now), "total": len(rep["findings"])},
              open(path, "w"), indent=1)
    return diff


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--depth", type=int, default=5)
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-words", type=int, default=180)
    ap.add_argument("--slow-s", type=float, default=8.0)
    ap.add_argument("--min-internal-links", type=int, default=3)
    ap.add_argument("--title-max", type=int, default=65)
    ap.add_argument("--no-sitemap-check", action="store_true")
    ap.add_argument("--sitemap-sample", type=int, default=60)
    ap.add_argument("--vision-hero-check", action="store_true",
                    help="ask Azure vision whether heroes have baked-in text (costs money)")
    ap.add_argument("--vision-max", type=int, default=25)
    ap.add_argument("--leak-token", action="append", default=[],
                    help="known-bad token that must not appear in a title (repeatable)")
    ap.add_argument("--monetized-re",
                    default=r"^/(product|products|buying-guide|buying-guides|kitchen|best)/")
    ap.add_argument("--editorial-re", default=r"^/(articles|blog|reviews|news)/")
    ap.add_argument("--state-dir", default="/tmp/reusable-agents-logs/site-audit-state")
    ap.add_argument("--config", help="extra audit config JSON laid over "
                                     "config/site-consistency-audit-config.json (+ its storage copy)")
    ap.add_argument("--no-storage-config", action="store_true",
                    help="ignore the storage copy of the audit config")
    ap.add_argument("--no-article-audit", action="store_true",
                    help="skip the recent-article audit (crawl only)")
    ap.add_argument("--article-window-days", type=int,
                    help="override the site config's article_audit.window_days")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    storage = None
    if not a.no_storage_config:
        try:
            from framework.core.storage import get_storage
            storage = get_storage()
        except (Exception, SystemExit):  # no backend here: the repo default applies
            storage = None
    site = site_config(load_config(a.config, storage), a.base)
    acfg = site["article_audit"]
    if a.article_window_days:
        acfg["window_days"] = a.article_window_days
    run_articles = bool(acfg.get("enabled") and acfg.get("configured") and not a.no_article_audit)

    cfg = dict(min_words=a.min_words, slow_s=a.slow_s, dupe_title_cap=3, imgs_per_page=6,
               max_images=500, tiny_img_bytes=12_000, recheck_delay_s=0.4,
               min_internal_links=a.min_internal_links, title_max=a.title_max,
               check_sitemap=not a.no_sitemap_check, sitemap_cap=5000,
               sitemap_sample=a.sitemap_sample, vision_hero_check=a.vision_hero_check,
               vision_max=a.vision_max, leak_tokens=a.leak_token,
               monetized_re=a.monetized_re, editorial_re=a.editorial_re,
               request_headers=site.get("request_headers") or {},
               never_fetch_path_re=site.get("never_fetch_path_re", DEFAULT_NEVER_FETCH))

    rep = crawl(a.base, a.depth, a.max_pages, a.timeout, a.workers, cfg)
    crawled = set(rep.pop("crawled", None) or [])
    scope = f"d{a.depth}-p{a.max_pages}"
    if run_articles:
        # Every article of the window, crawled or not. The crawl's own pages
        # are passed in so a page is not reported twice.
        art_findings, rep["recent_articles"] = audit_recent_articles(
            a.base, site, cfg, a.timeout, a.workers, crawled=crawled)
        rep["findings"].extend(art_findings)
        # A run that also audits the window measures more than a crawl-only
        # run did, so it gets its own baseline instead of reporting the new
        # kinds as a regression on its first day.
        scope += f"-a{acfg['window_days']}"
    by_kind = collections.Counter(f["kind"] for f in rep["findings"])
    by_sev = collections.Counter(f["severity"] for f in rep["findings"])
    rep["summary"] = {"by_kind": dict(by_kind), "by_severity": dict(by_sev)}
    rep["diff"] = diff_against_last(rep, a.state_dir, scope=scope)

    ra = rep.get("recent_articles")
    print(f"\n{a.base}  —  {rep['pages_crawled']} pages, {rep['images_checked']} images, "
          f"{len(rep['findings'])} findings")
    if ra:
        print(f"  recent articles ({ra['source'] or 'no source'}, {ra['window_days']} d): "
              f"{ra['audited']}/{ra['articles']} audited, {ra['not_in_crawl']} not reached by the crawl"
              + (f" — {ra['error']}" if ra.get("error") else ""))
    print(f"  severity: {dict(by_sev)}")
    for kind, n in by_kind.most_common():
        s = next(f for f in rep["findings"] if f["kind"] == kind)
        print(f"  {n:>5}  {kind:<28} e.g. {s['url'][:56]}")
        print(f"         {s['message'][:112]}")

    d = rep["diff"]
    print(f"\n  ── vs previous run ({d['compared_to'] or 'none yet'}) ──")
    if d["new_kinds"]:
        print("  NEW      " + ", ".join(d["new_kinds"]))
    if d["regressed"]:
        print("  WORSE    " + ", ".join(
            "{} {}→{}".format(k, v["was"], v["now"]) for k, v in d["regressed"].items()))
    if d["improved"]:
        print("  BETTER   " + ", ".join(
            "{} {}→{}".format(k, v["was"], v["now"]) for k, v in d["improved"].items()))
    if d["resolved_kinds"]:
        print("  RESOLVED " + ", ".join(d["resolved_kinds"]))
    if d.get("note"):
        print("  " + d["note"])
    elif not any((d["new_kinds"], d["regressed"], d["improved"], d["resolved_kinds"])):
        print("  no change" if d["compared_to"] else "  (baseline stored)")

    if a.out:
        with open(a.out, "w") as fh:
            json.dump(rep, fh, indent=1)
        print(f"\n  full report -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
