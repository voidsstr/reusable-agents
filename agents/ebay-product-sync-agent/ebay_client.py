"""eBay Browse API client.

Operator supplies EBAY_CLIENT_ID + EBAY_CLIENT_SECRET via env (preferred) or
the agent's site.yaml. Tokens cached in-process for ~2h. EPN affiliate
campaign id is optional; when set, eBay returns deep-linked
itemAffiliateWebUrls.

Docs: https://developer.ebay.com/api-docs/buy/browse/overview.html
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Iterable, Optional
import urllib.parse
import urllib.request
import urllib.error

logger = logging.getLogger("ebay-sync.ebay")


class EbayRateLimited(RuntimeError):
    """HTTP 429 from the Browse API — the app's daily call quota (or a
    short-window limit) is exhausted. Callers stop their loop instead of
    burning more calls."""


class EbayItemGroup(RuntimeError):
    """get_item_by_legacy_id hit a multi-variation listing (errorId 11006).
    The listing is an item GROUP; price it via get_items_by_item_group()."""

    def __init__(self, group_id: str):
        super().__init__(f"eBay item {group_id} is an item group")
        self.group_id = group_id


class EbayClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        env: str = "PRODUCTION",
        marketplace: str = "EBAY_US",
        campaign_id: Optional[str] = None,
        scopes: Optional[Iterable[str]] = None,
    ):
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.env = env.upper()
        self.marketplace = marketplace
        self.campaign_id = campaign_id or ""
        self.scopes = list(scopes or ["https://api.ebay.com/oauth/api_scope"])
        if self.env == "SANDBOX":
            self._token_url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
            self._base_url = "https://api.sandbox.ebay.com/buy/browse/v1"
        else:
            self._token_url = "https://api.ebay.com/identity/v1/oauth2/token"
            self._base_url = "https://api.ebay.com/buy/browse/v1"
        self._token: Optional[str] = None
        self._token_expires: float = 0.0
        # Browse API calls answered by eBay this process, by operation.
        # Feeds the per-day call ledger (eBay quotas are per app per day).
        self.calls: dict[str, int] = {}

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def _count(self, op: str) -> None:
        self.calls[op] = self.calls.get(op, 0) + 1

    @classmethod
    def from_env(cls, *, env_prefix: str = "EBAY_") -> "EbayClient":
        return cls(
            client_id=os.environ.get(f"{env_prefix}CLIENT_ID", ""),
            client_secret=os.environ.get(f"{env_prefix}CLIENT_SECRET", ""),
            env=os.environ.get(f"{env_prefix}ENV", "PRODUCTION"),
            marketplace=os.environ.get(f"{env_prefix}MARKETPLACE_ID", "EBAY_US"),
            campaign_id=os.environ.get(f"{env_prefix}CAMPAIGN_ID") or None,
        )

    # ─── OAuth ─────────────────────────────────────────────────────
    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - 300:
            return self._token
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": " ".join(self.scopes),
        }).encode()
        req = urllib.request.Request(
            self._token_url, data=body, method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"eBay OAuth failed: {e.code} {err_body}") from e
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 7200))
        return self._token

    # ─── Browse API ────────────────────────────────────────────────
    def search(
        self,
        *,
        q: Optional[str] = None,
        category_ids: Optional[str] = None,
        filter_str: Optional[str] = None,
        sort: str = "-bestMatch",
        limit: int = 50,
        offset: int = 0,
        fieldgroups: str = "EXTENDED",
    ) -> list[dict]:
        token = self._ensure_token()
        params = {}
        if q: params["q"] = q
        if category_ids: params["category_ids"] = category_ids
        if filter_str: params["filter"] = filter_str
        if sort: params["sort"] = sort
        params["limit"] = str(min(200, max(1, limit)))
        if offset: params["offset"] = str(max(0, offset))
        params["fieldgroups"] = fieldgroups
        url = self._base_url + "/item_summary/search?" + urllib.parse.urlencode(params)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
        }
        if self.campaign_id:
            headers["X-EBAY-C-ENDUSERCTX"] = (
                f"affiliateCampaignId={self.campaign_id},"
                f"affiliateReferenceId=ebay-product-sync-agent"
            )
        req = urllib.request.Request(url, headers=headers)
        # Resilience: eBay's Browse API frontend (Envoy proxy) returns 503
        # "upstream connect error" intermittently — typically <2% of calls
        # but several in a row during regional traffic spikes. Retry with
        # backoff on 503/502/504 and on transient socket errors. Other
        # HTTP errors (401 token, 400 bad query, 429 rate-limit) are NOT
        # retried — those need a different fix.
        import time as _time
        last_err = None
        for attempt in range(3):
            if attempt > 0:
                _time.sleep(2 ** attempt)  # 2s, 4s
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    data = json.loads(r.read().decode())
                self._count("search")
                return list(data.get("itemSummaries") or [])
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
                self._count("search")
                if e.code == 429:
                    raise EbayRateLimited(f"eBay search rate-limited: {err_body}") from e
                last_err = RuntimeError(f"eBay search failed: {e.code} {err_body}")
                if e.code in (502, 503, 504):
                    continue  # retry
                raise last_err from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = RuntimeError(f"eBay search failed (transport): {e}")
                continue  # retry transport errors
        # All retries exhausted
        raise last_err if last_err else RuntimeError("eBay search failed: unknown")

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
        }
        if self.campaign_id:
            headers["X-EBAY-C-ENDUSERCTX"] = (
                f"affiliateCampaignId={self.campaign_id},"
                f"affiliateReferenceId=ebay-product-sync-agent"
            )
        return headers

    def _browse_get(self, path: str, op: str) -> tuple[int, Any]:
        """GET a Browse API path. Returns (200, json) or (http_code, error
        json/dict) for 4xx the caller interprets (404, 400). Retries 5xx and
        transport errors; raises EbayRateLimited on 429 and RuntimeError on
        other failures. Every answered request is counted under `op`."""
        req = urllib.request.Request(self._base_url + path, headers=self._headers())
        last_err: Optional[Exception] = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2 ** attempt)  # 2s, 4s
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode())
                self._count(op)
                return 200, data
            except urllib.error.HTTPError as e:
                self._count(op)
                err_body = e.read().decode("utf-8", errors="replace")[:1000]
                if e.code == 429:
                    raise EbayRateLimited(f"eBay {op} rate-limited: {err_body[:300]}") from e
                if e.code in (502, 503, 504):
                    last_err = RuntimeError(f"eBay {op} failed: {e.code} {err_body[:300]}")
                    continue
                if e.code in (400, 404):
                    try:
                        return e.code, json.loads(err_body)
                    except ValueError:
                        return e.code, {"raw": err_body}
                raise RuntimeError(f"eBay {op} failed: {e.code} {err_body[:300]}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = RuntimeError(f"eBay {op} failed (transport): {e}")
                continue
        raise last_err if last_err else RuntimeError(f"eBay {op} failed: unknown")

    @staticmethod
    def _error_ids(body: Any) -> set[int]:
        if not isinstance(body, dict):
            return set()
        return {int(e.get("errorId") or 0) for e in (body.get("errors") or [])}

    def get_item_by_legacy_id(self, legacy_id: str) -> Optional[dict]:
        """Fetch one listing by its legacy (numeric) item id — the id the
        search results expose as `legacyItemId` and that we store.

        Returns the item dict, or None when eBay says the listing no longer
        exists (404 / errorIds 11001, 11003). Raises EbayItemGroup for a
        multi-variation listing (errorId 11006) so the caller can price
        the group instead.
        """
        path = ("/item/get_item_by_legacy_id?legacy_item_id="
                + urllib.parse.quote(str(legacy_id), safe=""))
        code, body = self._browse_get(path, "get_item_by_legacy_id")
        if code == 200:
            return body
        ids = self._error_ids(body)
        if 11006 in ids:
            raise EbayItemGroup(str(legacy_id))
        if code == 404 or ids & {11001, 11003}:
            return None
        raise RuntimeError(f"eBay get_item_by_legacy_id({legacy_id}) failed: "
                           f"{code} {str(body)[:300]}")

    def get_items_by_item_group(self, group_id: str) -> Optional[list[dict]]:
        """All variations of a multi-variation listing, or None when the
        group no longer exists."""
        path = ("/item/get_items_by_item_group?item_group_id="
                + urllib.parse.quote(str(group_id), safe=""))
        code, body = self._browse_get(path, "get_items_by_item_group")
        if code == 200:
            return list(body.get("items") or [])
        if code == 404 or self._error_ids(body) & {11001, 11003}:
            return None
        raise RuntimeError(f"eBay get_items_by_item_group({group_id}) failed: "
                           f"{code} {str(body)[:300]}")

    def get_item(self, item_id: str) -> Optional[dict]:
        """Fetch one listing. Returns the item dict, or None when it has
        ended/been removed.

        Accepts either id form: a legacy numeric id (what `ebay_listings`
        stores — routed to get_item_by_legacy_id) or a RESTful
        "v1|<id>|<variation>" id. Calling /item/<legacy id> directly always
        404s, which is what made the old audit mark every live listing ended.
        """
        item_id = str(item_id)
        if item_id.isdigit():
            return self.get_item_by_legacy_id(item_id)
        code, body = self._browse_get(
            "/item/" + urllib.parse.quote(item_id, safe="|"), "get_item")
        if code == 200:
            return body
        if code == 404:
            return None
        raise RuntimeError(f"eBay get_item({item_id}) failed: {code} {str(body)[:300]}")

    def browse_rate_limit(self, resource: str = "buy.browse") -> Optional[dict]:
        """eBay's own count of today's Browse calls for this app, across
        EVERY caller sharing the keys (other agents, the web app) — via the
        Developer Analytics getRateLimits call, which is not itself a Browse
        call. Returns {limit, remaining, count, reset} for `resource` or
        None when unavailable (callers fall back to their own ledger)."""
        base = self._base_url.split("/buy/browse/")[0]
        url = (base + "/developer/analytics/v1_beta/rate_limit/"
               "?api_context=buy&api_name=browse")
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self._ensure_token()}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                OSError, ValueError, RuntimeError) as e:
            logger.warning("eBay getRateLimits unavailable: %s", str(e)[:200])
            return None
        for api in data.get("rateLimits") or []:
            for res in api.get("resources") or []:
                if res.get("name") != resource:
                    continue
                for rate in res.get("rates") or []:
                    try:
                        return {"limit": int(rate["limit"]),
                                "remaining": int(rate["remaining"]),
                                "count": int(rate.get("count") or 0),
                                "reset": rate.get("reset")}
                    except (KeyError, TypeError, ValueError):
                        continue
        return None

    def healthcheck(self) -> dict:
        """Verify creds work and the marketplace is reachable."""
        self._ensure_token()
        return {
            "ok": True,
            "env": self.env,
            "marketplace": self.marketplace,
            "campaign": bool(self.campaign_id),
            "expires_at": self._token_expires,
        }
