"""Bright Data Amazon Products dataset client.

Stand-in for Amazon's PA-API while the SpecPicks affiliate account is in
the pre-qualification phase (PA-API requires 3 qualifying purchases).
Bright Data scrapes the same product detail pages and returns a JSON
record per ASIN with title / brand / price / availability / rating /
review count / images.

The TypeScript equivalent lives at
specpicks/scripts/refresh-via-brightdata.ts — this Python module mirrors
its trigger → poll → download flow so the scheduled hydration agent can
call it directly without a Node/ts-node dependency.

Cost: ~$1 per 1000 records at Bright Data 2026 rates. The hydration
agent caps each run at `max_refresh_per_run` so a 2h tick stays well
under budget (~$0.05–$0.20 per tick at typical caps).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

BD_BASE = "https://api.brightdata.com"
BD_DATASET_AMAZON_PRODUCTS = "gd_l7q7dkf244hwjntr0"


class BrightDataError(RuntimeError):
    """Raised on any non-2xx response or snapshot failure."""


@dataclass
class BrightDataConfig:
    api_key: str
    dataset_id: str = BD_DATASET_AMAZON_PRODUCTS
    poll_interval_s: int = 10
    poll_timeout_s: int = 600  # 10min — typical 100-URL batch finishes <90s
    download_retries: int = 4
    request_timeout_s: int = 30


def _request(method: str, url: str, *, headers: dict[str, str],
              body: bytes | None = None,
              timeout_s: int = 30) -> tuple[int, dict | list | str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read().decode("utf-8")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise BrightDataError(f"BD HTTP {e.code} {url}: {body}") from e
    except urllib.error.URLError as e:
        raise BrightDataError(f"BD URL error {url}: {e.reason}") from e


class BrightDataClient:
    """Minimal Amazon-Products dataset client."""

    def __init__(self, cfg: BrightDataConfig):
        if not cfg.api_key:
            raise BrightDataError("BRIGHTDATA_API_KEY not set")
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

    def trigger(self, urls: Iterable[str]) -> str:
        """Submit a batch of Amazon product URLs. Returns snapshot_id."""
        inputs = [{"url": u} for u in urls]
        if not inputs:
            raise BrightDataError("trigger: empty input list")
        qs = urllib.parse.urlencode({
            "dataset_id": self.cfg.dataset_id,
            "format": "json",
        })
        body = json.dumps(inputs).encode("utf-8")
        _, data = _request(
            "POST", f"{BD_BASE}/datasets/v3/trigger?{qs}",
            headers=self._headers(), body=body,
            timeout_s=self.cfg.request_timeout_s,
        )
        if not isinstance(data, dict) or not data.get("snapshot_id"):
            raise BrightDataError(
                f"BD trigger missing snapshot_id: {str(data)[:200]}")
        return data["snapshot_id"]

    def status(self, snapshot_id: str) -> dict:
        """Returns {status, records, errors, ...}. status in
        {running, building, ready, failed}."""
        _, data = _request(
            "GET",
            f"{BD_BASE}/datasets/v3/progress/{snapshot_id}",
            headers=self._headers(),
            timeout_s=self.cfg.request_timeout_s,
        )
        if not isinstance(data, dict):
            raise BrightDataError(f"BD status non-JSON: {str(data)[:200]}")
        return data

    def wait_until_ready(self, snapshot_id: str) -> dict:
        deadline = time.time() + self.cfg.poll_timeout_s
        last = {}
        while time.time() < deadline:
            last = self.status(snapshot_id)
            s = (last.get("status") or "").lower()
            if s == "ready":
                return last
            if s == "failed":
                raise BrightDataError(
                    f"BD snapshot {snapshot_id} failed: {last}")
            time.sleep(self.cfg.poll_interval_s)
        raise BrightDataError(
            f"BD snapshot {snapshot_id} timeout: last={last}")

    def download(self, snapshot_id: str, *,
                 expected_records: int | None = None) -> list[dict]:
        """Download the snapshot. Bright Data sometimes flags 'ready' before
        the file is fully materialised — retry with backoff if record count
        looks suspiciously low vs `expected_records`."""
        max_retries = self.cfg.download_retries
        for attempt in range(max_retries + 1):
            _, data = _request(
                "GET",
                f"{BD_BASE}/datasets/v3/snapshot/{snapshot_id}?format=json",
                headers=self._headers(),
                timeout_s=self.cfg.request_timeout_s,
            )
            arr: list[dict]
            if isinstance(data, list):
                arr = [x for x in data if isinstance(x, dict)]
            elif isinstance(data, dict):
                arr = [data]
            else:
                arr = []
            ok = (
                expected_records is None
                or len(arr) >= max(1, int(expected_records * 0.9))
                or attempt == max_retries
            )
            if ok:
                return arr
            time.sleep(min(10 * (2 ** attempt), 60))
        return []

    def scrape(self, urls: Iterable[str]) -> list[dict]:
        """trigger + wait + download in one call."""
        sid = self.trigger(urls)
        st = self.wait_until_ready(sid)
        return self.download(sid, expected_records=st.get("records"))


# ---------------------------------------------------------------------------
# Mapping helpers — turn a BD record into the columns hydration writes.
# ---------------------------------------------------------------------------

PRICE_FLOOR = 1.0
PRICE_CEILING = 10000.0


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        if "value" in v:
            return _maybe_float(v["value"])
        if "amount" in v:
            return _maybe_float(v["amount"])
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _coerce_price(p: dict) -> float | None:
    """Return a sane buybox-ish price, or None.

    BD sometimes returns gray-market reseller prices ($60k for a $300 NAS)
    or no-buybox listings at $0. Either is worse than no price — the
    runtime falls back to "Check current price on Amazon" which keeps us
    compliant with Amazon Associates' stale-pricing rule.
    """
    candidates = [p.get("final_price")]
    bb = p.get("buybox_prices") or {}
    if isinstance(bb, dict):
        candidates.append(bb.get("final_price"))
        candidates.append(bb.get("unit_price"))
    candidates.append(p.get("initial_price"))
    for c in candidates:
        v = _maybe_float(c)
        if v is None:
            continue
        if PRICE_FLOOR <= v <= PRICE_CEILING:
            return round(v, 2)
    return None


def parse_bd_record(rec: dict) -> dict | None:
    """Map a BD Amazon-Products record into the same shape the
    hydration agent's PA-API parser produces. None if the record is a
    BD warning/error stub."""
    if not isinstance(rec, dict):
        return None
    if rec.get("warning") or rec.get("warning_code"):
        return None
    asin = rec.get("asin") or ""
    if not asin:
        return None

    title = (rec.get("title") or "").strip()
    brand = (rec.get("brand") or "").strip()
    manufacturer = (rec.get("manufacturer") or "").strip()
    features = rec.get("features") or []
    if not isinstance(features, list):
        features = []
    main_image = (rec.get("image") or rec.get("image_url") or "").strip()
    images = rec.get("images") or []
    if not isinstance(images, list):
        images = []
    image_urls = [main_image] if main_image else []
    for img in images:
        if isinstance(img, str) and img and img not in image_urls:
            image_urls.append(img)

    price = _coerce_price(rec)
    original = _maybe_float(rec.get("initial_price"))
    if original is not None and (original < PRICE_FLOOR or
                                  original > PRICE_CEILING):
        original = None
    if original is not None and price is not None and original < price:
        original = None  # bogus discount

    rating = _maybe_float(rec.get("rating"))
    review_count = rec.get("reviews_count")
    if isinstance(review_count, (str, float)):
        try:
            review_count = int(float(str(review_count).replace(",", "")))
        except (ValueError, TypeError):
            review_count = None
    elif not isinstance(review_count, int):
        review_count = None

    availability = (rec.get("availability") or "").strip()[:200]
    is_prime = rec.get("amazon_prime")
    if not isinstance(is_prime, bool):
        is_prime = None

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "manufacturer": manufacturer,
        "features": features,
        "main_image_url": main_image,
        # BD doesn't return image dimensions on the products dataset; leave
        # width/height None and let the compliance check skip the
        # zoom-eligible flag for BD-sourced rows.
        "main_image_width": None,
        "main_image_height": None,
        "image_urls": image_urls,
        "price": price,
        "original_price": original,
        "currency": (rec.get("currency") or "USD").upper(),
        "availability": availability,
        "is_prime": is_prime,
        "rating": rating,
        "review_count": review_count,
        "amazon_url": rec.get("url"),
        "bs_rank": rec.get("bs_rank"),
        "bought_past_month": rec.get("bought_past_month"),
        "source_provider": "brightdata",
    }
