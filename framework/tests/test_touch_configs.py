"""touch_configs (F2a): rewrite config/*.json byte-identically, report archived blobs.

The generic path runs against LocalFilesystemStorage (the conftest ``storage``
fixture). The Azure path runs against an in-memory fake container that mimics
the SDK surface the store uses: ``list_blobs`` with tiers, ``download_blob``
(which gunzips unless told not to, as the SDK does), ETag-conditional
``upload_blob`` and the ``BlobArchived`` refusal. Neither needs the Azure SDK.

The ``test_real_sdk_*`` tests drive the installed azure-storage-blob SDK itself
against ``WireService``, an in-process fake of the Blob REST endpoint behind a
requests transport, so the real request building (If-Match,
x-ms-access-tier, content headers, metadata) and error mapping are checked.
They skip when the SDK is not installed. No test touches the network.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from framework.cli import touch_configs as tc
from framework.core.storage import LocalFilesystemStorage, reset_default_backend

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=200)
SDK = tc.AzureSdk(content_settings=lambda **kw: SimpleNamespace(**kw),
                  if_not_modified="IfNotModified", hot_tier="Hot")


def _put(storage: LocalFilesystemStorage, key: str, data: bytes,
         mtime: Optional[datetime] = OLD) -> None:
    storage.write_bytes(key, data)
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(storage.root / key, (ts, ts))


def _mtime(storage: LocalFilesystemStorage, key: str) -> float:
    return (storage.root / key).stat().st_mtime


def _run(argv: list[str], **kw: Any) -> tuple[int, str]:
    out = io.StringIO()
    rc = tc.main(argv, out=out, **kw)
    return rc, out.getvalue()


ODD_JSON = b'{"b": 1,   "a": [1,2]}'  # re-serialising would change these bytes
UNICODE_JSON = '{"x": "ünïcode"}\n'.encode()


@pytest.fixture
def seeded(storage: LocalFilesystemStorage) -> LocalFilesystemStorage:
    _put(storage, "config/a-config.json", ODD_JSON)
    _put(storage, "config/b-config.json", UNICODE_JSON)
    _put(storage, "config/amazon-creators-usage/2026-11-01/price-refresh.json", b"{}")
    _put(storage, "config/notes.txt", b"not json")
    _put(storage, "registry/agents.json", b"[]")
    return storage


# ---------------------------------------------------------------------------
# Generic store (LocalFilesystemStorage)
# ---------------------------------------------------------------------------

def test_rewrites_each_config_byte_identically_and_resets_its_clock(seeded):
    rc, out = _run([], storage=seeded)
    assert rc == tc.EXIT_OK, out
    assert seeded.read_bytes("config/a-config.json") == ODD_JSON
    assert seeded.read_bytes("config/b-config.json") == UNICODE_JSON
    for key in ("config/a-config.json", "config/b-config.json"):
        assert _mtime(seeded, key) > time.time() - 120, key
    # Nested usage counters, non-JSON files and other prefixes are left alone.
    for key in ("config/amazon-creators-usage/2026-11-01/price-refresh.json",
                "config/notes.txt", "registry/agents.json"):
        assert _mtime(seeded, key) == pytest.approx(OLD.timestamp()), key
    assert "touched=2" in out


def test_dry_run_lists_without_writing(seeded):
    rc, out = _run(["--dry-run", "--json"], storage=seeded, now=NOW)
    assert rc == tc.EXIT_OK
    report = json.loads(out)
    assert report["dry_run"] is True and report["backend"] == "local"
    assert report["counts"] == {"would-touch": 2}
    assert report["tiers"] == {"n/a": 2}
    rows = report["results"]
    assert [r["key"] for r in rows] == ["config/a-config.json", "config/b-config.json"]
    assert rows[0]["age_days"] == 200
    assert rows[0]["last_modified"].startswith(OLD.date().isoformat())
    # Local storage has no lifecycle rule, so no archive projection.
    assert rows[0]["archives_on"] is None and rows[0]["deletes_on"] is None
    assert _mtime(seeded, "config/a-config.json") == pytest.approx(OLD.timestamp())
    text_rc, text = _run(["--dry-run"], storage=seeded, now=NOW)
    assert text_rc == 0 and "(DRY RUN)" in text and "-> would-touch" in text


class _ArchivedLocal(LocalFilesystemStorage):
    """Mimics AzureBlobStorage on an archived blob: reads give None, writes raise."""

    archived = frozenset({"config/priority-config.json"})

    def read_bytes(self, key: str) -> Optional[bytes]:
        return None if key in self.archived else super().read_bytes(key)

    def write_bytes(self, key: str, data: bytes, cache_control: Optional[str] = None) -> None:
        if key in self.archived:
            raise RuntimeError("BlobArchived: This operation is not permitted on an archived blob.")
        super().write_bytes(key, data, cache_control=cache_control)


def test_unreadable_config_is_reported_not_fatal(storage):
    _put(storage, "config/priority-config.json", b'{"tiers": {}}')
    _put(storage, "config/required-models.json", b'{"by_agent_id": {}}')
    archived = _ArchivedLocal(str(storage.root))
    rc, out = _run([], storage=archived)
    assert rc == tc.EXIT_OK, out
    assert "unreadable" in out and "touched=1" in out
    assert "Restore them as Hot from operator-reviewed content" in out
    assert _mtime(storage, "config/required-models.json") > time.time() - 120
    rc, _ = _run(["--fail-on-archived"], storage=archived)
    assert rc == tc.EXIT_ARCHIVED


class _FailingWrite(LocalFilesystemStorage):
    def write_bytes(self, key: str, data: bytes, cache_control: Optional[str] = None) -> None:
        if key.endswith("a-config.json"):
            raise OSError("disk full")
        super().write_bytes(key, data, cache_control=cache_control)


def test_write_failure_exits_1_but_touches_the_rest(seeded):
    rc, out = _run(["--json"], storage=_FailingWrite(str(seeded.root)))
    assert rc == tc.EXIT_FAILED
    rows = {r["key"]: r for r in json.loads(out)["results"]}
    assert rows["config/a-config.json"]["action"] == tc.FAILED
    assert rows["config/a-config.json"]["detail"] == "write: OSError: disk full"
    assert rows["config/b-config.json"]["action"] == tc.TOUCHED


class _RacyLocal(LocalFilesystemStorage):
    """Another writer updates the config right after our first read."""

    def __init__(self, root: str):
        super().__init__(root)
        self.reads = 0

    def read_bytes(self, key: str) -> Optional[bytes]:
        data = super().read_bytes(key)
        self.reads += 1
        if self.reads == 1:
            super().write_bytes(key, b'{"changed": true}')
        return data


def test_change_between_read_and_write_is_not_reverted(storage):
    _put(storage, "config/x-config.json", b'{"changed": false}')
    rc, out = _run(["--json"], storage=_RacyLocal(str(storage.root)))
    assert rc == tc.EXIT_OK
    (row,) = json.loads(out)["results"]
    assert row["action"] == tc.CHANGED
    assert storage.read_bytes("config/x-config.json") == b'{"changed": true}'


def test_nothing_found_exits_1(storage):
    _put(storage, "registry/agents.json", b"[]")
    rc, out = _run([], storage=storage)
    assert rc == tc.EXIT_FAILED
    assert "nothing found" in out


@pytest.mark.parametrize("prefix", ["../config", "", "/"])
def test_invalid_prefix_exits_2(storage, prefix, capsys):
    assert tc.main(["--prefix", prefix], storage=storage, out=io.StringIO()) == 2
    assert "invalid prefix" in capsys.readouterr().err


def test_custom_prefix(storage):
    _put(storage, "settings/one.json", b"{}")
    _put(storage, "config/two.json", b"{}")
    rc, out = _run(["--prefix", "settings", "--json"], storage=storage)
    assert rc == 0
    assert [r["key"] for r in json.loads(out)["results"]] == ["settings/one.json"]


def test_exclude_globs_skip_matching_keys(seeded):
    _put(seeded, "config/a-config.backup-20261112.json", b"{}")
    rc, out = _run(["--exclude", "*.backup-*.json", "--exclude", "config/b-*", "--json"],
                   storage=seeded)
    assert rc == tc.EXIT_OK
    rows = {r["key"]: (r["action"], r["detail"]) for r in json.loads(out)["results"]}
    assert rows == {
        "config/a-config.backup-20261112.json": (tc.SKIPPED, "excluded"),
        "config/a-config.json": (tc.TOUCHED, ""),
        "config/b-config.json": (tc.SKIPPED, "excluded"),
    }
    assert _mtime(seeded, "config/a-config.backup-20261112.json") == pytest.approx(OLD.timestamp())
    assert tc.is_excluded("config/x.backup-1.json", ["*.backup-*.json"])
    assert not tc.is_excluded("config/x.json", ["*.backup-*.json"])


@pytest.fixture
def default_backend_reset():
    reset_default_backend()
    yield
    reset_default_backend()


def test_main_defaults_to_the_configured_storage(seeded, default_backend_reset):
    # The conftest fixture points STORAGE_BACKEND=local at this tmp dir.
    rc, out = _run(["--dry-run"])
    assert rc == 0 and "on local storage" in out and "config/a-config.json" in out


def test_key_and_prefix_helpers():
    assert tc.is_config_key("config/a.json", "config/")
    assert not tc.is_config_key("config/sub/a.json", "config/")
    assert not tc.is_config_key("config/a.jsonl", "config/")
    assert not tc.is_config_key("config/", "config/")
    assert not tc.is_config_key("other/a.json", "config/")
    assert tc.normalize_prefix("config") == "config/"
    assert tc.normalize_prefix("/config/") == "config/"
    with pytest.raises(ValueError):
        tc.normalize_prefix("a/../b")


# ---------------------------------------------------------------------------
# Azure store against a fake container
# ---------------------------------------------------------------------------

class FakeHttpError(Exception):
    def __init__(self, message: str, *, status_code: int, error_code: str):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class ResourceModifiedError(FakeHttpError):
    """Same class name as azure.core.exceptions.ResourceModifiedError."""


class ResourceNotFoundError(FakeHttpError):
    """Same class name as azure.core.exceptions.ResourceNotFoundError."""


def _archived_error() -> FakeHttpError:
    return FakeHttpError("This operation is not permitted on an archived blob.\nRequestId:x",
                         status_code=409, error_code="BlobArchived")


_FIELDS = ("content_type", "content_encoding", "content_language", "content_disposition",
           "cache_control")


class FakeBlob:
    _etags = 0

    def __init__(self, data: bytes, *, tier: Optional[str] = "Hot", modified: datetime = OLD,
                 tier_changed: Optional[datetime] = None, archive_status: Optional[str] = None,
                 blob_type: str = "BlockBlob", content_settings: Optional[dict] = None,
                 metadata: Optional[dict] = None):
        FakeBlob._etags += 1
        self.data = data
        self.tier = tier
        self.modified = modified
        self.tier_changed = tier_changed
        self.archive_status = archive_status
        self.blob_type = blob_type
        self.etag = f'"0x{FakeBlob._etags:04X}"'
        self.content_settings = {"content_type": "application/json", **(content_settings or {})}
        self.metadata = dict(metadata or {})


class FakeContainer:
    def __init__(self, now: datetime = NOW):
        self.now = now
        self.blobs: dict[str, FakeBlob] = {}
        self.phantoms: list[str] = []  # listed, then gone
        self.uploads: list[tuple[str, bytes, dict]] = []
        self.downloads = 0
        self.list_calls: list[tuple[Optional[str], Any]] = []
        self.after_download: Optional[Callable[[str], None]] = None
        self.upload_error: Optional[Exception] = None

    def list_blobs(self, name_starts_with: Optional[str] = None, include: Any = None):
        self.list_calls.append((name_starts_with, include))
        prefix = name_starts_with or ""
        for name in sorted(set(self.blobs) | set(self.phantoms)):
            if not name.startswith(prefix):
                continue
            b = self.blobs.get(name) or FakeBlob(b"{}", tier="Cool")
            yield SimpleNamespace(
                name=name, blob_tier=b.tier, last_modified=b.modified,
                blob_tier_change_time=b.tier_changed, archive_status=b.archive_status,
                blob_type=b.blob_type, etag=b.etag,
                content_settings=SimpleNamespace(**{f: b.content_settings.get(f) for f in (
                    "content_type", "content_encoding", "content_language",
                    "content_disposition", "cache_control", "content_md5")}),
                metadata=dict(b.metadata))

    def get_blob_client(self, name: str) -> "FakeBlobClient":
        return FakeBlobClient(self, name)


class FakeBlobClient:
    def __init__(self, container: FakeContainer, name: str):
        self.c = container
        self.name = name

    def download_blob(self, decompress: bool = True):
        blob = self.c.blobs.get(self.name)
        if blob is None:
            raise ResourceNotFoundError("The specified blob does not exist.",
                                        status_code=404, error_code="BlobNotFound")
        if blob.tier == "Archive":
            raise _archived_error()
        self.c.downloads += 1
        data = blob.data
        if decompress and blob.content_settings.get("content_encoding") == "gzip":
            data = gzip.decompress(data)  # what the SDK does by default
        # Like the SDK: the properties are those of the version just downloaded.
        props = SimpleNamespace(
            etag=blob.etag, metadata=dict(blob.metadata),
            content_settings=SimpleNamespace(**{f: blob.content_settings.get(f) for f in _FIELDS}))
        if self.c.after_download:
            self.c.after_download(self.name)
        return SimpleNamespace(readall=lambda: data, properties=props)

    def upload_blob(self, data: bytes, **kw: Any) -> None:
        self.c.uploads.append((self.name, data, kw))
        if self.c.upload_error is not None:
            raise self.c.upload_error
        blob = self.c.blobs.get(self.name)
        if blob is not None and blob.tier == "Archive":
            raise _archived_error()
        if kw.get("etag") and (blob is None or blob.etag != kw["etag"]):
            raise ResourceModifiedError("The condition specified using HTTP conditional header(s) "
                                        "is not met.", status_code=412, error_code="ConditionNotMet")
        settings = kw["content_settings"]
        self.c.blobs[self.name] = FakeBlob(
            data, tier=str(getattr(kw.get("standard_blob_tier"), "value", kw.get("standard_blob_tier"))),
            modified=self.c.now,
            content_settings={f: getattr(settings, f, None) for f in (
                "content_type", "content_encoding", "content_language", "content_disposition",
                "cache_control")},
            metadata=kw.get("metadata"))


def _fleet_like_container() -> FakeContainer:
    c = FakeContainer()
    c.blobs["config/ai-defaults.json"] = FakeBlob(b"{}", tier="Hot", modified=NOW - timedelta(days=7))
    c.blobs["config/required-models.json"] = FakeBlob(
        b'{"by_agent_id": {}}', tier="Cool", modified=datetime(2026, 10, 1, 9, tzinfo=timezone.utc),
        content_settings={"cache_control": "no-cache"}, metadata={"owner": "ops"})
    c.blobs["config/implementer-allowed-dispatch-kinds.json"] = FakeBlob(
        b'{"allow": ["*"]}', tier="Cool", modified=datetime(2026, 10, 11, 9, tzinfo=timezone.utc))
    c.blobs["config/priority-config.json"] = FakeBlob(
        b"", tier="Archive", modified=datetime(2026, 6, 9, 8, tzinfo=timezone.utc),
        tier_changed=datetime(2026, 10, 7, 3, tzinfo=timezone.utc))
    c.blobs["config/code-editor-config.json"] = FakeBlob(
        b"", tier="Archive", modified=datetime(2026, 5, 24, 8, tzinfo=timezone.utc),
        tier_changed=datetime(2026, 9, 21, 3, tzinfo=timezone.utc),
        archive_status="rehydrate-pending-to-hot")
    c.blobs["config/stream.json"] = FakeBlob(b"", tier=None, blob_type="AppendBlob")
    c.blobs["config/amazon-creators-usage/2026-11-01/discovery.json"] = FakeBlob(b"{}", tier="Cool")
    c.blobs["config/readme.txt"] = FakeBlob(b"x", tier="Cool")
    return c


def test_azure_dry_run_reports_tiers_dates_and_archived_blobs(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "priority-config.json").write_text("{}")
    c = _fleet_like_container()
    report = tc.touch_configs(tc.AzureConfigStore(c, sdk=SDK), dry_run=True, now=NOW,
                              repo_root=tmp_path, at_risk_days=80)
    rows = {r.key: r for r in report.results}
    assert sorted(rows) == [
        "config/ai-defaults.json", "config/code-editor-config.json",
        "config/implementer-allowed-dispatch-kinds.json", "config/priority-config.json",
        "config/required-models.json", "config/stream.json"]
    # The design's evidence: last written 10-01 -> Archive on 2027-01-29; 10-11 -> 2027-02-08.
    assert rows["config/required-models.json"].archives_on == "2027-01-29"
    assert rows["config/required-models.json"].days_to_archive == 72
    assert rows["config/implementer-allowed-dispatch-kinds.json"].archives_on == "2027-02-08"
    assert rows["config/required-models.json"].action == tc.WOULD_TOUCH
    pc = rows["config/priority-config.json"]
    assert (pc.action, pc.archived_since, pc.deletes_on, pc.repo_default) == (
        tc.ARCHIVED, "2026-10-07", "2027-06-09", "config/priority-config.json")
    ce = rows["config/code-editor-config.json"]
    assert (ce.action, ce.archived_since, ce.archive_status, ce.repo_default) == (
        tc.ARCHIVED, "2026-09-21", "rehydrate-pending-to-hot", None)
    assert rows["config/stream.json"].action == tc.SKIPPED
    assert report.tiers() == {"Archive": 2, "Cool": 2, "Hot": 1, "n/a": 1}
    assert [r.key for r in report.at_risk()] == ["config/required-models.json"]
    assert c.uploads == [] and c.downloads == 0  # a dry run only lists
    assert c.list_calls == [("config/", ["metadata"])]
    text = tc.render_text(report)
    assert "ARCHIVED since 2026-10-07; the lifecycle rule deletes it on 2027-06-09" in text
    assert "repo default: config/priority-config.json" in text
    assert "archives 2027-01-29 (in 72d) AT RISK -> would-touch" in text
    assert report.exit_code() == tc.EXIT_OK
    assert report.exit_code(fail_on_archived=True) == tc.EXIT_ARCHIVED


def test_azure_touch_is_conditional_hot_and_keeps_settings():
    c = _fleet_like_container()
    report = tc.touch_configs(tc.AzureConfigStore(c, sdk=SDK), now=NOW)
    assert report.counts() == {tc.TOUCHED: 3, tc.ARCHIVED: 2, tc.SKIPPED: 1}
    assert report.exit_code() == tc.EXIT_OK
    uploads = {name: (data, kw) for name, data, kw in c.uploads}
    assert sorted(uploads) == ["config/ai-defaults.json",
                               "config/implementer-allowed-dispatch-kinds.json",
                               "config/required-models.json"]
    data, kw = uploads["config/required-models.json"]
    assert data == b'{"by_agent_id": {}}'
    assert kw["overwrite"] is True and kw["match_condition"] == "IfNotModified"
    assert kw["etag"] and kw["standard_blob_tier"] == "Hot"
    assert kw["content_settings"].content_type == "application/json"
    assert kw["content_settings"].cache_control == "no-cache"
    assert kw["metadata"] == {"owner": "ops"}
    blob = c.blobs["config/required-models.json"]
    assert (blob.tier, blob.modified, blob.data) == ("Hot", NOW, b'{"by_agent_id": {}}')
    row = next(r for r in report.results if r.key == "config/required-models.json")
    assert (row.action, row.tier, row.tier_after) == (tc.TOUCHED, "Cool", "Hot")
    assert "(Cool -> Hot)" in tc.render_text(report)
    # Archived blobs are never read, deleted or written.
    assert c.blobs["config/priority-config.json"].tier == "Archive"


def test_azure_concurrent_change_is_not_reverted():
    c = FakeContainer()
    c.blobs["config/x.json"] = FakeBlob(b'{"v": 1}', tier="Cool")

    def other_writer(name: str) -> None:
        c.blobs[name] = FakeBlob(b'{"v": 2}', tier="Hot", modified=NOW)

    c.after_download = other_writer
    report = tc.touch_configs(tc.AzureConfigStore(c, sdk=SDK), now=NOW)
    (row,) = report.results
    assert row.action == tc.CHANGED
    assert c.blobs["config/x.json"].data == b'{"v": 2}'
    assert report.exit_code() == tc.EXIT_OK


def test_azure_archived_after_listing_is_reported():
    c = FakeContainer()
    c.blobs["config/read.json"] = FakeBlob(b"{}", tier="Cool")
    c.blobs["config/write.json"] = FakeBlob(b"{}", tier="Cool")
    store = tc.AzureConfigStore(c, sdk=SDK)
    states = store.list_states("config/")
    c.blobs["config/read.json"].tier = "Archive"  # archived between list and read

    def archive_after_read(name: str) -> None:
        if name == "config/write.json":
            c.blobs[name].tier = "Archive"  # archived between read and write

    c.after_download = archive_after_read
    store.list_states = lambda prefix: states  # type: ignore[method-assign]
    report = tc.touch_configs(store, now=NOW)
    rows = {r.key: r for r in report.results}
    assert (rows["config/read.json"].action, rows["config/read.json"].detail) == (
        tc.ARCHIVED, "archived (the read was refused)")
    assert (rows["config/write.json"].action, rows["config/write.json"].detail) == (
        tc.ARCHIVED, "archived (the rewrite was refused)")


def test_azure_vanished_and_failed():
    c = FakeContainer()
    c.phantoms.append("config/gone.json")
    c.blobs["config/denied.json"] = FakeBlob(b"{}", tier="Cool")
    c.upload_error = FakeHttpError("This request is not authorized.\nRequestId:abc",
                                   status_code=403, error_code="AuthorizationFailure")
    report = tc.touch_configs(tc.AzureConfigStore(c, sdk=SDK), now=NOW)
    rows = {r.key: r for r in report.results}
    assert rows["config/gone.json"].action == tc.VANISHED
    assert rows["config/denied.json"].action == tc.FAILED
    assert rows["config/denied.json"].detail == "write: FakeHttpError: This request is not authorized."
    assert report.exit_code() == tc.EXIT_FAILED


def test_main_with_an_azure_store_prints_json():
    c = _fleet_like_container()
    rc, out = _run(["--dry-run", "--json", "--fail-on-archived"],
                   store=tc.AzureConfigStore(c, sdk=SDK), now=NOW)
    assert rc == tc.EXIT_ARCHIVED
    doc = json.loads(out)
    assert doc["backend"] == "azure" and doc["dry_run"] is True
    assert doc["counts"] == {"would-touch": 3, "archived": 2, "skipped": 1}
    assert doc["at_risk"] == []  # default 30-day window
    assert c.uploads == []


def test_listing_failure_is_reported_cleanly(capsys):
    class Broken:
        backend = "azure"
        reports_tiers = True

        def list_states(self, prefix: str) -> list[tc.BlobState]:
            raise FakeHttpError("Server failed to authenticate the request.\nRequestId:y",
                                status_code=403, error_code="AuthenticationFailed")

        def read(self, state: tc.BlobState) -> Optional[tuple[bytes, Optional[str]]]:
            raise AssertionError("never reached")

        def rewrite(self, state: tc.BlobState, data: bytes, etag: Optional[str]) -> Optional[str]:
            raise AssertionError("never reached")

    assert tc.main([], store=Broken(), out=io.StringIO()) == tc.EXIT_FAILED
    assert "cannot list config/ on azure storage: FakeHttpError" in capsys.readouterr().err


def test_store_for_picks_the_backend(storage, monkeypatch):
    assert isinstance(tc.store_for(storage), tc.StorageConfigStore)
    fake = FakeContainer()
    store = tc.store_for(SimpleNamespace(name="azure", _container=fake))
    assert isinstance(store, tc.AzureConfigStore) and store.container is fake
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    assert isinstance(tc.store_for(SimpleNamespace(name="azure", _container=None)),
                      tc.StorageConfigStore)


def test_azure_gzip_encoded_blob_is_rewritten_byte_identically():
    stored = gzip.compress(b'{"by_agent_id": {"x": "opus"}}')
    c = FakeContainer()
    c.blobs["config/required-models.json"] = FakeBlob(
        stored, tier="Cool", content_settings={"content_encoding": "gzip"})
    report = tc.touch_configs(tc.AzureConfigStore(c, sdk=SDK), now=NOW)
    assert report.counts() == {tc.TOUCHED: 1}
    ((_, data, kw),) = c.uploads
    assert data == stored  # the stored gzip bytes, not the SDK's decompressed view of them
    assert kw["content_settings"].content_encoding == "gzip"
    assert c.blobs["config/required-models.json"].data == stored


def test_azure_rewrite_keeps_the_headers_of_the_version_it_read():
    c = FakeContainer()
    c.blobs["config/x.json"] = FakeBlob(b'{"v": 1}', tier="Cool",
                                        content_settings={"cache_control": "no-cache"},
                                        metadata={"owner": "ops"})
    store = tc.AzureConfigStore(c, sdk=SDK)
    states = store.list_states("config/")
    # A writer replaces the blob, headers included, between our listing and our read.
    c.blobs["config/x.json"] = FakeBlob(b'{"v": 2}', tier="Hot", modified=NOW,
                                        content_settings={"cache_control": "max-age=60"})
    store.list_states = lambda prefix: states  # type: ignore[method-assign]
    report = tc.touch_configs(store, now=NOW)
    assert report.counts() == {tc.TOUCHED: 1}
    ((_, data, kw),) = c.uploads
    assert data == b'{"v": 2}'
    assert kw["content_settings"].cache_control == "max-age=60"
    assert kw["metadata"] is None  # the listing's owner=ops is not brought back
    blob = c.blobs["config/x.json"]
    assert (blob.data, blob.content_settings["cache_control"], blob.metadata) == (
        b'{"v": 2}', "max-age=60", {})
    # Without a read through the store, the rewrite falls back to the listing.
    tc.AzureConfigStore(c, sdk=SDK).rewrite(states[0], b'{"v": 2}', None)
    assert c.uploads[-1][2]["content_settings"].cache_control == "no-cache"


def test_lifecycle_thresholds_are_configurable_and_reported(seeded):
    c = _fleet_like_container()
    rc, out = _run(["--dry-run", "--json", "--archive-after-days", "90",
                    "--delete-after-days", "200"], store=tc.AzureConfigStore(c, sdk=SDK), now=NOW)
    assert rc == tc.EXIT_OK
    doc = json.loads(out)
    assert doc["lifecycle"] == {"archive_after_days": 90, "delete_after_days": 200}
    row = next(r for r in doc["results"] if r["key"] == "config/required-models.json")
    written = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    assert row["archives_on"] == (written + timedelta(days=90)).date().isoformat()
    assert row["deletes_on"] == (written + timedelta(days=200)).date().isoformat()
    _, text = _run(["--dry-run"], store=tc.AzureConfigStore(c, sdk=SDK), now=NOW)
    assert "dates assume the lifecycle rule archives 120d and deletes 365d after the last write" in text
    _, local = _run(["--dry-run", "--json"], storage=seeded, now=NOW)
    assert json.loads(local)["lifecycle"] is None  # no tiers, so no projection
    with pytest.raises(SystemExit) as exc:
        tc.main(["--archive-after-days", "0"], store=tc.AzureConfigStore(c, sdk=SDK),
                out=io.StringIO())
    assert exc.value.code == tc.EXIT_USAGE


def test_require_backend_refuses_another_backend(seeded, capsys):
    assert tc.main(["--require-backend", "azure"], storage=seeded, out=io.StringIO()) == tc.EXIT_FAILED
    assert "refusing to run on local storage: --require-backend azure" in capsys.readouterr().err
    assert _mtime(seeded, "config/a-config.json") == pytest.approx(OLD.timestamp())  # untouched
    rc, out = _run(["--require-backend", "local", "--dry-run"], storage=seeded)
    assert rc == tc.EXIT_OK and "-> would-touch" in out


# ---------------------------------------------------------------------------
# The real azure-storage-blob SDK against an in-process fake Blob endpoint
# ---------------------------------------------------------------------------

@dataclass
class WireBlob:
    """A blob as the fake service stores it (``data`` is the stored bytes)."""

    data: bytes
    tier: Optional[str] = "Hot"
    modified: datetime = OLD
    tier_changed: Optional[datetime] = None
    content_type: str = "application/json"
    content_encoding: str = ""
    cache_control: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    etag: str = ""


def _http_date(dt: datetime) -> str:
    return format_datetime(dt, usegmt=True)


class WireService:
    """Just enough of the Blob REST API for touch_configs: List Blobs, Get Blob
    and Put Blob (honouring If-Match and x-ms-access-tier), with the service's
    error responses. ``container()`` builds a real ``ContainerClient`` whose
    requests transport is wired to this object, so nothing leaves the process
    and the SDK's own request building, signing and response parsing run."""

    def __init__(self) -> None:
        self.blobs: dict[str, WireBlob] = {}
        self.phantoms: set[str] = set()  # listed, then 404 on read
        self.refuse: dict[tuple[str, str], tuple[int, str, str]] = {}  # (method, name) -> error
        self.after_get: Optional[Callable[[str], None]] = None
        #: (method, blob name or None, lower-cased headers, query, body) per request.
        self.log: list[tuple[str, Optional[str], dict[str, str], dict[str, list[str]], bytes]] = []
        self._etags = 0
        self._requests: Any = None
        self._urllib3: Any = None

    def add(self, name: str, blob: WireBlob) -> WireBlob:
        self._etags += 1
        blob.etag = f'"0x8DE{self._etags:010d}"'
        self.blobs[name] = blob
        return blob

    def container(self) -> Any:
        blob_sdk = pytest.importorskip("azure.storage.blob")
        transport = pytest.importorskip("azure.core.pipeline.transport")
        self._requests = pytest.importorskip("requests")
        self._urllib3 = pytest.importorskip("urllib3")
        service = self

        class Adapter(self._requests.adapters.BaseAdapter):  # type: ignore[misc,name-defined]
            def send(self, request: Any, **kwargs: Any) -> Any:
                return service.handle(request)

            def close(self) -> None:
                pass

        session = self._requests.Session()
        session.trust_env = False
        session.mount("https://", Adapter())
        key = base64.b64encode(b"k" * 32).decode()
        conn = ("DefaultEndpointsProtocol=https;AccountName=fakeacct;"
                f"AccountKey={key};EndpointSuffix=core.windows.net")
        return blob_sdk.ContainerClient.from_connection_string(
            conn, "agents", transport=transport.RequestsTransport(session=session), retry_total=0)

    def handle(self, request: Any) -> Any:
        url = urlparse(request.url)
        query = parse_qs(url.query)
        parts = url.path.lstrip("/").split("/", 1)
        name = unquote(parts[1]) if len(parts) > 1 else None
        body = request.body or b""
        if hasattr(body, "read"):
            body = body.read()
        elif not isinstance(body, (bytes, bytearray, str)):
            body = b"".join(body)
        body = body.encode() if isinstance(body, str) else bytes(body)
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.log.append((request.method, name, headers, query, body))
        refused = self.refuse.get((request.method, name or ""))
        if refused:
            return self._error(request, *refused)
        if request.method == "GET" and name is None and query.get("comp") == ["list"]:
            return self._list(request, query.get("prefix", [""])[0])
        if request.method == "GET" and name:
            return self._get(request, name)
        if request.method == "PUT" and name:
            return self._put(request, name, headers, body)
        return self._error(request, 400, "UnsupportedHttpVerb", f"{request.method} {request.url}")

    def _list(self, request: Any, prefix: str) -> Any:
        items = []
        for name in sorted(set(self.blobs) | self.phantoms):
            if not name.startswith(prefix):
                continue
            b = self.blobs.get(name) or WireBlob(b"{}", tier="Cool", etag='"0xGONE"')
            meta = "".join(f"<{k}>{v}</{k}>" for k, v in b.metadata.items())
            tier = f"<AccessTier>{b.tier}</AccessTier>" if b.tier else ""
            if b.tier_changed:
                tier += f"<AccessTierChangeTime>{_http_date(b.tier_changed)}</AccessTierChangeTime>"
            items.append(
                f"<Blob><Name>{name}</Name><Properties>"
                f"<Creation-Time>{_http_date(b.modified)}</Creation-Time>"
                f"<Last-Modified>{_http_date(b.modified)}</Last-Modified><Etag>{b.etag}</Etag>"
                f"<Content-Length>{len(b.data)}</Content-Length>"
                f"<Content-Type>{b.content_type}</Content-Type>"
                f"<Content-Encoding>{b.content_encoding}</Content-Encoding><Content-Language />"
                f"<Content-MD5 /><Cache-Control>{b.cache_control}</Cache-Control>"
                f"<Content-Disposition /><BlobType>BlockBlob</BlobType>{tier}"
                "<LeaseStatus>unlocked</LeaseStatus><LeaseState>available</LeaseState>"
                f"<ServerEncrypted>true</ServerEncrypted></Properties><Metadata>{meta}</Metadata></Blob>")
        xml = ('<?xml version="1.0" encoding="utf-8"?><EnumerationResults '
               'ServiceEndpoint="https://fakeacct.blob.core.windows.net/" ContainerName="agents">'
               f'<Prefix>{prefix}</Prefix><Blobs>{"".join(items)}</Blobs><NextMarker />'
               "</EnumerationResults>")
        return self._response(request, 200, xml.encode(), {"Content-Type": "application/xml"})

    def _get(self, request: Any, name: str) -> Any:
        b = self.blobs.get(name)
        if b is None:
            return self._error(request, 404, "BlobNotFound", "The specified blob does not exist.")
        if b.tier == "Archive":
            return self._error(request, 409, "BlobArchived",
                               "This operation is not permitted on an archived blob.")
        headers = {"Content-Type": b.content_type, "ETag": b.etag,
                   "Last-Modified": _http_date(b.modified), "x-ms-blob-type": "BlockBlob",
                   "Content-Range": f"bytes 0-{len(b.data) - 1}/{len(b.data)}",
                   "Accept-Ranges": "bytes"}
        if b.content_encoding:
            headers["Content-Encoding"] = b.content_encoding
        if b.cache_control:
            headers["Cache-Control"] = b.cache_control
        headers.update({f"x-ms-meta-{k}": v for k, v in b.metadata.items()})
        response = self._response(request, 206, b.data, headers)
        if self.after_get is not None:
            self.after_get(name)
        return response

    def _put(self, request: Any, name: str, headers: dict[str, str], body: bytes) -> Any:
        b = self.blobs.get(name)
        if b is not None and b.tier == "Archive":
            return self._error(request, 409, "BlobArchived",
                               "This operation is not permitted on an archived blob.")
        if headers.get("if-match") and (b is None or b.etag != headers["if-match"]):
            return self._error(request, 412, "ConditionNotMet",
                               "The condition specified using HTTP conditional header(s) is not met.")
        new = self.add(name, WireBlob(
            body, tier=headers.get("x-ms-access-tier") or (b.tier if b else "Hot"), modified=NOW,
            content_type=headers.get("x-ms-blob-content-type", "application/octet-stream"),
            content_encoding=headers.get("x-ms-blob-content-encoding", ""),
            cache_control=headers.get("x-ms-blob-cache-control", ""),
            metadata={k[len("x-ms-meta-"):]: v for k, v in headers.items()
                      if k.startswith("x-ms-meta-")}))
        return self._response(request, 201, b"", {"ETag": new.etag, "Last-Modified": _http_date(NOW),
                                                  "x-ms-request-server-encrypted": "true"})

    def _error(self, request: Any, status: int, code: str, message: str) -> Any:
        body = ('<?xml version="1.0" encoding="utf-8"?><Error>'
                f"<Code>{code}</Code><Message>{message}\nRequestId:rid</Message></Error>").encode()
        return self._response(request, status, body,
                              {"x-ms-error-code": code, "Content-Type": "application/xml"})

    def _response(self, request: Any, status: int, body: bytes, headers: dict[str, str]) -> Any:
        all_headers = {"x-ms-request-id": "rid", "x-ms-version": "2025-01-05",
                       "Date": _http_date(NOW), "Content-Length": str(len(body)), **headers}
        response = self._requests.Response()
        response.status_code = status
        response.raw = self._urllib3.response.HTTPResponse(
            body=io.BytesIO(body), headers=all_headers, status=status,
            preload_content=False, decode_content=False)
        response.headers = self._requests.structures.CaseInsensitiveDict(all_headers)
        response.url = request.url
        response.request = request
        response.reason = {200: "OK", 201: "Created", 206: "Partial Content"}.get(status, "Error")
        return response


def _wire_fleet() -> WireService:
    svc = WireService()
    svc.add("config/required-models.json", WireBlob(
        b'{"by_agent_id": {}}', tier="Cool", modified=datetime(2026, 10, 1, 9, tzinfo=timezone.utc),
        cache_control="no-cache", metadata={"owner": "ops"}))
    svc.add("config/ai-providers.json", WireBlob(
        gzip.compress(b'{"providers": []}'), tier="Hot", content_encoding="gzip"))
    svc.add("config/priority-config.json", WireBlob(
        b"{}", tier="Archive", modified=datetime(2026, 6, 9, 8, tzinfo=timezone.utc),
        tier_changed=datetime(2026, 10, 7, 3, tzinfo=timezone.utc)))
    svc.add("config/amazon-creators-usage/2026-11-01/x.json", WireBlob(b"{}", tier="Cool"))
    return svc


def test_real_sdk_dry_run_is_a_single_list_request():
    svc = _wire_fleet()
    report = tc.touch_configs(tc.AzureConfigStore(svc.container()), dry_run=True, now=NOW)
    assert [(m, n, q.get("comp"), q.get("prefix"), q.get("include"))
            for m, n, _, q, _ in svc.log] == [("GET", None, ["list"], ["config/"], ["metadata"])]
    rows = {r.key: r for r in report.results}
    assert sorted(rows) == ["config/ai-providers.json", "config/priority-config.json",
                            "config/required-models.json"]
    rm = rows["config/required-models.json"]
    assert (rm.tier, rm.archives_on, rm.action) == ("Cool", "2027-01-29", tc.WOULD_TOUCH)
    pc = rows["config/priority-config.json"]
    assert (pc.tier, pc.action, pc.archived_since) == ("Archive", tc.ARCHIVED, "2026-10-07")
    assert report.counts() == {tc.WOULD_TOUCH: 2, tc.ARCHIVED: 1}


def test_real_sdk_touch_puts_the_stored_bytes_back_hot_and_conditional():
    svc = _wire_fleet()
    before = {k: (b.data, b.etag) for k, b in svc.blobs.items()}
    report = tc.touch_configs(tc.AzureConfigStore(svc.container()), now=NOW)
    assert report.counts() == {tc.TOUCHED: 2, tc.ARCHIVED: 1}
    puts = {n: (h, body) for m, n, h, _, body in svc.log if m == "PUT" and n}
    assert sorted(puts) == ["config/ai-providers.json", "config/required-models.json"]
    headers, body = puts["config/required-models.json"]
    data, etag = before["config/required-models.json"]
    assert body == data
    assert headers["if-match"] == etag
    assert headers["x-ms-access-tier"] == "Hot"
    assert headers["x-ms-blob-type"] == "BlockBlob"
    assert headers["x-ms-blob-content-type"] == "application/json"
    assert headers["x-ms-blob-cache-control"] == "no-cache"
    assert headers["x-ms-meta-owner"] == "ops"
    assert svc.blobs["config/required-models.json"].tier == "Hot"
    # A gzip-encoded config goes back as the same gzip bytes, still labelled gzip.
    headers, body = puts["config/ai-providers.json"]
    assert body == before["config/ai-providers.json"][0]
    assert headers["x-ms-blob-content-encoding"] == "gzip"
    assert gzip.decompress(svc.blobs["config/ai-providers.json"].data) == b'{"providers": []}'
    # The archived blob was neither read nor written.
    assert [n for _, n, *_ in svc.log if n == "config/priority-config.json"] == []


def test_real_sdk_maps_the_service_errors():
    svc = WireService()
    svc.add("config/denied.json", WireBlob(b"{}", tier="Cool"))
    svc.add("config/archived-late.json", WireBlob(b"{}", tier="Cool"))
    svc.phantoms.add("config/gone.json")
    svc.refuse[("PUT", "config/denied.json")] = (
        403, "AuthorizationPermissionMismatch",
        "This request is not authorized to perform this operation using this permission.")
    store = tc.AzureConfigStore(svc.container())
    states = store.list_states("config/")
    svc.blobs["config/archived-late.json"].tier = "Archive"  # archived after the listing
    store.list_states = lambda prefix: states  # type: ignore[method-assign]
    report = tc.touch_configs(store, now=NOW)
    rows = {r.key: (r.action, r.detail) for r in report.results}
    assert rows["config/gone.json"][0] == tc.VANISHED                     # 404
    assert rows["config/archived-late.json"] == (tc.ARCHIVED, "archived (the read was refused)")  # 409
    assert rows["config/denied.json"][0] == tc.FAILED                     # 403
    assert "not authorized" in rows["config/denied.json"][1]
    assert report.exit_code() == tc.EXIT_FAILED


def test_real_sdk_never_reverts_a_concurrent_write():
    svc = WireService()
    svc.add("config/a.json", WireBlob(b'{"v": 1}', tier="Cool", cache_control="no-cache",
                                      metadata={"owner": "ops"}))
    svc.add("config/b.json", WireBlob(b'{"v": 1}', tier="Cool"))
    store = tc.AzureConfigStore(svc.container())
    states = store.list_states("config/")
    # a.json is replaced, headers included, between the listing and the read.
    svc.add("config/a.json", WireBlob(b'{"v": 2}', tier="Hot", cache_control="max-age=60"))

    def write_b_after_our_read(name: str) -> None:
        if name == "config/b.json":
            svc.add(name, WireBlob(b'{"v": 3}', tier="Hot"))

    svc.after_get = write_b_after_our_read
    store.list_states = lambda prefix: states  # type: ignore[method-assign]
    report = tc.touch_configs(store, now=NOW)
    assert {r.key: r.action for r in report.results} == {
        "config/a.json": tc.TOUCHED, "config/b.json": tc.CHANGED}
    a = svc.blobs["config/a.json"]
    assert (a.data, a.cache_control, a.metadata) == (b'{"v": 2}', "max-age=60", {})
    assert svc.blobs["config/b.json"].data == b'{"v": 3}'  # 412: left as the writer left it
