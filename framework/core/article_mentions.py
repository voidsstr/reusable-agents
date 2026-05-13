"""Article ↔ product mention extraction + era classification.

Two reusable primitives for any site that publishes articles + has a
product catalog:

  1. ``classify_era(product)`` → ``"retro"`` | ``"modern"``
     Decides whether a product is "retro" (best fetched via eBay) or
     "modern" (Amazon affiliate link). Layered heuristics:

       a. explicit `era` field on the product (if set)
       b. category_slug starts with `retro-`, `vintage-`, `legacy-`
       c. release_year < `RETRO_YEAR_THRESHOLD` (default 2015)
       d. brand in the configured `vintage_brands` set (3dfx, Voodoo,
          original Pentium, etc.)
       e. fallback: "modern"

  2. ``extract_mentions(body, products, *, min_score=0.85)``
       → ``[{product_id, anchor, snippet, span_start, span_end, score}]``

     Scans an article body (markdown or HTML) for product-name mentions.
     Uses two-pass match:

       Pass 1 — exact fuzzy match against the canonical title (e.g.
                "GeForce 4 Ti 4600", "Pentium 4 Northwood")
       Pass 2 — alias match against optional ``product.aliases[]``
                (lowercased, normalized whitespace)

     Returns one mention per unique (product, paragraph) pair so a
     product mentioned twice in the same paragraph counts once.

These are the foundation primitives every site uses to wire articles
to listings (eBay for retro, Amazon for modern).
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Era classification
# ---------------------------------------------------------------------------

_RETRO_CATEGORY_PREFIXES: tuple[str, ...] = (
    "retro-", "vintage-", "legacy-", "classic-",
)
_RETRO_BRANDS_DEFAULT: frozenset[str] = frozenset({
    # GPU
    "3dfx", "voodoo", "matrox", "tseng labs", "s3", "rendition", "kyro",
    # Storage / chipsets / sound
    "creative", "aureal", "ensoniq", "yamaha audio", "soundblaster",
    # Modems / network (era-typical)
    "us robotics", "hayes",
})

# Era threshold — products released before this year default to retro
# unless explicitly marked. Configurable per site via env / cfg.
RETRO_YEAR_THRESHOLD = int(os.environ.get("RETRO_YEAR_THRESHOLD", "2015"))


def classify_era(
    product: dict,
    *,
    retro_year_threshold: int = RETRO_YEAR_THRESHOLD,
    extra_retro_brands: Optional[Iterable[str]] = None,
) -> str:
    """Return ``"retro"`` or ``"modern"`` for a product dict.

    Args:
      product: dict with optional keys: era, category_slug, release_year,
               brand, manufacturer.
      retro_year_threshold: products older than this default to retro.
      extra_retro_brands: site-specific brand whitelist (case-insensitive).

    The function NEVER raises — unknown shape just falls through to
    "modern" (the safe default for a marketplace where Amazon coverage
    is broader than eBay).
    """
    if not isinstance(product, dict):
        return "modern"

    # 1. Explicit era field wins
    explicit = (product.get("era") or "").strip().lower()
    if explicit in ("retro", "modern"):
        return explicit

    # 2. category_slug heuristic
    cat = (product.get("category_slug") or product.get("category") or "").lower()
    if any(cat.startswith(p) for p in _RETRO_CATEGORY_PREFIXES):
        return "retro"

    # 3. release_year heuristic
    yr = product.get("release_year") or product.get("year") or 0
    try:
        yr_int = int(yr)
    except (TypeError, ValueError):
        yr_int = 0
    if yr_int and yr_int < retro_year_threshold:
        return "retro"

    # 4. Brand-list heuristic
    brand = (product.get("brand") or product.get("manufacturer") or "").strip().lower()
    if brand:
        retro_brands = set(_RETRO_BRANDS_DEFAULT)
        if extra_retro_brands:
            retro_brands |= {b.strip().lower() for b in extra_retro_brands if b}
        if brand in retro_brands:
            return "retro"

    # 5. Default: modern
    return "modern"


# ---------------------------------------------------------------------------
# Mention extraction
# ---------------------------------------------------------------------------

# Strip HTML tags + collapse whitespace for body text comparison.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
# Identify paragraph boundaries (HTML <p>, markdown blank-line, or <h*>).
_PARA_SPLIT_RE = re.compile(r"(?:\n\s*\n+|<\/?p[^>]*>|<\/?h[1-6][^>]*>)", re.IGNORECASE)


def _normalize_for_match(text: str) -> str:
    """Lowercase + collapse whitespace + strip HTML for fuzzy matching."""
    if not text:
        return ""
    s = _HTML_TAG_RE.sub(" ", text)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.lower().strip()


def _build_aliases(product: dict) -> list[str]:
    """Collect every name we could match this product by, in priority
    order (most-specific first so a longer match beats a shorter one)."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        n = (s or "").strip()
        if not n:
            return
        nn = _normalize_for_match(n)
        if nn and nn not in seen and len(nn) >= 4:  # too-short = false-positive risk
            seen.add(nn)
            out.append(nn)

    # Explicit aliases first (site-curated)
    for a in (product.get("aliases") or product.get("title_aliases") or []):
        if isinstance(a, str):
            _add(a)
    # Then canonical title — most reliable signal
    _add(product.get("title") or "")
    _add(product.get("name") or "")
    # Brand + family + model — composite forms many articles use
    brand = product.get("brand") or product.get("manufacturer") or ""
    family = product.get("family") or ""
    model = product.get("model_number") or product.get("model") or ""
    if brand and family:
        _add(f"{brand} {family}")
    if family and model:
        _add(f"{family} {model}")
    if brand and model:
        _add(f"{brand} {model}")
    # Sort longest first so multi-word matches beat single-word
    out.sort(key=lambda s: (-len(s), s))
    return out


def _split_paragraphs(body: str) -> list[tuple[int, str]]:
    """Return [(start_offset, paragraph_text), ...] from the article body.

    Used to collapse multiple in-paragraph mentions into one, AND to
    surface a paragraph anchor for inline rendering.
    """
    if not body:
        return []
    parts: list[tuple[int, str]] = []
    pos = 0
    for chunk in _PARA_SPLIT_RE.split(body):
        if chunk and chunk.strip():
            parts.append((pos, chunk))
        pos += len(chunk) if chunk else 0
    if not parts:
        # Single paragraph fallback
        parts = [(0, body)]
    return parts


# Generic terms that show up as catalog rows on some sites (operating
# systems, software, platform names) and produce false-positive mentions
# in editorial articles. Matched case-insensitively against the canonical
# product title.
_GENERIC_TITLE_DENYLIST_DEFAULT: frozenset[str] = frozenset({
    "windows 95", "windows 98", "windows 98 se", "windows me",
    "windows 2000", "windows xp", "windows vista", "windows 7",
    "windows 8", "windows 10", "windows 11",
    "macos", "mac os", "mac os x", "linux", "ubuntu", "debian",
    "ms-dos", "dos",
    "youtube", "twitch", "discord", "reddit", "twitter", "x.com",
    "google", "facebook", "instagram", "tiktok",
    "internet", "web", "html", "css", "javascript",
})


def extract_mentions(
    body: str,
    products: list[dict],
    *,
    min_alias_len: int = 4,
    max_mentions_per_product: int = 5,
    category_allowlist: Optional[Iterable[str]] = None,
    generic_title_denylist: Optional[Iterable[str]] = None,
    require_brand_or_model: bool = False,
) -> list[dict]:
    """Find product mentions in an article body.

    Returns one mention per (product, paragraph) pair, with the byte
    offset of the match + the paragraph's anchor index. Order matches
    the article's top-down reading order.

    Args:
      body: raw article body — markdown or HTML, both work.
      products: list of product dicts from the catalog.
      min_alias_len: drop aliases shorter than this (false-positive guard).
      max_mentions_per_product: cap how many paragraphs reference one
                                product to avoid spammy rendering.

    Output schema:
      {
        "product_id":   <id>,
        "title":        <canonical title used to match>,
        "matched_alias": <which alias hit>,
        "paragraph_index": <0-based paragraph #>,
        "anchor":       "p<N>",      # stable rendering anchor
        "span_start":   <byte offset>,
        "span_end":     <byte offset>,
        "snippet":      <60-char excerpt>,
      }
    """
    if not body or not products:
        return []

    paragraphs = _split_paragraphs(body)
    norm_paragraphs = [(start, _normalize_for_match(p)) for start, p in paragraphs]

    mentions: list[dict] = []
    per_product_count: dict = {}

    # Resolve filters
    cat_allow: Optional[set[str]] = None
    if category_allowlist:
        cat_allow = {c.strip().lower() for c in category_allowlist if c}
    deny_titles: set[str] = set(_GENERIC_TITLE_DENYLIST_DEFAULT)
    if generic_title_denylist is not None:
        # Override (not merge) when caller passes their own list — lets a
        # site opt out entirely with `generic_title_denylist=[]`.
        deny_titles = {t.strip().lower() for t in generic_title_denylist if t}

    # Precompute alias bundles per product (sorted longest-first)
    bundles: list[tuple[dict, list[str]]] = []
    for prod in products:
        if not isinstance(prod, dict) or not prod.get("id"):
            continue
        # Generic-title denylist: skip products whose canonical title is
        # too generic to safely match (e.g. "Windows 98", "YouTube").
        title_l = ((prod.get("title") or prod.get("name") or "")
                   .strip().lower())
        if title_l and title_l in deny_titles:
            continue
        # Category allowlist: when set, product must be in one of the
        # allowed categories (e.g. {"hardware","components","gpu"}).
        if cat_allow is not None:
            cat_l = (prod.get("category_slug") or prod.get("category")
                     or "").strip().lower()
            if cat_l not in cat_allow:
                continue
        # Optional: require the product to have a brand or model_number,
        # which excludes generic catalog rows that have neither.
        if require_brand_or_model:
            has_brand = bool((prod.get("brand")
                              or prod.get("manufacturer") or "").strip())
            has_model = bool((prod.get("model_number")
                              or prod.get("model") or "").strip())
            if not (has_brand or has_model):
                continue
        aliases = _build_aliases(prod)
        aliases = [a for a in aliases if len(a) >= min_alias_len]
        if not aliases:
            continue
        bundles.append((prod, aliases))

    # Scan each paragraph for each product. Sort by alias length desc
    # at the global level so longer matches block shorter substring hits.
    for p_idx, (p_start, p_norm) in enumerate(norm_paragraphs):
        if not p_norm:
            continue
        for prod, aliases in bundles:
            pid = prod.get("id")
            if per_product_count.get(pid, 0) >= max_mentions_per_product:
                continue
            matched_alias = None
            offset_in_p = -1
            for alias in aliases:
                idx = p_norm.find(alias)
                if idx >= 0:
                    matched_alias = alias
                    offset_in_p = idx
                    break
            if not matched_alias:
                continue
            # Skip if this paragraph already has a mention for this
            # product (one mention per paragraph)
            already = any(
                m for m in mentions
                if m["product_id"] == pid and m["paragraph_index"] == p_idx
            )
            if already:
                continue
            # 60-char snippet around the match
            snip_start = max(0, offset_in_p - 30)
            snip_end = min(len(p_norm), offset_in_p + len(matched_alias) + 30)
            snippet = p_norm[snip_start:snip_end].strip()
            mentions.append({
                "product_id": pid,
                "title": prod.get("title") or prod.get("name") or "",
                "matched_alias": matched_alias,
                "paragraph_index": p_idx,
                "anchor": f"p{p_idx}",
                "span_start": p_start + offset_in_p,
                "span_end": p_start + offset_in_p + len(matched_alias),
                "snippet": snippet,
            })
            per_product_count[pid] = per_product_count.get(pid, 0) + 1

    # Sort by paragraph order (already in order since we iterated paragraphs
    # in order, but be explicit)
    mentions.sort(key=lambda m: (m["paragraph_index"], m["span_start"]))
    return mentions


def split_by_era(
    products: list[dict],
    **classify_kwargs,
) -> tuple[list[dict], list[dict]]:
    """Convenience: split a product list into (retro, modern) groups."""
    retro: list[dict] = []
    modern: list[dict] = []
    for p in products:
        if classify_era(p, **classify_kwargs) == "retro":
            retro.append(p)
        else:
            modern.append(p)
    return retro, modern
