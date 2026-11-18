#!/usr/bin/env python3
"""Vendor the affiliate-site kit into a site, and check a vendored copy.

    python3 -I vendor_kit.py vendor --kit KIT --site SITE [--dry-run] [--allow-downgrade] [--quiet] [--json]
    python3 -I vendor_kit.py check  --kit KIT --site SITE
    python3 -I vendor_kit.py verify --site SITE
    python3 -I vendor_kit.py anchor --kit KIT --site SITE [--quiet]
    python3 -I vendor_kit.py files  --kit KIT

The engine of install/sync-site-kit.sh (and of create-affiliate-site.sh, which
calls that script). Self-contained: it runs under `python3 -I`.

What a site gets
----------------
<site>/vendor/affiliate-kit holds the kit's shipped files (see EXCLUDED below:
installed and generated trees, the kit's own tests and gate self-test fixture,
and kit-repo tooling are left out), plus two files this tool writes:

  KIT_VERSION       the kit version, one line (the kit's VERSION)
  MANIFEST.sha256   "<sha256>  <path>" per vendored file, KIT_VERSION
                    included, sorted by path; the format `sha256sum -c` reads

The copy is built next to the old one and swapped in with renames, so a
failure leaves the previous vendored kit in place. Modes are normalised:
0755 for a file the kit marks executable, 0644 otherwise.

verify and anchor
-----------------
`verify` compares the vendored copy with its own MANIFEST.sha256. Both live
in the site, so an edit that also rewrites the manifest passes it. `anchor`
also requires the copy to be byte-identical to the kit of the version it
claims (KIT_VERSION) as the reusable-agents checkout holding KIT has it: the
kit tree of one of the commits (reachable from HEAD) whose VERSION is that
version, or KIT's working tree. Git is only read, without optional locks.

Exit codes: 0 ok (check/verify: in sync / intact; anchor: identical to that
kit version in a commit or the working tree); 1 out of date, tampered or a
failed step (anchor: the copy is not intact, matches no commit of a version
that has commits, or claims a version older than the newest committed one
that no commit holds); 2 usage or configuration error; 4 (anchor only)
nothing to anchor to: no commit holds that version, and it is not older than
the newest that one does, and the working tree is another version or differs.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
# anchor only: nothing in the reusable-agents checkout to compare the copy with.
EXIT_UNANCHORED = 4

VENDOR_REL = PurePosixPath("vendor/affiliate-kit")
KIT_VERSION_FILE = "KIT_VERSION"
MANIFEST_FILE = "MANIFEST.sha256"
WRITTEN_FILES = (KIT_VERSION_FILE, MANIFEST_FILE)
RENDER_MARKER = "# rendered-from: affiliate-kit/deploy/Dockerfile.azure.tmpl"

# Directory names never vendored, at any depth (installed or generated trees).
EXCLUDED_DIR_NAMES = frozenset({
    "node_modules", "dist", "coverage", "test-results", "playwright-report", "blob-report",
    "__pycache__", ".git", ".cache",
})
# Paths (relative to the kit root) never vendored: the kit's own unit tests,
# the gate's self-test fixture site and self-test, and kit-repo tooling that
# only works inside reusable-agents.
EXCLUDED_PATHS = frozenset({
    "unit", "fixture-site", "tests/unit", "tests/selftest", "tests/jest.config.js",
    ".gitignore", "jest.config.js", "tsconfig.jest.json", "test.sh", "package-lock.json",
})
# File names never vendored, at any depth (scratch and editor files).
EXCLUDED_FILE_GLOBS = ("*.tsbuildinfo", ".drift-report*.json", "*.log", "*.pyc", "*.swp", "*~", ".DS_Store")
# Files a kit must hold for a sync to make sense.
REQUIRED_KIT_FILES = (
    "VERSION", "package.json", "server/src/index.ts", "schema/site.schema.json",
    "migrations/kit_0001_outbound_clicks.sql", "tests/run-local-suite.sh", "deploy/Dockerfile.azure.tmpl",
    "scripts/build-client.mjs", "tsconfig.json",
)
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
MANIFEST_LINE_RE = re.compile(r"^([0-9a-f]{64})  (\S(?:.*\S)?)$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._@+=,-]+$")


class SyncError(Exception):
    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Report:
    quiet: bool = False
    warnings: List[str] = field(default_factory=list)

    def info(self, msg: str) -> None:
        if not self.quiet:
            # Flushed, so it stays in order with the warnings when a caller merges the two streams.
            print(f"[sync-site-kit] {msg}", flush=True)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"[sync-site-kit] WARNING: {msg}", file=sys.stderr)


# ─── the kit ──────────────────────────────────────────────────────────────────

def semver(v: str) -> Tuple[int, int, int, Tuple[Tuple[int, object], ...]]:
    m = SEMVER_RE.match(v)
    if not m:
        raise SyncError(f"version {v!r} is not MAJOR.MINOR.PATCH", EXIT_USAGE)
    pre: Tuple[Tuple[int, object], ...] = ()
    if m.group(4):
        pre = tuple((0, int(p)) if p.isdigit() else (1, p) for p in m.group(4).split("."))
    # A release sorts after its pre-releases.
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre if pre else ((2, ""),)


def kit_version(kit: Path) -> str:
    for rel in REQUIRED_KIT_FILES:
        if not (kit / rel).is_file():
            raise SyncError(f"{kit} is not an affiliate-site kit: {rel} is missing", EXIT_USAGE)
    version = (kit / "VERSION").read_text(encoding="utf-8").strip()
    semver(version)
    pkg = json.loads((kit / "package.json").read_text(encoding="utf-8"))
    if pkg.get("version") != version:
        raise SyncError(f"the kit is half-bumped: VERSION {version} but package.json {pkg.get('version')}", EXIT_USAGE)
    m = re.search(r"export const KIT_VERSION = '([^']+)'", (kit / "server/src/index.ts").read_text(encoding="utf-8"))
    if not m or m.group(1) != version:
        raise SyncError(
            f"the kit is half-bumped: VERSION {version} but server/src/index.ts KIT_VERSION {m.group(1) if m else 'missing'}",
            EXIT_USAGE,
        )
    return version


def excluded(rel: PurePosixPath, is_dir: bool) -> bool:
    if is_dir and rel.name in EXCLUDED_DIR_NAMES:
        return True
    if str(rel) in EXCLUDED_PATHS:
        return True
    if not is_dir and any(fnmatch.fnmatchcase(rel.name, g) for g in EXCLUDED_FILE_GLOBS):
        return True
    return False


def kit_files(kit: Path) -> List[PurePosixPath]:
    """The kit files a site vendors, sorted by path."""
    out: List[PurePosixPath] = []
    for dirpath, dirnames, filenames in os.walk(kit, followlinks=False):
        here = PurePosixPath(os.path.relpath(dirpath, kit).replace(os.sep, "/"))
        keep: List[str] = []
        for d in sorted(dirnames):
            rel = here / d if str(here) != "." else PurePosixPath(d)
            full = os.path.join(dirpath, d)
            if excluded(rel, True):
                continue
            if os.path.islink(full):
                raise SyncError(f"the kit holds a symbolic link, which is never vendored: {rel}")
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = here / f if str(here) != "." else PurePosixPath(f)
            if excluded(rel, False):
                continue
            st = os.lstat(os.path.join(dirpath, f))
            if stat.S_ISLNK(st.st_mode):
                raise SyncError(f"the kit holds a symbolic link, which is never vendored: {rel}")
            if not stat.S_ISREG(st.st_mode):
                raise SyncError(f"not a regular file: {rel}")
            if not all(SAFE_NAME_RE.match(p) for p in rel.parts):
                raise SyncError(f"kit file name {str(rel)!r} has characters a manifest line cannot carry")
            if rel.name in WRITTEN_FILES and len(rel.parts) == 1:
                raise SyncError(f"the kit itself holds {rel}, which only a sync writes")
            out.append(rel)
    return sorted(out, key=lambda p: str(p).encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def manifest_text(entries: Dict[str, str]) -> str:
    return "".join(f"{entries[p]}  {p}\n" for p in sorted(entries, key=lambda s: s.encode("utf-8")))


def expected_manifest(kit: Path, version: str) -> Dict[str, str]:
    entries = {str(rel): sha256_file(kit / rel) for rel in kit_files(kit)}
    entries[KIT_VERSION_FILE] = hashlib.sha256(f"{version}\n".encode("utf-8")).hexdigest()
    return entries


def git_env() -> Dict[str, str]:
    """The environment of a read-only git query.

    None of the caller's GIT_* variables (GIT_DIR or GIT_WORK_TREE would point
    the query at another repository), no prompts, and no optional locks: a
    plain `git status` may take .git/index.lock to refresh its stat cache,
    which would fail a commit another session makes in the same checkout at
    that moment (the live reusable-agents checkout is shared).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    return env


def git(cwd: Path, *args: str, stdin: Optional[bytes] = None, timeout: int = 120) -> "subprocess.CompletedProcess[bytes]":
    """Run a read-only git command in `cwd` (bytes in, bytes out; never raises on a non-zero exit)."""
    return subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args], input=stdin,
                          capture_output=True, timeout=timeout, env=git_env())


def git_provenance(kit: Path) -> Tuple[Optional[str], Optional[int]]:
    """(HEAD commit, number of uncommitted paths under the kit), or Nones outside git. Read-only and lock-free."""
    try:
        head = git(kit, "rev-parse", "HEAD", timeout=20)
        if head.returncode != 0:
            return None, None
        status = git(kit, "status", "--porcelain", "--untracked-files=all", "--", ".", timeout=60)
        lines = status.stdout.decode("utf-8", "replace").splitlines()
        dirty = len([line for line in lines if line.strip()]) if status.returncode == 0 else None
        return head.stdout.decode("utf-8", "replace").strip(), dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


# ─── the kit's history (anchor) ──────────────────────────────────────────────

class AnchorUnavailable(Exception):
    """The kit's git history cannot be read (no git, not a work tree, an unreadable repository)."""


def tree_path_vendored(rel: PurePosixPath) -> bool:
    """Whether kit_files() vendors the file at `rel` (relative to the kit): neither a
    directory above it nor the file itself is excluded."""
    parts = rel.parts
    for i in range(1, len(parts)):
        if excluded(PurePosixPath(*parts[:i]), True):
            return False
    return not excluded(rel, False)


def version_digest(version: str) -> str:
    """The MANIFEST.sha256 digest of the KIT_VERSION file a sync writes for `version`."""
    return hashlib.sha256(f"{version}\n".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KitTree:
    """One committed state of the kit: its tree object, the newest commit listed with it, and its VERSION."""
    tree: str
    commit: str
    committed_at: int
    version: Optional[str]


def _first_line(data: bytes) -> str:
    text = data.decode("utf-8", "replace").strip()
    return text.splitlines()[0] if text else "no output"


class GitKit:
    """Read-only access to every committed state of the kit in the repository that holds it."""

    BATCH = 400  # objects per `git cat-file --batch` call

    def __init__(self, kit: Path) -> None:
        try:
            r = git(kit, "rev-parse", "--show-toplevel", "--show-prefix", timeout=20)
        except FileNotFoundError:
            raise AnchorUnavailable("git is not installed") from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise AnchorUnavailable(f"git failed: {exc}") from None
        if r.returncode != 0:
            raise AnchorUnavailable(f"{kit} is not in a git work tree ({_first_line(r.stderr)})")
        lines = r.stdout.decode("utf-8", "surrogateescape").split("\n")
        self.top = Path(lines[0])
        self.prefix = lines[1].strip().rstrip("/") if len(lines) > 1 else ""
        self._digests: Dict[str, str] = {}

    @property
    def label(self) -> str:
        return self.prefix or "the kit"

    def _git(self, *args: str, stdin: Optional[bytes] = None, timeout: int = 120) -> bytes:
        try:
            r = git(self.top, *args, stdin=stdin, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AnchorUnavailable(f"git {args[0]} failed: {exc}") from None
        if r.returncode != 0:
            raise AnchorUnavailable(f"git {args[0]} failed in {self.top}: {_first_line(r.stderr)}")
        return r.stdout

    def _batch(self, names: Sequence[str]) -> List[Optional[Tuple[str, bytes]]]:
        """`git cat-file --batch` for each name: (type, content), or None when it does not exist."""
        out: List[Optional[Tuple[str, bytes]]] = []
        for i in range(0, len(names), self.BATCH):
            chunk = names[i:i + self.BATCH]
            data = self._git("cat-file", "--batch", stdin="".join(f"{n}\n" for n in chunk).encode("utf-8"))
            pos = 0
            for _name in chunk:
                nl = data.index(b"\n", pos)
                header = data[pos:nl].decode("utf-8", "replace").split(" ")
                pos = nl + 1
                if len(header) != 3 or not header[2].isdigit():
                    out.append(None)  # "<name> missing" / "ambiguous"
                    continue
                size = int(header[2])
                out.append((header[1], data[pos:pos + size]))
                pos += size + 1  # the content, then a newline
        return out

    def trees(self) -> List[KitTree]:
        """Every distinct kit tree in the history reachable from HEAD, newest first, with its VERSION.

        A repository without commits has none. --full-history keeps the commits
        that a merge's history simplification would hide.
        """
        try:
            head = git(self.top, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AnchorUnavailable(f"git rev-parse failed: {exc}") from None
        if head.returncode != 0:
            return []
        log = self._git("log", "--full-history", "--format=%H %ct", "HEAD", "--", self.prefix or ".")
        commits: List[Tuple[str, int]] = []
        for line in log.decode("utf-8", "replace").splitlines():
            sha, _, ts = line.partition(" ")
            if sha:
                commits.append((sha, int(ts) if ts.isdigit() else 0))
        if not commits:
            return []
        check = self._git("cat-file", "--batch-check",
                          stdin="".join(f"{sha}:{self.prefix}\n" for sha, _ in commits).encode("utf-8"))
        seen: Dict[str, Tuple[str, int]] = {}
        for (sha, ts), line in zip(commits, check.decode("utf-8", "replace").splitlines()):
            fields = line.split(" ")
            if len(fields) == 3 and fields[1] == "tree" and fields[0] not in seen:
                seen[fields[0]] = (sha, ts)
        trees = list(seen)
        versions = self._batch([f"{t}:VERSION" for t in trees])
        out: List[KitTree] = []
        for t, v in zip(trees, versions):
            version = v[1].decode("utf-8", "replace").strip() if v is not None and v[0] == "blob" else None
            out.append(KitTree(tree=t, commit=seen[t][0], committed_at=seen[t][1], version=version or None))
        return out

    def manifest(self, t: KitTree) -> Optional[Dict[str, str]]:
        """The MANIFEST.sha256 a sync of this tree writes, or None when kit_files() would refuse
        the tree (a symbolic link or an unsafe name among the vendored files)."""
        if not t.version:
            return None
        files: Dict[str, str] = {}
        for entry in self._git("ls-tree", "-r", "-z", t.tree).split(b"\0"):
            if not entry:
                continue
            meta, _, raw = entry.partition(b"\t")
            fields = meta.decode("ascii", "replace").split(" ")
            if len(fields) != 3:
                return None
            mode, kind, oid = fields
            rel = PurePosixPath(raw.decode("utf-8", "surrogateescape"))
            if not tree_path_vendored(rel) or kind == "commit":
                continue  # a submodule is an empty directory in a checkout: nothing to vendor
            if kind != "blob" or mode == "120000":
                return None
            if not all(SAFE_NAME_RE.match(p) for p in rel.parts):
                return None
            if len(rel.parts) == 1 and rel.name in WRITTEN_FILES:
                return None
            files[str(rel)] = oid
        need = sorted({oid for oid in files.values() if oid not in self._digests})
        for oid, obj in zip(need, self._batch(need)):
            if obj is None:
                return None
            self._digests[oid] = hashlib.sha256(obj[1]).hexdigest()
        entries = {rel: self._digests[oid] for rel, oid in files.items()}
        entries[KIT_VERSION_FILE] = version_digest(t.version)
        return entries


# ─── the site ─────────────────────────────────────────────────────────────────

def site_dir(path: str) -> Path:
    site = Path(path).absolute()
    if not site.is_dir():
        raise SyncError(f"site directory not found: {site}", EXIT_USAGE)
    if not (site / "site.json").is_file():
        raise SyncError(f"{site} has no site.json: not an affiliate site (create one with install/create-affiliate-site.sh)",
                        EXIT_USAGE)
    vendor = site / "vendor"
    if vendor.is_symlink() or (site / VENDOR_REL).is_symlink():
        raise SyncError(f"{site / VENDOR_REL} (or vendor/) is a symbolic link; vendor the kit as real files", EXIT_USAGE)
    return site


def read_manifest(vendor: Path) -> Dict[str, str]:
    path = vendor / MANIFEST_FILE
    entries: Dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = MANIFEST_LINE_RE.match(line)
        if not m:
            raise SyncError(f"{path}:{n}: not a '<sha256>  <path>' line")
        rel = m.group(2)
        if rel in entries or rel.startswith("/") or ".." in rel.split("/") or rel == MANIFEST_FILE:
            raise SyncError(f"{path}:{n}: bad or repeated path {rel!r}")
        entries[rel] = m.group(1)
    return entries


def entry_kind(mode: int) -> str:
    return "symbolic link" if stat.S_ISLNK(mode) else "not a regular file"


def vendored_entries(vendor: Path) -> Dict[str, int]:
    """Every entry under the vendored copy but its directories and MANIFEST.sha256, by
    relative path, with its lstat mode. Symbolic links (to files or directories) are
    listed, never followed: a vendored copy holds regular files only, so anything else
    is tampering, even a link to an identical file (the image would build from wherever
    it points)."""
    out: Dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(vendor, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            if stat.S_ISDIR(mode):
                continue
            rel = os.path.relpath(full, vendor).replace(os.sep, "/")
            if rel != MANIFEST_FILE:
                out[rel] = mode
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
    return out


@dataclass
class Integrity:
    """How a vendored copy compares with its own MANIFEST.sha256."""
    present: bool
    version: Optional[str] = None
    manifest: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    extra: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def intact(self) -> bool:
        return self.present and not (self.missing or self.changed or self.extra or self.error)


def inspect_vendor(site: Path) -> Integrity:
    vendor = site / VENDOR_REL
    if not vendor.is_dir():
        return Integrity(present=False)
    res = Integrity(present=True)
    vf = vendor / KIT_VERSION_FILE
    if vf.is_file():
        res.version = vf.read_text(encoding="utf-8").strip() or None
    manifest_path = vendor / MANIFEST_FILE
    if not manifest_path.is_file() and not manifest_path.is_symlink():
        res.error = f"{MANIFEST_FILE} is missing"
        return res
    if not stat.S_ISREG(os.lstat(manifest_path).st_mode):
        res.error = f"{MANIFEST_FILE} is a {entry_kind(os.lstat(manifest_path).st_mode)}"
        return res
    try:
        res.manifest = read_manifest(vendor)
    except SyncError as exc:
        res.error = str(exc)
        return res
    entries = vendored_entries(vendor)
    for rel, digest in sorted(res.manifest.items()):
        mode = entries.get(rel)
        if mode is None:
            res.missing.append(rel)
        elif not stat.S_ISREG(mode):
            res.changed.append(f"{rel} ({entry_kind(mode)})")
        elif sha256_file(vendor / rel) != digest:
            res.changed.append(rel)
    res.extra = sorted(
        (rel if stat.S_ISREG(mode) else f"{rel} ({entry_kind(mode)})"
         for rel, mode in entries.items() if rel not in res.manifest),
        key=lambda s: s.encode("utf-8"),
    )
    return res


def describe_integrity(i: Integrity, limit: int = 20) -> List[str]:
    lines: List[str] = []
    if i.error:
        lines.append(i.error)
    for label, items in (("modified", i.changed), ("missing", i.missing), ("not in the manifest", i.extra)):
        for rel in items[:limit]:
            lines.append(f"{label}: {rel}")
        if len(items) > limit:
            lines.append(f"... and {len(items) - limit} more {label}")
    return lines


def diff_manifests(old: Dict[str, str], new: Dict[str, str]) -> Tuple[List[str], List[str], List[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    return added, removed, changed


def dependency_drift(kit: Path, site: Path) -> List[str]:
    """Kit dependency pins the site's package.json does not carry exactly."""
    pkg_path = site / "package.json"
    if not pkg_path.is_file():
        return ["the site has no package.json (it needs the kit's dependencies, pinned exactly)"]
    kit_pkg = json.loads((kit / "package.json").read_text(encoding="utf-8"))
    site_pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    site_deps = site_pkg.get("dependencies") or {}
    site_dev = site_pkg.get("devDependencies") or {}
    out: List[str] = []
    for name, want in sorted((kit_pkg.get("dependencies") or {}).items()):
        got = site_deps.get(name)
        if got != want:
            where = " (it is a devDependency there; the server needs it at runtime)" if name in site_dev else ""
            out.append(f"dependencies.{name}: kit pins {want}, the site has {got or 'nothing'}{where}")
    for name, want in sorted((kit_pkg.get("devDependencies") or {}).items()):
        got = site_dev.get(name, site_deps.get(name))
        if got != want:
            out.append(f"devDependencies.{name}: kit pins {want}, the site has {got or 'nothing'}")
    return out


def dockerfile_drift(site: Path, version: str) -> Optional[str]:
    df = site / "Dockerfile.azure"
    if not df.is_file():
        return None
    first = df.read_text(encoding="utf-8").split("\n", 1)[0]
    if not first.startswith(RENDER_MARKER):
        return None
    want = f"{RENDER_MARKER} (kit {version})"
    if first.strip() != want:
        m = re.match(r"^\(kit ([^)]*)\)$", first[len(RENDER_MARKER):].strip())
        rendered = f"kit {m.group(1)}" if m else "an unknown kit"
        return (f"Dockerfile.azure was rendered from another kit version ({rendered}, vendored kit {version}); "
                f"re-render it: bash vendor/affiliate-kit/deploy/render-dockerfile.sh --env deploy.env --write")
    return None


# ─── commands ─────────────────────────────────────────────────────────────────

def copy_tree(kit: Path, files: Iterable[PurePosixPath], dest: Path) -> Dict[str, str]:
    entries: Dict[str, str] = {}
    for rel in files:
        src = kit / rel
        out = dest.joinpath(*rel.parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
        mode = 0o755 if os.stat(src).st_mode & 0o111 else 0o644
        os.chmod(out, mode)
        entries[str(rel)] = sha256_file(out)
    for dirpath, _dirs, _files in os.walk(dest):
        os.chmod(dirpath, 0o755)
    return entries


def cmd_vendor(args: argparse.Namespace, rep: Report) -> int:
    kit = Path(args.kit).resolve()
    site = site_dir(args.site)
    if site == kit or kit in site.parents:
        raise SyncError("the site may not live inside the kit", EXIT_USAGE)
    version = kit_version(kit)
    current = inspect_vendor(site)
    if current.present and current.version and current.version != version:
        try:
            older = semver(version) < semver(current.version)
        except SyncError:
            rep.warn(f"the vendored KIT_VERSION {current.version!r} is not a version; replacing the copy")
            older = False
        if older and not args.allow_downgrade:
            raise SyncError(f"the site has kit {current.version} and this kit is {version}: a downgrade "
                            f"(pass --allow-downgrade if that is intended)", EXIT_USAGE)
    commit, dirty = git_provenance(kit)
    if dirty:
        rep.warn(f"the kit tree has {dirty} uncommitted path(s): the vendored copy matches no commit")
    files = kit_files(kit)
    new_manifest = expected_manifest(kit, version)
    old_manifest = current.manifest if current.present and not current.error else {}
    if current.present and not current.intact:
        rep.warn("vendor/affiliate-kit did not match its own MANIFEST.sha256 (local edits are discarded):")
        for line in describe_integrity(current):
            rep.warn(f"  {line}")
    added, removed, changed = diff_manifests(old_manifest, new_manifest)
    if current.present and current.version == version and (added or removed or changed) and old_manifest:
        rep.warn(f"kit {version} differs from the vendored {version} without a version bump "
                 f"({len(added)} added, {len(removed)} removed, {len(changed)} changed)")

    summary = {
        "site": str(site), "kit": str(kit), "version": version, "previous": current.version,
        "kit_commit": commit, "kit_uncommitted_paths": dirty, "files": len(new_manifest),
        "added": added, "removed": removed, "changed": changed, "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        rep.info(f"dry run: would vendor kit {version} ({len(new_manifest)} files) into {site / VENDOR_REL}")
        for label, items in (("add", added), ("remove", removed), ("update", changed)):
            for rel in items:
                rep.info(f"  would {label} {rel}")
        if args.json:
            print(json.dumps(summary))
        return EXIT_OK

    vendor_parent = site / "vendor"
    vendor_parent.mkdir(exist_ok=True)
    os.chmod(vendor_parent, 0o755)
    stage = Path(tempfile.mkdtemp(prefix=".affiliate-kit.new.", dir=vendor_parent))
    old_aside: Optional[Path] = None
    target = site / VENDOR_REL
    try:
        entries = copy_tree(kit, files, stage)
        (stage / KIT_VERSION_FILE).write_text(f"{version}\n", encoding="utf-8")
        os.chmod(stage / KIT_VERSION_FILE, 0o644)
        entries[KIT_VERSION_FILE] = sha256_file(stage / KIT_VERSION_FILE)
        if entries != new_manifest:
            raise SyncError("the kit changed while it was being copied; run the sync again")
        (stage / MANIFEST_FILE).write_text(manifest_text(entries), encoding="utf-8")
        os.chmod(stage / MANIFEST_FILE, 0o644)
        if target.exists():
            old_aside = Path(tempfile.mkdtemp(prefix=".affiliate-kit.old.", dir=vendor_parent))
            os.rmdir(old_aside)
            os.rename(target, old_aside)
        os.rename(stage, target)
    except BaseException:
        if old_aside is not None and old_aside.exists() and not target.exists():
            os.rename(old_aside, target)
            old_aside = None
        shutil.rmtree(stage, ignore_errors=True)
        raise
    if old_aside is not None:
        shutil.rmtree(old_aside, ignore_errors=True)

    after = inspect_vendor(site)
    if not after.intact:
        raise SyncError("the new vendored copy does not verify against its manifest: " + "; ".join(describe_integrity(after)))
    if current.version and current.version != version:
        rep.info(f"vendored kit {version} (was {current.version}) into {target}")
    else:
        rep.info(f"vendored kit {version} into {target}")
    rep.info(f"{len(entries)} files; {len(added)} added, {len(removed)} removed, {len(changed)} changed"
             + (f"; kit commit {commit[:12]}" if commit else ""))
    for line in dependency_drift(kit, site):
        rep.warn(f"package.json: {line}")
    df = dockerfile_drift(site, version)
    if df:
        rep.warn(df)
    if args.json:
        print(json.dumps(summary))
    return EXIT_OK


def cmd_check(args: argparse.Namespace, rep: Report) -> int:
    kit = Path(args.kit).resolve()
    site = site_dir(args.site)
    version = kit_version(kit)
    current = inspect_vendor(site)
    problems: List[str] = []
    if not current.present:
        problems.append(f"{VENDOR_REL} is missing: run install/sync-site-kit.sh {site}")
    else:
        if not current.intact:
            problems.append("vendor/affiliate-kit does not match its MANIFEST.sha256 (it was edited; vendor is read-only):")
            problems.extend(f"  {line}" for line in describe_integrity(current))
        want = expected_manifest(kit, version)
        added, removed, changed = diff_manifests(current.manifest, want)
        if current.version != version or added or removed or changed:
            problems.append(f"out of date: vendored {current.version or 'unknown'}, kit {version} "
                            f"({len(added)} to add, {len(removed)} to remove, {len(changed)} to update)")
        problems.extend(f"package.json: {line}" for line in dependency_drift(kit, site))
        df = dockerfile_drift(site, version)
        if df:
            problems.append(df)
    for p in problems:
        print(f"[sync-site-kit] {p}", file=sys.stderr)
    if problems:
        return EXIT_FAIL
    rep.info(f"{site / VENDOR_REL}: kit {version}, manifest intact, in sync with {kit}")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace, rep: Report) -> int:
    site = site_dir(args.site)
    current = inspect_vendor(site)
    if not current.present:
        print(f"[sync-site-kit] {site / VENDOR_REL} is missing", file=sys.stderr)
        return EXIT_FAIL
    if not current.intact:
        print("[sync-site-kit] vendor/affiliate-kit does not match its MANIFEST.sha256:", file=sys.stderr)
        for line in describe_integrity(current):
            print(f"[sync-site-kit]   {line}", file=sys.stderr)
        return EXIT_FAIL
    rep.info(f"{site / VENDOR_REL}: kit {current.version}, {len(current.manifest)} files, manifest intact")
    return EXIT_OK


@dataclass
class Mismatch:
    """How a candidate copy of the kit differs from the vendored one."""
    where: str
    added: List[str]     # vendored, not in the candidate
    removed: List[str]   # in the candidate, not vendored
    changed: List[str]

    @property
    def size(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)

    def lines(self, limit: int = 20) -> List[str]:
        out: List[str] = []
        for label, items in (("modified", self.changed), ("missing", self.removed), ("not in the kit", self.added)):
            out.extend(f"{label}: {rel}" for rel in items[:limit])
            if len(items) > limit:
                out.append(f"... and {len(items) - limit} more {label}")
        return out


def _day(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "unknown date"


def cmd_anchor(args: argparse.Namespace, rep: Report) -> int:
    """Is the vendored copy byte-identical to the kit version it claims, as KIT's checkout has it?

    Fails closed: anything wrong with the site's copy is EXIT_FAIL, never a
    usage error, so a caller that tolerates "cannot anchor" cannot be fed a
    tampered copy as one.
    """
    kit = Path(args.kit).resolve()
    try:
        site = site_dir(args.site)
    except SyncError as exc:
        print(f"[sync-site-kit] {exc}", file=sys.stderr)
        return EXIT_FAIL
    real_site = site.resolve()
    if kit == real_site or real_site in kit.parents or kit in real_site.parents:
        print(f"[sync-site-kit] cannot anchor {site} to {kit}: the kit may not be inside the site, nor the site "
              "inside the kit (anchor to the kit in the reusable-agents checkout)", file=sys.stderr)
        return EXIT_FAIL
    vendor = site / VENDOR_REL
    current = inspect_vendor(site)
    if not current.present:
        print(f"[sync-site-kit] {vendor} is missing", file=sys.stderr)
        return EXIT_FAIL
    if not current.intact:
        print("[sync-site-kit] vendor/affiliate-kit does not match its MANIFEST.sha256:", file=sys.stderr)
        for line in describe_integrity(current):
            print(f"[sync-site-kit]   {line}", file=sys.stderr)
        return EXIT_FAIL
    version = current.version or ""
    if not SEMVER_RE.match(version):
        print(f"[sync-site-kit] vendor/affiliate-kit/KIT_VERSION {version!r} is not a kit version", file=sys.stderr)
        return EXIT_FAIL
    want = current.manifest
    closest: Optional[Mismatch] = None

    def consider(where: str, candidate: Dict[str, str]) -> bool:
        nonlocal closest
        if candidate == want:
            return True
        added, removed, changed = diff_manifests(candidate, want)
        m = Mismatch(where, added, removed, changed)
        if closest is None or m.size < closest.size:
            closest = m
        return False

    # Every commit of that version: the record a site cannot rewrite. A history
    # that cannot be read in full is no evidence either way.
    history: List[KitTree] = []
    candidates: List[KitTree] = []
    history_note: Optional[str] = None
    gk: Optional[GitKit] = None
    try:
        gk = GitKit(kit)
        history = gk.trees()
        candidates = [t for t in history if t.version == version]
        manifests = [(t, gk.manifest(t)) for t in candidates]
    except AnchorUnavailable as exc:
        history_note = str(exc)
        history, candidates, manifests = [], [], []
    for t, m in manifests:
        if m is not None and consider(f"commit {t.commit[:12]} ({_day(t.committed_at)})", m):
            assert gk is not None
            rep.info(f"{vendor}: kit {version}, {len(want)} files, identical to {gk.label} at commit "
                     f"{t.commit[:12]} ({_day(t.committed_at)}) in {gk.top}")
            return EXIT_OK

    # The checkout's working tree: outside the site too, but no release.
    wt_version: Optional[str] = None
    wt_note: Optional[str] = None
    if kit.is_dir():
        try:
            wt_version = kit_version(kit)
            if wt_version == version and consider("the working tree", expected_manifest(kit, version)):
                rep.info(f"{vendor}: kit {version}, {len(want)} files, identical to the kit working tree at {kit}")
                rep.warn(f"kit {version} as vendored is in no commit of {gk.top if gk else kit}: commit the kit, "
                         "or this check cannot tell it from an edit once the working tree moves on")
                return EXIT_OK
        except (SyncError, OSError, ValueError) as exc:
            wt_note = str(exc)
    else:
        wt_note = f"{kit} does not exist"

    where = gk.top if gk is not None else kit
    committed = sorted({t.version for t in history if t.version and SEMVER_RE.match(t.version)}, key=semver)
    if version in committed:
        print(f"[sync-site-kit] vendor/affiliate-kit says it is kit {version}, but it matches no copy of kit {version} in "
              f"{where}: none of its {len(candidates)} committed state(s)"
              + (", nor the working tree" if wt_version == version else "") + ".", file=sys.stderr)
        if closest is not None:
            print(f"[sync-site-kit] closest: {closest.where}:", file=sys.stderr)
            for line in closest.lines():
                print(f"[sync-site-kit]   {line}", file=sys.stderr)
        print("[sync-site-kit] It was edited in the site (its MANIFEST.sha256 too), or vendored from a kit tree that was "
              "never committed. vendor/affiliate-kit is read-only: re-sync it from the committed kit "
              f"(install/sync-site-kit.sh {site}).", file=sys.stderr)
        return EXIT_FAIL
    if committed and semver(version) < semver(committed[-1]):
        print(f"[sync-site-kit] vendor/affiliate-kit says it is kit {version}, which no commit in {where} holds, though "
              f"newer versions are committed there ({', '.join(committed)}). A kit version is only ever vendored from "
              f"reusable-agents: re-sync it (install/sync-site-kit.sh {site}).", file=sys.stderr)
        return EXIT_FAIL

    if history_note:
        reason = history_note
    elif committed:
        reason = f"no commit there holds kit {version} yet (the newest committed kit is {committed[-1]})"
    else:
        reason = f"no commit there holds the kit at {gk.label if gk else kit} yet"
    if wt_note:
        reason += f"; its working tree is not usable ({wt_note})"
    elif wt_version != version:
        reason += f"; its working tree is kit {wt_version}"
    elif closest is not None:
        reason += f"; its working tree is kit {version} but differs ({closest.size} file(s): "
        reason += "; ".join(closest.lines(limit=3)) + ")"
    rep.warn(f"cannot anchor {vendor} (kit {version}) to {where}: {reason}. It was checked only against its own "
             "MANIFEST.sha256, which an edit in the site can rewrite.")
    return EXIT_UNANCHORED


def cmd_files(args: argparse.Namespace, rep: Report) -> int:
    kit = Path(args.kit).resolve()
    kit_version(kit)
    for rel in kit_files(kit):
        print(rel)
    return EXIT_OK


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="vendor_kit.py", description="Vendor or check the affiliate-site kit in a site.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("vendor", help="copy the kit into <site>/vendor/affiliate-kit")
    p.add_argument("--kit", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-downgrade", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true", help="print a JSON summary on stdout")
    p = sub.add_parser("check", help="is the vendored copy intact and identical to the kit?")
    p.add_argument("--kit", required=True)
    p.add_argument("--site", required=True)
    p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("verify", help="is the vendored copy intact (MANIFEST.sha256)?")
    p.add_argument("--site", required=True)
    p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("anchor", help="is the vendored copy byte-identical to that kit version in KIT's checkout "
                                      "(a commit, or the working tree)?")
    p.add_argument("--kit", required=True, help="the kit in the reusable-agents checkout to anchor to")
    p.add_argument("--site", required=True)
    p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("files", help="list the kit files a site vendors")
    p.add_argument("--kit", required=True)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    rep = Report(quiet=bool(getattr(args, "quiet", False)))
    handlers = {"vendor": cmd_vendor, "check": cmd_check, "verify": cmd_verify, "anchor": cmd_anchor, "files": cmd_files}
    try:
        return handlers[args.cmd](args, rep)
    except SyncError as exc:
        print(f"[sync-site-kit] error: {exc}", file=sys.stderr)
        # anchor fails closed: its callers tolerate "cannot anchor", never a usage error in disguise.
        return EXIT_FAIL if args.cmd == "anchor" else exc.code
    except (OSError, ValueError) as exc:
        print(f"[sync-site-kit] error: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
