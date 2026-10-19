"""Deploy-gate repair: a red pre-deploy test gate starts a fix, not a dead end.

The deployer (agents/deployer/deployer.py) runs a site's Playwright suite
against the exact image it is about to ship, and nothing deploys while that
gate is red. Until 2026-10-19 nothing else happened either. The failing commit
stayed on master, every later dispatch rebuilt it and failed again, and the
site's deploys stayed blocked until a person looked. This module holds the
decisions that turn a red gate into test -> fix -> retest -> deploy:

  classify()             pass / flaky / code / infra / config, from the test
                         command's exit code, its output and Playwright's JSON
                         report. Only `code` ever reaches an LLM. An infra
                         failure (a container race, Docker down, a lock
                         timeout) is retried, never "fixed" in code.
  parse_report()         the failing tests out of a Playwright JSON report.
  failure_signature()    a stable id for one set of failures, so backoff and
                         alerts key on the failure rather than the run.
  build_repair_prompt()  the instructions for one repair attempt.
  check_repair()         what a repair committed may ship: inside the site's
                         path scope, protected gate files untouched, and no
                         weakened tests. No added skip/fixme/only, no deleted
                         test files, no fewer expect() calls, and a written
                         TEST-CHANGE-JUSTIFICATION for any test edit.
  GateState              per-site memory across runs: red/green, signature,
                         cycles, backoff, last alert, last deployed commit.
  load_config()          DEFAULTS < site.yaml deployer.repair < storage
                         config/deploy-repair-config.json (top level, then
                         by_site.<site>).

No site names, no cloud and no LLM client live here. The deployer owns the
subprocesses; this module owns the decisions so they can be unit tested.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


CONFIG_KEY = "config/deploy-repair-config.json"

DEFAULTS: dict[str, Any] = {
    # Master switch. DEPLOY_REPAIR_DISABLED=1 in the environment, or
    # {"enabled": false} in the storage config, turns repair off everywhere
    # (the gate still blocks; it just stops trying to fix).
    "enabled": True,
    # LLM repair attempts within one deploy run.
    "max_attempts": 3,
    # Wall-clock budget for the whole test -> repair -> retest loop in one
    # run. The auto-queue drainer waits 7200 s for a dispatch.
    "max_wall_s": 5400,
    # One LLM repair session.
    "attempt_timeout_s": 2400,
    # One run of the site's gate command.
    "test_timeout_s": 1800,
    # Re-runs of an infrastructure failure before giving up for this run.
    "infra_retries": 2,
    "infra_retry_wait_s": 60,
    # Re-run only the failed tests at --workers=1 before calling them a code
    # failure. One node container serves every worker, so load can look like
    # a bug.
    "confirm_failures": True,
    "model": "claude-opus-5-5",
    "max_turns": 150,
    # A repair may correct a test that asserts something untrue, with a
    # written justification. It may never weaken one (check_repair).
    "allow_test_edits": True,
    "test_globs": [
        "tests/**", "**/__tests__/**", "**/*.spec.ts", "**/*.spec.js",
        "**/*.test.ts", "**/*.test.tsx", "**/*.test.js",
        "playwright.config.*", "pw.config.*", "**/playwright.config.*",
        "**/pw.config.*",
    ],
    # The gate's own machinery. A repair that edits these is rejected.
    "protected_globs": [
        "tests/run-local-suite.sh", "docker-compose.test.yml",
        "agents/**/site.yaml",
    ],
    # Seconds to wait before the next repair cycle of the SAME failure
    # signature, indexed by exhausted cycles (the last value repeats).
    "backoff_s": [0, 1800, 7200, 21600, 43200],
    # Email the operator once a signature has exhausted this many cycles,
    # then at most every realert_s.
    "alert_after_cycles": 1,
    "realert_s": 86400,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(site_id: str, site_block: Optional[dict] = None,
                storage: Any = None) -> dict:
    """DEFAULTS < site.yaml `deployer.repair` < storage config < by_site.

    A missing or unreadable storage config means defaults (config blobs are
    archived 120 days after their last write; that must never switch repair
    off or on by accident).
    """
    cfg = dict(DEFAULTS)
    if isinstance(site_block, dict):
        cfg.update({k: v for k, v in site_block.items() if v is not None})
    stored: dict = {}
    if storage is not None:
        try:
            stored = storage.read_json(CONFIG_KEY) or {}
        except Exception:
            stored = {}
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k != "by_site"})
        per_site = (stored.get("by_site") or {}).get(site_id)
        if isinstance(per_site, dict):
            cfg.update(per_site)
    if os.environ.get("DEPLOY_REPAIR_DISABLED") == "1":
        cfg["enabled"] = False
    return cfg


# ---------------------------------------------------------------------------
# Playwright JSON report
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(text: str, limit: int) -> str:
    text = _ANSI.sub("", text or "")
    return text if len(text) <= limit else text[:limit] + " …"


@dataclass
class TestFailure:
    title: str                      # "describe › test" path
    file: str = ""
    line: int = 0
    project: str = ""
    error: str = ""                 # first error message, ANSI stripped
    attachments: list = field(default_factory=list)   # screenshot / trace paths

    @property
    def key(self) -> str:
        return f"{self.file}::{self.title}"


@dataclass
class Report:
    failures: list = field(default_factory=list)      # list[TestFailure]
    expected: int = 0
    unexpected: int = 0
    flaky: int = 0
    skipped: int = 0
    flaky_titles: list = field(default_factory=list)
    global_errors: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.expected + self.unexpected + self.flaky + self.skipped


def parse_report(path: str | Path) -> Optional[Report]:
    """The failing and flaky tests from a Playwright JSON report, or None when
    there is no readable report (the suite never reached Playwright)."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    stats = data.get("stats") or {}
    rep = Report(
        expected=int(stats.get("expected") or 0),
        unexpected=int(stats.get("unexpected") or 0),
        flaky=int(stats.get("flaky") or 0),
        skipped=int(stats.get("skipped") or 0),
    )
    for e in data.get("errors") or []:
        msg = _clean((e or {}).get("message") or str(e), 1500)
        rep.global_errors.append(msg)
        rep.failures.append(TestFailure(title="<global error>", error=msg))

    def walk(suite: dict, trail: list[str]) -> None:
        title = suite.get("title") or ""
        here = trail + ([title] if title and not title.endswith((".ts", ".js")) else [])
        for spec in suite.get("specs") or []:
            for t in spec.get("tests") or []:
                status = t.get("status")
                full = " › ".join(here + [spec.get("title") or ""])
                if status == "flaky":
                    rep.flaky_titles.append(full)
                if status != "unexpected":
                    continue
                results = t.get("results") or []
                last = results[-1] if results else {}
                errs = last.get("errors") or ([last["error"]] if last.get("error") else [])
                msg = "\n".join(_clean((e or {}).get("message") or "", 1200) for e in errs[:2])
                atts = [a.get("path") for a in last.get("attachments") or [] if a.get("path")]
                rep.failures.append(TestFailure(
                    title=full, file=spec.get("file") or suite.get("file") or "",
                    line=int(spec.get("line") or 0), project=t.get("projectName") or "",
                    error=msg.strip(), attachments=atts[:4],
                ))
        for child in suite.get("suites") or []:
            walk(child, here)

    for s in data.get("suites") or []:
        walk(s, [])
    return rep


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Exit codes of the sites' tests/run-local-suite.sh and the deployer.
INFRA_RC = {
    2: "the fixture DB is not up",
    4: "the test container did not start",
    6: "the test container never became healthy",
    7: "timed out waiting for another test run",
    79: "the local dev server is unavailable",
    124: "the test step timed out",
    137: "the test step was killed",
    143: "the test step was terminated",
}
CONFIG_RC = {78: "no tests matched the gate selection"}

# Output that means the machine, not the code, failed. Checked before an
# exit code that could be either (3 = image does not build, 5 = container
# exited before healthy).
INFRA_PATTERNS = [
    r"Cannot connect to the Docker daemon",
    r"No such container",
    r"is already in use by container",
    r"Conflict\. The container name",
    r"address already in use",
    r"port is already allocated",
    r"no space left on device",
    r"TLS handshake timeout",
    r"toomanyrequests",
    r"failed to resolve source metadata",
    r"failed to do request: .*(timeout|connection refused)",
    r"net/http: request canceled",
    r"error pulling image configuration",
    r"Temporary failure in name resolution",
    r"OOMKilled|out of memory",
]
_INFRA_RE = re.compile("|".join(f"(?:{p})" for p in INFRA_PATTERNS), re.I)


@dataclass
class Classification:
    kind: str            # pass | flaky | code | infra | config
    reason: str
    rc: int
    failures: list = field(default_factory=list)   # list[TestFailure]

    @property
    def ok(self) -> bool:
        return self.kind in ("pass", "flaky")


def classify(rc: int, output: str, report: Optional[Report]) -> Classification:
    text = (output or "")[-400_000:]
    if rc == 0:
        if report is not None and report.flaky:
            return Classification("flaky", f"{report.flaky} test(s) passed only on retry", 0)
        return Classification("pass", "gate passed", 0)
    if rc in CONFIG_RC:
        return Classification("config", CONFIG_RC[rc], rc)
    if rc in INFRA_RC:
        return Classification("infra", INFRA_RC[rc], rc)
    m = _INFRA_RE.search(text)
    if m and rc in (3, 5):
        return Classification("infra", f"exit {rc} with infrastructure error: {m.group(0)[:120]}", rc)
    if rc == 3:
        return Classification("code", "the image does not build", rc)
    if rc == 5:
        return Classification("code", "the container exited before it was healthy (boot crash)", rc)
    if report is not None and report.failures:
        n = len([f for f in report.failures if f.title != "<global error>"])
        what = f"{n} test(s) failed" if n else "the test run reported errors"
        return Classification("code", what, rc, list(report.failures))
    if m:
        return Classification("infra", f"exit {rc} with infrastructure error: {m.group(0)[:120]}", rc)
    return Classification("infra", f"exit {rc} with no test failure in the report", rc)


def failure_signature(c: Classification) -> str:
    """Same failures -> same signature, whatever run or attempt found them."""
    if c.failures:
        basis = "\n".join(sorted({f.key for f in c.failures}))
    else:
        basis = f"{c.kind}:{c.rc}"
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def summarize_failures(failures: Iterable[TestFailure], limit: int = 12) -> list[dict]:
    out = []
    for f in list(failures)[:limit]:
        out.append({"title": f.title, "file": f.file, "line": f.line,
                    "error": _clean(f.error, 600)})
    return out


# ---------------------------------------------------------------------------
# Repair prompt
# ---------------------------------------------------------------------------

def build_repair_prompt(*, site: str, worktree: str, runbook: str,
                        classification: Classification, output_tail: str,
                        container_log_tail: str, unshipped_log: str,
                        diffstat: str, attempt: int, max_attempts: int,
                        previous: list[str], rerun_hint: str,
                        allow_test_edits: bool) -> str:
    lines = [
        f"You are the deploy-gate repair agent for {site}. Attempt {attempt} of {max_attempts}.",
        "",
        f"Repository (a clean checkout of the commit the deployer is trying to ship): {worktree}",
        f"Gate result: {classification.kind} — {classification.reason} (exit {classification.rc})",
        "",
    ]
    if classification.failures:
        lines.append("Failing tests (from Playwright's JSON report):")
        for f in classification.failures[:15]:
            loc = f"{f.file}:{f.line}" if f.file else ""
            lines.append(f"- {f.title}  [{loc}]")
            if f.error:
                for el in _clean(f.error, 900).splitlines()[:12]:
                    lines.append(f"    {el}")
            for a in f.attachments[:2]:
                lines.append(f"    attachment: {a}")
        if len(classification.failures) > 15:
            lines.append(f"- … and {len(classification.failures) - 15} more")
        lines.append("")
    if container_log_tail.strip():
        lines += ["Server log tail (the test container):", "```", container_log_tail[-6000:], "```", ""]
    lines += ["Gate output tail:", "```", output_tail[-8000:], "```", ""]
    if unshipped_log.strip():
        lines += ["Commits since the last deploy that passed this gate (the likely cause):",
                  "```", unshipped_log[-4000:], "```", ""]
    if diffstat.strip():
        lines += ["Their diffstat:", "```", diffstat[-3000:], "```", ""]
    if previous:
        lines.append("Earlier attempts in this run, and why they did not get the gate green:")
        lines += [f"- {p}" for p in previous]
        lines.append("")
    lines += [
        "To check your fix against one test (it rebuilds the image from this checkout):",
        f"  {rerun_hint}",
        "",
        "Test edits are " + ("allowed only under the runbook's rules." if allow_test_edits
                              else "NOT allowed in this run: fix the application."),
        "",
        "Your runbook is below. Follow it exactly.",
        "",
        runbook,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# What a repair may ship
# ---------------------------------------------------------------------------

_WEAKEN_RE = re.compile(
    r"^\+(?!\+\+).*\b(?:test|it|describe|suite)(?:\.describe)?\.(?:skip|fixme|only|fail)\s*\(",
    re.M)
_ONLY_RE = re.compile(r"^\+(?!\+\+).*\.only\s*\(", re.M)
_EXPECT_RE = re.compile(r"\bexpect(?:\.soft|\.poll)?\s*\(")
JUSTIFICATION = "TEST-CHANGE-JUSTIFICATION:"


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, check=False).stdout


def _glob_match(path: str, globs: Iterable[str]) -> bool:
    p = path.lstrip("./")
    for g in globs:
        if fnmatch.fnmatch(p, g):
            return True
        if g.startswith("**/") and fnmatch.fnmatch(p, g[3:]):
            return True
    return False


def check_repair(repo: str, base_sha: str, head_sha: str, *, scope: Any = None,
                 cfg: Optional[dict] = None) -> tuple[bool, list[str], list[str]]:
    """Return (ok, problems, test_files_changed) for the commits base..head.

    `scope` is an implementer_scope.ScopePolicy (or anything with
    is_path_allowed). Problems make the attempt void: the deployer resets the
    checkout to base and tells the next attempt why.
    """
    cfg = cfg or DEFAULTS
    problems: list[str] = []
    if not head_sha or head_sha == base_sha:
        return False, ["the repair made no commit"], []
    changed = []
    for row in _git(repo, "diff", "--name-status", "--no-renames", base_sha, head_sha).splitlines():
        parts = row.split("\t")
        if len(parts) >= 2:
            changed.append((parts[0][:1], parts[-1]))
    if not changed:
        return False, ["the repair's commits change no files"], []
    test_globs = cfg.get("test_globs") or []
    protected = cfg.get("protected_globs") or []
    tests_changed: list[str] = []
    for status, path in changed:
        if _glob_match(path, protected):
            problems.append(f"{path}: part of the gate's own machinery, a repair may not edit it")
        if scope is not None and not scope.is_path_allowed(path):
            problems.append(f"{path}: outside the site's implementer scope")
        if _glob_match(path, test_globs):
            tests_changed.append(path)
            if status == "D":
                problems.append(f"{path}: a repair may not delete a test file")
    if tests_changed:
        if not cfg.get("allow_test_edits", True):
            problems.append("test files changed but test edits are not allowed: "
                            + ", ".join(tests_changed[:5]))
        diff = _git(repo, "diff", "-U0", base_sha, head_sha, "--", *tests_changed)
        if _WEAKEN_RE.search(diff) or _ONLY_RE.search(diff):
            problems.append("adds test.skip/fixme/only/fail to a test")
        for path in tests_changed:
            old = _git(repo, "show", f"{base_sha}:{path}")
            new = _git(repo, "show", f"{head_sha}:{path}")
            if old and len(_EXPECT_RE.findall(new)) < len(_EXPECT_RE.findall(old)):
                problems.append(f"{path}: fewer expect() calls than before")
        messages = _git(repo, "log", "--format=%B", f"{base_sha}..{head_sha}")
        if JUSTIFICATION not in messages:
            problems.append(f"test files changed without a '{JUSTIFICATION}' line in the commit message")
    return (not problems), problems, tests_changed


# ---------------------------------------------------------------------------
# Per-site gate memory
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    d = Path(os.path.expanduser(os.environ.get(
        "DEPLOY_GATE_STATE_DIR", "~/.reusable-agents/deployer/gate-state")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "site")


@dataclass
class GateState:
    site: str
    status: str = "unknown"          # green | red | unknown
    signature: str = ""
    red_since: str = ""
    cycles: int = 0                  # exhausted repair cycles for this signature
    attempts_total: int = 0
    last_attempt_at: str = ""
    next_repair_at: str = ""
    last_reason: str = ""
    last_failures: list = field(default_factory=list)
    last_green_sha: str = ""
    last_deployed_sha: str = ""
    alerts: dict = field(default_factory=dict)        # signature -> iso
    history: list = field(default_factory=list)       # last 30 events

    @classmethod
    def load(cls, site: str) -> "GateState":
        p = state_dir() / f"{_safe(site)}.json"
        try:
            d = json.loads(p.read_text())
            known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
            return cls(**{**known, "site": site})
        except (OSError, ValueError, TypeError):
            return cls(site=site)

    def save(self) -> None:
        p = state_dir() / f"{_safe(self.site)}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, p)

    def _event(self, what: str, **extra: Any) -> None:
        self.history.append({"at": _iso(_now()), "event": what, **extra})
        self.history = self.history[-30:]

    def may_repair(self, signature: str, now: Optional[datetime] = None) -> tuple[bool, str]:
        now = now or _now()
        if self.status == "red" and signature == self.signature:
            nxt = _parse_iso(self.next_repair_at)
            if nxt and now < nxt:
                return False, f"repair of failure {signature} backing off until {self.next_repair_at}"
        return True, ""

    def record_red(self, c: Classification, signature: str, *, attempts: int,
                   exhausted: bool, cfg: dict, now: Optional[datetime] = None) -> None:
        now = now or _now()
        if self.status != "red" or signature != self.signature:
            self.cycles = 0
            self.red_since = self.red_since if self.status == "red" else _iso(now)
        self.status = "red"
        self.signature = signature
        self.attempts_total += attempts
        self.last_attempt_at = _iso(now)
        self.last_reason = f"{c.kind}: {c.reason}"
        self.last_failures = summarize_failures(c.failures, 8)
        if exhausted:
            self.cycles += 1
            backoff = list(cfg.get("backoff_s") or DEFAULTS["backoff_s"])
            wait = backoff[min(self.cycles, len(backoff) - 1)]
            self.next_repair_at = _iso(now + timedelta(seconds=int(wait)))
        self._event("red", signature=signature, kind=c.kind, attempts=attempts,
                    exhausted=exhausted)

    def record_green(self, sha: str, *, repaired: bool) -> None:
        was_red = self.status == "red"
        self.status = "green"
        self.last_green_sha = sha
        self.signature = ""
        self.cycles = 0
        self.next_repair_at = ""
        self.red_since = ""
        self.last_reason = "repaired" if repaired else "passed"
        self.last_failures = []
        self._event("green", sha=sha[:12], repaired=repaired, was_red=was_red)

    def record_deployed(self, sha: str, tag: str) -> None:
        self.last_deployed_sha = sha
        self._event("deployed", sha=sha[:12], tag=tag)

    def should_alert(self, signature: str, cfg: dict, now: Optional[datetime] = None) -> bool:
        now = now or _now()
        if self.cycles < int(cfg.get("alert_after_cycles", 1)):
            return False
        last = _parse_iso(self.alerts.get(signature, ""))
        return last is None or (now - last).total_seconds() >= int(cfg.get("realert_s", 86400))

    def record_alert(self, signature: str, now: Optional[datetime] = None) -> None:
        self.alerts[signature] = _iso(now or _now())
        # Keep the map small: only the 20 newest signatures.
        if len(self.alerts) > 20:
            for k in sorted(self.alerts, key=self.alerts.get)[:-20]:
                self.alerts.pop(k, None)
