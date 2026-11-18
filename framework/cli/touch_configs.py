"""Keep framework storage config blobs out of Azure's Archive tier.

Why
---
The lifecycle rule ``agents-runhistory-tier-then-delete`` on nscagentstorage
has ``prefixMatch: ["agents/"]``. A lifecycle prefix starts with the container
name, so the rule covers every block blob in the ``agents`` container, not
just run history: 30 days after its last write a blob turns Cool, at 120 days
Archive, and at 365 days it is deleted. Config under ``config/`` is live
settings that are written rarely, so it ages out like everything else. An
archived blob can be neither read nor overwritten: ``read_json`` returns None
and every caller silently falls back to its code defaults.

A read-only check on 2026-11-12 found ``priority-config.json`` archived since
10-07, ``code-editor-config.json`` since 09-21 and
``implementer-allowed-handlers.json`` since 09-23. ``required-models.json``
(last written 10-01) archives on 2027-01-29, after which
``required_model._load_config()`` returns None and the Opus-only rule lapses
without a warning; ``implementer-allowed-dispatch-kinds.json`` follows on
2027-02-08. This tool's own dry run on 2026-11-17 confirmed all of that and
found ``ai-providers.json`` (last written 09-24) archiving first, on
2027-01-22. It also showed that an ordinary overwrite keeps a Cool blob Cool,
which is why the rewrite below sets the tier explicitly.

What this does
--------------
Rewrites every ``config/*.json`` with byte-identical content (direct children
of the prefix only: ``config/amazon-creators-usage/<day>/`` holds daily usage
counters, which are history and may age out). A write resets the
last-modified date, which restarts the lifecycle clock, so a blob touched
well inside 30 days never even turns Cool. This is ``touch_manifests`` for
config.

* On Azure the rewrite is conditional on the ETag of the bytes just read, so
  a config someone changes between our read and our write is never reverted
  (it is reported as ``changed-concurrently``; that write reset its clock
  anyway). The rewritten blob is Hot and keeps its content settings and
  metadata. Other backends get a compare-before-write.
* Archived blobs are reported, never touched: they cannot be read, and unlike
  a manifest there is no second copy to recreate one from. The report gives
  the archive date, the date the lifecycle rule deletes the blob, and the
  repo's default copy when there is one. Restoring them changes live
  behaviour (the fleet has been running on code defaults), so it is an
  operator step (F2b) from reviewed content.
* ``--dry-run`` writes nothing and reads no blob content: one listing, which
  on Azure carries each blob's tier, last-modified date and archive status.
* ``--exclude GLOB`` skips matching keys, e.g. ``'*.backup-*.json'`` so
  one-off backup snapshots under ``config/`` can age out instead of being
  kept alive forever.
* The archive and delete dates are projections from the lifecycle rule's
  thresholds (120 and 365 days). The rule lives only in the storage account,
  so if it changes pass ``--archive-after-days`` / ``--delete-after-days``;
  every report states the thresholds it used.
* ``--require-backend azure`` refuses to run on any other backend, so a
  scheduled or KTLO run with ``STORAGE_BACKEND`` unset fails instead of
  touching local files and reporting success.

Usage::

    python3 -m framework.cli.touch_configs [--dry-run] [--json] [--prefix config/]
                                           [--exclude GLOB ...] [--fail-on-archived]
                                           [--at-risk-days 30] [--require-backend azure]
                                           [--archive-after-days 120] [--delete-after-days 365]

Exit codes: 0 every readable config was touched (archived ones are reported,
not failures); 1 a read or rewrite failed, nothing was found under the prefix
(wrong storage backend?), the listing failed, or ``--require-backend`` did not
match; 2 an invalid ``--prefix`` or other bad arguments; 3
``--fail-on-archived`` and at least one config is archived or unreadable (for
a KTLO "config ok or ARCHIVED" check).

Scheduling (F2b): run this from an AgentBase agent, with ``enabled: false``
until F2b, not from a bare systemd timer. CLAUDE.md's 'AGENTBASE IS MANDATORY'
rule covers cron, run history and status. The agent calls ``touch_configs()``
and reports the archived, at-risk and failed counts as metrics, and it runs
with ``--require-backend azure`` semantics.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Protocol, Sequence, TextIO

DEFAULT_PREFIX = "config/"
#: The lifecycle rule's thresholds (days after last modification).
ARCHIVE_AFTER_DAYS = 120
DELETE_AFTER_DAYS = 365
DEFAULT_AT_RISK_DAYS = 30
REWRITE_TIER = "Hot"
REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ARCHIVED = 3

# Per-blob outcomes.
TOUCHED = "touched"
WOULD_TOUCH = "would-touch"
ARCHIVED = "archived"
UNREADABLE = "unreadable"
CHANGED = "changed-concurrently"
VANISHED = "vanished"
SKIPPED = "skipped"
FAILED = "failed"
ACTIONS = (TOUCHED, WOULD_TOUCH, ARCHIVED, UNREADABLE, CHANGED, VANISHED, SKIPPED, FAILED)

_CONTENT_FIELDS = ("content_type", "content_encoding", "content_language",
                   "content_disposition", "cache_control")
_MAX_DETAIL = 200


class ConcurrentChange(Exception):
    """The blob changed between our read and our write; nothing was written."""


class BlobIsArchived(Exception):
    """The backend refused the operation because the blob is in the Archive tier."""


@dataclass(frozen=True, eq=False)
class BlobState:
    """What a listing says about one blob."""

    key: str
    tier: Optional[str] = None  # Hot / Cool / Cold / Archive; None when the backend has no tiers
    last_modified: Optional[datetime] = None
    tier_changed: Optional[datetime] = None
    archive_status: Optional[str] = None  # e.g. "rehydrate-pending-to-hot"
    blob_type: Optional[str] = None
    etag: Optional[str] = None
    content_settings: dict[str, Optional[str]] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def archived(self) -> bool:
        return (self.tier or "").lower() == "archive"


class ConfigStore(Protocol):
    """The storage operations ``touch_configs`` needs."""

    backend: str
    reports_tiers: bool

    def list_states(self, prefix: str) -> list[BlobState]: ...

    def read(self, state: BlobState) -> Optional[tuple[bytes, Optional[str]]]:
        """``(data, etag)``, or None when the blob cannot be read. Raises
        ``BlobIsArchived`` when the backend says so."""

    def rewrite(self, state: BlobState, data: bytes, etag: Optional[str]) -> Optional[str]:
        """Write ``data`` back; return the tier it now has (None if unknown).
        Raises ``ConcurrentChange`` or ``BlobIsArchived``."""


def is_config_key(key: str, prefix: str) -> bool:
    """A direct child ``<prefix><name>.json`` (no deeper path)."""
    if not key.startswith(prefix):
        return False
    rest = key[len(prefix):]
    return bool(rest) and "/" not in rest and rest.endswith(".json")


def normalize_prefix(prefix: str) -> str:
    p = prefix.strip().lstrip("/")
    if not p or ".." in p.split("/"):
        raise ValueError(f"invalid prefix {prefix!r}")
    return p if p.endswith("/") else p + "/"


def _value(x: Any) -> Optional[str]:
    """Enum or string -> its string value."""
    if x is None:
        return None
    return str(getattr(x, "value", x))


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    text = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "error_code", "") or "")


def _is_archived_error(exc: BaseException) -> bool:
    return _error_code(exc) == "BlobArchived" or "BlobArchived" in str(exc)


def _is_condition_failed(exc: BaseException) -> bool:
    return (type(exc).__name__ == "ResourceModifiedError"
            or getattr(exc, "status_code", None) == 412
            or _error_code(exc) == "ConditionNotMet")


def _is_not_found(exc: BaseException) -> bool:
    return (type(exc).__name__ == "ResourceNotFoundError"
            or getattr(exc, "status_code", None) == 404
            or _error_code(exc) == "BlobNotFound")


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

class StorageConfigStore:
    """Any ``StorageBackend``. No tiers; last-modified only for a local directory."""

    reports_tiers = False

    def __init__(self, storage: Any):
        self.storage = storage
        self.backend = str(getattr(storage, "name", type(storage).__name__))
        root = getattr(storage, "root", None)
        self._root = Path(root) if isinstance(root, (str, os.PathLike)) else None

    def _read_bytes(self, key: str) -> Optional[bytes]:
        reader = getattr(self.storage, "read_bytes_uncached", None) or self.storage.read_bytes
        return reader(key)

    def _mtime(self, key: str) -> Optional[datetime]:
        if self._root is None:
            return None
        try:
            return datetime.fromtimestamp((self._root / key).stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None

    def list_states(self, prefix: str) -> list[BlobState]:
        keys = sorted(k for k in self.storage.list_prefix(prefix) if is_config_key(k, prefix))
        return [BlobState(key=k, last_modified=self._mtime(k)) for k in keys]

    def read(self, state: BlobState) -> Optional[tuple[bytes, Optional[str]]]:
        data = self._read_bytes(state.key)
        return None if data is None else (data, None)

    def rewrite(self, state: BlobState, data: bytes, etag: Optional[str]) -> Optional[str]:
        # No conditional writes in the generic interface: re-read right before
        # writing so a change made since our first read is not reverted.
        if self._read_bytes(state.key) != data:
            raise ConcurrentChange("content changed since it was read")
        self.storage.write_bytes(state.key, data,
                                 cache_control=state.content_settings.get("cache_control"))
        return None


@dataclass(frozen=True)
class AzureSdk:
    """The few SDK symbols the Azure store needs (stand-ins when it is absent)."""

    content_settings: Callable[..., Any]
    if_not_modified: Any
    hot_tier: Any

    @classmethod
    def load(cls) -> "AzureSdk":
        try:
            from azure.core import MatchConditions
            from azure.storage.blob import ContentSettings, StandardBlobTier
        except ImportError:  # only reachable with an injected container (tests)
            return cls(content_settings=lambda **kw: SimpleNamespace(**kw),
                       if_not_modified="IfNotModified", hot_tier=REWRITE_TIER)
        return cls(content_settings=ContentSettings,
                   if_not_modified=MatchConditions.IfNotModified,
                   hot_tier=StandardBlobTier.HOT)


@dataclass(frozen=True)
class _ReadVersion:
    """Content settings and metadata of the exact blob version a read returned."""

    etag: Optional[str]
    content_settings: dict[str, Optional[str]]
    metadata: dict[str, str]


class AzureConfigStore:
    """Azure Blob container: real tiers, ETag-conditional rewrites, Hot on rewrite."""

    backend = "azure"
    reports_tiers = True

    def __init__(self, container: Any, *, sdk: Optional[AzureSdk] = None):
        self.container = container
        self.sdk = sdk or AzureSdk.load()
        # key -> the version read() returned. rewrite() puts back that version's
        # headers, which belong to the same ETag it is conditional on, not the
        # listing's (a writer may have replaced the blob in between).
        self._read_versions: dict[str, _ReadVersion] = {}

    def list_states(self, prefix: str) -> list[BlobState]:
        out: list[BlobState] = []
        for props in self.container.list_blobs(name_starts_with=prefix, include=["metadata"]):
            if not is_config_key(props.name, prefix):
                continue
            settings = getattr(props, "content_settings", None)
            out.append(BlobState(
                key=props.name,
                tier=_value(getattr(props, "blob_tier", None)),
                last_modified=getattr(props, "last_modified", None),
                tier_changed=getattr(props, "blob_tier_change_time", None),
                archive_status=_value(getattr(props, "archive_status", None)),
                blob_type=_value(getattr(props, "blob_type", None)),
                etag=getattr(props, "etag", None),
                content_settings={f: getattr(settings, f, None) for f in _CONTENT_FIELDS}
                if settings is not None else {},
                metadata=dict(getattr(props, "metadata", None) or {}),
            ))
        return sorted(out, key=lambda s: s.key)

    def read(self, state: BlobState) -> Optional[tuple[bytes, Optional[str]]]:
        blob = self.container.get_blob_client(state.key)
        try:
            # decompress=False: the stored bytes, exactly. The SDK otherwise
            # gunzips a blob stored with Content-Encoding: gzip, and the rewrite
            # (which keeps that header) would store plain JSON labelled gzip,
            # which every framework read then fails to decode.
            downloader = blob.download_blob(decompress=False)
            data = downloader.readall()
        except Exception as exc:
            if _is_not_found(exc):
                return None
            if _is_archived_error(exc):
                raise BlobIsArchived(_short(exc)) from exc
            raise
        props = getattr(downloader, "properties", None)
        etag = getattr(props, "etag", None) or state.etag
        settings = getattr(props, "content_settings", None)
        metadata = getattr(props, "metadata", None)
        self._read_versions[state.key] = _ReadVersion(
            etag=etag,
            content_settings=({f: getattr(settings, f, None) for f in _CONTENT_FIELDS}
                              if settings is not None else dict(state.content_settings)),
            metadata=dict(metadata) if isinstance(metadata, dict) else dict(state.metadata),
        )
        return data, etag

    def rewrite(self, state: BlobState, data: bytes, etag: Optional[str]) -> Optional[str]:
        version = self._read_versions.pop(state.key, None)
        if version is not None and version.etag == etag:
            content_settings, metadata = version.content_settings, version.metadata
        else:  # no read through this store: fall back to what the listing said
            content_settings, metadata = state.content_settings, state.metadata
        settings = {f: v for f, v in content_settings.items() if v}
        settings.setdefault("content_type", "application/json")
        kwargs: dict[str, Any] = {
            "overwrite": True,
            "content_settings": self.sdk.content_settings(**settings),
            "metadata": dict(metadata) or None,
            "standard_blob_tier": self.sdk.hot_tier,
        }
        if etag:
            kwargs.update(etag=etag, match_condition=self.sdk.if_not_modified)
        try:
            self.container.get_blob_client(state.key).upload_blob(data, **kwargs)
        except Exception as exc:
            if _is_condition_failed(exc):
                raise ConcurrentChange(_short(exc)) from exc
            if _is_archived_error(exc):
                raise BlobIsArchived(_short(exc)) from exc
            raise
        return REWRITE_TIER


def azure_container_for(storage: Any) -> Any:
    """The container client behind an Azure storage backend, else None."""
    if getattr(storage, "name", None) != "azure":
        return None
    # AzureBlobStorage keeps its client private; reuse it so we touch exactly
    # the account and container the fleet reads.
    container = getattr(storage, "_container", None)
    if container is not None:
        return container
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if not conn:
        return None
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        return None
    name = os.environ.get("AZURE_STORAGE_CONTAINER", "agents")
    return BlobServiceClient.from_connection_string(conn).get_container_client(name)


def store_for(storage: Any) -> ConfigStore:
    container = azure_container_for(storage)
    if container is not None:
        return AzureConfigStore(container)
    return StorageConfigStore(storage)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _date(dt: Optional[datetime]) -> Optional[str]:
    return dt.date().isoformat() if dt else None


@dataclass
class ConfigResult:
    key: str
    tier: Optional[str]
    last_modified: Optional[str]
    age_days: Optional[int]
    archives_on: Optional[str]
    days_to_archive: Optional[int]
    deletes_on: Optional[str]
    archived_since: Optional[str]
    archive_status: Optional[str]
    repo_default: Optional[str]
    action: str = ""
    tier_after: Optional[str] = None
    detail: str = ""

    @classmethod
    def from_state(cls, st: BlobState, *, now: datetime, repo_root: Path, lifecycle: bool,
                   archive_after_days: int = ARCHIVE_AFTER_DAYS,
                   delete_after_days: int = DELETE_AFTER_DAYS) -> "ConfigResult":
        """``lifecycle``: project the rule's archive/delete dates (tiered backends only)."""
        lm = st.last_modified
        archives = lm + timedelta(days=archive_after_days) if lm and lifecycle else None
        deletes = lm + timedelta(days=delete_after_days) if lm and lifecycle else None
        since = (st.tier_changed or archives) if st.archived else None
        return cls(
            key=st.key, tier=st.tier,
            last_modified=lm.isoformat(timespec="seconds") if lm else None,
            age_days=(now - lm).days if lm else None,
            archives_on=None if st.archived else _date(archives),
            days_to_archive=None if st.archived or archives is None else (archives - now).days,
            deletes_on=_date(deletes),
            archived_since=_date(since),
            archive_status=st.archive_status,
            repo_default=st.key if (repo_root / st.key).is_file() else None,
        )


@dataclass
class TouchReport:
    backend: str
    prefix: str
    dry_run: bool
    generated_at: str
    at_risk_days: int
    results: list[ConfigResult]
    #: The lifecycle thresholds the dates were projected with; None when the
    #: backend has no tiers (no projection).
    lifecycle: Optional[dict[str, int]] = None

    def count(self, action: str) -> int:
        return sum(1 for r in self.results if r.action == action)

    def counts(self) -> dict[str, int]:
        return {a: self.count(a) for a in ACTIONS if self.count(a)}

    def tiers(self) -> dict[str, int]:
        return dict(sorted(Counter(r.tier or "n/a" for r in self.results).items()))

    def at_risk(self) -> list[ConfigResult]:
        return [r for r in self.results
                if r.days_to_archive is not None and r.days_to_archive <= self.at_risk_days]

    def exit_code(self, *, fail_on_archived: bool = False) -> int:
        if not self.results or self.count(FAILED):
            return EXIT_FAILED
        if fail_on_archived and (self.count(ARCHIVED) or self.count(UNREADABLE)):
            return EXIT_ARCHIVED
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend, "prefix": self.prefix, "dry_run": self.dry_run,
            "generated_at": self.generated_at, "at_risk_days": self.at_risk_days,
            "lifecycle": dict(self.lifecycle) if self.lifecycle else None,
            "counts": self.counts(), "tiers": self.tiers(),
            "at_risk": [r.key for r in self.at_risk()],
            "results": [asdict(r) for r in self.results],
        }


def is_excluded(key: str, patterns: Sequence[str]) -> bool:
    """``patterns`` are globs matched against the key and against its basename."""
    name = key.rsplit("/", 1)[-1]
    return any(fnmatchcase(key, p) or fnmatchcase(name, p) for p in patterns)


def touch_configs(store: ConfigStore, *, prefix: str = DEFAULT_PREFIX, dry_run: bool = False,
                  now: Optional[datetime] = None, at_risk_days: int = DEFAULT_AT_RISK_DAYS,
                  exclude: Sequence[str] = (), repo_root: Path = REPO_ROOT,
                  archive_after_days: int = ARCHIVE_AFTER_DAYS,
                  delete_after_days: int = DELETE_AFTER_DAYS) -> TouchReport:
    """Rewrite (or, with ``dry_run``, just list) every config blob under ``prefix``.

    ``archive_after_days`` / ``delete_after_days``: the storage lifecycle rule's
    thresholds, used only to project dates on a tiered backend."""
    prefix = normalize_prefix(prefix)
    if archive_after_days < 1 or delete_after_days < 1:
        raise ValueError("lifecycle thresholds must be at least 1 day")
    now = now or datetime.now(timezone.utc)
    results: list[ConfigResult] = []
    for st in store.list_states(prefix):
        block_blob = st.blob_type in (None, "BlockBlob")
        res = ConfigResult.from_state(st, now=now, repo_root=repo_root,
                                      lifecycle=store.reports_tiers and block_blob,
                                      archive_after_days=archive_after_days,
                                      delete_after_days=delete_after_days)
        if not block_blob:
            res.action, res.detail = SKIPPED, f"{st.blob_type}: the lifecycle rule only tiers block blobs"
        elif is_excluded(st.key, exclude):
            res.action, res.detail = SKIPPED, "excluded"
        elif st.archived:
            res.action = ARCHIVED
        elif dry_run:
            res.action = WOULD_TOUCH
        else:
            _touch_one(store, st, res)
        results.append(res)
    lifecycle = ({"archive_after_days": archive_after_days, "delete_after_days": delete_after_days}
                 if store.reports_tiers else None)
    return TouchReport(backend=store.backend, prefix=prefix, dry_run=dry_run,
                       generated_at=now.isoformat(timespec="seconds"),
                       at_risk_days=at_risk_days, results=results, lifecycle=lifecycle)


def _touch_one(store: ConfigStore, st: BlobState, res: ConfigResult) -> None:
    try:
        got = store.read(st)
    except BlobIsArchived:
        res.action, res.detail = ARCHIVED, "archived (the read was refused)"
        return
    except Exception as exc:
        res.action, res.detail = FAILED, f"read: {_short(exc)}"
        return
    if got is None:
        if store.reports_tiers:
            res.action, res.detail = VANISHED, "deleted after it was listed"
        else:
            res.action, res.detail = UNREADABLE, "listed but unreadable (archived?)"
        return
    data, etag = got
    try:
        res.tier_after = store.rewrite(st, data, etag)
    except ConcurrentChange:
        res.action, res.detail = CHANGED, "changed while being touched; left as written"
    except BlobIsArchived:
        res.action, res.detail = ARCHIVED, "archived (the rewrite was refused)"
    except Exception as exc:
        res.action, res.detail = FAILED, f"write: {_short(exc)}"
    else:
        res.action = TOUCHED


def render_text(report: TouchReport) -> str:
    head = (f"touch-configs: {len(report.results)} config blob(s) under {report.prefix} "
            f"on {report.backend} storage" + (" (DRY RUN)" if report.dry_run else ""))
    if report.lifecycle:
        head += (f"; dates assume the lifecycle rule archives {report.lifecycle['archive_after_days']}d "
                 f"and deletes {report.lifecycle['delete_after_days']}d after the last write")
    if not report.results:
        return (head + "\ntouch-configs: nothing found; is STORAGE_BACKEND / "
                "AZURE_STORAGE_CONNECTION_STRING pointing at the fleet's storage?")
    width = max(len(r.key) for r in report.results)
    at_risk = {r.key for r in report.at_risk()}
    lines = [head]
    for r in report.results:
        tier = r.tier or "n/a"
        if r.action == ARCHIVED:
            state = f"ARCHIVED since {r.archived_since or '?'}"
            if r.deletes_on:
                state += f"; the lifecycle rule deletes it on {r.deletes_on}"
            if r.archive_status:
                state += f"; {r.archive_status}"
            state += f"; repo default: {r.repo_default}" if r.repo_default else "; no repo default"
        else:
            parts = []
            if r.last_modified:
                parts.append(f"modified {r.last_modified[:10]} ({r.age_days}d ago)")
            if r.archives_on:
                parts.append(f"archives {r.archives_on} (in {r.days_to_archive}d)"
                             + (" AT RISK" if r.key in at_risk else ""))
            state = ", ".join(parts) or "last-modified unknown"
            state += f" -> {r.action}"
            if r.tier_after and r.tier_after != r.tier:
                state += f" ({tier} -> {r.tier_after})"
        if r.detail:
            state += f" [{r.detail}]"
        lines.append(f"  {r.key:<{width}}  {tier:<7}  {state}")
    summary = " ".join(f"{a}={n}" for a, n in report.counts().items())
    tiers = " ".join(f"{t}={n}" for t, n in report.tiers().items())
    lines.append(f"touch-configs: {summary}; tiers: {tiers}"
                 + (f"; archiving within {report.at_risk_days}d: {len(at_risk)}" if at_risk else ""))
    archived = report.count(ARCHIVED) + report.count(UNREADABLE)
    if archived:
        lines.append(f"touch-configs: {archived} config(s) cannot be read or rewritten and readers "
                     "are on code defaults. Restore them as Hot from operator-reviewed content (F2b).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python3 -m framework.cli.touch_configs",
        description="Rewrite config/*.json with identical content so the storage lifecycle "
                    "rule never archives it; report blobs that are already archived.")
    ap.add_argument("--dry-run", action="store_true",
                    help="list tiers and archive dates; write nothing")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX,
                    help=f"storage prefix to touch (default {DEFAULT_PREFIX})")
    ap.add_argument("--fail-on-archived", action="store_true",
                    help=f"exit {EXIT_ARCHIVED} when any config is archived or unreadable")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip keys whose name or full key matches GLOB (repeatable), "
                         "e.g. '*.backup-*.json' to let backup snapshots age out")
    ap.add_argument("--at-risk-days", type=int, default=DEFAULT_AT_RISK_DAYS,
                    help="flag configs that archive within this many days "
                         f"(default {DEFAULT_AT_RISK_DAYS})")
    ap.add_argument("--require-backend", metavar="NAME",
                    help=f"exit {EXIT_FAILED} without listing unless the storage backend is NAME "
                         "(e.g. azure), so a scheduled run cannot quietly touch local files")
    ap.add_argument("--archive-after-days", type=_positive_int, default=ARCHIVE_AFTER_DAYS,
                    metavar="N", help="the lifecycle rule's days-to-Archive, for the projected "
                                      f"dates (default {ARCHIVE_AFTER_DAYS})")
    ap.add_argument("--delete-after-days", type=_positive_int, default=DELETE_AFTER_DAYS,
                    metavar="N", help="the lifecycle rule's days-to-delete, for the projected "
                                      f"dates (default {DELETE_AFTER_DAYS})")
    return ap.parse_args(argv)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def main(argv: Optional[Sequence[str]] = None, *, storage: Any = None,
         store: Optional[ConfigStore] = None, now: Optional[datetime] = None,
         out: Optional[TextIO] = None) -> int:
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    out = out or sys.stdout
    try:
        prefix = normalize_prefix(args.prefix)
    except ValueError as exc:
        print(f"touch-configs: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if store is None:
        if storage is None:
            from framework.core.storage import get_storage
            storage = get_storage()
        store = store_for(storage)
    if args.require_backend and store.backend != args.require_backend:
        print(f"touch-configs: refusing to run on {store.backend} storage: --require-backend "
              f"{args.require_backend} (check STORAGE_BACKEND and AZURE_STORAGE_CONNECTION_STRING)",
              file=sys.stderr)
        return EXIT_FAILED
    try:
        report = touch_configs(store, prefix=prefix, dry_run=args.dry_run, now=now,
                               at_risk_days=args.at_risk_days, exclude=args.exclude,
                               archive_after_days=args.archive_after_days,
                               delete_after_days=args.delete_after_days)
    except Exception as exc:  # listing failed: no blob was read or written
        print(f"touch-configs: cannot list {prefix} on {store.backend} storage: {_short(exc)}",
              file=sys.stderr)
        return EXIT_FAILED
    text = json.dumps(report.to_dict(), indent=2) if args.json else render_text(report)
    out.write(text + "\n")
    return report.exit_code(fail_on_archived=args.fail_on_archived)


if __name__ == "__main__":
    raise SystemExit(main())
