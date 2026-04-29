"""Implementer dispatch queue — lists pending responses-queue items and
recent dispatch log files so the ImplementerQueue UI page can show what's
been queued, what's running, and what recently completed."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query

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


@router.get("/batches")
def list_batches(limit: int = Query(20, le=50)):
    """List active + recent batched dispatch chains.

    A "batch chain" is a single user reply that the responder split into
    N batches (sorted high→low priority) — recorded as
    `<run_dir>/dispatch-batches.json` and dispatched sequentially by the
    implementer's auto-chain logic.

    Reads from BOTH:
      - Azure storage at agents/<source-agent>/runs/<run_ts>/dispatch-batches.json
        (synced by the implementer's sync_back trap)
      - /tmp/rundir-*/dispatch-batches.json on the host where the API
        runs (only when the API runs on the same host as the implementer
        — Docker deployments only see Azure)
    """
    from glob import glob
    import time

    s = get_storage()
    manifests: list[dict] = []
    seen_keys: set[str] = set()

    # ── Azure: walk every <agent>/runs/<run_ts>/dispatch-batches.json ──
    try:
        for blob_key in s.list_prefix("agents/"):
            if not blob_key.endswith("/dispatch-batches.json"):
                continue
            if blob_key in seen_keys:
                continue
            seen_keys.add(blob_key)
            try:
                m = s.read_json(blob_key)
            except Exception:
                continue
            if not m or not m.get("batches"):
                continue
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
            # Load rec titles + lifecycle markers from BOTH:
            #   - source run's recommendations.json (titles, priority, base state)
            #   - dispatch run's recommendations.json (overrides — implementer/
            #     deployer write `implemented`, `implemented_via`, `shipped` here)
            # Dispatch-run state wins where present so claude's
            # `implemented_via: pre-existing` markers and the deployer's
            # `shipped: true` propagate to the UI.
            rec_titles: dict[str, str] = {}
            rec_kinds: dict[str, str] = {}
            rec_lifecycle: dict[str, dict] = {}  # rid -> {implemented, implemented_via, shipped, ...}
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
                            rec_titles[rid] = (
                                r.get("title") or r.get("description") or ""
                            )[:200]
                            for f in ("priority", "severity", "tier"):
                                if r.get(f):
                                    rec_kinds[rid] = str(r.get(f))
                                    break
                        # Lifecycle markers — last writer wins (so dispatch
                        # run's updates override source's defaults).
                        for f in ("implemented", "implemented_at", "implemented_run_ts",
                                  "implemented_via", "implemented_commit",
                                  "shipped", "shipped_at",
                                  "shipped_tag", "shipped_image", "shipped_via"):
                            if f in r:
                                rec_lifecycle.setdefault(rid, {})[f] = r[f]
                except Exception:
                    pass
            # Load per-rec changes/<rec>.summary.md — implementer writes
            # them under the DISPATCH run_ts (not source).
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
            # applied-recs.json under the dispatch run_ts (DB-write evidence)
            applied_set: set[str] = set()
            try:
                ar = s.read_json(f"agents/{source_agent}/runs/{dispatch_run_ts}/applied-recs.json")
                if ar:
                    ids = ar.get("applied_rec_ids") or ar.get("rec_ids") or []
                    if isinstance(ids, list):
                        applied_set = {str(x) for x in ids if x}
            except Exception:
                pass
            # Stitch
            batches_out = []
            for b in m.get("batches", []):
                cs = b.get("completion_status") or b.get("status", "")
                items = []
                for rid in b.get("rec_ids", []):
                    rs = dict(rec_status.get(rid, {}))
                    lc = rec_lifecycle.get(rid, {})
                    if rid in applied_set:
                        rs["applied"] = True
                    # Two-stage lifecycle. Order of precedence (highest first):
                    #   shipped       — deployer pushed this rec live to Azure
                    #   implemented   — claude committed code OR marked
                    #                   implemented_via: pre-existing
                    #   deferred      — summary file says DEFERRED/SKIP
                    #   pending       — anything else
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
            # Use the latest started/completed_at for sort
            ts_for_sort = ""
            for b in reversed(batches_out):
                if b.get("started_at") or b.get("completed_at"):
                    ts_for_sort = b.get("completed_at") or b.get("started_at")
                    break
            manifests.append({
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
            })
    except Exception:
        pass

    # Sort by latest activity (mtime_iso descending — chains with active
    # work first, then by recency).
    manifests.sort(key=lambda mm: mm.get("mtime_iso", ""), reverse=True)
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
