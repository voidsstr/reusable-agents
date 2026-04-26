"""Release tagging for production-affecting commits.

When an agent ships code changes:

    self.commit_and_release(
        files=["src/server.ts"],
        message="SEO agent: shipped /best/best-tacos for top-5 query",
    )

The framework runs:
  1. git add <files>
  2. git commit -m "<message>"
  3. git tag agent/<agent-id>/release/<run-ts>
  4. git push --tags origin <branch>

…and appends a `kind="release"` entry to agents/<id>/changelog.jsonl
so the dashboard shows a release timeline.

Hard rules:
  - Never `--no-verify` (hooks must pass)
  - Tag format is the source of truth: `agent/<id>/release/<UTC-ts>`
  - Tag pushes are idempotent — safe to retry
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from .decision_log import append_changelog
from .storage import StorageBackend


logger = logging.getLogger("framework.release")


class ReleaseError(Exception):
    pass


def _git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120,
    )
    if check and proc.returncode != 0:
        raise ReleaseError(
            f"git {' '.join(shlex.quote(a) for a in args)} failed (rc={proc.returncode}):\n"
            f"  stdout: {proc.stdout[-500:]}\n"
            f"  stderr: {proc.stderr[-500:]}"
        )
    return proc


def release_tag(agent_id: str, run_ts: str) -> str:
    """Canonical release tag for an agent + run."""
    return f"agent/{agent_id}/release/{run_ts}"


def commit_and_release(
    *,
    agent_id: str,
    run_ts: str,
    repo_dir: str,
    files: list[str],
    message: str,
    push: bool = True,
    branch: str = "",
    storage: Optional[StorageBackend] = None,
) -> dict:
    """Stage, commit, tag, and (optionally) push.

    Returns: {commit_sha, release_id, tag, pushed}
    Raises: ReleaseError on any git failure.
    """
    if not files:
        raise ValueError("files cannot be empty")
    repo = str(Path(repo_dir).resolve())

    # Sanity — must be a git repo
    _git(["rev-parse", "--show-toplevel"], cwd=repo)

    # Stage
    _git(["add", "--", *files], cwd=repo)

    # Anything to commit?
    proc = _git(["diff", "--cached", "--quiet"], cwd=repo, check=False)
    if proc.returncode == 0:
        logger.info(f"[release] nothing staged for {agent_id} — skipping commit")
        # Return a dict that indicates no-op; caller can decide what to do
        return {"commit_sha": "", "release_id": "", "tag": "", "pushed": False, "no_op": True}

    # Commit
    full_message = f"{message}\n\nAgent: {agent_id}\nRun: {run_ts}"
    _git(["commit", "-m", full_message], cwd=repo)

    # Get the commit sha
    sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

    # Tag
    tag = release_tag(agent_id, run_ts)
    _git(["tag", "-f", tag, "-m", message], cwd=repo)

    pushed = False
    if push:
        try:
            push_args = ["push", "origin"]
            if branch:
                push_args.append(branch)
            _git(push_args, cwd=repo)
            _git(["push", "origin", tag], cwd=repo)
            pushed = True
        except ReleaseError as e:
            logger.warning(f"[release] push failed: {e}")

    # Changelog entry
    if storage is not None:
        append_changelog(
            agent_id,
            kind="release",
            message=message,
            release_id=tag,
            commit_sha=sha,
            files=files,
            extra={"branch": branch, "pushed": pushed},
            storage=storage,
        )

    return {
        "commit_sha": sha,
        "release_id": tag,
        "tag": tag,
        "pushed": pushed,
        "no_op": False,
    }


def list_releases(agent_id: str, repo_dir: str, limit: int = 50) -> list[dict]:
    """List recent release tags for an agent (parsed from `git tag --list`).

    Returns: [{tag, sha, message, ts}] newest first.
    """
    pattern = f"agent/{agent_id}/release/*"
    proc = _git(
        ["for-each-ref", "--format=%(refname:short)|%(objectname)|%(creatordate:iso-strict)|%(subject)",
         "--sort=-creatordate", f"refs/tags/{pattern}"],
        cwd=str(Path(repo_dir).resolve()),
        check=False,
    )
    out: list[dict] = []
    for line in (proc.stdout or "").splitlines()[:limit]:
        parts = line.split("|", 3)
        if len(parts) < 3:
            continue
        out.append({
            "tag": parts[0],
            "sha": parts[1],
            "ts": parts[2],
            "message": parts[3] if len(parts) > 3 else "",
        })
    return out
