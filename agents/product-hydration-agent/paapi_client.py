"""Amazon Product Advertising API 5.0 client.

Used by the product-hydration-agent to refresh price + offer data on
Amazon-sourced products so prices in our catalog stay <24h fresh
(Amazon Associates policy and Google Merchant Center / Google product
structured data both expect a current `price`).

Implements just what we need: GetItems with a small Resources set, request
signing (AWS SigV4), batching (10 ASINs/request), and a 1 req/sec throttle.

Credentials come from env vars (resolved by the caller from site.yaml's
amazon_paapi block); we never accept inline secrets in the constructor.

Throttling: PA-API enforces a TPS that depends on prior 30-day shipped
revenue, with the floor at 1 TPS for new accounts. We hold ourselves to
1 req/sec (well under the floor) so the agent doesn't have to know which
tier it's on.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable

DEFAULT_HOST = "webservices.amazon.com"
DEFAULT_REGION = "us-east-1"
DEFAULT_MARKETPLACE = "www.amazon.com"
DEFAULT_RESOURCES = (
    "ItemInfo.Title",
    "ItemInfo.Features",
    "ItemInfo.ProductInfo",
    "ItemInfo.ByLineInfo",
    "Offers.Listings.Price",
    "Offers.Listings.SavingBasis",
    "Offers.Listings.Availability.Message",
    "Offers.Listings.DeliveryInfo.IsPrimeEligible",
    "Images.Primary.Large",
    "Images.Variants.Large",
    "CustomerReviews.Count",
    "CustomerReviews.StarRating",
)


@dataclass
class PaapiConfig:
    """Resolved configuration for a single PA-API client instance."""
    access_key: str
    secret_key: str
    associate_tag: str
    region: str = DEFAULT_REGION
    host: str = DEFAULT_HOST
    marketplace: str = DEFAULT_MARKETPLACE
    throttle_per_second: float = 1.0
    request_timeout_s: int = 15

    @classmethod
    def from_env(cls, *,
                 access_key_env: str = "AMAZON_PAAPI_ACCESS_KEY",
                 secret_key_env: str = "AMAZON_PAAPI_SECRET_KEY",
                 associate_tag: str | None = None,
                 associate_tag_env: str = "AMAZON_PAAPI_ASSOCIATE_TAG",
                 region: str = DEFAULT_REGION,
                 host: str = DEFAULT_HOST,
                 marketplace: str = DEFAULT_MARKETPLACE,
                 throttle_per_second: float = 1.0,
                 request_timeout_s: int = 15) -> "PaapiConfig | None":
        ak = os.environ.get(access_key_env, "").strip()
        sk = os.environ.get(secret_key_env, "").strip()
        tag = (associate_tag or os.environ.get(associate_tag_env, "")).strip()
        if not (ak and sk and tag):
            return None
        return cls(access_key=ak, secret_key=sk, associate_tag=tag,
                   region=region, host=host, marketplace=marketplace,
                   throttle_per_second=throttle_per_second,
                   request_timeout_s=request_timeout_s)


class PaapiError(RuntimeError):
    """Raised on any non-200 response or signature failure."""


def _sign(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = hmac.new(("AWS4" + secret_key).encode(), date_stamp.encode(),
                       hashlib.sha256).digest()
    k_region = hmac.new(k_date, region.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode(), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _build_headers(*, cfg: PaapiConfig, path: str, target: str,
                   payload: str) -> dict[str, str]:
    now = _dt.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    service = "ProductAdvertisingAPI"

    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"content-type:application/json; charset=utf-8\n"
        f"host:{cfg.host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-target:{target}\n"
    )
    signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join([
        "POST", path, "", canonical_headers, signed_headers, payload_hash,
    ])
    credential_scope = f"{date_stamp}/{cfg.region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signing_key = _sign(cfg.secret_key, date_stamp, cfg.region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={cfg.access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Content-Encoding": "amz-1.0",
        "Content-Type": "application/json; charset=utf-8",
        "Host": cfg.host,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": target,
        "Authorization": auth,
    }


class PaapiClient:
    """Minimal PA-API v5 client. One operation: GetItems."""

    def __init__(self, cfg: PaapiConfig):
        self.cfg = cfg
        self._last_call_ts = 0.0

    # ---- public ----------------------------------------------------

    def get_items(self, asins: list[str], *,
                  resources: Iterable[str] = DEFAULT_RESOURCES) -> dict:
        """Call PA-API GetItems for up to 10 ASINs at a time.

        Returns the raw decoded JSON response. Caller handles per-ASIN
        parsing because the agent wants flexibility (some products are
        unavailable / OOS / regional-blocked and we want to record that
        in price_updated_at without nuking the row).
        """
        if not asins:
            return {"ItemsResult": {"Items": []}}
        if len(asins) > 10:
            raise PaapiError("GetItems accepts at most 10 ASINs per request")

        self._throttle()

        path = "/paapi5/getitems"
        target = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems"
        body = json.dumps({
            "ItemIds": asins,
            "Resources": list(resources),
            "PartnerTag": self.cfg.associate_tag,
            "PartnerType": "Associates",
            "Marketplace": self.cfg.marketplace,
        }, separators=(",", ":"))

        headers = _build_headers(cfg=self.cfg, path=path, target=target,
                                  payload=body)
        url = f"https://{self.cfg.host}{path}"
        req = urllib.request.Request(url, data=body.encode("utf-8"),
                                      headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req,
                                         timeout=self.cfg.request_timeout_s) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise PaapiError(f"PA-API HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise PaapiError(f"PA-API URL error: {e.reason}") from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise PaapiError(f"PA-API non-JSON response: {raw[:200]}") from e

    # ---- helpers ---------------------------------------------------

    def _throttle(self) -> None:
        if self.cfg.throttle_per_second <= 0:
            return
        min_gap = 1.0 / self.cfg.throttle_per_second
        elapsed = time.time() - self._last_call_ts
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_call_ts = time.time()


# ---------------------------------------------------------------------------
# Item-shape helpers — pull just the fields the hydration agent writes.
# ---------------------------------------------------------------------------

def parse_get_items_response(resp: dict) -> dict[str, dict]:
    """Return {asin: parsed_dict} for items the API actually returned.

    The agent uses this to update price + image + availability. Items
    not in the response (errors, OOS, gone) are simply absent — caller
    decides what to do with that.
    """
    out: dict[str, dict] = {}
    items = ((resp.get("ItemsResult") or {}).get("Items") or [])
    for raw in items:
        if not isinstance(raw, dict):
            continue
        asin = raw.get("ASIN")
        if not asin:
            continue
        out[str(asin)] = _parse_item(raw)
    return out


def _parse_item(raw: dict) -> dict:
    info = raw.get("ItemInfo") or {}
    title = ((info.get("Title") or {}).get("DisplayValue") or "").strip()
    features = ((info.get("Features") or {}).get("DisplayValues") or [])
    if not isinstance(features, list):
        features = []
    by_line = info.get("ByLineInfo") or {}
    brand = ((by_line.get("Brand") or {}).get("DisplayValue") or "").strip()
    manufacturer = ((by_line.get("Manufacturer") or {}).get("DisplayValue") or "").strip()

    images = raw.get("Images") or {}
    primary = images.get("Primary") or {}
    large = primary.get("Large") or {}
    main_image_url = (large.get("URL") or "").strip()
    main_image_height = large.get("Height")
    main_image_width = large.get("Width")
    image_urls: list[str] = []
    if main_image_url:
        image_urls.append(main_image_url)
    for v in (images.get("Variants") or []):
        v_large = (v or {}).get("Large") or {}
        u = (v_large.get("URL") or "").strip()
        if u and u not in image_urls:
            image_urls.append(u)

    offers = (raw.get("Offers") or {}).get("Listings") or []
    price: float | None = None
    original_price: float | None = None
    availability_msg = ""
    is_prime: bool | None = None
    if offers and isinstance(offers, list):
        first = offers[0] or {}
        p_obj = first.get("Price") or {}
        if "Amount" in p_obj:
            try:
                price = float(p_obj["Amount"])
            except (ValueError, TypeError):
                price = None
        sb_obj = first.get("SavingBasis") or {}
        if "Amount" in sb_obj:
            try:
                original_price = float(sb_obj["Amount"])
            except (ValueError, TypeError):
                original_price = None
        avail = first.get("Availability") or {}
        availability_msg = (avail.get("Message") or "").strip()[:200]
        delivery = first.get("DeliveryInfo") or {}
        if "IsPrimeEligible" in delivery:
            is_prime = bool(delivery["IsPrimeEligible"])

    reviews = raw.get("CustomerReviews") or {}
    rating = None
    review_count = None
    star = (reviews.get("StarRating") or {}).get("Value")
    if star is not None:
        try:
            rating = float(star)
        except (ValueError, TypeError):
            rating = None
    rc = reviews.get("Count")
    if rc is not None:
        try:
            review_count = int(rc)
        except (ValueError, TypeError):
            review_count = None

    return {
        "asin": raw.get("ASIN"),
        "title": title,
        "brand": brand,
        "manufacturer": manufacturer,
        "features": features,
        "main_image_url": main_image_url,
        "main_image_width": main_image_width,
        "main_image_height": main_image_height,
        "image_urls": image_urls,
        "price": price,
        "original_price": original_price,
        "currency": "USD",
        "availability": availability_msg,
        "is_prime": is_prime,
        "rating": rating,
        "review_count": review_count,
        "amazon_url": raw.get("DetailPageURL"),
    }
