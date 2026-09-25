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

SEARCH — ``search_items()``, verified live 2026-09-24
-----------------------------------------------------
``searchItems`` is how an agent DISCOVERS products (keywords → ASINs) the
catalog does not have yet; ``getItems`` only refreshes ASINs it already
knows. Same auth, host, header and camelCase rules as above. Request body:
``keywords``, ``searchIndex`` (``All``, ``Electronics``, …), ``itemCount``
(1-10), ``itemPage``, ``resources``, ``partnerTag``, ``partnerType``, plus
optional filters passed through verbatim (``browseNodeId``, ``brand``,
``sortBy``, ``minPrice``/``maxPrice`` — PA-API took those in CENTS).

The response differs from getItems in ways that matter:

  * Items live under ``searchResult.items``, not ``itemsResult.items``,
    next to ``totalResultCount`` (reported as 1000 for a broad query —
    a ceiling, not a count) and ``searchURL``.
  * ``customerReviews.*`` was requested and NOT returned for any item, and
    ``browseNodeInfo.websiteSalesRank`` neither; ``browseNodeInfo.browseNodes``
    (id + displayName) and ``itemInfo.classifications`` were.
  * The buy-box winner can be a USED listing ("Amazon Resale", VeryGood)
    even for a current product, and availability ``IN_STOCK_SCARCE``
    ("Only 1 left in stock") is still in stock. ``parse_item`` therefore
    prefers a New-condition listing and treats ``IN_STOCK*`` as in stock.
  * ``detailPageURL`` already carries the partner tag (``?tag=<tag>``).

SHARED DAILY BUDGET — ``remaining_budget()`` / ``client.budget_ok()``
---------------------------------------------------------------------
Creators quotas are sales-scaled like PA-API's and ours is unknown, while
several agents on the host share ONE credential (price refresh, product
discovery, hydration, shelf audit). A 429 only says the quota is gone
after the fact, so each client charges every request Amazon answers to a
per-UTC-day ledger in framework storage, one file per consumer::

    config/amazon-creators-usage/<YYYY-MM-DD>/<consumer>.json
        {"calls": n, "throttled": n, "by_operation": {...}, ...}

One file per consumer (rather than one per day) means two different
agents never overwrite each other's count; only two processes of the SAME
consumer can lose an increment, which is acceptable for an advisory
budget. The split comes from ``config/amazon-creators-budget.json``
(merged over ``DEFAULT_BUDGET_CONFIG``)::

    {"total_calls_per_day": 8000,
     "shares": {"price-refresh": 0.45, "discovery": 0.25,
                "hydration": 0.15, "shelf-audit": 0.10},
     "default_share": 0.05}

``remaining_budget(consumer)`` = min(consumer share left, day total left).
The budget is ADVISORY: nothing blocks a call. Agents with large loops
size them with ``client.remaining_budget()`` or check
``client.budget_ok()`` first. Accounting is best-effort — a storage
failure is logged and never fails the API call, and an unreadable ledger
reads as zero usage (fail-open; Amazon's 429 stays the hard backstop).
A client is charged under the ``consumer`` it was built with
(``AmazonCreatorsClient(cfg, consumer="discovery")`` /
``client_from_env(consumer=...)``, else env ``AMAZON_CREATORS_CONSUMER``,
else ``unattributed``).
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("framework.amazon_creators")

TOKEN_ENDPOINT = "https://api.amazon.com/auth/o2/token"
TOKEN_SCOPE = "creatorsapi::default"
DEFAULT_HOST = "creatorsapi.amazon"
DEFAULT_MARKETPLACE = "www.amazon.com"

# getItems accepts at most 10 ASINs per call (same ceiling PA-API had).
MAX_ITEMS_PER_CALL = 10
# searchItems returns at most 10 items per page (itemCount 1-10).
MAX_SEARCH_ITEM_COUNT = 10

# The resources worth pulling for catalog hydration. Kept small on purpose:
# every extra resource widens the response and slows the call.
# `condition` + `isBuyBoxWinner` let parse_item prefer a New listing over a
# used buy-box winner (see module docstring).
DEFAULT_RESOURCES = (
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.isBuyBoxWinner",
    "images.primary.large",
    "customerReviews.starRating",
    "customerReviews.count",
)

# searchItems defaults: everything a discovery agent needs to insert a new
# product in one call (no follow-up getItems). Every name here was accepted
# and returned by the live API on 2026-09-24; customerReviews.* is omitted
# because searchItems did not return it.
SEARCH_RESOURCES = (
    "itemInfo.title",
    "itemInfo.byLineInfo",
    "itemInfo.features",
    "itemInfo.classifications",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.isBuyBoxWinner",
    "images.primary.large",
    "browseNodeInfo.browseNodes",
)

# -- shared daily budget (see module docstring) ---------------------------
BUDGET_CONFIG_KEY = "config/amazon-creators-budget.json"
USAGE_PREFIX = "config/amazon-creators-usage/"
UNATTRIBUTED = "unattributed"

# PA-API started every account at 8,640 requests/day (1 TPS) and scaled up
# with sales; the Creators quota is unknown, so default just under that
# floor. Shares are FRACTIONS of total_calls_per_day; any consumer not
# named gets default_share. Operators override per deployment by writing
# BUDGET_CONFIG_KEY — keys given there replace these, `shares` merges.
DEFAULT_BUDGET_CONFIG: dict = {
    "schema_version": "1",
    "total_calls_per_day": 8000,
    "shares": {
        "price-refresh": 0.45,
        "discovery": 0.25,
        "hydration": 0.15,
        "shelf-audit": 0.10,
    },
    "default_share": 0.05,
}


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


class CreatorsRequestError(RuntimeError):
    """Any other HTTP error from an operation (400, 404, 5xx).

    A RuntimeError subclass so existing ``except RuntimeError`` / ``except
    Exception`` callers are unaffected; the ``status`` and ``detail``
    attributes let ``search_items`` tell "no results" from a real failure
    without parsing the message.
    """

    def __init__(self, message: str, *, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


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


# -- shared daily budget ----------------------------------------------------

def _utc_day() -> str:
    """Ledger day key. A function (not inline) so tests can pin the date."""
    return time.strftime("%Y-%m-%d", time.gmtime())


def normalize_consumer(consumer: Optional[str]) -> str:
    """Kebab-case a consumer name so it is a safe, stable storage key."""
    name = re.sub(r"[^a-z0-9]+", "-", (consumer or "").strip().lower()).strip("-")
    return name or UNATTRIBUTED


def _resolve_storage(storage=None):
    """The framework storage backend, or None when it can't be built.

    get_storage() raises SystemExit on an unknown STORAGE_BACKEND, which an
    ``except Exception`` would miss — and accounting must never take the
    API call down with it.
    """
    if storage is not None:
        return storage
    try:
        from .storage import get_storage
        return get_storage()
    except (Exception, SystemExit) as e:
        log.warning("amazon_creators: usage ledger disabled, storage unavailable: %s", e)
        return None


def load_budget_config(storage=None) -> dict:
    """DEFAULT_BUDGET_CONFIG with the storage override merged on top."""
    cfg = copy.deepcopy(DEFAULT_BUDGET_CONFIG)
    s = _resolve_storage(storage)
    try:
        over = s.read_json(BUDGET_CONFIG_KEY) if s is not None else None
    except Exception as e:
        log.warning("amazon_creators: budget config unreadable, using defaults: %s", e)
        over = None
    if isinstance(over, dict):
        shares = over.get("shares")
        cfg.update({k: v for k, v in over.items() if k != "shares"})
        if isinstance(shares, dict):
            cfg["shares"].update(shares)
    return cfg


def usage_key(consumer: str, day: Optional[str] = None) -> str:
    return f"{USAGE_PREFIX}{day or _utc_day()}/{normalize_consumer(consumer)}.json"


def usage_today(storage=None, day: Optional[str] = None) -> dict:
    """Aggregate the day's ledger: ``{date, total, throttled, consumers}``.

    ``consumers`` maps each consumer to its ledger record. An unreadable
    ledger reports zero usage rather than raising (the budget fails open).
    """
    day = day or _utc_day()
    out: dict = {"date": day, "total": 0, "throttled": 0, "consumers": {}}
    s = _resolve_storage(storage)
    if s is None:
        return out
    try:
        keys = s.list_prefix(f"{USAGE_PREFIX}{day}/") or []
    except Exception as e:
        log.warning("amazon_creators: usage ledger unreadable: %s", e)
        return out
    for key in keys:
        if not key.endswith(".json"):
            continue
        try:
            rec = s.read_json(key) or {}
        except Exception:
            continue
        name = key.rsplit("/", 1)[-1][:-len(".json")]
        out["consumers"][name] = rec
        out["total"] += int(rec.get("calls") or 0)
        out["throttled"] += int(rec.get("throttled") or 0)
    return out


def remaining_budget(consumer: Optional[str], *, storage=None,
                     day: Optional[str] = None) -> int:
    """Calls ``consumer`` may still make today (never negative).

    min(its share of total_calls_per_day minus what it used, the day's
    total minus what EVERY consumer used). Shares are fractions; a
    consumer not listed in the config gets ``default_share``.
    """
    name = normalize_consumer(consumer)
    s = _resolve_storage(storage)
    cfg = load_budget_config(s)
    usage = usage_today(s, day)
    try:
        total_cap = max(0, int(cfg.get("total_calls_per_day") or 0))
        share = float((cfg.get("shares") or {}).get(name, cfg.get("default_share", 0)) or 0)
    except (TypeError, ValueError):
        total_cap, share = int(DEFAULT_BUDGET_CONFIG["total_calls_per_day"]), 0.0
    share = min(max(share, 0.0), 1.0)
    used = int((usage["consumers"].get(name) or {}).get("calls") or 0)
    return max(0, min(int(total_cap * share) - used, total_cap - usage["total"]))


class AmazonCreatorsClient:
    """Thread-safe Creators API client with token caching + throttling.

    ``consumer`` names the budget line every request is charged to (see
    the module docstring); ``storage`` overrides the framework storage
    backend the ledger lives in; ``track_usage=False`` (or env
    ``AMAZON_CREATORS_TRACK_USAGE=0``) turns the ledger off, e.g. in tests.
    """

    def __init__(self, cfg: CreatorsConfig, *, min_interval_s: float = 1.1,
                 consumer: str = "", storage=None, track_usage: bool = True):
        self.cfg = cfg
        self._token = _Token()
        self._lock = threading.Lock()
        self._min_interval_s = min_interval_s
        self._last_call = 0.0
        # ASINs get_items() never attempted because the quota ran dry.
        # NOT errors (an "error" ASIN gets deactivated); retry next run.
        self.throttled_remainder: list[str] = []
        # {total_result_count, search_url, errors, throttled} of the last
        # search_items() call — lets a discovery loop stop paging early.
        self.last_search_meta: dict = {}
        self.consumer = normalize_consumer(
            consumer or os.environ.get("AMAZON_CREATORS_CONSUMER", ""))
        self.track_usage = bool(track_usage) and os.environ.get(
            "AMAZON_CREATORS_TRACK_USAGE", "1").strip().lower() not in ("0", "false", "no", "off")
        self._usage_storage = storage
        self._usage_storage_resolved = storage is not None
        self._usage_lock = threading.Lock()
        # Charges not yet written to the ledger (only non-zero after a
        # storage write failed; they ride along on the next write).
        self._usage_pending: dict = {"calls": 0, "throttled": 0, "by_operation": {}}

    # -- usage ledger ------------------------------------------------------
    def _ledger(self):
        if not self._usage_storage_resolved:
            self._usage_storage = _resolve_storage(None)
            self._usage_storage_resolved = True
        return self._usage_storage

    def _record_usage(self, operation: str, *, throttled: bool = False) -> None:
        """Charge one answered request to today's ledger. Never raises."""
        if not self.track_usage:
            return
        try:
            with self._usage_lock:
                p = self._usage_pending
                if throttled:
                    p["throttled"] += 1
                else:
                    p["calls"] += 1
                    p["by_operation"][operation] = p["by_operation"].get(operation, 0) + 1
                s = self._ledger()
                if s is None:
                    return
                day = _utc_day()
                key = usage_key(self.consumer, day)
                rec = s.read_json(key) or {}
                ops = dict(rec.get("by_operation") or {})
                for op, n in p["by_operation"].items():
                    ops[op] = int(ops.get(op) or 0) + n
                rec.update({
                    "date": day,
                    "consumer": self.consumer,
                    "calls": int(rec.get("calls") or 0) + p["calls"],
                    "throttled": int(rec.get("throttled") or 0) + p["throttled"],
                    "by_operation": ops,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                s.write_json(key, rec)
                self._usage_pending = {"calls": 0, "throttled": 0, "by_operation": {}}
        except Exception as e:
            log.warning("amazon_creators: usage ledger write failed (kept pending): %s", e)

    def remaining_budget(self, consumer: str = "") -> int:
        """Calls left today for ``consumer`` (default: this client's)."""
        name = normalize_consumer(consumer) if consumer else self.consumer
        left = remaining_budget(name, storage=self._ledger())
        if name == self.consumer:
            left -= self._usage_pending["calls"]  # charged but not yet written
        return max(0, left)

    def budget_ok(self, consumer: str = "", calls: int = 1) -> bool:
        """True when ``consumer`` can still spend ``calls`` requests today."""
        return self.remaining_budget(consumer) >= max(1, int(calls))

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
        net_attempt = 0
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
                    data = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:400]
                self._record_usage(operation, throttled=(e.code == 429))
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
                raise CreatorsRequestError(
                    f"Creators {operation} failed HTTP {e.code}: {detail}",
                    status=e.code, detail=detail,
                ) from e
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
                # 2026-09-18: a TLS handshake timeout to the catalog host
                # escaped as a raw URLError and failed the whole
                # kitchen-scraper run (27 min of work lost, unit marked
                # failed). Network-level trouble is "unavailable this run",
                # the same class callers already handle: retry once, then
                # surface CreatorsUnavailable so the caller skips the refresh
                # and carries on.
                net_attempt += 1
                if net_attempt <= 1:
                    time.sleep(3.0)
                    continue
                raise CreatorsUnavailable(
                    f"Creators {operation} unreachable: {e}"
                ) from e
            else:
                # Charged only once Amazon answered — a network failure
                # never reached the quota. Outside the try so a ledger
                # problem can't masquerade as a network error and trigger
                # a second, double-charged request.
                self._record_usage(operation)
                return data

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

    def search_items(
        self,
        keywords: str,
        *,
        search_index: str = "All",
        item_count: int = 10,
        item_page: int = 1,
        resources: Optional[tuple[str, ...]] = None,
        **filters: Any,
    ) -> list[dict]:
        """Keyword / browse-node search: one ``searchItems`` call = one page
        of ≤10 items. ``keywords`` may be empty when a filter such as
        ``browse_node_id`` scopes the search (e.g. newest GPUs:
        ``search_items("", browse_node_id="284822", sort_by="NewestArrivals")``,
        PA-API's sortBy vocabulary — not yet verified against Creators).

        Returns the raw items, in Amazon's relevance order, in the same
        shape ``get_items`` returns — feed each to ``parse_item()``. Extra
        ``filters`` go into the request body verbatim after snake_case →
        camelCase (``browse_node_id=`` → ``browseNodeId``, ``sort_by=`` →
        ``sortBy``, ``brand=``, ``min_price=``/``max_price=`` in CENTS);
        None values are dropped.

        Paging/metadata land in ``self.last_search_meta``:
        ``total_result_count`` (a ceiling for broad queries), ``search_url``,
        ``errors`` and ``throttled``. Like ``get_items`` this never raises on
        throttle (returns [] with ``throttled=True``), and "no results" is
        an empty list, not an error. Auth/network failures still raise
        ``CreatorsUnavailable``.
        """
        body: dict[str, Any] = {
            "searchIndex": search_index or "All",
            "itemCount": max(1, min(int(item_count), MAX_SEARCH_ITEM_COUNT)),
            "resources": list(resources or SEARCH_RESOURCES),
            "partnerTag": self.cfg.partner_tag,
            "partnerType": self.cfg.partner_type,
        }
        if keywords:  # optional: a browse_node_id-only search is valid
            body["keywords"] = keywords
        if item_page and int(item_page) > 1:
            body["itemPage"] = int(item_page)
        for key, val in filters.items():
            if val is None:
                continue
            head, *rest = key.split("_")
            body[head + "".join(w[:1].upper() + w[1:] for w in rest)] = val

        self.last_search_meta = {"total_result_count": 0, "search_url": None,
                                 "errors": [], "throttled": False}
        try:
            resp = self._post("searchItems", body)
        except CreatorsThrottled:
            self.last_search_meta["throttled"] = True
            return []
        except CreatorsRequestError as e:
            # PA-API answered an empty search with HTTP 404 NoResults; treat
            # that shape as "nothing found" should Creators do the same.
            if e.status == 404 or "NoResults" in (e.detail or ""):
                self.last_search_meta["errors"] = [e.detail]
                return []
            raise
        result = resp.get("searchResult") or {}
        self.last_search_meta.update({
            "total_result_count": int(result.get("totalResultCount") or 0),
            "search_url": result.get("searchURL"),
            "errors": [err.get("message") or err.get("code") or "unknown"
                       for err in (resp.get("errors") or [])],
        })
        return [it for it in (result.get("items") or []) if it.get("asin")]


# Availability types that mean "buyable now". IN_STOCK_SCARCE is the
# "Only 1 left in stock" state — still in stock (it was mis-read as out of
# stock before 2026-09-24). LEADTIME / PREORDER / OUT_OF_STOCK are not.
IN_STOCK_TYPES = frozenset({"IN_STOCK", "IN_STOCK_SCARCE"})


def _pick_listing(listings: list) -> dict:
    """The listing whose price represents the product.

    A NEW listing wins over a used one even when the used one holds the buy
    box — showing an "Amazon Resale / VeryGood" price as a new card's price
    is the misleading-price case the pricing rules forbid. Order: New buy-box
    winner → any New → buy-box winner → first. When the response carries no
    ``condition`` (resource not requested) this reduces to the old
    buy-box-then-first rule.
    """
    listings = [l for l in listings if isinstance(l, dict)]
    if not listings:
        return {}

    def is_new(l: dict) -> bool:
        return str(((l.get("condition") or {}).get("value")) or "").lower() == "new"

    return (next((l for l in listings if l.get("isBuyBoxWinner") and is_new(l)), None)
            or next((l for l in listings if is_new(l)), None)
            or next((l for l in listings if l.get("isBuyBoxWinner")), None)
            or listings[0])


def parse_item(raw: dict) -> dict:
    """Flatten a Creators item (getItems OR searchItems) into catalog shape.

    Keys: asin, title, brand, manufacturer, url (detailPageURL, partner tag
    included), price, original_price (savingBasis — list/was price),
    original_price_type (LIST_PRICE / WAS_PRICE), currency, availability
    (message), availability_type, in_stock, condition (New/Used/…),
    merchant, image_url, rating, review_count, features, browse_nodes
    ([{id, name, context_free_name}]), product_group, binding, sales_rank.
    Absent resources come back None / [] (COALESCE-safe for callers).

    `price` is returned in DOLLARS. Amazon reports both a numeric `amount`
    and a display string; prefer the numeric one and only fall back to
    parsing the display string, because a cents/dollars mix-up here is what
    produced the $0.01 Atari listings the pricing-integrity rules exist to
    catch.
    """
    info = raw.get("itemInfo") or {}
    listings = ((raw.get("offersV2") or {}).get("listings")) or []
    best = _pick_listing(listings)
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
    saving_basis = price_block.get("savingBasis") or {}
    original_price, _ = _money(saving_basis)
    # A list price below the sale price is the "Save -$50" bug the pricing
    # rules forbid; drop it rather than store a negative discount.
    if original_price is not None and price is not None and original_price < price:
        original_price = None

    def _dv(node: Any) -> Optional[str]:
        return (node.get("displayValue") or None) if isinstance(node, dict) else None

    avail = best.get("availability") or {}
    byline = info.get("byLineInfo") or {}
    classes = info.get("classifications") or {}
    browse = raw.get("browseNodeInfo") or {}
    rank = browse.get("websiteSalesRank") or {}
    reviews = raw.get("customerReviews") or {}
    return {
        "asin": raw.get("asin"),
        "title": _dv(info.get("title")),
        "brand": _dv(byline.get("brand")),
        "manufacturer": _dv(byline.get("manufacturer")),
        "url": raw.get("detailPageURL"),
        "price": price,
        "original_price": original_price,
        "original_price_type": (saving_basis.get("savingBasisType")
                                if original_price is not None else None),
        "currency": currency or "USD",
        "availability": avail.get("message"),
        "availability_type": avail.get("type"),
        "in_stock": avail.get("type") in IN_STOCK_TYPES,
        "condition": (best.get("condition") or {}).get("value"),
        "merchant": (best.get("merchantInfo") or {}).get("name"),
        "image_url": (((raw.get("images") or {}).get("primary") or {}).get("large") or {}).get("url"),
        "rating": (reviews.get("starRating") or {}).get("value"),
        "review_count": reviews.get("count"),
        "features": ((info.get("features") or {}).get("displayValues")) or [],
        "browse_nodes": [
            {"id": n.get("id"), "name": n.get("displayName"),
             "context_free_name": n.get("contextFreeName")}
            for n in (browse.get("browseNodes") or []) if isinstance(n, dict)
        ],
        "product_group": _dv(classes.get("productGroup")),
        "binding": _dv(classes.get("binding")),
        "sales_rank": rank.get("salesRank") if isinstance(rank, dict) else None,
    }


def client_from_env(*, consumer: str = "", storage=None, track_usage: bool = True,
                    **kwargs) -> Optional[AmazonCreatorsClient]:
    """Convenience: build a client, or None when Amazon isn't configured.

    ``consumer`` / ``storage`` / ``track_usage`` go to the client (budget
    ledger); everything else to ``CreatorsConfig.from_env``.
    """
    cfg = CreatorsConfig.from_env(**kwargs)
    if not cfg:
        return None
    return AmazonCreatorsClient(cfg, consumer=consumer, storage=storage,
                                track_usage=track_usage)
