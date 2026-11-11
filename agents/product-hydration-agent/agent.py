#!/usr/bin/env python3
"""product-hydration-agent — daily catalog content hydration.

Replaces the at-request-time content generators (contentGenerator.ts,
ai-content.ts) that were emitting AI content from Ollama / OpenAI per
HTTP request. Now content is precomputed once per product per
`stale_after_days` window and stored in DB columns the runtime can
read directly. Routes that used to call the generator now read from
DB and return HTTP 202 Accepted if the row hasn't been hydrated yet.

For each daily run:
  1. Read site.yaml from PRODUCT_HYDRATION_CONFIG env var.
  2. Connect to Postgres via DATABASE_URL (cfg.database.url_env).
  3. Select up to `batch_size` products that need hydration:
       any of {description, pros_cons, faq, seo_meta} NULL  OR
       hydrated_at NULL  OR
       hydrated_at < NOW() - stale_after_days
     Order by review_count DESC NULLS LAST so popular products
     hydrate first.
  4. For each product: build a single user prompt (product context +
     requested content types) and call `claude --print` with the
     system prompt at prompts/hydrate_product_system.md. Parse JSON.
  5. Validate each emitted content type and update the matching
     column(s); stamp hydrated_at + hydration_model. Commit per
     product so partial failures don't lose work.
  6. Track per-content-type validation pass/fail.
  7. Stop hydrating new products when max_runtime_minutes exceeded;
     finish in-flight; write partial results.
  8. Write run artifacts (results.json, hydration-log.jsonl,
     llm-output.txt, context-summary.md, goal-progress.json) to
     framework storage and to the local run dir.
  9. Email digest if (cfg.email.send_only_when_failures AND failures>0)
     OR if today is Monday (weekly status); otherwise no email.
 10. Return RunResult with totals + catalog coverage %.

Invoke:
    PRODUCT_HYDRATION_CONFIG=/path/to/site.yaml \
        DATABASE_URL='postgresql://...' \
        python3 agent.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Make framework + shared importable regardless of cwd.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402
from framework.core.resilience import with_retry, notify_operator  # noqa: E402
from framework.core import pg_conn  # noqa: E402

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
import yaml  # noqa: E402

# Sibling modules — colocated with agent.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paapi_client import (  # noqa: E402
    PaapiClient, PaapiConfig, PaapiError, parse_get_items_response,
)
from brightdata_client import (  # noqa: E402
    BrightDataClient, BrightDataConfig, BrightDataError, parse_bd_record,
)


AGENT_ID = "product-hydration-agent"

# Map content_type -> (column_name, sql_value_builder_fn, validator_fn)
# value_builder takes the parsed JSON value and returns a SQL parameter
# (string for description, JSON-serialized string for JSONB).
def _v_description(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def _v_pros_cons(v: Any) -> str | None:
    """Validate {pros[5], cons[5]}. Each bullet capped at 100 chars to
    fit Amazon's 5-bullet PDP shape and Google rich-snippet rendering.
    Trim any LLM overrun rather than reject — the rest of the row is
    usable even if a few bullets were long."""
    if not isinstance(v, dict):
        return None
    pros = v.get("pros") or []
    cons = v.get("cons") or []
    if not isinstance(pros, list) or not isinstance(cons, list):
        return None
    pros = [str(p).strip()[:100] for p in pros
            if isinstance(p, (str, int, float)) and str(p).strip()]
    cons = [str(c).strip()[:100] for c in cons
            if isinstance(c, (str, int, float)) and str(c).strip()]
    pros = pros[:5]
    cons = cons[:5]
    if not pros and not cons:
        return None
    return json.dumps({"pros": pros, "cons": cons})


def _v_faq(v: Any) -> str | None:
    if not isinstance(v, list):
        return None
    out = []
    for item in v:
        if not isinstance(item, dict):
            continue
        q = (item.get("question") or "").strip()
        a = (item.get("answer") or "").strip()
        if q and a:
            out.append({"question": q, "answer": a})
    if not out:
        return None
    return json.dumps(out)


def _v_seo_meta(v: Any) -> str | None:
    """Validate seo_meta. Title hard-capped at 60 (Google SERP truncation).
    Meta description hard-capped at 160 with a soft floor of 120 — values
    below 120 chars don't fail validation but get a `short_meta_description`
    flag the runtime can use to trigger rebuilds."""
    if not isinstance(v, dict):
        return None
    title = (v.get("title") or "").strip()[:60]
    meta_desc = (v.get("meta_description") or "").strip()[:160]
    keywords = v.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    keywords = [str(k).strip() for k in keywords
                if isinstance(k, (str, int, float)) and str(k).strip()]
    if not title and not meta_desc and not keywords:
        return None
    payload = {"title": title, "meta_description": meta_desc,
               "keywords": keywords[:7]}
    if meta_desc and len(meta_desc) < 120:
        payload["short_meta_description"] = True
    return json.dumps(payload)


# content_type -> (column, validator/builder, is_jsonb)
CONTENT_SPEC: dict[str, tuple[str, Any, bool]] = {
    "description": ("description", _v_description, False),
    "pros_cons":   ("pros_cons",   _v_pros_cons,   True),
    "faq":         ("faq",         _v_faq,         True),
    "seo_meta":    ("seo_meta",    _v_seo_meta,    True),
}


PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "hydrate_product_system.md"


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

@with_retry(retries=3, backoff=1.5,
            on=(psycopg2.OperationalError, psycopg2.InterfaceError))
def _connect(dsn: str):
    return pg_conn.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)


def _reopen(conn, dsn: str):
    """The connection after a long LLM call: reuse it if alive, else reconnect.
    Azure Postgres drops idle connections after ~5 min, and a claude-pool
    wait easily exceeds that (2026-09-27: a whole run of 80 hydrated
    products failed to persist with "connection already closed")."""
    return pg_conn.ensure_open(conn, dsn, cursor_factory=psycopg2.extras.RealDictCursor)


def _select_candidates(conn, *, content_types: list[str],
                       batch_size: int, stale_after_days: int,
                       site_id_filter: str | None,
                       priority_column: str = "review_count",
                       priority_asins: list[str] | None = None) -> list[dict]:
    """Return rows that need any of `content_types` hydrated.

    A row is a candidate if EITHER:
      - hydrated_at IS NULL (never hydrated), OR
      - hydrated_at < NOW() - stale_after_days, OR
      - any of the requested content_types' columns IS NULL.

    `priority_asins` (from `hydration.priority_asins_sql`) are selected
    before everything else, then `priority_column DESC`. Ordering by
    review_count alone never reaches rows with no reviews (a site's eBay
    listings), however much traffic their pages get.
    """
    cols = []
    for ct in content_types:
        col, _, _ = CONTENT_SPEC[ct]
        cols.append(col)
    # All hydration columns live on `products` (alias `p`).
    null_clauses = " OR ".join([f"p.{c} IS NULL" for c in cols]) or "FALSE"
    site_clause = ""
    params: list[Any] = []
    if site_id_filter:
        site_clause = "AND p.site_id = %s"
        params.append(site_id_filter)
    params.append(stale_after_days)
    prio_order = ""
    if priority_asins:
        prio_order = "(p.asin = ANY(%s)) DESC, "
        params.append(list(priority_asins))
    params.append(batch_size)

    # `category_slug` was a denormalized column; the schema now keeps the
    # slug on the categories table referenced via products.category_id.
    # Join + alias so callers still see `category_slug` in the row dict.
    sql = f"""
        SELECT p.id, p.site_id, p.asin, p.title, p.brand, p.price, p.rating, p.review_count,
               p.features, p.description, c.slug AS category_slug,
               p.pros_cons, p.faq, p.seo_meta, p.hydrated_at
        FROM products p
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.is_active = TRUE
          {site_clause}
          AND (
              {null_clauses}
              OR p.hydrated_at IS NULL
              OR p.hydrated_at < NOW() - (%s || ' days')::interval
          )
        ORDER BY {prio_order}p.{priority_column} DESC NULLS LAST, p.id ASC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _priority_asins(conn, sql: str | None, *, timeout_ms: int = 30000,
                    cap: int = 50000) -> tuple[list[str], str | None]:
    """Run the site's optional `hydration.priority_asins_sql` (a read-only
    SELECT whose first column is an ASIN). Returns (asins, error). A failure
    never blocks hydration -- the run falls back to priority_column order."""
    if not sql or not str(sql).strip():
        return [], None
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s", (int(timeout_ms),))
            cur.execute(str(sql))
            rows = cur.fetchmany(cap)
        conn.commit()
        out = []
        for r in rows:
            v = list(r.values())[0] if isinstance(r, dict) else r[0]
            if v:
                out.append(str(v))
        return out, None
    except Exception as e:  # noqa: BLE001 -- optional ordering hint
        try:
            conn.rollback()
        except Exception:
            pass
        return [], str(e)[:300]


def _coverage_stats(conn, *, content_types: list[str],
                    site_id_filter: str | None,
                    stale_after_days: int) -> dict[str, Any]:
    """Compute catalog-level coverage for goal-progress.json."""
    site_clause = "AND site_id = %s" if site_id_filter else ""
    params: list[Any] = []
    if site_id_filter:
        params.append(site_id_filter)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS total FROM products WHERE is_active = TRUE {site_clause}",
            params,
        )
        total = cur.fetchone()["total"] or 0

        per_type: dict[str, int] = {}
        for ct in content_types:
            col, _, _ = CONTENT_SPEC[ct]
            cur.execute(
                f"SELECT COUNT(*) AS n FROM products "
                f"WHERE is_active = TRUE {site_clause} AND {col} IS NOT NULL",
                params,
            )
            per_type[ct] = cur.fetchone()["n"] or 0

        # All-fields populated
        all_clauses = " AND ".join(
            [f"{CONTENT_SPEC[ct][0]} IS NOT NULL" for ct in content_types]
        )
        cur.execute(
            f"SELECT COUNT(*) AS n FROM products "
            f"WHERE is_active = TRUE {site_clause} AND {all_clauses}",
            params,
        )
        fully_hydrated = cur.fetchone()["n"] or 0

        # Stale: hydrated_at older than threshold
        cur.execute(
            f"SELECT COUNT(*) AS n FROM products WHERE is_active = TRUE "
            f"{site_clause} AND hydrated_at IS NOT NULL "
            f"AND hydrated_at < NOW() - (%s || ' days')::interval",
            [*params, stale_after_days],
        )
        stale = cur.fetchone()["n"] or 0

    pct = lambda n: round(100.0 * n / total, 2) if total > 0 else 0.0
    return {
        "total_active_products": total,
        "fully_hydrated": fully_hydrated,
        "fully_hydrated_pct": pct(fully_hydrated),
        "per_content_type": {
            ct: {"populated": n, "pct": pct(n)} for ct, n in per_type.items()
        },
        "stale_count": stale,
        "stale_pct": pct(stale),
    }


@with_retry(retries=3, backoff=1.5,
            on=(psycopg2.OperationalError, psycopg2.InterfaceError))
def _persist_product(conn, product_id: int, updates: dict[str, str | None],
                     model: str) -> None:
    """Write the validated content_type columns for one product, stamp
    hydrated_at + hydration_model. Commit per product so partial work
    survives mid-run failures."""
    if not updates:
        return
    set_clauses = []
    params: list[Any] = []
    for ct, val in updates.items():
        col, _, is_jsonb = CONTENT_SPEC[ct]
        if is_jsonb:
            set_clauses.append(f"{col} = %s::jsonb")
        else:
            set_clauses.append(f"{col} = %s")
        params.append(val)
    set_clauses.append("hydrated_at = NOW()")
    set_clauses.append("hydration_model = %s")
    params.append(model)
    params.append(product_id)
    sql = f"UPDATE products SET {', '.join(set_clauses)} WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


# ---------------------------------------------------------------------------
# Amazon PA-API price refresh
# ---------------------------------------------------------------------------

# The SQL the tiered selector below ranks first: published articles' declared
# products and published buying-guide picks. Shared with the demand-ranked
# path, where it is a site SQL group.
_EDITORIAL_ASINS_SQL = """
                SELECT DISTINCT unnest(related_product_asins) AS asin
                FROM editorial_articles
                WHERE status='published' AND related_product_asins IS NOT NULL
                UNION
                SELECT DISTINCT (p->>'asin') AS asin
                FROM buying_guides bg, jsonb_array_elements(bg.picks) p
                WHERE bg.status='published'
                  AND jsonb_typeof(bg.picks)='array'
                  AND (p->>'asin') IS NOT NULL
                  AND (p->>'asin') <> ''
"""

_HYDRATION_ASIN_REGEX = "^[A-Z0-9]{10}$"


def _demand_ranked_config(raw_cfg: dict, *, agent_id: str, site: str,
                          site_id_filter: str | None, storage=None) -> dict | None:
    """The resolved framework price_refresh_priority config for this run, or
    None when demand ranking is off for this agent (the default).

    Turned on by site.yaml `price_refresh.demand_priority: {enabled: true,
    sources: {...}}` or by storage config/price-refresh-priority-config.json
    (by_site.<site> / by_agent_id.<agent>). This agent supplies the catalog
    (the products table it already prices), its 24h freshness window as the
    long tail's gate, and -- when prioritize_featured_and_linked is on -- the
    featured and editorially-linked products as site SQL groups after the
    demand tiers. Any problem resolving it keeps the old selector.

    Freshness is the newer of price_updated_at and last_fetched_at: an ASIN
    Amazon left out of a response only records the attempt in
    last_fetched_at (see _stamp_miss), and must go to the back of its tier
    rather than return to the head of the queue every run."""
    try:
        from framework.core import price_refresh_priority as prp
    except Exception:
        return None
    freshness_hours = int(raw_cfg.get("price_freshness_hours", 24))
    where = ("c.is_active = TRUE AND c.asin IS NOT NULL AND c.asin <> ''"
             + (" AND c.site_id = %(hyd_site_id)s" if site_id_filter else ""))
    site_cfg: dict = {
        "catalog": {"table": "products", "asin_col": "asin",
                    "fresh_cols": ["price_updated_at", "last_fetched_at"],
                    "where": where,
                    "params": ({"hyd_site_id": site_id_filter} if site_id_filter else {}),
                    "asin_regex": _HYDRATION_ASIN_REGEX},
        "long_tail": {"enabled": True, "gate_hours": freshness_hours},
    }
    if raw_cfg.get("prioritize_featured_and_linked"):
        site_cfg["extra_groups"] = [
            {"name": "featured", "kind": "sql", "gate_hours": freshness_hours,
             "sql": "SELECT asin FROM products WHERE is_featured = TRUE AND is_active = TRUE"},
            {"name": "editorial", "kind": "sql", "gate_hours": freshness_hours,
             "sql": _EDITORIAL_ASINS_SQL},
        ]
    block = raw_cfg.get("demand_priority")
    try:
        if isinstance(block, dict):
            site_cfg = prp.merge_config(site_cfg, block)
        cfg = prp.resolve_config(agent_id=agent_id, site=site, site_cfg=site_cfg,
                                 storage=storage)
    except Exception:
        return None
    return cfg if cfg.get("enabled") else None


def _select_demand_ranked(conn, demand_cfg: dict, *, site_id_filter: str | None,
                          limit: int, storage=None) -> tuple[list[dict], dict]:
    """Stale product rows in framework demand order (price_refresh_priority):
    recently clicked, then AI-used, then the site groups, then the long tail
    oldest first -- instead of review_count. Returns (rows, info); every
    active row of a chosen ASIN is returned, each tagged with its `tier`."""
    from framework.core import price_refresh_priority as prp
    tiers = prp.collect_tiers(conn, demand_cfg, storage=storage)
    cands = prp.rank_refresh_candidates(conn, demand_cfg, limit=limit, tiers=tiers)
    info = {"by_tier": prp.tier_counts(cands), "signals": tiers.summary()}
    if not cands:
        return [], info
    site_clause = "AND p.site_id = %s" if site_id_filter else ""
    params: list[Any] = [[c.asin for c in cands]]
    if site_id_filter:
        params.append(site_id_filter)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT p.id, p.asin, p.title, p.price, p.price_updated_at, p.source
              FROM products p
             WHERE p.asin = ANY(%s) AND p.is_active = TRUE {site_clause}
             ORDER BY p.id""", params)
        rows = [dict(r) for r in cur.fetchall()]
    pos = {c.asin: i for i, c in enumerate(cands)}
    tier = {c.asin: c.tier for c in cands}
    rows.sort(key=lambda r: (pos.get(r["asin"], len(pos)), r["id"]))
    for r in rows:
        r["tier"] = tier.get(r["asin"], prp.LONG_TAIL)
    return rows, info


def _select_stale_amazon_prices(conn, *, site_id_filter: str | None,
                                  freshness_hours: int,
                                  limit: int,
                                  prioritize_featured_and_linked: bool = False,
                                  demand_cfg: dict | None = None,
                                  demand_info: dict | None = None,
                                  storage=None) -> list[dict]:
    """Return Amazon-sourced products whose price_updated_at is older than
    `freshness_hours` (or NULL) and that were not attempted within the window
    either: GREATEST() ignores NULLs, so a last_fetched_at stamped by a miss
    (_stamp_miss: the ASIN was left out of the response) defers the row
    instead of re-picking it every run.

    With `demand_cfg` (see _demand_ranked_config) the framework's
    demand-ranked order is used instead, and `demand_info` (when given) is
    filled with its per-tier counts. If that path fails, this falls back to
    the modes below and records the error in `demand_info["error"]`.

    Two ordering modes:
      • plain: `ORDER BY review_count DESC` — old behavior
      • tiered (when prioritize_featured_and_linked=True): rank with
            tier 0 = is_featured (95 products today)
            tier 1 = ASIN referenced in any published article or
                     buying-guide pick (~276 today)
            tier 2 = high-review-count tail (review_count DESC)
        Within a tier, ORDER BY review_count DESC NULLS LAST.
        Featured + editorially-linked tiers therefore refresh on
        every run (since they fit comfortably under the per-run cap),
        which keeps the most user-visible prices <24h fresh while the
        long tail catches up.
    """
    if demand_cfg:
        try:
            rows, info = _select_demand_ranked(conn, demand_cfg,
                                               site_id_filter=site_id_filter,
                                               limit=limit, storage=storage)
            if demand_info is not None:
                demand_info.update(info)
            return rows
        except Exception as e:  # noqa: BLE001 -- the old selector still works
            try:
                conn.rollback()
            except Exception:
                pass
            if demand_info is not None:
                demand_info["error"] = f"{type(e).__name__}: {e}"[:300]

    site_clause = "AND p.site_id = %s" if site_id_filter else ""
    params: list[Any] = []
    if site_id_filter:
        params.append(site_id_filter)
    params.append(freshness_hours)
    params.append(limit)

    if prioritize_featured_and_linked:
        sql = f"""
            WITH editorial_asins AS ({_EDITORIAL_ASINS_SQL}
            )
            SELECT p.id, p.asin, p.title, p.price, p.price_updated_at, p.source,
                   CASE
                     WHEN p.is_featured = TRUE THEN 0
                     WHEN p.asin IN (SELECT asin FROM editorial_asins) THEN 1
                     ELSE 2
                   END AS tier
            FROM products p
            WHERE p.is_active = TRUE
              AND p.asin IS NOT NULL
              AND p.asin <> ''
              AND p.asin ~ '^[A-Z0-9]{{10}}$'
              {site_clause}
              AND (
                  GREATEST(p.price_updated_at, p.last_fetched_at) IS NULL
                  OR GREATEST(p.price_updated_at, p.last_fetched_at)
                     < NOW() - (%s || ' hours')::interval
              )
            ORDER BY tier ASC, COALESCE(p.review_count, 0) DESC, p.id ASC
            LIMIT %s
        """
    else:
        sql = f"""
            SELECT p.id, p.asin, p.title, p.price, p.price_updated_at, p.source
            FROM products p
            WHERE p.is_active = TRUE
              AND p.asin IS NOT NULL
              AND p.asin <> ''
              AND p.asin ~ '^[A-Z0-9]{{10}}$'
              {site_clause}
              AND (
                  GREATEST(p.price_updated_at, p.last_fetched_at) IS NULL
                  OR GREATEST(p.price_updated_at, p.last_fetched_at)
                     < NOW() - (%s || ' hours')::interval
              )
            ORDER BY COALESCE(p.review_count, 0) DESC, p.id ASC
            LIMIT %s
        """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# quality_flags is '{}' (a legacy object default) on many rows; fold it to an
# empty array so flag removal / concatenation stays a flat array.
_FLAGS_BASE = "COALESCE(NULLIF(quality_flags, '{}'::jsonb), '[]'::jsonb)"


def _outcome_flags_cfg(raw_cfg: dict) -> dict | None:
    """The site's Amazon-offer flags a price verdict must keep right, from
    site.yaml ``price_refresh.outcome_flags``:

        outcome_flags:
          owned: [flag, ...]                 # cleared by every verdict
          by_status: {unavailable: [...], not_found: [...],
                      no_offer: [...], non_usd: [...]}   # set per outcome

    i.e. the flags the site's own price verifier owns. A site that reads such
    flags as "unbuyable" needs this before this agent re-prices products
    ahead of that verifier (the demand-ranked order does): otherwise a
    product that came back in stock is priced but stays flagged unbuyable,
    and one that went out of stock is never flagged. None (the default) =
    no flag bookkeeping, as before. Flags named under by_status are always
    cleared too, so a verdict never duplicates one."""
    block = raw_cfg.get("outcome_flags") if isinstance(raw_cfg, dict) else None
    if not isinstance(block, dict):
        return None
    owned = [str(f) for f in (block.get("owned") or []) if str(f).strip()]
    by_status = {str(k): [str(f) for f in (v or []) if str(f).strip()]
                 for k, v in (block.get("by_status") or {}).items()
                 if isinstance(v, (list, tuple))}
    for flags in by_status.values():
        owned += [f for f in flags if f not in owned]
    if not owned:
        return None
    return {"owned": owned, "by_status": by_status}


@with_retry(retries=3, backoff=1.5,
            on=(psycopg2.OperationalError, psycopg2.InterfaceError))
def _persist_provider_refresh(conn, product_id: int, parsed: dict, *,
                                provider: str) -> None:
    """Apply normalized provider output (Creators, PA-API or BrightData) to
    a product row, tag raw_amazon_data with the source provider for
    auditability, and commit per row.

    price_updated_at is the site's "this price was checked at" time: the
    24h display gate and the PDP's price-as-of date read it. So it is only
    stamped when this refresh decided the price:
      * ``price`` set: the price is written (price_updated_at + last_fetched_at).
      * ``unpriced`` set: the provider answered and there is no valid price
        (out of stock, no New offer, used-only, foreign currency). price and
        original_price become NULL, availability is written (default
        'Currently unavailable'), and both timestamps are stamped.
      * neither: nothing is known about the price, so price, availability
        and price_updated_at are left alone and only last_fetched_at records
        the attempt. Stamping price_updated_at here re-dated a stale price
        as fresh: on 2026-11-11, 90 specpicks rows read "Currently
        unavailable." next to a price this agent had just re-stamped.
    ``list_price_current`` means the provider reports the list price
    whenever Amazon shows one (Creators savingBasis), so a missing one is
    written as NULL. That is the price verifier's rule: a retained old list
    price is how a "Save $X" off a stale basis appears.
    ``flags_clear`` / ``flags_add`` (from _outcome_flags_cfg) rewrite the
    site's Amazon-offer flags with a priced or unpriced verdict, never
    without one."""
    set_clauses: list[str] = []
    params: list[Any] = []
    price = parsed.get("price")
    flags_clear = parsed.get("flags_clear")
    if isinstance(flags_clear, list) and (price is not None or parsed.get("unpriced")):
        # The site's own Amazon-offer flags (price_refresh.outcome_flags): a
        # verdict written here must leave them as the site's price verifier
        # would, or a product that came back in stock stays "unbuyable".
        set_clauses.append(f"quality_flags = ({_FLAGS_BASE} - %s::text[]) "
                           "|| to_jsonb(%s::text[])")
        params += [list(flags_clear), list(parsed.get("flags_add") or [])]
    if price is not None:
        set_clauses += ["price = %s", "price_updated_at = NOW()",
                        "last_fetched_at = NOW()"]
        params.append(price)
        if parsed.get("list_price_current"):
            set_clauses.append("original_price = %s")
            params.append(parsed.get("original_price"))
        elif parsed.get("original_price") is not None:
            set_clauses.append("original_price = %s")
            params.append(parsed["original_price"])
        else:
            # No list price in this refresh, but the row may still hold an
            # OLD one. If the new price has risen above it we would leave
            # behind original_price < price -- a "Save -$50" negative
            # discount, which the pricing-integrity rules forbid on any
            # visible pick.
            #
            # This is a real regression seen in the field: a Creators run
            # that refreshed 430 rows pushed catalog-wide negative discounts
            # from 277 to 314, purely from stale list prices left in place.
            # Clearing conditionally (rather than unconditionally) preserves
            # legitimate discounts where the stored list price is still
            # above the price.
            set_clauses.append(
                "original_price = CASE WHEN original_price < %s THEN NULL "
                "ELSE original_price END")
            params.append(price)
        if parsed.get("currency"):
            set_clauses.append("currency = %s")
            params.append(parsed["currency"])
        if parsed.get("availability"):
            set_clauses.append("availability = LEFT(%s, 100)")
            params.append(parsed["availability"])
    elif parsed.get("unpriced"):
        set_clauses += ["price = NULL", "original_price = NULL",
                        "availability = LEFT(COALESCE(%s, 'Currently unavailable'), 100)",
                        "price_updated_at = NOW()", "last_fetched_at = NOW()"]
        params.append(parsed.get("availability") or None)
    else:
        set_clauses.append("last_fetched_at = NOW()")
    if parsed.get("is_prime") is not None:
        set_clauses.append("is_prime = %s")
        params.append(bool(parsed["is_prime"]))
    if parsed.get("rating") is not None:
        set_clauses.append("rating = %s")
        params.append(parsed["rating"])
    if parsed.get("review_count") is not None:
        set_clauses.append("review_count = %s")
        params.append(parsed["review_count"])
    if parsed.get("main_image_url"):
        set_clauses.append("main_image_url = %s")
        params.append(parsed["main_image_url"])
    if parsed.get("amazon_url"):
        set_clauses.append("amazon_url = %s")
        params.append(parsed["amazon_url"])
    if parsed.get("brand"):
        set_clauses.append("brand = COALESCE(NULLIF(brand, ''), %s)")
        params.append(parsed["brand"])
    # Mirror the parsed payload into raw_amazon_data for auditability.
    set_clauses.append("raw_amazon_data = %s::jsonb")
    params.append(json.dumps({
        "source": provider,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "main_image_width": parsed.get("main_image_width"),
        "main_image_height": parsed.get("main_image_height"),
        "image_urls": parsed.get("image_urls") or [],
        "features": parsed.get("features") or [],
        "bs_rank": parsed.get("bs_rank"),
        "bought_past_month": parsed.get("bought_past_month"),
        "compliance": _compliance_flags(parsed),
    }))
    params.append(product_id)
    sql = f"UPDATE products SET {', '.join(set_clauses)} WHERE id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def _compliance_flags(parsed: dict) -> dict:
    """Compute Amazon + Google guideline pass/fail flags from PA-API output.
    Stored on raw_amazon_data so the runtime can hide non-compliant rows
    and the dashboard can surface coverage % over time."""
    title = parsed.get("title") or ""
    img_w = parsed.get("main_image_width") or 0
    img_h = parsed.get("main_image_height") or 0
    long_side = max(img_w or 0, img_h or 0)
    has_brand = bool(parsed.get("brand"))
    has_image = bool(parsed.get("main_image_url"))
    has_price = parsed.get("price") is not None
    return {
        # Amazon Associates / PDP guidelines
        "amazon_title_in_range": 80 <= len(title) <= 200,
        "amazon_image_zoom_eligible": long_side >= 1000,
        "amazon_has_brand": has_brand,
        "amazon_has_main_image": has_image,
        # Google Search / Merchant Center
        "google_seo_title_fits": 1 <= len(title) <= 60 if title else False,
        "google_has_offer": has_price,
        "google_jsonld_ready": has_image and has_price and bool(title),
    }


def _refresh_via_paapi(*, conn, paapi_cfg: PaapiConfig, raw_cfg: dict,
                        site_id_filter: str | None,
                        status_cb, decide_cb, demand_cfg: dict | None = None,
                        storage=None) -> dict:
    """Refresh price + offer data via Amazon PA-API. Used once SpecPicks
    qualifies for the Creators API (3 qualifying purchases). Until then
    the BrightData path is the default — see _refresh_via_brightdata."""
    freshness_hours = int(raw_cfg.get("price_freshness_hours", 24))
    batch_size = int(raw_cfg.get("paapi_batch_size", 10))
    max_per_run = int(raw_cfg.get("max_refresh_per_run", 200))
    candidates = _select_stale_amazon_prices(
        conn, site_id_filter=site_id_filter,
        freshness_hours=freshness_hours, limit=max_per_run,
        prioritize_featured_and_linked=bool(
            raw_cfg.get("prioritize_featured_and_linked", False)),
        demand_cfg=demand_cfg, storage=storage,
    )
    decide_cb("observation",
              f"PA-API: {len(candidates)} stale-price products to refresh "
              f"(freshness={freshness_hours}h, cap={max_per_run})")
    summary = {
        "provider": "paapi",
        "refreshed": 0, "failed": 0, "processed": 0,
        "batches": 0, "asins_seen": 0,
        "compliance_pass": 0, "compliance_fail": 0,
    }
    if not candidates:
        return summary

    client = PaapiClient(paapi_cfg)
    by_asin = {p["asin"]: p for p in candidates}
    asins = list(by_asin.keys())
    for i in range(0, len(asins), batch_size):
        chunk = asins[i:i + batch_size]
        summary["batches"] += 1
        status_cb(
            f"PA-API batch {summary['batches']}: {len(chunk)} ASINs",
            progress=0.05 + 0.05 * (i / max(1, len(asins))),
            current_action=f"prices {i+1}-{i+len(chunk)}/{len(asins)}",
        )
        try:
            resp = client.get_items(chunk)
        except PaapiError as e:
            summary["failed"] += len(chunk)
            decide_cb("error", f"PA-API GetItems failed for "
                                f"batch {summary['batches']}: {str(e)[:200]}")
            continue
        parsed = parse_get_items_response(resp)
        summary["asins_seen"] += len(parsed)
        for asin, payload in parsed.items():
            row = by_asin.get(asin)
            if not row:
                continue
            try:
                _persist_provider_refresh(conn, row["id"], payload,
                                            provider="paapi")
                summary["refreshed"] += 1
                compliance = _compliance_flags(payload)
                if all(compliance.values()):
                    summary["compliance_pass"] += 1
                else:
                    summary["compliance_fail"] += 1
            except Exception as e:
                summary["failed"] += 1
                # Roll back so the next iteration's queries don't fail with
                # "current transaction is aborted, commands ignored until
                # end of transaction block." Without this, a single bad
                # ASIN poisons the connection for the entire run, including
                # the post-refresh _select_candidates() call.
                try:
                    conn.rollback()
                except Exception:
                    pass
                decide_cb("error",
                           f"PA-API persist failed for {asin}: {str(e)[:200]}")
        missing = [a for a in chunk if a not in parsed]
        for a in missing:
            row = by_asin.get(a)
            if not row:
                continue
            try:
                _stamp_miss(conn, row["id"], "paapi: not in response")
                summary["processed"] += 1
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                decide_cb("error",
                           f"PA-API miss-stamp failed for {a}: {str(e)[:200]}")
    summary["processed"] = summary["refreshed"] + summary["failed"]
    return summary


def _refresh_via_creators(*, conn, creators_client, raw_cfg: dict,
                          site_id_filter: str | None,
                          status_cb, decide_cb, demand_cfg: dict | None = None,
                          storage=None) -> dict:
    """Refresh price + offer data via the Amazon Creators API.

    This is the provider to use: it is the only Amazon path whose
    credentials are actually provisioned fleet-wide. PA-API needs
    AMAZON_PAAPI_ACCESS_KEY/SECRET_KEY/ASSOCIATE_TAG (absent) and
    BrightData needs BRIGHTDATA_API_KEY (absent), so before this existed
    every hydration run on both sites reported a skipped_reason and
    refreshed nothing.

    Shares _select_stale_amazon_prices + _persist_provider_refresh with the
    other providers, so the candidate ordering, freshness window, per-row
    commit, and compliance accounting are identical -- only the fetch
    differs. With `demand_cfg` the candidates come in the framework's
    demand-ranked order (price_refresh_priority) and the summary carries
    `demand_ranked` and `by_tier` (candidates); `refreshed_by_tier` is
    added only when that order was actually used.

    Each answered item is classified with the framework's price outcome
    rules (amazon_price_refresh.classify_item), the same rules the price
    verifier and catalog-freshness write by: `priced` writes the price and
    Amazon's current list price (or none); out of stock / no New offer /
    used-only / foreign currency writes price NULL (`unpriced`, counted in
    `refreshed` and `unpriced`); a per-ASIN error (ItemNotAccessible) also
    clears the price (`inaccessible`); an ASIN left out of the response
    only records the attempt; a failed batch writes nothing. With
    `price_refresh.outcome_flags` set, every verdict also rewrites the
    site's Amazon-offer flags the way its price verifier does.
    """
    from framework.core.amazon_creators import (
        CreatorsUnavailable, hi_res_image_url, parse_item)
    from framework.core.amazon_price_refresh import classify_item

    freshness_hours = int(raw_cfg.get("price_freshness_hours", 24))
    # The API caps getItems at 10 ASINs; the client chunks internally, but
    # keeping our loop aligned makes the per-batch status readable.
    batch_size = int(raw_cfg.get("creators_batch_size", 10))
    max_per_run = int(raw_cfg.get("max_refresh_per_run", 200))
    flags = _outcome_flags_cfg(raw_cfg)
    demand_info: dict = {}
    candidates = _select_stale_amazon_prices(
        conn, site_id_filter=site_id_filter,
        freshness_hours=freshness_hours, limit=max_per_run,
        prioritize_featured_and_linked=bool(
            raw_cfg.get("prioritize_featured_and_linked", False)),
        demand_cfg=demand_cfg, demand_info=demand_info, storage=storage,
    )
    demand_ranked = bool(demand_cfg) and "error" not in demand_info
    if demand_cfg and demand_info.get("error"):
        decide_cb("error", "demand-ranked price selection failed, used the "
                           f"review_count order: {demand_info['error']}")
    order_note = (" demand-ranked " + ", ".join(
        f"{k}={v}" for k, v in (demand_info.get("by_tier") or {}).items())
        if demand_ranked else "")
    decide_cb("observation",
              f"Creators: {len(candidates)} stale-price products to refresh "
              f"(freshness={freshness_hours}h, cap={max_per_run}){order_note}")
    summary = {
        "provider": "creators",
        "refreshed": 0, "failed": 0, "processed": 0,
        "batches": 0, "asins_seen": 0, "inaccessible": 0, "unpriced": 0,
        "compliance_pass": 0, "compliance_fail": 0,
    }
    if demand_cfg:
        summary["demand_ranked"] = demand_ranked
        summary["by_tier"] = dict(demand_info.get("by_tier") or {})
        if demand_ranked:
            # Only a demand-ranked selection tags rows with tier NAMES. The
            # fallback selector's integer tiers (0 featured / 1 editorial /
            # 2 tail) would otherwise be booked as demand refreshes.
            summary["refreshed_by_tier"] = {}
        if demand_info.get("error"):
            summary["demand_error"] = demand_info["error"]
        if (demand_info.get("signals") or {}).get("errors"):
            summary["demand_signal_errors"] = demand_info["signals"]["errors"]
    if not candidates:
        return summary

    # Every candidate row of an ASIN is written: a {asin: row} map updated
    # one row and left the ASIN's other active row stale, so it was picked
    # again next run, forever.
    by_asin: dict[str, list[dict]] = {}
    for p in candidates:
        by_asin.setdefault(p["asin"], []).append(p)
    asins = list(by_asin.keys())
    for i in range(0, len(asins), batch_size):
        chunk = asins[i:i + batch_size]
        summary["batches"] += 1
        status_cb(
            f"Creators batch {summary['batches']}: {len(chunk)} ASINs",
            progress=0.05 + 0.05 * (i / max(1, len(asins))),
            current_action=f"prices {i+1}-{i+len(chunk)}/{len(asins)}",
        )
        try:
            items, errors = creators_client.get_items(chunk)
        except CreatorsUnavailable as e:
            # Auth/credential failure is not per-batch bad luck -- every
            # remaining batch would fail the same way. Stop and report.
            decide_cb("error", f"Creators API unavailable: {str(e)[:200]}")
            summary["failed"] += len(asins) - i
            break
        except Exception as e:
            summary["failed"] += len(chunk)
            decide_cb("error", f"Creators getItems failed for batch "
                               f"{summary['batches']}: {str(e)[:200]}")
            continue

        summary["asins_seen"] += len(items)
        for asin, raw in items.items():
            rows = by_asin.get(asin) or []
            if not rows:
                continue
            p = parse_item(raw)
            o = classify_item(asin, raw)
            priced = o.status == "priced"
            # Map to the shared persist contract's key names. The price
            # fields follow the outcome, never the raw listing: a used-only,
            # out-of-stock or foreign-currency item has no price to show.
            payload = {
                "price": o.price if priced else None,
                "original_price": o.original_price if priced else None,
                "list_price_current": True,
                "unpriced": not priced,
                "currency": o.currency if priced else None,
                # A priced row with an empty availability message reads as
                # in stock (the price verifier's default), never as the
                # "Currently unavailable" an earlier unpriced write left.
                "availability": (o.availability or ("In Stock" if priced else None)),
                # the site's Amazon-offer flags, as its price verifier keeps them
                "flags_clear": flags["owned"] if flags else None,
                "flags_add": ([] if priced else flags["by_status"].get(o.status, []))
                             if flags else None,
                "rating": p.get("rating"),
                "review_count": p.get("review_count"),
                # SL1500 form, the one shelf-audit writes: the raw 500px URL
                # re-orphaned heroes and comparison images on every refresh.
                "main_image_url": hi_res_image_url(p.get("image_url")),
                "amazon_url": p.get("url"),
                "brand": p.get("brand"),
                "title": p.get("title"),
            }
            for row in rows:
                try:
                    _persist_provider_refresh(conn, row["id"], payload,
                                              provider="creators")
                    summary["refreshed"] += 1
                    if not priced:
                        summary["unpriced"] += 1
                    if "refreshed_by_tier" in summary:
                        t = row.get("tier")
                        t = t if isinstance(t, str) and t else "long-tail"
                        summary["refreshed_by_tier"][t] = summary["refreshed_by_tier"].get(t, 0) + 1
                    compliance = _compliance_flags(payload)
                    if all(compliance.values()):
                        summary["compliance_pass"] += 1
                    else:
                        summary["compliance_fail"] += 1
                except Exception as e:
                    summary["failed"] += 1
                    # Same rationale as the PA-API path: without a rollback one
                    # bad row poisons the connection for the rest of the run.
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    decide_cb("error",
                              f"Creators persist failed for {asin}: {str(e)[:200]}")

        # An ItemNotAccessible ASIN is delisted/regional/not-offered: no
        # valid price (outcome not_found), so the price is cleared and
        # re-dated, which also ages it out of the stale queue instead of
        # retrying it every single run forever.
        for asin, msg in errors.items():
            rows = by_asin.get(asin) or []
            if not rows:
                continue
            summary["inaccessible"] += 1
            for row in rows:
                try:
                    _mark_not_found(
                        conn, row["id"], f"creators: {str(msg)[:120]}",
                        flags_clear=flags["owned"] if flags else None,
                        flags_add=flags["by_status"].get("not_found", []) if flags else None)
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
        for a in chunk:
            if a in items or a in errors:
                continue
            for row in by_asin.get(a) or []:
                try:
                    _stamp_miss(conn, row["id"], "creators: not in response")
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
    summary["processed"] = summary["refreshed"] + summary["failed"]
    return summary


def _refresh_via_brightdata(*, conn, bd_cfg: BrightDataConfig, raw_cfg: dict,
                             site_id_filter: str | None,
                             status_cb, decide_cb, demand_cfg: dict | None = None,
                             storage=None) -> dict:
    """Refresh price + offer data via Bright Data's Amazon Products
    dataset. Default provider until PA-API qualification clears."""
    freshness_hours = int(raw_cfg.get("price_freshness_hours", 24))
    batch_size = int(raw_cfg.get("brightdata_batch_size", 100))
    max_per_run = int(raw_cfg.get("max_refresh_per_run", 200))
    candidates = _select_stale_amazon_prices(
        conn, site_id_filter=site_id_filter,
        freshness_hours=freshness_hours, limit=max_per_run,
        prioritize_featured_and_linked=bool(
            raw_cfg.get("prioritize_featured_and_linked", False)),
        demand_cfg=demand_cfg, storage=storage,
    )
    decide_cb("observation",
              f"BrightData: {len(candidates)} stale-price products "
              f"(freshness={freshness_hours}h, cap={max_per_run}, "
              f"batch={batch_size})")
    summary = {
        "provider": "brightdata",
        "refreshed": 0, "failed": 0, "processed": 0,
        "batches": 0, "asins_seen": 0,
        "compliance_pass": 0, "compliance_fail": 0,
        "estimated_cost_usd": 0.0,
    }
    if not candidates:
        return summary

    client = BrightDataClient(bd_cfg)
    by_asin = {p["asin"]: p for p in candidates}
    asins = list(by_asin.keys())
    for i in range(0, len(asins), batch_size):
        chunk = asins[i:i + batch_size]
        summary["batches"] += 1
        status_cb(
            f"BrightData batch {summary['batches']}: {len(chunk)} ASINs "
            f"(can take 30-90s)",
            progress=0.05 + 0.05 * (i / max(1, len(asins))),
            current_action=f"prices {i+1}-{i+len(chunk)}/{len(asins)}",
        )
        urls = [f"https://www.amazon.com/dp/{a}" for a in chunk]
        try:
            records = client.scrape(urls)
        except BrightDataError as e:
            summary["failed"] += len(chunk)
            decide_cb("error",
                       f"BrightData scrape failed for batch "
                       f"{summary['batches']}: {str(e)[:200]}")
            continue
        seen_asins: set[str] = set()
        for rec in records:
            payload = parse_bd_record(rec)
            if not payload:
                continue
            asin = payload["asin"]
            seen_asins.add(asin)
            row = by_asin.get(asin)
            if not row:
                continue
            try:
                _persist_provider_refresh(conn, row["id"], payload,
                                            provider="brightdata")
                summary["refreshed"] += 1
                summary["asins_seen"] += 1
                compliance = _compliance_flags(payload)
                # Bright Data doesn't return image dimensions, so the
                # zoom-eligible flag would always be False — skip it
                # from the pass/fail check on BD-sourced rows.
                bd_relevant = {k: v for k, v in compliance.items()
                                if k != "amazon_image_zoom_eligible"}
                if all(bd_relevant.values()):
                    summary["compliance_pass"] += 1
                else:
                    summary["compliance_fail"] += 1
            except Exception as e:
                summary["failed"] += 1
                # Rollback the aborted transaction (see PA-API path).
                try:
                    conn.rollback()
                except Exception:
                    pass
                decide_cb("error",
                           f"BD persist failed for {asin}: {str(e)[:200]}")
        missing = [a for a in chunk if a not in seen_asins]
        for a in missing:
            row = by_asin.get(a)
            if not row:
                continue
            try:
                _stamp_miss(conn, row["id"], "brightdata: warning/no-data")
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                decide_cb("error",
                           f"BD miss-stamp failed for {a}: {str(e)[:200]}")
    summary["processed"] = summary["refreshed"] + summary["failed"]
    summary["estimated_cost_usd"] = round(summary["asins_seen"] * 0.001, 4)
    return summary


@with_retry(retries=3, backoff=1.5,
            on=(psycopg2.OperationalError, psycopg2.InterfaceError))
def _stamp_miss(conn, product_id: int, reason: str) -> None:
    """Record a fetch miss (the provider silently left the ASIN out) without
    touching the price. Stamps last_fetched_at, NOT price_updated_at: the
    price was not checked, so it must keep ageing toward the display gate.
    Both selectors treat a recent last_fetched_at as "tried", so the row is
    not retried every tick."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE products SET last_fetched_at = NOW(), "
            "fetch_attempts = COALESCE(fetch_attempts, 0) + 1, "
            "fetch_error = %s WHERE id = %s",
            [reason[:500], product_id],
        )
    conn.commit()


@with_retry(retries=3, backoff=1.5,
            on=(psycopg2.OperationalError, psycopg2.InterfaceError))
def _mark_not_found(conn, product_id: int, reason: str, *,
                    flags_clear: list | None = None,
                    flags_add: list | None = None) -> None:
    """Amazon answered with a per-ASIN error (ItemNotAccessible / invalid
    ASIN): there is no valid price, so it is cleared and re-dated, the same
    write the price verifier and catalog-freshness make for a not_found
    outcome (framework amazon_price_refresh). The page stays live (never
    deactivated); the site shows "check price on Amazon". With the site's
    outcome flags configured, they are rewritten like the verifier does."""
    flags_sql, params = "", []
    if isinstance(flags_clear, list):
        flags_sql = (f"quality_flags = ({_FLAGS_BASE} - %s::text[]) "
                     "|| to_jsonb(%s::text[]), ")
        params = [list(flags_clear), list(flags_add or [])]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE products SET price = NULL, original_price = NULL, "
            "availability = 'Currently unavailable', " + flags_sql +
            "price_updated_at = NOW(), last_fetched_at = NOW(), "
            "fetch_attempts = COALESCE(fetch_attempts, 0) + 1, "
            "fetch_error = %s WHERE id = %s",
            params + [reason[:500], product_id],
        )
    conn.commit()


def _price_outcome_metrics(summary: dict) -> dict:
    """RunResult metrics for the price pass beyond refreshed/failed.

    prices_demand_ranked: 1 when the candidates came in the framework
    demand order (price_refresh_priority). prices_refreshed_demand: rows
    refreshed outside the long tail (recently clicked / AI-used / site
    groups), counted ONLY on a demand-ranked run; a run that fell back to
    the review_count selector reports 0, never its featured/editorial
    tiers. prices_unpriced: rows whose price was cleared because Amazon
    answered with no valid offer; prices_inaccessible: ASINs Amazon
    reported not accessible (price cleared too)."""
    demand_ranked = bool(summary.get("demand_ranked"))
    by_tier = (summary.get("refreshed_by_tier") or {}) if demand_ranked else {}
    return {
        "prices_demand_ranked": int(demand_ranked),
        "prices_refreshed_demand": sum(
            n for t, n in by_tier.items() if isinstance(t, str) and t != "long-tail"),
        "prices_unpriced": int(summary.get("unpriced") or 0),
        "prices_inaccessible": int(summary.get("inaccessible") or 0),
    }


def _refresh_amazon_prices(*, conn, raw_cfg: dict,
                            paapi_cfg: PaapiConfig | None,
                            bd_cfg: BrightDataConfig | None,
                            provider: str,
                            site_id_filter: str | None,
                            status_cb, decide_cb,
                            agent_id: str = "", site: str = "",
                            storage=None) -> dict:
    """Top-level dispatcher — selects the provider and delegates.

    `creators` is the Amazon path whose credentials are provisioned, so it
    is also the automatic fallback: a site still configured for `paapi` or
    `brightdata` (neither of which has credentials on this host) would
    otherwise silently refresh nothing forever. Falling back beats
    reporting a skip, and the swap is recorded via decide_cb so it shows up
    in the run's decision log rather than happening invisibly.

    `agent_id` / `site` / `storage` resolve the optional demand-ranked
    candidate order (framework price_refresh_priority; off unless the site
    or the storage config turns it on for this agent).
    """
    from framework.core.amazon_creators import client_from_env

    demand_cfg = _demand_ranked_config(raw_cfg, agent_id=agent_id, site=site,
                                       site_id_filter=site_id_filter,
                                       storage=storage)
    if demand_cfg:
        decide_cb("observation",
                  "price candidates: framework demand-ranked order ("
                  + " > ".join(g["name"] for g in demand_cfg["groups"])
                  + f" > long tail @{demand_cfg['long_tail'].get('gate_hours')}h)")

    creators_client = client_from_env(
        partner_tag=raw_cfg.get("associate_tag", "") or "",
        marketplace=raw_cfg.get("marketplace", "") or "",
        consumer="hydration",  # shared Creators daily-budget line
    )
    if provider in ("paapi", "brightdata") and creators_client:
        have_configured_creds = (paapi_cfg if provider == "paapi" else bd_cfg)
        if not have_configured_creds:
            decide_cb("observation",
                      f"provider={provider} has no credentials in env; "
                      f"falling back to the Amazon Creators API")
            provider = "creators"

    if provider == "creators":
        if not creators_client:
            return {"provider": "creators", "skipped_reason":
                    "Creators credentials not in env "
                    "(AMAZON_CREATORS_CLIENT_ID / CLIENT_SECRET / PARTNER_TAG)",
                    "refreshed": 0, "failed": 0, "processed": 0,
                    "batches": 0, "asins_seen": 0,
                    "compliance_pass": 0, "compliance_fail": 0}
        return _refresh_via_creators(
            conn=conn, creators_client=creators_client, raw_cfg=raw_cfg,
            site_id_filter=site_id_filter,
            status_cb=status_cb, decide_cb=decide_cb,
            demand_cfg=demand_cfg, storage=storage,
        )
    if provider == "paapi":
        if not paapi_cfg:
            return {"provider": "paapi", "skipped_reason":
                    "PA-API credentials not in env "
                    "(AMAZON_PAAPI_ACCESS_KEY / SECRET_KEY / ASSOCIATE_TAG)",
                    "refreshed": 0, "failed": 0, "processed": 0,
                    "batches": 0, "asins_seen": 0,
                    "compliance_pass": 0, "compliance_fail": 0}
        return _refresh_via_paapi(
            conn=conn, paapi_cfg=paapi_cfg, raw_cfg=raw_cfg,
            site_id_filter=site_id_filter,
            status_cb=status_cb, decide_cb=decide_cb,
            demand_cfg=demand_cfg, storage=storage,
        )
    if provider == "brightdata":
        if not bd_cfg:
            return {"provider": "brightdata", "skipped_reason":
                    "BRIGHTDATA_API_KEY not in env",
                    "refreshed": 0, "failed": 0, "processed": 0,
                    "batches": 0, "asins_seen": 0,
                    "compliance_pass": 0, "compliance_fail": 0,
                    "estimated_cost_usd": 0.0}
        return _refresh_via_brightdata(
            conn=conn, bd_cfg=bd_cfg, raw_cfg=raw_cfg,
            site_id_filter=site_id_filter,
            status_cb=status_cb, decide_cb=decide_cb,
            demand_cfg=demand_cfg, storage=storage,
        )
    return {"provider": provider,
            "skipped_reason": f"unknown provider: {provider}",
            "refreshed": 0, "failed": 0, "processed": 0,
            "batches": 0, "asins_seen": 0,
            "compliance_pass": 0, "compliance_fail": 0}


# ---------------------------------------------------------------------------
# Claude prompt + invocation
# ---------------------------------------------------------------------------

def _build_user_prompt(product: dict, content_types: list[str]) -> str:
    """Assemble the product context as the user message."""
    features = product.get("features") or []
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except Exception:
            features = [features]
    if not isinstance(features, list):
        features = []
    features = [str(f).strip() for f in features if str(f).strip()][:10]

    raw_desc = (product.get("description") or "").strip()
    if len(raw_desc) > 600:
        raw_desc = raw_desc[:600] + "…"

    ctx = {
        "title": product.get("title") or "",
        "asin": product.get("asin") or "",
        "brand": product.get("brand") or "",
        "category": product.get("category_slug") or "",
        "price_usd": float(product["price"]) if product.get("price") is not None else None,
        "rating": float(product["rating"]) if product.get("rating") is not None else None,
        "review_count": int(product["review_count"]) if product.get("review_count") is not None else None,
        "features": features,
        "raw_description": raw_desc,
    }
    return (
        "PRODUCT CONTEXT (JSON):\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\nrequested_content_types: "
        + json.dumps(content_types)
        + "\n\nReturn the JSON object now."
    )


@with_retry(retries=2, backoff=2.0, base_delay=2.0,
            on=(subprocess.TimeoutExpired, IOError, OSError))
def _pool_admits(model: str) -> bool:
    """False when CLAUDE_POOL_RESERVE is set and no more than that many
    claude-pool profiles can serve `model`: this agent is a background Opus
    consumer and must not take the profile authoring needs. Checked before
    each product (cheap state.json read) so a run stops as soon as headroom
    goes, instead of burning a failover per product."""
    reserve = int(os.environ.get("CLAUDE_POOL_RESERVE", "0") or 0)
    if reserve <= 0:
        return True
    try:
        from framework.cli.claude_pool import ready_profiles
        return len(ready_profiles(model)) > reserve
    except Exception:  # noqa: BLE001 - never block hydration on a read error
        return True


def _claude_call(*, system_prompt: str, user_prompt: str, model: str,
                  max_turns: int, timeout_s: int) -> tuple[str, dict]:
    """Run claude --print with separate system/user prompts. Returns
    (stdout, metadata) where metadata includes wall_clock_s + chars.

    Mirrors article-author / benchmark-research: each call appends a
    request + response (or error) record to the framework's LLMStream
    so the dashboard's Live LLM tab shows live progress per ASIN.
    """
    started = time.time()
    cmd = [
        "claude",
        "--print",
        "--output-format", "text",
        "--model", model,
        "--max-turns", str(max_turns),
        "--append-system-prompt", system_prompt,
        "--dangerously-skip-permissions",
    ]

    live_stream = None
    try:
        from framework.core.llm_stream import stream_for_current_run  # type: ignore
        live_stream = stream_for_current_run()
        if live_stream:
            live_stream.request(model=model, messages=[
                {"role": "system", "content": system_prompt[:8000]},
                {"role": "user",   "content": user_prompt[:8000]},
            ], provider="claude-cli", max_turns=max_turns)
    except Exception:
        live_stream = None

    try:
        proc = subprocess.run(
            cmd, input=user_prompt, capture_output=True, text=True, timeout=timeout_s,
        )
    except Exception as e:
        if live_stream:
            try: live_stream.error(str(e), duration_s=time.time() - started)
            except Exception: pass
        raise
    elapsed = time.time() - started
    meta = {
        "wall_clock_s": round(elapsed, 2),
        "stdout_chars": len(proc.stdout or ""),
        "stderr_chars": len(proc.stderr or ""),
        "rc": proc.returncode,
    }
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout)[:300]
        if live_stream:
            try: live_stream.error(f"rc={proc.returncode}: {msg}",
                                    duration_s=elapsed)
            except Exception: pass
        raise RuntimeError(f"claude rc={proc.returncode}: {msg}")
    out = (proc.stdout or "").strip()
    if live_stream:
        try: live_stream.response(out, duration_s=elapsed,
                                  model=model, provider="claude-cli")
        except Exception: pass
    return out, meta


def _parse_claude_json(raw: str) -> dict:
    """Extract the top-level JSON object from claude's output, tolerant
    of fenced blocks + leading/trailing prose."""
    if not raw:
        return {}
    s = raw.strip()
    # Strip code fences if present
    m = re.search(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", s)
    if m:
        s = m.group(1)
    else:
        # Largest top-level {...}
        first = s.find("{")
        last = s.rfind("}")
        if first >= 0 and last > first:
            s = s[first:last + 1]
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Email digest
# ---------------------------------------------------------------------------

def _maybe_send_digest(*, cfg: dict, agent_id: str, run_ts: str,
                       totals: dict, coverage: dict,
                       failures: list[dict], successes: list[dict]) -> tuple[bool, str]:
    email_cfg = cfg.get("email") or {}
    to = email_cfg.get("to") or []
    sender = email_cfg.get("from", "")
    if not to or not sender:
        return False, "no recipient/sender configured"
    only_failures = bool(email_cfg.get("send_only_when_failures", True))
    failures_n = len(failures)
    is_monday = datetime.now(timezone.utc).weekday() == 0
    should_send = (failures_n > 0 if only_failures else True) or is_monday
    if not should_send:
        return False, f"suppressed (no failures, weekday={datetime.now(timezone.utc).weekday()})"

    site = (cfg.get("site") or {}).get("id", "")
    subject_tpl = email_cfg.get(
        "subject_template",
        "[HYDRATION:{site}] {n} products hydrated — {date}",
    )
    subject = subject_tpl.format(
        site=site,
        n=totals.get("hydrated", 0),
        p=totals.get("prices_refreshed", 0),
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )

    rows_html = []
    for s in successes[:50]:
        rows_html.append(
            f"<tr><td><code>{s['asin']}</code></td>"
            f"<td>{s['title'][:70]}</td>"
            f"<td>{', '.join(s['fields'])}</td>"
            f"<td>{s['wall_clock_s']:.1f}s</td></tr>"
        )
    fail_rows = []
    for f in failures[:50]:
        fail_rows.append(
            f"<tr><td><code>{f['asin']}</code></td>"
            f"<td>{f['title'][:70]}</td>"
            f"<td style='color:#b91c1c'>{f['error'][:160]}</td></tr>"
        )

    prices_refreshed = totals.get("prices_refreshed", 0)
    prices_failed = totals.get("prices_failed", 0)
    compliance_pass = totals.get("compliance_pass", 0)
    compliance_fail = totals.get("compliance_fail", 0)
    body = f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;color:#0f172a;line-height:1.5">
<div style="max-width:820px;margin:0 auto;padding:20px">
  <h2 style="margin:0 0 8px;color:#0f172a">Product Hydration — {site}</h2>
  <div style="color:#475569;font-size:13px;margin-bottom:18px">
    Run <code>{run_ts}</code> — {totals.get('hydrated', 0)} hydrated /
    {totals.get('failed', 0)} failed / {totals.get('skipped_already_fresh', 0)} already fresh
    · {prices_refreshed} prices refreshed via Amazon PA-API
  </div>

  <h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Amazon PA-API price refresh</h3>
  <table style="font-size:13px;border-collapse:collapse">
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">Prices refreshed (24h freshness)</td><td>{prices_refreshed}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">PA-API failures</td><td style="color:{'#b91c1c' if prices_failed else '#0f172a'}">{prices_failed}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">Amazon + Google compliance pass</td><td>{compliance_pass} / {compliance_pass + compliance_fail}</td></tr>
  </table>

  <h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Catalog coverage</h3>
  <table style="font-size:13px;border-collapse:collapse">
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">Total active products</td><td>{coverage.get('total_active_products')}</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">Fully hydrated</td><td>{coverage.get('fully_hydrated')} ({coverage.get('fully_hydrated_pct')}%)</td></tr>
    <tr><td style="padding:4px 12px 4px 0;color:#64748b">Stale</td><td>{coverage.get('stale_count')} ({coverage.get('stale_pct')}%)</td></tr>
  </table>

  <h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Per-content-type coverage</h3>
  <table style="font-size:13px;border-collapse:collapse">
    {''.join(f'<tr><td style="padding:2px 12px 2px 0;color:#64748b">{ct}</td><td>{v["populated"]} ({v["pct"]}%)</td></tr>' for ct, v in coverage.get('per_content_type', {}).items())}
  </table>

  {'<h3 style="margin-top:18px;font-size:14px;color:#b91c1c;border-bottom:1px solid #fecaca;padding-bottom:4px">Failures</h3><table style="font-size:12px;border-collapse:collapse"><tr><th>ASIN</th><th>Title</th><th>Error</th></tr>' + ''.join(fail_rows) + '</table>' if fail_rows else ''}

  <h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Hydrated this run</h3>
  <table style="font-size:12px;border-collapse:collapse"><tr><th>ASIN</th><th>Title</th><th>Fields</th><th>Time</th></tr>{''.join(rows_html)}</table>

  <div style="color:#64748b;font-size:11px;margin-top:18px">
    Run dir: <code>agents/{agent_id}/runs/{run_ts}/</code>
  </div>
</div></body></html>"""

    try:
        from shared.site_quality import send_via_msmtp
    except Exception as e:
        return False, f"send_via_msmtp unavailable: {e}"
    msmtp_account = email_cfg.get("msmtp_account", "automation")
    try:
        ok, detail = send_via_msmtp(
            subject=subject, body_html=body, to=to, sender=sender,
            msmtp_account=msmtp_account,
            extra_headers={"X-Reusable-Agent": agent_id},
        )
        return ok, detail
    except Exception as e:
        return False, f"send raised: {e}"


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ProductHydrationAgent(AgentBase):
    agent_id = AGENT_ID
    send_run_summary_email = False  # already sends its own completion email
    name = "Product Hydration Agent"
    description = (
        "Daily catalog content hydration — replaces request-time AI "
        "content generation. Picks the highest-priority products needing "
        "description / pros_cons / faq / seo_meta and asks Claude to "
        "produce SEO-optimized, citation-ready content. Writes back to "
        "DB columns the runtime reads directly."
    )
    category = "research"

    capabilities = [
        declare("read_db",
                "Read products table to pick + prioritize hydration candidates",
                confirmation_required=False, risk_level="low"),
        declare("call_claude",
                "Invoke claude --print to generate hydrated content per product",
                confirmation_required=False, risk_level="low"),
        declare("write_db",
                "Update products table (description, pros_cons, faq, seo_meta, "
                "hydrated_at, hydration_model) per product",
                confirmation_required=False, risk_level="medium",
                affects=["product-catalog"]),
        declare("send_email_digest",
                "Send optional daily/weekly hydration digest email",
                confirmation_required=False, risk_level="low"),
    ]

    def __init__(self, *args, **kwargs):
        env_id = os.environ.get("AGENT_ID")
        if env_id and env_id != AGENT_ID:
            kwargs.setdefault("agent_id", env_id)
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        cfg_path = os.environ.get("PRODUCT_HYDRATION_CONFIG")
        if not cfg_path:
            raise SystemExit("PRODUCT_HYDRATION_CONFIG env var not set")
        if not Path(cfg_path).is_file():
            raise SystemExit(f"config not found: {cfg_path}")
        self.cfg = yaml.safe_load(Path(cfg_path).read_text()) or {}

        db_cfg = self.cfg.get("database") or {}
        url_env = db_cfg.get("url_env", "DATABASE_URL")
        dsn = os.environ.get(url_env, "")
        if not dsn:
            raise SystemExit(
                f"{url_env} env var not set — required by site.yaml database.url_env"
            )
        self.dsn = dsn
        self.site_id_filter = db_cfg.get("site_id_filter") or None

        if not PROMPT_PATH.is_file():
            raise SystemExit(f"system prompt not found: {PROMPT_PATH}")
        self.system_prompt = PROMPT_PATH.read_text()

        # Local run dir for human inspection (parallels framework storage)
        runs_root = Path(self.cfg.get("runs_root") or
                         "~/.reusable-agents/product-hydration-agent/runs"
                         ).expanduser()
        site_id = (self.cfg.get("site") or {}).get("id", "default")
        self.run_dir = runs_root / site_id / self.run_ts
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.decide("setup",
                    f"agent_id={self.agent_id} site={site_id} "
                    f"site_id_filter={self.site_id_filter} run_dir={self.run_dir}")

    def run(self) -> RunResult:
        cfg = self.cfg
        hyd = cfg.get("hydration") or {}
        content_types = list(hyd.get("content_types") or
                              ["description", "pros_cons", "faq", "seo_meta"])
        # Validate content_types against the spec
        unknown = [ct for ct in content_types if ct not in CONTENT_SPEC]
        if unknown:
            raise SystemExit(f"unknown content_types in site.yaml: {unknown}")

        batch_size = int(hyd.get("batch_size", 25))
        max_runtime_minutes = int(hyd.get("max_runtime_minutes", 30))
        stale_after_days = int(hyd.get("stale_after_days", 90))
        priority_column = hyd.get("selection_priority", "review_count")
        if priority_column not in {"review_count", "rating", "sales_rank",
                                    "price", "id"}:
            priority_column = "review_count"

        claude_cfg = cfg.get("claude") or {}
        model = claude_cfg.get("model", "opus")
        max_turns = int(claude_cfg.get("max_turns", 10))
        per_call_timeout = int(claude_cfg.get("per_call_timeout_s", 300))

        deadline = time.time() + max_runtime_minutes * 60

        self.status("connecting to db", progress=0.05, current_action="db-connect")
        try:
            conn = _connect(self.dsn)
        except Exception as e:
            self.decide("error", f"db connect failed: {e}")
            notify_operator(
                agent_id=self.agent_id, error=e,
                context={"phase": "db-connect"},
                severity="high",
            )
            return RunResult(status="failure",
                              summary=f"db connect failed: {e}", metrics={})

        # ---- Phase 0: Amazon price refresh (provider: brightdata|paapi) -----
        price_refresh_cfg = (cfg.get("price_refresh") or {})
        # Backward compat: if cfg.amazon_paapi exists with .enabled, treat
        # as "paapi" provider.
        paapi_block = (cfg.get("amazon_paapi") or {})
        if not price_refresh_cfg and paapi_block.get("enabled"):
            price_refresh_cfg = {**paapi_block, "provider": "paapi"}
        provider = (price_refresh_cfg.get("provider") or "").lower()
        paapi_summary: dict = {
            "provider": provider or "none",
            "refreshed": 0, "failed": 0, "processed": 0,
            "batches": 0, "asins_seen": 0,
            "compliance_pass": 0, "compliance_fail": 0,
            "skipped_reason": None,
        }
        if not provider or provider == "none":
            paapi_summary["skipped_reason"] = "price_refresh.provider not set"
            self.decide("observation", paapi_summary["skipped_reason"])
        else:
            paapi_cfg = PaapiConfig.from_env(
                access_key_env=price_refresh_cfg.get("paapi_access_key_env",
                    paapi_block.get("access_key_env", "AMAZON_PAAPI_ACCESS_KEY")),
                secret_key_env=price_refresh_cfg.get("paapi_secret_key_env",
                    paapi_block.get("secret_key_env", "AMAZON_PAAPI_SECRET_KEY")),
                associate_tag_env=price_refresh_cfg.get("paapi_associate_tag_env",
                    paapi_block.get("associate_tag_env", "AMAZON_PAAPI_ASSOCIATE_TAG")),
                associate_tag=price_refresh_cfg.get("associate_tag",
                    paapi_block.get("associate_tag")),
                region=price_refresh_cfg.get("paapi_region",
                    paapi_block.get("region", "us-east-1")),
                marketplace=price_refresh_cfg.get("marketplace",
                    paapi_block.get("marketplace", "www.amazon.com")),
                throttle_per_second=float(price_refresh_cfg.get(
                    "paapi_throttle_per_second",
                    paapi_block.get("throttle_per_second", 1.0))),
            )
            bd_key = os.environ.get(
                price_refresh_cfg.get("brightdata_api_key_env", "BRIGHTDATA_API_KEY"),
                "").strip()
            bd_cfg = (BrightDataConfig(
                api_key=bd_key,
                poll_interval_s=int(price_refresh_cfg.get(
                    "brightdata_poll_interval_s", 10)),
                poll_timeout_s=int(price_refresh_cfg.get(
                    "brightdata_poll_timeout_s", 600)),
            ) if bd_key else None)

            self.status(f"refreshing Amazon prices via {provider}",
                         progress=0.05, current_action=f"{provider}-refresh")
            try:
                paapi_summary.update(_refresh_amazon_prices(
                    conn=conn, raw_cfg=price_refresh_cfg,
                    paapi_cfg=paapi_cfg, bd_cfg=bd_cfg,
                    provider=provider,
                    site_id_filter=self.site_id_filter,
                    status_cb=self.status, decide_cb=self.decide,
                    agent_id=self.agent_id,
                    site=str((cfg.get("site") or {}).get("id") or ""),
                    storage=getattr(self, "storage", None),
                ))
                self.decide(
                    "action",
                    f"{provider}: refreshed {paapi_summary['refreshed']}"
                    + (f" ({paapi_summary['unpriced']} without a valid offer -> price cleared)"
                       if paapi_summary.get("unpriced") else "")
                    + (f", {paapi_summary['inaccessible']} not accessible -> price cleared"
                       if paapi_summary.get("inaccessible") else "")
                    + f", failed {paapi_summary['failed']}, "
                    f"compliance {paapi_summary['compliance_pass']}/"
                    f"{paapi_summary['compliance_pass'] + paapi_summary['compliance_fail']} pass"
                    + (f", est cost ${paapi_summary.get('estimated_cost_usd', 0):.4f}"
                       if 'estimated_cost_usd' in paapi_summary else "")
                )
            except Exception as e:
                paapi_summary["skipped_reason"] = f"refresh raised: {e}"
                self.decide("error",
                             f"{provider} refresh raised: {str(e)[:200]}")

        totals = {
            "queued": 0,
            "hydrated": 0,
            "failed": 0,
            "partial": 0,
            "skipped_already_fresh": 0,
            "claude_calls": 0,
            "claude_total_seconds": 0.0,
            "deferred_pool_reserve": 0,
            "prices_refreshed": paapi_summary["refreshed"],
            "prices_failed": paapi_summary["failed"],
            "compliance_pass": paapi_summary["compliance_pass"],
            "compliance_fail": paapi_summary["compliance_fail"],
        }
        per_type_counts = {ct: 0 for ct in content_types}
        per_type_failures = {ct: 0 for ct in content_types}
        successes: list[dict] = []
        failures: list[dict] = []
        log_path = self.run_dir / "hydration-log.jsonl"
        sample_outputs: list[str] = []  # capped at ~50 KB
        sample_budget = 50_000

        log_f = log_path.open("w")
        try:
            self.status("selecting candidates", progress=0.10,
                        current_action=f"batch_size={batch_size}")
            # Defensive rollback: if anything earlier in this run (price
            # refresh batches especially) hit a SQL error and the inner
            # except didn't roll back, the connection is in an aborted-
            # transaction state and EVERY query fails with "current
            # transaction is aborted, commands ignored until end of
            # transaction block." Clearing it here makes the rest of
            # the run robust to upstream bugs we haven't found yet.
            try:
                conn.rollback()
            except Exception:
                pass
            prio_asins, prio_err = _priority_asins(conn, hyd.get("priority_asins_sql"))
            if prio_err:
                self.decide("observation", f"priority_asins_sql failed, using {priority_column} order: {prio_err}")
            elif prio_asins:
                self.decide("observation", f"{len(prio_asins)} priority ASIN(s) from priority_asins_sql are selected first")
            try:
                queue = _select_candidates(
                    conn,
                    content_types=content_types,
                    batch_size=batch_size,
                    stale_after_days=stale_after_days,
                    site_id_filter=self.site_id_filter,
                    priority_column=priority_column,
                    priority_asins=prio_asins,
                )
            except Exception as e:
                self.decide("error", f"candidate selection failed: {e}")
                notify_operator(
                    agent_id=self.agent_id, error=e,
                    context={"phase": "select-candidates"},
                    severity="high",
                )
                conn.close()
                return RunResult(status="failure",
                                  summary=f"candidate selection failed: {e}",
                                  metrics=totals)
            totals["queued"] = len(queue)
            self.decide("observation", f"queued {len(queue)} products for hydration")
            if not queue:
                coverage = _coverage_stats(
                    conn, content_types=content_types,
                    site_id_filter=self.site_id_filter,
                    stale_after_days=stale_after_days,
                )
                self._write_artifacts(
                    cfg=cfg, totals=totals, per_type_counts=per_type_counts,
                    per_type_failures=per_type_failures, coverage=coverage,
                    successes=successes, failures=failures, model=model,
                    sample_outputs=sample_outputs,
                )
                conn.close()
                return RunResult(
                    status="success",
                    summary="queue empty — catalog already fully hydrated within freshness window",
                    metrics={**totals, "catalog_coverage_pct":
                             coverage.get("fully_hydrated_pct", 0.0)},
                )

            for i, prod in enumerate(queue):
                if not _pool_admits(model):
                    # Leave the pool's last profiles to authoring; the rest of
                    # the queue waits for the next run (not a failure).
                    totals["deferred_pool_reserve"] = len(queue) - i
                    self.decide("observation",
                                f"claude-pool reserve reached - deferring {len(queue) - i} products")
                    break
                if time.time() > deadline:
                    self.decide(
                        "observation",
                        f"runtime budget reached ({max_runtime_minutes}m) — "
                        f"stopping at {i}/{len(queue)}",
                    )
                    break

                progress = 0.15 + 0.75 * (i / max(1, len(queue)))
                title_short = (prod.get("title") or "")[:60]
                self.status(f"hydrating {prod['asin']}: {title_short}",
                            progress=progress,
                            current_action=f"{i+1}/{len(queue)}")

                # Determine which content_types ACTUALLY need work for this
                # product. If hydrated_at is fresh AND all columns populated,
                # skip entirely (defensive — selection should already exclude).
                needs: list[str] = []
                for ct in content_types:
                    col, _, _ = CONTENT_SPEC[ct]
                    if prod.get(col) is None:
                        needs.append(ct)
                hydrated_at = prod.get("hydrated_at")
                stale = (hydrated_at is None or
                         (datetime.now(timezone.utc) - hydrated_at.replace(tzinfo=timezone.utc)
                          if hydrated_at.tzinfo is None else
                          datetime.now(timezone.utc) - hydrated_at)
                         > timedelta(days=stale_after_days))
                if not needs and not stale:
                    totals["skipped_already_fresh"] += 1
                    continue
                if stale and not needs:
                    # All columns populated but stale — refresh everything
                    needs = list(content_types)

                user_prompt = _build_user_prompt(prod, needs)
                try:
                    raw, meta = _claude_call(
                        system_prompt=self.system_prompt,
                        user_prompt=user_prompt,
                        model=model, max_turns=max_turns,
                        timeout_s=per_call_timeout,
                    )
                    totals["claude_calls"] += 1
                    totals["claude_total_seconds"] += meta["wall_clock_s"]
                    if sample_budget > 0:
                        chunk = (
                            f"\n\n=== ASIN {prod['asin']} (rc={meta['rc']}, "
                            f"{meta['wall_clock_s']:.1f}s) ===\n{raw[:4000]}"
                        )
                        sample_outputs.append(chunk)
                        sample_budget -= len(chunk)
                    parsed = _parse_claude_json(raw)
                    if not parsed:
                        raise RuntimeError("claude returned non-JSON or empty output")
                except Exception as e:
                    totals["failed"] += 1
                    failures.append({
                        "product_id": prod["id"],
                        "asin": prod["asin"],
                        "title": prod.get("title") or "",
                        "error": str(e)[:500],
                    })
                    self.decide("error",
                                f"{prod['asin']}: {type(e).__name__}: {str(e)[:160]}")
                    log_f.write(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "asin": prod["asin"],
                        "status": "failed",
                        "error": str(e)[:500],
                    }) + "\n")
                    log_f.flush()
                    continue

                # Validate per-type and build update map
                updates: dict[str, str | None] = {}
                emitted_fields: list[str] = []
                this_failures: list[str] = []
                for ct in needs:
                    _, validator, _ = CONTENT_SPEC[ct]
                    val = validator(parsed.get(ct))
                    if val is None:
                        per_type_failures[ct] += 1
                        this_failures.append(ct)
                    else:
                        updates[ct] = val
                        per_type_counts[ct] += 1
                        emitted_fields.append(ct)

                if not updates:
                    totals["failed"] += 1
                    failures.append({
                        "product_id": prod["id"],
                        "asin": prod["asin"],
                        "title": prod.get("title") or "",
                        "error": f"all requested types failed validation: {this_failures}",
                    })
                    log_f.write(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "asin": prod["asin"],
                        "status": "validation_failed",
                        "validation_failures": this_failures,
                    }) + "\n")
                    log_f.flush()
                    continue

                # Persist
                try:
                    conn = _reopen(conn, self.dsn)
                    _persist_product(conn, prod["id"], updates,
                                      f"claude-{model}")
                except Exception as e:
                    totals["failed"] += 1
                    failures.append({
                        "product_id": prod["id"],
                        "asin": prod["asin"],
                        "title": prod.get("title") or "",
                        "error": f"db write failed: {e}",
                    })
                    self.decide("error", f"{prod['asin']}: db write failed: {e}")
                    log_f.write(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "asin": prod["asin"],
                        "status": "db_write_failed",
                        "error": str(e)[:500],
                    }) + "\n")
                    log_f.flush()
                    continue

                if this_failures:
                    totals["partial"] += 1
                else:
                    totals["hydrated"] += 1
                successes.append({
                    "product_id": prod["id"],
                    "asin": prod["asin"],
                    "title": prod.get("title") or "",
                    "fields": emitted_fields,
                    "wall_clock_s": meta["wall_clock_s"],
                })
                log_f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "asin": prod["asin"],
                    "status": "partial" if this_failures else "success",
                    "fields_written": emitted_fields,
                    "validation_failures": this_failures,
                    "claude_seconds": meta["wall_clock_s"],
                }) + "\n")
                log_f.flush()
                self.decide("action",
                            f"{prod['asin']}: hydrated {len(emitted_fields)}/"
                            f"{len(needs)} fields ({','.join(emitted_fields)})")
        finally:
            log_f.close()

        # ---- Phase 2: refresh featured-products curation -----------
        # Every tick recomputes the is_featured set so it reflects the
        # freshest price/rating/review_count. The script lives in the
        # site-specific agent dir (since the focus areas are site-
        # specific); we look it up via env var so reusable-agents stays
        # site-agnostic.
        featured_summary: dict = {"executed": False, "skipped_reason": None}
        featured_script = os.environ.get("FEATURED_SELECT_SCRIPT", "").strip()
        if featured_script and Path(featured_script).is_file():
            try:
                self.status("refreshing featured-products set",
                             progress=0.95,
                             current_action="select-featured")
                proc = subprocess.run(
                    [sys.executable, featured_script],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ, "DATABASE_URL": self.dsn},
                )
                featured_summary["executed"] = True
                featured_summary["rc"] = proc.returncode
                if proc.returncode == 0:
                    try:
                        featured_summary["result"] = json.loads(proc.stdout.strip())
                    except Exception:
                        featured_summary["result_raw"] = proc.stdout[:500]
                    self.decide("action",
                                 "featured set refreshed: "
                                 + (proc.stdout.strip().splitlines()[-1]
                                    if proc.stdout.strip() else "ok"))
                else:
                    featured_summary["error"] = (proc.stderr or proc.stdout)[:500]
                    self.decide("error",
                                 f"featured-select rc={proc.returncode}: "
                                 f"{(proc.stderr or proc.stdout)[:200]}")
            except Exception as e:
                featured_summary["error"] = str(e)[:500]
                self.decide("error",
                             f"featured-select raised: {str(e)[:200]}")
        else:
            featured_summary["skipped_reason"] = (
                "FEATURED_SELECT_SCRIPT env var not set" if not featured_script
                else f"script not found: {featured_script}"
            )

        # Compute final coverage
        try:
            conn = _reopen(conn, self.dsn)
            coverage = _coverage_stats(
                conn, content_types=content_types,
                site_id_filter=self.site_id_filter,
                stale_after_days=stale_after_days,
            )
        except Exception as e:
            self.decide("error", f"coverage stats failed: {e}")
            coverage = {"error": str(e)[:200]}
        try:
            conn.close()
        except Exception:
            pass

        # Artifacts
        self._write_artifacts(
            cfg=cfg, totals=totals, per_type_counts=per_type_counts,
            per_type_failures=per_type_failures, coverage=coverage,
            successes=successes, failures=failures, model=model,
            sample_outputs=sample_outputs, paapi_summary=paapi_summary,
        )

        # Email
        try:
            sent, detail = _maybe_send_digest(
                cfg=cfg, agent_id=self.agent_id, run_ts=self.run_ts,
                totals=totals, coverage=coverage,
                failures=failures, successes=successes,
            )
            if sent:
                self.decide("action", f"emailed digest: {detail}")
            else:
                self.decide("observation", f"email skipped: {detail}")
        except Exception as e:
            self.decide("error", f"email send failed: {e}")

        # Status / RunResult
        status = "success"
        if totals["failed"] > 0 and totals["hydrated"] == 0 and totals["partial"] == 0:
            status = "failure"
            err = RuntimeError(f"all {totals['failed']} products failed")
            notify_operator(
                agent_id=self.agent_id, error=err,
                context={"phase": "hydration", "totals": totals,
                         "first_failures": failures[:3]},
                severity="high",
            )
        elif totals["failed"] > 0:
            status = "partial_failure"

        summary = (
            f"Hydrated {totals['hydrated']} (+{totals['partial']} partial), "
            f"{totals['failed']} failed, {totals['skipped_already_fresh']} fresh"
            + (f", {totals['deferred_pool_reserve']} deferred (claude-pool reserve)"
               if totals["deferred_pool_reserve"] else "") + ". "
            f"Refreshed {totals['prices_refreshed']} Amazon prices via PA-API "
            f"(failed {totals['prices_failed']}). "
            f"Catalog coverage: {coverage.get('fully_hydrated_pct', 0.0)}% fully hydrated."
        )
        return RunResult(
            status=status,
            summary=summary,
            metrics={
                "total_products": totals["queued"],
                "hydrated": totals["hydrated"],
                "partial": totals["partial"],
                "failed": totals["failed"],
                "skipped_already_fresh": totals["skipped_already_fresh"],
                "claude_calls": totals["claude_calls"],
                "claude_total_seconds": round(totals["claude_total_seconds"], 1),
                "deferred_pool_reserve": totals["deferred_pool_reserve"],
                "catalog_coverage_pct": coverage.get("fully_hydrated_pct", 0.0),
                "stale_pct": coverage.get("stale_pct", 0.0),
                "prices_refreshed": totals["prices_refreshed"],
                "prices_failed": totals["prices_failed"],
                "amazon_paapi_batches": paapi_summary.get("batches", 0),
                "amazon_paapi_skipped_reason": paapi_summary.get("skipped_reason"),
                **_price_outcome_metrics(paapi_summary),
                "compliance_pass": totals["compliance_pass"],
                "compliance_fail": totals["compliance_fail"],
                **{f"per_type_{ct}": n for ct, n in per_type_counts.items()},
                **{f"per_type_failed_{ct}": n for ct, n in per_type_failures.items()},
            },
            next_state={"last_run_ts": self.run_ts,
                        "site_id_filter": self.site_id_filter},
        )

    # ----- Artifacts -----

    def _write_artifacts(self, *, cfg: dict, totals: dict,
                          per_type_counts: dict, per_type_failures: dict,
                          coverage: dict, successes: list, failures: list,
                          model: str, sample_outputs: list[str],
                          paapi_summary: dict | None = None) -> None:
        run_prefix = f"agents/{self.agent_id}/runs/{self.run_ts}"

        results = {
            "schema_version": "1",
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "site": (cfg.get("site") or {}).get("id"),
            "ai_provider": "claude-cli",
            "ai_model": model,
            "totals": totals,
            "per_content_type_success": per_type_counts,
            "per_content_type_failures": per_type_failures,
            "success_rate": (
                round(100.0 * (totals["hydrated"] + totals["partial"]) /
                      max(1, totals["queued"] - totals["skipped_already_fresh"]), 2)
                if (totals["queued"] - totals["skipped_already_fresh"]) > 0 else 0.0
            ),
            "avg_seconds_per_product": (
                round(totals["claude_total_seconds"] / totals["claude_calls"], 2)
                if totals["claude_calls"] > 0 else 0.0
            ),
            "coverage": coverage,
            "amazon_paapi": paapi_summary or {},
        }
        self._save("results.json", results, run_prefix)

        # goal-progress.json — narrow view aligned with site.yaml.goals
        goals_block = cfg.get("goals") or {}
        ps = paapi_summary or {}
        compliance_total = ps.get("compliance_pass", 0) + ps.get("compliance_fail", 0)
        compliance_pct = (round(100.0 * ps.get("compliance_pass", 0)
                                / compliance_total, 2)
                          if compliance_total > 0 else None)
        goal_progress = {
            "schema_version": "1",
            "run_ts": self.run_ts,
            "primary": goals_block.get("primary"),
            "freshness": goals_block.get("freshness"),
            "content_freshness": goals_block.get("content_freshness"),
            "quality": goals_block.get("quality"),
            "amazon_compliance": goals_block.get("amazon_compliance"),
            "google_compliance": goals_block.get("google_compliance"),
            "current": {
                "fully_hydrated_pct": coverage.get("fully_hydrated_pct"),
                "stale_pct": coverage.get("stale_pct"),
                "per_content_type_pct": {
                    ct: v["pct"] for ct, v in
                    (coverage.get("per_content_type") or {}).items()
                },
                "prices_refreshed_this_run": ps.get("refreshed", 0),
                "prices_failed_this_run": ps.get("failed", 0),
                "amazon_paapi_skipped_reason": ps.get("skipped_reason"),
                "compliance_pct_this_run": compliance_pct,
            },
        }
        self._save("goal-progress.json", goal_progress, run_prefix)

        # llm-output.txt — sample, capped
        sample_text = "".join(sample_outputs)[:50_000]
        self._save("llm-output.txt", sample_text, run_prefix)

        # context-summary.md — narrative for the next run
        ps = paapi_summary or {}
        ctx_md = (
            f"# Hydration run {self.run_ts}\n\n"
            f"Site: **{(cfg.get('site') or {}).get('id')}**  Model: `{model}`\n\n"
            f"- Queued: {totals['queued']}\n"
            f"- Hydrated: {totals['hydrated']} (+{totals['partial']} partial)\n"
            f"- Failed: {totals['failed']}\n"
            f"- Skipped (already fresh): {totals['skipped_already_fresh']}\n"
            f"- Claude calls: {totals['claude_calls']} "
            f"(total {totals['claude_total_seconds']:.0f}s, "
            f"avg {(totals['claude_total_seconds']/max(1,totals['claude_calls'])):.1f}s)\n\n"
            f"## Amazon PA-API price refresh\n\n"
            f"- Refreshed: {ps.get('refreshed', 0)}\n"
            f"- Failed: {ps.get('failed', 0)}\n"
            f"- Batches: {ps.get('batches', 0)}\n"
            f"- Compliance pass: {ps.get('compliance_pass', 0)} / "
            f"{ps.get('compliance_pass', 0) + ps.get('compliance_fail', 0)}\n"
            + (f"- Skipped: {ps['skipped_reason']}\n"
               if ps.get('skipped_reason') else "")
            + "\n## Catalog coverage\n\n"
            f"- Fully hydrated: {coverage.get('fully_hydrated')} "
            f"({coverage.get('fully_hydrated_pct')}%)\n"
            f"- Stale: {coverage.get('stale_count')} "
            f"({coverage.get('stale_pct')}%)\n\n"
        )
        ctx_md += "## Per-content-type populated %\n\n"
        for ct, v in (coverage.get("per_content_type") or {}).items():
            ctx_md += f"- **{ct}**: {v['populated']} ({v['pct']}%)\n"
        if failures:
            ctx_md += f"\n## Recent failures (first 10)\n\n"
            for f in failures[:10]:
                ctx_md += f"- `{f['asin']}` — {f['error'][:200]}\n"
        self._save("context-summary.md", ctx_md, run_prefix)

        # hydration-log.jsonl was streamed during the run; lift the local
        # copy into framework storage so the dashboard's Runs tab shows it.
        log_path = self.run_dir / "hydration-log.jsonl"
        if log_path.is_file():
            try:
                self.storage.write_text(
                    f"{run_prefix}/hydration-log.jsonl",
                    log_path.read_text(),
                )
            except Exception as e:
                self.decide("error", f"failed to upload hydration-log.jsonl: {e}")

    def _save(self, name: str, content, run_prefix: str) -> None:
        """Mirror helper from progressive-improvement-agent — writes to
        BOTH the local run dir (operator inspection) and framework storage
        (dashboard drill-down)."""
        disk = self.run_dir / name
        if isinstance(content, (dict, list)):
            disk.write_text(json.dumps(content, indent=2))
            try:
                self.storage.write_json(f"{run_prefix}/{name}", content)
            except Exception as e:
                self.decide("error", f"storage write failed for {name}: {e}")
        else:
            text = str(content)
            disk.write_text(text)
            try:
                self.storage.write_text(f"{run_prefix}/{name}", text)
            except Exception as e:
                self.decide("error", f"storage write failed for {name}: {e}")


if __name__ == "__main__":
    ProductHydrationAgent().run_once()
