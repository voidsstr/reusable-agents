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
import json
import os
import re
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
PLACEHOLDER = re.compile(
    r"(lorem ipsum|TODO:|FIXME:|\[object Object\]|"
    r"\bundefined\s*(?:px|%|,|\)|</)|=\s*undefined\b|"
    r"\bNaN\b|null null|\{\{[a-z_]+\}\}|<no title>|\bUntitled\b)", re.I)

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


def _get(url: str, timeout: int, method: str = "GET", ua: str = UA):
    """Fetch one URL WITHOUT following redirects.

    Returns (status, headers, body_text, elapsed_s). A 3xx carries its
    Location header so the caller can record the hop.
    """
    t0 = time.time()
    try:
        rq = urllib.request.Request(url, headers={"User-Agent": ua}, method=method)
        with _OPENER.open(rq, timeout=timeout) as r:
            body = b"" if method == "HEAD" else r.read(800_000)
            return r.status, dict(r.headers), body.decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        hd = dict(getattr(e, "headers", {}) or {})
        return e.code, hd, "", time.time() - t0
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


def _meta(html: str, name: str) -> str:
    m = (re.search(rf'<meta[^>]+name\s*=\s*["\']{name}["\'][^>]*>', html, re.I)
         or re.search(rf'<meta[^>]+property\s*=\s*["\']{name}["\'][^>]*>', html, re.I))
    if not m:
        return ""
    c = re.search(r'content\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
    return c.group(1).strip() if c else ""


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
           "meta_desc": "", "links": [], "imgs": [], "redirect_to": "", "is_html": False}

    def add(sev, kind, msg, **extra):
        findings.append(dict(severity=sev, kind=kind, url=url, message=msg, **extra))

    if status == 0:
        add("error", "unreachable", f"request failed ({headers.get('_err', '?')})")
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
    if not re.search(r'<link[^>]+rel\s*=\s*["\']canonical["\'][^>]*>', html, re.I):
        add("warn", "no-canonical", "no rel=canonical")

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
        nxt = urllib.parse.urljoin(url, href).split("#")[0]
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

def sitemap_urls(base: str, timeout: int, cap: int) -> list[str]:
    """Every page <loc> in the sitemap index and its children."""
    st, _h, body, _e = _get(urllib.parse.urljoin(base, "/sitemap.xml"), timeout)
    if st != 200 or not body:
        return []
    locs = re.findall(r"<loc>([^<]+)</loc>", body)
    pages = [u for u in locs if not u.endswith(".xml")]
    for child in [u for u in locs if u.endswith(".xml")][:20]:
        s2, _h2, b2, _e2 = _get(child, timeout)
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
            fetched = list(ex.map(lambda p: (p[0], p[1], *_get(p[0], timeout)), batch))

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
        sm = sitemap_urls(base, timeout, cfg["sitemap_cap"])
        step = max(1, len(sm) // cfg["sitemap_sample"]) if sm else 1
        sample = sm[::step][: cfg["sitemap_sample"]]
        if sample:
            with ThreadPoolExecutor(workers) as ex:
                probes = list(ex.map(lambda u: (u, *_get(u, timeout, method="HEAD")), sample))
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
            "findings": results}


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
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    cfg = dict(min_words=a.min_words, slow_s=a.slow_s, dupe_title_cap=3, imgs_per_page=6,
               max_images=500, tiny_img_bytes=12_000, recheck_delay_s=0.4,
               min_internal_links=a.min_internal_links, title_max=a.title_max,
               check_sitemap=not a.no_sitemap_check, sitemap_cap=5000,
               sitemap_sample=a.sitemap_sample, vision_hero_check=a.vision_hero_check,
               vision_max=a.vision_max, leak_tokens=a.leak_token,
               monetized_re=a.monetized_re, editorial_re=a.editorial_re)

    rep = crawl(a.base, a.depth, a.max_pages, a.timeout, a.workers, cfg)
    by_kind = collections.Counter(f["kind"] for f in rep["findings"])
    by_sev = collections.Counter(f["severity"] for f in rep["findings"])
    rep["summary"] = {"by_kind": dict(by_kind), "by_severity": dict(by_sev)}
    rep["diff"] = diff_against_last(rep, a.state_dir, scope=f"d{a.depth}-p{a.max_pages}")

    print(f"\n{a.base}  —  {rep['pages_crawled']} pages, {rep['images_checked']} images, "
          f"{len(rep['findings'])} findings")
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
