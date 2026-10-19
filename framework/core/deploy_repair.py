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
                         failure (a container race, Docker down, a registry
                         blip, a browser that cannot launch) is retried, never
                         "fixed" in code.
  parse_report()         the failing tests, and the tests that never ran
                         because a beforeAll or a serial neighbour failed.
  failure_signature()    a stable id for one set of failures.
  build_repair_prompt()  the instructions for one repair attempt.
  check_repair()         what a repair committed may ship. It must build on the
                         commit under test, stay inside the site's path scope,
                         leave the gate machinery alone (runner, Playwright
                         configs, page manifests, migrations), and not weaken a
                         test: no added skip/fixme/only/fail, no deleted test
                         files, no fewer expect() calls, and a written
                         TEST-CHANGE-JUSTIFICATION for any test edit.
  verify_repaired()      the semantic check after the re-run. Every test that
                         failed or never ran before the repair must now be
                         present and passing, and the suite must not have
                         shrunk. A green gate that got there by losing tests
                         is not green.
  GateState              per-site memory across runs: per-signature cycles and
                         backoff (an infra red cannot reset a code failure's
                         backoff), a daily cap on repair sessions, alert times,
                         last green / deployed commit.
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
    # Repair sessions per site per rolling 24 h, across every signature. A
    # failure set that keeps shifting (one test more, one fewer) would
    # otherwise look new each time and get a fresh budget.
    "max_repairs_per_day": 9,
    # Wall-clock budget for the whole test -> repair -> retest loop in one
    # run (the deploy-lock wait is not part of it).
    "max_wall_s": 5400,
    # One LLM repair session (shortened so the re-test always fits).
    "attempt_timeout_s": 2400,
    # One run of the site's gate command.
    "test_timeout_s": 1800,
    # Re-runs of an infrastructure failure before giving up for this run.
    "infra_retries": 2,
    "infra_retry_wait_s": 60,
    # Re-run the spec files that failed, alone at --workers=1, before calling
    # it a code failure. Green there, with nothing skipped, means flaky.
    "confirm_failures": True,
    "model": "claude-opus-5-5",
    "max_turns": 150,
    # A repair may correct a test that asserts something untrue, with a
    # written justification. It may never weaken one (check_repair).
    "allow_test_edits": True,
    "test_globs": [
        "tests/**", "**/__tests__/**", "**/*.spec.ts", "**/*.spec.js",
        "**/*.test.ts", "**/*.test.tsx", "**/*.test.js",
    ],
    # Never edited by a repair: the gate's own machinery (what runs, how),
    # the all-pages manifest (an "exception" there drops a page from the
    # gate), and DB migrations (they run against production at boot).
    "protected_globs": [
        "tests/run-local-suite.sh", "docker-compose.test.yml",
        "agents/**/site.yaml",
        "playwright.config.*", "**/playwright.config.*",
        "pw.config.*", "**/pw.config.*",
        "tests/**/*manifest*", "tests/**/global-setup*", "tests/**/global-teardown*",
        "db/migrations/**", "**/migrations/**",
    ],
    # Seconds before the next repair cycle of the SAME failure signature,
    # indexed by exhausted cycles (the last value repeats).
    "backoff_s": [0, 1800, 7200, 21600, 43200],
    # When the claude-pool has no capacity, retry after this long. It is not
    # a failed repair, so it does not climb the backoff ladder.
    "pool_retry_s": 900,
    # Email the operator once a code signature has exhausted this many repair
    # cycles; any other red (infra, config, repair disabled, cannot land)
    # emails once the site has been red this long. Then at most every
    # realert_s per signature.
    "alert_after_cycles": 1,
    "alert_red_for_s": 7200,
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
    off or on by accident). Keys starting with "_" are notes, ignored.
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
        cfg.update({k: v for k, v in stored.items() if k != "by_site" and not k.startswith("_")})
        per_site = (stored.get("by_site") or {}).get(site_id)
        if isinstance(per_site, dict):
            cfg.update({k: v for k, v in per_site.items() if not k.startswith("_")})
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
    did_not_run: bool = False       # skipped because a beforeAll / serial neighbour failed

    @property
    def key(self) -> str:
        return f"{self.file}::{self.project}::{self.title}"


@dataclass
class Report:
    failures: list = field(default_factory=list)      # list[TestFailure]: failed + did not run
    expected: int = 0
    unexpected: int = 0
    flaky: int = 0
    skipped: int = 0
    did_not_run: int = 0
    flaky_titles: list = field(default_factory=list)
    global_errors: list = field(default_factory=list)
    passed_keys: set = field(default_factory=set)     # keys of tests that passed (incl. flaky)
    all_keys: set = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.expected + self.unexpected + self.flaky + self.skipped

    @property
    def failing_files(self) -> list[str]:
        return sorted({f.file for f in self.failures if f.file})


def parse_report(path: str | Path) -> Optional[Report]:
    """The failing, never-run and flaky tests from a Playwright JSON report, or
    None when there is no readable report (the suite never reached
    Playwright)."""
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
                f = TestFailure(title=full, file=spec.get("file") or suite.get("file") or "",
                                line=int(spec.get("line") or 0), project=t.get("projectName") or "")
                rep.all_keys.add(f.key)
                if status in ("expected", "flaky"):
                    rep.passed_keys.add(f.key)
                if status == "flaky":
                    rep.flaky_titles.append(full)
                # A test that was skipped although it was expected to run did
                # not run: its beforeAll failed, or a test before it in a
                # serial group did. Nothing proves it passes.
                dnr = status == "skipped" and (t.get("expectedStatus") or "passed") != "skipped"
                if dnr:
                    rep.did_not_run += 1
                if status != "unexpected" and not dnr:
                    continue
                results = t.get("results") or []
                last = results[-1] if results else {}
                errs = last.get("errors") or ([last["error"]] if last.get("error") else [])
                f.error = "\n".join(_clean((e or {}).get("message") or "", 1200)
                                    for e in errs[:2]).strip() or ("did not run" if dnr else "")
                f.attachments = [a.get("path") for a in last.get("attachments") or [] if a.get("path")][:4]
                f.did_not_run = dnr
                rep.failures.append(f)
        for child in suite.get("suites") or []:
            walk(child, here)

    for s in data.get("suites") or []:
        walk(s, [])
    return rep


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Exit codes of the sites' tests/run-local-suite.sh and the deployer. Exit 6
# (never became healthy) is infra the first time; the gate loop re-classifies
# a repeat as code (the app's own boot is the likelier cause by then).
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
_NO_TESTS = re.compile(r"No tests found|0 tests found", re.I)

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
    r"no space left on device|ENOSPC",
    r"TLS handshake timeout",
    r"toomanyrequests",
    r"failed to resolve source metadata",
    r"failed to do request: .*(timeout|connection refused)",
    r"net/http: request canceled",
    r"error pulling image configuration",
    r"Temporary failure in name resolution|EAI_AGAIN|Could not resolve host",
    r"npm (ERR!|error) (network|code E(TIMEDOUT|CONNRESET|CONNREFUSED|AI_AGAIN))",
    r"\b(ETIMEDOUT|ECONNRESET)\b.*registry",
    r"OOMKilled|out of memory",
]
_INFRA_RE = re.compile("|".join(f"(?:{p})" for p in INFRA_PATTERNS), re.I)
# Test errors that mean the test runner's browser could not start: every
# test fails at launch, whatever the code does.
_BROWSER_INFRA_RE = re.compile(
    r"Executable doesn't exist at|browserType\.launch: (Failed to launch|Target page)|"
    r"Host system is missing dependencies|Looks like Playwright Test or Playwright was just installed",
    re.I)


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
    dnr = report.did_not_run if report is not None else 0
    if rc in CONFIG_RC or _NO_TESTS.search(text) and (report is None or report.total == 0):
        return Classification("config", CONFIG_RC[78], 78)
    if rc == 0 and not dnr:
        if report is not None and report.flaky:
            return Classification("flaky", f"{report.flaky} test(s) passed only on retry", 0)
        return Classification("pass", "gate passed", 0)
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
        tests = [f for f in report.failures if f.title != "<global error>"]
        if tests and all(_BROWSER_INFRA_RE.search(f.error or "") for f in tests):
            return Classification("infra", "the test browser could not launch", rc)
        failed = sum(1 for f in tests if not f.did_not_run)
        what = (f"{failed} test(s) failed" if failed else "the test run reported errors")
        if dnr:
            what += f", {dnr} did not run"
        return Classification("code", what, rc or 1, list(report.failures))
    if m:
        return Classification("infra", f"exit {rc} with infrastructure error: {m.group(0)[:120]}", rc)
    return Classification("infra", f"exit {rc} with no test failure in the report", rc)


def failure_signature(c: Classification) -> str:
    """Same failures -> same signature, whatever run or attempt found them."""
    if c.failures:
        basis = c.kind + "\n" + "\n".join(sorted({f.key for f in c.failures}))
    else:
        basis = f"{c.kind}:{c.rc}"
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def summarize_failures(failures: Iterable[TestFailure], limit: int = 12) -> list[dict]:
    out = []
    for f in list(failures)[:limit]:
        out.append({"title": f.title, "file": f.file, "line": f.line,
                    "did_not_run": f.did_not_run, "error": _clean(f.error, 600)})
    return out


def verify_repaired(before: Classification, after: Optional[Report]) -> tuple[bool, list[str]]:
    """After a repair and a green re-run: did the failing tests really pass?

    Every test that failed or did not run before the repair must be in the new
    report and have passed (a flaky pass counts). Nothing may have stopped
    running, and the suite must not have shrunk. A repair that gets green by
    excluding, renaming or skipping the failing tests is rejected here, whatever
    the syntax of the change.
    """
    if after is None:
        return False, ["the re-run produced no Playwright report"]
    problems: list[str] = []
    if after.unexpected or after.did_not_run:
        problems.append(f"the re-run still has {after.unexpected} failed and "
                        f"{after.did_not_run} not-run test(s)")
    missing = [f for f in before.failures
               if f.title != "<global error>" and f.key not in after.passed_keys]
    if missing:
        names = "; ".join(f.title for f in missing[:5])
        problems.append(f"{len(missing)} previously failing test(s) did not pass in the re-run "
                        f"(missing, renamed or skipped): {names}")
    return (not problems), problems


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
            tag = "  (DID NOT RUN: an earlier test or hook in its group failed)" if f.did_not_run else ""
            lines.append(f"- {f.title}  [{loc}]{tag}")
            if f.error and not f.did_not_run:
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
        "The deployer re-runs the whole gate after you, and checks that every test listed",
        "above is present and passes. Making a test disappear does not count as a fix.",
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


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, check=False)


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
    # A repair builds on the commit under test. A HEAD that is not a
    # descendant (reset to an older commit, a rewritten history) would make
    # the deployer ship something else entirely.
    if _git(repo, "merge-base", "--is-ancestor", base_sha, head_sha).returncode != 0:
        return False, ["the repair moved HEAD off the commit under test (it is not a descendant)"], []
    changed = []
    for row in _git(repo, "diff", "--name-status", "--no-renames", base_sha, head_sha).stdout.splitlines():
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
            problems.append(f"{path}: the gate's own machinery (or a migration); a repair may not edit it")
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
        diff = _git(repo, "diff", "-U0", base_sha, head_sha, "--", *tests_changed).stdout
        if _WEAKEN_RE.search(diff) or _ONLY_RE.search(diff):
            problems.append("adds test.skip/fixme/only/fail to a test")
        for path in tests_changed:
            old = _git(repo, "show", f"{base_sha}:{path}").stdout
            new = _git(repo, "show", f"{head_sha}:{path}").stdout
            if old and len(_EXPECT_RE.findall(new)) < len(_EXPECT_RE.findall(old)):
                problems.append(f"{path}: fewer expect() calls than before")
        messages = _git(repo, "log", "--format=%B", f"{base_sha}..{head_sha}").stdout
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
    signature: str = ""              # the newest red's signature
    red_since: str = ""
    last_reason: str = ""
    last_failures: list = field(default_factory=list)
    # signature -> {kind, cycles, attempts, next_repair_at, first_seen, last_seen}
    failures: dict = field(default_factory=dict)
    repair_times: list = field(default_factory=list)  # iso, last 24 h
    attempts_total: int = 0
    last_green_sha: str = ""
    last_deployed_sha: str = ""
    last_deployed_tag: str = ""
    alerts: dict = field(default_factory=dict)        # signature -> iso
    history: list = field(default_factory=list)       # last 30 events

    @classmethod
    def load(cls, site: str) -> "GateState":
        p = state_dir() / f"{_safe(site)}.json"
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return cls(site=site)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        st = cls(**{**known, "site": site})
        # State written before per-signature tracking (2026-10-19) kept one
        # signature with top-level cycles / next_repair_at.
        if d.get("signature") and "failures" not in d:
            st.failures[d["signature"]] = {
                "kind": "code", "cycles": int(d.get("cycles") or 0), "attempts": 0,
                "next_repair_at": d.get("next_repair_at") or "",
                "first_seen": d.get("red_since") or "", "last_seen": d.get("last_attempt_at") or ""}
        return st

    def save(self) -> None:
        p = state_dir() / f"{_safe(self.site)}.json"
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, p)

    def _event(self, what: str, **extra: Any) -> None:
        self.history.append({"at": _iso(_now()), "event": what, **extra})
        self.history = self.history[-30:]

    def entry(self, signature: str) -> dict:
        return self.failures.get(signature) or {}

    def repairs_last_day(self, now: Optional[datetime] = None) -> int:
        now = now or _now()
        keep = [t for t in self.repair_times
                if (p := _parse_iso(t)) and (now - p).total_seconds() < 86400]
        self.repair_times = keep
        return len(keep)

    def may_repair(self, signature: str, cfg: Optional[dict] = None,
                   now: Optional[datetime] = None) -> tuple[bool, str]:
        cfg = cfg or DEFAULTS
        now = now or _now()
        nxt = _parse_iso(self.entry(signature).get("next_repair_at", ""))
        if nxt and now < nxt:
            return False, f"repair of failure {signature} backing off until {_iso(nxt)}"
        cap = int(cfg.get("max_repairs_per_day", DEFAULTS["max_repairs_per_day"]))
        if self.repairs_last_day(now) >= cap:
            return False, f"{cap} repair sessions already ran for this site in the last 24 h"
        return True, ""

    def record_repair_session(self, now: Optional[datetime] = None) -> None:
        self.repair_times.append(_iso(now or _now()))
        self.attempts_total += 1

    def _mark_red(self, c: Classification, signature: str, now: datetime) -> dict:
        if self.status != "red":
            self.red_since = _iso(now)
        self.status = "red"
        self.signature = signature
        self.last_reason = f"{c.kind}: {c.reason}"
        self.last_failures = summarize_failures(c.failures, 8)
        e = self.failures.setdefault(signature, {
            "kind": c.kind, "cycles": 0, "attempts": 0, "next_repair_at": "",
            "first_seen": _iso(now), "last_seen": ""})
        e["kind"] = c.kind
        e["last_seen"] = _iso(now)
        return e

    def record_red(self, c: Classification, signature: str, *, attempts: int,
                   exhausted: bool, cfg: dict, now: Optional[datetime] = None) -> None:
        """A red run. Only this signature's entry changes: an infra red can
        never reset a code failure's cycles or backoff."""
        now = now or _now()
        e = self._mark_red(c, signature, now)
        e["attempts"] = int(e.get("attempts") or 0) + attempts
        if exhausted:
            e["cycles"] = int(e.get("cycles") or 0) + 1
            backoff = list(cfg.get("backoff_s") or DEFAULTS["backoff_s"])
            wait = backoff[min(e["cycles"], len(backoff) - 1)]
            e["next_repair_at"] = _iso(now + timedelta(seconds=int(wait)))
        self._event("red", signature=signature, kind=c.kind, attempts=attempts,
                    exhausted=exhausted)

    def record_deferred(self, c: Classification, signature: str, *, wait_s: int, why: str,
                        now: Optional[datetime] = None) -> None:
        """Red, and the repair could not even start (the pool had no capacity).
        Not a failed cycle: retry soon, no backoff climb."""
        now = now or _now()
        e = self._mark_red(c, signature, now)
        e["next_repair_at"] = _iso(now + timedelta(seconds=int(wait_s)))
        self.last_reason = f"{c.kind}: {c.reason}; {why}"
        self._event("deferred", signature=signature, why=why[:120])

    def record_green(self, sha: str, *, repaired: bool) -> None:
        was_red = self.status == "red"
        self.status = "green"
        self.last_green_sha = sha
        self.signature = ""
        self.red_since = ""
        self.failures = {}
        self.last_reason = "repaired" if repaired else "passed"
        self.last_failures = []
        self._event("green", sha=sha[:12], repaired=repaired, was_red=was_red)

    def record_deployed(self, sha: str, tag: str) -> None:
        self.last_deployed_sha = sha
        self.last_deployed_tag = tag
        self._event("deployed", sha=sha[:12], tag=tag)

    def should_alert(self, signature: str, cfg: dict, now: Optional[datetime] = None) -> bool:
        now = now or _now()
        e = self.entry(signature)
        if e.get("kind") == "code":
            if int(e.get("cycles") or 0) < int(cfg.get("alert_after_cycles", 1)):
                return False
        elif e.get("kind") == "landing":
            pass                     # a green repair stuck off the branch needs a human now
        else:
            since = _parse_iso(self.red_since)
            if since is None or (now - since).total_seconds() < int(cfg.get("alert_red_for_s", 7200)):
                return False
        last = _parse_iso(self.alerts.get(signature, ""))
        return last is None or (now - last).total_seconds() >= int(cfg.get("realert_s", 86400))

    def record_alert(self, signature: str, now: Optional[datetime] = None) -> None:
        self.alerts[signature] = _iso(now or _now())
        # Keep the map small: only the 20 newest signatures.
        if len(self.alerts) > 20:
            for k in sorted(self.alerts, key=self.alerts.get)[:-20]:
                self.alerts.pop(k, None)
