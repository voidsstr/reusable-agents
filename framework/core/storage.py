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

    def list_child_prefixes(self, prefix: str) -> list[str]:
        """Immediate child "directory" names under `prefix`, without listing
        every blob beneath them.

        Why this exists: list_prefix() caps at `limit` keys and returns them
        LEXICOGRAPHICALLY, so a caller enumerating an agent's run history hits
        the cap and silently gets the OLDEST slice. Observed 2026-08-14:
        specpicks-seo-opportunity-agent has >10000 blobs under runs/, so
        peer_runs.latest_run_ts() returned 20260511T060000Z -- three months
        stale -- and reported it as current. Truncation that yields wrong-but-
        plausible data is worse than an error.

        Default implementation derives the children from list_prefix and is
        therefore still cap-bound; backends that can enumerate delimiters
        natively override it and are not. Callers that need correctness over
        a large history should prefer this over list_prefix.
        """
        seen: set[str] = set()
        for key in self.list_prefix(prefix) or []:
            rest = key[len(prefix):] if key.startswith(prefix) else ""
            head = rest.split("/", 1)[0]
            if head and "/" in rest:
                seen.add(head)
        return sorted(seen)

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

    def read_bytes_range(self, key: str, offset: int, length: Optional[int] = None) -> Optional[bytes]:
        """Read a byte range. Negative offset is interpreted as "last N bytes"
        (Python slice convention). Used for tail-window reads of growing
        append-only logs (events.jsonl, fixes-log.jsonl, etc.) so we don't
        pull megabytes when we only need the most recent records."""
        try:
            blob = self._blob(key)
            # Get blob size first if we need to convert negative offset
            if offset < 0 or length is None:
                props = blob.get_blob_properties()
                total = props.size
                if offset < 0:
                    offset = max(0, total + offset)
                if length is None:
                    length = total - offset
            stream = blob.download_blob(offset=offset, length=length)
            return stream.readall()
        except Exception as e:
            if "ResourceNotFound" in type(e).__name__ or "BlobNotFound" in str(e):
                return None
            logger.warning(f"azure read_bytes_range {key}@{offset}+{length}: {e}")
            return None

    def write_bytes(self, key: str, data: bytes,
                    cache_control: Optional[str] = None) -> None:
        cs_kwargs = {"content_type": _guess_content_type(key)}
        if cache_control:
            cs_kwargs["cache_control"] = cache_control
        blob = self._blob(key)
        try:
            blob.upload_blob(
                data, overwrite=True,
                content_settings=self._ContentSettings(**cs_kwargs),
            )
        except Exception as e:
            # Existing blob is the wrong type (e.g. append blob written
            # by streaming logger at the same key). Delete + retry as
            # block blob. Mirrors the symmetric handler in append_bytes.
            if "InvalidBlobType" in str(e):
                blob.delete_blob()
                blob.upload_blob(
                    data, overwrite=True,
                    content_settings=self._ContentSettings(**cs_kwargs),
                )
            else:
                raise

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

    def list_child_prefixes(self, prefix: str) -> list[str]:
        """Delimiter walk — returns child dirs only, so it is NOT subject to
        list_prefix's key cap. See StorageBackend.list_child_prefixes."""
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        out: list[str] = []
        for item in self._container.walk_blobs(name_starts_with=prefix, delimiter="/"):
            name = getattr(item, "name", "") or ""
            if name.endswith("/"):
                child = name[len(prefix):].rstrip("/")
                if child:
                    out.append(child)
        return sorted(out)

    @contextmanager
    def lock(self, key: str, timeout_s: float = 30.0) -> Iterator[bool]:
        """Multi-host advisory lock backed by Azure Blob Leases.

        Creates a tiny sentinel blob at `<key>.lock` and acquires a
        60-second renewable lease on it. The lease auto-releases when
        the process exits (Azure breaks idle leases after the lease
        duration elapses without a renewal).

        Why this exists: the framework runs on a single host today,
        and `framework.core.locks.FileLock` (fcntl-based) covers all
        intra-host races. If we ever scale to multiple framework hosts
        (e.g. an HA pair for the dashboard, or per-cloud workers), the
        fcntl lock becomes useless — only Azure-side coordination
        prevents races. This method is the multi-host fallback.

        Yields True on acquisition, False on timeout. Lease is released
        in the finally block. On process crash, the lease expires
        naturally after 60s (no manual cleanup needed).
        """
        from azure.core.exceptions import HttpResponseError, ResourceExistsError
        lock_blob_name = f"{key}.lock"
        lock_blob = self._container.get_blob_client(lock_blob_name)
        # Ensure the sentinel exists. Idempotent — ignore if it's
        # already there. Use a tiny payload so we don't pay storage.
        try:
            lock_blob.upload_blob(
                b"lock", overwrite=False,
                content_settings=self._ContentSettings(content_type="application/octet-stream"),
            )
        except ResourceExistsError:
            pass
        except Exception:
            # Best-effort — if create fails for any other reason we
            # still attempt the lease. The lease itself surfaces real
            # errors.
            pass

        # 60s lease duration is the SDK's max for a fixed-length lease.
        # We don't auto-renew (single short operation expected); if
        # callers need longer-held leases, raise the lease duration
        # via the underlying SDK and add a renewer thread.
        deadline = time.monotonic() + timeout_s
        lease = None
        acquired = False
        while time.monotonic() < deadline:
            try:
                lease = lock_blob.acquire_lease(lease_duration=60)
                acquired = True
                break
            except HttpResponseError as e:
                # 409 LeaseAlreadyPresent — someone else holds it.
                if getattr(e, "status_code", None) == 409:
                    time.sleep(0.5)
                    continue
                # Any other error: surface it.
                raise
        try:
            yield acquired
        finally:
            if acquired and lease is not None:
                try:
                    lease.release()
                except Exception:
                    # Lease will expire naturally in ≤60s if release
                    # fails — not worth surfacing here.
                    pass


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


# Pluggable backend registry — third parties can add S3, GCS, R2,
# MinIO, etc. without forking. Register a factory before any agent
# code calls get_storage():
#
#   from framework.core.storage import register_backend
#   register_backend("s3", lambda: MyS3Backend(...))
#   # then set STORAGE_BACKEND=s3 in env, get_storage() will use it.
#
# Built-in factories live in `_BUILTIN_BACKENDS` below; user-registered
# ones go in `_USER_BACKENDS`. Lookup tries user first, then built-in,
# so a deployment can override 'local' or 'azure' if needed.
_BUILTIN_BACKENDS: dict[str, "callable"] = {
    "azure": lambda: AzureBlobStorage(),
    "local": lambda: LocalFilesystemStorage(),
}
_USER_BACKENDS: dict[str, "callable"] = {}


def register_backend(name: str, factory: "callable") -> None:
    """Register a backend factory. `factory()` must return a
    `StorageBackend` instance. Idempotent — re-registering the same
    name overwrites the prior factory (useful for tests).
    """
    _USER_BACKENDS[name] = factory


def list_backends() -> list[str]:
    """Names registered + built-in. Used by setup scripts + the
    dashboard's Settings page to populate a dropdown."""
    return sorted(set(_BUILTIN_BACKENDS) | set(_USER_BACKENDS))


def get_storage(backend: Optional[str] = None) -> StorageBackend:
    """Return a process-wide cached storage backend.

    Backend selection (in priority order):
      1. `backend` parameter (any registered name)
      2. STORAGE_BACKEND env var
      3. Auto-detect: 'azure' if AZURE_STORAGE_CONNECTION_STRING is set,
         else 'local'

    See `register_backend` for adding S3 / GCS / etc.
    """
    global _DEFAULT_BACKEND
    with _BACKEND_LOCK:
        if _DEFAULT_BACKEND is not None and backend is None:
            return _DEFAULT_BACKEND
        choice = backend or os.getenv("STORAGE_BACKEND")
        if not choice:
            choice = "azure" if os.getenv("AZURE_STORAGE_CONNECTION_STRING") else "local"
        factory = _USER_BACKENDS.get(choice) or _BUILTIN_BACKENDS.get(choice)
        if factory is None:
            available = ", ".join(list_backends())
            raise SystemExit(
                f"unknown STORAGE_BACKEND={choice!r}. "
                f"Available: {available}. Register custom backends via "
                f"`framework.core.storage.register_backend(name, factory)`."
            )
        inst: StorageBackend = factory()
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
