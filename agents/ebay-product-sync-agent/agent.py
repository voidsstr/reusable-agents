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
e-commerce affiliate site and wants to ingest listings from the eBay
Browse API into their existing `products`-table-shaped database.

You receive:
  1. A list of destination columns (name, type, nullable, length,
     unique-flag).
  2. 1–3 sample existing rows (so you can infer conventions like "price
     is numeric, not string", "image_url is the canonical name").
  3. A real eBay Browse API itemSummary sample showing the source shape.

Produce a STRICT JSON mapping document. No prose, no markdown fences:

{
  "destination_table": "<table>",
  "key_columns": ["site_id","asin"],          // unique key for upsert dedup
  "fields": [
    {
      "destination_column": "asin",
      "source_path": "legacyItemId",           // dotted path into the eBay item
      "transform": "ebay_id_prefix",           // optional, see transform list
      "default": null,                          // fallback when source path is null
      "notes": "..."
    },
    ...
  ],
  "constants": [
    {"destination_column": "source",   "value": "ebay"},
    {"destination_column": "is_active", "value": true}
  ]
}

Allowed transforms (use ONLY these names — pick "null" or omit for
direct copy):
  ebay_id_prefix, parse_float, parse_int, iso_date,
  feedback_pct_to_5, affiliate_url, image_first,
  buying_options_csv, json_dumps, condition_lower,
  seller_username, seller_feedback_score,
  location_country, location_postal

Hard rules:
- Map every source field that has a clean destination column. If a column
  has no eBay equivalent, OMIT it (do NOT invent values).
- Use `condition_lower` for any column that stores condition text.
- Use `affiliate_url` for any column that stores a click-through URL.
- For the eBay item ID column (typically `asin` or `external_id`), ALWAYS
  use transform `ebay_id_prefix` so it shares a namespace with non-eBay
  listings.
- Pick `key_columns` to match the destination's unique constraint.
  If the destination has (site_id, asin) unique, use both.
- For any column tagged `source` / `marketplace` / `vendor`, add a constant
  with value "ebay".
- For boolean is_active / is_published / active columns, add constant true.
"""


def propose_mapping_via_claude(
    ai,
    destination_columns: list[ColumnInfo],
    sample_rows: list[dict],
    sample_ebay_item: dict,
    destination_table: str,
    site_id_value: Optional[str] = None,
) -> dict:
    """Build the prompt + call Claude + parse JSON. Returns the mapping doc.
    Raises on parse failure."""
    cols_lines = []
    for c in destination_columns:
        flags = []
        if c.is_unique: flags.append("UNIQUE")
        if not c.nullable: flags.append("NOT NULL")
        flag_s = " " + "/".join(flags) if flags else ""
        len_s = f"({c.max_length})" if c.max_length else ""
        cols_lines.append(f"  - {c.name}: {c.data_type}{len_s}{flag_s}")
    cols_text = "\n".join(cols_lines)

    # Truncate sample rows to avoid blowing context — show only the shape.
    sample_text = "(no rows yet)"
    if sample_rows:
        compact = []
        for r in sample_rows[:3]:
            compact.append({k: (str(v)[:120] if v is not None else None) for k, v in r.items()})
        sample_text = json.dumps(compact, indent=2, default=str)

    item_text = json.dumps(sample_ebay_item, indent=2, default=str)
    if len(item_text) > 4000:
        item_text = item_text[:4000] + "\n... (truncated)"

    user_prompt = f"""Destination table: `{destination_table}`
Operator-provided site_id constant: {site_id_value or '(none — emit a constant if needed)'}

Destination columns:
{cols_text}

Sample existing rows:
{sample_text}

Sample eBay Browse API itemSummary:
{item_text}

Produce the mapping JSON."""

    raw = ai.chat(
        [{"role": "system", "content": MAPPING_SYS},
         {"role": "user", "content": user_prompt}],
        model="claude-sonnet-4-6",
        max_tokens=8000,
        max_turns=4,
        timeout=420,
    )
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    i = s.find("{")
    j = s.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError(f"Claude did not return a JSON object. Got: {s[:300]!r}")
    return json.loads(s[i:j+1])


# ───────────────────────────────────────────────────────────────────
# Mapping email proposal
# ───────────────────────────────────────────────────────────────────

def _render_mapping_html(mapping: dict, destination_columns: list[ColumnInfo],
                          confirmation_id: str, request_id: str, agent_id: str,
                          site_id: str) -> tuple[str, str]:
    """Returns (subject, html). Subject embeds the request_id so inbound
    replies can be matched by the responder agent."""
    rows_html = ""
    for f in mapping.get("fields", []):
        rows_html += (
            f"<tr>"
            f"<td><code>{f.get('destination_column','')}</code></td>"
            f"<td><code>{f.get('source_path','') or '—'}</code></td>"
            f"<td>{f.get('transform') or '<i>direct</i>'}</td>"
            f"<td><code>{f.get('default') if f.get('default') is not None else ''}</code></td>"
            f"<td>{f.get('notes','') or ''}</td>"
            f"</tr>"
        )
    consts_html = ""
    for c in mapping.get("constants", []):
        consts_html += (
            f"<li><code>{c.get('destination_column')}</code> = "
            f"<code>{json.dumps(c.get('value'))}</code></li>"
        )
    keys = mapping.get("key_columns") or []
    subject_text = encode_subject(
        agent_id, request_id,
        f"eBay → {site_id}.{mapping.get('destination_table','products')} mapping",
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;max-width:780px;margin:0 auto;padding:24px;}}
h1{{font-size:1.5rem;margin:0 0 8px}}
h2{{font-size:1.1rem;margin:24px 0 8px;color:#1e293b}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}}
th,td{{border:1px solid #e2e8f0;padding:6px 10px;text-align:left;vertical-align:top}}
th{{background:#f1f5f9}}
code{{background:#f8fafc;padding:1px 4px;border-radius:3px;font-size:12.5px}}
.kbox{{background:#fff7ed;border:1px solid #fdba74;padding:12px 14px;border-radius:8px;margin:18px 0}}
.actions{{margin-top:28px;padding:16px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0}}
.actions code{{background:#fff}}
</style></head><body>
<h1>eBay → <code>{mapping.get('destination_table','products')}</code> schema mapping proposal</h1>
<p>Site: <strong>{site_id}</strong>. Generated by <code>{agent_id}</code>.</p>

<div class="kbox">
  <strong>Upsert key columns:</strong> {", ".join(f"<code>{k}</code>" for k in keys) or "<i>none</i>"}<br>
  These determine how the agent dedups across runs. Make sure they match
  the destination's unique constraint.
</div>

<h2>Field mapping ({len(mapping.get('fields', []))} columns)</h2>
<table>
  <thead><tr><th>Destination column</th><th>Source path (eBay item)</th>
    <th>Transform</th><th>Default</th><th>Notes</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2>Constants (always written)</h2>
<ul>{consts_html or '<li><i>none</i></li>'}</ul>

<div class="actions">
  <h2 style="margin-top:0">How to respond</h2>
  <p>Reply to this email with one of:</p>
  <ul>
    <li><code>APPROVE</code> — store this mapping and start ingesting on the next run.</li>
    <li><code>REJECT &lt;reason&gt;</code> — discard the mapping. Re-run the agent to
      generate a new proposal (e.g. after schema changes).</li>
    <li><code>EDIT &lt;json&gt;</code> — paste a corrected JSON mapping. The agent
      will overwrite the proposal with your version on approval.</li>
  </ul>
  <p style="font-size:12px;color:#64748b;margin-top:12px">
    Confirmation id: <code>{confirmation_id}</code><br>
    Request id: <code>{request_id}</code><br>
    Storage key:
    <code>{mapping_storage_key(agent_id, site_id)}</code>
  </p>
</div>
</body></html>"""
    return subject_text, html


# ───────────────────────────────────────────────────────────────────
# Stale-listing reaper
# ───────────────────────────────────────────────────────────────────

def _mark_stale_inactive(adapter: DbAdapter, table: str, hours: int) -> int:
    """eBay listings expire (auctions end, items get sold). Mark any
    `source='ebay'` row whose updated_at hasn't been touched in the last
    `hours` as is_active=false. Returns the count.
    """
    if hours <= 0:
        return 0
    if adapter.kind == "postgres":
        cur = adapter.conn.cursor()
        cur.execute(f"""
            UPDATE {table} SET is_active = false
             WHERE source = 'ebay' AND is_active = true
               AND updated_at < NOW() - INTERVAL '{int(hours)} hours'
        """)
        adapter.conn.commit()
        n = cur.rowcount
        cur.close()
        return n or 0
    if adapter.kind == "azure-sql":
        cur = adapter.conn.cursor()
        cur.execute(f"""
            UPDATE {table} SET is_active = 0
             WHERE source = 'ebay' AND is_active = 1
               AND updated_at < DATEADD(hour, -?, SYSUTCDATETIME())
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

    def run(self, *, run_kind: str = "manual", **kwargs) -> RunResult:
        config_path = os.environ.get("EBAY_PRODUCT_SYNC_CONFIG")
        if not config_path:
            raise RuntimeError("EBAY_PRODUCT_SYNC_CONFIG env var not set")
        cfg = yaml.safe_load(Path(config_path).read_text())

        site_id = cfg["site_id"]
        dest = cfg["destination"]
        dsn = os.path.expandvars(dest["dsn"])
        db_kind = dest.get("kind", "postgres")
        table = dest.get("table", "products")
        site_constants = dest.get("site_constants") or {}
        stale_hours = int(cfg.get("stale_hours", 72))

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
        try:
            return self._run_inner(
                adapter, table, site_id, site_constants, dest, cfg,
                seeds, per_query_limit, max_queries_per_run, ebay_filter,
                stale_hours, kwargs,
            )
        finally:
            adapter.close()

    # ───────────────────────────────────────────────────────────────
    def _run_inner(
        self, adapter: DbAdapter, table: str, site_id: str,
        site_constants: dict, dest: dict, cfg: dict,
        seeds: list, per_query_limit: int, max_queries_per_run: int,
        ebay_filter: str, stale_hours: int, kwargs: dict,
    ) -> RunResult:
        storage = get_storage()
        dry_run = bool(kwargs.get("dry_run", False))
        force_remap = bool(kwargs.get("force_remap", False))

        ebay = EbayClient.from_env()
        ebay_health = ebay.healthcheck()
        log.info("eBay OK: %s", ebay_health)

        # ─── PHASE 1 — load existing mapping or propose a new one ──
        mapping = None
        if not force_remap:
            existing = storage.read_json(mapping_storage_key(self.agent_id, site_id))
            if existing and existing.get("approved_at"):
                mapping = existing
                log.info("loaded approved mapping for site=%s (approved %s by %s)",
                         site_id, mapping.get("approved_at"), mapping.get("approved_by"))

        if not mapping:
            mapping = self._propose_or_resume_mapping(
                adapter, table, site_id, site_constants, ebay, ebay_filter,
                cfg.get("owner_email"),
            )
            # propose_or_resume_mapping raises ConfirmationPending if not yet approved.

        # ─── PHASE 2 — ingest ──────────────────────────────────────
        return self._ingest(
            adapter, table, site_id, site_constants, mapping, ebay, ebay_filter,
            seeds, per_query_limit, max_queries_per_run, stale_hours, dry_run,
        )

    # ───────────────────────────────────────────────────────────────
    def _propose_or_resume_mapping(
        self, adapter: DbAdapter, table: str, site_id: str,
        site_constants: dict, ebay: EbayClient, ebay_filter: str,
        owner_email: Optional[str],
    ) -> dict:
        """Returns the approved mapping. Raises ConfirmationPending if a
        proposal is already out and not yet answered, or if a fresh one
        was just emitted."""
        storage = get_storage()
        mapping_key = mapping_storage_key(self.agent_id, site_id)
        existing = storage.read_json(mapping_key) or {}

        # Was there already a pending proposal? Look up its confirmation.
        pending_cid = existing.get("pending_confirmation_id")
        if pending_cid:
            cr = get_confirmation(self.agent_id, pending_cid, storage)
            if cr is None:
                log.warning("pending confirmation %s not found — re-proposing", pending_cid)
            elif cr.state == "approved":
                # Operator approved. Promote pending to approved.
                approved = dict(existing)
                approved["approved_at"] = cr.resolved_at or _now()
                approved["approved_by"] = cr.approved_by or "unknown"
                approved.pop("pending_confirmation_id", None)
                # If the operator pasted EDIT <json> in their reply, the
                # responder will have stashed it in cr.notes — apply that.
                edit_json = self._extract_edit_json(cr.notes or "")
                if edit_json:
                    log.info("applying operator's EDIT <json> override")
                    edit_json["approved_at"] = approved["approved_at"]
                    edit_json["approved_by"] = approved["approved_by"]
                    approved = edit_json
                storage.write_json(mapping_key, approved)
                log.info("mapping approved for site=%s", site_id)
                return approved
            elif cr.state == "rejected":
                log.warning("operator rejected mapping (notes=%s) — clearing and re-proposing",
                            (cr.notes or "")[:200])
                storage.write_json(mapping_key, {})
            elif cr.state == "pending":
                raise ConfirmationPending(pending_cid, "mapping awaiting approval")

        # No approved/pending mapping → propose one.
        log.info("introspecting destination table %s", table)
        cols = adapter.introspect_table(table)
        if not cols:
            raise RuntimeError(f"Destination table `{table}` has no columns. Misconfigured?")
        log.info("introspected %d columns", len(cols))
        sample = []
        try: sample = adapter.sample_rows(table, n=3)
        except Exception as e:
            log.warning("sample_rows failed (continuing without): %s", e)

        log.info("fetching one eBay sample item")
        sample_items = ebay.search(q="raspberry pi", filter_str=ebay_filter, limit=1)
        if not sample_items:
            sample_items = ebay.search(q="laptop", limit=1)
        if not sample_items:
            raise RuntimeError("eBay search returned no items — credentials OK but search empty?")
        sample_item = sample_items[0]

        ai = ai_client_for(self.agent_id)
        log.info("calling Claude to propose mapping (%s)", type(ai).__name__)
        site_id_value = site_constants.get("site_id")
        proposal = propose_mapping_via_claude(
            ai, cols, sample, sample_item, table, site_id_value=site_id_value,
        )
        proposal["site_id"] = site_id

        # Send proposal email + record confirmation.
        confirmation_id = f"map-{site_id}-{int(time.time())}"
        request_id = new_request_id()
        subject, html = _render_mapping_html(
            proposal, cols, confirmation_id, request_id, self.agent_id, site_id,
        )

        mailer = self._get_mailer()
        recipients = [owner_email] if owner_email else []
        try:
            sent = mailer.send(
                agent_id=self.agent_id, request_id=request_id,
                subject=subject, body_html=html, to=recipients,
                expects_response=True,
            )
            if not sent:
                log.warning("mailer reported failure")
        except Exception as e:
            log.warning("mailer failed: %s", e)

        cr = ConfirmationRecord(
            confirmation_id=confirmation_id,
            agent_id=self.agent_id,
            method_name="ingest_to_destination",
            reason=f"Approve eBay→{table} mapping for site {site_id}",
            state="pending",
            requested_at=_now(),
            request_id=request_id,
        )
        write_confirmation(cr, storage)

        # Store the proposal alongside the pending confirmation id so the next
        # run can pick up where we left off.
        proposal_with_pending = dict(proposal)
        proposal_with_pending["pending_confirmation_id"] = confirmation_id
        storage.write_json(mapping_key, proposal_with_pending)

        raise ConfirmationPending(
            confirmation_id,
            "mapping proposal emailed; waiting for operator approval",
        )

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
    def _get_mailer(self):
        # The framework upgrades to SmtpMailer / SesMailer when configured.
        # Fall back to LogMailer (writes to storage) — the operator can
        # poll outbound-emails/ to see the proposal even without SMTP.
        try:
            from framework.core.mailer import SmtpMailer  # type: ignore
            return SmtpMailer()
        except Exception:
            return LogMailer()

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


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except write to the destination DB.")
    parser.add_argument("--force-remap", action="store_true",
                        help="Discard the stored mapping and re-propose.")
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
