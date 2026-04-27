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
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"eBay search failed: {e.code} {err_body}") from e
        return list(data.get("itemSummaries") or [])

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
