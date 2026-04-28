"""Storage backend abstraction for the reusable-agents framework.

Supports Azure Blob Storage (production) and Local Filesystem (tests / dev).

Conventions:
- All blob keys use forward slashes; the framework treats them as a
  hierarchical path even though Azure Blob is technically a flat namespace.
- All JSON read/write methods round-trip via UTF-8.
- Helpers `read_json`, `write_json`, `read_text`, `write_text`, `append_jsonl`,
  `list_prefix`, `delete`, `exists`, `read_bytes`, `write_bytes` are stable
  contracts every backend must implement.
- The framework rate-limits writes from agents (e.g., status.json) to avoid
  blob churn; the backend itself does not throttle.

Default backend selection:
  STORAGE_BACKEND env var = "azure" (default) or "local"
  AZURE_STORAGE_CONNECTION_STRING for Azure
  AGENT_STORAGE_LOCAL_PATH for Local (defaults to ~/.reusable-agents/storage)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


logger = logging.getLogger("framework.storage")


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class StorageBackend(ABC):
    """Common interface for any storage backend."""

    name: str  # 'azure' | 'local'

    @abstractmethod
    def read_bytes(self, key: str) -> Optional[bytes]:
        """Return the raw bytes at `key`, or None if not found."""

    @abstractmethod
    def write_bytes(self, key: str, data: bytes,
                    cache_control: Optional[str] = None) -> None:
        """Write bytes at `key`. Overwrites if exists.

        `cache_control` (optional) sets the HTTP Cache-Control response
        header on backends that support it (Azure Blob). Local FS ignores."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete `key`. Returns True if it existed."""

    @abstractmethod
    def list_prefix(self, prefix: str, limit: int = 10000) -> list[str]:
        """List keys with the given prefix. Returns full keys (not basenames)."""

    @abstractmethod
    def append_bytes(self, key: str, data: bytes) -> None:
        """Append-only write — used for jsonl logs (decisions, events, changelog)."""

    # ---- Convenience helpers (concrete; build on the abstract methods) ----

    def read_text(self, key: str, encoding: str = "utf-8") -> Optional[str]:
        b = self.read_bytes(key)
        return b.decode(encoding) if b is not None else None

    def write_text(self, key: str, text: str, encoding: str = "utf-8",
                   cache_control: Optional[str] = None) -> None:
        self.write_bytes(key, text.encode(encoding), cache_control=cache_control)

    def read_json(self, key: str) -> Optional[Any]:
        b = self.read_bytes(key)
        if b is None:
            return None
        try:
            return json.loads(b.decode("utf-8"))
        except json.JSONDecodeError as e:
            logger.warning(f"read_json: invalid JSON at {key}: {e}")
            return None

    def write_json(self, key: str, obj: Any, indent: int = 2,
                   cache_control: Optional[str] = None) -> None:
        self.write_bytes(
            key, json.dumps(obj, indent=indent, default=str).encode("utf-8"),
            cache_control=cache_control,
        )

    def append_jsonl(self, key: str, obj: Any) -> None:
        self.append_bytes(key, (json.dumps(obj, default=str) + "\n").encode("utf-8"))

    def read_jsonl(self, key: str) -> list[Any]:
        b = self.read_bytes(key)
        if b is None:
            return []
        out = []
        for line in b.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def list_basenames(self, prefix: str, limit: int = 10000) -> list[str]:
        """Return the last path-component of each key under `prefix`."""
        prefix_norm = prefix.rstrip("/") + "/"
        return [k[len(prefix_norm):].split("/", 1)[0]
                for k in self.list_prefix(prefix_norm, limit=limit)
                if k.startswith(prefix_norm)]

    @contextmanager
    def lock(self, key: str, timeout_s: float = 30.0) -> Iterator[bool]:
        """Best-effort advisory lock. Backends override; default is a no-op."""
        yield True


# ---------------------------------------------------------------------------
# Azure Blob backend
# ---------------------------------------------------------------------------

class AzureBlobStorage(StorageBackend):
    """Azure Blob Storage backend.

    Uses the azure-storage-blob SDK. Connection string from
    AZURE_STORAGE_CONNECTION_STRING; container name from
    AZURE_STORAGE_CONTAINER (default 'agents').

    Append-blob support: jsonl files are stored as Append Blobs to enable
    server-side append (no read-modify-write churn). Other files use
    standard Block Blobs.
    """

    name = "azure"

    def __init__(
        self,
        connection_string: Optional[str] = None,
        container_name: Optional[str] = None,
    ):
        try:
            from azure.storage.blob import BlobServiceClient, ContentSettings  # noqa
        except ImportError as e:
            raise SystemExit(
                "azure-storage-blob not installed. Install with: "
                "pip install azure-storage-blob"
            ) from e
        self._BlobServiceClient = BlobServiceClient
        self._ContentSettings = ContentSettings

        cs = connection_string or os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        if not cs:
            raise SystemExit(
                "AZURE_STORAGE_CONNECTION_STRING is not set. "
                "Either set the env var or pass connection_string=..."
            )
        self._service = BlobServiceClient.from_connection_string(cs)
        self._container_name = container_name or os.getenv("AZURE_STORAGE_CONTAINER", "agents")
        self._container = self._service.get_container_client(self._container_name)
        # Best-effort container create — idempotent.
        try:
            self._container.create_container()
            logger.info(f"created blob container '{self._container_name}'")
        except Exception:
            pass  # Already exists or no permission to create — both are fine.

    def _blob(self, key: str):
        return self._container.get_blob_client(key)

    def read_bytes(self, key: str) -> Optional[bytes]:
        try:
            return self._blob(key).download_blob().readall()
        except Exception as e:
            # ResourceNotFoundError is the common case
            if "ResourceNotFound" in type(e).__name__ or "BlobNotFound" in str(e):
                return None
            logger.warning(f"azure read_bytes {key}: {e}")
            return None

    def write_bytes(self, key: str, data: bytes,
                    cache_control: Optional[str] = None) -> None:
        cs_kwargs = {"content_type": _guess_content_type(key)}
        if cache_control:
            cs_kwargs["cache_control"] = cache_control
        self._blob(key).upload_blob(
            data, overwrite=True,
            content_settings=self._ContentSettings(**cs_kwargs),
        )

    def append_bytes(self, key: str, data: bytes) -> None:
        # Use AppendBlob for server-side append (no read-modify-write).
        from azure.storage.blob import BlobType  # noqa
        blob = self._blob(key)
        try:
            blob.append_block(data)
        except Exception:
            # First write — create as AppendBlob then append.
            try:
                blob.create_append_blob(content_settings=self._ContentSettings(
                    content_type="application/x-ndjson",
                ))
                blob.append_block(data)
            except Exception as e:
                # Existing blob is wrong type; re-create.
                if "InvalidBlobType" in str(e):
                    blob.delete_blob()
                    blob.create_append_blob()
                    blob.append_block(data)
                else:
                    raise

    def exists(self, key: str) -> bool:
        try:
            self._blob(key).get_blob_properties()
            return True
        except Exception:
            return False

    def delete(self, key: str) -> bool:
        try:
            self._blob(key).delete_blob()
            return True
        except Exception:
            return False

    def list_prefix(self, prefix: str, limit: int = 10000) -> list[str]:
        out: list[str] = []
        for blob in self._container.list_blobs(name_starts_with=prefix):
            out.append(blob.name)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# Local filesystem backend (for tests / dev)
# ---------------------------------------------------------------------------

class LocalFilesystemStorage(StorageBackend):
    """Local filesystem backend. Each key becomes a file path under root_path.

    Useful for tests and offline dev. Keys may contain '/' which become
    real subdirectories.
    """

    name = "local"

    def __init__(self, root_path: Optional[str] = None):
        self.root = Path(root_path or os.getenv(
            "AGENT_STORAGE_LOCAL_PATH", os.path.expanduser("~/.reusable-agents/storage")
        )).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_dir = self.root / ".locks"
        self._lock_dir.mkdir(exist_ok=True)
        self._lock_lock = threading.Lock()

    def _path(self, key: str) -> Path:
        # Reject path traversal up-front
        norm = key.lstrip("/")
        if ".." in norm.split("/"):
            raise ValueError(f"key contains '..': {key!r}")
        return (self.root / norm).resolve()

    def read_bytes(self, key: str) -> Optional[bytes]:
        p = self._path(key)
        if not p.is_file():
            return None
        return p.read_bytes()

    def write_bytes(self, key: str, data: bytes,
                    cache_control: Optional[str] = None) -> None:
        # cache_control is a no-op on local FS (no HTTP serving layer).
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — write to .tmp then rename
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def append_bytes(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("ab") as f:
            f.write(data)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> bool:
        p = self._path(key)
        if not p.exists():
            return False
        if p.is_dir():
            import shutil
            shutil.rmtree(p)
        else:
            p.unlink()
        return True

    def list_prefix(self, prefix: str, limit: int = 10000) -> list[str]:
        # If prefix ends with '/', list everything under that directory.
        # Otherwise, treat as a string prefix on filenames within parent.
        out: list[str] = []
        if prefix.endswith("/"):
            base = self._path(prefix.rstrip("/"))
            if not base.is_dir():
                return []
            for child in base.rglob("*"):
                if child.is_file():
                    rel = child.relative_to(self.root).as_posix()
                    out.append(rel)
                    if len(out) >= limit:
                        break
        else:
            base = self._path(prefix).parent
            if not base.is_dir():
                return []
            stem = self._path(prefix).name
            for child in base.iterdir():
                if child.name.startswith(stem) and child.is_file():
                    out.append(child.relative_to(self.root).as_posix())
                    if len(out) >= limit:
                        break
        return sorted(out)

    @contextmanager
    def lock(self, key: str, timeout_s: float = 30.0) -> Iterator[bool]:
        """File-based advisory lock via lockfile creation."""
        lock_path = self._lock_dir / (key.replace("/", "_") + ".lock")
        deadline = time.monotonic() + timeout_s
        acquired = False
        while time.monotonic() < deadline:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.1)
        try:
            yield acquired
        finally:
            if acquired:
                try: lock_path.unlink()
                except FileNotFoundError: pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_DEFAULT_BACKEND: Optional[StorageBackend] = None
_BACKEND_LOCK = threading.Lock()


def get_storage(backend: Optional[str] = None) -> StorageBackend:
    """Return a process-wide cached storage backend.

    Backend selection (in priority order):
      1. `backend` parameter ('azure' | 'local')
      2. STORAGE_BACKEND env var
      3. Default: 'azure' if AZURE_STORAGE_CONNECTION_STRING is set, else 'local'
    """
    global _DEFAULT_BACKEND
    with _BACKEND_LOCK:
        if _DEFAULT_BACKEND is not None and backend is None:
            return _DEFAULT_BACKEND
        choice = backend or os.getenv("STORAGE_BACKEND")
        if not choice:
            choice = "azure" if os.getenv("AZURE_STORAGE_CONNECTION_STRING") else "local"
        if choice == "azure":
            inst: StorageBackend = AzureBlobStorage()
        elif choice == "local":
            inst = LocalFilesystemStorage()
        else:
            raise SystemExit(f"unknown STORAGE_BACKEND={choice!r}")
        if backend is None:
            _DEFAULT_BACKEND = inst
        logger.info(f"storage backend: {inst.name}")
        return inst


def reset_default_backend() -> None:
    """Test helper — clear the cached backend."""
    global _DEFAULT_BACKEND
    with _BACKEND_LOCK:
        _DEFAULT_BACKEND = None


def _guess_content_type(key: str) -> str:
    if key.endswith(".json"):
        return "application/json"
    if key.endswith(".jsonl"):
        return "application/x-ndjson"
    if key.endswith(".md"):
        return "text/markdown; charset=utf-8"
    if key.endswith(".html"):
        return "text/html; charset=utf-8"
    if key.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"
