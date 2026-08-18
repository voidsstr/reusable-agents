"""Amazon Creators API client — the fleet-wide path for Amazon product lookup.

WHY THIS EXISTS
---------------
Amazon product lookup had drifted into three separate implementations:

  * ``specpicks/src/services/amazonCreators.ts``      — the only working one
  * ``specpicks/agents/amazon-price-verifier``        — shells out to the TS
  * ``specpicks/agents/amazon-catalog-freshness``     — shells out to the TS
  * ``agents/product-hydration-agent/paapi_client.py`` — PA-API v5 (SigV4)

The fleet-wide hydration agent (the one BOTH sites use) was on the PA-API
SigV4 path, whose credentials (AMAZON_PAAPI_ACCESS_KEY / SECRET_KEY /
ASSOCIATE_TAG) are not provisioned — so every Amazon lookup it attempted
returned ``skipped_reason: PA-API credentials not in env``. Meanwhile the
Creators credentials the operator is cleared to use were reachable only
from TypeScript, i.e. only from specpicks.

This module is the generic primitive so any agent on any site can look up
Amazon products with the credentials we actually have. Per the
framework-first policy the logic lives here and only the VALUES (partner
tag, marketplace) are per-deployment config.

CREDENTIALS  (``~/.reusable-agents/secrets.env``)
-------------------------------------------------
  AMAZON_CREATORS_CLIENT_ID      LWA client id     (amzn1.application-oa2-client.*)
  AMAZON_CREATORS_CLIENT_SECRET  LWA client secret (amzn1.oa2-cs.v1.*)
  AMAZON_CREATORS_PARTNER_TAG    associate tag, per site (e.g. specpicks-20)
  AMAZON_CREATORS_MARKETPLACE    default www.amazon.com

PROTOCOL — verified live 2026-08-13
-----------------------------------
Creators is NOT PA-API v5 and the two are easy to conflate (a site.yaml
comment in this repo still calls Creators "a.k.a. the PA-API"). Auth and
payload shape both differ, and each difference below cost a failed call to
discover, so they are recorded here rather than rediscovered:

  * Auth is LWA OAuth2 client_credentials, NOT AWS SigV4 request signing.
  * The token scope is ``creatorsapi::default``. Plausible-looking guesses
    (``advertising::creator_api`` and friends) return 400 "invalid
    parameter : scope" — which reads like bad credentials but is not.
  * Marketplace travels in an ``x-marketplace`` HEADER, not the body, and
    wants the DOMAIN (``www.amazon.com``). The PA-API marketplace id
    ``ATVPDKIKX0DER`` is rejected as "Invalid marketplace".
  * Request fields are camelCase (``itemIds``, ``partnerTag``), where
    PA-API used PascalCase (``ItemIds``, ``PartnerTag``).
  * Resource names are camelCase AND offers are ``offersV2``:
    ``offersV2.listings.price``, not PA-API's ``Offers.Listings.Price``.
    Sending an invalid resource returns a 400 that helpfully enumerates
    the entire valid set — the cheapest way to re-discover it.

A per-ASIN ``ItemNotAccessible`` error is normal and NOT a failure: it
means that ASIN is not available through the API (delisted, regional, or
not offered). Those arrive in a top-level ``errors`` array alongside a
perfectly good ``itemsResult`` for the rest of the batch, so callers must
read both.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

TOKEN_ENDPOINT = "https://api.amazon.com/auth/o2/token"
TOKEN_SCOPE = "creatorsapi::default"
DEFAULT_HOST = "creatorsapi.amazon"
DEFAULT_MARKETPLACE = "www.amazon.com"

# getItems accepts at most 10 ASINs per call (same ceiling PA-API had).
MAX_ITEMS_PER_CALL = 10

# The resources worth pulling for catalog hydration. Kept small on purpose:
# every extra resource widens the response and slows the call.
DEFAULT_RESOURCES = (
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "images.primary.large",
    "customerReviews.starRating",
    "customerReviews.count",
)


class CreatorsUnavailable(RuntimeError):
    """Raised when the API cannot be used at all (no creds, auth rejected).

    Distinct from a per-ASIN miss so callers can tell "this product isn't
    available" (routine, skip the row) from "the integration is broken"
    (escalate). Catching this and degrading gracefully is correct; catching
    it and silently reporting success is not.
    """


class CreatorsThrottled(RuntimeError):
    """Raised when Amazon keeps returning 429 after in-call backoff.

    ``_post`` already sleeps and retries on 429 (honoring Retry-After), so
    by the time this surfaces the quota is genuinely exhausted for now.
    ``get_items`` catches it and returns the partial batch — completed work
    is never discarded over a throttle (the 2026-08-18 kitchen-scraper
    failure: 49 products repriced, then one 429 aborted the whole run).
    """


@dataclass
class CreatorsConfig:
    client_id: str
    client_secret: str
    partner_tag: str
    marketplace: str = DEFAULT_MARKETPLACE
    host: str = DEFAULT_HOST
    partner_type: str = "Associates"

    @classmethod
    def from_env(
        cls,
        *,
        partner_tag: str = "",
        marketplace: str = "",
        client_id_env: str = "AMAZON_CREATORS_CLIENT_ID",
        client_secret_env: str = "AMAZON_CREATORS_CLIENT_SECRET",
        partner_tag_env: str = "AMAZON_CREATORS_PARTNER_TAG",
        marketplace_env: str = "AMAZON_CREATORS_MARKETPLACE",
    ) -> Optional["CreatorsConfig"]:
        """Build from env, or return None when credentials are absent.

        Returns None rather than raising so a caller can cheaply ask "is
        Amazon configured here?" and degrade to another provider. The
        explicit `partner_tag` argument wins over the env var so a
        multi-site host can hydrate each site under its own associate tag
        from one process.
        """
        cid = os.environ.get(client_id_env, "").strip()
        sec = os.environ.get(client_secret_env, "").strip()
        tag = (partner_tag or os.environ.get(partner_tag_env, "")).strip()
        if not cid or not sec or not tag:
            return None
        return cls(
            client_id=cid,
            client_secret=sec,
            partner_tag=tag,
            marketplace=(marketplace or os.environ.get(marketplace_env, "")
                         or DEFAULT_MARKETPLACE).strip(),
        )


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0


class AmazonCreatorsClient:
    """Thread-safe Creators API client with token caching + throttling."""

    def __init__(self, cfg: CreatorsConfig, *, min_interval_s: float = 1.1):
        self.cfg = cfg
        self._token = _Token()
        self._lock = threading.Lock()
        self._min_interval_s = min_interval_s
        self._last_call = 0.0
        # ASINs get_items() never attempted because the quota ran dry.
        # NOT errors (an "error" ASIN gets deactivated); retry next run.
        self.throttled_remainder: list[str] = []

    # -- auth ------------------------------------------------------------
    def _access_token(self) -> str:
        """Return a cached token, refreshing ~60s before expiry.

        The 60s skew matters: tokens live an hour and a batch hydration run
        can straddle the boundary, so refreshing exactly at expiry would
        fail mid-run under clock skew.
        """
        with self._lock:
            now = time.time()
            if self._token.value and now < self._token.expires_at - 60:
                return self._token.value
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "scope": TOKEN_SCOPE,
            }).encode()
            req = urllib.request.Request(
                TOKEN_ENDPOINT, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:300]
                raise CreatorsUnavailable(
                    f"LWA token request failed HTTP {e.code}: {detail}"
                ) from e
            except Exception as e:
                raise CreatorsUnavailable(f"LWA token request failed: {e}") from e
            tok = data.get("access_token")
            if not tok:
                raise CreatorsUnavailable(f"LWA returned no access_token: {data}")
            self._token = _Token(tok, time.time() + float(data.get("expires_in", 3600)))
            return tok

    # -- throttle --------------------------------------------------------
    def _throttle(self) -> None:
        delta = time.time() - self._last_call
        if delta < self._min_interval_s:
            time.sleep(self._min_interval_s - delta)
        self._last_call = time.time()

    # -- api -------------------------------------------------------------
    def _post(self, operation: str, payload: dict,
              *, max_throttle_retries: int = 4) -> dict:
        """POST one operation, absorbing 429s with exponential backoff.

        The Creators quota is shared by every agent on the host (kitchen-
        scraper and product-hydration fire a minute apart), so a 429 here
        usually means "wait out the minute", not "broken". Sleep-and-retry
        honors Retry-After when Amazon sends it, else 4s/8s/16s/32s — ~1min
        total, which rides out a per-minute quota window. Only after that
        does CreatorsThrottled surface.
        """
        attempt = 0
        while True:
            self._throttle()
            req = urllib.request.Request(
                f"https://{self.cfg.host}/catalog/v1/{operation}",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {self._access_token()}",
                    "Content-Type": "application/json",
                    # Marketplace is a header, and wants the domain — see module docstring.
                    "x-marketplace": self.cfg.marketplace,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:400]
                if e.code in (401, 403):
                    raise CreatorsUnavailable(
                        f"Creators API rejected credentials HTTP {e.code}: {detail}"
                    ) from e
                if e.code == 429:
                    attempt += 1
                    if attempt > max_throttle_retries:
                        raise CreatorsThrottled(
                            f"Creators {operation} still throttled after "
                            f"{max_throttle_retries} retries: {detail}"
                        ) from e
                    try:
                        retry_after = float(e.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    time.sleep(max(retry_after, min(2.0 * (2 ** attempt), 60.0)))
                    continue
                raise RuntimeError(
                    f"Creators {operation} failed HTTP {e.code}: {detail}"
                ) from e

    def get_items(
        self,
        asins: list[str],
        *,
        resources: Optional[tuple[str, ...]] = None,
    ) -> tuple[dict[str, dict], dict[str, str]]:
        """Look up ASINs. Returns ``(items_by_asin, errors_by_asin)``.

        Automatically chunks to the API's 10-per-call ceiling. Both halves
        of the tuple matter: an ASIN in `errors` is inaccessible (delisted /
        regional / not offered) and should be deactivated rather than
        retried forever.

        Never raises on throttle: if 429s persist past `_post`'s backoff,
        the remaining ASINs land in ``self.throttled_remainder`` (neither
        item nor error) and the partial result is returned.
        """
        res = list(resources or DEFAULT_RESOURCES)
        items: dict[str, dict] = {}
        errors: dict[str, str] = {}
        self.throttled_remainder = []
        clean = [a.strip() for a in asins if a and a.strip()]
        for i in range(0, len(clean), MAX_ITEMS_PER_CALL):
            batch = clean[i:i + MAX_ITEMS_PER_CALL]
            try:
                resp = self._post("getItems", {
                    "itemIds": batch,
                    "resources": res,
                    "partnerTag": self.cfg.partner_tag,
                    "partnerType": self.cfg.partner_type,
                })
            except CreatorsThrottled:
                # Quota exhausted even after backoff: keep the completed
                # work, park the untried tail for the caller to inspect
                # (``client.throttled_remainder``) and retry next run.
                self.throttled_remainder = clean[i:]
                break
            for it in (resp.get("itemsResult") or {}).get("items") or []:
                if it.get("asin"):
                    items[it["asin"]] = it
            for err in resp.get("errors") or []:
                msg = err.get("message") or err.get("code") or "unknown"
                # The ASIN is named in the message, not a field of its own.
                for a in batch:
                    if a in msg and a not in items:
                        errors[a] = msg
        return items, errors


def parse_item(raw: dict) -> dict:
    """Flatten a Creators item into the shape the catalog stores.

    `price` is returned in DOLLARS. Amazon reports both a numeric `amount`
    and a display string; prefer the numeric one and only fall back to
    parsing the display string, because a cents/dollars mix-up here is what
    produced the $0.01 Atari listings the pricing-integrity rules exist to
    catch.
    """
    info = raw.get("itemInfo") or {}
    listings = ((raw.get("offersV2") or {}).get("listings")) or []
    best = next((l for l in listings if l.get("isBuyBoxWinner")), listings[0] if listings else {})
    price_block = best.get("price") or {}

    def _money(block: Any) -> tuple[Optional[float], Optional[str]]:
        """Pull (amount, currency) out of a Creators `money` envelope.

        Creators nests the number one level deeper than PA-API did —
        ``price.money.amount``, not ``price.amount`` — so reading the old
        shape yields None and silently writes a NULL price. Falls back to
        parsing displayAmount only if the numeric field is missing.
        """
        if not isinstance(block, dict):
            return None, None
        money = block.get("money") if isinstance(block.get("money"), dict) else block
        amt = money.get("amount")
        cur = money.get("currency")
        if amt is None:
            disp = (money.get("displayAmount") or "").strip()
            cleaned = "".join(ch for ch in disp if ch.isdigit() or ch == ".")
            try:
                amt = float(cleaned) if cleaned else None
            except ValueError:
                amt = None
        return (float(amt) if amt is not None else None), cur

    price, currency = _money(price_block)
    original_price, _ = _money(price_block.get("savingBasis"))
    # A list price below the sale price is the "Save -$50" bug the pricing
    # rules forbid; drop it rather than store a negative discount.
    if original_price is not None and price is not None and original_price < price:
        original_price = None

    reviews = raw.get("customerReviews") or {}
    return {
        "asin": raw.get("asin"),
        "title": ((info.get("title") or {}).get("displayValue")) or None,
        "brand": (((info.get("byLineInfo") or {}).get("brand") or {}).get("displayValue")) or None,
        "url": raw.get("detailPageURL"),
        "price": price,
        "original_price": original_price,
        "currency": currency or "USD",
        "availability": (best.get("availability") or {}).get("message"),
        "in_stock": ((best.get("availability") or {}).get("type") == "IN_STOCK"),
        "image_url": (((raw.get("images") or {}).get("primary") or {}).get("large") or {}).get("url"),
        "rating": (reviews.get("starRating") or {}).get("value"),
        "review_count": reviews.get("count"),
        "features": ((info.get("features") or {}).get("displayValues")) or [],
    }


def client_from_env(**kwargs) -> Optional[AmazonCreatorsClient]:
    """Convenience: build a client, or None when Amazon isn't configured."""
    cfg = CreatorsConfig.from_env(**kwargs)
    return AmazonCreatorsClient(cfg) if cfg else None
