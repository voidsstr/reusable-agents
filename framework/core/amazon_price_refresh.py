"""Budget-sized Amazon price refresh over the Creators API — any site.

WHY THIS EXISTS
---------------
Price refresh had been done by fetching ``amazon.com/dp/<asin>`` HTML with a
browser User-Agent and parsing the buy box. Scraping Amazon, and showing
prices obtained that way, is outside the Amazon Associates Operating
Agreement (prices shown must come from the Product Advertising / Creators
API) and puts the associate account at risk. The HTML path also DEACTIVATED
products whose page looked dead, which turned pages that AI assistants and
search engines cite into 404s.

This module is the generic replacement: it walks a list of ASINs through
``getItems`` (10 per call), stops at the caller's call budget or on a
persistent 429, and CLASSIFIES each ASIN into an outcome the caller writes
back in its own schema. It never touches a database — the SQL is
site-specific (column names differ per site), the decision is not.

OUTCOMES (``PriceOutcome.status``)
----------------------------------
  priced       a New-condition offer in the expected currency, buyable now
               (IN_STOCK / IN_STOCK_SCARCE) or orderable (PREORDER /
               LEADTIME / AVAILABLE_DATE) → write price, original_price (list
               price, or None), currency, availability.
  unavailable  Amazon returned the item but it cannot be bought (out of
               stock / unavailable availability type) → price must be NULL.
  no_offer     item returned without a usable New offer (no listings, no
               price, or used-only) → price must be NULL; showing a used or
               stale price as the product price is misleading. Availability
               is cleared too (it describes the listing we are not showing).
  not_found    Amazon answered with a per-ASIN error (ItemNotAccessible /
               invalid ASIN) → price must be NULL. NEVER a reason to
               deactivate: the product page stays live and shows
               "check price on Amazon".
  non_usd      a price in a currency other than ``expected_currency`` → price
               must be NULL (never write a foreign price as USD).
  missing      the ASIN was neither returned nor errored (silent omission)
               → write nothing; it is retried next run.
  error        the batch failed with a non-auth HTTP error → write nothing.

Untried ASINs (budget exhausted, quota 429, or the sweep aborted) are simply
absent from the result and listed in ``stats["untried"]``. A sweep aborts —
keeping every batch already handed to ``on_batch`` — after
``max_consecutive_errors`` failed batches in a row (a Creators outage should
not burn the whole call allowance) or when Creators becomes unreachable
after at least one answered call; ``stats["aborted"]`` says why.

Budget: pass ``max_calls``; it is further clamped to the client's remaining
share of the shared daily Creators budget (``client.remaining_budget()``,
consumer ``price-refresh`` by convention — see amazon_creators.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from framework.core.amazon_creators import (
    IN_STOCK_TYPES,
    MAX_ITEMS_PER_CALL,
    CreatorsRequestError,
    CreatorsUnavailable,
    parse_item,
)

# Just what a price check needs. savingBasis (list price) rides along inside
# offersV2.listings.price; condition + isBuyBoxWinner let parse_item prefer a
# New listing over a used buy-box winner. Smaller than DEFAULT_RESOURCES on
# purpose — a price sweep covers thousands of ASINs a day.
PRICE_RESOURCES = (
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.isBuyBoxWinner",
)

# Orderable now even though not "in stock": a real, current price.
# AVAILABLE_DATE is PA-API's "available from <date>" (orderable ahead).
ORDERABLE_TYPES = frozenset({"PREORDER", "LEADTIME", "AVAILABLE_DATE"})

# Statuses whose correct write is "price = NULL, mark unavailable".
UNAVAILABLE_STATUSES = frozenset({"unavailable", "no_offer", "not_found", "non_usd"})


@dataclass
class PriceOutcome:
    asin: str
    status: str
    price: Optional[float] = None
    original_price: Optional[float] = None
    currency: Optional[str] = None
    availability: Optional[str] = None
    availability_type: Optional[str] = None
    condition: Optional[str] = None
    error: Optional[str] = None

    @property
    def marks_unavailable(self) -> bool:
        return self.status in UNAVAILABLE_STATUSES


def _raw_currency(raw: dict) -> Optional[str]:
    """The currency Amazon actually sent. parse_item() defaults a missing
    currency to USD, which is right for display but would hide a foreign
    price here, so read it off the listings directly."""
    for l in ((raw.get("offersV2") or {}).get("listings")) or []:
        money = ((l or {}).get("price") or {})
        money = money.get("money") if isinstance(money.get("money"), dict) else money
        cur = (money or {}).get("currency")
        if cur:
            return str(cur).upper()
    return None


def classify_item(asin: str, raw: Optional[dict], error: Optional[str] = None,
                  *, expected_currency: str = "USD") -> PriceOutcome:
    """Decide what a price refresh should write for one ASIN."""
    if raw is None:
        if error:
            return PriceOutcome(asin, "not_found", error=error[:300])
        return PriceOutcome(asin, "missing")
    p = parse_item(raw)
    out = PriceOutcome(
        asin, "priced", price=p["price"], original_price=p["original_price"],
        currency=p["currency"], availability=p["availability"],
        availability_type=p["availability_type"], condition=p["condition"],
    )
    listings = ((raw.get("offersV2") or {}).get("listings")) or []
    cur = _raw_currency(raw)
    if cur and cur != expected_currency.upper():
        out.status = "non_usd"
    elif out.availability_type and out.availability_type not in IN_STOCK_TYPES \
            and out.availability_type not in ORDERABLE_TYPES:
        # Checked before "no price": live OUT_OF_STOCK items come back with
        # an availability block but no price (verified 2026-09-24).
        out.status = "unavailable"
    elif not listings or out.price is None or out.price <= 0:
        out.status = "no_offer"
    elif out.condition and str(out.condition).lower() != "new":
        # parse_item already prefers any New listing, so a non-New pick
        # means there is no New offer at all.
        out.status = "no_offer"
    if out.status != "priced":
        out.price = out.original_price = None
        if out.status in ("no_offer", "non_usd"):
            # The availability text belongs to a listing we are NOT showing
            # (e.g. a used-only "Only 2 left in stock"); next to a NULL price
            # it would read as buyable. The caller writes its default.
            out.availability = None
    else:
        out.currency = expected_currency.upper()
    return out


def refresh_prices(
    client,
    asins: Iterable[str],
    *,
    max_calls: Optional[int] = None,
    expected_currency: str = "USD",
    resources: tuple[str, ...] = PRICE_RESOURCES,
    on_batch: Optional[Callable[[list[PriceOutcome]], None]] = None,
    max_consecutive_errors: int = 3,
) -> tuple[list[PriceOutcome], dict]:
    """Classify ``asins`` via getItems, ≤10 per call, within budget.

    ``max_calls`` caps this run; it is clamped to the client's remaining
    daily budget when the client tracks usage. ``on_batch`` is called with
    each batch's outcomes as soon as it lands so the caller can commit
    incrementally (a long sweep killed mid-way keeps its work).

    Returns ``(outcomes, stats)``; stats keys: ``api_calls``, ``throttled``
    (1 when a persistent 429 ended the sweep), ``errors`` (failed batches),
    ``budget_calls`` (the call allowance actually used as the ceiling),
    ``untried`` (ASINs not attempted), ``aborted`` (why the sweep stopped
    early, else ""), and one count per outcome status.
    Auth / network failure (``CreatorsUnavailable``) propagates when it hits
    before any call was answered; later, it ends the sweep with the work so
    far (``aborted``) so a mid-run blip doesn't discard what was written.
    """
    clean = list(dict.fromkeys(a.strip() for a in asins if a and a.strip()))
    allowed = -(-len(clean) // MAX_ITEMS_PER_CALL)  # ceil: calls to cover all
    if max_calls is not None:
        allowed = max(0, int(max_calls))
    if getattr(client, "track_usage", False):
        allowed = min(allowed, client.remaining_budget())
    stats: dict = {"api_calls": 0, "throttled": 0, "errors": 0,
                   "budget_calls": allowed, "untried": [], "aborted": ""}
    outcomes: list[PriceOutcome] = []
    consecutive_errors = 0
    i = 0
    while i < len(clean):
        if stats["api_calls"] + stats["errors"] >= allowed:
            break
        batch = clean[i:i + MAX_ITEMS_PER_CALL]
        try:
            items, errors = client.get_items(batch, resources=resources)
        except CreatorsRequestError as e:
            # One bad batch (e.g. a malformed ASIN → 400) must not end the
            # sweep; it cost a call, nothing is written for it. A run of
            # them (5xx outage) does.
            stats["errors"] += 1
            outcomes.extend(PriceOutcome(a, "error") for a in batch)
            i += len(batch)
            consecutive_errors += 1
            if max_consecutive_errors and consecutive_errors >= max_consecutive_errors:
                stats["aborted"] = f"{consecutive_errors} failed batches in a row: {e}"[:300]
                break
            continue
        except CreatorsUnavailable as e:
            if stats["api_calls"] == 0:
                raise
            stats["aborted"] = f"Creators unreachable: {e}"[:300]
            break
        if client.throttled_remainder:
            stats["throttled"] = 1
            break
        consecutive_errors = 0
        stats["api_calls"] += 1
        batch_out = [classify_item(a, items.get(a), errors.get(a),
                                   expected_currency=expected_currency)
                     for a in batch]
        outcomes.extend(batch_out)
        if on_batch:
            on_batch(batch_out)
        i += len(batch)
    stats["untried"] = clean[i:]
    for o in outcomes:
        stats[o.status] = stats.get(o.status, 0) + 1
    return outcomes, stats
