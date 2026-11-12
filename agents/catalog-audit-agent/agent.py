#!/usr/bin/env python3
"""Catalog Audit Agent — generic blueprint for site-catalog quality audits.

Wraps an existing per-site audit script (typescript / shell) and converts
its findings into the standard reusable-agents recommendations.json schema
so the responder + implementer flow can apply fixes via email reply.

Configuration (via env-pointed YAML):
  CATALOG_AUDIT_CONFIG=/path/to/site.yaml

Site config (yaml) shape:
  site_id: aisleprompt
  what_we_do: "AI grocery / meal-planning catalog (recipes + kitchen products)"
  audit:
    command:
      - bash
      - "-c"
      - |
        cd "/path/to/site/repo" && \\
        DATABASE_URL="..." npx tsx scripts/catalog-quality-audit.ts --no-email
    findings_path: "/path/to/site/repo/audit-history"  # latest YYYY-MM-DD.json
    findings_format: aisleprompt-catalog-audit  # or 'specpicks-image-csv'
  reporter:
    email:
      from: "AislePrompt Audit <automation@northernsoftwareconsulting.com>"
      to: ["mperry@northernsoftwareconsulting.com"]
      subject_template: "[{agent_id}:{request_id}] Catalog Audit — {site} — {tag}"

Inherits all framework lessons from PI/CR work:
  - sys.path = parent.parent.parent (repo root, not agents/)
  - self.agent_id (per-site) for email subject + outbound metadata
  - send_via_msmtp uses Graph internally (msmtp is sandboxed by AppArmor)
  - Outbound metadata at agents/<agent_id>/outbound-emails/<request_id>.json
  - Responder routes [<agent_id>:<request_id>] subject tags to implementer
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# After agents/ consolidation: this file is at agents/catalog-audit-agent/,
# so the repo root is THREE levels up.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.email_codes import new_request_id  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

from shared.site_quality import (  # noqa: E402
    apply_user_responses,
    assign_rec_ids,
    dispatch_auto_recs,
    load_quality_config,
    render_recs_email,
    score_tier,
    send_via_msmtp,
    validate_recs_doc,
)


AGENT_ID = "catalog-audit-agent"


# ---------------------------------------------------------------------------
# Findings → recs converters
# ---------------------------------------------------------------------------

# Severity mapping from audit script vocabulary → standard schema vocabulary
_SEVERITY_MAP = {
    "critical": "critical", "fatal": "critical",
    "warn": "high", "warning": "high",
    "info": "medium", "notice": "medium",
    "low": "low",
}


# Per-criterion migration templates. The implementer (LLM or
# deterministic substitution) uses these instead of re-deriving SQL
# from the audit script — small, structured, low-context. {ids}
# expands to the comma-joined ref_ids from rec.evidence.
#
# Template KEYS:
#   table       — target table for the UPDATE
#   action      — short human label
#   sql         — SQL with {ids} placeholder. WHERE clauses MUST be
#                 idempotent (e.g. include is_active=true) so re-runs
#                 are safe.
#   filename    — base name (no ts prefix) for the migration file
_AISLEPROMPT_TEMPLATES: dict[str, dict] = {
    "recipe-image-present": {
        "table": "recipe_catalog",
        "action": "deactivate recipes with no image — backfill regenerates if scrapers find one later",
        "sql": (
            "UPDATE recipe_catalog\n"
            "SET is_active = false\n"
            "WHERE is_active = true\n"
            "  AND (image_url IS NULL OR image_url = '')\n"
            "  AND id IN ({ids});"
        ),
        "filename": "catalog-audit-recipe-image-present",
    },
    "recipe-image-valid-url": {
        "table": "recipe_catalog",
        "action": "null broken image_url so backfill re-fetches",
        "sql": (
            "UPDATE recipe_catalog\n"
            "SET image_url = NULL\n"
            "WHERE id IN ({ids})\n"
            "  AND is_active = true;"
        ),
        "filename": "catalog-audit-recipe-image-valid-url",
    },
    "recipe-nutrition-sanity": {
        "table": "recipe_catalog",
        "action": "null implausible nutrition so enrichment regenerates",
        "sql": (
            "UPDATE recipe_catalog\n"
            "SET calories = NULL, protein = NULL, carbs = NULL, fat = NULL\n"
            "WHERE id IN ({ids})\n"
            "  AND is_active = true;"
        ),
        "filename": "catalog-audit-recipe-nutrition-sanity",
    },
    "recipe-duplicate-source-url": {
        "table": "recipe_catalog",
        "action": "deactivate duplicates pointing at non-recipe domains",
        "sql": (
            "UPDATE recipe_catalog\n"
            "SET is_active = false\n"
            "WHERE id IN ({ids})\n"
            "  AND is_active = true;"
        ),
        "filename": "catalog-audit-recipe-duplicate-source-url",
    },
    "product-image-present": {
        "table": "kitchen_products",
        "action": "deactivate products with no image",
        "sql": (
            "UPDATE kitchen_products\n"
            "SET is_active = false\n"
            "WHERE is_active = true\n"
            "  AND (primary_image_url IS NULL OR primary_image_url = '')\n"
            "  AND id IN ({ids});"
        ),
        "filename": "catalog-audit-product-image-present",
    },
    "product-schema-rich-results": {
        "table": "kitchen_products",
        "action": "flag products missing structured-data fields for manual review",
        "sql": (
            "-- Manual review required; structured-data field inference\n"
            "-- isn't safe to automate. Listed ids: {ids}\n"
            "-- (no UPDATE — write a notes row in product_review_queue\n"
            "--  if that table exists, otherwise leave as DEFERRED.)"
        ),
        "filename": "catalog-audit-product-schema-rich-results",
    },
}


def _findings_to_recs_aisleprompt(findings_doc: dict, max_recs: int = 30) -> list[dict]:
    """Convert aisleprompt catalog-quality-audit.ts output → recs.

    Recs include a `migration_template` field per criterion so the
    implementer can produce SQL with deterministic substitution. Recs
    with no usable sample are skipped — un-actionable, would just waste
    LLM tokens.
    """
    findings = findings_doc.get("findings", [])
    recs: list[dict] = []
    skipped_no_sample = 0
    for f in findings:
        count = int(f.get("count", 0))
        if count == 0:
            continue
        crit = f.get("criterionId", "unknown")
        sev = _SEVERITY_MAP.get(f.get("severity", "low"), "low")
        sample = f.get("sample", []) or []
        ref_ids: list[str] = []
        evidence: list[dict] = []
        for s in sample[:5]:
            if isinstance(s, dict) and s.get("id") not in (None, ""):
                rid = str(s.get("id"))
                ref_ids.append(rid)
                evidence.append({
                    "ref_id": rid,
                    "snippet": (s.get("title") or s.get("detail") or "")[:160],
                })

        # Skip recs with no concrete row IDs — the implementer can't act
        # on them and we don't want to flood the dispatch queue with
        # un-actionable work. The audit will keep flagging them on the
        # next run; if the script is updated to capture samples for the
        # criterion, it'll start emitting actionable recs.
        if not ref_ids:
            skipped_no_sample += 1
            continue

        # Heuristic confidence: high when the audit caught it directly,
        # lower when it's a sampled subset (image url checks).
        conf = 0.95 if "valid-url" not in crit else 0.85

        goal_ids = []
        if "miscategor" in crit or "category-in-allow" in crit or "categor" in crit:
            goal_ids.append("goal-zero-miscategorized-products")
        if "image" in crit:
            goal_ids.append("goal-image-quality")
        if "duplicate" in crit:
            goal_ids.append("goal-zero-duplicate-content")
        if "title" in crit or "instructions" in crit or "ingredients" in crit:
            goal_ids.append("goal-content-completeness")

        title = f"Fix {count} {crit.replace('-', ' ')} issue(s)"
        rec = {
            "category": crit,
            "check_id": crit,
            "severity": sev,
            "confidence": conf,
            "tier": score_tier(confidence=conf, severity=sev, threshold=0.92),
            "title": title[:160],
            "rationale": f"Catalog audit found {count} rows failing the '{crit}' criterion. Sample evidence (first 5): {', '.join(ref_ids)}.",
            "evidence": evidence,
            # Also surface the comma-joined IDs at top level so a
            # deterministic substituter doesn't have to walk the
            # evidence list — and so an LLM sees the IDs upfront.
            "ref_ids": ref_ids,
            "implemented": False,
            "goal_ids": goal_ids,
        }
        # Attach migration template when we know the criterion. The
        # implementer prompt + deterministic path both read this.
        tmpl = _AISLEPROMPT_TEMPLATES.get(crit)
        if tmpl:
            rec["migration_template"] = {
                **tmpl,
                "sql_with_ids": tmpl["sql"].replace("{ids}", ", ".join(ref_ids)),
            }
            rec["implementation_outline"] = {
                "approach": (
                    f"Apply migration_template.sql_with_ids "
                    f"(criterion={crit}, table={tmpl['table']}, "
                    f"action={tmpl['action']}). Write the SQL to "
                    f"db/migrations/<UTC-ts>_{tmpl['filename']}.sql."
                ),
            }
            # Going-forward: surface the public URL each affected row
            # corresponds to so the dashboard can show "what got fixed"
            # without making the user click through. Maps the table name
            # to its canonical public URL pattern for AislePrompt:
            #   recipe_catalog → /recipes/<id>
            #   kitchen_products → /products/<id>
            # If a future table doesn't have a public surface, leave
            # affected_urls unset so the UI just shows the IDs.
            URL_PATTERNS = {
                "recipe_catalog": "https://aisleprompt.com/recipes/{id}",
                "kitchen_products": "https://aisleprompt.com/products/{id}",
            }
            pat = URL_PATTERNS.get(tmpl["table"])
            if pat:
                rec["affected_urls"] = [pat.format(id=rid) for rid in ref_ids[:20]]
        else:
            rec["implementation_outline"] = {
                "approach": (
                    f"Unknown criterion '{crit}' — no template registered. "
                    f"Read scripts/catalog-quality-audit.ts to understand "
                    f"the check, then write a SQL migration that targets "
                    f"the listed ref_ids. Default shape: deactivate via "
                    f"`is_active=false` for content quality failures, or "
                    f"NULL the offending column for backfill regeneration."
                ),
            }
        recs.append(rec)
    if skipped_no_sample:
        # Logged so the operator can see how many criteria are being
        # silently dropped because the audit script doesn't capture a
        # sample for them. (Fix: update catalog-quality-audit.ts to
        # populate `sample` for that criterion's check query.)
        import sys as _sys
        print(
            f"[catalog-audit-recs] skipped {skipped_no_sample} criteria "
            f"with empty sample (no actionable row IDs)",
            file=_sys.stderr,
        )
    return recs[:max_recs]


IMAGE_NAME_CHECK = "product-image-matches-name"


def _findings_to_recs_specpicks_images(findings_doc: dict, max_recs: int = 30) -> list[dict]:
    """Convert an audit-product-images CSV → recs (one per mismatch).

    Each rec keeps the raw finding under `finding` and names its row in
    `ref_ids` (the CSV key, normally the slug), so the image/name rule
    (apply_image_name_rule) can triage it and the remedy guards can see which
    row it targets. Without `ref_ids` the guards dropped every such rec.

    A slug is not always unique. When the CSV has an `id` column (the row id
    recs and migrations use), the rule matches the finding to that row;
    otherwise it matches on the image URL, then the name, and reports rather
    than deactivates when that does not single out one row.

    The outline written here never instructs a deactivation: until the rule
    has triaged a rec (identity_rule), nothing in it may be acted on, and the
    framework rec contract (framework/core/rec_contract.py) holds such a rec
    if one ever reaches the dispatcher."""
    rows = findings_doc.get("rows", [])
    mismatches = [r for r in rows if r.get("verdict") == "mismatch"]
    recs: list[dict] = []
    for m in mismatches[:max_recs]:
        key = str(m.get("slug") or "").strip()
        recs.append({
            "category": "image-name-mismatch",
            "check_id": IMAGE_NAME_CHECK,
            "severity": "high",
            "confidence": 0.9,
            "tier": "review",
            "title": f"Image mismatch: {(m.get('name') or '')[:120]}",
            "rationale": (m.get("reason") or "")[:240],
            "evidence": [{
                "ref_id": key,
                "snippet": f"image_url: {m.get('image_url','')[:160]}",
            }],
            "ref_ids": [key] if key else [],
            "finding": {
                "key": key,
                # Optional: the row id, when the audit emits it. It pins the
                # finding to one row when the key (slug) is shared.
                "id": str(m.get("id") or "").strip()[:128],
                "name": (m.get("name") or "")[:300],
                "reason": (m.get("reason") or "")[:600],
                "image_url": (m.get("image_url") or "")[:500],
                # Optional columns a classifier may emit; the rule prefers
                # them over reading the free-text reason.
                "basis": (m.get("basis") or "").strip().lower(),
                "brand": (m.get("brand") or "").strip(),
                "manufacturer": (m.get("manufacturer") or "").strip(),
            },
            "implementation_outline": {
                "approach": (
                    "NOT TRIAGED. The image/name rule has not classified this "
                    "finding (no identity_rule), so do not change any row for "
                    "it: mark it deferred."
                ),
            },
            "implemented": False,
            "goal_ids": ["goal-image-quality"],
        })
    return recs


CONVERTERS = {
    "aisleprompt-catalog-audit": _findings_to_recs_aisleprompt,
    "specpicks-image-csv": _findings_to_recs_specpicks_images,
}


# ---------------------------------------------------------------------------
# Remedy guards (SHOP-20, shop audit 2026-11-11)
#
# The catalog audit deactivated kitchen_products 61595 on 10-01 for a missing
# image; three days later it became a guide's #1 pick and drew 16 verified
# clicks / 30 d while invisible on the shop. Two guards, both per site and
# both OFF unless configured (storage config/catalog-audit-config.json
# by_site.<site>, or the site.yaml `catalog_audit:` block):
#
#   protect_set        rows people use: the site's `sql` (e.g. products a
#                      published article links, featured products) plus rows
#                      with >=1 framework-verified human click in
#                      `human_clicks.window_days`. A destructive remedy
#                      (deactivate, NULL a column) never targets them: their
#                      ids leave the rec and are reported for an image
#                      refresh / relink / a person instead. Every run also
#                      HEAD-checks each protected row's image first.
#   pending_migrations a row that already has an unapplied migration for the
#                      same criterion is not re-flagged (one recipe had 12
#                      identical copies queued behind a stalled deploy).
# ---------------------------------------------------------------------------

CATALOG_AUDIT_CONFIG_KEY = "config/catalog-audit-config.json"

GUARD_DEFAULTS: dict = {
    "protect_set": {
        "enabled": False,
        "dsn_env": "",
        # Tables whose rows the protect set holds. A rec is guarded when its
        # migration template targets one of them, or (no template) when its
        # criterion starts with one of criteria_prefixes.
        "tables": [],
        "criteria_prefixes": [],
        # Trusted site SQL: first column the row id, second its image URL
        # (optional), third a reason label (optional). May use
        # %(clicked_ids)s (text[] of ids with a verified human click).
        "sql": "",
        "human_clicks": {"table": "", "spec": {}, "match": {}, "group_col": "",
                         "window_days": 30, "profile": ""},
        "image_check": {"enabled": True, "max": 1500, "timeout_s": 10, "workers": 16},
        "statement_timeout_ms": 60000,
    },
    "pending_migrations": {
        "enabled": False,
        "dir": "",
        "dsn_env": "",
        "applied_sql": "",
        "max_age_days": 60,
        # Columns whose `<col> IN (...)`, `<col> = '<v>'` or tuple
        # `(<a>, <col>) IN ((...), ...)` name a row in a migration file
        # (["asin"] for a catalog keyed by a text id). Comments are ignored,
        # including a `-- rec-NNN` after a list value.
        "id_cols": ["id"],
        # Columns that name rows by a key that is not the row id (["slug"]).
        # key_resolve_sql maps them to ids: trusted site SQL, read-only, bound
        # %(keys)s (text[]), returning (key, id). An UPDATE keyed by a
        # non-unique key hits every row that has it, so every id counts.
        "key_cols": [],
        "key_resolve_sql": "",
        # When set, every matched file counts for this criterion instead of
        # the one in its file name (hand-named files of a one-check site).
        "criterion": "",
    },
    # The image/name check (IMAGE_NAME_CHECK). See apply_image_name_rule.
    # Always on for that check: with no lookup configured nothing it flags
    # can be deactivated, because the brand relation cannot be checked.
    "image_name_rule": {
        "dsn_env": "",
        # Trusted site SQL, read-only, bound %(keys)s (text[] of finding keys).
        # Named columns: key (the finding's key, e.g. slug), id (the row id
        # recs and migrations use), title, brand, manufacturer, description,
        # image_url. Missing columns are treated as empty.
        "lookup_sql": "",
        "statement_timeout_ms": 30000,
        # Bases that may deactivate. identity = the title names a different
        # product than the listing's own content; image = the image shows a
        # different product. attribute (a spec disagreement between title and
        # description) and attribution (who makes it) never do by default.
        "deactivate_bases": ["identity", "image"],
        "require_lookup_to_deactivate": True,
        # True: only a basis the audit's classifier states (a `basis` CSV
        # column) may deactivate. A basis read from the reason text is then
        # report-only.
        "deactivate_requires_explicit_basis": False,
        # {table, action, sql (with {ids}), filename, quote_ids} for the
        # deactivate remedy; empty = the implementer writes the SQL.
        "deactivate_template": {},
        "affected_url_template": "",
        # framework.core.brand_identity.resolve() overrides
        # (extra_brand_groups, extra_reseller_patterns, platform_brands...),
        # on top of the shared storage config/brand-identity-config.json.
        # extra_* lists accumulate across config layers.
        "brand_identity": {},
    },
}

_DESTRUCTIVE_RE = None


def _deep_merge(base: dict, over: Any) -> dict:
    """`over` on top of `base`: dicts merge, other values replace, except
    `extra_*` lists, which accumulate across layers (so a by_site
    `extra_brand_groups` adds to the defaults-level one instead of dropping
    it). Replace a list outright with its non-extra key."""
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items() if isinstance(over, dict) else []:
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif (str(k).startswith("extra_") and isinstance(v, list)
              and isinstance(out.get(k), list)):
            out[k] = out[k] + [x for x in json.loads(json.dumps(v)) if x not in out[k]]
        else:
            out[k] = v
    return out


def resolve_guard_config(site_id: str, *, site_block: Any = None,
                         file_cfg: Any = None, storage=None) -> dict:
    """GUARD_DEFAULTS <- site.yaml `catalog_audit` <- storage defaults <-
    storage by_site[site_id]. `file_cfg` skips the storage read (tests)."""
    if file_cfg is None:
        try:
            from framework.core.storage import get_storage
            file_cfg = (storage or get_storage()).read_json(CATALOG_AUDIT_CONFIG_KEY) or {}
        except Exception:
            file_cfg = {}
    if not isinstance(file_cfg, dict):
        file_cfg = {}
    cfg = _deep_merge(GUARD_DEFAULTS, site_block)
    cfg = _deep_merge(cfg, file_cfg.get("defaults"))
    return _deep_merge(cfg, (file_cfg.get("by_site") or {}).get(site_id))


def is_destructive_sql(sql: str) -> bool:
    """A remedy that deactivates a row or blanks a column."""
    import re
    global _DESTRUCTIVE_RE
    if _DESTRUCTIVE_RE is None:
        _DESTRUCTIVE_RE = re.compile(
            r"is_active\s*=\s*false|=\s*null\b|\bdelete\s+from\b", re.I)
    body = "\n".join(l for l in (sql or "").splitlines() if not l.strip().startswith("--"))
    return bool(_DESTRUCTIVE_RE.search(body))


def rec_is_guarded(rec: dict, protect: dict) -> bool:
    """Does the protect set apply to this rec's rows? A report-only rec
    (review_required) changes nothing, so it never is."""
    if rec.get("review_required"):
        return False
    tmpl = rec.get("migration_template") or {}
    if tmpl:
        return (tmpl.get("table") in set(protect.get("tables") or [])
                and is_destructive_sql(tmpl.get("sql") or ""))
    crit = str(rec.get("check_id") or rec.get("category") or "")
    return any(crit.startswith(p) for p in protect.get("criteria_prefixes") or [] if p)


_SAFE_TEXT_ID = None


def render_ids(ids: list, tmpl: Optional[dict] = None) -> str:
    """The `{ids}` substitution for a migration template. Numeric ids are
    joined as-is (the long-standing behaviour); with `quote_ids` each id
    becomes a SQL string literal, and an id that is not a plain token
    (letters, digits, `_ . : -`) is left out rather than quoted."""
    import re
    global _SAFE_TEXT_ID
    if not (tmpl or {}).get("quote_ids"):
        return ", ".join(str(i) for i in ids)
    if _SAFE_TEXT_ID is None:
        _SAFE_TEXT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
    return ", ".join("'%s'" % i for i in (str(x) for x in ids) if _SAFE_TEXT_ID.match(i))


def drop_ref_ids(rec: dict, drop: set) -> dict:
    """The rec without the given ref_ids (evidence, template SQL and URLs
    rebuilt). Returns the rec unchanged when nothing is dropped."""
    ids = [i for i in rec.get("ref_ids") or [] if str(i) not in drop]
    if len(ids) == len(rec.get("ref_ids") or []):
        return rec
    out = dict(rec)
    out["ref_ids"] = ids
    out["evidence"] = [e for e in rec.get("evidence") or []
                       if str(e.get("ref_id")) not in drop]
    tmpl = rec.get("migration_template")
    if tmpl:
        out["migration_template"] = {**tmpl, "sql_with_ids": tmpl["sql"].replace(
            "{ids}", render_ids(ids, tmpl))}
    if rec.get("affected_urls"):
        out["affected_urls"] = [u for u in rec["affected_urls"]
                                if u.rstrip("/").rsplit("/", 1)[-1] not in drop]
    out["rationale"] = (rec.get("rationale") or "") + (
        " %d row(s) left out by the remedy guards (protected or already pending)."
        % (len(rec.get("ref_ids") or []) - len(ids)))
    return out


def apply_guards(recs: list[dict], *, protected: dict, pending: dict,
                 protect_cfg: Optional[dict] = None,
                 pending_files: Optional[dict] = None) -> tuple[list, list, int]:
    """-> (recs, blocked, pending_dropped).

    protected: {row id: reason}; pending: {criterion: {row ids}};
    protect_cfg: the protect_set block (tables, criteria_prefixes);
    pending_files: {criterion: {row id: [file names]}} (optional). A guarded
    rec loses its protected ids (each reported in `blocked`); every rec loses
    ids already pending for its criterion. Recs left with no ids are dropped.

    A protected row that an unapplied migration already targets is reported
    in `blocked` too, with `pending_files`, instead of leaving silently as a
    pending duplicate: that file would deactivate a row people use."""
    out, blocked, dropped = [], [], 0
    for rec in recs:
        crit = str(rec.get("check_id") or rec.get("category") or "")
        if not rec.get("ref_ids"):
            # Names no row, so there is nothing per-row to guard. It used to
            # be dropped silently; now it passes, except that a destructive
            # rec is held while a protect set is active (never deactivate
            # blind).
            if protected and rec_is_guarded(rec, protect_cfg or {}):
                blocked.append({"id": "", "criterion": crit,
                                "reason": "rec names no row ids",
                                "remedy_instead": "re-run the audit so the rec names its rows"})
            else:
                out.append(rec)
            continue
        drop = {str(i) for i in rec.get("ref_ids") or []} & set(pending.get(crit) or ())
        dropped += len(drop)
        if protected and rec_is_guarded(rec, protect_cfg or {}):
            for i in rec.get("ref_ids") or []:
                if str(i) not in protected:
                    continue
                entry = {"id": str(i), "criterion": crit, "reason": protected[str(i)],
                         "remedy_instead": "refresh the image from the Amazon listing, "
                         "then relink; a person decides if neither works. Never "
                         "deactivate or blank it."}
                if str(i) in drop:
                    files = sorted(set(((pending_files or {}).get(crit) or {}).get(str(i)) or []))
                    entry["pending_files"] = files
                    entry["remedy_instead"] += (
                        " An unapplied migration file already targets it; remove that "
                        "file from the migrations dir before any server starts from it.")
                blocked.append(entry)
                drop.add(str(i))
        rec = drop_ref_ids(rec, drop)
        if rec.get("ref_ids"):
            out.append(rec)
    return out, blocked, dropped


def check_images(rows: list[tuple[str, str]], *, timeout_s: float = 10,
                 workers: int = 16, opener=None) -> dict:
    """HEAD each (id, url); GET a byte range when HEAD is refused.
    -> {"dead": [{id, url, status}], "missing": [ids], "checked": n,
        "unknown": n}. 404/410 or a non-image content type are dead; network
    errors are unknown (never dead), so an outage cannot mass-flag rows."""
    import concurrent.futures as cf
    import urllib.error
    import urllib.request

    ua = {"User-Agent": "Mozilla/5.0 (compatible; catalog-audit-agent/1.0)"}
    op = opener or urllib.request.build_opener()

    def one(item):
        rid, url = item
        for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
            try:
                req = urllib.request.Request(url, headers={**ua, **extra}, method=method)
                with op.open(req, timeout=timeout_s) as r:
                    ct = (r.headers.get("Content-Type") or "").lower()
                    if ct and not ct.startswith("image/") and "octet-stream" not in ct:
                        return rid, url, "dead", "content-type %s" % ct[:40]
                    return rid, url, "ok", r.status
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return rid, url, "dead", e.code
                if method == "HEAD" and e.code in (403, 405, 501):
                    continue
                return rid, url, "unknown", e.code
            except Exception as e:  # noqa: BLE001 -- network: unknown, not dead
                return rid, url, "unknown", str(e)[:60]
        return rid, url, "unknown", "no answer"

    out = {"dead": [], "missing": [], "checked": 0, "unknown": 0}
    todo = []
    for rid, url in rows:
        if not url or not str(url).startswith(("http://", "https://")):
            out["missing"].append(str(rid))
        else:
            todo.append((str(rid), str(url)))
    with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for rid, url, verdict, detail in ex.map(one, todo):
            out["checked"] += 1
            if verdict == "dead":
                out["dead"].append({"id": rid, "url": url, "status": detail})
            elif verdict == "unknown":
                out["unknown"] += 1
    return out


def load_protect_set(conn, protect: dict, *, storage=None) -> dict:
    """{row id: {"image": url|None, "reason": str}} from the site SQL plus
    rows with a verified human click (framework human_clicks)."""
    clicked: list[str] = []
    hc = protect.get("human_clicks") or {}
    if hc.get("table") and hc.get("group_col"):
        from framework.core import human_clicks
        spec = human_clicks.resolve_spec(hc.get("spec") or {}, profile=hc.get("profile") or "",
                                         storage=storage)
        counts = human_clicks.human_counts_by(
            conn, spec, table=hc["table"], group_col=hc["group_col"],
            window_days=int(hc.get("window_days") or 30), match=hc.get("match") or None)
        clicked = [k for k, n in counts.items() if k and n > 0]
        conn.rollback()
    out: dict = {}
    sql = protect.get("sql") or ""
    if sql:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s" % int(protect.get("statement_timeout_ms") or 60000))
            cur.execute(sql, {"clicked_ids": clicked})
            for row in cur.fetchall():
                rid = str(row[0])
                out[rid] = {"image": row[1] if len(row) > 1 else None,
                            "reason": str(row[2]) if len(row) > 2 and row[2] else "protected"}
        conn.rollback()
    for rid in clicked:
        out.setdefault(str(rid), {"image": None, "reason": "clicked"})
    return out


PENDING_FILENAME_RE = r"_catalog-audit-(?:rec\d+-)?(?P<crit>[a-z0-9][a-z0-9-]*)\.sql$"


def sql_strip_comments(text: str) -> str:
    """SQL text without `--` line comments and `/* */` block comments,
    leaving string literals intact (a `--` inside quotes is data)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if text[j] == c:
                    if j + 1 < n and text[j + 1] == c:   # '' / "" escape
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j            # keep the newline
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            out.append(" ")
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _sql_balanced(body: str, open_at: int) -> tuple[str, int]:
    """(inside, index after the close) for the parenthesis at `open_at`,
    skipping quoted text. ("", open_at) when it is not closed."""
    depth, i, n = 0, open_at, len(body)
    while i < n:
        c = body[i]
        if c in ("'", '"'):
            j = i + 1
            while j < n and not (body[j] == c and not (j + 1 < n and body[j + 1] == c)):
                j += 2 if body[j] == c else 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return body[open_at + 1:i], i + 1
        i += 1
    return "", open_at


def _sql_split_top(s: str) -> list[str]:
    """Split on commas outside parentheses and quotes."""
    parts, cur, depth, i, n = [], [], 0, 0, len(s)
    while i < n:
        c = s[i]
        if c in ("'", '"'):
            j = i + 1
            while j < n and not (s[j] == c and not (j + 1 < n and s[j + 1] == c)):
                j += 2 if s[j] == c else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [x.strip() for x in parts if x.strip()]


def _sql_literal(v: str) -> Optional[str]:
    """A SQL literal's value ('x', 'x'::text, 42), or None for anything
    else (a column, an expression, a subquery)."""
    import re
    v = re.sub(r"::\s*[A-Za-z_][\w ]*(?:\[\])?\s*$", "", v.strip()).strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v if re.fullmatch(r"-?\d+", v) else None


def sql_row_refs(sql: str, cols, *, weak=()) -> dict:
    """{column: {values}} for the rows a migration names through `cols`:
    `<col> IN (<literals>)`, `<col> = <literal>`, and tuple membership
    `(<a>, <col>, ...) IN ((...), ...)`. Comments are dropped first, so a
    commented-out line or a `-- rec-NNN` after a value names nothing.
    `<col> NOT IN` and non-literal values (subqueries, columns) are ignored.
    A `weak` column (a non-unique key such as a slug) is not read from a
    tuple that also has another wanted column: the tuple then names one row
    by that column, not every row that has the key."""
    import re
    want = {c.lower() for c in cols if c}
    weak = {c.lower() for c in weak or () if c}
    out: dict = {}
    if not want:
        return out
    body = sql_strip_comments(sql or "")
    names = "|".join(re.escape(c) for c in sorted(want, key=len, reverse=True))
    # tuple IN first, so its parenthesised column list is not read below
    tup_re = re.compile(r"\(\s*((?:[A-Za-z_][\w.]*\s*,\s*)+[A-Za-z_][\w.]*)\s*\)\s+IN\s*\(", re.I)
    for m in tup_re.finditer(body):
        tcols = [c.strip().rsplit(".", 1)[-1].lower() for c in m.group(1).split(",")]
        inside, _ = _sql_balanced(body, m.end() - 1)
        strong = {c for c in tcols if c in want and c not in weak}
        read = {c for c in tcols if c in want and (c not in weak or not strong)}
        for el in _sql_split_top(inside):
            if not (el.startswith("(") and el.endswith(")")):
                continue
            vals = _sql_split_top(el[1:-1])
            if len(vals) != len(tcols):
                continue
            for c, v in zip(tcols, vals):
                lit = _sql_literal(v)
                if c in read and lit:
                    out.setdefault(c, set()).add(lit)
    in_re = re.compile(r"(?<![\w.])(?:[A-Za-z_]\w*\.)?(%s)\s+IN\s*\(" % names, re.I)
    for m in in_re.finditer(body):
        if re.search(r"\bNOT\s*$", body[max(0, m.start() - 8):m.start()], re.I):
            continue
        inside, _ = _sql_balanced(body, m.end() - 1)
        for v in _sql_split_top(inside):
            lit = _sql_literal(v)
            if lit:
                out.setdefault(m.group(1).lower(), set()).add(lit)
    eq_re = re.compile(r"(?<![\w.])(?:[A-Za-z_]\w*\.)?(%s)\s*=\s*('(?:[^']|'')*'|-?\d+)(?![\w.])"
                       % names, re.I)
    for m in eq_re.finditer(body):
        lit = _sql_literal(m.group(2))
        if lit:
            out.setdefault(m.group(1).lower(), set()).add(lit)
    return out


def pending_migration_refs(mig_dir: str, applied: set, max_age_days: int = 60,
                           filename_regex: str = PENDING_FILENAME_RE, *,
                           id_cols=("id",), key_cols=(), criterion: str = "") -> dict:
    """What this agent's migration files on disk, not applied, would touch.

    -> {"ids": {criterion: {row ids}}, "keys": {criterion: {keys}},
        "files": {criterion: {id or key: [file names]}}}

    Rows are read with sql_row_refs (`id_cols` give row ids, `key_cols`
    give keys such as a slug, for the caller to resolve). The criterion
    comes from the file name (<ts>_catalog-audit[-recNNN]-<criterion>.sql)
    unless `criterion` is set; files older than max_age_days are ignored."""
    import re
    import time
    out: dict = {"ids": {}, "keys": {}, "files": {}}
    d = Path(mig_dir)
    if not d.is_dir():
        return out
    name_re = re.compile(filename_regex)
    ids_c = [c.lower() for c in (id_cols or ("id",)) if c] or ["id"]
    keys_c = [c.lower() for c in (key_cols or ()) if c and c.lower() not in ids_c]
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    for f in sorted(d.glob("*.sql")):
        if f.name in applied:
            continue
        m = name_re.search(f.name)
        if not m:
            continue
        crit = criterion or (m.groupdict().get("crit") or "")
        if not crit:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                continue
            text = f.read_text(errors="replace")
        except OSError:
            continue
        refs = sql_row_refs(text, ids_c + keys_c, weak=keys_c)
        for kind, cols in (("ids", ids_c), ("keys", keys_c)):
            vals = set().union(*(refs.get(c, set()) for c in cols)) if cols else set()
            if vals:
                out[kind].setdefault(crit, set()).update(vals)
                for v in vals:
                    out["files"].setdefault(crit, {}).setdefault(v, []).append(f.name)
    return out


def pending_migration_ids(mig_dir: str, applied: set, max_age_days: int = 60,
                          filename_regex: str = PENDING_FILENAME_RE, *,
                          id_cols=("id",), criterion: str = "") -> dict:
    """{criterion: {row ids}} named in this agent's migration files that are
    on disk but not applied (pending_migration_refs, ids only)."""
    return pending_migration_refs(mig_dir, applied, max_age_days, filename_regex,
                                  id_cols=id_cols, criterion=criterion)["ids"]


def resolve_pending_keys(conn, sql: str, refs: dict, *, timeout_ms: int = 30000) -> dict:
    """Fold pending_migration_refs()["keys"] into its "ids" with the site's
    key_resolve_sql (read-only; bound %(keys)s, returns (key, id)). Every
    row that has a key counts: an UPDATE keyed by it hits them all."""
    keys = sorted({k for ks in refs.get("keys", {}).values() for k in ks})
    if not keys or not sql:
        return refs
    by_key: dict = {}
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = %d" % int(timeout_ms))
        cur.execute(sql, {"keys": keys})
        for row in cur.fetchall():
            by_key.setdefault(str(row[0]), set()).add(str(row[1]))
    conn.rollback()
    for crit, ks in refs.get("keys", {}).items():
        for k in ks:
            for rid in by_key.get(k, ()):
                refs["ids"].setdefault(crit, set()).add(rid)
                refs["files"].setdefault(crit, {}).setdefault(rid, []).extend(
                    refs["files"].get(crit, {}).get(k, []))
    return refs


# ---------------------------------------------------------------------------
# Image/name rule (IMAGE_NAME_CHECK)
#
# The image/name audit asks a model whether a listing's name, manufacturer,
# description and image URL describe one product. From 2026-10-16 to 11-12
# every "mismatch" became a deactivation, and most of them were normal
# marketplace patterns: a WD_BLACK SSD whose manufacturer is SanDisk, a
# "Logitech for Creators" Blue Yeti described as "Logitech G", a Pentium 4
# resold by a reseller, a private-label switch made by an OEM, a spec that
# differs between title and description. 82 products went dark, 25 of them
# linked from published articles.
#
# The rule triages each finding before it becomes a rec:
#
#   row          the site's row the finding is about, read with lookup_sql.
#                Keys (slugs) are not always unique. Rows that share an id
#                are one product; otherwise the finding's `id` column, else
#                its image URL, else its name must single out one product,
#                or the row counts as unread (never deactivated).
#   basis        what the finding rests on. A classifier may say so in a
#                `basis` column; otherwise it is read from the reason:
#                  identity     the title names a different kind of product
#                               than the listing's own content (description,
#                               image), and the contrast is not a spec
#                  image        the image shows a different product
#                  description  the description is the odd one out
#                               (boilerplate, copied, or contradicted by the
#                               name and maker together, or part of the
#                               listing agrees with the name)
#                  attribution  who makes it (brand / manufacturer / maker)
#                  attribute    a spec, colour, size, lighting, closure,
#                               count or connector that the title and
#                               description disagree on, even when the reason
#                               calls them "different products"
#   relation     framework.core.brand_identity.relation(brand, manufacturer)
#                from the row: same / alias / reseller / partner / unknown
#                are explained; differs is not.
#
#   remedy       cleared      not a defect: an attribution finding whose
#                             names are explained, or an attribution /
#                             attribute / description finding that only
#                             contrasts one maker's aliases (WD vs SanDisk)
#                deactivate   basis in deactivate_bases (identity, image),
#                             the row was read, the reason does not hedge
#                             ("suggests", "typical", "appears to", "no
#                             clear", ...) and does not merely contrast one
#                             maker's aliases. The remedy guards still keep
#                             protected rows out.
#                report       everything else: a review_required rec that is
#                             never dispatched. brand != manufacturer alone
#                             ends here at most.
#
# Reading the basis from free text is a heuristic, and it errs toward
# report. A classifier `basis` column removes the guesswork, and
# deactivate_requires_explicit_basis makes it a requirement.
# ---------------------------------------------------------------------------

_REASON_RX: dict = {}


def _reason_rx() -> dict:
    import re
    if not _REASON_RX:
        _REASON_RX.update({
            # The model cannot open the image; "image URL" is a string it read.
            "image": re.compile(
                r"\b(?:image|photo|picture|thumbnail)s?\b(?!\s*url)|\bpictured\b|\bdepict", re.I),
            # Evidence from the listing's own content, not from its maker field.
            "listing": re.compile(
                r"\b(?:description|listing|bullets?|specifications?)\b"
                r"|\b(?:image|photo|picture)s?\b(?!\s*url)", re.I),
            "identity": re.compile(
                r"\b(?:different|distinct|separate|unrelated|another|wrong)\s+(?:products?|items?|"
                r"devices?|product\s+(?:types?|categor(?:y|ies)|class(?:es)?)|"
                r"(?:types?|kinds?)\s+of\s+(?:products?|items?|devices?))\b"
                r"(?!\s+(?:variants?|lines?|versions?|editions?|revisions?|generations?|"
                r"names?|naming|branding|skus?|family|families))"
                r"|\b(?:totally|completely|entirely|wholly|fundamentally)\s+(?:different|unrelated)"
                r"(?:\s+[\w-]+){0,3}?\s+(?:products?|items?|devices?|types?|categor(?:y|ies)|"
                r"kinds?|hardware|media|goods)\b", re.I),
            # The reason itself says the TITLE is corroborated and the
            # description is the odd one out (boilerplate, copied from another
            # listing, contradicted by name and maker together, or another part
            # of the listing agrees with the name). The title and image
            # shoppers see are then not shown to be wrong.
            "description_odd": re.compile(
                r"\b(?:generic|boilerplate|templated?|copied|copy[- ]?pasted?|placeholder)\b"
                r"|\bname\s+and\s+(?:the\s+)?(?:manufacturer|brand|maker|image)\b"
                r"|\b(?:manufacturer|brand|maker)\s+and\s+(?:the\s+)?(?:product\s+)?name\b"
                r"|\b(?:agrees?\s+with|consistent\s+with|matches)\s+the\s+(?:product\s+)?"
                r"(?:name|title)\b", re.I),
            # A spec dimension as the point of contrast: the listing names the
            # same kind of product with a different length, colour, lighting,
            # size, capacity, closure... That is an attribute disagreement,
            # whatever words ("different products") the reason uses.
            # Words that are often part of a product's own name ("LED strip",
            # "voltage regulator", "weight plate") are left out on purpose.
            "spec": re.compile(
                r"\b(?:lengths?|colou?r(?:s|ed|ways?)?|lighting|backlit|backlight(?:ing)?|"
                r"rgb|closures?|zippers?|zippered|sizes?|sizing|dimensions?|"
                r"capacit(?:y|ies)|resolution)\b", re.I),
            "attribution": re.compile(
                r"\b(?:manufacturer|manufactured|manufactures|brand(?:s|ed|ing)?|made\s+by|maker|"
                r"publisher|developer|compan(?:y|ies)|vendor|oem|third[- ]party|aftermarket|"
                r"reseller|retailer|integrator|distributor|sold\s+under|owner(?:ship)?|"
                r"subsidiar\w*|licens\w*|counterfeit|clone|knock[- ]?off|rebrand\w*|"
                r"entit(?:y|ies))\b", re.I),
            # The auditor is not sure. A hedged finding never deactivates.
            "hedge": re.compile(
                r"\b(?:suggest(?:s|ed|ing|ive)?|typical(?:ly)?|expected|unclear|no\s+clear|"
                r"not\s+clear(?:ly)?|appears?\s+to|seem(?:s|ingly)?|may\s+be|might|could\s+be|"
                r"possibl[ey]|(?:un)?likely|probabl[ey]|perhaps|presumably|potentially|"
                r"uncertain|ambiguous|if\s+(?:accurate|correct|true))\b", re.I),
        })
    return _REASON_RX


_BASIS_ALIASES = {
    "identity": "identity", "title": "identity", "product": "identity", "type": "identity",
    "image": "image", "photo": "image",
    "attribution": "attribution", "brand": "attribution", "manufacturer": "attribution",
    "maker": "attribution",
    "attribute": "attribute", "spec": "attribute", "specs": "attribute", "detail": "attribute",
    "description": "description",
}


def classify_reason(reason: str, basis_hint: str = "") -> str:
    """identity | image | description | attribution | attribute for one
    finding. An explicit basis from the classifier wins; otherwise the reason
    text decides. identity needs a different-product phrase AND evidence from
    the listing's own content (its description or image), so "the
    manufacturer is a different company" is attribution, not identity. A
    reason that says the name is corroborated while the description is
    boilerplate or copied is `description` (the description is wrong, not
    the title or image). A reason whose contrast is a spec dimension (length,
    colour, lighting, size, capacity, closure...) is `attribute`, even when it
    calls the two "different products"."""
    hint = _BASIS_ALIASES.get((basis_hint or "").strip().lower())
    if hint:
        return hint
    rx = _reason_rx()
    text = reason or ""
    if rx["identity"].search(text) and rx["listing"].search(text):
        if rx["description_odd"].search(text):
            return "description"
        if rx["spec"].search(text):
            return "attribute"
        return "image" if rx["image"].search(text) else "identity"
    if rx["image"].search(text) and not rx["attribution"].search(text):
        return "attribute" if rx["spec"].search(text) else "image"
    if rx["attribution"].search(text):
        return "attribution"
    return "attribute"


def _norm_text(t: Any) -> str:
    return " ".join(str(t or "").split()).casefold()


def pick_identity_row(finding: dict, rows: Any) -> tuple[Optional[dict], str]:
    """The site row a finding is about. -> (row, "") or (None, why).

    `rows` are the lookup rows for the finding's key (a list, or one dict).
    Keys such as slugs are not always unique. Rows that share an id are one
    product (the same id listed twice); among several products the
    finding's `id` (when the audit emits it), else its image URL, else its
    name must single out exactly one. Anything else is (None, why): the
    caller treats the row as unread, which never deactivates."""
    if isinstance(rows, dict):
        rows = [rows]
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if not rows:
        return None, "no row has this key"
    fid = str(finding.get("id") or "").strip()
    if fid:
        rows = [r for r in rows if str(r.get("id") or "") == fid]
        if not rows:
            return None, "row id %s is not among the rows with this key" % fid
    img = str(finding.get("image_url") or "").strip()
    name = _norm_text(finding.get("name"))
    by_img = [r for r in rows if img and str(r.get("image_url") or "")[:500].strip() == img]
    pool = by_img or rows
    by_name = [r for r in pool if name and _norm_text(str(r.get("title") or "")[:300]) == name]
    # Rows that share an id are one product (the same id under two catalogs):
    # the best-matching one stands for it.
    if len({str(r.get("id") or "") for r in rows}) == 1:
        return (by_name or by_img or rows)[0], ""
    if len({str(r.get("id") or "") for r in by_img}) == 1:
        return (by_name or by_img)[0], ""
    if len({str(r.get("id") or "") for r in by_name}) == 1:
        return by_name[0], ""
    n_ids = len({str(r.get("id") or "") for r in rows})
    return None, ("%d products share this key and the finding's image and name do not single "
                  "one out" % n_ids)


def triage_image_name_finding(finding: dict, ident: Optional[dict], rule: dict, *,
                              brand_cfg: Optional[dict] = None) -> dict:
    """-> {"remedy": cleared|deactivate|report, "basis", "relation", "why"}.
    `ident` is the site's row for the finding (pick_identity_row) or None.
    `brand_cfg` is a resolved brand_identity config (resolved from
    rule["brand_identity"] when omitted)."""
    from framework.core import brand_identity as bi
    bcfg = brand_cfg if brand_cfg is not None else bi.resolve(rule.get("brand_identity") or {})
    reason = finding.get("reason") or ""
    explicit = bool(_BASIS_ALIASES.get((finding.get("basis") or "").strip().lower()))
    basis = classify_reason(reason, finding.get("basis") or "")
    relation = None
    if ident is not None:
        relation = bi.relation(
            ident.get("brand") or finding.get("brand") or "",
            ident.get("manufacturer") or finding.get("manufacturer") or "",
            title=ident.get("title") or finding.get("name") or "", cfg=bcfg)
    out = {"basis": basis, "relation": relation}
    # A reason that contrasts makers which are all aliases of this listing's
    # own brand or manufacturer may be a naming confusion (WD_BLACK vs
    # "SanDisk Optimus", Logitech G vs Logitech for Creators). "Contrasts" =
    # two or more alias names, at least one of them not in the title, so a
    # title phrase like "Lenovo Legion" alone does not count. For an
    # attribution, attribute or description finding that settles it
    # (cleared). An image or identity finding can still be a real wrong
    # product inside one maker's family (an MX Master mouse on a G Pro
    # keyboard listing), so it goes to a person: report, never deactivate.
    if ident is not None and relation in ("same", "alias", "reseller", "partner"):
        found = bi.makers_in_text(reason, bcfg)
        own = (bi.groups_for(ident.get("brand") or "", bcfg)
               | bi.groups_for(ident.get("manufacturer") or "", bcfg))
        names = {n for v in found.values() for n in v}
        title_makers = {n for v in bi.makers_in_text(
            ident.get("title") or finding.get("name") or "", bcfg).values() for n in v}
        if found and set(found) <= own and len(names) >= 2 and names - title_makers:
            aliases = ", ".join(sorted(names))
            if basis in ("attribution", "attribute", "description"):
                return {**out, "remedy": "cleared",
                        "why": "the reason only contrasts aliases of this listing's own maker (%s)"
                               % aliases}
            return {**out, "remedy": "report",
                    "why": "the reason contrasts aliases of this listing's own maker (%s), so it "
                           "may be a naming confusion; an %s finding inside one maker's family "
                           "goes to a person, never a deactivation" % (aliases, basis)}
    if basis == "attribution":
        if relation is None:
            return {**out, "remedy": "report",
                    "why": "a brand/manufacturer finding, and the row could not be read "
                           "to check how the two relate"}
        if relation in bi.EXPLAINED:
            return {**out, "remedy": "cleared",
                    "why": "brand/manufacturer relation is %r, a normal listing pattern" % relation}
        return {**out, "remedy": "report",
                "why": "brand and manufacturer differ; that alone never deactivates a product"}
    if basis in set(rule.get("deactivate_bases") or ()):
        if ident is None and rule.get("require_lookup_to_deactivate", True):
            return {**out, "remedy": "report",
                    "why": "the row could not be read, so it is not deactivated blind"}
        if rule.get("deactivate_requires_explicit_basis") and not explicit:
            return {**out, "remedy": "report",
                    "why": "the %s basis was read from the reason text, and this site "
                           "deactivates only on a basis the audit states" % basis}
        hedge = _reason_rx()["hedge"].search(reason)
        if hedge:
            return {**out, "remedy": "report",
                    "why": "the auditor hedges (%r); a hedged finding never deactivates"
                           % hedge.group(0)}
        return {**out, "remedy": "deactivate",
                "why": "the %s contradicts the product" % ("image" if basis == "image" else "title")}
    return {**out, "remedy": "report",
            "why": "basis %r is not one that deactivates (deactivate_bases=%s)"
                   % (basis, ",".join(rule.get("deactivate_bases") or []) or "none")}


def _report_only(rec: dict, why: str) -> dict:
    """A rec a person reads and nothing dispatches: review_required, no
    migration template, an outline that forbids deactivation."""
    out = {k: v for k, v in rec.items() if k not in ("migration_template", "affected_urls")}
    out.update({
        "severity": "low", "confidence": 0.5, "tier": "review", "review_required": True,
        "title": ("Review only: " + str(rec.get("title") or ""))[:160],
        "rationale": ((rec.get("rationale") or "") + " Report only: " + why)[:600],
        "implementation_outline": {"approach": (
            "REPORT ONLY. Do not deactivate or blank this product: " + why + ". If a "
            "field is wrong, fix that field (title, brand, manufacturer) or refresh the "
            "image from the listing; otherwise mark the rec deferred.")},
    })
    return out


def apply_image_name_rule(recs: list[dict], ident: dict, rule: dict, *,
                          brand_cfg: Optional[dict] = None) -> tuple[list, list]:
    """Triage every IMAGE_NAME_CHECK rec that carries a `finding`.
    ident: {finding key: [rows]} from lookup_sql (a single row dict per key
    is accepted too). -> (recs, triage rows).

    cleared recs are dropped; report recs become review_required; deactivate
    recs name the one row the finding is about (pick_identity_row) and, when
    configured, carry the site's deactivate_template so the implementer writes
    exactly that. Every rec that leaves carries an `identity_rule` block."""
    from framework.core import brand_identity as bi
    bcfg = brand_cfg if brand_cfg is not None else bi.resolve(rule.get("brand_identity") or {})
    out: list[dict] = []
    triage: list[dict] = []
    tmpl = rule.get("deactivate_template") or {}
    url_tmpl = rule.get("affected_url_template") or ""
    for rec in recs:
        f = rec.get("finding")
        if rec.get("check_id") != IMAGE_NAME_CHECK or not isinstance(f, dict):
            out.append(rec)
            continue
        key = str(f.get("key") or "")
        rows = ident.get(key) if key else None
        rows = [rows] if isinstance(rows, dict) else [r for r in rows or [] if isinstance(r, dict)]
        row, unread_why = pick_identity_row(f, rows)
        t = triage_image_name_finding(f, row, rule, brand_cfg=bcfg)
        if row is None and rows and t["remedy"] != "cleared":
            t = {**t, "why": t["why"] + " (" + unread_why + ")"}
        rid = str((row or {}).get("id") or "")
        if t["remedy"] == "deactivate" and row is None:
            if rows:
                # Several products have this key and none is singled out:
                # never deactivate blind, whatever require_lookup_to_deactivate says.
                t = {**t, "remedy": "report",
                     "why": "no single row to scope a deactivation to (" + unread_why + ")"}
            else:
                # No lookup configured (require_lookup_to_deactivate: false):
                # the finding's own id, else its key, as before.
                rid = str(f.get("id") or key)
        if t["remedy"] == "deactivate" and (not rid or (tmpl.get("sql")
                                                        and not render_ids([rid], tmpl))):
            t = {**t, "remedy": "report",
                 "why": "row id %r is not a plain token, so no SQL is written for it" % rid}
        ref_ids = [rid] if rid else (
            [str(r.get("id")) for r in rows if r.get("id")] or ([key] if key else []))
        triage.append({"key": key, "id": rid or None, "candidates": len(rows),
                       "name": f.get("name", ""), "reason": f.get("reason", ""), **t,
                       "brand": (row or {}).get("brand"),
                       "manufacturer": (row or {}).get("manufacturer")})
        if t["remedy"] == "cleared":
            continue
        new = dict(rec)
        new["identity_rule"] = {k: t[k] for k in ("remedy", "basis", "relation", "why")}
        new["ref_ids"] = ref_ids
        new["evidence"] = [{**e, "ref_id": ref_ids[0] if ref_ids else ""}
                           for e in rec.get("evidence") or []] or [
            {"ref_id": ref_ids[0] if ref_ids else "", "snippet": f.get("name", "")[:160]}]
        if t["remedy"] == "report":
            out.append(_report_only(new, t["why"]))
            continue
        new["rationale"] = ((rec.get("rationale") or "") + " Deactivate: " + t["why"] + ".")[:600]
        if tmpl.get("sql"):
            new["migration_template"] = {**tmpl, "sql_with_ids": tmpl["sql"].replace(
                "{ids}", render_ids([rid], tmpl))}
            new["implementation_outline"] = {"approach": (
                "Apply migration_template.sql_with_ids (criterion=%s, table=%s, action=%s) "
                "and nothing else. Write it to db/migrations/<UTC-ts>_%s.sql."
                % (IMAGE_NAME_CHECK, tmpl.get("table", "?"), tmpl.get("action", "deactivate"),
                   tmpl.get("filename", "catalog-audit-" + IMAGE_NAME_CHECK)))}
        else:
            new["implementation_outline"] = {"approach": (
                "Deactivate only row %s, with an idempotent UPDATE scoped to that id "
                "(WHERE <id> = ... AND is_active = true). Touch no other row." % rid)}
        if url_tmpl and rid:
            new["affected_urls"] = [url_tmpl.format(id=rid)]
        out.append(new)
    return out, triage


def load_identity(conn, rule: dict, keys: list[str]) -> dict:
    """{key: [{"id", "title", "brand", "manufacturer", "description",
    "image_url"}, ...]} from the site's lookup_sql (read-only). Every row is
    kept: a key such as a slug may name several rows, and
    pick_identity_row decides which one a finding is about."""
    out: dict = {}
    if not keys or not rule.get("lookup_sql"):
        return out
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = %s" % int(rule.get("statement_timeout_ms") or 30000))
        cur.execute(rule["lookup_sql"], {"keys": list(keys)})
        cols = [d[0] for d in cur.description or []]
        for row in cur.fetchall():
            r = dict(zip(cols, row))
            key = str(r.get("key") if r.get("key") is not None else row[0])
            out.setdefault(key, []).append({
                "id": str(r.get("id") or key),
                **{c: ("" if r.get(c) is None else str(r.get(c)))
                   for c in ("title", "brand", "manufacturer", "description", "image_url")},
            })
    conn.rollback()
    return out


# ---------------------------------------------------------------------------
# Audit input loaders — read the file the audit script wrote
# ---------------------------------------------------------------------------

def _latest_findings_aisleprompt(findings_dir: Path) -> dict | None:
    """The aisleprompt catalog-quality-audit writes audit-history/YYYY-MM-DD.json
    each day. Pick the newest file."""
    if not findings_dir.is_dir():
        return None
    files = sorted(findings_dir.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return None


def _latest_findings_specpicks_images(csv_glob: str) -> dict | None:
    """Specpicks audit-product-images writes audit-product-images-<ts>.csv. Pick
    the newest file matching the glob, parse to {rows: [...]}.
    """
    from glob import glob as _glob
    matches = sorted(_glob(csv_glob))
    if not matches:
        return None
    rows: list[dict] = []
    with open(matches[-1]) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return {"rows": rows, "source_csv": matches[-1]}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class CatalogAuditAgent(AgentBase):
    agent_id = AGENT_ID
    send_run_summary_email = False  # already sends its own report email
    name = "Catalog Audit Agent"
    description = (
        "Runs a per-site catalog quality audit (recipes, products, listings, "
        "images), converts findings into recommendation IDs, and emails them "
        "with reply-syntax to dispatch fixes."
    )
    category = "research"

    capabilities = [
        declare("run_audit_script", "Execute the configured audit command",
                confirmation_required=False, risk_level="low"),
        declare("send_recommendations_email",
                "Send ranked findings via email to the configured recipient",
                confirmation_required=False, risk_level="low"),
    ]

    def __init__(self, *args, **kwargs):
        env_id = os.environ.get("AGENT_ID")
        if env_id and env_id != AGENT_ID:
            kwargs.setdefault("agent_id", env_id)
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        cfg_path = os.environ.get("CATALOG_AUDIT_CONFIG")
        if not cfg_path:
            raise SystemExit("CATALOG_AUDIT_CONFIG env var not set")
        if not Path(cfg_path).is_file():
            raise SystemExit(f"config not found: {cfg_path}")
        # load_quality_config gives us the .reporter / .site_id / .label structure
        # we already use for PI/CR; we just plug an `audit:` section into the same
        # SiteQualityConfig.
        self.cfg = load_quality_config(cfg_path)
        self.run_dir = self.cfg.run_dir_for_now(self.agent_id)
        self.decide("setup",
                    f"site={self.cfg.site_id} run_dir={self.run_dir} "
                    f"agent_id={self.agent_id}",
                    evidence={"site": self.cfg.site_id})

    def signals(self) -> dict | None:
        """Short-circuit when the latest findings file is unchanged since
        last run. The audit script writes a YYYY-MM-DD.json (or per-day
        CSV); if its mtime + size match what we hashed last time, the
        conversion + email work is identical."""
        try:
            audit = self.cfg.get("audit") or {}
            fmt = audit.get("findings_format", "aisleprompt-catalog-audit")
            if fmt == "aisleprompt-catalog-audit":
                findings_dir = Path(audit.get("findings_path", ""))
                if not findings_dir.is_dir():
                    return None
                files = sorted(findings_dir.glob("*.json"))
                if not files:
                    return None
                latest = files[-1]
                st = latest.stat()
                return {"site": self.cfg.site_id, "fmt": fmt,
                        "file": latest.name, "size": st.st_size, "mtime": int(st.st_mtime)}
            elif fmt == "specpicks-image-csv":
                csv_glob = audit.get("findings_glob", "")
                if not csv_glob:
                    return None
                import glob as _glob
                paths = sorted(_glob.glob(csv_glob))
                if not paths:
                    return None
                latest = Path(paths[-1])
                st = latest.stat()
                return {"site": self.cfg.site_id, "fmt": fmt,
                        "file": latest.name, "size": st.st_size, "mtime": int(st.st_mtime)}
        except Exception:
            return None
        return None

    def run(self) -> RunResult:
        cfg = self.cfg
        audit = cfg.get("audit") or {}
        cmd = audit.get("command")
        fmt = audit.get("findings_format", "aisleprompt-catalog-audit")

        # ── 1. Run the audit script (best-effort — many audit scripts already
        #      run on cron and write their findings file, so a fresh exec
        #      isn't strictly required) ─────────────────────────────────────
        if cmd:
            self.status("running audit script", progress=0.1, current_action="audit")
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=int(audit.get("timeout_s", 1800)),
                )
                self.decide("observation",
                            f"audit rc={proc.returncode} stdout={len(proc.stdout)}b "
                            f"stderr={len(proc.stderr)}b")
                if proc.returncode != 0:
                    self.decide("error",
                                f"audit script rc={proc.returncode}: "
                                f"{(proc.stderr or proc.stdout)[:300]}")
            except Exception as e:
                self.decide("error", f"audit exec failed: {e}")
        else:
            self.decide("observation",
                        "no audit.command configured — using existing findings file")

        # ── 2. Load latest findings ────────────────────────────────────────
        self.status("loading findings", progress=0.4, current_action="load")
        if fmt == "aisleprompt-catalog-audit":
            findings_dir = Path(audit.get("findings_path", ""))
            findings_doc = _latest_findings_aisleprompt(findings_dir)
        elif fmt == "specpicks-image-csv":
            csv_glob = audit.get("findings_glob", "")
            findings_doc = _latest_findings_specpicks_images(csv_glob)
        else:
            raise SystemExit(f"unsupported findings_format: {fmt!r}")
        if not findings_doc:
            return RunResult(status="failure", summary="no findings file found",
                              metrics={})

        # ── 3. Convert to recs ─────────────────────────────────────────────
        self.status("converting findings → recs", progress=0.55, current_action="convert")
        recs = CONVERTERS[fmt](findings_doc, max_recs=int(audit.get("max_recs", 30)))
        recs, rule_metrics = self._apply_image_name_rule(recs)
        recs, guard_metrics = self._apply_remedy_guards(recs)
        guard_metrics = {**rule_metrics, **guard_metrics}
        if not recs:
            return RunResult(status="success",
                              summary="audit found 0 actionable issues — clean run",
                              metrics={"recs_total": 0, "recs_critical": 0,
                                       **guard_metrics})
        recs = sorted(recs, key=lambda r: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(r["severity"], 9),
            -r["confidence"],
        ))
        recs = assign_rec_ids(recs)

        # ── 4. Drain user replies from prior run, write recommendations.json ─
        prior_recs_path = self._most_recent_recs_path()
        applied = apply_user_responses(
            responses=getattr(self, "responses", []) or [],
            prior_recs_path=prior_recs_path,
        )
        if applied:
            self.decide("observation",
                        f"applied {len(applied)} user response(s) to prior recs",
                        evidence={"applied": applied})

        request_id = new_request_id()
        recs_doc = {
            "schema_version": "1",
            "site": cfg.site_id,
            "agent": self.agent_id,
            # Duplicate as `agent_id` so the implementer dispatcher (which
            # reads `agent_id` to pick the runbook) matches without a
            # legacy-key fallback. PI/CR don't strictly need this because
            # their dispatch is the default seo/AGENT.md path; catalog-audit
            # needs the catalog-audit branch + CATALOG_AUDIT.md runbook.
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "summary": (
                f"Catalog audit on {cfg.label} found {len(recs)} actionable issue(s) "
                f"(critical={sum(1 for r in recs if r['severity']=='critical')}, "
                f"high={sum(1 for r in recs if r['severity']=='high')}, "
                f"medium={sum(1 for r in recs if r['severity']=='medium')}, "
                f"low={sum(1 for r in recs if r['severity']=='low')})."
            ),
            "recommendations": recs,
        }
        # Producer-history dedup — see AgentBase.filter_proposals_against_history.
        recs_doc["recommendations"] = self.filter_proposals_against_history(
            recs_doc["recommendations"])
        # Safety tag — catalog-audit recs are well-bounded DB-row
        # updates, safe for the gpt-4.1 channel. The framework's
        # implementer_safety primitive infers STRUCTURED from
        # dispatch_kind="catalog-audit".
        self.stamp_implementer_safety(
            recs_doc["recommendations"], dispatch_kind="catalog-audit")
        validate_recs_doc(recs_doc)
        self._save_artifact("recommendations.json", recs_doc)
        self.record_emitted_proposals(recs_doc["recommendations"])

        # ── 5. Email — use self.agent_id (per-site) so the responder routes ─
        # auto_queued=True swaps the "Reply to ship" block for an "auto-queued"
        # block so the user knows every found issue is already on the
        # implementer's queue. Reply with `defer rec-NNN` to opt out of one.
        self.status("rendering email", progress=0.85, current_action="email")
        subject, html = render_recs_email(
            cfg=cfg, agent_id=self.agent_id, request_id=request_id,
            recs=recs, summary=recs_doc["summary"],
            auto_queued=bool(recs),
        )
        self._save_artifact("email-rendered.html", html)

        email_cfg = (cfg.get("reporter", {}) or {}).get("email") or {}
        to = email_cfg.get("to") or []
        sender = email_cfg.get("from", "")
        msmtp_account = email_cfg.get("msmtp_account", "automation")
        if to and sender:
            ok, detail = send_via_msmtp(
                subject=subject, body_html=html, to=to,
                sender=sender, msmtp_account=msmtp_account,
                extra_headers={
                    "X-Reusable-Agent": self.agent_id,
                    "Reply-To": sender,
                },
            )
            if ok:
                self.decide("action",
                            f"emailed {len(to)} recipient(s) ({detail})")
                # Outbound metadata so responder can match the reply
                self.storage.write_json(
                    f"agents/{self.agent_id}/outbound-emails/{request_id}.json",
                    {
                        "schema_version": "1",
                        "request_id": request_id,
                        "agent_id": self.agent_id,
                        "site": cfg.site_id,
                        "run_ts": self.run_ts,
                        "subject": subject,
                        "to": list(to),
                        "expects_response": True,
                        "rec_count": len(recs),
                        "rec_ids": [r["id"] for r in recs],
                        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "transport": detail,
                        "ok": True,
                        "kind": "email-recommendations",
                    },
                )
                # ── Auto-queue every rec for implementation ─────────────
                # Same pattern as PI agent + seo-reporter: drop a trigger
                # file the responder picks up on its next tick (every 60s)
                # and fans out to the implementer in batches. Email becomes
                # informational — reply with `defer rec-NNN` to override.
                if recs:
                    self._write_auto_queue(request_id=request_id, recs=recs)
            else:
                self.decide("error", f"email send failed: {detail}")
        else:
            self.decide("observation", "no recipient/sender configured")

        # ── 6. Auto-dispatch tier=auto recs (if opted in) ───────────────────
        dispatched = dispatch_auto_recs(
            cfg=cfg, agent_id=self.agent_id, recs=recs, storage=self.storage,
        )
        if dispatched:
            self.decide("action",
                        f"auto-dispatched {len(dispatched)} recs to implementer",
                        evidence={"rec_ids": dispatched})

        return RunResult(
            status="success",
            summary=recs_doc["summary"],
            metrics={
                "recs_total": len(recs),
                "recs_critical": sum(1 for r in recs if r["severity"] == "critical"),
                "recs_high": sum(1 for r in recs if r["severity"] == "high"),
                "applied_responses": len(applied),
                "auto_dispatched": len(dispatched),
                **guard_metrics,
            },
            next_state={
                "last_run_ts": self.run_ts,
                "last_request_id": request_id,
                "site_id": cfg.site_id,
            },
        )

    def _apply_image_name_rule(self, recs: list[dict]) -> tuple[list[dict], dict]:
        """Triage IMAGE_NAME_CHECK findings (apply_image_name_rule). Reads
        the site's rows read-only when lookup_sql is configured; if they
        cannot be read, nothing from this check is deactivated this run."""
        targets = [r for r in recs
                   if r.get("check_id") == IMAGE_NAME_CHECK and isinstance(r.get("finding"), dict)]
        if not targets:
            return recs, {}
        gcfg = resolve_guard_config(self.cfg.site_id, site_block=self.cfg.get("catalog_audit"),
                                    storage=self.storage)
        rule = gcfg["image_name_rule"]
        ident: dict = {}
        if rule.get("lookup_sql") and rule.get("dsn_env"):
            keys = sorted({str(r["finding"].get("key")) for r in targets if r["finding"].get("key")})
            try:
                import psycopg2
                conn = psycopg2.connect(os.environ[rule["dsn_env"]], connect_timeout=15)
                try:
                    conn.set_session(readonly=True)
                    ident = load_identity(conn, rule, keys)
                finally:
                    conn.close()
            except Exception as e:
                self.decide("error", f"image/name rule: rows unreadable, nothing from this "
                                     f"check is deactivated this run: {e}"[:300])
                ident = {}
        from framework.core import brand_identity as bi
        brand_cfg = bi.resolve(rule.get("brand_identity") or {},
                               shared=bi.load_shared(self.storage))
        out, triage = apply_image_name_rule(recs, ident, rule, brand_cfg=brand_cfg)
        counts = {k: sum(1 for t in triage if t["remedy"] == k)
                  for k in ("cleared", "report", "deactivate")}
        shared_keys = sorted(k for k, v in ident.items() if isinstance(v, list) and len(v) > 1)
        self._save_artifact("image-name-triage.json", {
            "run_ts": self.run_ts, "counts": counts, "rows_found": len(ident),
            "keys_with_several_rows": shared_keys,
            "deactivate_bases": rule.get("deactivate_bases") or [], "findings": triage})
        self.decide("observation",
                    f"image/name rule: {len(triage)} finding(s) -> {counts['deactivate']} "
                    f"deactivate, {counts['report']} report-only, {counts['cleared']} cleared",
                    evidence={"cleared": [t["key"] for t in triage if t["remedy"] == "cleared"][:50],
                              "report": [t["key"] for t in triage if t["remedy"] == "report"][:50]})
        return out, {"image_name_findings": float(len(triage)),
                     "image_name_deactivate": float(counts["deactivate"]),
                     "image_name_report_only": float(counts["report"]),
                     "image_name_cleared": float(counts["cleared"]),
                     "image_name_shared_keys": float(sum(
                         1 for t in triage if (t.get("candidates") or 0) > 1))}

    def _apply_remedy_guards(self, recs: list[dict]) -> tuple[list[dict], dict]:
        """Protect set + pending-migration dedup (see GUARD_DEFAULTS). Both
        off unless configured; any failure leaves the recs as they were and
        is logged, except that a configured protect set that cannot be read
        drops every guarded rec this run (never deactivate blind). An
        image/name rec kept out by the protect set comes back report-only."""
        gcfg = resolve_guard_config(self.cfg.site_id, site_block=self.cfg.get("catalog_audit"),
                                    storage=self.storage)
        protect, pend = gcfg["protect_set"], gcfg["pending_migrations"]
        metrics: dict = {}
        protected: dict = {}
        if protect.get("enabled"):
            try:
                import psycopg2
                conn = psycopg2.connect(os.environ[protect["dsn_env"]], connect_timeout=15)
                try:
                    conn.set_session(readonly=True)
                    protected = load_protect_set(conn, protect, storage=self.storage)
                finally:
                    conn.close()
            except Exception as e:
                self.decide("error", f"protect set unreadable, guarded recs held this run: {e}"[:300])
                kept = [r for r in recs if not rec_is_guarded(r, protect)]
                metrics["protected_remedies_blocked"] = float(len(recs) - len(kept))
                kept += [_report_only(r, "the protect set could not be read this run")
                         for r in recs if rec_is_guarded(r, protect) and r.get("identity_rule")]
                recs = kept
                protect = {**protect, "enabled": False}
            metrics["protect_set_size"] = float(len(protected))
            ic = protect.get("image_check") or {}
            if protected and ic.get("enabled", True):
                rows = [(rid, v.get("image")) for rid, v in protected.items()
                        if v.get("reason") != "clicked" or v.get("image")]
                rows = rows[:int(ic.get("max") or 1500)]
                res = check_images(rows, timeout_s=float(ic.get("timeout_s") or 10),
                                   workers=int(ic.get("workers") or 16))
                metrics.update({"protect_set_images_checked": float(res["checked"]),
                                "protect_set_images_dead": float(len(res["dead"])),
                                "protect_set_images_missing": float(len(res["missing"]))})
                self._save_artifact("protect-set-images.json", {
                    "run_ts": self.run_ts, "checked": res["checked"],
                    "unknown": res["unknown"], "dead": res["dead"],
                    "missing": [{"id": i, "reason": protected[i]["reason"]}
                                for i in res["missing"]]})
                if res["dead"] or res["missing"]:
                    self.decide("observation",
                                f"protect set: {len(res['dead'])} dead and "
                                f"{len(res['missing'])} missing image(s) on rows people use; "
                                f"these need an image refresh, never deactivation",
                                evidence={"dead": [d["id"] for d in res["dead"]][:50],
                                          "missing": res["missing"][:50]})
        pending: dict = {}
        pending_files: dict = {}
        if pend.get("enabled") and pend.get("dir"):
            try:
                applied: set = set()
                applied_known = False

                def _refs(done: set) -> dict:
                    return pending_migration_refs(
                        pend["dir"], done, int(pend.get("max_age_days") or 60),
                        pend.get("filename_regex") or PENDING_FILENAME_RE,
                        id_cols=pend.get("id_cols") or ("id",),
                        key_cols=pend.get("key_cols") or (), criterion=pend.get("criterion") or "")

                dsn_env = pend.get("dsn_env") or protect.get("dsn_env")
                if pend.get("applied_sql") and dsn_env:
                    import psycopg2
                    conn = psycopg2.connect(os.environ[dsn_env], connect_timeout=15)
                    try:
                        conn.set_session(readonly=True)
                        with conn.cursor() as cur:
                            cur.execute(pend["applied_sql"])
                            applied = {str(r[0]) for r in cur.fetchall()}
                            applied_known = True
                        conn.rollback()
                        refs = _refs(applied)
                        if pend.get("key_resolve_sql") and refs["keys"]:
                            refs = resolve_pending_keys(conn, pend["key_resolve_sql"], refs)
                    finally:
                        conn.close()
                else:
                    refs = _refs(applied)
                pending, pending_files = refs["ids"], refs["files"]
                if refs["keys"] and not pend.get("key_resolve_sql"):
                    self.decide("observation",
                                "pending migrations name %d row(s) by key (%s) with no "
                                "key_resolve_sql to map them to ids; those rows are not deduped"
                                % (sum(len(v) for v in refs["keys"].values()),
                                   ",".join(pend.get("key_cols") or [])))
                if applied_known:
                    metrics.update(self._migration_ledger_metrics(pend, applied))
            except Exception as e:
                self.decide("observation", f"pending-migration scan failed: {e}"[:300])
        # A stranded migration that targets a row people use is worth knowing
        # about whether or not this run re-flagged that row: a server started
        # from the checkout would apply it.
        pend_protected = sorted(
            ({"id": i, "criterion": c, "reason": protected[i]["reason"],
              "files": sorted(set((pending_files.get(c) or {}).get(i) or []))}
             for c, ids in pending.items() for i in ids if i in protected),
            key=lambda x: (x["criterion"], x["id"]))
        if protected and pend.get("enabled"):
            metrics["pending_migrations_targeting_protected"] = float(len(pend_protected))
        if protected or pending:
            by_id = {str(i): r for r in recs if r.get("identity_rule") for i in r.get("ref_ids") or []}
            recs, blocked, dropped = apply_guards(
                recs, protected={k: v["reason"] for k, v in protected.items()},
                pending=pending, protect_cfg=protect, pending_files=pending_files)
            metrics["protected_remedies_blocked"] = metrics.get(
                "protected_remedies_blocked", 0.0) + float(len(blocked))
            metrics["pending_migration_dupes_dropped"] = float(dropped)
            # Rows people use are report-only, never deactivated: an
            # image/name finding on one is still worth a person's look.
            recs += [_report_only(by_id[b["id"]], "a protected row (%s); never deactivated"
                                  % (b.get("reason") or "protected"))
                     for b in blocked if b.get("id") in by_id]
            if blocked or pend_protected:
                self._save_artifact("protected-rows.json", {
                    "run_ts": self.run_ts, "blocked": blocked,
                    "pending_migrations_targeting_protected": pend_protected})
            if blocked:
                self.decide("observation",
                            f"remedy guard: {len(blocked)} protected row(s) kept out of "
                            f"deactivate/blank remedies",
                            evidence={"ids": [b["id"] for b in blocked][:50]})
            if pend_protected:
                self.decide("observation",
                            f"{len(pend_protected)} protected row(s) are targeted by an "
                            f"unapplied migration file of this agent; remove those files "
                            f"before any server starts from the checkout",
                            evidence={"rows": pend_protected[:50]})
        return recs, metrics

    def _migration_ledger_metrics(self, pend: dict, applied: set) -> dict:
        """This agent's migration files that git does not track: applied but
        never committed (production changed, the repo has no record) and
        stranded (a server started from the checkout would apply them). The
        implementer's migration ledger (framework/core/migration_ledger.py)
        commits the first kind; this only measures and reports."""
        import re
        from framework.core import migration_ledger as ml
        d = Path(pend["dir"])
        repo = ml.repo_root(str(d)) if d.is_dir() else None
        if not repo:
            return {}
        rx = re.compile(pend.get("filename_regex") or PENDING_FILENAME_RE)
        names = [n for n in ml.untracked_sql(repo, d) if rx.search(n)]
        applied_unc = [n for n in names if n in applied]
        stranded = [n for n in names if n not in applied]
        if applied_unc or stranded:
            self._save_artifact("migration-ledger.json", {
                "run_ts": self.run_ts, "dir": str(d),
                "applied_uncommitted": applied_unc, "stranded": stranded})
            self.decide("observation",
                        f"{len(applied_unc)} of this agent's migrations were applied but never "
                        f"committed, and {len(stranded)} untracked one(s) are not applied; a "
                        f"server started from this checkout applies untracked files too",
                        evidence={"applied_uncommitted": applied_unc[:50],
                                  "stranded": stranded[:50]})
        return {"migrations_applied_uncommitted": float(len(applied_unc)),
                "migrations_stranded": float(len(stranded))}

    def _write_auto_queue(self, *, request_id: str, recs: list[dict]) -> None:
        """Direct-dispatch the rec batch to the implementer via
        framework.core.dispatch. Site-level lock; retries on transient
        failures; falls back to writing agents/responder-agent/auto-queue/
        if all retries exhaust (transitional). Email becomes informational
        — recipient replies only to override (defer rec-NNN)."""
        try:
            from framework.core import dispatch
            # Report-only recs (review_required) are for a person; the
            # backlog dispatcher skips them too.
            rec_ids = [r["id"] for r in recs if r.get("id") and not r.get("review_required")]
            if not rec_ids:
                self.decide("observation", "no dispatchable recs (all report-only)")
                return
            handle = dispatch.gated_dispatch_now(
                cfg=self.cfg, agent=self,
                agent_id=self.agent_id, run_dir=str(self.run_dir),
                rec_ids=rec_ids, action="implement",
                site=self.cfg.site_id, subject_tag="catalog-audit",
                request_id=request_id,
            )
            if handle is None:
                # auto_implement=false → awaiting email approval
                # (gated_dispatch_now already logged the decision)
                return  # skip the rest of the dispatch block
            label = "auto-queue fallback" if handle.fell_back_to_queue else "direct dispatch"
            self.decide("action",
                        f"dispatched {len(rec_ids)} rec(s) to implementer via {label} "
                        f"(request_id={request_id})")
        except Exception as e:
            self.decide("error", f"dispatch failed: {e}")

    def _most_recent_recs_path(self) -> Path | None:
        """Find the most recent prior run's recommendations.json for this site."""
        site_runs = self.run_dir.parent  # runs_root/<site>/
        if not site_runs.is_dir():
            return None
        candidates = sorted(
            p for p in site_runs.iterdir()
            if p.is_dir() and p.name != self.run_ts and (p / "recommendations.json").is_file()
        )
        if not candidates:
            return None
        return candidates[-1] / "recommendations.json"

    def _save_artifact(self, name: str, content: Any) -> None:
        storage_key = f"agents/{self.agent_id}/runs/{self.run_ts}/{name}"
        disk = self.run_dir / name
        if isinstance(content, (dict, list)):
            disk.write_text(json.dumps(content, indent=2))
            self.storage.write_json(storage_key, content)
        else:
            text = str(content)
            disk.write_text(text)
            self.storage.write_text(storage_key, text)


if __name__ == "__main__":
    CatalogAuditAgent().run_once()
