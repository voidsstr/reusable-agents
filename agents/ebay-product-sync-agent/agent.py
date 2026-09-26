"""ebay-product-sync-agent — keep a destination product database stocked
with FRESH eBay listings per a configured set of search queries.

Three phases:

  PHASE 1  schema discovery + mapping proposal (one-time per destination)
    * Introspect the destination table.
    * Pull a sample eBay item.
    * Ask Claude to propose a mapping (eBay field → destination column +
      transform).
    * Send the proposal via email; raise ConfirmationPending until the
      operator approves.
    * On approval, store the mapping in framework storage. Subsequent runs
      skip this phase.

  PHASE 2  ingestion (every cron tick)
    * For each (category, seed_query) pair, call eBay Browse API.
    * Apply the approved mapping to each item.
    * Upsert into the destination table.
    * Mark stale rows (last refreshed > X hours ago) inactive — eBay
      listings are short-lived.

  PHASE 3  confirmation handling (any time)
    * The framework's responder agent processes inbound email replies
      and writes confirmation records. The next run picks them up and
      either applies the mapping (approve) or aborts (reject).

This agent is generic. SpecPicks supplies a site.yaml with destination
DSN + seed queries; another operator with their own eBay account can
register a different site.yaml against a different destination DB.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

_AGENT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _AGENT_DIR.parent.parent
for p in (_AGENT_DIR, _REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from framework.core.agent_base import AgentBase, RunResult           # noqa: E402
from framework.core.ai_providers import ai_client_for                # noqa: E402
from framework.core.confirmations import (                            # noqa: E402
    ConfirmationPending, ConfirmationRecord, get_confirmation,
    write_confirmation,
)
from framework.core.email_codes import new_request_id, encode_subject # noqa: E402
from framework.core.llm_json import (                                 # noqa: E402
    extract_json_array, extract_json_object,
)
from framework.core.mailer import LogMailer                           # noqa: E402
from framework.core.storage import get_storage                        # noqa: E402

from db_adapter import make_adapter, DbAdapter, ColumnInfo            # noqa: E402
from ebay_client import EbayClient                                    # noqa: E402
from mapping import (                                                 # noqa: E402
    apply_mapping, mapping_storage_key, TRANSFORMS,
)

AGENT_ID = "ebay-product-sync-agent"
log = logging.getLogger(AGENT_ID)


# ───────────────────────────────────────────────────────────────────
# Mapping proposal (Claude)
# ───────────────────────────────────────────────────────────────────

MAPPING_SYS = """You are a database schema mapper. The operator runs an
e-commerce affiliate site. Their model: a `products` table holds CANONICAL
hardware items (one row per real-world SKU like "3dfx Voodoo 3 3000 AGP")
with stable hardware specifications. A separate `listings` table holds
short-lived eBay listings (current price, condition, seller, end-time)
that link back to the canonical product via foreign key.

eBay listings expire fast — auctions end, items get sold. Listings rotate.
The canonical product persists. The product page shows current listings
joined from the listings table.

You receive:
  1. A list of destination columns for the `products` table (or empty if
     the operator wants you to design one from scratch).
  2. A list of destination columns for the `listings` table (or empty if
     the operator wants you to design one from scratch).
  3. 1–3 sample existing rows from each (or empty if tables don't exist).
  4. A real eBay Browse API itemSummary sample showing the source shape.
  5. The operator's selected `mode`:
        - "use-existing-products-and-listings": both tables already exist
        - "use-existing-products-new-listings": products exists, create listings
        - "create-new-tables": create both tables to your design

Produce a STRICT JSON mapping document. No prose, no markdown fences:

{
  "schema_version": "2",
  "mode": "<one of the modes above>",

  "products_table": {
    "name": "<table name>",
    "create_ddl": null,           // SQL CREATE TABLE if mode includes 'create' for products; else null
    "key_columns": ["site_id","asin"],
    "match_columns": ["title_fingerprint"],   // how to dedup canonical products at hydrate time
    "fields": [
      {
        "destination_column": "asin",
        "source_path": "legacyItemId",       // dotted path into eBay item OR null when value comes from hydration
        "from_hydration": null,              // OR a key like "manufacturer" / "family" / "name" populated by canonical extraction
        "transform": "ebay_id_prefix",
        "default": null,
        "notes": "..."
      }
    ],
    "constants": [
      {"destination_column": "source", "value": "ebay"},
      {"destination_column": "is_active", "value": true}
    ]
  },

  "listings_table": {
    "name": "<table name>",
    "create_ddl": null,           // SQL CREATE TABLE if mode includes 'create' for listings; else null
    "key_columns": ["ebay_item_id"],
    "fk_to_product_column": "product_id",   // listings column that holds product FK
    "fk_target": "id",                       // products column being referenced
    "fields": [
      {
        "destination_column": "ebay_item_id",
        "source_path": "legacyItemId",
        "transform": null,
        "default": null
      }
    ],
    "constants": [
      {"destination_column": "marketplace", "value": "ebay"}
    ]
  },

  "notes": "Anything the operator should know before approving."
}

Allowed transforms (use ONLY these names — null or omit for direct copy):
  ebay_id_prefix, parse_float, parse_int, iso_date,
  feedback_pct_to_5, affiliate_url, image_first,
  buying_options_csv, json_dumps, condition_lower,
  seller_username, seller_feedback_score,
  location_country, location_postal

Hard rules:
- The CANONICAL product mapping must use only the durable parts of the
  listing — title, brand, manufacturer, hardware specs. NOT price,
  condition, seller — those go on the listings row.
- Listings carry the volatile fields: price, currency, condition, seller,
  bid count, auction end date, location, listing type.
- For the listings.<key column> use the eBay legacy item id directly
  (NOT prefixed). For products.asin (or equivalent durable id) use
  ebay_id_prefix when the existing product table is keyed by ASIN.
- If `from_hydration` is set, the value comes from canonical extraction
  (run separately by Claude per listing) — `source_path` and `transform`
  are ignored. Common hydration keys: name, manufacturer, family,
  release_year, form_factor, key_specs (JSON).
- For mode=create-new-tables: produce idiomatic Postgres DDL in
  `create_ddl` for the database flavor matched by the operator's existing
  schema (or PostgreSQL 16 default). Include sensible indexes:
    products: btree(site_id, asin), btree(manufacturer), GIN(title_fingerprint)
    listings: btree(ebay_item_id), btree(product_id), btree(updated_at)
- For mode=use-existing-products-new-listings: products fields fill the
  EXISTING product table conservatively (only set columns that have an
  eBay equivalent or hydration source); listings table gets a fresh DDL.
- For mode=use-existing-both: respect existing schemas, fill what fits,
  omit columns without a clean source.
- products.constants must include `source: "ebay"` (or whatever the
  existing table uses to denote eBay-sourced canonical entries) when
  applicable.
"""

HYDRATION_SYS = """You extract canonical hardware product info from a raw
eBay listing title (and optionally short description). The output is the
record that goes into the operator's `products` table — the durable
identity of what the listing is selling, NOT the listing-specific stuff.

Return STRICT JSON only. No prose, no markdown fences:

{
  "name": "Concise canonical name (e.g. '3dfx Voodoo 3 3000 AGP 16MB')",
  "manufacturer": "3dfx",
  "family": "Voodoo 3",                    // model family or null
  "model_number": "3000",                  // null if not a numbered SKU
  "release_year": 1999,                    // null if unknown
  "form_factor": "AGP",                    // socket / interface / connector
  "key_specs": {                           // arbitrary kind-relevant specs
    "vram_mb": 16,
    "memory_type": "SDR",
    "core_clock_mhz": 166
  },
  "category_slug": "retro-gpus",           // best matching category slug
                                            // from the operator's catalog
  "title_fingerprint": "voodoo-3-3000-agp-16mb",   // lowercase-slugified
                                                    // canonical name; used
                                                    // to dedup products
  "confidence": 0.92                       // 0-1 how sure you are
}

Rules:
- Be CONSERVATIVE. If the listing title is ambiguous (e.g. "vintage GPU
  lot") set confidence < 0.5 and leave fields you can't read as null.
- The title_fingerprint MUST collapse to the same string across many
  listings of the same SKU — drop seller-specific words like "tested",
  "pulled", "OEM", "lot of 3", capacity duplicates, etc. Two distinct
  SKUs (Voodoo 3 2000 vs 3000) MUST produce different fingerprints.
- category_slug must be one of the slugs the operator's catalog uses.
- If the listing is for a multi-item lot, return confidence < 0.4 (the
  agent will skip it).
"""


def _format_columns(cols: list[ColumnInfo]) -> str:
    out = []
    for c in cols:
        flags = []
        if c.is_unique: flags.append("UNIQUE")
        if not c.nullable: flags.append("NOT NULL")
        flag_s = " " + "/".join(flags) if flags else ""
        len_s = f"({c.max_length})" if c.max_length else ""
        out.append(f"  - {c.name}: {c.data_type}{len_s}{flag_s}")
    return "\n".join(out) if out else "  (table doesn't exist yet — agent will create it)"


def _format_samples(rows: list[dict]) -> str:
    if not rows: return "(no rows yet)"
    compact = [{k: (str(v)[:120] if v is not None else None) for k, v in r.items()} for r in rows[:3]]
    return json.dumps(compact, indent=2, default=str)


def propose_mapping_via_claude(
    chat_fn,
    products_columns: list[ColumnInfo],
    products_samples: list[dict],
    listings_columns: list[ColumnInfo],
    listings_samples: list[dict],
    sample_ebay_item: dict,
    products_table_name: str,
    listings_table_name: str,
    mode: str,
    site_id_value: Optional[str] = None,
    db_kind: str = "postgres",
    category_slugs: Optional[list[str]] = None,
) -> dict:
    # `chat_fn` is a callable: (messages, **kwargs) -> str. Pass
    # `self.ai_chat` so this function inherits the framework's
    # claude→copilot→azure_openai fallback chain. (Was an AIClient
    # instance pre-fallback; refactor preserves the call shape.)
    """Build the prompt + call Claude + parse JSON. Returns the v2 mapping doc.
    Raises on parse failure."""
    item_text = json.dumps(sample_ebay_item, indent=2, default=str)
    if len(item_text) > 4000:
        item_text = item_text[:4000] + "\n... (truncated)"

    cat_text = ""
    if category_slugs:
        cat_text = "\nCategory slugs operator's catalog uses:\n  " + ", ".join(sorted(category_slugs)[:60])

    user_prompt = f"""Database flavor: {db_kind}
Mode: {mode}
Operator-provided site_id constant: {site_id_value or '(none)'}

PRODUCTS table: `{products_table_name}`
{_format_columns(products_columns)}

Sample products rows:
{_format_samples(products_samples)}

LISTINGS table: `{listings_table_name}`
{_format_columns(listings_columns)}

Sample listings rows:
{_format_samples(listings_samples)}
{cat_text}

Sample eBay Browse API itemSummary:
{item_text}

Produce the v2 mapping JSON. If `mode` includes "create" for a table,
include an idiomatic CREATE TABLE statement in `create_ddl` for that
section. If the table already exists for that section, set
`create_ddl: null` and only fill columns that exist."""

    raw = chat_fn(
        [{"role": "system", "content": MAPPING_SYS},
         {"role": "user", "content": user_prompt}],
        max_tokens=10000,
        # claude-cli-only kwargs — OpenAI-shape clients ignore them:
        max_turns=4,
        timeout=600,
    )
    # Use raw_decode-based extractor — tolerates fenced blocks, prose
    # preface/suffix, and trailing footnote-style references like [1].
    # The old find('{')/rfind('}') approach broke when the model
    # appended commentary containing `}` or `]`.
    try:
        return extract_json_object(raw)
    except ValueError as e:
        raise RuntimeError(f"Claude did not return a JSON object: {e}; got: {raw[:300]!r}")


# ───────────────────────────────────────────────────────────────────
# Mapping email proposal
# ───────────────────────────────────────────────────────────────────

def _render_table_section(label: str, section: dict, accent: str = "#1e293b") -> str:
    name = section.get("name") or "?"
    keys = section.get("key_columns") or []
    fields = section.get("fields") or []
    consts = section.get("constants") or []
    create_ddl = section.get("create_ddl")

    rows = ""
    for f in fields:
        src = f.get("source_path") or ""
        if f.get("from_hydration"):
            src = f"&lt;hydration:{f['from_hydration']}&gt;"
        rows += (
            f"<tr>"
            f"<td><code>{f.get('destination_column','')}</code></td>"
            f"<td><code>{src or '—'}</code></td>"
            f"<td>{f.get('transform') or '<i>direct</i>'}</td>"
            f"<td><code>{f.get('default') if f.get('default') is not None else ''}</code></td>"
            f"<td>{f.get('notes','') or ''}</td>"
            f"</tr>"
        )
    consts_html = "".join(
        f"<li><code>{c.get('destination_column')}</code> = "
        f"<code>{json.dumps(c.get('value'))}</code></li>"
        for c in consts
    )
    ddl_html = ""
    if create_ddl:
        ddl_html = f"""<details style="margin:12px 0">
  <summary style="cursor:pointer;color:{accent};font-weight:600">
    Proposed CREATE TABLE statement (this section will run on approval)
  </summary>
  <pre style="background:#0f172a;color:#e2e8f0;padding:12px;border-radius:6px;
              font-size:12px;overflow-x:auto;margin-top:8px">{create_ddl}</pre>
</details>"""

    fk = ""
    if section.get("fk_to_product_column"):
        fk = (f"<p style='margin:6px 0;font-size:13px'>"
              f"<strong>FK to products:</strong> "
              f"<code>{section['fk_to_product_column']}</code> → "
              f"<code>{section.get('fk_target','id')}</code></p>")

    return f"""<h2 style="color:{accent}">{label}: <code>{name}</code></h2>
<p style="margin:6px 0;font-size:13px"><strong>Upsert key:</strong>
   {", ".join(f"<code>{k}</code>" for k in keys) or "<i>none</i>"}</p>
{fk}{ddl_html}
<h3 style="font-size:1rem;margin:14px 0 6px">Field mapping ({len(fields)} columns)</h3>
<table>
  <thead><tr><th>Destination column</th><th>Source</th>
    <th>Transform</th><th>Default</th><th>Notes</th></tr></thead>
  <tbody>{rows or '<tr><td colspan=5><i>(no fields)</i></td></tr>'}</tbody>
</table>
<h3 style="font-size:1rem;margin:14px 0 6px">Constants</h3>
<ul>{consts_html or '<li><i>none</i></li>'}</ul>"""


def _render_mapping_html(mapping: dict, *,
                          confirmation_id: str, request_id: str, agent_id: str,
                          site_id: str,
                          existing_products_table: str,
                          existing_listings_table: str,
                          existing_listings_exists: bool,
                          ) -> tuple[str, str]:
    """Returns (subject, html). Subject embeds the request_id so inbound
    replies can be matched by the responder agent."""
    pt = mapping.get("products_table") or {}
    lt = mapping.get("listings_table") or {}
    mode = mapping.get("mode") or "use-existing-products-new-listings"

    # Mode-specific banner.
    if mode == "create-new-tables":
        mode_label = "Create both tables"
        mode_color = "#16a34a"
    elif mode == "use-existing-products-new-listings":
        mode_label = "Use existing products table, create new listings table"
        mode_color = "#2563eb"
    else:
        mode_label = "Use existing tables for both"
        mode_color = "#9333ea"

    products_section = _render_table_section("Canonical products", pt, "#1e293b")
    listings_section = _render_table_section("eBay listings", lt, "#dc2626")

    notes_html = ""
    if mapping.get("notes"):
        notes_html = (
            f'<div class="kbox"><strong>Notes from Claude:</strong> '
            f'{mapping["notes"]}</div>'
        )

    subject_text = encode_subject(
        agent_id, request_id,
        f"eBay product/listings mapping for {site_id}",
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;line-height:1.5;max-width:920px;margin:0 auto;padding:24px;}}
h1{{font-size:22px;color:#0f172a;border-bottom:1px solid #e2e8f0;padding-bottom:12px;margin:0 0 12px 0}}
h2{{font-size:1.15rem;margin:28px 0 8px;color:#0f172a}}
h3{{margin:14px 0 6px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #e2e8f0;padding:6px 10px;text-align:left;vertical-align:top}}
th{{background:#f1f5f9}}
code{{background:#f8fafc;padding:1px 4px;border-radius:3px;font-size:12.5px}}
.kbox{{background:#fff7ed;border:1px solid #fdba74;padding:12px 14px;border-radius:8px;margin:18px 0}}
.modebox{{background:#eff6ff;border:1px solid #93c5fd;padding:14px 16px;border-radius:8px;margin:14px 0}}
.actions{{margin-top:32px;padding:18px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0}}
.option{{padding:10px 12px;background:#fff;border:1px solid #e2e8f0;border-radius:6px;margin:8px 0}}
.option strong{{display:inline-block;background:#f1f5f9;padding:2px 8px;border-radius:4px;font-family:monospace;font-size:12.5px;margin-right:8px}}
</style></head><body>

<h1>eBay product/listings ingestion proposal</h1>
<p>Site: <strong>{site_id}</strong>. Generated by <code>{agent_id}</code>.
   Two-table architecture: canonical <strong>products</strong> ←→ short-lived
   <strong>listings</strong> joined by foreign key.</p>

<div class="modebox">
  <strong>Proposed mode:</strong>
  <span style="color:{mode_color};font-weight:700">{mode_label}</span>
</div>

{notes_html}

{products_section}

{listings_section}

<div class="actions">
  <h2 style="margin-top:0">How to respond</h2>
  <p>Reply to this email with one of the following commands:</p>

  <div class="option">
    <strong>APPROVE</strong>
    Use this mode + mapping as-is. The next agent run executes any
    `create_ddl` (if mode includes new-tables), stores the mapping, and
    begins ingesting + hydrating canonical products from listings.
  </div>

  <div class="option">
    <strong>CREATE NEW</strong>
    Discard the existing-table assumptions. Agent generates fresh
    <code>products</code> and <code>listings</code> tables to its own
    canonical design and uses those instead.
  </div>

  <div class="option">
    <strong>USE TABLES products=&lt;name&gt; listings=&lt;name&gt;</strong>
    Re-target both tables. Example:<br>
    <code>USE TABLES products=catalog.hardware listings=catalog.ebay_listings</code><br>
    Agent regenerates the mapping against those tables on the next run.
  </div>

  <div class="option">
    <strong>EDIT &lt;json&gt;</strong>
    Paste a corrected v2 mapping JSON below the EDIT line. The agent
    overwrites the proposal with your version, then proceeds as if you
    had typed APPROVE.
  </div>

  <div class="option">
    <strong>REJECT &lt;reason&gt;</strong>
    Discard. Re-run the agent later to generate a new proposal.
  </div>

  <p style="font-size:12px;color:#64748b;margin-top:14px">
    Confirmation id: <code>{confirmation_id}</code><br>
    Request id: <code>{request_id}</code><br>
    Storage key: <code>{mapping_storage_key(agent_id, site_id)}</code><br>
    Existing products table inspected: <code>{existing_products_table}</code><br>
    Existing listings table inspected:
    <code>{existing_listings_table}</code> ({"present" if existing_listings_exists else "does not exist yet"})
  </p>
</div>
</body></html>"""
    return subject_text, html


# ───────────────────────────────────────────────────────────────────
# Stale-listing reaper
# ───────────────────────────────────────────────────────────────────

def _products_lacking_listings(
    adapter: DbAdapter, products_table: str, listings_table: str,
    fk_col: str, limit: int = 40,
) -> list[dict]:
    """Coverage goal — products with zero ACTIVE eBay listings should be
    queried first. Returns a list of `{query, category_slug}` dicts where
    `query` is derived from the product's canonical name, suitable to
    feed back into the eBay Browse API.

    Best-effort: errors are swallowed so the agent falls back to seed
    rotation cleanly if the schema differs from what we expect.
    """
    if limit <= 0:
        return []
    out: list[dict] = []
    try:
        if adapter.kind == "postgres":
            cur = adapter.conn.cursor()
            # products has `title` (not `name`); category_slug doesn't exist
            # — categories live in a sibling table joined via category_id FK.
            # Fall through cleanly on errors so the upsert path still works.
            # Bias ordering: products whose most-recently-deactivated
            # listing went inactive *recently* bubble to the top. Newly
            # emptied products (audit just killed their last live listing)
            # get backfilled in the same run instead of waiting for the
            # round-robin to circle back around. NULLS LAST so products
            # that have NEVER had a listing fall to the back — those are
            # likely stale catalog rows we'll handle as remaining slots
            # allow.
            cur.execute(f"""
                SELECT p.id, p.title, COALESCE(c.slug, '') AS category_slug,
                       last_seen_listing.last_updated
                  FROM {products_table} p
                  LEFT JOIN categories c ON c.id = p.category_id
                  LEFT JOIN LATERAL (
                      SELECT MAX(updated_at) AS last_updated
                        FROM {listings_table} l
                       WHERE l.{fk_col} = p.id
                  ) last_seen_listing ON true
                 WHERE NOT EXISTS (
                     SELECT 1 FROM {listings_table} l
                      WHERE l.{fk_col} = p.id
                        AND l.is_active = true
                 )
                 ORDER BY last_seen_listing.last_updated DESC NULLS LAST,
                          p.id
                 LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            for r in rows:
                name = (r[1] or "").strip()
                if not name:
                    continue
                out.append({"query": name, "category_slug": r[2] or None})
        elif adapter.kind == "azure-sql":
            cur = adapter.conn.cursor()
            cur.execute(f"""
                SELECT TOP (?) p.id, p.title, COALESCE(c.slug, '') AS category_slug
                  FROM {products_table} p
                  LEFT JOIN categories c ON c.id = p.category_id
                  OUTER APPLY (
                      SELECT MAX(updated_at) AS last_updated
                        FROM {listings_table} l
                       WHERE l.{fk_col} = p.id
                  ) last_seen_listing
                 WHERE NOT EXISTS (
                     SELECT 1 FROM {listings_table} l
                      WHERE l.{fk_col} = p.id
                        AND l.is_active = 1
                 )
                 ORDER BY last_seen_listing.last_updated DESC,
                          p.id
            """, (limit,))
            rows = cur.fetchall()
            cur.close()
            for r in rows:
                name = (r[1] or "").strip()
                if not name:
                    continue
                out.append({"query": name, "category_slug": r[2] or None})
    except Exception as e:
        log.warning("coverage probe failed (%s) — falling back to seeds only", e)
        # Critical: rollback so subsequent statements on this connection don't
        # all fail with "current transaction is aborted". This was the root
        # cause of all-products-upsert-failed in the 04-29 run.
        if adapter.kind == "postgres":
            try:
                adapter.conn.rollback()
            except Exception:
                pass
    return out


def _ensure_item_end_date_column(adapter: DbAdapter, table: str) -> None:
    """Idempotent ALTER TABLE — adds `item_end_date` if missing. Safe to
    call every run; cheap when the column already exists."""
    # Long Claude-hydration calls upstream can outlive Azure Postgres' 5-min
    # idle timeout. Probe + reconnect before issuing DDL.
    if hasattr(adapter, "ensure_open"):
        try: adapter.ensure_open()
        except Exception: pass
    try:
        if adapter.kind == "postgres":
            cur = adapter.conn.cursor()
            cur.execute(
                f"ALTER TABLE {table} "
                f"ADD COLUMN IF NOT EXISTS item_end_date TIMESTAMPTZ"
            )
            adapter.conn.commit()
            cur.close()
        elif adapter.kind == "azure-sql":
            cur = adapter.conn.cursor()
            cur.execute(f"""
                IF COL_LENGTH('{table}', 'item_end_date') IS NULL
                BEGIN
                  ALTER TABLE {table} ADD item_end_date DATETIMEOFFSET NULL
                END
            """)
            adapter.conn.commit()
            cur.close()
    except Exception as e:
        log.warning("ensure item_end_date column on %s failed: %s", table, e)


# ───────────────────────────────────────────────────────────────────
# Rolling re-price — keep existing listings' price + liveness current
# ───────────────────────────────────────────────────────────────────

DEFAULT_EBAY_API = {
    # eBay Browse quota is per app per day (basic plan: 5,000 calls/day).
    # Searches and item lookups share it.
    "daily_call_limit": 5000,
    # How many runs the cron fires per day (hourly = 24). Used to split
    # the daily quota into a per-run allowance.
    "runs_per_day": 24,
    # Fraction of the daily quota held back for on-demand/manual runs.
    "headroom_pct": 0.10,
}

DEFAULT_REPRICE = {
    "enabled": True,
    # Hard cap on item-lookup calls per run. None → derived from the daily
    # quota: usable_daily // runs_per_day − planned search calls.
    "max_calls_per_run": None,
    # Active listings re-checked at most once per this many hours; anything
    # fresher (just seen in search, or just re-priced) is skipped.
    "min_age_hours": 12,
    # Inactive listings with no recorded end date (deactivated by the time
    # reaper, not confirmed ended) are re-checked newest-first within this
    # window and revived when still live.
    "revive_window_days": 90,
    # Roll live listing prices up onto eBay-sourced PRODUCT rows. OFF by
    # default: a destination may keep products.price NULL for eBay stock on
    # purpose (the live price lives in the listings table) and render any
    # fresh products.price with a marketplace-specific sourcing claim, so a
    # derived price there is a new, possibly false, claim. Opt in per site
    # only once the site renders eBay-sourced product prices as such.
    "update_product_prices": False,
    # Products whose key column starts with this prefix are eBay-sourced
    # (mapping transform `ebay_id_prefix`); their price is the lowest live
    # USD listing price. Other products (e.g. Amazon rows that also carry
    # eBay listings) keep their own price. "" disables product updates.
    "product_key_column": "asin",
    "product_key_prefix": "EBAY_",
    # When an eBay-sourced product has no live listing left, NULL its price
    # so pricing-integrity filters (price IS NOT NULL) stop promoting it.
    # The product row itself is never deactivated.
    "clear_price_when_unlisted": True,
    # Circuit breaker: when at least this share of a run's lookups (min 20)
    # come back not-found, the lookup itself is presumed broken (the
    # 2026-09 /item/<legacy id> bug 404'd every live listing) and no row is
    # switched off for being not-found that run.
    "max_not_found_ratio": 0.9,
}

_LIVE_STATUSES = {"IN_STOCK", "LIMITED_STOCK"}


def _reprice_config(cfg: dict) -> dict:
    out = dict(DEFAULT_REPRICE)
    out.update({k: v for k, v in ((cfg or {}).get("reprice") or {}).items()})
    # Legacy knob from the old audit pass.
    if out.get("max_calls_per_run") is None and (cfg or {}).get("audit_max_per_run") is not None:
        out["max_calls_per_run"] = int(cfg["audit_max_per_run"])
    return out


def _ebay_call_budget(cfg: dict, *, used_today: int,
                      planned_search_calls: int,
                      live: Optional[dict] = None,
                      now: Optional[datetime] = None) -> dict:
    """Split eBay's per-app daily call quota into this run's re-price cap.

    reprice_cap = min(per-run allowance, what is left of today's usable
    quota after this run's searches).

    `live` is eBay's own count (EbayClient.browse_rate_limit): it covers
    every caller sharing the app keys, not just this engine's ledger, so
    it wins over `used_today` when present. With its `reset` time the
    usable remainder is also PACED over the runs left before the reset, so
    other callers' consumption slows this run down instead of this engine
    draining the quota and 429-ing them. Pure — unit-tested."""
    api = dict(DEFAULT_EBAY_API)
    api.update((cfg or {}).get("ebay_api") or {})
    daily = max(0, int(api["daily_call_limit"]))
    runs = max(1, int(api["runs_per_day"]))
    headroom = min(0.9, max(0.0, float(api["headroom_pct"])))
    searches = max(0, planned_search_calls)
    used = max(0, used_today)
    runs_left = None
    if live and live.get("limit"):
        # eBay's count is authoritative; a lower configured limit still caps.
        daily = min(daily, int(live["limit"]))
        used = max(used, int(live["limit"]) - int(live.get("remaining") or 0))
        reset = _parse_iso(live.get("reset"))
        if reset is not None:
            now = now or datetime.now(timezone.utc)
            hours_left = max(0.0, (reset - now).total_seconds() / 3600.0)
            runs_left = max(1, math.ceil(hours_left * runs / 24.0))
    usable = int(daily * (1.0 - headroom))
    rp = _reprice_config(cfg)
    if rp.get("max_calls_per_run") is not None:
        per_run = max(0, int(rp["max_calls_per_run"]))
    else:
        per_run = max(0, usable // runs - searches)
    left_today = max(0, usable - used - searches)
    cap = min(per_run, left_today)
    if runs_left is not None:
        cap = min(cap, max(0, (usable - used) // runs_left - searches))
    if not rp.get("enabled", True):
        cap = 0
    return {"daily_call_limit": daily, "usable_today": usable,
            "used_today": used, "per_run_cap": per_run,
            "runs_left_before_reset": runs_left,
            "reprice_cap": cap}


def _usage_prefix(client_id: str, day: str) -> str:
    import hashlib
    app = hashlib.sha1((client_id or "").encode()).hexdigest()[:12]
    return f"agents/{AGENT_ID}/api-usage/{day}/{app}/"


def _read_api_usage_today(storage, client_id: str, day: str) -> int:
    """Calls already made today by every instance sharing this eBay app
    (one ledger file per instance). Unreadable storage → 0 (eBay's 429
    stays the hard limit)."""
    total = 0
    try:
        for key in storage.list_prefix(_usage_prefix(client_id, day)) or []:
            d = storage.read_json(key) or {}
            total += int(d.get("calls") or 0)
    except Exception as e:
        log.warning("eBay usage ledger read failed (treating as 0): %s", e)
    return total


def _record_api_usage(storage, client_id: str, day: str, instance_id: str,
                      calls_by_op: dict) -> None:
    n = sum(int(v) for v in (calls_by_op or {}).values())
    if n <= 0:
        return
    key = _usage_prefix(client_id, day) + f"{instance_id}.json"
    try:
        d = storage.read_json(key) or {}
        d["calls"] = int(d.get("calls") or 0) + n
        by_op = d.get("by_operation") or {}
        for op, c in calls_by_op.items():
            by_op[op] = int(by_op.get(op) or 0) + int(c)
        d["by_operation"] = by_op
        d["updated_at"] = _now()
        storage.write_json(key, d)
    except Exception as e:
        log.warning("eBay usage ledger write failed: %s", e)


def _parse_iso(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _item_state(item: dict, *, now: datetime) -> dict:
    """Interpret one Browse getItem payload → {live, price, currency,
    item_end_date, status}. A listing is live unless eBay reports it
    OUT_OF_STOCK or its end date has passed."""
    price_obj = item.get("price") or item.get("currentBidPrice") or {}
    try:
        price = float(price_obj.get("value")) if price_obj.get("value") is not None else None
    except (TypeError, ValueError):
        price = None
    avail = item.get("estimatedAvailabilities") or []
    status = ((avail[0] or {}).get("estimatedAvailabilityStatus") if avail else None) or ""
    end = _parse_iso(item.get("itemEndDate"))
    ended = end is not None and end <= now
    live = not ended and (not status or status in _LIVE_STATUSES)
    return {"live": live, "price": price,
            "currency": price_obj.get("currency") or None,
            "item_end_date": end.isoformat() if end else None,
            "status": "ENDED" if ended else (status or "UNKNOWN")}


def _group_state(items: list[dict], *, now: datetime) -> dict:
    """A multi-variation listing is live while any variation is; its price
    is the cheapest live variation (USD preferred)."""
    states = [_item_state(it, now=now) for it in (items or [])]
    live = [s for s in states if s["live"] and s["price"] is not None]
    if not live:
        return {"live": False, "price": None, "currency": None,
                "item_end_date": None, "status": "OUT_OF_STOCK"}
    usd = [s for s in live if (s["currency"] or "USD") == "USD"] or live
    best = min(usd, key=lambda s: s["price"])
    return dict(best, status="GROUP")


def _select_reprice_candidates(adapter: DbAdapter, table: str, fk_col: str, *,
                                limit: int, min_age_hours: int,
                                revive_window_days: int) -> list[tuple]:
    """(ebay_item_id, product_id, price, is_active) rows to re-check:
    stale ACTIVE listings oldest-first, then — with whatever budget is
    left — INACTIVE listings with no confirmed end, newest-first (most
    likely still live).

    "Confirmed end" = item_end_date <= updated_at: the re-price pass stamps
    both when eBay says ended/out of stock. A NULL end date, or one that was
    still in the future when the row was last seen (a GTC renewal date the
    time reaper outlived), is unconfirmed and worth a re-check."""
    if limit <= 0:
        return []
    if hasattr(adapter, "ensure_open"):
        try: adapter.ensure_open()
        except Exception: pass
    rows: list[tuple] = []
    try:
        cur = adapter.conn.cursor()
        if adapter.kind == "postgres":
            cur.execute(f"""
                SELECT ebay_item_id, {fk_col}, price, is_active
                  FROM {table}
                 WHERE is_active = true
                   AND ebay_item_id IS NOT NULL AND ebay_item_id <> ''
                   AND updated_at < NOW() - make_interval(hours => %s)
                 ORDER BY updated_at ASC
                 LIMIT %s
            """, (int(min_age_hours), int(limit)))
            rows = list(cur.fetchall())
            if len(rows) < limit and revive_window_days > 0:
                cur.execute(f"""
                    SELECT ebay_item_id, {fk_col}, price, is_active
                      FROM {table}
                     WHERE is_active = false
                       AND (item_end_date IS NULL OR item_end_date > updated_at)
                       AND ebay_item_id IS NOT NULL AND ebay_item_id <> ''
                       AND updated_at > NOW() - make_interval(days => %s)
                     ORDER BY updated_at DESC
                     LIMIT %s
                """, (int(revive_window_days), int(limit - len(rows))))
                rows += list(cur.fetchall())
        elif adapter.kind == "azure-sql":
            cur.execute(f"""
                SELECT TOP (?) ebay_item_id, {fk_col}, price, is_active
                  FROM {table}
                 WHERE is_active = 1
                   AND ebay_item_id IS NOT NULL AND ebay_item_id <> ''
                   AND updated_at < DATEADD(hour, -?, SYSUTCDATETIME())
                 ORDER BY updated_at ASC
            """, (int(limit), int(min_age_hours)))
            rows = list(cur.fetchall())
            if len(rows) < limit and revive_window_days > 0:
                cur.execute(f"""
                    SELECT TOP (?) ebay_item_id, {fk_col}, price, is_active
                      FROM {table}
                     WHERE is_active = 0
                       AND (item_end_date IS NULL OR item_end_date > updated_at)
                       AND ebay_item_id IS NOT NULL AND ebay_item_id <> ''
                       AND updated_at > DATEADD(day, -?, SYSUTCDATETIME())
                     ORDER BY updated_at DESC
                """, (int(limit - len(rows)), int(revive_window_days)))
                rows += list(cur.fetchall())
        cur.close()
    except Exception as e:
        log.warning("reprice: candidate query failed: %s", e)
        if adapter.kind == "postgres":
            try: adapter.conn.rollback()
            except Exception: pass
        return []
    return rows


def _reprice_listings(adapter: DbAdapter, table: str, ebay, *, fk_col: str,
                      max_calls: int, min_age_hours: int = 12,
                      revive_window_days: int = 90, dry_run: bool = False,
                      max_not_found_ratio: float = 0.9,
                      now: Optional[datetime] = None) -> dict:
    """Rolling re-price: look each candidate up via Browse
    get_item_by_legacy_id (getItems batch is a restricted API), then
      • live   → write price/currency/end date, is_active=true, bump
                 updated_at (+ verified_active_at when the column exists)
      • ended / out of stock → is_active=false and item_end_date=NOW()
                 (row kept — never deleted; a later search sighting
                 rewrites item_end_date and re-activates it)
    Stops at `max_calls` Browse calls or on the first 429. When
    ≥ `max_not_found_ratio` of ≥ 20 lookups are not-found, those rows are
    left untouched (suspected broken lookup, not real ends).

    Returns stats incl. `product_ids` touched (for the product price
    roll-up) and up to 10 `samples`."""
    from ebay_client import EbayItemGroup, EbayRateLimited  # noqa: E402
    now = now or datetime.now(timezone.utc)
    stats: dict = {"checked": 0, "repriced": 0, "price_changed": 0,
                   "revived": 0, "ended": 0, "unavailable": 0, "errors": 0,
                   "rate_limited": False, "api_calls": 0,
                   "product_ids": set(), "samples": []}
    if max_calls <= 0:
        return stats
    rows = _select_reprice_candidates(
        adapter, table, fk_col, limit=max_calls,
        min_age_hours=min_age_hours, revive_window_days=revive_window_days)
    if not rows:
        return stats
    calls_at_start = ebay.total_calls
    live_updates: list[tuple] = []   # (item_id, price, currency, end_iso)
    dead_ids: list[str] = []
    not_found_ids: set = set()
    for item_id, product_id, old_price, was_active in rows:
        if ebay.total_calls - calls_at_start >= max_calls:
            break
        item_id = str(item_id)
        try:
            try:
                item = ebay.get_item(item_id)
                state = _item_state(item, now=now) if item is not None else None
            except EbayItemGroup:
                variants = ebay.get_items_by_item_group(item_id)
                state = _group_state(variants, now=now) if variants else None
        except EbayRateLimited as e:
            stats["rate_limited"] = True
            log.warning("reprice: eBay rate limit hit after %d checks — stopping (%s)",
                        stats["checked"], str(e)[:160])
            break
        except Exception as e:
            stats["errors"] += 1
            if stats["errors"] <= 5:
                log.warning("reprice: lookup %s failed: %s", item_id, str(e)[:200])
            continue
        stats["checked"] += 1
        if product_id is not None:
            stats["product_ids"].add(product_id)
        if state is None:
            outcome = "ended"
            stats["ended"] += 1
            dead_ids.append(item_id)
            not_found_ids.add(item_id)
        elif not state["live"]:
            outcome = "unavailable"
            stats["unavailable"] += 1
            dead_ids.append(item_id)
        else:
            outcome = "live"
            stats["repriced"] += 1
            if not was_active:
                stats["revived"] += 1
            if (state["price"] is not None and old_price is not None
                    and abs(float(old_price) - state["price"]) >= 0.005):
                stats["price_changed"] += 1
            live_updates.append((item_id, state["price"], state["currency"],
                                 state["item_end_date"]))
        if len(stats["samples"]) < 10:
            stats["samples"].append({
                "ebay_item_id": item_id, "outcome": outcome,
                "was_active": bool(was_active),
                "old_price": float(old_price) if old_price is not None else None,
                "new_price": state["price"] if state else None,
                "status": state["status"] if state else "NOT_FOUND",
            })
    stats["api_calls"] = ebay.total_calls - calls_at_start
    if (stats["ended"] >= 20
            and stats["ended"] >= max_not_found_ratio * stats["checked"]):
        stats["not_found_suspect"] = True
        log.warning("reprice: %d/%d lookups not-found — treating as a broken "
                    "lookup, not real ends; those rows are left as they are",
                    stats["ended"], stats["checked"])
        dead_ids = [i for i in dead_ids if i not in not_found_ids]

    if dry_run or not (live_updates or dead_ids):
        return stats
    has_verified = False
    try:
        has_verified = any(c.name == "verified_active_at"
                           for c in adapter.introspect_table(table))
    except Exception:
        pass
    if hasattr(adapter, "ensure_open"):
        try: adapter.ensure_open()
        except Exception: pass
    try:
        cur = adapter.conn.cursor()
        if adapter.kind == "postgres":
            from psycopg2.extras import execute_values
            verified = ", verified_active_at = NOW()" if has_verified else ""
            if live_updates:
                execute_values(cur, f"""
                    UPDATE {table} AS t
                       SET price = COALESCE(v.price, t.price),
                           currency = COALESCE(v.currency, t.currency),
                           item_end_date = v.end_date,
                           is_active = true,
                           updated_at = NOW(){verified}
                      FROM (VALUES %s) AS v(ebay_item_id, price, currency, end_date)
                     WHERE t.ebay_item_id = v.ebay_item_id
                """, live_updates,
                    template="(%s, %s::numeric, %s::varchar, %s::timestamptz)")
            if dead_ids:
                cur.execute(f"""
                    UPDATE {table}
                       SET is_active = false,
                           item_end_date = CASE WHEN item_end_date IS NULL
                                                  OR item_end_date > NOW()
                                                THEN NOW() ELSE item_end_date END,
                           updated_at = NOW()
                     WHERE ebay_item_id = ANY(%s)
                """, (dead_ids,))
        elif adapter.kind == "azure-sql":
            verified = ", verified_active_at = SYSUTCDATETIME()" if has_verified else ""
            for iid, price, currency, end_iso in live_updates:
                cur.execute(f"""
                    UPDATE {table}
                       SET price = COALESCE(?, price), currency = COALESCE(?, currency),
                           item_end_date = ?, is_active = 1,
                           updated_at = SYSUTCDATETIME(){verified}
                     WHERE ebay_item_id = ?
                """, (price, currency, end_iso, iid))
            for iid in dead_ids:
                cur.execute(f"""
                    UPDATE {table}
                       SET is_active = 0,
                           item_end_date = CASE WHEN item_end_date IS NULL
                                                  OR item_end_date > SYSUTCDATETIME()
                                                THEN SYSUTCDATETIME() ELSE item_end_date END,
                           updated_at = SYSUTCDATETIME()
                     WHERE ebay_item_id = ?
                """, (iid,))
        adapter.conn.commit()
        cur.close()
    except Exception as e:
        stats["errors"] += 1
        log.warning("reprice: listing update failed: %s", e)
        try: adapter.conn.rollback()
        except Exception: pass
    return stats


def _refresh_product_prices(adapter: DbAdapter, products_table: str,
                            listings_table: str, fk_col: str,
                            product_ids, *, key_column: str, key_prefix: str,
                            clear_when_unlisted: bool = True) -> dict:
    """Roll live listing prices up to eBay-sourced product rows: price =
    cheapest live USD listing ≥ $1, price_updated_at = NOW(). A stale
    original_price below the new price is cleared (no negative discount).
    Only rows whose `key_column` starts with `key_prefix` are touched —
    marketplace-owned prices (Amazon) are never overwritten."""
    ids = sorted({int(i) for i in (product_ids or []) if i is not None})
    out = {"priced": 0, "cleared": 0}
    if not ids or not key_prefix or adapter.kind != "postgres":
        return out
    if hasattr(adapter, "ensure_open"):
        try: adapter.ensure_open()
        except Exception: pass
    like = key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        cur = adapter.conn.cursor()
        cur.execute(f"""
            WITH s AS (
                SELECT l.{fk_col} AS pid,
                       MIN(l.price) FILTER (
                           WHERE l.is_active AND l.price >= 1.0
                             AND (l.currency IS NULL OR l.currency = 'USD')
                       ) AS min_price
                  FROM {listings_table} l
                 WHERE l.{fk_col} = ANY(%s)
                 GROUP BY l.{fk_col}
            )
            UPDATE {products_table} p
               SET price = s.min_price,
                   currency = 'USD',
                   original_price = CASE WHEN p.original_price < s.min_price
                                         THEN NULL ELSE p.original_price END,
                   price_updated_at = NOW()
              FROM s
             WHERE p.id = s.pid
               AND s.min_price IS NOT NULL
               AND p.{key_column} LIKE %s
        """, (ids, like))
        out["priced"] = cur.rowcount or 0
        if clear_when_unlisted:
            cur.execute(f"""
                UPDATE {products_table} p
                   SET price = NULL, price_updated_at = NOW()
                 WHERE p.id = ANY(%s)
                   AND p.price IS NOT NULL
                   AND p.{key_column} LIKE %s
                   AND NOT EXISTS (
                       SELECT 1 FROM {listings_table} l
                        WHERE l.{fk_col} = p.id AND l.is_active
                          AND l.price >= 1.0
                          AND (l.currency IS NULL OR l.currency = 'USD'))
            """, (ids, like))
            out["cleared"] = cur.rowcount or 0
        adapter.conn.commit()
        cur.close()
    except Exception as e:
        log.warning("product price roll-up failed: %s", e)
        try: adapter.conn.rollback()
        except Exception: pass
    return out


def _mark_stale_inactive(adapter: DbAdapter, table: str, hours: int) -> int:
    """eBay listings expire (auctions end, items get sold). Mark any
    row in the listings table inactive if EITHER:
      - `item_end_date` is in the past, OR
      - `updated_at` hasn't been touched in the last `hours`.
    Returns the count of newly-marked-inactive rows.

    No `source` filter — the two-table architecture dedicates this
    table to eBay listings exclusively, so the predicate would be
    redundant (and broke on schemas that omitted the column entirely).
    """
    if hours <= 0:
        return 0
    if hasattr(adapter, "ensure_open"):
        try: adapter.ensure_open()
        except Exception: pass
    if adapter.kind == "postgres":
        cur = adapter.conn.cursor()
        cur.execute(f"""
            UPDATE {table} SET is_active = false
             WHERE is_active = true
               AND (
                 (item_end_date IS NOT NULL AND item_end_date < NOW())
                 OR updated_at < NOW() - INTERVAL '{int(hours)} hours'
               )
        """)
        adapter.conn.commit()
        n = cur.rowcount
        cur.close()
        return n or 0
    if adapter.kind == "azure-sql":
        cur = adapter.conn.cursor()
        cur.execute(f"""
            UPDATE {table} SET is_active = 0
             WHERE is_active = 1
               AND (
                 (item_end_date IS NOT NULL AND item_end_date < SYSUTCDATETIME())
                 OR updated_at < DATEADD(hour, -?, SYSUTCDATETIME())
               )
        """, (int(hours),))
        adapter.conn.commit()
        n = cur.rowcount
        cur.close()
        return n or 0
    return 0


# ───────────────────────────────────────────────────────────────────
# Main agent
# ───────────────────────────────────────────────────────────────────

class EbayProductSyncAgent(AgentBase):
    agent_id = AGENT_ID
    send_run_summary_email = False  # sends its own completion email

    def run(self, *, run_kind: str = "manual", **kwargs) -> RunResult:
        config_path = os.environ.get("EBAY_PRODUCT_SYNC_CONFIG")
        if not config_path:
            raise RuntimeError("EBAY_PRODUCT_SYNC_CONFIG env var not set")
        config_path = Path(config_path).resolve()
        cfg = yaml.safe_load(config_path.read_text())

        # Load secrets_file (gitignored .env) into process env BEFORE
        # building any clients, so EbayClient + the destination DSN can
        # reference its values via ${ENV_VAR}. Path is relative to the
        # site.yaml directory unless absolute.
        secrets_file = cfg.get("secrets_file")
        if secrets_file:
            sp = Path(secrets_file)
            if not sp.is_absolute():
                sp = config_path.parent / sp
            if sp.exists():
                _load_dotenv(sp)
                log.info("loaded secrets from %s", sp)
            else:
                log.warning("secrets_file %s not found — relying on process env", sp)

        site_id = cfg["site_id"]
        dest = cfg["destination"]
        dsn = os.path.expandvars(dest["dsn"])
        db_kind = dest.get("kind", "postgres")
        # v2 supports both single-table legacy `table:` and the new
        # two-table `products_table:` + `listings_table:` form.
        products_table = (dest.get("products_table")
                          or dest.get("table")
                          or "products")
        listings_table = dest.get("listings_table") or "ebay_listings"
        proposed_mode = (dest.get("mode")
                         or "use-existing-products-new-listings")
        site_constants = dest.get("site_constants") or {}
        stale_hours = int(cfg.get("stale_hours", 72))

        # Resolve eBay client config from yaml `ebay:` block, with
        # `*_env: NAME` indirection or inline strings.
        self._ebay_cfg = cfg.get("ebay") or {}

        # Seeds: list of {category_slug, queries[]} or simpler {queries[]}
        seeds = cfg.get("seeds") or []
        if not isinstance(seeds, list):
            raise RuntimeError("`seeds` must be a list")
        per_query_limit = int(cfg.get("per_query_limit", 60))
        max_queries_per_run = int(cfg.get("max_queries_per_run", 80))
        ebay_filter = cfg.get("ebay_filter") or (
            "buyingOptions:{FIXED_PRICE},"
            "conditions:{USED|NEW|REFURBISHED|FOR_PARTS_OR_NOT_WORKING},"
            "price:[5..5000],priceCurrency:USD"
        )

        # ─── Bring up clients ──────────────────────────────────────
        adapter = make_adapter(db_kind, dsn)
        self._adapter = adapter  # exposed so completion email can run COUNT(*)
        try:
            self._cfg = cfg
            return self._run_inner(
                adapter, products_table, listings_table, proposed_mode,
                site_id, site_constants, dest, cfg,
                seeds, per_query_limit, max_queries_per_run, ebay_filter,
                stale_hours, kwargs,
            )
        finally:
            adapter.close()

    # ───────────────────────────────────────────────────────────────
    def _run_inner(
        self, adapter: DbAdapter, products_table: str, listings_table: str,
        proposed_mode: str, site_id: str, site_constants: dict, dest: dict,
        cfg: dict, seeds: list, per_query_limit: int, max_queries_per_run: int,
        ebay_filter: str, stale_hours: int, kwargs: dict,
    ) -> RunResult:
        storage = get_storage()
        dry_run = bool(kwargs.get("dry_run", False))
        force_remap = bool(kwargs.get("force_remap", False))

        ebay = self._make_ebay_client()
        ebay_health = ebay.healthcheck()
        log.info("eBay OK: %s", ebay_health)

        if kwargs.get("reprice_only"):
            return self._reprice_only(adapter, products_table, listings_table,
                                      site_id, ebay, storage, dry_run,
                                      kwargs.get("reprice_limit"))

        # ─── PHASE 1 — load existing mapping or propose a new one ──
        mapping = None
        if not force_remap:
            existing = storage.read_json(mapping_storage_key(self.agent_id, site_id))
            if existing and existing.get("approved_at") and existing.get("schema_version") == "2":
                mapping = existing
                log.info("loaded approved v2 mapping for site=%s (approved %s by %s)",
                         site_id, mapping.get("approved_at"), mapping.get("approved_by"))

        if not mapping:
            if force_remap:
                storage.write_json(mapping_storage_key(self.agent_id, site_id), {})
                log.info("--force-remap: cleared prior mapping state for %s", site_id)
            mapping = self._propose_or_resume_mapping(
                adapter, products_table, listings_table, proposed_mode,
                site_id, site_constants, ebay, ebay_filter,
                cfg.get("owner_email"),
            )

        # ─── PHASE 2 — apply create_ddl if any (one-time per approval)
        if not mapping.get("ddl_applied"):
            self._apply_ddl(adapter, mapping, dry_run)
            if not dry_run:
                mapping["ddl_applied"] = True
                storage.write_json(mapping_storage_key(self.agent_id, site_id), mapping)

        # ─── PHASE 3 — ingest with hydration
        return self._ingest_v2(
            adapter, mapping, site_id, site_constants, ebay, ebay_filter,
            seeds, per_query_limit, max_queries_per_run, stale_hours, dry_run,
        )

    # ───────────────────────────────────────────────────────────────
    def _reprice_phase(self, adapter: DbAdapter, products_table: str,
                       listings_table: str, fk_col: str, ebay: EbayClient, *,
                       planned_search_calls: int, dry_run: bool,
                       limit_override: Optional[int] = None) -> dict:
        """Budget → re-price listings → roll prices up to products.
        Returns flat metrics (repriced, ended, reprice_* …)."""
        cfg = self._cfg or {}
        rp = _reprice_config(cfg)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        used = _read_api_usage_today(self.storage, ebay.client_id, day)
        live = ebay.browse_rate_limit()  # None → ledger-only fallback
        budget = _ebay_call_budget(cfg, used_today=used,
                                   planned_search_calls=planned_search_calls,
                                   live=live)
        cap = budget["reprice_cap"]
        if limit_override is not None:
            cap = min(cap, max(0, int(limit_override)))
        log.info("reprice: budget %s (eBay live: %s) → %d calls this run",
                 budget, live, cap)
        out = {"reprice_cap": cap, "ebay_calls_used_today": budget["used_today"]}
        if live:
            out["ebay_quota_remaining"] = live["remaining"]
        try:
            r = _reprice_listings(
                adapter, listings_table, ebay, fk_col=fk_col, max_calls=cap,
                min_age_hours=int(rp["min_age_hours"]),
                revive_window_days=int(rp["revive_window_days"]),
                max_not_found_ratio=float(rp["max_not_found_ratio"]),
                dry_run=dry_run)
        except Exception as e:
            log.warning("reprice phase failed (non-fatal): %s", e)
            if adapter.kind == "postgres":
                try: adapter.conn.rollback()
                except Exception: pass
            return out
        log.info("reprice: checked=%d repriced=%d revived=%d price_changed=%d "
                 "ended=%d unavailable=%d errors=%d calls=%d%s",
                 r["checked"], r["repriced"], r["revived"], r["price_changed"],
                 r["ended"], r["unavailable"], r["errors"], r["api_calls"],
                 " (rate-limited)" if r["rate_limited"] else "")
        out.update({
            "repriced": r["repriced"],
            # ended = no longer buyable (404 + out of stock), both marked inactive
            "ended": r["ended"] + r["unavailable"],
            "reprice_checked": r["checked"],
            "reprice_revived": r["revived"],
            "reprice_price_changed": r["price_changed"],
            "reprice_not_found": r["ended"],
            "reprice_out_of_stock": r["unavailable"],
            "reprice_errors": r["errors"],
            "reprice_rate_limited": r["rate_limited"],
            "reprice_not_found_suspect": bool(r.get("not_found_suspect")),
            "reprice_api_calls": r["api_calls"],
            "reprice_samples": r["samples"],
        })
        if not dry_run and r["product_ids"] and rp.get("update_product_prices"):
            roll = _refresh_product_prices(
                adapter, products_table, listings_table, fk_col, r["product_ids"],
                key_column=rp["product_key_column"],
                key_prefix=rp["product_key_prefix"],
                clear_when_unlisted=bool(rp["clear_price_when_unlisted"]))
            out["products_repriced"] = roll["priced"]
            out["products_price_cleared"] = roll["cleared"]
        return out

    def _record_ebay_usage(self, ebay: EbayClient) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _record_api_usage(self.storage, ebay.client_id, day, self.agent_id,
                          ebay.calls)
        ebay.calls = {}  # recorded — never double-count on a second call

    def _reprice_only(self, adapter: DbAdapter, products_table: str,
                      listings_table: str, site_id: str, ebay: EbayClient,
                      storage, dry_run: bool,
                      limit: Optional[int]) -> RunResult:
        """`--reprice-only`: skip discovery (and the mapping flow); just
        run the re-price pass. Used for manual catch-up and dry-run checks."""
        mapping = storage.read_json(mapping_storage_key(self.agent_id, site_id)) or {}
        lt = mapping.get("listings_table") or {}
        fk_col = lt.get("fk_to_product_column") or "product_id"
        stats = self._reprice_phase(
            adapter, products_table, lt.get("name") or listings_table, fk_col,
            ebay, planned_search_calls=0, dry_run=dry_run,
            limit_override=limit)
        stats["api_calls"] = ebay.total_calls
        self._record_ebay_usage(ebay)
        return RunResult(
            status="success",
            summary=(f"reprice-only{' (dry-run)' if dry_run else ''}: "
                     f"checked={stats.get('reprice_checked', 0)} "
                     f"repriced={stats.get('repriced', 0)} "
                     f"revived={stats.get('reprice_revived', 0)} "
                     f"ended={stats.get('ended', 0)} "
                     f"api_calls={stats['api_calls']}"),
            metrics=stats,
        )

    # ───────────────────────────────────────────────────────────────
    def _drain_responses_queue(self, storage) -> list[dict]:
        """Pull any pending email replies dropped by the responder-agent."""
        prefix = f"agents/{self.agent_id}/responses-queue/"
        out = []
        for key in storage.list_prefix(prefix):
            d = storage.read_json(key)
            if d:
                out.append(d)
                # Mark consumed by deleting (or moving). We delete here.
                try: storage.delete(key)
                except Exception: pass
        if out:
            log.info("drained %d email replies from responses-queue", len(out))
        return out

    def _resolve_reply(self, reply_text: str) -> dict:
        """Parse the operator's email reply into a structured action.
        Recognized commands (case-insensitive on the keyword):
          APPROVE
          CREATE NEW
          USE TABLES products=<name> listings=<name>
          EDIT <json>
          REJECT <reason>
        Anything else returns action='unknown'.
        """
        s = (reply_text or "").strip()
        if not s:
            return {"action": "unknown", "raw": ""}
        first_line = s.splitlines()[0].strip()
        upper = first_line.upper()

        if upper.startswith("APPROVE"):
            return {"action": "approve"}
        if upper.startswith("CREATE NEW"):
            return {"action": "create-new-tables"}
        if upper.startswith("USE TABLES"):
            # Parse "USE TABLES products=X listings=Y"
            tail = first_line[len("USE TABLES"):].strip()
            kv = {}
            for tok in tail.split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    kv[k.strip().lower()] = v.strip()
            return {"action": "use-tables", "products": kv.get("products"),
                    "listings": kv.get("listings")}
        if upper.startswith("EDIT"):
            tail = s[4:].strip()
            try: return {"action": "edit", "mapping": json.loads(tail)}
            except json.JSONDecodeError as e:
                return {"action": "edit-bad-json", "error": str(e), "raw": tail[:300]}
        if upper.startswith("REJECT"):
            return {"action": "reject", "reason": s[6:].strip()[:300]}
        return {"action": "unknown", "raw": s[:300]}

    def _propose_or_resume_mapping(
        self, adapter: DbAdapter, products_table: str, listings_table: str,
        proposed_mode: str, site_id: str, site_constants: dict,
        ebay: EbayClient, ebay_filter: str, owner_email: Optional[str],
    ) -> dict:
        """Returns the approved v2 mapping. Raises ConfirmationPending if a
        proposal is out + unanswered, or if a fresh one was just emitted."""
        storage = get_storage()
        mapping_key = mapping_storage_key(self.agent_id, site_id)
        existing = storage.read_json(mapping_key) or {}

        # Drain any inbound replies the responder-agent has parked.
        replies = self._drain_responses_queue(storage)

        # If we have a pending proposal, look at its confirmation + replies.
        pending_cid = existing.get("pending_confirmation_id")
        if pending_cid:
            cr = get_confirmation(self.agent_id, pending_cid, storage)
            # Apply any new email replies. The responder pre-filters by
            # subject route, so anything in our queue is meant for us. We
            # don't strictly match on request_id because the responder
            # generates its own request_id from run_ts, not from our
            # original confirmation request_id — instead, if we have any
            # pending confirmation and any reply, we apply it.
            for r in replies:
                cmd = self._resolve_reply(
                    r.get("reply_text") or r.get("notes") or r.get("action") or ""
                )
                log.info("reply command: %s", cmd)
                cr = self._apply_reply_to_confirmation(
                    cr, cmd, storage, mapping_key,
                    adapter, products_table, listings_table,
                    site_id, site_constants, ebay, ebay_filter, owner_email,
                )

            if cr is None:
                log.warning("pending confirmation %s lost — re-proposing", pending_cid)
            elif cr.state == "approved":
                approved = dict(storage.read_json(mapping_key) or {})
                approved["approved_at"] = cr.resolved_at or _now()
                approved["approved_by"] = cr.approved_by or "unknown"
                approved.pop("pending_confirmation_id", None)
                storage.write_json(mapping_key, approved)
                log.info("v2 mapping approved for site=%s", site_id)
                return approved
            elif cr.state == "rejected":
                log.warning("rejected — clearing and re-proposing (notes=%s)",
                            (cr.notes or "")[:200])
                storage.write_json(mapping_key, {})
            elif cr.state == "pending":
                raise ConfirmationPending(pending_cid, "mapping awaiting approval")

        # No approved/pending mapping → propose a fresh one.
        proposal = self._build_proposal(
            adapter, products_table, listings_table, proposed_mode,
            site_id, site_constants, ebay, ebay_filter,
        )
        return self._send_proposal_and_park(
            proposal, site_id, owner_email,
            existing_products_table=products_table,
            existing_listings_table=listings_table,
            existing_listings_exists=self._table_exists(adapter, listings_table),
        )

    def _table_exists(self, adapter: DbAdapter, table: str) -> bool:
        try:
            cols = adapter.introspect_table(table)
            return bool(cols)
        except Exception:
            return False

    def _build_proposal(self, adapter: DbAdapter, products_table: str,
                        listings_table: str, proposed_mode: str, site_id: str,
                        site_constants: dict, ebay: EbayClient, ebay_filter: str,
                        ) -> dict:
        """Run schema introspection + Claude → return v2 proposal dict
        (with pending_confirmation_id NOT yet set)."""
        log.info("introspecting products table %s", products_table)
        prod_cols = adapter.introspect_table(products_table)
        prod_samples = []
        try:
            prod_samples = adapter.sample_rows(products_table, n=3) if prod_cols else []
        except Exception as e:
            log.warning("products sample_rows failed: %s", e)

        log.info("introspecting listings table %s (may not exist)", listings_table)
        list_cols: list[ColumnInfo] = []
        list_samples: list[dict] = []
        try:
            list_cols = adapter.introspect_table(listings_table)
            if list_cols:
                list_samples = adapter.sample_rows(listings_table, n=3)
        except Exception as e:
            log.info("listings table not found: %s", e)

        # Auto-pick mode if not specified or if existing-state contradicts:
        mode = proposed_mode
        if not prod_cols and not list_cols:
            mode = "create-new-tables"
        elif prod_cols and not list_cols:
            mode = "use-existing-products-new-listings"
        elif prod_cols and list_cols:
            mode = "use-existing-products-and-listings"
        log.info("auto-picked mode: %s", mode)

        # Pull category slugs from operator's catalog (if `categories` table exists)
        cats = self._fetch_category_slugs(adapter)

        log.info("fetching eBay sample item")
        sample_items = ebay.search(q="Voodoo 3", filter_str=ebay_filter, limit=1)
        if not sample_items:
            sample_items = ebay.search(q="raspberry pi", limit=1)
        if not sample_items:
            raise RuntimeError("eBay search returned no items.")
        sample_item = sample_items[0]

        log.info("calling LLM (with framework fallback chain) to propose v2 mapping")
        site_id_value = site_constants.get("site_id")
        proposal = propose_mapping_via_claude(
            self.ai_chat,
            products_columns=prod_cols, products_samples=prod_samples,
            listings_columns=list_cols, listings_samples=list_samples,
            sample_ebay_item=sample_item,
            products_table_name=products_table,
            listings_table_name=listings_table,
            mode=mode,
            site_id_value=site_id_value,
            db_kind=adapter.kind,
            category_slugs=cats,
        )
        proposal.setdefault("schema_version", "2")
        proposal["site_id"] = site_id
        proposal.setdefault("mode", mode)
        return proposal

    def _fetch_category_slugs(self, adapter: DbAdapter) -> list[str]:
        try:
            if adapter.kind == "postgres":
                cur = adapter.conn.cursor()
                cur.execute("SELECT slug FROM categories WHERE is_active = true ORDER BY slug")
                rows = [r[0] for r in cur.fetchall()]
                cur.close()
                return rows
        except Exception:
            return []
        return []

    def _send_proposal_and_park(
        self, proposal: dict, site_id: str, owner_email: Optional[str],
        *, existing_products_table: str, existing_listings_table: str,
        existing_listings_exists: bool,
    ) -> dict:
        """Send proposal email, write a pending ConfirmationRecord, persist
        the proposal in storage with pending_confirmation_id, and raise
        ConfirmationPending."""
        storage = get_storage()
        mapping_key = mapping_storage_key(self.agent_id, site_id)
        confirmation_id = f"map-{site_id}-{int(time.time())}"
        request_id = new_request_id()

        subject, html = _render_mapping_html(
            proposal,
            confirmation_id=confirmation_id,
            request_id=request_id,
            agent_id=self.agent_id,
            site_id=site_id,
            existing_products_table=existing_products_table,
            existing_listings_table=existing_listings_table,
            existing_listings_exists=existing_listings_exists,
        )

        recipients = [owner_email] if owner_email else []
        ok, detail = self._send_email(
            subject=subject, body_html=html, to=recipients,
            request_id=request_id,
        )
        log.info("proposal email sent: %s — %s", ok, detail)

        cr = ConfirmationRecord(
            confirmation_id=confirmation_id,
            agent_id=self.agent_id,
            method_name="ingest_to_destination",
            reason=f"Approve eBay→{site_id} v2 mapping",
            state="pending",
            requested_at=_now(),
            request_id=request_id,
        )
        write_confirmation(cr, storage)

        proposal_with_pending = dict(proposal)
        proposal_with_pending["pending_confirmation_id"] = confirmation_id
        storage.write_json(mapping_key, proposal_with_pending)

        raise ConfirmationPending(
            confirmation_id,
            "v2 mapping proposal emailed; waiting for operator approval",
        )

    def _apply_reply_to_confirmation(
        self, cr: ConfirmationRecord, cmd: dict, storage,
        mapping_key: str,
        adapter: DbAdapter, products_table: str, listings_table: str,
        site_id: str, site_constants: dict,
        ebay: EbayClient, ebay_filter: str, owner_email: Optional[str],
    ) -> Optional[ConfirmationRecord]:
        """Mutate the confirmation + stored proposal based on the operator's
        reply. Returns the updated record (still 'pending' if more action
        is needed; 'approved' or 'rejected' otherwise)."""
        action = cmd.get("action")
        existing = storage.read_json(mapping_key) or {}

        if action == "approve":
            cr.state = "approved"
            cr.resolved_at = _now()
            cr.approved_by = cr.approved_by or "email-reply"
            cr.notes = (cr.notes or "") + "\nAPPROVED via email reply"
            write_confirmation(cr, storage)

        elif action == "edit":
            edited = cmd.get("mapping") or {}
            edited.setdefault("schema_version", "2")
            edited.setdefault("site_id", site_id)
            edited["approved_at"] = _now()
            edited["approved_by"] = "email-edit"
            storage.write_json(mapping_key, edited)
            cr.state = "approved"
            cr.resolved_at = _now()
            cr.approved_by = "email-edit"
            cr.notes = (cr.notes or "") + "\nEDITED + APPROVED via email reply"
            write_confirmation(cr, storage)

        elif action == "create-new-tables":
            log.info("operator chose CREATE NEW TABLES — regenerating proposal")
            proposal = self._build_proposal(
                adapter, products_table, listings_table,
                "create-new-tables", site_id, site_constants, ebay, ebay_filter,
            )
            return self._send_proposal_and_park(
                proposal, site_id, owner_email,
                existing_products_table=products_table,
                existing_listings_table=listings_table,
                existing_listings_exists=self._table_exists(adapter, listings_table),
            )

        elif action == "use-tables":
            new_p = cmd.get("products") or products_table
            new_l = cmd.get("listings") or listings_table
            log.info("operator re-targeted tables: products=%s listings=%s", new_p, new_l)
            proposal = self._build_proposal(
                adapter, new_p, new_l, "use-existing-products-new-listings",
                site_id, site_constants, ebay, ebay_filter,
            )
            return self._send_proposal_and_park(
                proposal, site_id, owner_email,
                existing_products_table=new_p,
                existing_listings_table=new_l,
                existing_listings_exists=self._table_exists(adapter, new_l),
            )

        elif action == "reject":
            cr.state = "rejected"
            cr.resolved_at = _now()
            cr.notes = (cr.notes or "") + "\nREJECTED: " + (cmd.get("reason") or "")
            write_confirmation(cr, storage)

        else:
            log.warning("unrecognized reply action %s — leaving pending", action)

        return cr

    # ───────────────────────────────────────────────────────────────
    def _hydrate_from_ebay_fields(self, item: dict, cat_hint: Optional[str]) -> Optional[dict]:
        """Build a canonical-product record DIRECTLY from the eBay Browse
        API's structured fields, skipping Claude entirely.

        eBay returns brand + mpn + title + categories on every Browse search
        result. When brand AND (mpn OR a recognizable model in the title) are
        present, we can synthesize a canonical record without an LLM call.
        Returns None when fields are insufficient — caller falls back to
        Claude hydration."""
        title = (item.get("title") or "").strip()
        brand = (item.get("brand") or "").strip()
        mpn = (item.get("mpn") or "").strip()
        if not title:
            return None
        # We need at least a brand + (mpn or distinct title) to skip claude
        if not brand:
            return None
        # Build name: prefer "Brand Title" format (dedupes seller noise via fingerprint)
        title_lower = title.lower()
        # If brand isn't already in the title, prepend it
        name = title if brand.lower() in title_lower else f"{brand} {title}"
        name = name[:140]  # cap
        # Slug-fingerprint: lowercase, strip non-alnum, drop common seller-noise tokens
        import re as _re
        sw = {"new", "used", "tested", "working", "vintage", "pulled", "open",
              "box", "lot", "of", "for", "parts", "oem", "the", "and", "with",
              "from", "in", "fast", "ship", "free", "shipping", "rare", "free"}
        words = [w for w in _re.findall(r'[a-z0-9]+', name.lower()) if w not in sw]
        title_fingerprint = "-".join(words)[:120]
        if not title_fingerprint:
            return None
        return {
            "name": name,
            "manufacturer": brand,
            "family": None,
            "model_number": mpn or None,
            "release_year": None,
            "form_factor": None,
            "key_specs": {},
            "category_slug": cat_hint or "",
            "title_fingerprint": title_fingerprint,
            # Lower confidence than Claude when MPN is missing — flags
            # that we'd want claude validation for these later if reviews
            # prove inaccurate. With brand+mpn+title we trust 0.85.
            "confidence": 0.85 if mpn else 0.65,
            "_source": "ebay-fields",
        }

    def _hydrate_canonical_products(self, items: list[dict], cat_hint: Optional[str]) -> list[dict]:
        """Batch-canonicalize listings → canonical product fingerprints.
        Two-pass strategy:

        1. **Field-extraction pass (free, fast)**: try to build the canonical
           record from eBay's structured fields (brand, mpn, title). Works
           cleanly for branded items with MPNs — most modern hardware listings.

        2. **Claude pass (expensive)**: ONLY for items that field extraction
           couldn't handle (no brand, vague titles, multi-item lots, retro
           hardware where MPNs are absent). Same prompt as before.

        Returns parallel list — entries are dicts on success (with `_source`
        identifying which path produced them), None to skip."""
        if not items:
            return []

        # Pass 1: field extraction
        out: list[Optional[dict]] = []
        claude_indexes: list[int] = []
        claude_items: list[dict] = []
        for idx, item in enumerate(items):
            rec = self._hydrate_from_ebay_fields(item, cat_hint)
            if rec is not None:
                out.append(rec)
            else:
                out.append(None)  # placeholder; filled by claude pass
                claude_indexes.append(idx)
                claude_items.append(item)
        if claude_items:
            log.info("hydration: %d/%d items handled by ebay-fields; "
                     "%d still need claude", len(items) - len(claude_items),
                     len(items), len(claude_items))
        else:
            log.info("hydration: all %d items handled by ebay-fields (no claude)",
                     len(items))
            return out

        # Pass 2: LLM — only for items field-extraction couldn't handle.
        # Uses self.ai_chat so the framework's fallback chain
        # (claude-cli → copilot → azure_openai → openai) auto-recovers
        # when claude-cli is rate-limited. Was: ai_client_for + ai.chat,
        # which had no fallback and would hang for hours when the
        # claude-pool returned all-rate-limited.
        BATCH = 8
        claude_results: list[Optional[dict]] = []
        for i in range(0, len(claude_items), BATCH):
            chunk = claude_items[i:i + BATCH]
            user_prompt = "Canonicalize each listing. Return a JSON array of objects (one per listing, in order):\n\n"
            for j, it in enumerate(chunk):
                hint = f" (category hint: {cat_hint})" if cat_hint else ""
                user_prompt += (
                    f"#{j}{hint}\n  title: {it.get('title','')[:240]}\n"
                    f"  short: {(it.get('shortDescription') or '')[:160]}\n\n"
                )
            try:
                raw = self.ai_chat(
                    [{"role": "system", "content": HYDRATION_SYS},
                     {"role": "user", "content": user_prompt}],
                    max_tokens=4000,
                    # claude-cli-only kwargs — OpenAI-shape clients ignore them:
                    max_turns=2, timeout=300,
                )
                # Robust JSON parse — see framework.core.llm_json. Old
                # find('[')/rfind(']') broke when the model emitted
                # trailing prose or footnote refs (`[1]`, `[CC]`),
                # losing whole batches with "Extra data" errors.
                try:
                    arr = extract_json_array(raw)
                except ValueError as e:
                    log.warning("hydration: parse failed (%s), skipping batch", e)
                    claude_results.extend([None] * len(chunk))
                    continue
                if len(arr) != len(chunk):
                    log.warning("hydration: array length mismatch (%d vs %d)",
                                len(arr), len(chunk))
                    arr = arr + [None] * max(0, len(chunk) - len(arr))
                claude_results.extend(arr)
            except Exception as e:
                log.warning("hydration batch failed: %s", e)
                claude_results.extend([None] * len(chunk))
        # Tag claude-pass results so downstream stats can split by source
        for r in claude_results:
            if isinstance(r, dict):
                r.setdefault("_source", "claude")
        # Splice claude results back into `out` at the original indexes.
        # Only dicts count: the model sometimes puts a bare string (a note,
        # "skip") in an array slot, and a str reaching _ingest_v2's
        # hyd.get() crashed the whole run (2026-09-26).
        for k, idx in enumerate(claude_indexes):
            if k < len(claude_results):
                r = claude_results[k]
                out[idx] = r if isinstance(r, dict) else None
        return out

    def _upsert_canonical_product(
        self, adapter: DbAdapter, mapping: dict, item: dict, hyd: dict,
        cat_id: Optional[int], site_constants: dict,
    ) -> Optional[Any]:
        """Insert/update one canonical product row, return its primary
        key value so we can FK from the listing."""
        pt = mapping["products_table"]
        match_cols = pt.get("match_columns") or ["title_fingerprint"]
        # Build the row from the products field-mapping.
        row = self._build_row_from_section(pt, item, hyd, site_constants, cat_id)
        # Look up by match columns first (so multiple listings of same
        # SKU dedup to one canonical product).
        existing_id = self._lookup_existing_product(adapter, pt["name"], match_cols, row)
        if existing_id is not None:
            return existing_id
        # Insert via upsert on key_columns.
        key_cols = pt.get("key_columns") or ["asin"]
        # If destination has an `id` column we want returned, capture it.
        try:
            ir = adapter.upsert_rows(pt["name"], [row], key_cols)
            # upsert_rows catches per-row exceptions internally and bumps
            # result.failed; the caller MUST check this. Without the
            # check, an FK violation (or similar) silently increments
            # `failed`, the SELECT below finds nothing, and we report the
            # opaque "no PK returned" error — which is what bit us with
            # the products_site_id_fkey UUID-vs-slug mismatch.
            if ir.failed and ir.failed_samples:
                samp = ir.failed_samples[0]
                err_text = samp.get("err", "(unknown)")[:300]
                log.warning("product upsert failed [item=%s, asin=%s, site_id=%s]: %s",
                            row.get("title", "")[:60], row.get("asin"),
                            row.get("site_id"), err_text)
                self._last_upsert_error = err_text
                if adapter.kind == "postgres":
                    try: adapter.conn.rollback()
                    except Exception: pass
                return None
            if adapter.kind == "postgres":
                cur = adapter.conn.cursor()
                where_clauses = " AND ".join(f"{k} = %s" for k in key_cols)
                cur.execute(f"SELECT id FROM {pt['name']} WHERE {where_clauses} LIMIT 1",
                            [row[k] for k in key_cols])
                r = cur.fetchone(); cur.close()
                if r:
                    return r[0]
                # Insert claimed success but SELECT can't find it — log
                # what we actually upserted so the cause is visible.
                self._last_upsert_error = (
                    f"upsert_rows reported inserted={ir.inserted} updated={ir.updated} "
                    f"failed={ir.failed} but SELECT WHERE "
                    f"{', '.join(k+'=%s' % repr(row.get(k)) for k in key_cols)} "
                    f"returned no row — likely a trigger or RLS hiding the row, "
                    f"or column-type mismatch (e.g. site_id slug vs UUID)"
                )
                log.warning("product upsert: %s", self._last_upsert_error[:300])
                return None
        except Exception as e:
            # Surface the actual error class + message so stats["errors"]
            # has actionable detail instead of every row reading "product
            # upsert failed". The 4-30 outage was 80+ FK-violation errors
            # all logged as the same opaque string.
            err_text = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("product upsert failed [item=%s, asin=%s, site_id=%s]: %s",
                        row.get("title", "")[:60], row.get("asin"), row.get("site_id"), err_text)
            # Stash on the row so the caller can include it in stats["errors"]
            self._last_upsert_error = err_text
            # Roll back so the next upsert in the loop doesn't fail with
            # "current transaction is aborted, commands ignored until end
            # of transaction block." Each rec gets a fresh transaction.
            if adapter.kind == "postgres":
                try:
                    adapter.conn.rollback()
                except Exception:
                    pass
            return None
        return None

    def _build_row_from_section(self, section: dict, item: dict,
                                 hydration: Optional[dict],
                                 site_constants: dict,
                                 cat_id: Optional[int] = None) -> dict:
        """Build a destination row from a section's fields + constants."""
        row: dict = {}
        for f in section.get("fields", []):
            col = f["destination_column"]
            if f.get("from_hydration"):
                if hydration:
                    val = hydration.get(f["from_hydration"])
                    if isinstance(val, (dict, list)):
                        val = json.dumps(val)
                    row[col] = val if val is not None else f.get("default")
                else:
                    row[col] = f.get("default")
            else:
                row[col] = self._extract_field(item, f)
        # Constants.
        for c in section.get("constants", []):
            row[c["destination_column"]] = c["value"]
        # Site-level constants override.
        row.update(site_constants or {})
        if cat_id is not None and "category_id" in row and row["category_id"] is None:
            row["category_id"] = cat_id
        return row

    def _extract_field(self, item: dict, f: dict) -> Any:
        from mapping import _resolve_path, TRANSFORMS  # type: ignore
        val = _resolve_path(item, f.get("source_path") or "")
        tform = f.get("transform")
        if tform and tform in TRANSFORMS:
            try: return TRANSFORMS[tform](val, item)
            except Exception: return f.get("default")
        return val if val is not None else f.get("default")

    def _row_asin_for_product(self, mapping: dict, item: dict,
                               hyd: Optional[dict]) -> str:
        """Compute the synthetic ASIN that lands in products.asin for this
        item, so the completion email can build the catalog URL
        (/product/<asin>) where the eBay listings render."""
        pt = mapping.get("products_table") or {}
        for f in pt.get("fields", []):
            if f.get("destination_column") == "asin":
                try:
                    return str(self._extract_field(item, f) or "")
                except Exception:
                    return ""
        return ""

    def _lookup_existing_product(self, adapter: DbAdapter, table: str,
                                  match_cols: list[str], row: dict) -> Optional[Any]:
        """Find existing canonical product by match columns (e.g.
        title_fingerprint). Returns id or None."""
        present = [c for c in match_cols if row.get(c) not in (None, "")]
        if not present:
            return None
        if hasattr(adapter, "ensure_open"):
            try: adapter.ensure_open()
            except Exception: pass
        try:
            if adapter.kind == "postgres":
                cur = adapter.conn.cursor()
                where = " AND ".join(f"{c} = %s" for c in present)
                cur.execute(f"SELECT id FROM {table} WHERE {where} LIMIT 1",
                            [row[c] for c in present])
                r = cur.fetchone(); cur.close()
                return r[0] if r else None
        except Exception as e:
            log.debug("product lookup failed: %s", e)
        return None

    # ───────────────────────────────────────────────────────────────
    def _ingest_v2(
        self, adapter: DbAdapter, mapping: dict, site_id: str,
        site_constants: dict, ebay: EbayClient,
        ebay_filter: str, seeds: list, per_query_limit: int,
        max_queries_per_run: int, stale_hours: int, dry_run: bool,
    ) -> RunResult:
        """Two-table ingestion: hydrate canonical products from each
        listing batch, upsert into products table, then write listings
        rows referencing the product's primary key."""
        stats = {"queries_run": 0, "items_seen": 0,
                 "products_upserted": 0, "products_skipped_low_conf": 0,
                 "listings_inserted": 0, "listings_updated": 0,
                 "by_category": {},
                 "product_samples": [],   # up to 30 — for the completion email
                 "listing_samples": [],   # up to 30 — for the completion email
                 "errors": []}
        # Per-category running totals for the email's breakdown table.
        def _bump_cat(cat_slug: str | None, *, products: int = 0,
                      listings_in: int = 0, listings_up: int = 0):
            if not cat_slug: return
            bk = stats["by_category"].setdefault(
                cat_slug, {"products": 0, "listings_inserted": 0,
                           "listings_updated": 0})
            bk["products"] += products
            bk["listings_inserted"] += listings_in
            bk["listings_updated"] += listings_up
        pt = mapping["products_table"]
        lt = mapping["listings_table"]
        listings_keys = lt.get("key_columns") or ["ebay_item_id"]
        fk_col = lt.get("fk_to_product_column") or "product_id"

        # ── Re-price pass — refresh existing listings (price + liveness)
        # oldest-first via Browse get_item_by_legacy_id, revive listings the
        # time reaper deactivated while they were still live, and mark
        # ended/out-of-stock ones inactive (never deleted). Runs BEFORE the
        # coverage probe so products whose last listing just ended get
        # backfilled in this same run. Capped by the eBay daily call budget
        # (see _ebay_call_budget). Dry-run still looks items up (read-only
        # API calls) but writes nothing.
        stats.update(self._reprice_phase(
            adapter, pt["name"], lt["name"], fk_col, ebay,
            planned_search_calls=max_queries_per_run, dry_run=dry_run))

        search_product_ids: set = set()
        plan: list[tuple[Optional[str], str]] = []
        # Build coverage queue + seed queue separately, then INTERLEAVE
        # them so seeds (the curated retro-PC / retro-console keyword
        # rotation in site.yaml) always get airtime even when 70k+
        # products lack listings. Without this, the coverage queue
        # eats every slot and entire categories (retro-cpus, retro-gpus,
        # retro-motherboards, retro-sound, etc.) never get crawled.
        coverage_targets = _products_lacking_listings(
            adapter, pt["name"], lt["name"], fk_col, limit=40,
        )
        coverage_queue: list[tuple[Optional[str], str]] = [
            (prod.get("category_slug"), prod["query"])
            for prod in coverage_targets
        ]
        seed_queue: list[tuple[Optional[str], str]] = []
        priority_categories = set((self._cfg or {}).get("priority_categories") or [])
        priority_seed_queue: list[tuple[Optional[str], str]] = []
        for entry in seeds:
            cat = entry.get("category") or entry.get("category_slug")
            for q in (entry.get("queries") or []):
                if cat and cat in priority_categories:
                    priority_seed_queue.append((cat, q))
                else:
                    seed_queue.append((cat, q))

        # ── Inbound handoff bypass ──────────────────────────────────────
        # When article-author publishes an article that mentions hardware
        # products, it sends a handoff with `work_type=ebay_fetch_for_product`
        # and `rec.product_query` / `rec.product_category` populated.
        # Inject those queries into priority_seed_queue ahead of every-
        # thing else so the next eBay tick fetches listings for the just-
        # published article's mentions instead of waiting for the seed
        # rotation to come around.
        handoff_count = 0
        for h in (self.inbound_handoffs or []):
            if h.get("work_type") not in (
                "ebay_fetch_for_product",
                "ebay_on_demand_fetch",
            ):
                continue
            rec = h.get("rec") or {}
            q = rec.get("product_query") or rec.get("query")
            cat = rec.get("product_category") or rec.get("category_slug")
            if not q:
                continue
            priority_seed_queue.insert(0, (cat, q))
            handoff_count += 1
        if handoff_count:
            self.decide(
                "observation",
                f"injected {handoff_count} handoff-driven query/queries "
                f"into priority seed queue (from article-author publish)")

        coverage_count = len(coverage_targets)
        stats["coverage_targets"] = coverage_count
        stats["seed_pool_size"] = len(seed_queue) + len(priority_seed_queue)
        stats["priority_seed_pool"] = len(priority_seed_queue)

        # Reservation policy:
        #   • priority_seeds_pct of the budget is reserved for seeds in
        #     `cfg.priority_categories` (e.g., retro-cpus, retro-gpus
        #     when SpecPicks is rebuilding retro-PC coverage).
        #   • seed_reservation_pct of the remaining budget goes to other
        #     seeds.
        #   • Coverage gets whatever's left.
        priority_pct = float((self._cfg or {}).get("priority_seeds_pct", 0.0))
        seed_pct = float((self._cfg or {}).get("seed_reservation_pct", 0.6))
        priority_pct = max(0.0, min(1.0, priority_pct))
        seed_pct = max(0.0, min(1.0, seed_pct))

        priority_quota = (int(max_queries_per_run * priority_pct)
                          if priority_seed_queue else 0)
        non_priority_budget = max_queries_per_run - priority_quota
        seed_quota = (int(non_priority_budget * seed_pct)
                      if seed_queue else 0)
        coverage_quota = non_priority_budget - seed_quota

        # Stable rotation across runs so each tick advances the seed offset
        # and the full pool gets covered over ~10-15 ticks.
        from datetime import datetime as _rotdt
        rot_seed = _rotdt.utcnow().timetuple().tm_yday * 7

        def _rotate(pool: list, n: int) -> list:
            if not pool or n <= 0:
                return []
            offset = rot_seed % len(pool)
            rotated = pool[offset:] + pool[:offset]
            return rotated[:n]

        priority_pick = _rotate(priority_seed_queue, priority_quota)
        seed_pick = _rotate(seed_queue, seed_quota)
        coverage_pick = coverage_queue[:coverage_quota]

        # Interleave: priority-seed, coverage, regular-seed, coverage, ...
        # so the priority categories get crawled even if every other
        # query fails.
        plan = []
        ps_iter = iter(priority_pick)
        s_iter = iter(seed_pick)
        c_iter = iter(coverage_pick)
        while len(plan) < max_queries_per_run:
            advanced = False
            for it in (ps_iter, c_iter, s_iter):
                try:
                    plan.append(next(it))
                    advanced = True
                    if len(plan) >= max_queries_per_run:
                        break
                except StopIteration:
                    continue
            if not advanced:
                break
        plan = plan[:max_queries_per_run]
        stats["seeds_in_plan"] = (len(priority_pick) + len(seed_pick)
                                   if plan else 0)
        stats["coverage_in_plan"] = len(coverage_pick)
        if not plan:
            stats["api_calls"] = ebay.total_calls
            self._record_ebay_usage(ebay)
            return RunResult(status="success", summary="no seeds configured",
                             metrics={k: v for k, v in stats.items()
                                      if isinstance(v, (int, float, bool))})

        for cat, q in plan:
            stats["queries_run"] += 1
            try:
                items = ebay.search(q=q, filter_str=ebay_filter, limit=per_query_limit)
            except Exception as e:
                stats["errors"].append({"q": q, "err": str(e)[:300]})
                continue
            stats["items_seen"] += len(items)
            log.info("[%s] %r → %d items", cat or "?", q, len(items))
            if not items: continue

            cat_id = self._resolve_category_id(adapter, cat) if cat else None

            # Phase A: canonicalize the whole batch in one or two LLM calls.
            # This can take 30s-5min depending on Claude pool state, easily
            # outliving Azure Postgres' 5min idle timeout. We pre-emptively
            # ensure the conn is alive AFTER hydration before the upsert
            # phase so a closed conn doesn't cascade into "product upsert
            # failed: connection already closed" for every row.
            hyd_list = self._hydrate_canonical_products(items, cat)

            if not dry_run and hasattr(adapter, "ensure_open"):
                try:
                    adapter.ensure_open()
                except Exception as e:
                    log.warning("DB conn refresh failed before upsert phase: %s", e)

            listings_rows = []
            for item, hyd in zip(items, hyd_list):
                if not hyd or float(hyd.get("confidence") or 0) < 0.5:
                    stats["products_skipped_low_conf"] += 1
                    continue
                if dry_run:
                    continue
                self._last_upsert_error = ""
                product_pk = self._upsert_canonical_product(
                    adapter, mapping, item, hyd, cat_id, site_constants,
                )
                if product_pk is None:
                    err = self._last_upsert_error or "product upsert failed (no PK returned, no exception captured — likely SELECT after INSERT didn't find row)"
                    stats["errors"].append({
                        "item": item.get("legacyItemId"),
                        "title": (item.get("title") or "")[:80],
                        "err": err,
                    })
                    # Bucket distinct error classes for the email summary
                    err_class = err.split(":", 1)[0]
                    stats.setdefault("error_class_counts", {})[err_class] = (
                        stats.get("error_class_counts", {}).get(err_class, 0) + 1
                    )
                    continue
                stats["products_upserted"] += 1
                _bump_cat(cat, products=1)
                if len(stats["product_samples"]) < 30:
                    # Compute the public catalog URL where the eBay listings
                    # for this product render. SpecPicks routes /products/<asin>
                    # → 301 to /product/<asin> (the React shell mounts the
                    # ProductDetailPage which calls /api/products/<asin>/ebay-listings).
                    asin_for_url = self._row_asin_for_product(mapping, item, hyd)
                    # URL shape comes from site.yaml `catalog_url_template`
                    # (e.g. "https://<site>/product/{asin}") — no site
                    # literal in the engine.
                    url_tpl = (self._cfg or {}).get("catalog_url_template") or ""
                    catalog_url = (
                        url_tpl.replace("{asin}", asin_for_url)
                        if (asin_for_url and url_tpl) else ""
                    )
                    stats["product_samples"].append({
                        "name": hyd.get("name") or "",
                        "manufacturer": hyd.get("manufacturer") or "",
                        "family": hyd.get("family") or "",
                        "category": cat or hyd.get("category_slug") or "",
                        "product_pk": product_pk,
                        "asin": asin_for_url,
                        "catalog_url": catalog_url,
                        "confidence": hyd.get("confidence"),
                    })

                # Build the listings row, with fk to the product.
                lrow = self._build_row_from_section(lt, item, hyd, site_constants={})
                lrow[fk_col] = product_pk
                # A search sighting proves the listing is live: bump
                # updated_at so the stale reaper (updated_at < stale_hours)
                # and the re-price min-age both see it. upsert_rows only
                # SETs mapped columns and the table has no touch trigger, so
                # without this a re-sighted listing kept its insert time and
                # was reaped again in the same run.
                lrow["updated_at"] = _now()  # ISO-8601 UTC, like item_end_date
                # Always track listing end-date so the site can hide expired
                # listings without waiting on the freshness reaper. Browse
                # API field is `itemEndDate` (ISO 8601); absent for
                # fixed-price listings without an explicit end. Falls back
                # to mapping value if the operator already wired it.
                end_iso = item.get("itemEndDate")
                if end_iso and "item_end_date" not in lrow:
                    lrow["item_end_date"] = end_iso
                listings_rows.append(lrow)
                search_product_ids.add(product_pk)
                if len(stats["listing_samples"]) < 30:
                    stats["listing_samples"].append({
                        "ebay_item_id": item.get("legacyItemId") or "",
                        "title": (item.get("title") or "")[:120],
                        "price": (item.get("price") or {}).get("value"),
                        "currency": (item.get("price") or {}).get("currency", "USD"),
                        "condition": item.get("condition"),
                        "url": item.get("itemAffiliateWebUrl") or item.get("itemWebUrl"),
                        "category": cat,
                        "product_pk": product_pk,
                    })

            if listings_rows:
                ir = adapter.upsert_rows(lt["name"], listings_rows, listings_keys)
                stats["listings_inserted"] += ir.inserted
                stats["listings_updated"] += ir.updated
                _bump_cat(cat, listings_in=ir.inserted, listings_up=ir.updated)
                if ir.failed_samples:
                    for s in ir.failed_samples[:2]:
                        log.warning("  listing upsert err: %s", s["err"])

            time.sleep(0.05)

        if not dry_run:
            _ensure_item_end_date_column(adapter, lt["name"])
            stale = _mark_stale_inactive(adapter, lt["name"], stale_hours)
            stats["stale_listings_inactive"] = stale
            # Listings just upserted from search carry fresh prices — roll
            # them up to their eBay-sourced product rows too (opt-in).
            rp = _reprice_config(self._cfg or {})
            if rp.get("update_product_prices"):
                roll = _refresh_product_prices(
                    adapter, pt["name"], lt["name"], fk_col, search_product_ids,
                    key_column=rp["product_key_column"],
                    key_prefix=rp["product_key_prefix"],
                    clear_when_unlisted=bool(rp["clear_price_when_unlisted"]))
                stats["products_repriced"] = (stats.get("products_repriced", 0)
                                              + roll["priced"])
                stats["products_price_cleared"] = (
                    stats.get("products_price_cleared", 0) + roll["cleared"])
        stats["api_calls"] = ebay.total_calls
        self._record_ebay_usage(ebay)

        log.info("v2 ingestion complete: %s", json.dumps(stats, default=str))

        # Send a completion-summary email to the operator with the actual
        # products + listings created. Skipped on dry-run.
        if not dry_run:
            try:
                self._send_completion_email(site_id, stats, mapping)
            except Exception as e:
                log.warning("completion email failed: %s", e)

        # Compute scalar success-rate metric for goal auto-track.
        # Stats dict carries lots of nested non-scalar things — RunResult.metrics
        # needs scalars to be useful, so we flatten the goal-relevant ones here.
        items_seen = int(stats.get("items_seen", 0))
        products_upserted = int(stats.get("products_upserted", 0))
        errors_count = len(stats.get("errors") or [])
        # Success rate = product upserts / items seen. ≥80% is healthy.
        sync_success_rate_pct = round(
            100.0 * products_upserted / max(items_seen, 1), 2
        )
        # Add the scalar keys directly to the stats dict so they show
        # up alongside existing counters in RunResult.metrics.
        stats["sync_success_rate_pct"] = sync_success_rate_pct
        stats["errors_count"] = float(errors_count)
        stats["products_upserted_count"] = float(products_upserted)
        stats["items_seen_count"] = float(items_seen)

        return RunResult(
            status="success",
            summary=(f"queries={stats['queries_run']} items={stats['items_seen']} "
                     f"repriced={stats.get('repriced', 0)} ended={stats.get('ended', 0)} "
                     f"api_calls={stats.get('api_calls', 0)} "
                     f"products={stats['products_upserted']} "
                     f"listings_in={stats['listings_inserted']} "
                     f"listings_up={stats['listings_updated']} "
                     f"success_rate={sync_success_rate_pct}%"),
            metrics=stats,
        )

    # ───────────────────────────────────────────────────────────────
    def _send_completion_email(self, site_id: str, stats: dict, mapping: dict) -> None:
        """After ingestion, email the operator a summary of what canonical
        products and listings were created/updated this run, with samples
        and per-category breakdown so they can audit at a glance."""
        owner_email = (self._cfg or {}).get("owner_email")
        if not owner_email:
            log.info("no owner_email configured — skipping completion email")
            return

        prod_table = (mapping.get("products_table") or {}).get("name", "products")
        list_table = (mapping.get("listings_table") or {}).get("name", "ebay_listings")

        # Headline KPIs
        n_queries = stats.get("queries_run", 0)
        n_items = stats.get("items_seen", 0)
        n_products = stats.get("products_upserted", 0)
        n_listings_in = stats.get("listings_inserted", 0)
        n_listings_up = stats.get("listings_updated", 0)
        n_skipped = stats.get("products_skipped_low_conf", 0)
        n_stale = stats.get("stale_listings_inactive", 0)

        # By-category rows (sorted by total volume).
        cats = sorted(
            (stats.get("by_category") or {}).items(),
            key=lambda kv: -(kv[1].get("listings_inserted", 0)
                             + kv[1].get("listings_updated", 0)),
        )
        cat_rows = "".join(
            f"<tr><td><code>{slug}</code></td>"
            f"<td>{c.get('products',0)}</td>"
            f"<td>{c.get('listings_inserted',0)}</td>"
            f"<td>{c.get('listings_updated',0)}</td></tr>"
            for slug, c in cats
        ) or "<tr><td colspan=4><i>(no category activity)</i></td></tr>"

        # Sample products — include the public catalog URL so the operator
        # can click straight to the SpecPicks page that renders the eBay
        # listings widget for that product.
        prod_rows = ""
        for p in (stats.get("product_samples") or [])[:15]:
            curl = p.get("catalog_url") or ""
            url_cell = (
                f"<a href=\"{curl}\" target=\"_blank\" style='color:#2563eb;font-size:12px'>view →</a>"
                if curl else "<small style='color:#94a3b8'>—</small>"
            )
            prod_rows += (
                f"<tr><td><strong>{p.get('name','')}</strong><br>"
                f"<small>{p.get('manufacturer','')}"
                f"{(' · ' + p.get('family')) if p.get('family') else ''}</small></td>"
                f"<td><code>{p.get('category','')}</code></td>"
                f"<td>{p.get('product_pk','?')}</td>"
                f"<td>{(p.get('confidence') or 0):.2f}</td>"
                f"<td>{url_cell}</td></tr>"
            )
        prod_rows = prod_rows or "<tr><td colspan=5><i>(no products created)</i></td></tr>"

        # Sample listings
        list_rows = ""
        for l in (stats.get("listing_samples") or [])[:15]:
            price = l.get("price")
            price_s = f"${float(price):.2f}" if price else "—"
            url = l.get("url") or "#"
            list_rows += (
                f"<tr><td><a href=\"{url}\" target=\"_blank\">{(l.get('title') or '')[:80]}</a></td>"
                f"<td>{price_s}</td>"
                f"<td>{(l.get('condition') or '').replace('_',' ').title()}</td>"
                f"<td><code>{l.get('category','')}</code></td>"
                f"<td>{l.get('product_pk','?')}</td></tr>"
            )
        list_rows = list_rows or "<tr><td colspan=5><i>(no listings ingested)</i></td></tr>"

        # Errors — group by error class to show what's actually broken
        # at a glance, then list a few sample items per class. Was just
        # showing the first 5 raw lines which made 80 identical FK
        # violations look opaque.
        all_errs = stats.get("errors") or []
        err_class_counts = stats.get("error_class_counts") or {}
        err_html = ""
        if all_errs:
            class_summary = ""
            if err_class_counts:
                for cls, cnt in sorted(err_class_counts.items(), key=lambda x: -x[1]):
                    class_summary += f"<li><strong>{cnt}×</strong> <code>{cls}</code></li>"
                class_summary = f"<p>Grouped by error class:</p><ul>{class_summary}</ul>"
            sample_rows = ""
            for e in all_errs[:6]:
                ident = e.get("q") or e.get("item") or ""
                title = e.get("title") or ""
                line = f"<code>{ident}</code>"
                if title: line += f" — <small>{title}</small>"
                line += f"<br><small style='color:#b45309'>{(e.get('err') or '')[:240]}</small>"
                sample_rows += f"<li>{line}</li>"
            err_html = (
                f"<h2 style='color:#b91c1c'>Errors ({len(all_errs)})</h2>"
                f"{class_summary}"
                f"<p style='font-size:13px;color:#64748b'>Sample:</p>"
                f"<ul>{sample_rows}</ul>"
            )

        # Total live eBay listings the catalog now holds (not just this run).
        # User explicitly asked: "have the email report how many total eBay
        # listings have been found." Pulled live from the DB so it reflects
        # state across all historical runs.
        total_live_listings = "?"
        total_active_listings = "?"
        try:
            from db_adapter import PostgresAdapter  # type: ignore
            # Use the same connection the agent already opened during run()
            ad = self._adapter if hasattr(self, "_adapter") else None
            if ad and ad.kind == "postgres":
                if hasattr(ad, "ensure_open"):
                    try: ad.ensure_open()
                    except Exception: pass
                cur = ad.conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {list_table}")
                total_live_listings = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM {list_table} WHERE is_active = true")
                total_active_listings = cur.fetchone()[0]
                cur.close()
        except Exception as e:
            log.warning("total-listings count query failed: %s", e)

        request_id = new_request_id()
        subject = encode_subject(
            self.agent_id, request_id,
            f"eBay sync results — {site_id}: {n_products} products, "
            f"{n_listings_in} new + {n_listings_up} updated listings",
        )

        body = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a;line-height:1.5;max-width:920px;margin:0 auto;padding:24px;}}
h1{{font-size:1.5rem;margin:0 0 8px}}
h2{{font-size:1.1rem;margin:24px 0 8px}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0 18px}}
.kpi{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;text-align:center}}
.kpi .n{{font-size:1.6rem;font-weight:700;color:#1e293b}}
.kpi .l{{font-size:.78rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-top:4px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border:1px solid #e2e8f0;padding:6px 10px;text-align:left;vertical-align:top}}
th{{background:#f1f5f9}}
code{{background:#f8fafc;padding:1px 4px;border-radius:3px;font-size:12.5px}}
small{{color:#64748b}}
</style></head><body>
<h1>eBay sync results — <code>{site_id}</code></h1>
<p>Run finished. Wrote canonical products into <code>{prod_table}</code> and live
   listings into <code>{list_table}</code>.</p>

<div class="kpis">
  <div class="kpi"><div class="n">{n_queries}</div><div class="l">queries this run</div></div>
  <div class="kpi"><div class="n">{n_items}</div><div class="l">items seen this run</div></div>
  <div class="kpi"><div class="n">{n_products}</div><div class="l">products upserted</div></div>
  <div class="kpi"><div class="n">{n_listings_in + n_listings_up}</div><div class="l">listings touched</div></div>
</div>

<h2>Catalog totals</h2>
<div class="kpis">
  <div class="kpi" style="background:#eff6ff;border-color:#bfdbfe">
    <div class="n">{total_live_listings}</div>
    <div class="l">total eBay listings (all time)</div>
  </div>
  <div class="kpi" style="background:#ecfdf5;border-color:#a7f3d0">
    <div class="n">{total_active_listings}</div>
    <div class="l">currently active</div>
  </div>
</div>

<p style="font-size:13px;color:#64748b">
  Listings inserted: <strong>{n_listings_in}</strong> &middot;
  updated: <strong>{n_listings_up}</strong> &middot;
  stale &rarr; inactive: <strong>{n_stale}</strong> &middot;
  skipped (low confidence / lots): <strong>{n_skipped}</strong>
</p>

<h2>Per-category activity</h2>
<table>
  <thead><tr><th>Category</th><th>Products</th><th>Listings inserted</th><th>Listings updated</th></tr></thead>
  <tbody>{cat_rows}</tbody>
</table>

<h2>Sample canonical products created (up to 15)</h2>
<p style="font-size:12px;color:#64748b;margin:0 0 6px">
  Each "view" link opens the SpecPicks product page where the eBay listings
  for that product render (via <code>/api/products/&lt;asin&gt;/ebay-listings</code>).
</p>
<table>
  <thead><tr><th>Product</th><th>Category</th><th>PK</th><th>Conf.</th><th>Catalog</th></tr></thead>
  <tbody>{prod_rows}</tbody>
</table>

<h2>Sample listings ingested (up to 15)</h2>
<table>
  <thead><tr><th>Listing</th><th>Price</th><th>Condition</th><th>Category</th><th>Product PK</th></tr></thead>
  <tbody>{list_rows}</tbody>
</table>

{err_html}

<p style="font-size:11px;color:#94a3b8;margin-top:32px">
  Sent by <code>{self.agent_id}</code> · request_id <code>{request_id}</code> ·
  next cron tick: see manifest cron_expr · listings older than
  {(self._cfg or {}).get("stale_hours", 36)}h are auto-marked inactive.
</p>
</body></html>"""

        ok, detail = self._send_email(
            subject=subject, body_html=body, to=[owner_email], request_id=request_id,
        )
        log.info("completion email: ok=%s detail=%s recipient=%s", ok, detail, owner_email)

    # ───────────────────────────────────────────────────────────────
    def _ingest(
        self, adapter: DbAdapter, table: str, site_id: str,
        site_constants: dict, mapping: dict, ebay: EbayClient,
        ebay_filter: str, seeds: list, per_query_limit: int,
        max_queries_per_run: int, stale_hours: int, dry_run: bool,
    ) -> RunResult:
        stats = {
            "queries_run": 0, "items_seen": 0, "rows_inserted": 0,
            "rows_updated": 0, "rows_failed": 0, "errors": [],
            "by_category": {},
        }
        key_cols = mapping.get("key_columns") or ["asin"]
        seen = set()

        # Flatten (category, query) tuples
        plan: list[tuple[Optional[str], str]] = []
        for entry in seeds:
            cat = entry.get("category") or entry.get("category_slug")
            for q in (entry.get("queries") or []):
                plan.append((cat, q))
        if not plan:
            log.warning("no seed queries configured")
            return RunResult(status="success", summary="no seeds configured")

        # Cap queries per run so cron ticks finish in bounded time.
        plan = plan[:max_queries_per_run]

        for cat, q in plan:
            stats["queries_run"] += 1
            try:
                items = ebay.search(q=q, filter_str=ebay_filter, limit=per_query_limit)
            except Exception as e:
                stats["rows_failed"] += 1
                stats["errors"].append({"q": q, "err": str(e)[:300]})
                log.warning("query %r failed: %s", q, e)
                continue
            stats["items_seen"] += len(items)
            log.info("[%s] %r → %d items", cat or "?", q, len(items))

            rows = []
            for item in items:
                # Sticky per-category override: tag the row with category_slug so
                # downstream UI / categorizer can resolve it.
                consts = dict(site_constants)
                if cat:
                    consts.setdefault("__category_slug_hint", cat)
                row = apply_mapping(item, mapping, site_constants=consts)
                # Strip the hint before write — destination doesn't have that col.
                cat_hint = row.pop("__category_slug_hint", None)
                if cat_hint and "category_slug" in row:
                    row["category_slug"] = cat_hint

                # Resolve category_slug → category_id if the destination uses ints.
                if cat_hint and "category_id" in row and row["category_id"] is None:
                    row["category_id"] = self._resolve_category_id(adapter, cat_hint)

                # Skip rows without the upsert key — eBay returned a malformed item.
                if any(row.get(k) in (None, "") for k in key_cols):
                    continue
                # Inflate site dedup
                key_tuple = tuple(row.get(k) for k in key_cols)
                if key_tuple in seen:
                    continue
                seen.add(key_tuple)
                rows.append(row)

            if dry_run:
                continue

            if rows:
                ir = adapter.upsert_rows(table, rows, key_cols)
                stats["rows_inserted"] += ir.inserted
                stats["rows_updated"] += ir.updated
                stats["rows_failed"] += ir.failed
                if cat:
                    bk = stats["by_category"].setdefault(cat, {"inserted": 0, "updated": 0, "failed": 0})
                    bk["inserted"] += ir.inserted
                    bk["updated"] += ir.updated
                    bk["failed"] += ir.failed
                if ir.failed_samples:
                    for s in ir.failed_samples[:2]:
                        log.warning("  upsert err sample: %s", s["err"])

            time.sleep(0.05)  # be polite to eBay

        if not dry_run:
            stale = _mark_stale_inactive(adapter, table, stale_hours)
            stats["stale_marked_inactive"] = stale

        log.info("ingestion complete: %s", json.dumps(stats, default=str))
        return RunResult(
            status="success",
            summary=(f"queries={stats['queries_run']}, "
                     f"inserted={stats['rows_inserted']}, "
                     f"updated={stats['rows_updated']}, "
                     f"failed={stats['rows_failed']}, "
                     f"stale_inactive={stats.get('stale_marked_inactive', 0)}"),
            metrics=stats,
        )

    # ───────────────────────────────────────────────────────────────
    def _resolve_category_id(self, adapter: DbAdapter, slug: str) -> Optional[int]:
        """Look up category_id by slug. Cached per-run on the adapter object."""
        cache = getattr(adapter, "_cat_cache", None)
        if cache is None:
            cache = {}
            setattr(adapter, "_cat_cache", cache)
        if slug in cache:
            return cache[slug]
        try:
            if adapter.kind == "postgres":
                cur = adapter.conn.cursor()
                cur.execute("SELECT id FROM categories WHERE slug = %s LIMIT 1", (slug,))
                r = cur.fetchone(); cur.close()
                cache[slug] = r[0] if r else None
            elif adapter.kind == "azure-sql":
                cur = adapter.conn.cursor()
                cur.execute("SELECT TOP 1 id FROM categories WHERE slug = ?", (slug,))
                r = cur.fetchone(); cur.close()
                cache[slug] = r[0] if r else None
        except Exception as e:
            log.warning("category lookup failed for %s: %s", slug, e)
            cache[slug] = None
        return cache[slug]

    # ───────────────────────────────────────────────────────────────
    def _make_ebay_client(self) -> "EbayClient":
        """Build an EbayClient from the site.yaml `ebay:` block.

        Each setting supports two forms:
          - `<name>_env: VAR_NAME`  → read `os.environ[VAR_NAME]`
          - `<name>: literal`       → use the literal string (dev only)
        Falls back to the conventional EBAY_* env vars when neither is set.
        """
        ec = self._ebay_cfg or {}

        def _resolve(key: str, default_env: str) -> str:
            env_key = ec.get(f"{key}_env") or default_env
            inline = ec.get(key)
            if inline:
                return str(inline)
            v = os.environ.get(env_key, "")
            return v

        client_id = _resolve("client_id", "EBAY_CLIENT_ID")
        client_secret = _resolve("client_secret", "EBAY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "eBay credentials not configured. Either set EBAY_CLIENT_ID/"
                "EBAY_CLIENT_SECRET in the env, point `ebay.client_id_env` "
                "and `ebay.client_secret_env` at custom var names in "
                "site.yaml, or store inline values under `ebay.client_id` "
                "and `ebay.client_secret` (dev only)."
            )
        env = (ec.get("env") or os.environ.get("EBAY_ENV", "PRODUCTION")).upper()
        marketplace = (ec.get("marketplace_id")
                        or os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US"))
        campaign_id = _resolve("campaign_id", "EBAY_CAMPAIGN_ID") or None

        return EbayClient(
            client_id=client_id,
            client_secret=client_secret,
            env=env,
            marketplace=marketplace,
            campaign_id=campaign_id,
        )

    # ───────────────────────────────────────────────────────────────
    def _send_email(self, *, subject: str, body_html: str, to: list[str],
                    request_id: str) -> tuple[bool, str]:
        """Real mail send. Tries the framework's Graph-or-msmtp path
        (shared.site_quality.send_via_msmtp). Falls back to LogMailer when
        neither is configured. Always records to storage either way so the
        dashboard's outbound-emails view shows the proposal."""
        sender = (self._cfg.get("sender_email")
                  or "noreply@reusable-agents.local")
        ok, detail = False, ""
        try:
            from shared.site_quality import send_via_msmtp  # type: ignore
            ok, detail = send_via_msmtp(
                subject=subject, body_html=body_html, to=to, sender=sender,
            )
        except Exception as e:
            ok, detail = False, f"send_via_msmtp unavailable: {e}"

        # Always record to outbound-emails/ for audit, regardless of send.
        try:
            storage = get_storage()
            from framework.core.mailer import outbound_email_key
            storage.write_json(outbound_email_key(self.agent_id, request_id), {
                "schema_version": "1",
                "request_id": request_id,
                "agent_id": self.agent_id,
                "subject": subject,
                "to": to,
                "body_html": body_html,
                "expects_response": True,
                "sent_at": _now(),
                "transport": "send_via_msmtp" if ok else "log-only",
                "ok": ok,
                "detail": detail,
            })
        except Exception:
            pass

        if not ok:
            log.warning("real email send failed: %s — proposal stored in outbound-emails/", detail)
        return ok, detail

    # ───────────────────────────────────────────────────────────────
    def _apply_ddl(self, adapter: DbAdapter, mapping: dict, dry_run: bool):
        """If mapping has CREATE TABLE statements, run them. Idempotent —
        the LLM is told to use IF NOT EXISTS where supported."""
        for section_name in ("products_table", "listings_table"):
            sec = mapping.get(section_name) or {}
            ddl = sec.get("create_ddl")
            if not ddl:
                continue
            log.info("applying %s.create_ddl (%d chars)", section_name, len(ddl))
            if dry_run:
                continue
            try:
                if adapter.kind == "postgres":
                    cur = adapter.conn.cursor()
                    cur.execute(ddl)
                    adapter.conn.commit()
                    cur.close()
                elif adapter.kind == "azure-sql":
                    cur = adapter.conn.cursor()
                    # Azure SQL doesn't have IF NOT EXISTS for CREATE TABLE
                    # uniformly; trust the LLM to gate appropriately or wrap.
                    cur.execute(ddl)
                    adapter.conn.commit()
                    cur.close()
                log.info("DDL for %s applied", section_name)
            except Exception as e:
                # If table already exists, that's fine. Anything else: surface.
                msg = str(e).lower()
                if "already exists" in msg or "object_name" in msg:
                    log.info("DDL for %s skipped (already exists)", section_name)
                else:
                    raise

    # ───────────────────────────────────────────────────────────────
    def _extract_edit_json(self, notes: str) -> Optional[dict]:
        """If the operator's reply was 'EDIT <json>', extract and return."""
        if not notes:
            return None
        s = notes.strip()
        if s.upper().startswith("EDIT"):
            s = s[4:].strip()
        if not s.startswith("{"):
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_dotenv(path: Path) -> None:
    """Minimal dotenv loader (no extra dep). Reads `KEY=VALUE` lines from
    `path` and writes any missing keys into `os.environ`. Existing env
    vars are NOT overwritten — process env wins, file is the fallback."""
    try:
        text = path.read_text()
    except Exception as e:
        log.warning("could not read %s: %s", path, e)
        return
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        # Strip wrapping quotes if present.
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except write to the destination DB.")
    parser.add_argument("--force-remap", action="store_true",
                        help="Discard the stored mapping and re-propose.")
    parser.add_argument("--reprice-only", action="store_true",
                        help="Only run the listing re-price pass (no discovery).")
    parser.add_argument("--reprice-limit", type=int, default=None,
                        help="Cap re-price lookups this run (≤ the budget cap).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.environ.get("EBAY_PRODUCT_SYNC_CONFIG"):
        print("ERROR: EBAY_PRODUCT_SYNC_CONFIG must be set to a yaml path",
              file=sys.stderr)
        sys.exit(2)
    agent = EbayProductSyncAgent()
    try:
        result = agent.run(
            run_kind="manual", dry_run=args.dry_run, force_remap=args.force_remap,
            reprice_only=args.reprice_only, reprice_limit=args.reprice_limit,
        )
    except ConfirmationPending as e:
        print(json.dumps({
            "status": "awaiting_confirmation",
            "confirmation_id": e.confirmation_id,
            "message": str(e),
        }, indent=2))
        sys.exit(0)
    print(json.dumps({
        "status": result.status, "summary": result.summary,
        "metrics": result.metrics,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
