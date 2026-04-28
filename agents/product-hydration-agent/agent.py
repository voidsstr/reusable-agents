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

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
import yaml  # noqa: E402


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
    if not isinstance(v, dict):
        return None
    pros = v.get("pros") or []
    cons = v.get("cons") or []
    if not isinstance(pros, list) or not isinstance(cons, list):
        return None
    pros = [str(p).strip() for p in pros if isinstance(p, (str, int, float)) and str(p).strip()]
    cons = [str(c).strip() for c in cons if isinstance(c, (str, int, float)) and str(c).strip()]
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
    return json.dumps({"title": title, "meta_description": meta_desc,
                        "keywords": keywords[:7]})


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
    return psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor)


def _select_candidates(conn, *, content_types: list[str],
                       batch_size: int, stale_after_days: int,
                       site_id_filter: str | None,
                       priority_column: str = "review_count") -> list[dict]:
    """Return rows that need any of `content_types` hydrated.

    A row is a candidate if EITHER:
      - hydrated_at IS NULL (never hydrated), OR
      - hydrated_at < NOW() - stale_after_days, OR
      - any of the requested content_types' columns IS NULL.
    """
    cols = []
    for ct in content_types:
        col, _, _ = CONTENT_SPEC[ct]
        cols.append(col)
    null_clauses = " OR ".join([f"{c} IS NULL" for c in cols])
    site_clause = ""
    params: list[Any] = []
    if site_id_filter:
        site_clause = "AND site_id = %s"
        params.append(site_id_filter)
    params.append(stale_after_days)
    params.append(batch_size)

    sql = f"""
        SELECT id, site_id, asin, title, brand, price, rating, review_count,
               features, description, category_slug,
               pros_cons, faq, seo_meta, hydrated_at
        FROM products
        WHERE is_active = TRUE
          {site_clause}
          AND (
              {null_clauses}
              OR hydrated_at IS NULL
              OR hydrated_at < NOW() - (%s || ' days')::interval
          )
        ORDER BY {priority_column} DESC NULLS LAST, id ASC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


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

    body = f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;color:#0f172a;line-height:1.5">
<div style="max-width:820px;margin:0 auto;padding:20px">
  <h2 style="margin:0 0 8px;color:#0f172a">Product Hydration — {site}</h2>
  <div style="color:#475569;font-size:13px;margin-bottom:18px">
    Run <code>{run_ts}</code> — {totals.get('hydrated', 0)} hydrated /
    {totals.get('failed', 0)} failed / {totals.get('skipped_already_fresh', 0)} already fresh
  </div>

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

        totals = {
            "queued": 0,
            "hydrated": 0,
            "failed": 0,
            "partial": 0,
            "skipped_already_fresh": 0,
            "claude_calls": 0,
            "claude_total_seconds": 0.0,
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
            try:
                queue = _select_candidates(
                    conn,
                    content_types=content_types,
                    batch_size=batch_size,
                    stale_after_days=stale_after_days,
                    site_id_filter=self.site_id_filter,
                    priority_column=priority_column,
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

        # Compute final coverage
        try:
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
            sample_outputs=sample_outputs,
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
            f"{totals['failed']} failed, {totals['skipped_already_fresh']} fresh. "
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
                "catalog_coverage_pct": coverage.get("fully_hydrated_pct", 0.0),
                "stale_pct": coverage.get("stale_pct", 0.0),
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
                          model: str, sample_outputs: list[str]) -> None:
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
        }
        self._save("results.json", results, run_prefix)

        # goal-progress.json — narrow view aligned with site.yaml.goals
        goals_block = cfg.get("goals") or {}
        goal_progress = {
            "schema_version": "1",
            "run_ts": self.run_ts,
            "primary": goals_block.get("primary"),
            "freshness": goals_block.get("freshness"),
            "quality": goals_block.get("quality"),
            "current": {
                "fully_hydrated_pct": coverage.get("fully_hydrated_pct"),
                "stale_pct": coverage.get("stale_pct"),
                "per_content_type_pct": {
                    ct: v["pct"] for ct, v in
                    (coverage.get("per_content_type") or {}).items()
                },
            },
        }
        self._save("goal-progress.json", goal_progress, run_prefix)

        # llm-output.txt — sample, capped
        sample_text = "".join(sample_outputs)[:50_000]
        self._save("llm-output.txt", sample_text, run_prefix)

        # context-summary.md — narrative for the next run
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
            f"## Catalog coverage\n\n"
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
