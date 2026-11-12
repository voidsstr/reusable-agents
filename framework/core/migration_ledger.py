"""Migration ledger: keep a site repo's migrations directory, git, and the
database's applied-migrations table telling the same story.

WHY THIS EXISTS
---------------
A site's boot-time migration runner applies every ``*.sql`` file in its
migrations directory that its applied table (``_migrations``) does not list.
It does not ask git. So an untracked migration file in a checkout is one
server start away from production.

On 2026-11-11 at 14:44:31Z an implementer session checked an SSR change by
starting a site's server from the live checkout with the production DSN.
The runner applied 57 catalog-audit migrations that earlier implementer runs
had written and never committed (a gate had discarded those runs with
``git reset --hard``, which keeps untracked files). Production changed, 82
products went dark, and the only record was a working tree on one host.

Two states are wrong, and this module names both:

  applied, uncommitted   the database ran the file but git does not track it.
                         Production changed and the repo has no record.
  stranded               nobody applied or committed the file. It waits in
                         the directory for the next local boot.

``plan()`` is pure. ``reconcile()`` acts on a plan, and only as configured:
it commits applied files (the record of a change production already has)
and, if asked, moves stranded files out of the directory into a quarantine
folder, so no later boot can apply them. Files younger than
``min_age_minutes`` are left alone because an in-flight run may still commit
them.

The applied table records a name and a time, not a checksum. When
``applied_sql`` also returns the time (second column), a file modified after
it was applied is not committed as the record of what ran: its content may
not be what production executed. It is reported as ``edited_after_apply``
for a person to reconcile.

CONFIG
------
Storage ``config/migration-ledger-config.json``. ``by_site`` overrides
``defaults``, and ``defaults`` overrides DEFAULTS::

    {"schema_version": "1",
     "defaults": {"enabled": false},
     "by_site": {"<site>": {"enabled": true, "quarantine_stranded": true}}}

Shell callers (the implementer) use ``framework/cli/migration_ledger.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

CONFIG_KEY = "config/migration-ledger-config.json"

DEFAULTS: dict = {
    "enabled": False,
    # Relative to the repo root, or absolute.
    "dir": "db/migrations",
    # Run read-only. First column = migration file name. An optional second
    # column = when it was applied: with it, a file modified after that time
    # is reported (edited_after_apply) instead of committed.
    "applied_sql": "SELECT name, applied_at FROM _migrations",
    # Zone of a naive applied_at (timestamp without time zone), and the
    # clock skew allowed between the database and this host.
    "applied_at_tz": "UTC",
    "applied_at_tolerance_s": 120,
    # Only files whose name matches (re.search) are considered; "" = all.
    "name_regex": "",
    "commit_applied": True,
    "min_age_minutes": 30,
    "quarantine_stranded": False,
    "quarantine_after_hours": 24,
    "quarantine_dir": "~/.reusable-agents/migration-quarantine/{site}",
    "commit_message": ("migrations: record {n} migration(s) the database already applied\n\n"
                       "These files were applied to the {site} database (they are in its "
                       "applied-migrations table) but were never committed. Committing them "
                       "records what production already ran; the runner will not re-apply "
                       "them.\n\nFiles:\n{files}\n\nCommitted by framework/core/migration_ledger.py."),
}


def load_config(site: str, *, storage=None, file_cfg: Any = None) -> dict:
    """DEFAULTS <- storage defaults <- storage by_site[site]. `file_cfg`
    skips the storage read (tests)."""
    if file_cfg is None:
        try:
            from framework.core.storage import get_storage
            file_cfg = (storage or get_storage()).read_json(CONFIG_KEY) or {}
        except Exception:
            file_cfg = {}
    if not isinstance(file_cfg, Mapping):
        file_cfg = {}
    out = dict(DEFAULTS)
    for block in (file_cfg.get("defaults"), (file_cfg.get("by_site") or {}).get(site)):
        if isinstance(block, Mapping):
            out.update(block)
    return out


def _git(repo: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          timeout=timeout)


def repo_root(path: str) -> Optional[str]:
    try:
        r = _git(path, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def migrations_dir(repo: str, cfg: Mapping) -> Path:
    d = Path(os.path.expanduser(str(cfg.get("dir") or DEFAULTS["dir"])))
    return d if d.is_absolute() else Path(repo) / d


def untracked_sql(repo: str, mig_dir: Path) -> list[str]:
    """Names of the ``*.sql`` files directly in `mig_dir` that git does not
    track (ignored files included: the runner does not read .gitignore)."""
    try:
        rel = os.path.relpath(str(mig_dir), repo)
    except ValueError:
        return []
    r = _git(repo, "ls-files", "--others", "-z", "--", rel)
    if r.returncode != 0:
        raise RuntimeError("git ls-files failed: %s" % r.stderr.strip()[:200])
    out = []
    prefix = "" if rel in ("", ".") else rel.rstrip("/") + "/"
    for p in r.stdout.split("\0"):
        if p.startswith(prefix) and p.endswith(".sql") and "/" not in p[len(prefix):]:
            out.append(p[len(prefix):])
    return sorted(out)


def tracked_sql(repo: str, mig_dir: Path) -> list[str]:
    """Names of the ``*.sql`` files directly in `mig_dir` that git tracks."""
    try:
        rel = os.path.relpath(str(mig_dir), repo)
    except ValueError:
        return []
    r = _git(repo, "ls-files", "-z", "--", rel)
    if r.returncode != 0:
        raise RuntimeError("git ls-files failed: %s" % r.stderr.strip()[:200])
    prefix = "" if rel in ("", ".") else rel.rstrip("/") + "/"
    return sorted(p[len(prefix):] for p in r.stdout.split("\0")
                  if p.startswith(prefix) and p.endswith(".sql") and "/" not in p[len(prefix):])


def applied_table_mismatch(tracked: Iterable[str], applied: Iterable[str], *,
                           max_missing: int = 5, max_missing_ratio: float = 0.05) -> str:
    """Why `applied` cannot be this repo's database, or "". A deployed site
    has applied (nearly) every tracked migration; an empty table or many
    tracked files missing from it means the DSN points somewhere else, and
    acting on it would misfile applied files as stranded."""
    tracked, applied = set(tracked), set(applied)
    if not applied:
        return "the applied-migrations table is empty"
    missing = tracked - applied
    if len(missing) > max(max_missing, max_missing_ratio * len(tracked)):
        return ("%d of %d tracked migrations are not in the applied table; wrong database?"
                % (len(missing), len(tracked)))
    return ""


def _epoch(ts: Any, tz_name: str = "UTC") -> Optional[float]:
    """POSIX time of an applied_at value (datetime or ISO text); a naive
    value is read in `tz_name`. None when it cannot be read."""
    from datetime import datetime, timezone
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            tz = timezone.utc if tz_name.upper() == "UTC" else ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 -- an unknown zone: refuse to guess
            return None
        ts = ts.replace(tzinfo=tz)
    return ts.timestamp()


def edited_after_apply(names: Iterable[str], applied_at: Mapping[str, Any],
                       mtimes: Mapping[str, float], cfg: Mapping) -> set[str]:
    """Applied files whose content changed after the database applied them
    (file mtime later than applied_at, beyond the configured skew)."""
    tol = float(cfg.get("applied_at_tolerance_s", DEFAULTS["applied_at_tolerance_s"]) or 0)
    tz = str(cfg.get("applied_at_tz") or DEFAULTS["applied_at_tz"])
    out = set()
    for n in names:
        at = _epoch(applied_at.get(n), tz)
        if at is not None and n in mtimes and float(mtimes[n]) > at + tol:
            out.add(n)
    return out


def plan(names: Iterable[str], applied: Iterable[str], ages_s: Mapping[str, float],
         cfg: Mapping, *, edited: Iterable[str] = ()) -> dict:
    """Sort untracked migration files into what to do with them.

    -> {"commit": [...], "applied_uncommitted": [...], "edited_after_apply": [...],
        "quarantine": [...], "stranded": [...], "young": [...], "skipped": [...]}
    commit / quarantine are what reconcile() acts on; applied_uncommitted
    and stranded are the same states left in place because the config says
    so; edited_after_apply are applied files changed since (`edited`), never
    committed as the record of what ran; young files may still belong to a
    running job."""
    applied = set(applied)
    edited = set(edited)
    rx = re.compile(cfg.get("name_regex")) if cfg.get("name_regex") else None
    min_age = float(cfg.get("min_age_minutes") or 0) * 60
    q_after = float(cfg.get("quarantine_after_hours") or 0) * 3600
    out: dict = {k: [] for k in ("commit", "applied_uncommitted", "edited_after_apply",
                                 "quarantine", "stranded", "young", "skipped")}
    for n in sorted(set(names)):
        if rx and not rx.search(n):
            out["skipped"].append(n)
            continue
        age = float(ages_s.get(n, 0.0))
        if n in applied:
            if age < min_age:
                out["young"].append(n)
            elif n in edited:
                out["edited_after_apply"].append(n)
            elif cfg.get("commit_applied", True):
                out["commit"].append(n)
            else:
                out["applied_uncommitted"].append(n)
        elif age < max(min_age, q_after):
            (out["young"] if age < min_age else out["stranded"]).append(n)
        elif cfg.get("quarantine_stranded"):
            out["quarantine"].append(n)
        else:
            out["stranded"].append(n)
    return out


def commit_files(repo: str, mig_dir: Path, names: list[str], message: str) -> str:
    """git add + commit exactly these files (other staged work is left as it
    was: a pathspec commit is --only). -> the new HEAD sha."""
    rel = [os.path.relpath(str(mig_dir / n), repo) for n in names]
    r = _git(repo, "add", "-f", "--", *rel)
    if r.returncode != 0:
        raise RuntimeError("git add failed: %s" % r.stderr.strip()[:300])
    r = _git(repo, "commit", "-q", "-m", message, "--only", "--", *rel, timeout=180)
    if r.returncode != 0:
        _git(repo, "reset", "-q", "--", *rel)
        raise RuntimeError("git commit failed: %s" % (r.stderr or r.stdout).strip()[:300])
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def quarantine_files(mig_dir: Path, names: list[str], dest: Path, *, reason: str) -> list[dict]:
    """Move files out of the migrations directory into `dest`, appending one
    JSON line per file to dest/MANIFEST.jsonl. -> [{name, to, sha1}]."""
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for n in names:
        src = mig_dir / n
        if not src.is_file():
            continue
        target = dest / n
        i = 1
        while target.exists():
            target = dest / ("%s.%d" % (n, i))
            i += 1
        sha1 = hashlib.sha1(src.read_bytes()).hexdigest()
        shutil.move(str(src), str(target))
        entry = {"name": n, "from": str(src), "to": str(target), "sha1": sha1,
                 "moved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "reason": reason}
        with open(dest / "MANIFEST.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        moved.append({"name": n, "to": str(target), "sha1": sha1})
    return moved


def reconcile(repo: str, site: str, applied: Iterable[str], cfg: Mapping, *,
              now: Optional[float] = None, dry_run: bool = False) -> dict:
    """Plan, then act on the plan (unless dry_run). Never raises for a git
    failure: it lands in the summary's "errors". `applied` is a set of names,
    or {name: applied_at} (applied_rows) to also catch files edited after
    they were applied."""
    now = time.time() if now is None else now
    mig = migrations_dir(repo, cfg)
    summary: dict = {"site": site, "dir": str(mig), "dry_run": dry_run, "errors": []}
    if not mig.is_dir():
        summary["errors"].append("no migrations dir %s" % mig)
        return summary
    applied_at = dict(applied) if isinstance(applied, Mapping) else {}
    applied = set(applied)
    why = applied_table_mismatch(tracked_sql(repo, mig), applied)
    if why:
        summary["errors"].append("not reconciling: " + why)
        return summary
    names = untracked_sql(repo, mig)
    ages, mtimes = {}, {}
    for n in names:
        try:
            mtimes[n] = (mig / n).stat().st_mtime
            ages[n] = now - mtimes[n]
        except OSError:
            ages[n] = 0.0
    edited = edited_after_apply([n for n in names if n in applied], applied_at, mtimes, cfg)
    p = plan(names, applied, ages, cfg, edited=edited)
    summary["plan"] = p
    if dry_run:
        return summary
    if p["commit"]:
        msg = str(cfg.get("commit_message") or DEFAULTS["commit_message"]).format(
            n=len(p["commit"]), site=site, files="\n".join("  " + n for n in p["commit"]))
        try:
            summary["commit_sha"] = commit_files(repo, mig, p["commit"], msg)
        except Exception as e:  # noqa: BLE001 -- best effort, reported
            summary["errors"].append(str(e))
    if p["quarantine"]:
        dest = Path(os.path.expanduser(str(cfg.get("quarantine_dir")
                                           or DEFAULTS["quarantine_dir"]).format(site=site)))
        try:
            summary["quarantined"] = quarantine_files(
                mig, p["quarantine"], dest,
                reason="untracked and not applied for more than %sh"
                       % cfg.get("quarantine_after_hours"))
        except Exception as e:  # noqa: BLE001
            summary["errors"].append("quarantine failed: %s" % e)
    return summary


def applied_rows(dsn: str, sql: str, *, timeout_ms: int = 20000) -> dict:
    """{name: applied_at or None} from the applied-migrations table, in a
    read-only session. applied_at is the query's second column, if any."""
    import psycopg2
    conn = psycopg2.connect(dsn, connect_timeout=15)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = %d" % int(timeout_ms))
            cur.execute(sql)
            return {str(r[0]): (r[1] if len(r) > 1 else None) for r in cur.fetchall()}
    finally:
        conn.close()


def applied_names(dsn: str, sql: str, *, timeout_ms: int = 20000, with_times: bool = False):
    """Names from the applied-migrations table, in a read-only session. With
    `with_times`, {name: applied_at or None} instead (applied_rows), which
    reconcile() uses to catch files edited after they were applied."""
    rows = applied_rows(dsn, sql, timeout_ms=timeout_ms)
    return rows if with_times else set(rows)


__all__ = ["CONFIG_KEY", "DEFAULTS", "applied_names", "applied_rows", "commit_files",
           "edited_after_apply", "load_config", "migrations_dir", "plan", "quarantine_files",
           "reconcile", "repo_root", "untracked_sql"]
