"""Implementer edit guards — pre-commit sanity checks on LLM-produced diffs.

Two distinct guards, both invoked from `agents/implementer/run.sh` after the
post-LLM scope-revert pass and before `git add`:

  1. Date-regression guard.
     Catches the LLM-stale-knowledge pattern where a recent year (current or
     last) gets replaced with an older one. Real example caught after the
     fact on aisleprompt 2026-05-13: a /features footnote replaced "as of
     2026" with "as of 2024". The model's training cutoff makes this
     hallucination predictable; a 10-line regex check stops it cold.

  2. Critical-element sentinel.
     Site config declares a list of must-not-disappear DOM selectors / file
     anchors (data-testid attributes, key href targets, ARIA landmarks
     monetization depends on). If a changed file used to contain the
     sentinel and no longer does, the guard flags it for revert. Caught
     after the fact: `[data-testid="floating-instacart-cta"]` disappeared
     from /recipes pages and the deployer's smoke test caught it post-push.
     This guard catches it BEFORE the commit.

Invocation contract (called as a script from run.sh):

    REPO_ROOT=...                 # framework checkout root (for PYTHONPATH)
    REPO_PATH=...                 # path to the site repo (where the diff lives)
    CHANGED_FILES_LIST=...        # newline-delimited file paths (relative to REPO_PATH)
    SITE_CONFIG_PATH=...          # site.yaml (for implementer.critical_elements)
    python3 -m framework.core.edit_guards

Exits 0 if all guards pass. Exits non-zero with a JSON object on stdout
describing what failed and which files should be reverted. The caller
(run.sh) consumes that JSON and runs `git checkout -- <file>` on each.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Years considered "recent" for date-regression detection. Replacing a
# recent year with an older one is the hallucination signature.
RECENT_YEAR_WINDOW = 2  # current year and one prior


# ──────────────────────────────────────────────────────────────────
# Guard 1: date-regression
# ──────────────────────────────────────────────────────────────────

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass
class DateRegression:
    file: str
    line_no: int
    removed_year: int
    added_year: int

    def __str__(self) -> str:
        return (f"{self.file}:{self.line_no}  "
                f"{self.removed_year} → {self.added_year} "
                f"(stale-knowledge hallucination)")


def detect_date_regressions(
    repo_path: Path,
    changed_files: Iterable[str],
    *,
    today_year: int | None = None,
) -> list[DateRegression]:
    """Walk the unstaged diff for each changed file. Flag any hunk that
    replaces a recent year (current or last) with an older one.

    We use `git diff --unified=0 HEAD` so we see only the lines that
    actually changed; matching across context lines would produce false
    positives when the SAME year appears in context.
    """
    if today_year is None:
        today_year = datetime.now(timezone.utc).year
    recent_threshold = today_year - RECENT_YEAR_WINDOW
    flagged: list[DateRegression] = []

    for f in changed_files:
        if not f:
            continue
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--unified=0", "HEAD", "--", f],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except Exception:
            continue
        if proc.returncode != 0 or not proc.stdout:
            continue
        flagged.extend(_scan_diff_for_year_regression(
            proc.stdout, f, recent_threshold,
        ))
    return flagged


def _scan_diff_for_year_regression(
    diff_text: str, file: str, recent_threshold: int,
) -> list[DateRegression]:
    """Pair each `-` line with its sibling `+` line(s) inside the same hunk.
    Only when a year on the `-` side is >= recent_threshold AND a year on
    the `+` side is strictly smaller do we flag.
    """
    out: list[DateRegression] = []
    new_line_no = 0
    removed_buf: list[tuple[int, str]] = []  # (line_no_estimate, content)
    added_buf: list[tuple[int, str]] = []

    def _flush_hunk() -> None:
        nonlocal removed_buf, added_buf
        removed_years = [
            (line, int(y))
            for line, content in removed_buf
            for y in _YEAR_RE.findall(content)
        ]
        added_years = [
            (line, int(y))
            for line, content in added_buf
            for y in _YEAR_RE.findall(content)
        ]
        for r_line, r_year in removed_years:
            if r_year < recent_threshold:
                continue
            for a_line, a_year in added_years:
                if a_year < r_year:
                    out.append(DateRegression(
                        file=file, line_no=a_line,
                        removed_year=r_year, added_year=a_year,
                    ))
        removed_buf = []
        added_buf = []

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            _flush_hunk()
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                new_line_no = int(m.group(1))
            continue
        if line.startswith("---") or line.startswith("+++") or line.startswith("diff "):
            continue
        if line.startswith("-") and not line.startswith("--"):
            removed_buf.append((new_line_no, line[1:]))
            # `-` lines don't advance new-side line numbers
        elif line.startswith("+") and not line.startswith("++"):
            added_buf.append((new_line_no, line[1:]))
            new_line_no += 1
        else:
            new_line_no += 1
    _flush_hunk()
    return out


# ──────────────────────────────────────────────────────────────────
# Guard 2: critical-element sentinel
# ──────────────────────────────────────────────────────────────────

@dataclass
class SentinelLoss:
    selector: str
    file: str
    reason: str
    was_present_at: str = ""  # HEAD sha or "HEAD" for visual reference

    def __str__(self) -> str:
        return f"missing `{self.selector}` from {self.file} — {self.reason}"


def detect_critical_element_loss(
    repo_path: Path,
    changed_files: Iterable[str],
    sentinels: list[dict],
) -> list[SentinelLoss]:
    """For each (selector, files) entry in `sentinels`, check whether each
    referenced file (a) still exists on disk AND (b) still contains the
    selector substring. If the file was present at HEAD with the selector
    but the working copy has lost it, flag it.

    The check is a substring grep — we're not running a DOM parser. That's
    cheap, fast, and a single false positive (sentinel string lives in a
    comment that got reformatted) costs an operator one revert. A miss
    (regression slips through) costs a production outage. We bias toward
    false positives.

    `sentinels` shape (from site.yaml `implementer.critical_elements`):
        [
          {"selector": "[data-testid=\"floating-instacart-cta\"]",
           "files": ["src/simple-server.ts", "frontend/src/components/Recipe*.tsx"],
           "reason": "monetization: Instacart click recording"},
          ...
        ]
    """
    changed_set = {Path(f).as_posix() for f in changed_files if f}
    out: list[SentinelLoss] = []

    for spec in sentinels or []:
        sel = (spec.get("selector") or "").strip()
        if not sel:
            continue
        files_spec = spec.get("files") or []
        if isinstance(files_spec, str):
            files_spec = [files_spec]
        reason = (spec.get("reason") or "unspecified").strip()
        # The sentinel only matters if a file it watches was changed.
        affected = [
            f for f in changed_set
            if _file_matches_any_glob(f, files_spec)
        ]
        if not affected:
            continue
        for f in affected:
            try:
                head_blob = subprocess.run(
                    ["git", "-C", str(repo_path), "show", f"HEAD:{f}"],
                    capture_output=True, text=True, check=False, timeout=30,
                )
            except Exception:
                continue
            if head_blob.returncode != 0:
                continue
            head_has = sel in head_blob.stdout
            if not head_has:
                continue
            full = repo_path / f
            try:
                current = full.read_text(encoding="utf-8", errors="replace") if full.exists() else ""
            except Exception:
                current = ""
            if sel not in current:
                out.append(SentinelLoss(
                    selector=sel, file=f, reason=reason, was_present_at="HEAD",
                ))
    return out


def _file_matches_any_glob(file: str, patterns: list[str]) -> bool:
    """fnmatch-style glob, with `**` matching across segments. Reuses the
    same matching primitive `implementer_scope.ScopePolicy` already does."""
    if not patterns:
        return False
    from fnmatch import fnmatch
    posix = Path(file).as_posix()
    for p in patterns:
        # Normalize `**` to match across segments
        norm = p.replace("**/", "*/").replace("**", "*")
        if fnmatch(posix, p) or fnmatch(posix, norm):
            return True
        # Also try the unanchored form (any path containing the pattern)
        if "/" in posix and not p.startswith("/"):
            for prefix_drop in range(len(posix.split("/"))):
                tail = "/".join(posix.split("/")[prefix_drop:])
                if fnmatch(tail, p) or fnmatch(tail, norm):
                    return True
    return False


# ──────────────────────────────────────────────────────────────────
# Orchestrator — runs both guards, prints JSON report
# ──────────────────────────────────────────────────────────────────

@dataclass
class GuardReport:
    date_regressions: list[DateRegression] = field(default_factory=list)
    sentinel_losses: list[SentinelLoss] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return bool(self.date_regressions or self.sentinel_losses)

    @property
    def files_to_revert(self) -> set[str]:
        s = {dr.file for dr in self.date_regressions}
        s.update(sl.file for sl in self.sentinel_losses)
        return s

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.has_failures,
            "date_regressions": [
                {"file": dr.file, "line": dr.line_no,
                 "removed_year": dr.removed_year, "added_year": dr.added_year}
                for dr in self.date_regressions
            ],
            "sentinel_losses": [
                {"selector": sl.selector, "file": sl.file, "reason": sl.reason}
                for sl in self.sentinel_losses
            ],
            "files_to_revert": sorted(self.files_to_revert),
        }


def run_guards(
    repo_path: Path,
    changed_files: list[str],
    site_config: dict | None,
    *,
    today_year: int | None = None,
) -> GuardReport:
    """Run all enabled guards, return a single report."""
    report = GuardReport()

    # Guard 1 — always on
    report.date_regressions = detect_date_regressions(
        repo_path, changed_files, today_year=today_year,
    )

    # Guard 2 — opt-in via site.yaml `implementer.critical_elements`
    sentinels = []
    if site_config:
        impl_block = (site_config.get("implementer") or {})
        sentinels = impl_block.get("critical_elements") or []
    if sentinels:
        report.sentinel_losses = detect_critical_element_loss(
            repo_path, changed_files, sentinels,
        )

    return report


def _main_cli() -> int:
    """CLI entry — reads env, prints JSON to stdout, exits non-zero on failure."""
    repo_path = Path(os.environ.get("REPO_PATH") or ".").resolve()
    changed_files_list = os.environ.get("CHANGED_FILES_LIST")
    if not changed_files_list:
        print(json.dumps({"ok": True, "skipped": "no CHANGED_FILES_LIST"}))
        return 0
    try:
        changed_files = [
            ln.strip() for ln in Path(changed_files_list).read_text().splitlines()
            if ln.strip()
        ]
    except Exception as e:
        print(json.dumps({"ok": True, "skipped": f"read CHANGED_FILES_LIST: {e}"}))
        return 0
    if not changed_files:
        print(json.dumps({"ok": True, "skipped": "empty changed_files"}))
        return 0

    site_cfg = None
    site_cfg_path = os.environ.get("SITE_CONFIG_PATH")
    if site_cfg_path and Path(site_cfg_path).exists():
        try:
            import yaml  # type: ignore
            site_cfg = yaml.safe_load(Path(site_cfg_path).read_text()) or {}
        except Exception as e:
            print(json.dumps({
                "ok": True, "skipped": f"read site config: {e}"
            }))
            return 0

    report = run_guards(repo_path, changed_files, site_cfg)
    print(json.dumps(report.to_dict()))
    return 1 if report.has_failures else 0


if __name__ == "__main__":
    sys.exit(_main_cli())


__all__ = [
    "DateRegression", "SentinelLoss", "GuardReport",
    "detect_date_regressions", "detect_critical_element_loss",
    "run_guards",
]
