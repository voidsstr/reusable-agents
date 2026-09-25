"""Keyword-driven Amazon product discovery over Creators ``searchItems``.

WHY THIS EXISTS
---------------
``amazon_creators.AmazonCreatorsClient.search_items()`` answers one search
page. Every catalog that wants to *grow* from Amazon (not just re-price
what it has) needs the same loop around it: rotate through a configured
keyword list, stop at a per-run call cap / the shared daily budget / a 429,
drop listings the pricing-integrity rules forbid, and split what came back
into genuinely-new ASINs vs ones the catalog already carries (whose search
price is a free refresh). Before this module that loop lived only in a
site's TypeScript script. It is site-neutral, so it lives here; the
site's agent keeps what is site-specific — where the keywords come from,
how a title maps to a category, and the INSERT.

USAGE
-----
::

    from framework.core import amazon_creators, amazon_discovery as ad

    client = amazon_creators.client_from_env(consumer="discovery")
    kws = ad.normalize_keywords(cfg["keywords"])      # str or {q, category, …}
    todays, _ = ad.select_rotation(kws, state.get("cursor", 0), 40)
    res = ad.discover(client, todays, known_asins=lookup_fn,
                      max_calls=60, max_new=300)
    res.candidates   # parsed items (amazon_creators.parse_item shape) for NEW ASINs
    res.seen_known   # parsed items for ASINs the catalog already has
    res.api_calls, res.throttled, res.searched_keywords, res.stopped_reason
    state["cursor"] = ad.cursor_after(state.get("cursor", 0), len(kws), todays, res)

Persist ``cursor_after()``, not ``select_rotation()``'s cursor: a run cut
short by the budget or a 429 must resume at the first keyword it never
reached, not skip the rest of its slice for a whole rotation cycle.

Each candidate / seen item carries ``_keyword`` and ``_category_hint`` (the
keyword entry's ``category``) so the caller can route it.

PRICING INTEGRITY
-----------------
``listing_rejection()`` is the gate: no title, no price, price below the
floor (default $1 — the $0/$0.01 incidents), a non-allowed currency
(foreign-marketplace listings), a Used-only listing (searchItems can
return an "Amazon Resale / VeryGood" buy-box winner as the only offer; a
used price must never become a new product's card price), or an
availability that is not buyable/orderable (same rule as
``amazon_price_refresh.classify_item``). Rejected items are counted by
reason, never returned.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from framework.core.amazon_creators import (
    CreatorsRequestError,
    IN_STOCK_TYPES,
    MAX_SEARCH_ITEM_COUNT,
    parse_item,
)
from framework.core.amazon_price_refresh import ORDERABLE_TYPES

log = logging.getLogger("framework.amazon_discovery")

DEFAULT_MIN_PRICE = 1.0
DEFAULT_CURRENCIES = ("USD",)


def normalize_keywords(entries: Iterable[Any]) -> list[dict]:
    """Coerce config keyword entries into ``{q, category, search_index, min_price}``.

    An entry is either a plain string or a dict with ``q`` (alias
    ``keywords``) plus optional ``category`` (the caller's category hint),
    ``search_index`` and ``min_price`` (a per-keyword price floor, e.g. so a
    "RTX 5090" search doesn't admit $12 GPU brackets). Blank entries are
    dropped; duplicates (case/whitespace-insensitive) keep the FIRST
    occurrence so earlier, more specific sources win.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for e in entries or []:
        if isinstance(e, str):
            e = {"q": e}
        if not isinstance(e, dict):
            continue
        q = " ".join(str(e.get("q") or e.get("keywords") or "").split())
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        mp = e.get("min_price")
        out.append({
            "q": q,
            "category": e.get("category") or None,
            "search_index": e.get("search_index") or None,
            "min_price": float(mp) if mp is not None else None,
        })
    return out


def select_rotation(keywords: list, cursor: int, n: int) -> tuple[list, int]:
    """Take ``n`` keywords starting at ``cursor`` (wrapping); return
    ``(selected, next_cursor)``. Lets a long keyword list be covered over
    several runs instead of re-searching the first ``n`` every tick."""
    total = len(keywords)
    if total == 0 or n <= 0:
        return [], 0
    start = int(cursor or 0) % total
    if n >= total:
        return list(keywords[start:] + keywords[:start]), start
    picked = [keywords[(start + i) % total] for i in range(n)]
    return picked, (start + n) % total


def cursor_after(cursor: int, total: int, picked: list, res: "DiscoveryResult") -> int:
    """The rotation cursor for the next run: just past the ``picked``
    (``select_rotation`` output) keywords this run actually got through
    (``res.processed``). Equals ``select_rotation``'s cursor when the run
    finished its slice; when the budget, a 429 or a cap cut it short, the
    next run resumes at the first keyword never reached."""
    if total <= 0:
        return 0
    done = {str(q).lower() for q in res.processed}
    n = 0
    for kw in picked:
        if str(kw["q"] if isinstance(kw, dict) else kw).lower() not in done:
            break
        n += 1
    return (int(cursor or 0) % total + n) % total


def listing_rejection(item: dict, *, min_price: float = DEFAULT_MIN_PRICE,
                      currencies: Iterable[str] = DEFAULT_CURRENCIES,
                      require_new: bool = True) -> Optional[str]:
    """Why a parsed item must not become a catalog product, or None if OK.

    Reasons: ``no-title``, ``no-price``, ``price-below-min``, ``currency``,
    ``used-only``, ``unavailable`` (an availability type that is neither
    in stock nor pre-order/lead-time). A missing ``condition`` or
    availability (resource not requested) is not held against the item.
    """
    if not (item.get("title") or "").strip():
        return "no-title"
    price = item.get("price")
    if price is None:
        return "no-price"
    try:
        if float(price) < float(min_price):
            return "price-below-min"
    except (TypeError, ValueError):
        return "no-price"
    allowed = {c.upper() for c in (currencies or ())}
    if allowed and (item.get("currency") or "").upper() not in allowed:
        return "currency"
    cond = (item.get("condition") or "").strip().lower()
    if require_new and cond and cond != "new":
        return "used-only"
    avail = item.get("availability_type")
    if avail and avail not in IN_STOCK_TYPES and avail not in ORDERABLE_TYPES:
        return "unavailable"
    return None


@dataclass
class DiscoveryResult:
    candidates: list = field(default_factory=list)   # new ASINs, parsed + _keyword
    seen_known: list = field(default_factory=list)   # already-carried ASINs, parsed
    searched_keywords: int = 0
    api_calls: int = 0
    throttled: int = 0
    errors: int = 0
    rejected: dict = field(default_factory=dict)      # reason -> count
    per_keyword: list = field(default_factory=list)   # [{q, results, new, known, rejected}]
    processed: list = field(default_factory=list)     # q of each keyword got through (searched or errored)
    stopped_reason: str = ""                          # "" = finished the keyword list


def discover(
    client,
    keywords: list[dict],
    *,
    known_asins: Callable[[list[str]], set],
    max_calls: int = 60,
    max_new: int = 300,
    pages_per_keyword: int = 1,
    search_index: str = "All",
    item_count: int = MAX_SEARCH_ITEM_COUNT,
    min_price: float = DEFAULT_MIN_PRICE,
    currencies: Iterable[str] = DEFAULT_CURRENCIES,
    require_new: bool = True,
    respect_budget: bool = True,
) -> DiscoveryResult:
    """Search each keyword (``normalize_keywords`` shape) and split results.

    ``known_asins(asins) -> set`` answers which of a page's ASINs the
    catalog already carries (the caller's dedupe; any site_id). New ASINs
    must clear the keyword's ``min_price`` floor; carried ones only the
    base ``min_price`` (the floor keeps accessories out, it is not a
    reason to skip a valid re-price). Stops early
    — recording ``stopped_reason`` — when ``max_calls`` / ``max_new`` is
    reached, the client's advisory daily budget is spent
    (``client.budget_ok()``, skipped when ``respect_budget`` is False), or
    Amazon throttles (a 429 that survived the client's backoff: the quota is
    gone, further calls only burn retries). A page 2+ is fetched only while
    the previous page came back full. A per-keyword request error is
    counted and skipped; ``CreatorsUnavailable`` (auth / network) propagates.
    """
    res = DiscoveryResult()
    seen: set[str] = set()
    pages = max(1, int(pages_per_keyword or 1))
    for kw in keywords:
        if res.stopped_reason:
            break
        stats = {"q": kw["q"], "results": 0, "new": 0, "known": 0, "rejected": 0}
        floor = kw.get("min_price")
        floor = float(min_price) if floor is None else float(floor)
        searched = False
        for page in range(1, pages + 1):
            if res.api_calls >= max_calls:
                res.stopped_reason = "max-calls"
                break
            if len(res.candidates) >= max_new:
                res.stopped_reason = "max-new"
                break
            if respect_budget and hasattr(client, "budget_ok") and not client.budget_ok():
                res.stopped_reason = "budget-exhausted"
                break
            res.api_calls += 1
            try:
                raw = client.search_items(
                    kw["q"], search_index=kw.get("search_index") or search_index,
                    item_count=item_count, item_page=page)
            except CreatorsRequestError as e:
                res.errors += 1
                log.warning("amazon_discovery: search %r failed: %s", kw["q"], e)
                if not searched:
                    res.processed.append(kw["q"])  # a bad keyword: move past it
                break
            if (getattr(client, "last_search_meta", None) or {}).get("throttled"):
                res.throttled += 1
                res.stopped_reason = "throttled"
                break
            searched = True
            parsed = [parse_item(r) for r in raw]
            parsed = [p for p in parsed if p.get("asin") and p["asin"] not in seen]
            stats["results"] += len(parsed)
            known = set(known_asins([p["asin"] for p in parsed])) if parsed else set()
            for p in parsed:
                seen.add(p["asin"])
                p["_keyword"] = kw["q"]
                p["_category_hint"] = kw.get("category")
                if p["asin"] in known:
                    # A keyword's floor keeps accessories OUT of the catalog;
                    # a product already carried just needs a valid price.
                    stats["known"] += 1
                    if listing_rejection(p, min_price=min_price, currencies=currencies,
                                         require_new=require_new) is None:
                        res.seen_known.append(p)
                    continue
                reason = listing_rejection(p, min_price=floor, currencies=currencies,
                                           require_new=require_new)
                if reason is not None:
                    stats["rejected"] += 1
                    res.rejected[reason] = res.rejected.get(reason, 0) + 1
                    continue
                if len(res.candidates) < max_new:
                    res.candidates.append(p)
                    stats["new"] += 1
            if len(raw) < item_count:
                break  # a short page is the last page
        if searched:
            res.searched_keywords += 1
            res.per_keyword.append(stats)
            res.processed.append(kw["q"])
    return res
