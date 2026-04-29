"""Per-recommendation deep context — framework convention.

`recommendations.json` already carries every rec's title, description,
rationale, evidence, fix, priority, and `data_refs` (paths into the run
dir). For most recs that's enough for the implementer.

But some agents need to surface richer supporting material per rec —
full page HTML excerpts, side-by-side comparisons, related queries,
sample DB rows, scraped competitor pages, etc. Cramming that into the
recommendations.json `evidence` string makes the JSON huge and mixes
human-readable summary with implementer-only artifacts.

This module formalizes a per-rec context bucket:

    agents/<source-agent>/runs/<run_ts>/rec-context/<rec-id>/
        context.json             # {"rec_id", "kind", "summary", "fields": {...}, "attachments": [...]}
        attachments/<filename>   # arbitrary supporting blobs (HTML, JSON, etc.)

The producing agent calls `write_rec_context()` once per rec it wants
to give the implementer extra material on. The implementer (or any
downstream consumer) calls `read_rec_context()` to get a typed bundle
including any attachment files materialized to a local tempdir.

Usage from a producing agent (e.g., SEO analyzer)
-------------------------------------------------

    from framework.core.rec_context import write_rec_context

    write_rec_context(
        agent_id="aisleprompt-seo-opportunity-agent",
        run_ts=run_ts,
        rec_id="rec-007",
        kind="seo-page-edit",
        summary="Recipe page lacks FAQ schema and conversational intro.",
        fields={
            "url": "https://aisleprompt.com/recipes/foo-bar",
            "current_title_tag": "Foo Bar Recipe",
            "competing_pages": [...],
            "suggested_faq_questions": [...],
        },
        attachments={
            "page.html":         page_html_str,
            "competitor.html":   competitor_html_str,
            "gsc-data.json":     json.dumps({"queries": [...]}),
        },
    )

Usage from the implementer
--------------------------

    from framework.core.rec_context import read_rec_context

    ctx = read_rec_context(source_agent, run_ts, rec_id)
    if ctx:
        page_html = ctx.attachment_text("page.html")
        suggested = ctx.fields.get("suggested_faq_questions", [])
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from framework.core.storage import get_storage, StorageBackend


logger = logging.getLogger("framework.core.rec_context")


_CTX_DIR_TPL = "agents/{agent_id}/runs/{run_ts}/rec-context/{rec_id}"
_CTX_FILE_TPL = "agents/{agent_id}/runs/{run_ts}/rec-context/{rec_id}/context.json"
_ATTACH_TPL = "agents/{agent_id}/runs/{run_ts}/rec-context/{rec_id}/attachments/{name}"


@dataclass
class RecContext:
    """Materialized rec context — what `read_rec_context` returns."""
    rec_id: str
    kind: str = ""
    summary: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)
    # The agent_id + run_ts used to load it (so callers can pass to
    # downstream tools that need to fetch attachments lazily).
    agent_id: str = ""
    run_ts: str = ""
    # Cached attachment bytes, populated lazily by attachment_bytes()
    _attach_cache: dict[str, bytes] = field(default_factory=dict)

    def attachment_bytes(self, name: str, storage: Optional[StorageBackend] = None) -> Optional[bytes]:
        """Fetch one attachment by filename. Caches in-memory after first read."""
        if name in self._attach_cache:
            return self._attach_cache[name]
        if name not in self.attachments:
            return None
        s = storage or get_storage()
        key = _ATTACH_TPL.format(agent_id=self.agent_id, run_ts=self.run_ts,
                                  rec_id=self.rec_id, name=name)
        try:
            data = s.read_bytes(key)
        except Exception as e:
            logger.warning(f"rec_context: failed to read attachment {key}: {e}")
            return None
        if data is not None:
            self._attach_cache[name] = data
        return data

    def attachment_text(self, name: str, storage: Optional[StorageBackend] = None) -> Optional[str]:
        b = self.attachment_bytes(name, storage=storage)
        if b is None:
            return None
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("utf-8", errors="replace")

    def to_dict(self) -> dict:
        return {
            "rec_id": self.rec_id,
            "kind": self.kind,
            "summary": self.summary,
            "fields": self.fields,
            "attachments": list(self.attachments),
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
        }


def write_rec_context(
    *,
    agent_id: str,
    run_ts: str,
    rec_id: str,
    kind: str = "",
    summary: str = "",
    fields: Optional[dict[str, Any]] = None,
    attachments: Optional[dict[str, bytes | str]] = None,
    storage: Optional[StorageBackend] = None,
) -> str:
    """Write a per-rec context bundle to storage. Returns the context.json key.

    Parameters
    ----------
    agent_id : str
        The producing agent's id (e.g. "aisleprompt-seo-opportunity-agent").
    run_ts : str
        The run timestamp the rec belongs to.
    rec_id : str
        Recommendation id (e.g. "rec-007").
    kind : str, optional
        Free-form tag for downstream routing — common values:
        "seo-page-edit", "catalog-row-fix", "h2h-commentary", etc.
    summary : str, optional
        Short human-readable line shown in the dashboard's rec drill-down.
    fields : dict, optional
        Structured data the implementer should consume. Anything JSON-serializable.
    attachments : dict[name -> bytes|str], optional
        Supporting files. Strings are encoded as UTF-8.

    Returns the storage key of the written context.json. Idempotent —
    re-writing replaces the previous context.
    """
    if not (agent_id and run_ts and rec_id):
        raise ValueError("agent_id, run_ts, and rec_id are required")
    s = storage or get_storage()
    fields = dict(fields or {})
    atts = attachments or {}
    # Write attachments first so context.json reflects what's actually present
    for name, data in atts.items():
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"invalid attachment name: {name!r}")
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"attachment {name!r} must be bytes or str, got {type(data)}")
        s.write_bytes(
            _ATTACH_TPL.format(agent_id=agent_id, run_ts=run_ts, rec_id=rec_id, name=name),
            bytes(data),
        )
    # Write context.json last (acts as a manifest)
    ctx = {
        "schema_version": "1",
        "rec_id": rec_id,
        "kind": kind,
        "summary": summary,
        "fields": fields,
        "attachments": sorted(atts.keys()),
    }
    key = _CTX_FILE_TPL.format(agent_id=agent_id, run_ts=run_ts, rec_id=rec_id)
    s.write_json(key, ctx)
    return key


def read_rec_context(
    agent_id: str,
    run_ts: str,
    rec_id: str,
    storage: Optional[StorageBackend] = None,
) -> Optional[RecContext]:
    """Load a per-rec context bundle. Returns None if no context was written."""
    if not (agent_id and run_ts and rec_id):
        return None
    s = storage or get_storage()
    key = _CTX_FILE_TPL.format(agent_id=agent_id, run_ts=run_ts, rec_id=rec_id)
    try:
        data = s.read_json(key)
    except Exception:
        data = None
    if not data:
        return None
    return RecContext(
        rec_id=data.get("rec_id", rec_id),
        kind=data.get("kind", ""),
        summary=data.get("summary", ""),
        fields=data.get("fields") or {},
        attachments=list(data.get("attachments") or []),
        agent_id=agent_id,
        run_ts=run_ts,
    )


def list_rec_contexts(
    agent_id: str,
    run_ts: str,
    storage: Optional[StorageBackend] = None,
) -> list[str]:
    """List rec_ids that have a context bundle for this run."""
    s = storage or get_storage()
    prefix = f"agents/{agent_id}/runs/{run_ts}/rec-context/"
    rec_ids: set[str] = set()
    try:
        for key in s.list_prefix(prefix):
            if not key.endswith("/context.json"):
                continue
            rest = key[len(prefix):]  # "<rec-id>/context.json"
            rid = rest.split("/", 1)[0]
            if rid:
                rec_ids.add(rid)
    except Exception:
        pass
    return sorted(rec_ids)


def materialize_rec_context_to_dir(
    agent_id: str,
    run_ts: str,
    rec_id: str,
    target_dir: str,
    storage: Optional[StorageBackend] = None,
) -> Optional[str]:
    """Materialize the rec's context.json + every attachment into a local
    directory so the implementer (which uses a real-FS toolchain like
    `claude --print` reading file paths) can access them as ordinary files.

    Returns the materialized directory path on success, or None when no
    context bundle exists.
    """
    import os
    ctx = read_rec_context(agent_id, run_ts, rec_id, storage=storage)
    if ctx is None:
        return None
    s = storage or get_storage()
    out_dir = os.path.join(target_dir, "rec-context", rec_id)
    os.makedirs(os.path.join(out_dir, "attachments"), exist_ok=True)
    # context.json
    with open(os.path.join(out_dir, "context.json"), "w") as f:
        json.dump(ctx.to_dict(), f, indent=2)
    # attachments
    for name in ctx.attachments:
        b = ctx.attachment_bytes(name, storage=s)
        if b is None:
            continue
        with open(os.path.join(out_dir, "attachments", name), "wb") as f:
            f.write(b)
    return out_dir
