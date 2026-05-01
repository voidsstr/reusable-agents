"""Implementer dispatch queue — lists pending responses-queue items and
recent dispatch log files so the ImplementerQueue UI page can show what's
been queued, what's running, and what recently completed."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/implementer", tags=["dispatch"], dependencies=[Depends(require_token)])

_LOG_DIR = Path(os.getenv("AGENT_LOG_DIR", "/tmp/reusable-agents-logs"))
# Match BOTH legacy `dispatch-seo-implementer-...` and current
# `dispatch-implementer-...` filename shapes.
_DISPATCH_PAT = re.compile(r"dispatch-(?:seo-)?implementer-([a-z]+)-(\d{8}T\d{6}Z)\.log$")
_REC_IDS_PAT = re.compile(r"\[implementer\] recs=([^\n\r]+)")
_COMMIT_PAT = re.compile(r"\[implementer\] committed ([a-f0-9]{7,40})")
_DONE_PAT = re.compile(r"\[implementer\] done")
# Default response-queue agent ids to enumerate. Each agent that consumes
# user replies has its own queue under agents/<id>/responses-queue/. The
# implementer is the default target; others have opted in via
# `target_agent: <id>` in responder routes.
_QUEUE_AGENT_IDS = ("implementer", "ebay-product-sync-agent")


def _parse_dispatch_log(path: Path) -> dict:
    try:
        text = path.read_text(errors="replace")
    except Exception:
        text = ""
    rec_ids: list[str] = []
    m = _REC_IDS_PAT.search(text)
    if m:
        raw = m.group(1).strip()
        rec_ids = [r.strip().strip("'\"[]") for r in raw.split(",") if r.strip()]
    commit_sha = ""
    cm = _COMMIT_PAT.search(text)
    if cm:
        commit_sha = cm.group(1)
    done = bool(_DONE_PAT.search(text))
    # last 4 kB for the tail snippet
    tail = text[-4096:] if len(text) > 4096 else text
    return {
        "rec_ids": rec_ids,
        "rec_count": len(rec_ids),
        "commit_sha": commit_sha,
        "done": done,
        "tail": tail,
    }


def _list_dispatches(limit: int = 20) -> list[dict]:
    if not _LOG_DIR.is_dir():
        return []
    results = []
    for p in sorted(_LOG_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        m = _DISPATCH_PAT.match(p.name)
        if not m:
            continue
        site, run_ts = m.group(1), m.group(2)
        stat = p.stat()
        parsed = _parse_dispatch_log(p)
        # "running" = file modified in last 5 min AND not done
        age_s = datetime.now(timezone.utc).timestamp() - stat.st_mtime
        running = (not parsed["done"]) and age_s < 300
        started = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
        results.append({
            "id": f"{site}-{run_ts}",
            "site": site,
            "run_ts": run_ts,
            "log_filename": p.name,
            "started_at": started,
            "size_bytes": stat.st_size,
            "status": "running" if running else "completed",
            **parsed,
        })
        if len(results) >= limit:
            break
    return results


@router.get("/queue")
def get_queue(limit: int = Query(20, le=100)):
    """Return pending responses-queue items + recent dispatches.

    Walks each known queue-owning agent's `responses-queue/` prefix
    directly (cheap), then reads only the most recent `limit` items
    (avoids fanning out 400+ blob GETs that timed out the endpoint).
    """
    s = get_storage()

    # 1. List keys across all known queue-owning agents (cheap — one
    #    list_prefix per agent, no per-item reads).
    all_keys: list[tuple[str, str]] = []  # (agent_id, key)
    for agent_id in _QUEUE_AGENT_IDS:
        try:
            for key in s.list_prefix(f"agents/{agent_id}/responses-queue/"):
                if key.endswith(".json"):
                    all_keys.append((agent_id, key))
        except Exception:
            # Missing queue dir or transient list error → skip this agent.
            continue

    # 2. Sort by key (request-id is monotonic — `r-<run_ts>-<rec_id>`)
    #    so the suffix sorts ~chronologically. Take the most recent `limit`.
    all_keys.sort(key=lambda x: x[1], reverse=True)
    head = all_keys[:limit]

    # 3. Read only the head — at most `limit` Azure GETs (default 20).
    pending: list[dict] = []
    for agent_id, key in head:
        try:
            d = s.read_json(key)
        except Exception:
            d = None
        if d:
            pending.append({"agent_id": agent_id, "_key": key, **d})

    dispatches = _list_dispatches(limit)

    return {
        "pending": pending,
        "pending_total": len(all_keys),
        "dispatches": dispatches,
    }


@router.get("/dispatches")
def list_dispatches(limit: int = Query(20, le=100)):
    return _list_dispatches(limit)


_BATCHES_CACHE: dict = {"data": None, "ts": 0.0}
_BATCHES_TTL_S = 30.0


@router.get("/batches")
def list_batches(limit: int = Query(20, le=50)):
    """List active + recent batched dispatch chains.

    Was 25s+ — every chain triggered ~10 sequential Azure blob reads
    inside the loop body. Now: pre-sort dispatch-batches.json keys by
    embedded run_ts desc, take top `limit`, then parallel-stitch those
    chains with a 16-worker thread pool. Plus a 30s TTL cache because
    the queue page polls every 5-15s.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    now_mono = time.monotonic()
    cached = _BATCHES_CACHE["data"]
    if cached is not None and (now_mono - _BATCHES_CACHE["ts"]) < _BATCHES_TTL_S:
        return {"chains": cached[:limit]}

    s = get_storage()
    manifests: list[dict] = []

    # 1. List dispatch-batches.json paths (cheap — one prefix walk)
    candidate_keys: list[tuple[str, str]] = []  # (run_ts_for_sort, blob_key)
    try:
        for blob_key in s.list_prefix("agents/"):
            if not blob_key.endswith("/dispatch-batches.json"):
                continue
            parts = blob_key.split("/")
            run_ts_sort = parts[3] if len(parts) > 3 else ""
            candidate_keys.append((run_ts_sort, blob_key))
    except Exception:
        candidate_keys = []
    # Sort desc by embedded run_ts (lex sort is correct on UTC iso ts).
    candidate_keys.sort(key=lambda x: x[0], reverse=True)
    # Keep more than `limit` because some entries may be skipped (no batches);
    # 3× provides headroom while still bounding worker count.
    head_keys = [bk for _, bk in candidate_keys[: limit * 3]]

    def _stitch_one(blob_key: str) -> dict | None:
        try:
            m = s.read_json(blob_key)
        except Exception:
            return None
        if not m or not m.get("batches"):
            return None
            # Derive metadata. Two run-ts values matter:
            #   - dispatch_run_ts: where dispatch-batches.json lives
            #     (typically `rundir-<agent>-<source-ts>-<rand>` because
            #     the implementer sync_back uses the tempdir name)
            #   - source_run_ts: where the original SEO/PI run wrote
            #     recommendations.json. From the manifest itself, set
            #     by the responder's _write_batch_manifest.
        parts = blob_key.split("/")
        source_agent = parts[1] if len(parts) > 1 else ""
        dispatch_run_ts = parts[3] if len(parts) > 3 else ""
        source_run_ts = m.get("source_run_ts") or dispatch_run_ts
        run_dir_basename = f"rundir-{source_agent}-{dispatch_run_ts}"
        rec_titles: dict[str, str] = {}
        rec_kinds: dict[str, str] = {}
        rec_lifecycle: dict[str, dict] = {}
        for read_run_ts in (source_run_ts, dispatch_run_ts):
            if not read_run_ts:
                continue
            try:
                rd = s.read_json(f"agents/{source_agent}/runs/{read_run_ts}/recommendations.json") or {}
                for r in rd.get("recommendations", []):
                    rid = r.get("id")
                    if not rid:
                        continue
                    if rid not in rec_titles:
                        rec_titles[rid] = (r.get("title") or r.get("description") or "")[:200]
                        for f in ("priority", "severity", "tier"):
                            if r.get(f):
                                rec_kinds[rid] = str(r.get(f))
                                break
                    for f in ("implemented", "implemented_at", "implemented_run_ts",
                              "implemented_via", "implemented_commit",
                              "shipped", "shipped_at",
                              "shipped_tag", "shipped_image", "shipped_via"):
                        if f in r:
                            rec_lifecycle.setdefault(rid, {})[f] = r[f]
            except Exception:
                pass
        rec_status: dict[str, dict] = {}
        try:
            changes_prefix = f"agents/{source_agent}/runs/{dispatch_run_ts}/changes/"
            for ck in s.list_prefix(changes_prefix):
                if not ck.endswith(".summary.md"):
                    continue
                fname = ck[len(changes_prefix):]
                rid = fname.split(".summary.md")[0]
                body = s.read_text(ck) or ""
                head = body.strip().split("\n", 1)[0]
                is_deferred = bool(re.match(
                    r"^(DEFERRED|SKIP|SKIPPED|NO ACTION|NOT APPLIED)[:.]",
                    head, re.I
                ))
                rec_status[rid] = {
                    "summary_first_line": head[:200],
                    "summary_chars": len(body),
                    "deferred": is_deferred,
                }
        except Exception:
            pass
        applied_set: set[str] = set()
        try:
            ar = s.read_json(f"agents/{source_agent}/runs/{dispatch_run_ts}/applied-recs.json")
            if ar:
                ids = ar.get("applied_rec_ids") or ar.get("rec_ids") or []
                if isinstance(ids, list):
                    applied_set = {str(x) for x in ids if x}
        except Exception:
            pass
        batches_out = []
        for b in m.get("batches", []):
            cs = b.get("completion_status") or b.get("status", "")
            items = []
            for rid in b.get("rec_ids", []):
                rs = dict(rec_status.get(rid, {}))
                lc = rec_lifecycle.get(rid, {})
                if rid in applied_set:
                    rs["applied"] = True
                if lc.get("shipped"):
                    rs["shipped"] = True
                    rs["shipped_at"] = lc.get("shipped_at", "")
                    rs["shipped_tag"] = lc.get("shipped_tag", "")
                    rs["shipped_via"] = lc.get("shipped_via", "")
                if lc.get("implemented"):
                    rs["implemented"] = True
                    rs["implemented_at"] = lc.get("implemented_at", "")
                    rs["implemented_via"] = lc.get("implemented_via", "")
                items.append({
                    "rec_id": rid,
                    "title": rec_titles.get(rid, ""),
                    "kind": rec_kinds.get(rid, ""),
                    **rs,
                })
            batches_out.append({
                "index": b.get("index"),
                "status": cs,
                "rec_count": b.get("rec_count", len(items)),
                "priority_summary": b.get("priority_summary", ""),
                "started_at": b.get("started_at", ""),
                "completed_at": b.get("completed_at", ""),
                "dispatch_log": b.get("dispatch_log", ""),
                "rec_items": items,
            })
        chain_status = "completed"
        statuses = {b["status"] for b in batches_out}
        if "running" in statuses:
            chain_status = "running"
        elif "pending" in statuses:
            chain_status = "queued"
        elif "paused" in statuses and "completed" not in statuses:
            chain_status = "paused"
        site = ""
        for suffix in ("-seo-opportunity-agent", "-progressive-improvement-agent",
                        "-competitor-research-agent", "-catalog-audit-agent",
                        "-head-to-head-agent", "-article-author-agent"):
            if source_agent.endswith(suffix):
                site = source_agent[: -len(suffix)]
                break
        ts_for_sort = ""
        for b in reversed(batches_out):
            if b.get("started_at") or b.get("completed_at"):
                ts_for_sort = b.get("completed_at") or b.get("started_at")
                break
        return {
            "run_dir": "",
            "run_dir_basename": run_dir_basename,
            "dispatch_run_ts": dispatch_run_ts,
            "source_agent": source_agent,
            "site": site,
            "source_run_ts": source_run_ts,
            "batch_size": m.get("batch_size", 0),
            "total_recs": m.get("total_recs", 0),
            "chain_status": chain_status,
            "mtime": 0.0,
            "mtime_iso": ts_for_sort,
            "batches": batches_out,
        }

    # Parallel-stitch — 16 workers process the head keys concurrently.
    # Was sequential, ~10 blob reads × 50 chains × 50ms = 25s. With
    # concurrency 16: ~10 × 50 / 16 × 50ms ≈ 1.5s for the same data.
    with ThreadPoolExecutor(max_workers=16) as ex:
        for result in ex.map(_stitch_one, head_keys):
            if result is not None:
                manifests.append(result)

    manifests.sort(key=lambda mm: mm.get("mtime_iso", ""), reverse=True)
    _BATCHES_CACHE["data"] = manifests
    _BATCHES_CACHE["ts"] = now_mono
    return {"chains": manifests[:limit]}


@router.get("/batches/{run_dir_basename}/rec/{rec_id}")
def get_batch_rec_detail(run_dir_basename: str, rec_id: str):
    """Return the rec's recommendation entry + summary.md content if any.

    `run_dir_basename` is the synthetic key from /api/implementer/batches:
    `rundir-<source_agent>-<source_run_ts>` (no random suffix because the
    Azure-side data is keyed on the agent + run_ts pair).
    """
    if "/" in run_dir_basename or ".." in run_dir_basename or not run_dir_basename.startswith("rundir-"):
        return {"error": "invalid run_dir"}
    # Parse <agent>-<run_ts> from the basename. run_ts is `YYYYmmddTHHMMSSZ`.
    m = re.match(r"^rundir-(?P<agent>.+?)-(?P<ts>\d{8}T\d{6}Z)$", run_dir_basename)
    if not m:
        return {"error": "invalid run_dir basename"}
    source_agent = m.group("agent")
    source_run_ts = m.group("ts")
    s = get_storage()
    # Pull the rec's full record + its summary.md
    rec_obj: dict = {}
    try:
        rd = s.read_json(f"agents/{source_agent}/runs/{source_run_ts}/recommendations.json") or {}
        for r in rd.get("recommendations", []):
            if r.get("id") == rec_id:
                rec_obj = r
                break
    except Exception:
        pass
    summary = ""
    summary_key = f"agents/{source_agent}/runs/{source_run_ts}/changes/{rec_id}.summary.md"
    try:
        summary = s.read_text(summary_key) or ""
    except Exception:
        pass
    # Per-rec deep context bundle (framework.core.rec_context). Optional —
    # only present if the producing agent wrote one for this rec.
    context_bundle: dict | None = None
    try:
        from framework.core.rec_context import read_rec_context
        ctx = read_rec_context(source_agent, source_run_ts, rec_id, storage=s)
        if ctx is not None:
            context_bundle = ctx.to_dict()
    except Exception:
        pass
    return {
        "rec_id": rec_id,
        "rec": rec_obj,
        "summary_md": summary,
        "summary_key": summary_key if summary else "",
        "source_agent": source_agent,
        "source_run_ts": source_run_ts,
        "rec_context": context_bundle,
    }


@router.get("/batches/{run_dir_basename}/rec/{rec_id}/verification")
def get_rec_verification_script(run_dir_basename: str, rec_id: str):
    """Return the verification script for a shipped rec.

    The implementer (or a one-shot backfill) writes a per-rec verification
    JSON at ship time to:
      agents/<source_agent>/runs/<source_run_ts>/verifications/<rec_id>.json

    Document shape:
      {
        "rec_id": "...",
        "generated_at": "iso8601",
        "generated_by": "implementer" | "backfill",
        "explanation": "1-2 sentence plain-English description of what's being verified",
        "script_js": "async function verify({ proxyFetch }) { ... return { ok: bool, evidence: <any> }; }"
      }

    The dashboard's frontend fetches this doc + runs script_js in a
    Function() sandbox, passing in a proxyFetch helper that goes through
    POST /api/proxy/fetch (server-side fetch — bypasses CORS and lets the
    client inspect any production URL's response body).

    Returns 404 with explanation if no verification has been generated yet.
    """
    if "/" in run_dir_basename or ".." in run_dir_basename:
        raise HTTPException(status_code=400, detail="invalid run_dir")
    # Parse <agent>-<run_ts>
    m = re.match(r"^(?:rundir-)?(?P<agent>.+?)-(?P<ts>\d{8}T\d{6}Z)", run_dir_basename)
    if not m:
        raise HTTPException(status_code=400, detail="couldn't parse run_dir basename")
    source_agent = m.group("agent")
    source_run_ts = m.group("ts")
    s = get_storage()
    key = f"agents/{source_agent}/runs/{source_run_ts}/verifications/{rec_id}.json"
    try:
        doc = s.read_json(key)
    except Exception:
        doc = None
    # Generate-on-demand fallback — if the doc isn't there, look up the
    # rec in the source recommendations.json and let the framework's
    # verifications module produce one. Persist it for next time.
    # This means clicking "verify" never 404s as long as the rec exists.
    if not doc:
        try:
            rd = s.read_json(f"agents/{source_agent}/runs/{source_run_ts}/recommendations.json") or {}
            rec = next(
                (r for r in (rd.get("recommendations") or [])
                 if r.get("id") == rec_id),
                None,
            )
            if rec:
                from framework.core import verifications as _verifs
                site_hint = ""
                for prefix in ("specpicks-", "aisleprompt-", "reusable-agents-"):
                    if source_agent.startswith(prefix):
                        site_hint = prefix.rstrip("-")
                        break
                doc = _verifs.generate_and_persist(
                    source_agent=source_agent,
                    source_run_ts=source_run_ts,
                    rec=rec, site=site_hint,
                    generated_by="api-on-demand",
                    storage=s, overwrite=False,
                )
        except Exception:
            doc = None
    if not doc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "no verification script for this rec yet",
                "key_checked": key,
                "hint": "rec not found in source run, or framework couldn't generate a script for it",
            },
        )
    return doc


@router.get("/batches/{run_dir_basename}/rec/{rec_id}/__legacy_verify_disabled__")
def _legacy_verify_disabled(run_dir_basename: str, rec_id: str):
    """Disabled — replaced by /verification + the proxy_fetch endpoint.
    Kept as a stub to make sure no client still calls the old path."""
    return {"ok": False, "explanation": "deprecated; use /verification + /api/proxy/fetch from the dashboard"}


def _legacy_disabled_unused(run_dir_basename: str, rec_id: str):
    """Old per-rec-type recipe verification — replaced by stored
    verification scripts. Kept commented for reference."""
    if "/" in run_dir_basename or ".." in run_dir_basename:
        return {"ok": False, "explanation": "invalid run_dir"}
    m = re.match(r"^(?:rundir-)?(?P<agent>.+?)-(?P<ts>\d{8}T\d{6}Z)", run_dir_basename)
    if not m:
        return {"ok": False, "explanation": "couldn't parse run_dir"}
    source_agent = m.group("agent")
    source_run_ts = m.group("ts")
    s = get_storage()
    try:
        rd = s.read_json(f"agents/{source_agent}/runs/{source_run_ts}/recommendations.json") or {}
    except Exception as e:
        return {"ok": False, "explanation": f"couldn't load run: {e}"}
    rec = next((r for r in rd.get("recommendations", []) if r.get("id") == rec_id), None)
    if not rec:
        return {"ok": False, "explanation": f"rec {rec_id} not found in run"}

    import urllib.request
    typ = rec.get("type", "")
    title = rec.get("title", "")
    explanation_lines: list[str] = []
    cmd: str = ""
    output: str = ""
    ok = False

    def _http_get(url: str, timeout: float = 12.0) -> tuple[int, str]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "verify/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            return 0, str(e)

    # ── Article-author ────────────────────────────────────────────────
    if typ == "article-author-proposal":
        ap = rec.get("article_proposal") or {}
        slug = ap.get("slug") or rec.get("slug", "")
        if not slug:
            return {"ok": False, "command": "", "output": "",
                    "explanation": "rec has no slug — can't verify"}
        api_url = f"https://specpicks.com/api/testbench/articles/{slug}"
        public_url = f"https://specpicks.com/reviews/{slug}"
        cmd = f"curl -s {api_url} | head -c 500"
        code, body = _http_get(api_url)
        if code != 200:
            output = f"HTTP {code} from {api_url}"
            explanation_lines.append(f"❌ API returned HTTP {code} — article not served. The DB row may exist but the public site can't find it.")
        else:
            has_body = '"body_md"' in body
            has_slug = slug in body
            output = body[:500] + ("..." if len(body) > 500 else "")
            ok = has_body and has_slug
            explanation_lines.append(f"GET {api_url} → 200")
            explanation_lines.append(f"contains body_md: {has_body}")
            explanation_lines.append(f"contains slug: {has_slug}")
            if ok:
                explanation_lines.append(f"✅ Live at {public_url}")

    # ── SEO top5-target-page ──────────────────────────────────────────
    elif typ == "top5-target-page":
        m_q = re.search(r'for "([^"]+)"', title)
        query = m_q.group(1) if m_q else ""
        slug = re.sub(r'[^a-z0-9]+', '-', query.lower()).strip('-')
        # aisleprompt path; could expand for other sites later
        url = f"https://aisleprompt.com/recipes/q/{slug}"
        cmd = f"curl -s {url} | grep -oE '<title>[^<]+'"
        code, body = _http_get(url)
        if code != 200:
            output = f"HTTP {code}"
            explanation_lines.append(f"❌ HTTP {code} at {url} — target page is missing.")
        else:
            tm = re.search(r"<title>([^<]+)", body)
            page_title = tm.group(1) if tm else ""
            title_has_query = query.lower() in page_title.lower()
            has_faq = '"@type":"FAQPage"' in body or 'FAQPage' in body
            intro_has_query = query.lower() in body.lower()[:6000]
            output = f"<title>: {page_title[:120]}\nfaq_schema: {has_faq}\nintro_mentions_query: {intro_has_query}"
            ok = title_has_query and has_faq and intro_has_query
            explanation_lines.append(f"GET {url} → 200")
            explanation_lines.append(f"<title> contains \"{query}\": {title_has_query}")
            explanation_lines.append(f"FAQPage schema present: {has_faq}")
            explanation_lines.append(f"intro mentions query: {intro_has_query}")
            if ok:
                explanation_lines.append(f"✅ Keyword page is live and properly structured.")

    # ── SEO ctr-fix ───────────────────────────────────────────────────
    elif typ == "ctr-fix":
        m_url = re.search(r'on (https?://\S+?)(?:["\s]|$)', title)
        if not m_url:
            return {"ok": False, "command": "", "output": "",
                    "explanation": "rec title doesn't contain a target URL"}
        url = m_url.group(1).rstrip('"., ')
        cmd = f"curl -s {url} | grep -oE '<title>[^<]+'"
        code, body = _http_get(url)
        if code != 200:
            output = f"HTTP {code}"
            explanation_lines.append(f"❌ HTTP {code} at {url}.")
        else:
            tm = re.search(r"<title>([^<]+)", body)
            page_title = tm.group(1) if tm else ""
            # The recipe SHOULD have a non-generic, query-specific title
            # (the override should have replaced the stock recipe title)
            generic = ("AI-Powered Grocery" in page_title) or page_title.strip() == ""
            ok = (not generic) and len(page_title) < 70
            output = f"<title>: {page_title}"
            explanation_lines.append(f"GET {url} → 200")
            explanation_lines.append(f"<title>: {page_title}")
            explanation_lines.append(f"override-active (non-generic + <70 chars): {ok}")
            if ok:
                explanation_lines.append("✅ SERP snippet override is rendering on the live page.")

    # ── SEO internal-link ─────────────────────────────────────────────
    elif typ == "internal-link":
        m_q = re.search(r'"([^"]+)"', title)
        query = m_q.group(1) if m_q else ""
        slug = re.sub(r'[^a-z0-9]+', '-', query.lower()).strip('-')
        target_path = f"/best/best-{slug}"
        target_url = f"https://aisleprompt.com{target_path}"
        cmd = f"curl -s https://aisleprompt.com/ | grep -oE '{target_path}' && curl -s {target_url} -o /dev/null -w '%{{http_code}}'"
        # Check 1: homepage links to it
        code_hp, body_hp = _http_get("https://aisleprompt.com/")
        homepage_links = (target_path in body_hp) if code_hp == 200 else False
        # Check 2: target page exists
        code_t, body_t = _http_get(target_url)
        target_ok = (code_t == 200 and query.lower() in body_t.lower())
        output = f"homepage_has_link={homepage_links}\ntarget_page_status={code_t}\ntarget_contains_query={query.lower() in body_t.lower() if body_t else False}"
        ok = homepage_links and target_ok
        explanation_lines.append(f"homepage links to {target_path}: {homepage_links}")
        explanation_lines.append(f"target {target_url} → {code_t}")
        explanation_lines.append(f"target body mentions \"{query}\": {query.lower() in body_t.lower() if body_t else False}")
        if ok:
            explanation_lines.append("✅ Internal link is on the homepage and the target page is live.")

    # ── SEO article-snippet-rewrite / article-title-fix ───────────────
    elif typ in ("article-snippet-rewrite", "article-title-fix"):
        url = rec.get("url", "")
        if not url:
            return {"ok": False, "command": "", "output": "",
                    "explanation": "rec has no url field"}
        cmd = f"curl -s {url} | grep -oE '<title>[^<]+'"
        code, body = _http_get(url)
        ok = code == 200 and ("body_md" in body or len(body) > 1000)
        tm = re.search(r"<title>([^<]+)", body or "")
        page_title = tm.group(1) if tm else ""
        output = f"HTTP {code}\n<title>: {page_title[:120]}"
        explanation_lines.append(f"GET {url} → {code}")
        explanation_lines.append(f"page has content: {ok}")

    else:
        return {"ok": False, "command": "", "output": "",
                "explanation": f"no verification recipe for rec type={typ!r}"}

    return {
        "ok": bool(ok),
        "rec_id": rec_id,
        "rec_type": typ,
        "command": cmd,
        "output": output[:2000],
        "explanation": "\n".join(explanation_lines),
    }


@router.get("/batches/{run_dir_basename}/rec/{rec_id}/attachment/{name}")
def get_rec_context_attachment(run_dir_basename: str, rec_id: str, name: str):
    """Stream a single rec-context attachment back to the dashboard.

    Used by the rec drill-down modal when the user clicks an attachment
    link (HTML excerpts, supporting JSON, scraped competitor pages, etc).
    """
    from fastapi.responses import Response
    if "/" in run_dir_basename or ".." in run_dir_basename or not run_dir_basename.startswith("rundir-"):
        return Response(status_code=400, content=b"invalid run_dir")
    if "/" in name or ".." in name or name.startswith("."):
        return Response(status_code=400, content=b"invalid attachment name")
    m = re.match(r"^rundir-(?P<agent>.+?)-(?P<ts>\d{8}T\d{6}Z)$", run_dir_basename)
    if not m:
        return Response(status_code=400, content=b"invalid run_dir basename")
    source_agent = m.group("agent")
    source_run_ts = m.group("ts")
    s = get_storage()
    try:
        from framework.core.rec_context import read_rec_context
        ctx = read_rec_context(source_agent, source_run_ts, rec_id, storage=s)
        if ctx is None:
            return Response(status_code=404, content=b"no context")
        b = ctx.attachment_bytes(name, storage=s)
        if b is None:
            return Response(status_code=404, content=b"attachment not found")
        # Best-effort content-type
        ct = "application/octet-stream"
        nlow = name.lower()
        if nlow.endswith(".json"):     ct = "application/json"
        elif nlow.endswith(".html"):   ct = "text/html; charset=utf-8"
        elif nlow.endswith(".txt") or nlow.endswith(".md"): ct = "text/plain; charset=utf-8"
        elif nlow.endswith(".csv"):    ct = "text/csv"
        return Response(content=b, media_type=ct)
    except Exception as e:
        return Response(status_code=500, content=str(e).encode())


@router.get("/dispatches/{dispatch_id}/log")
def get_dispatch_log(dispatch_id: str, tail_bytes: int = Query(32768, le=262144)):
    """Return the content of a specific dispatch log."""
    if not _LOG_DIR.is_dir():
        return {"content": "", "size_bytes": 0, "status": "not_found"}
    # dispatch_id is "<site>-<run_ts>"
    for p in _LOG_DIR.iterdir():
        m = _DISPATCH_PAT.match(p.name)
        if not m:
            continue
        site, run_ts = m.group(1), m.group(2)
        if f"{site}-{run_ts}" == dispatch_id:
            stat = p.stat()
            try:
                if tail_bytes and stat.st_size > tail_bytes:
                    with p.open("rb") as f:
                        f.seek(-tail_bytes, 2)
                        content = f.read().decode("utf-8", errors="replace")
                else:
                    content = p.read_text(errors="replace")
            except Exception as e:
                content = f"[error reading log: {e}]"
            parsed = _parse_dispatch_log(p)
            age_s = datetime.now(timezone.utc).timestamp() - stat.st_mtime
            running = (not parsed["done"]) and age_s < 300
            return {
                "id": dispatch_id,
                "site": site,
                "run_ts": run_ts,
                "content": content,
                "size_bytes": stat.st_size,
                "status": "running" if running else "completed",
                **parsed,
            }
    return {"content": "", "size_bytes": 0, "status": "not_found", "id": dispatch_id}


# ── Proxy fetch — lets verification scripts in the dashboard inspect
#                  production URLs without browser CORS blocking. The
#                  client passes a target URL; the server fetches it and
#                  returns the response. Token-gated like every other
#                  API endpoint. Hard cap on URL allow-list for safety.

class _ProxyFetchRequest(BaseModel):
    url: str
    method: str = "GET"
    timeout_s: float = 12.0
    max_bytes: int = 200_000


# Allow-list of hostnames the proxy will fetch. Keeps this from being
# turned into an open SSRF gateway. Add new hosts as new sites come online.
_PROXY_ALLOWED_HOSTS = {
    "specpicks.com", "www.specpicks.com",
    "aisleprompt.com", "www.aisleprompt.com",
    # Add others (e.g. aislepromptstaging.com) as they're set up.
}


@router.post("/proxy/fetch")
def proxy_fetch(body: _ProxyFetchRequest):
    """Server-side fetch on behalf of dashboard JS so verification
    scripts can inspect production HTML/JSON without browser CORS.

    Constrained:
      - URL host must be on the allow-list (no open SSRF)
      - Method must be GET / HEAD (no writes)
      - Body capped at max_bytes (default 200KB)
      - Timeout capped at 30s
    """
    from urllib.parse import urlparse
    import urllib.request, urllib.error

    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"unsupported scheme: {parsed.scheme}")
    if parsed.hostname not in _PROXY_ALLOWED_HOSTS:
        raise HTTPException(status_code=400,
            detail=f"host not on proxy allow-list: {parsed.hostname}")
    if body.method.upper() not in ("GET", "HEAD"):
        raise HTTPException(status_code=400, detail="only GET/HEAD allowed")
    timeout = max(1.0, min(30.0, float(body.timeout_s)))
    max_bytes = max(1024, min(2_000_000, int(body.max_bytes)))

    try:
        req = urllib.request.Request(
            body.url, method=body.method.upper(),
            headers={"User-Agent": "reusable-agents/verify-proxy/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read(max_bytes).decode("utf-8", errors="replace")
            return {
                "ok": True,
                "url": body.url,
                "status": resp.status,
                "headers": {k: v for k, v in resp.getheaders()[:30]},
                "body": content,
                "truncated": len(content) >= max_bytes,
            }
    except urllib.error.HTTPError as e:
        try:
            content = e.read(max_bytes).decode("utf-8", errors="replace")
        except Exception:
            content = ""
        return {
            "ok": False,
            "url": body.url,
            "status": e.code,
            "headers": {},
            "body": content,
            "truncated": False,
            "error": str(e),
        }
    except Exception as e:
        return {
            "ok": False, "url": body.url, "status": 0,
            "headers": {}, "body": "", "truncated": False,
            "error": str(e)[:200],
        }
