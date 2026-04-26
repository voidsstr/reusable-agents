"""Storage browser — read-only proxy so the UI can render an agent's
storage hierarchy without holding the connection string itself.

Every read goes through the framework's StorageBackend, so it works
identically against Azure Blob and LocalFilesystem.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response

from framework.core.storage import get_storage

from ..auth import require_token


router = APIRouter(prefix="/api/storage", tags=["storage"], dependencies=[Depends(require_token)])


@router.get("/list")
def list_at(prefix: str = "", limit: int = Query(500, le=10000)):
    """List blob keys under a prefix. Empty prefix lists from root."""
    s = get_storage()
    keys = s.list_prefix(prefix.lstrip("/"), limit=limit)
    return {"prefix": prefix, "keys": keys, "count": len(keys)}


@router.get("/read")
def read(key: str, format: str = "auto"):
    """Read a single blob. format=auto picks based on extension."""
    s = get_storage()
    if not s.exists(key):
        raise HTTPException(status_code=404, detail="key not found")

    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    if format == "auto":
        if ext == "json":
            data = s.read_json(key)
            return {"key": key, "format": "json", "content": data}
        if ext == "jsonl":
            return {"key": key, "format": "jsonl", "content": s.read_jsonl(key)}
        if ext in ("md", "txt", "html"):
            return PlainTextResponse(s.read_text(key) or "")
        # Fall through to bytes for everything else
    if format == "text":
        return PlainTextResponse(s.read_text(key) or "")
    if format == "bytes":
        b = s.read_bytes(key)
        if b is None:
            raise HTTPException(status_code=404, detail="key not found")
        return Response(content=b, media_type="application/octet-stream")
    if format == "json":
        return {"key": key, "format": "json", "content": s.read_json(key)}
    if format == "jsonl":
        return {"key": key, "format": "jsonl", "content": s.read_jsonl(key)}
    raise HTTPException(status_code=400, detail=f"unknown format {format!r}")


@router.get("/exists")
def exists(key: str):
    return {"key": key, "exists": get_storage().exists(key)}
