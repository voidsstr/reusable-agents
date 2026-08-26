"""Shelf discovery + Amazon truth-check primitives.

Kept separate from agent.py so the crawl/compare logic is unit-testable
without a framework run.
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import io
import json
import re
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; shelf-audit-agent/1.0; +https://specpicks.com)"}

ASIN_RE = re.compile(r"(?:amazon\.[a-z.]+/(?:[^\"'\s]*/)?dp/|/dp/)([A-Z0-9]{10})")
HREF_RE = re.compile(r'href="([^"#?]{1,200})"')
_IMG_RE = re.compile(
    r"(https://m\.media-amazon\.com/images/I/)([A-Za-z0-9+%-]{6,}?)(\._[^/]*)?\.jpg")

# Validated 2026-08-26: an identical picture compared across two renditions
# hashes 0-1 apart once normalized; 12 leaves generous headroom for JPEG noise.
IMAGE_THRESHOLD = 12
PRICE_TOLERANCE = 0.02


def fetch(url: str, timeout: int = 25) -> str:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            if "text/html" not in ct and "json" not in ct:
                return ""
            return r.read(3_000_000).decode("utf-8", "replace")
    except Exception:
        return ""


def crawl(origin: str, max_depth: int = 3, per_depth=(None, 400, 500, 500),
          seeds=(), workers: int = 16) -> dict:
    """BFS the site with a PER-DEPTH budget.

    A single global page cap is wrong: specpicks' homepage alone links 408
    pages, so a 400-page global budget was spent entirely at depth 1 and
    depths 2-3 were never visited (measured 2026-08-26). Budgeting per depth
    is what makes "3 levels deep" mean anything.
    """
    seen: set[str] = set()
    out: dict[str, dict] = {}
    taken: collections.Counter = collections.Counter()
    q = collections.deque([(origin, 0)] + [(s, 1) for s in seeds])
    host = urllib.parse.urlparse(origin).netloc

    while q:
        batch = []
        while q and len(batch) < workers:
            u, d = q.popleft()
            if u in seen or d > max_depth:
                continue
            cap = per_depth[d] if d < len(per_depth) else 0
            if cap is not None and taken[d] >= cap:
                continue
            seen.add(u)
            taken[d] += 1
            batch.append((u, d))
        if not batch:
            break
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for (u, d), html in zip(batch, ex.map(lambda x: fetch(x[0]), batch)):
                if not html:
                    continue
                out[u] = {"depth": d, "asins": sorted(set(ASIN_RE.findall(html)))}
                if d < max_depth:
                    for h in set(HREF_RE.findall(html)):
                        nu = urllib.parse.urljoin(u, h)
                        p = urllib.parse.urlparse(nu)
                        if p.netloc == host and p.scheme in ("http", "https"):
                            nu = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}"
                            if nu not in seen:
                                q.append((nu, d + 1))
    return out


def crawl_api_surface(endpoints: list[str], slug_key: str = "slug",
                      workers: int = 12) -> dict:
    """Collect products from JSON endpoints for CLIENT-RENDERED shelves.

    aisleprompt renders its kitchen shelves in the browser: crawling 982 of
    its pages yielded ZERO ASINs in HTML. Any shelf audit that only greps
    rendered HTML silently reports "no products" for such a site instead of
    failing loudly — so API surfaces are a first-class input, not a fallback.
    """
    out: dict[str, dict] = {}

    def get(u):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA),
                                        timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return {}

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for doc in ex.map(get, endpoints):
            for p in (doc.get("products") or doc.get("items") or []):
                if isinstance(p, dict) and p.get(slug_key):
                    out[p[slug_key]] = p
    return out


def normalize_image(u: str, suffix: str = "._AC_SX679_.jpg") -> str:
    """Rewrite an Amazon image URL to ONE rendition.

    Comparing image IDs or raw URLs does NOT work: Amazon serves the same
    product under several image IDs, and two renditions of the SAME id
    (._SL500_ vs ._AC_SX679_) differ by 117 of 256 hash bits. Measured on a
    200-product sample, raw-URL comparison reported ~99% of images "wrong"
    where the true rate was 10-14%.
    """
    m = _IMG_RE.search(u or "")
    return (m.group(1) + m.group(2) + suffix) if m else (u or "")


def ahash(url: str):
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                    timeout=20) as r:
            im = Image.open(io.BytesIO(r.read())).convert("L").resize((16, 16))
        px = list(im.getdata())
        avg = sum(px) / len(px)
        return int("".join("1" if p > avg else "0" for p in px), 2)
    except Exception:
        return None


def same_picture(site_url: str, amazon_url: str):
    """-> (verdict, hamming) where verdict is 'same' | 'different' | 'unknown'."""
    a = ahash(normalize_image(site_url))
    b = ahash(normalize_image(amazon_url))
    if a is None or b is None:
        return "unknown", None
    h = bin(a ^ b).count("1")
    return ("same" if h <= IMAGE_THRESHOLD else "different"), h


def cents(v):
    if v is None:
        return None
    try:
        return int(round(float(v) * 100))
    except Exception:
        return None


def compare_row(row: dict, az: dict, check_images: bool = True) -> list[str]:
    """Compare one shelf row against the Amazon truth. Returns issue tags."""
    issues: list[str] = []
    azc, dbc = cents(az.get("price")), row.get("db_price_cents")
    if azc is None:
        issues.append("amazon_no_price")
    elif dbc is None:
        issues.append("site_no_price")
    else:
        drift = (dbc - azc) / max(azc, 1)
        if abs(drift) > PRICE_TOLERANCE:
            issues.append(
                "price_drift:%+.0f%%:site=%.2f:amazon=%.2f"
                % (100 * drift, dbc / 100, azc / 100))
    if check_images and row.get("db_image") and az.get("image_url"):
        verdict, h = same_picture(row["db_image"], az["image_url"])
        if verdict == "different":
            issues.append("image_mismatch:hamming=%s" % h)
    elif not row.get("db_image"):
        issues.append("image_missing")
    if az.get("in_stock") is False:
        issues.append("out_of_stock_on_amazon")
    for f in ("title", "brand"):
        if not (row.get(f) or "").strip():
            issues.append("missing_%s" % f)
    return issues
