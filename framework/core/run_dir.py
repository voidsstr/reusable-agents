"""Run-directory abstraction for multi-stage pipelines (SEO collector →
analyzer → reporter → implementer → deployer, etc.).

A "run dir" is identified by (agent_id, run_ts). Its files live in the
configured StorageBackend under:

    agents/<agent_id>/runs/<run_ts>/

This module exposes:

  RunDir(agent_id, run_ts)
      .read_json(relpath)            -> Any | None
      .write_json(relpath, data)
      .read_text(relpath)            -> str | None
      .write_text(relpath, text)
      .read_bytes(relpath)           -> bytes | None
      .write_bytes(relpath, data)
      .exists(relpath)               -> bool
      .list_files(sub_prefix='')     -> list[str]    # relative paths
      .delete(relpath)               -> bool
      .copy_file_in(local_path, relpath)
      .materialize(local_root)       # pull all files to a local dir
      .sync_back(local_root, only_new=True)  # push local changes back
      .tempdir()                     # context manager: materialize + sync

  rundir_from_env() / rundir_from_args(args)
      Construct a RunDir from RESPONDER_AGENT_ID / RESPONDER_RUN_TS env
      vars, or from CLI args, or from a legacy --run-dir path (fallback).

The intent: every SEO sub-agent script reads/writes files through this
class instead of touching pathlib.Path directly. The storage of record
is Azure Blob (the StorageBackend); local filesystem use is reduced to
short-lived temp working dirs for tools that need a real FS (claude
--print, bash deploy steps), with sync_back() copying changes back to
Azure when the stage exits.

Backwards-compat: if `LEGACY_RUNS_ROOT_MIRROR` env var is set to a path,
every write is also mirrored to <LEGACY_RUNS_ROOT_MIRROR>/<site>/<run_ts>/
so any external script still reading the legacy path keeps working.
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.run_dir")


def _legacy_mirror_root() -> Optional[Path]:
    """If LEGACY_RUNS_ROOT_MIRROR is set, return the path; else None."""
    p = os.environ.get("LEGACY_RUNS_ROOT_MIRROR", "").strip()
    if not p:
        return None
    return Path(os.path.expanduser(p))


class RunDir:
    """Identifies and accesses files in a run dir backed by StorageBackend.

    Run-dir layout (relpaths the SEO pipeline writes):

        data/                          # collector raw output
            gsc-*.json                 # GSC reports
            ga4-*.json                 # GA4 reports
            ads-*.json                 # Google Ads reports (optional)
            db-stats.json              # DB report (optional)
            pages-by-type.jsonl        # page-type inventory crawl
            page-cache/<sha1>.html     # crawl HTML cache
            repo-routes.json           # repo route scan
        snapshot.json                  # analyzer snapshot
        comparison.json                # snapshot vs prior
        recommendations.json           # the contract
        goals.json                     # this run's declared goals
        goal-progress.json             # prior run's goals scored
        responses.json                 # legacy: per-run user replies
        run-summary.md                 # short markdown for reporter
        changes/                       # implementer artifacts
            rec-001.diff
            rec-001.summary.md
        deploy.json                    # deployer output
        results.json                   # implementer end-of-run summary
        llm-output.txt                 # implementer claude transcript
        context-summary.md             # framework-level run summary
    """

    def __init__(
        self,
        agent_id: str,
        run_ts: str,
        *,
        storage: Optional[StorageBackend] = None,
        # Optional site id — only used to compute the legacy-mirror dir.
        # Not needed for Azure paths (which key by agent_id + run_ts only).
        site: Optional[str] = None,
    ):
        if not agent_id:
            raise ValueError("RunDir requires non-empty agent_id")
        if not run_ts:
            raise ValueError("RunDir requires non-empty run_ts")
        self.agent_id = agent_id
        self.run_ts = run_ts
        self.site = site
        self.storage = storage or get_storage()
        self.prefix = f"agents/{agent_id}/runs/{run_ts}"

    # ---- Identity / formatting ----

    @property
    def handle(self) -> str:
        """Stable string identifier passed between stages: '<agent_id>:<run_ts>'."""
        return f"{self.agent_id}:{self.run_ts}"

    def __repr__(self) -> str:
        return f"RunDir(agent_id={self.agent_id!r}, run_ts={self.run_ts!r})"

    # ---- Path helpers ----

    def key(self, relpath: str) -> str:
        """Compose a storage key from a run-dir-relative path."""
        rp = relpath.lstrip("/")
        return f"{self.prefix}/{rp}" if rp else self.prefix

    # ---- Core read/write API ----

    def exists(self, relpath: str) -> bool:
        return self.storage.exists(self.key(relpath))

    def read_bytes(self, relpath: str) -> Optional[bytes]:
        return self.storage.read_bytes(self.key(relpath))

    def write_bytes(self, relpath: str, data: bytes) -> None:
        self.storage.write_bytes(self.key(relpath), data)
        self._mirror_write(relpath, data)

    def read_text(self, relpath: str, encoding: str = "utf-8") -> Optional[str]:
        return self.storage.read_text(self.key(relpath), encoding=encoding)

    def write_text(self, relpath: str, text: str, encoding: str = "utf-8") -> None:
        self.storage.write_text(self.key(relpath), text, encoding=encoding)
        self._mirror_write(relpath, text.encode(encoding))

    def read_json(self, relpath: str) -> Optional[Any]:
        return self.storage.read_json(self.key(relpath))

    def write_json(self, relpath: str, obj: Any, indent: int = 2) -> None:
        self.storage.write_json(self.key(relpath), obj, indent=indent)
        # Mirror via the same JSON path
        try:
            import json as _json
            self._mirror_write(relpath, _json.dumps(obj, indent=indent, default=str).encode("utf-8"))
        except Exception:
            pass

    def append_jsonl(self, relpath: str, obj: Any) -> None:
        self.storage.append_jsonl(self.key(relpath), obj)
        # Mirror
        try:
            import json as _json
            line = (_json.dumps(obj, default=str) + "\n").encode("utf-8")
            self._mirror_append(relpath, line)
        except Exception:
            pass

    def delete(self, relpath: str) -> bool:
        return self.storage.delete(self.key(relpath))

    def list_files(self, sub_prefix: str = "") -> list[str]:
        """List all files under <run-dir>/<sub_prefix>/. Returns relpaths."""
        full_prefix = self.key(sub_prefix.rstrip("/")) + "/" if sub_prefix else self.prefix + "/"
        full_keys = self.storage.list_prefix(full_prefix)
        out = []
        prefix_len = len(self.prefix) + 1  # strip 'agents/<id>/runs/<ts>/'
        for k in full_keys:
            if k.startswith(self.prefix + "/"):
                out.append(k[prefix_len:])
        return sorted(out)

    def copy_file_in(self, local_path: str | Path, relpath: str) -> None:
        """Upload a local file into the run dir."""
        p = Path(local_path)
        if not p.is_file():
            raise FileNotFoundError(local_path)
        self.write_bytes(relpath, p.read_bytes())

    # ---- Local-FS materialization ----

    def materialize(self, local_root: str | Path) -> Path:
        """Pull every blob under self.prefix into local_root, preserving
        relative paths. Returns the resolved local_root. Used by stages
        that need a real local FS (e.g. claude --print, bash deploy)."""
        root = Path(local_root)
        root.mkdir(parents=True, exist_ok=True)
        for relpath in self.list_files(""):
            blob = self.read_bytes(relpath)
            if blob is None:
                continue
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        return root

    def sync_back(
        self,
        local_root: str | Path,
        *,
        only_new: bool = True,
        skip_globs: Optional[list[str]] = None,
    ) -> int:
        """Push every file under local_root back to Azure. Returns # synced.

        only_new=True (default) skips files whose Azure blob bytes already
        match the local file (idempotent re-syncs). only_new=False
        unconditionally overwrites everything (useful when you know the
        local copy is authoritative).

        skip_globs is a list of fnmatch patterns; matching relpaths are
        skipped (e.g., page cache files).
        """
        import fnmatch
        root = Path(local_root)
        if not root.is_dir():
            return 0
        skip_globs = skip_globs or []
        synced = 0
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, g) for g in skip_globs):
                continue
            local_bytes = path.read_bytes()
            if only_new:
                remote = self.read_bytes(rel)
                if remote is not None and remote == local_bytes:
                    continue
            self.write_bytes(rel, local_bytes)
            synced += 1
        return synced

    @contextlib.contextmanager
    def tempdir(
        self,
        *,
        sync_back_on_exit: bool = True,
        skip_globs: Optional[list[str]] = None,
        structured_parent: bool = False,
    ) -> Iterator[Path]:
        """Context manager: materialize the run dir on local FS for tools
        that need a real filesystem path. On exit, syncs new/changed
        files back to Azure (unless sync_back_on_exit=False).

        structured_parent=True creates a parent layout that mirrors the
        legacy SEO runs root: <tmp>/<site>/<run_ts>/. Use this for code
        paths (e.g. analyzer.find_prior_snapshot) that walk
        cfg.site_runs_dir to find sibling run dirs. Also pulls every
        prior run's snapshot.json + goals.json from Azure into the
        sibling layout so find_prior_snapshot finds them.
        """
        if structured_parent:
            base = Path(tempfile.mkdtemp(prefix=f"rundir-{self.agent_id}-"))
            site_dir = base / (self.site or "_unknown")
            td = site_dir / self.run_ts
            td.mkdir(parents=True, exist_ok=True)
            try:
                self.materialize(td)
                # Mirror prior runs' snapshot.json + goals.json so the analyzer
                # can find them via cfg.site_runs_dir/<prior_ts>/snapshot.json.
                self._mirror_prior_runs_for_analyzer(site_dir)
                yield td
                if sync_back_on_exit:
                    count = self.sync_back(td, only_new=True, skip_globs=skip_globs)
                    if count:
                        logger.info(f"RunDir.sync_back: {count} files → {self.prefix}")
            finally:
                shutil.rmtree(base, ignore_errors=True)
            return

        td = Path(tempfile.mkdtemp(prefix=f"rundir-{self.agent_id}-{self.run_ts}-"))
        try:
            self.materialize(td)
            yield td
            if sync_back_on_exit:
                count = self.sync_back(td, only_new=True, skip_globs=skip_globs)
                if count:
                    logger.info(f"RunDir.sync_back: {count} files → {self.prefix}")
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def _mirror_prior_runs_for_analyzer(self, site_dir: Path) -> None:
        """Pull each prior run's snapshot.json + goals.json into a sibling
        dir under site_dir so find_prior_snapshot() works in tempdir mode."""
        prefix = f"agents/{self.agent_id}/runs/"
        keys = self.storage.list_prefix(prefix)
        prior_ts: set[str] = set()
        for k in keys:
            rest = k[len(prefix):]
            if "/" not in rest:
                continue
            ts = rest.split("/", 1)[0]
            if ts and ts != self.run_ts:
                prior_ts.add(ts)
        for ts in prior_ts:
            sib = site_dir / ts
            sib.mkdir(parents=True, exist_ok=True)
            for relpath in ("snapshot.json", "goals.json"):
                data = self.storage.read_bytes(f"{prefix}{ts}/{relpath}")
                if data is not None:
                    (sib / relpath).write_bytes(data)

    # ---- Legacy-mirror helpers (best-effort, off by default) ----

    def _mirror_path(self, relpath: str) -> Optional[Path]:
        root = _legacy_mirror_root()
        if not root or not self.site:
            return None
        return root / self.site / self.run_ts / relpath.lstrip("/")

    def _mirror_write(self, relpath: str, data: bytes) -> None:
        p = self._mirror_path(relpath)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        except Exception as e:
            logger.debug(f"legacy mirror write failed for {relpath}: {e}")

    def _mirror_append(self, relpath: str, data: bytes) -> None:
        p = self._mirror_path(relpath)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("ab") as f:
                f.write(data)
        except Exception as e:
            logger.debug(f"legacy mirror append failed for {relpath}: {e}")


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------

def parse_handle(handle: str) -> tuple[str, str]:
    """Parse a '<agent_id>:<run_ts>' handle. Raises on malformed input."""
    if ":" not in handle:
        raise ValueError(f"bad RunDir handle (expected 'agent:ts'): {handle!r}")
    agent_id, run_ts = handle.rsplit(":", 1)
    if not agent_id or not run_ts:
        raise ValueError(f"bad RunDir handle: {handle!r}")
    return agent_id.strip(), run_ts.strip()


def rundir_from_env(
    *,
    agent_env: str = "RESPONDER_AGENT_ID",
    run_ts_env: str = "RESPONDER_RUN_TS",
    site_env: str = "RESPONDER_SITE",
    storage: Optional[StorageBackend] = None,
) -> Optional[RunDir]:
    """Construct a RunDir from process env vars. Returns None if either is unset."""
    agent_id = os.environ.get(agent_env, "").strip()
    run_ts = os.environ.get(run_ts_env, "").strip()
    if not agent_id or not run_ts:
        return None
    site = os.environ.get(site_env, "").strip() or None
    return RunDir(agent_id, run_ts, storage=storage, site=site)
