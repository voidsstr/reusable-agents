"""Schema-mapping store + applicator.

A mapping is a JSON document that says "this eBay field/expression goes
into THIS destination column". Once approved by the operator (via email),
it's stored under the agent's storage prefix and used for every
subsequent ingestion run.

Mapping file shape (stored in framework storage):

  {
    "schema_version": "1",
    "site_id": "specpicks",
    "destination_table": "products",
    "key_columns": ["site_id", "asin"],
    "approved_at": "2026-04-27T18:30:00Z",
    "approved_by": "user@example.com",
    "fields": [
      {
        "destination_column": "asin",
        "source_path": "legacyItemId",
        "transform": "ebay_id_prefix",
        "default": null,
        "notes": "Prefix EBAY_ + numeric legacyItemId for cross-marketplace dedup"
      },
      {
        "destination_column": "title",
        "source_path": "title",
        "transform": null,
        "default": "",
        "notes": "Direct copy"
      },
      ...
    ],
    "constants": [
      {"destination_column": "site_id", "value": "<UUID>"},
      {"destination_column": "source",  "value": "ebay"},
      {"destination_column": "is_active", "value": true}
    ]
  }

Transforms (whitelist — keeps the agent from running arbitrary code):

  ebay_id_prefix     →  "EBAY_" + str(value)
  parse_float        →  float(value) or None
  parse_int          →  int(value) or None
  iso_date           →  pass-through ISO 8601
  feedback_pct_to_5  →  min(5, float(pct)/20)
  affiliate_url      →  use itemAffiliateWebUrl, fall back to itemWebUrl
  image_first        →  pick first non-empty from image.imageUrl, thumbnailImages[0].imageUrl
  buying_options_csv →  ",".join(buyingOptions)
  json_dumps         →  json.dumps(value)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("ebay-sync.mapping")


def mapping_storage_key(agent_id: str, site_id: str) -> str:
    return f"agents/{agent_id}/mappings/{site_id}.json"


# ───────────────────────────────────────────────────────────────────
# Transforms
# ───────────────────────────────────────────────────────────────────

def _ebay_id_prefix(value: Any, item: dict) -> Any:
    if value is None:
        # Try to derive from itemId "v1|123456789012|0"
        legacy = item.get("legacyItemId")
        if not legacy:
            full = item.get("itemId") or ""
            parts = full.split("|")
            if len(parts) >= 2:
                legacy = parts[1]
        return f"EBAY_{legacy}" if legacy else None
    return f"EBAY_{value}"


def _parse_float(value: Any, item: dict) -> Optional[float]:
    if value is None or value == "":
        return None
    try: return float(value)
    except (TypeError, ValueError): return None


def _parse_int(value: Any, item: dict) -> Optional[int]:
    if value is None or value == "":
        return None
    try: return int(value)
    except (TypeError, ValueError): return None


def _iso_date(value: Any, item: dict) -> Optional[str]:
    if not value:
        return None
    return str(value)


def _feedback_pct_to_5(value: Any, item: dict) -> Optional[float]:
    """Convert eBay seller feedback percentage (0-100) to a 0-5 rating."""
    try:
        pct = float(value)
        return min(5.0, pct / 20.0)
    except (TypeError, ValueError):
        return None


def _affiliate_url(value: Any, item: dict) -> Optional[str]:
    return item.get("itemAffiliateWebUrl") or item.get("itemWebUrl") or value


def _image_first(value: Any, item: dict) -> Optional[str]:
    img = (item.get("image") or {}).get("imageUrl")
    if img:
        return img
    thumb = item.get("thumbnailImages") or []
    if thumb and thumb[0].get("imageUrl"):
        return thumb[0]["imageUrl"]
    addl = item.get("additionalImages") or []
    if addl and addl[0].get("imageUrl"):
        return addl[0]["imageUrl"]
    return value or None


def _buying_options_csv(value: Any, item: dict) -> Optional[str]:
    bo = item.get("buyingOptions") or []
    return ",".join(bo) if bo else None


def _json_dumps(value: Any, item: dict) -> Optional[str]:
    if value is None:
        return None
    try: return json.dumps(value)
    except (TypeError, ValueError): return None


def _condition_lower(value: Any, item: dict) -> Optional[str]:
    return str(value).strip() if value is not None else None


def _seller_username(value: Any, item: dict) -> Optional[str]:
    return (item.get("seller") or {}).get("username") or value


def _seller_feedback_score(value: Any, item: dict) -> Optional[int]:
    score = (item.get("seller") or {}).get("feedbackScore")
    if score is None:
        return None
    try: return int(score)
    except (TypeError, ValueError): return None


def _location_country(value: Any, item: dict) -> Optional[str]:
    return (item.get("itemLocation") or {}).get("country") or value


def _location_postal(value: Any, item: dict) -> Optional[str]:
    return (item.get("itemLocation") or {}).get("postalCode") or value


TRANSFORMS = {
    "ebay_id_prefix": _ebay_id_prefix,
    "parse_float": _parse_float,
    "parse_int": _parse_int,
    "iso_date": _iso_date,
    "feedback_pct_to_5": _feedback_pct_to_5,
    "affiliate_url": _affiliate_url,
    "image_first": _image_first,
    "buying_options_csv": _buying_options_csv,
    "json_dumps": _json_dumps,
    "condition_lower": _condition_lower,
    "seller_username": _seller_username,
    "seller_feedback_score": _seller_feedback_score,
    "location_country": _location_country,
    "location_postal": _location_postal,
}


def _resolve_path(item: dict, path: str) -> Any:
    """Dotted-path lookup. e.g. 'price.value', 'seller.username'.
    Returns None if any segment is missing.
    """
    if not path:
        return None
    cur: Any = item
    for seg in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list):
            try: cur = cur[int(seg)]
            except (ValueError, IndexError): return None
        else:
            return None
    return cur


def apply_mapping(item: dict, mapping: dict, *, site_constants: Optional[dict] = None) -> dict:
    """Convert a Browse API item to a destination row using the mapping doc.
    Caller passes site-level constants (e.g. {"site_id": "<uuid>"}) that
    override mapping.constants — useful when the same mapping is shared
    across sites with different ids.
    """
    row: dict = {}
    for f in mapping.get("fields", []):
        col = f["destination_column"]
        path = f.get("source_path") or ""
        raw = _resolve_path(item, path) if path else None
        tform = f.get("transform")
        if tform:
            fn = TRANSFORMS.get(tform)
            if fn is None:
                logger.warning("unknown transform %s — using raw", tform)
                row[col] = raw
            else:
                try: row[col] = fn(raw, item)
                except Exception as e:
                    logger.warning("transform %s failed for col=%s: %s", tform, col, e)
                    row[col] = f.get("default")
        else:
            row[col] = raw if raw is not None else f.get("default")
    constants = {c["destination_column"]: c["value"] for c in (mapping.get("constants") or [])}
    if site_constants:
        constants.update(site_constants)
    row.update(constants)
    return row
