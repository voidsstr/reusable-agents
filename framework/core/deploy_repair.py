"""Deploy-gate repair: a red pre-deploy test gate starts a fix, not a dead end.

The deployer (agents/deployer/deployer.py) runs a site's Playwright suite
against the exact image it is about to ship, and nothing deploys while that
gate is red. Until 2026-10-19 nothing else happened either. The failing commit
stayed on master, every later dispatch rebuilt it and failed again, and the
site's deploys stayed blocked until a person looked. This module holds the
decisions that turn a red gate into test -> fix -> retest -> deploy:

  classify()             pass / flaky / code / infra / config / environment,
                         from the test command's exit code, its output and
                         Playwright's JSON report. Only `code` ever reaches an
                         LLM. An infra failure (a container race, Docker down,
                         a registry blip, a browser that cannot launch) is
                         retried, never "fixed" in code. `environment` (opt-in,
                         `environment_timeouts`): every failure is a request or
                         navigation timeout on a test that passed quickly in
                         the last green run, so an upstream the test container
                         calls got slow; it is retried and alerted, never
                         repaired.
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
                         last green / deployed commit, a log of deploys, and
                         the last green run's per-test durations.
  load_config()          DEFAULTS < site.yaml deployer.repair < storage
                         config/deploy-repair-config.json (top level, then
                         by_site.<site>).
  data_only_kinds()      the ONE list of dispatch kinds that never deploy
                         (dispatch.py's site-lock bypass, the implementer's
                         deployer skip).
  no_deploy_reason()     whether a batch's changed paths need no deploy
                         (storage config/deploy-skip-paths.json, off unless
                         configured there).
  gate_hold()            whether a dispatch of an app-code batch should wait
                         because the site's gate has been red too long
                         (storage config/deploy-gate-hold.json, off unless
                         enabled there). release_probe() / release_holds()
                         undo its bookkeeping when a probe never reached the
                         gate, or the gate is green again.

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
    # Environment reds (opt-in). On 2026-11-09 one site's gate container
    # started reaching a slow LAN LLM host, three import tests that had
    # passed in about a second began timing out at 45-120 s, and 17 Opus
    # repair sessions went into a failure no code change could fix. With this
    # on, a red run whose every failure is a request or navigation timeout
    # (env_timeout_patterns) on a test that took at most env_fast_fraction of
    # that timeout in the last green run is classed `environment`: re-run
    # once (env_retries), then blocked and alerted at once, never repaired.
    # Off by default: it withholds LLM repair from a class of reds, so an
    # operator switches it on in the storage config.
    "environment_timeouts": False,
    "env_timeout_patterns": [
        r"(?:apiRequestContext|request|fetch)\.\w+: (?:Test )?[Tt]imeout (?:of )?\d+ms exceeded",
        r"page\.(?:goto|reload|goBack|goForward|waitForURL|waitForLoadState|waitForNavigation"
        r"|waitForResponse|waitForRequest|waitForEvent): (?:Test )?[Tt]imeout (?:of )?\d+ms exceeded",
    ],
    "env_fast_fraction": 0.25,
    "env_retries": 1,
    "env_retry_wait_s": 60,
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
    timeout_ms: int = 0             # the test's own timeout, from the report

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
    durations: dict = field(default_factory=dict)     # key -> seconds of the passing attempt

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
                try:
                    f.timeout_ms = int(t.get("timeout") or 0)
                except (TypeError, ValueError):
                    f.timeout_ms = 0
                rep.all_keys.add(f.key)
                if status in ("expected", "flaky"):
                    rep.passed_keys.add(f.key)
                    # How long the passing attempt took: the environment
                    # check compares a later timeout against it.
                    passing = [r for r in (t.get("results") or [])
                               if isinstance(r, dict) and r.get("status") == "passed"]
                    try:
                        if passing:
                            rep.durations[f.key] = round(float(passing[-1].get("duration") or 0) / 1000.0, 2)
                    except (TypeError, ValueError):
                        pass
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
    kind: str            # pass | flaky | code | infra | config | environment
    reason: str
    rc: int
    failures: list = field(default_factory=list)   # list[TestFailure]

    @property
    def ok(self) -> bool:
        return self.kind in ("pass", "flaky")


_TIMEOUT_MS_RE = re.compile(r"[Tt]imeout (?:of )?(\d+)ms")


def _timeout_limit_ms(f: TestFailure) -> int:
    """The tightest timeout named in the test's error, else the test's own."""
    found = [int(x) for x in _TIMEOUT_MS_RE.findall(f.error or "")]
    return min(found) if found else int(f.timeout_ms or 0)


def environment_reason(report: Optional[Report], last_green: Optional[dict],
                       cfg: Optional[dict]) -> str:
    """Why this red looks like the test environment rather than the code, or
    "" when it does not (or the check is off).

    Every test that ran and failed must have timed out on a request or a
    navigation (cfg env_timeout_patterns, never an element or assertion
    wait, which is how a UI regression fails), and must have passed in the
    last green run in at most env_fast_fraction of that timeout. A test that
    did not run counts only beside a timed-out test in the same file (its
    serial group). A global error, or any test with no green duration,
    means "code" as before. The gate still blocks either way."""
    cfg = cfg or {}
    if not cfg.get("environment_timeouts") or report is None or not last_green:
        return ""
    if report.global_errors:
        return ""
    tests = [f for f in report.failures if f.title != "<global error>"]
    ran = [f for f in tests if not f.did_not_run]
    if not ran:
        return ""
    try:
        pats = [re.compile(p) for p in (cfg.get("env_timeout_patterns")
                                        or DEFAULTS["env_timeout_patterns"])]
        frac = float(cfg.get("env_fast_fraction", DEFAULTS["env_fast_fraction"]))
    except (re.error, TypeError, ValueError):
        return ""
    files: set[str] = set()
    then_s: list[float] = []
    limits_s: list[float] = []
    for f in ran:
        if not any(p.search(f.error or "") for p in pats):
            return ""
        limit_ms = _timeout_limit_ms(f)
        try:
            then = float(last_green.get(f.key))
        except (TypeError, ValueError):
            return ""
        if limit_ms <= 0 or then > frac * limit_ms / 1000.0:
            return ""
        files.add(f.file)
        then_s.append(then)
        limits_s.append(limit_ms / 1000.0)
    if any(f.did_not_run and f.file not in files for f in tests):
        return ""

    def _span(xs: list[float]) -> str:
        lo, hi = min(xs), max(xs)
        return f"{lo:g}s" if lo == hi else f"{lo:g}-{hi:g}s"
    return (f"{len(ran)} test(s) that took {_span(then_s)} in the last green run now time out "
            f"at {_span(limits_s)}: an upstream the test container calls got slow or "
            f"unreachable (LLM, network), not the code")


def classify(rc: int, output: str, report: Optional[Report], *,
             last_green: Optional[dict] = None, cfg: Optional[dict] = None) -> Classification:
    """`last_green` is GateState.last_green_durations and `cfg` the repair
    config; with both, a timeout-only red can come back as `environment`
    (environment_reason). Without them the result is what it always was."""
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
        env = environment_reason(report, last_green, cfg)
        if env:
            return Classification("environment", env, rc or 1, list(report.failures))
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


def verify_repaired(before: Classification, after: Optional[Report],
                    confirm: Optional[Report] = None) -> tuple[bool, list[str]]:
    """After a repair and a green re-run: did the failing tests really pass?

    Every test that failed or did not run before the repair must be in the new
    report and have passed (a flaky pass counts). Nothing may have stopped
    running, and the suite must not have shrunk. A repair that gets green by
    excluding, renaming or skipping the failing tests is rejected here, whatever
    the syntax of the change.

    `confirm` is the report of the confirm run, when the re-run went green
    through it: the re-run failed some tests under parallel load and their
    spec files then passed alone at --workers=1. The re-run's own report
    still lists those failures, so judging it alone rejected every such green
    (2026-11-11: a correct repair, then 361 passed / 0 failed in the confirm,
    no deploy). With `confirm`, the confirm run must be clean (0 failed,
    0 not run), every test the re-run failed must have passed in it, and
    every previously failing test must have passed in one of the two.
    """
    if after is None:
        return False, ["the re-run produced no Playwright report"]
    problems: list[str] = []
    passed = set(after.passed_keys)
    if confirm is None:
        if after.unexpected or after.did_not_run:
            problems.append(f"the re-run still has {after.unexpected} failed and "
                            f"{after.did_not_run} not-run test(s)")
    else:
        if confirm.unexpected or confirm.did_not_run:
            problems.append(f"the confirm run still has {confirm.unexpected} failed and "
                            f"{confirm.did_not_run} not-run test(s)")
        unconfirmed = [f for f in after.failures
                       if f.title != "<global error>" and f.key not in confirm.passed_keys]
        if unconfirmed:
            names = "; ".join(f.title for f in unconfirmed[:5])
            problems.append(f"{len(unconfirmed)} test(s) that failed in the re-run did not pass "
                            f"in the confirm run: {names}")
        passed |= confirm.passed_keys
    missing = [f for f in before.failures
               if f.title != "<global error>" and f.key not in passed]
    if missing:
        names = "; ".join(f.title for f in missing[:5])
        where = "the re-run" if confirm is None else "the re-run or its confirm run"
        problems.append(f"{len(missing)} previously failing test(s) did not pass in {where} "
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
    # Every deploy, newest last: {at, sha, tag}. `history` keeps 30 events,
    # which is under a day of a busy site's deploys; the placement watch
    # (framework/core/placement_watch.py) compares clicks before and after
    # deploys up to a few days old.
    deploys: list = field(default_factory=list)
    # test key -> seconds its passing attempt took in the last green run
    # (environment_reason compares a later timeout against it).
    last_green_durations: dict = field(default_factory=dict)

    DEPLOYS_KEEP = 400

    @classmethod
    def load(cls, site: str) -> "GateState":
        p = state_dir() / f"{_safe(site)}.json"
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return cls(site=site)
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        st = cls(**{**known, "site": site})
        # State written before the deploy log (2026-11-11) has its deploys
        # only in the 30-event history: seed the log so they outlive it.
        if "deploys" not in d:
            st.deploys = [{"at": e.get("at"), "sha": e.get("sha") or "", "tag": e.get("tag") or ""}
                          for e in st.history
                          if isinstance(e, dict) and e.get("event") == "deployed" and e.get("at")]
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

    def _event(self, what: str, *, at: Optional[datetime] = None, **extra: Any) -> None:
        self.history.append({"at": _iso(at or _now()), "event": what, **extra})
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
        self._event("red", at=now, signature=signature, kind=c.kind, attempts=attempts,
                    exhausted=exhausted)

    def record_deferred(self, c: Classification, signature: str, *, wait_s: int, why: str,
                        now: Optional[datetime] = None) -> None:
        """Red, and the repair could not even start (the pool had no capacity).
        Not a failed cycle: retry soon, no backoff climb."""
        now = now or _now()
        e = self._mark_red(c, signature, now)
        e["next_repair_at"] = _iso(now + timedelta(seconds=int(wait_s)))
        self.last_reason = f"{c.kind}: {c.reason}; {why}"
        self._event("deferred", at=now, signature=signature, why=why[:120])

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

    def record_deployed(self, sha: str, tag: str, now: Optional[datetime] = None) -> None:
        now = now or _now()
        self.last_deployed_sha = sha
        self.last_deployed_tag = tag
        self.history.append({"at": _iso(now), "event": "deployed", "sha": sha[:12], "tag": tag})
        self.history = self.history[-30:]
        self.deploys.append({"at": _iso(now), "sha": sha, "tag": tag})
        self.deploys = self.deploys[-self.DEPLOYS_KEEP:]

    def deploy_log(self) -> list[dict]:
        """Every known deploy, oldest first: `deploys`, plus `deployed`
        history events written before `deploys` existed. Deduplicated by tag."""
        seen: dict[str, dict] = {}
        for e in list(self.history) + list(self.deploys):
            if not isinstance(e, dict) or (e.get("event") not in (None, "deployed")):
                continue
            at = _parse_iso(str(e.get("at") or ""))
            if at is None:
                continue
            key = str(e.get("tag") or "") or _iso(at)
            cur = seen.get(key)
            if cur is None or len(str(e.get("sha") or "")) > len(str(cur.get("sha") or "")):
                seen[key] = {"at": _iso(at), "sha": str(e.get("sha") or ""), "tag": str(e.get("tag") or "")}
        return sorted(seen.values(), key=lambda d: d["at"])

    def record_green_durations(self, *reports: Optional[Report]) -> None:
        """Keep how long each test's passing attempt took in this green run.
        A later report's duration wins over an earlier one's for the same
        test. A test with no fresh duration keeps its older value while it is
        still in the suite; tests no longer in the suite drop out."""
        fresh: dict[str, float] = {}
        present: set[str] = set()
        for r in reports:
            if r is None:
                continue
            fresh.update(r.durations)
            present |= r.all_keys
        if not fresh:
            return
        keep = {k: v for k, v in self.last_green_durations.items()
                if k in present and k not in fresh}
        self.last_green_durations = {**keep, **fresh}

    def should_alert(self, signature: str, cfg: dict, now: Optional[datetime] = None) -> bool:
        now = now or _now()
        e = self.entry(signature)
        if e.get("kind") == "code":
            if int(e.get("cycles") or 0) < int(cfg.get("alert_after_cycles", 1)):
                return False
        elif e.get("kind") in ("landing", "environment"):
            # A green repair stuck off the branch, or an upstream the test
            # container depends on, needs a human now: no repair will come.
            pass
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


# ---------------------------------------------------------------------------
# What deploys at all: one list for the dispatcher and the implementer
# ---------------------------------------------------------------------------

# Dispatch kinds whose batches never go through the deployer: they write rows
# (catalog-audit, h2h, product-hydration) or publish / rewrite article rows
# (article-author, article-refresh). framework/core/dispatch.py lets them past
# the per-site dispatch lock, agents/implementer/run.sh skips the deployer
# chain for them, and gate_hold() never holds them. Until 2026-11-11 the
# dispatcher and run.sh each kept their own copy, and the two had drifted
# (product-hydration in one, article-refresh in the other).
# DATA_ONLY_KINDS (comma-separated) in the environment replaces the list for
# all three.
DATA_ONLY_KINDS: tuple[str, ...] = (
    "article-author", "article-refresh", "catalog-audit", "h2h", "product-hydration",
)


def data_only_kinds() -> frozenset[str]:
    raw = os.environ.get("DATA_ONLY_KINDS")
    kinds = raw.split(",") if raw else DATA_ONLY_KINDS
    return frozenset(k.strip() for k in kinds if k and k.strip())


def kind_skips_deploy(kind: str) -> bool:
    return bool(kind) and kind.strip() in data_only_kinds()


# Paths whose changes need no deploy of their own. OFF unless configured, in
# storage config/deploy-skip-paths.json (top level, then by_site.<site>) or
# the environment (DEPLOY_SKIP_PATHS / DEPLOY_SKIP_PATHS_WHILE_RED:
# comma-separated globs, or "none"). Without it every batch with commits
# deploys, as it always did. Two lists:
#   globs            paths that are not in the site's app image at all: change
#                    summaries, docs, the site's own agent code (read from disk
#                    by the agents). Check a site's Dockerfile before listing a
#                    path here. A batch whose commits touch only these skips
#                    the deployer.
#   while_red_globs  paths that ARE in the image but need no gate run of their
#                    own while the site's gate is red. DB migrations: the
#                    implementer applies a DB-only lever to the database itself
#                    and commits the migration as the record, but the image
#                    carries db/ and the app replays unrecorded migrations at
#                    boot, so while the gate is green a migration-only batch
#                    deploys as before and its own gate run checks that boot.
#                    While the gate is red nothing can ship anyway. A gate run
#                    for such a batch only spends gate time, and maybe an Opus
#                    repair, on a failure it did not cause. The next gate run
#                    tests the migration with everything else pending.
SKIP_CONFIG_KEY = "config/deploy-skip-paths.json"


def _env_globs(name: str) -> Optional[tuple[str, ...]]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    if raw.strip().lower() in ("none", "off", "0"):
        return ()
    return tuple(g.strip() for g in raw.split(",") if g.strip())


def load_skip_config(site: str = "", storage: Any = None) -> dict:
    """{"globs": (...), "while_red_globs": (...)}: storage config (top level,
    then by_site.<site>), then the environment overrides. A missing,
    unreadable or archived config means both are empty: deploy, as before."""
    out: dict[str, tuple[str, ...]] = {"globs": (), "while_red_globs": ()}
    env = {"globs": _env_globs("DEPLOY_SKIP_PATHS"),
           "while_red_globs": _env_globs("DEPLOY_SKIP_PATHS_WHILE_RED")}
    if any(v is None for v in env.values()):
        stored: Any = {}
        try:
            if storage is None:
                from .storage import get_storage
                storage = get_storage()
            stored = storage.read_json(SKIP_CONFIG_KEY) or {}
        except Exception:
            stored = {}
        layers = []
        if isinstance(stored, dict):
            layers.append(stored)
            by_site = stored.get("by_site")
            if site and isinstance(by_site, dict) and isinstance(by_site.get(site), dict):
                layers.append(by_site[site])
        for layer in layers:
            for k in out:
                v = layer.get(k)
                if isinstance(v, list):
                    out[k] = tuple(str(g).strip() for g in v if str(g).strip())
    for k, v in env.items():
        if v is not None:
            out[k] = v
    return out


def no_deploy_path_globs(site: str = "", storage: Any = None) -> tuple[str, ...]:
    """The configured paths that are never in the app image (`globs`)."""
    return load_skip_config(site, storage)["globs"]


def no_deploy_reason(paths: Iterable[str], globs: Optional[Iterable[str]] = None, *,
                     while_red_globs: Optional[Iterable[str]] = None,
                     gate_red: Optional[bool] = None, site: str = "",
                     storage: Any = None) -> str:
    """Why the changed `paths` need no deploy, or "" when they do. Also ""
    when nothing is configured (the default) or there are no paths (no
    evidence means deploy, as before).

    Every path must match `globs`, or match `while_red_globs` while the site's
    gate is red (GateState.load(site); `gate_red` overrides that). Lists left
    as None come from load_skip_config(site, storage)."""
    files = [p.strip() for p in paths if p and p.strip()]
    if not files:
        return ""
    if globs is None or while_red_globs is None:
        sc = load_skip_config(site, storage)
        globs = sc["globs"] if globs is None else globs
        while_red_globs = sc["while_red_globs"] if while_red_globs is None else while_red_globs
    always, red_only = tuple(globs), tuple(while_red_globs)
    if not always and not red_only:
        return ""
    tops = sorted({p.lstrip("./").split("/", 1)[0] + "/" for p in files})
    where = f"{', '.join(tops)} ({len(files)} file(s))"
    if always and all(_glob_match(p, always) for p in files):
        return (f"the batch's commits touch only {where}, which the deploy-skip config lists as "
                f"outside the app image")
    if not red_only or not all(_glob_match(p, always) or _glob_match(p, red_only) for p in files):
        return ""
    if gate_red is None:
        try:
            gate_red = bool(site) and GateState.load(site).status == "red"
        except Exception:
            gate_red = False
    if not gate_red:
        return ""
    return (f"the batch's commits touch only {where} and the {site or 'site'} deploy gate is red: "
            f"nothing can ship until it is green, and the next gate run tests these with "
            f"everything else that is pending")


# A rec its producer marked as needing no deploy: `db_only: true` (a database
# change the implementer applies directly, committing the migration only as
# the record), or a `dispatch_kind` that is data-only (data_only_kinds(), or
# the generic "db-only"). gate_hold() lets a batch through when every rec in
# it is marked: holding it would only delay a change that goes live without a
# deploy (2026-11-09..11: 13 of the 50 commits one site made while its gate
# was red were such levers, from four producers). The mark never skips the
# deployer: whether the implementer's commits need a deploy is still decided
# from what they touch (no_deploy_reason), so an app-code change a marked
# batch makes anyway still goes through the gate.
DB_ONLY_REC_KIND = "db-only"


def rec_declares_data_only(rec: Any, extra_kinds: Iterable[str] = ()) -> bool:
    if not isinstance(rec, dict):
        return False
    if rec.get("db_only") is True:
        return True
    k = str(rec.get("dispatch_kind") or "").strip()
    return bool(k) and (k == DB_ONLY_REC_KIND or k in data_only_kinds() or k in set(extra_kinds))


# ---------------------------------------------------------------------------
# Holding app-code dispatches while the gate stays red
# ---------------------------------------------------------------------------

# 2026-11-09..11: one site's gate was red for two days on failures no
# code change could fix. Every app-code dispatch still ran an Opus
# implementer session, then waited 40-70 minutes in the gate and exited rc=1
# (9 such runs and 3 reaped as stale in half a day). gate_hold() is the
# decision dispatch_now() asks before spawning one: hold app-code batches once
# the site's gate has been red for `red_for_h`; let data-only kinds, and
# batches whose every rec is marked DB-only (rec_declares_data_only), through;
# and let one dispatch through every `probe_every_h` so the gate is re-tested
# (nothing else re-runs it, and a red that is never re-tested never clears).
# A probe counts as spent once a gate verdict follows it. One that got none
# within `probe_grace_h` (the batch failed, or never reached the deployer) is
# granted again; dispatch_now() and the implementer also give a probe back
# when they know it will not reach the gate (release_probe). A held rec is
# parked in defer_backoff for `hold_retry_h` (not a counted defer), and
# release_holds() lifts the parked recs once the gate is green.
#
# OFF unless the storage config enables it. The hold changes what every
# producer's dispatch does, and it reads the deployer's local gate-state
# file; a missing, unreadable or archived config (config blobs are archived
# 120 days after their last write) must fall back to today's behaviour, never
# to holding work.
HOLD_CONFIG_KEY = "config/deploy-gate-hold.json"
HOLD_STATE_PREFIX = "framework/deploy-gate-hold/"
HOLD_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "red_for_h": 6.0,
    "probe_every_h": 4.0,
    # Longer than an implementer session plus a gate run with its repair
    # loop, so a probe still in flight is not granted twice.
    "probe_grace_h": 3.0,
    "hold_retry_h": 1.0,
    "pass_kinds": [],
}


@dataclass
class HoldDecision:
    hold: bool
    reason: str = ""
    probe: bool = False
    site: str = ""
    kind: str = ""
    red_since: str = ""
    red_for_h: float = 0.0
    gate_status: str = ""  # the site's gate status seen: green | red | unknown ("" = not checked)
    probe_at: str = ""     # when a granted probe was recorded (release_probe names it)
    retry_at: str = ""     # a held batch's recs are parked in defer_backoff until then

    def to_dict(self) -> dict:
        return asdict(self)


def load_hold_config(site: str, storage: Any = None) -> dict:
    """HOLD_DEFAULTS < storage config (top level) < by_site.<site>.
    DEPLOY_GATE_HOLD_DISABLED=1 turns it off whatever the config says."""
    cfg = dict(HOLD_DEFAULTS)
    stored: Any = {}
    try:
        if storage is None:
            from .storage import get_storage
            storage = get_storage()
        stored = storage.read_json(HOLD_CONFIG_KEY) or {}
    except Exception:
        stored = {}
    if isinstance(stored, dict):
        cfg.update({k: v for k, v in stored.items() if k != "by_site" and not k.startswith("_")})
        per_site = (stored.get("by_site") or {}).get(site) if isinstance(stored.get("by_site"), dict) else None
        if isinstance(per_site, dict):
            cfg.update({k: v for k, v in per_site.items() if not k.startswith("_")})
    if os.environ.get("DEPLOY_GATE_HOLD_DISABLED") == "1":
        cfg["enabled"] = False
    return cfg


def _hold_state_key(site: str) -> str:
    return f"{HOLD_STATE_PREFIX}{_safe(site)}.json"


def _last_gate_verdict_at(st: GateState) -> Optional[datetime]:
    times = [_parse_iso(str(e.get("at") or "")) for e in st.history
             if isinstance(e, dict) and e.get("event") in ("red", "deferred", "green")]
    times = [t for t in times if t is not None]
    return max(times) if times else _parse_iso(st.red_since)


def gate_hold(site: str, kind: str, *, subject_tag: str = "", recs: Optional[Iterable[dict]] = None,
              storage: Any = None, now: Optional[datetime] = None,
              state: Optional[GateState] = None, grant_probe: bool = True) -> HoldDecision:
    """Should a dispatch of `kind` for `site` wait for the gate?

    Holds only when the hold is enabled, the kind is not data-only (by the
    implementer's kind or the caller's subject_tag), not every rec in `recs`
    (the dispatched recs) is marked DB-only, the site's gate is red and has
    been for red_for_h, and no probe is due. Any error means no hold: the
    dispatch then behaves exactly as it did before this existed."""
    try:
        cfg = load_hold_config(site, storage)
        if not cfg.get("enabled") or not site:
            return HoldDecision(False, site=site, kind=kind)
        st = state if state is not None else GateState.load(site)
        gate = st.status or "unknown"
        passing = set(data_only_kinds()) | {str(k).strip() for k in (cfg.get("pass_kinds") or [])}
        if (kind or "").strip() in passing or (subject_tag or "").strip() in passing:
            return HoldDecision(False, site=site, kind=kind, gate_status=gate)
        since = _parse_iso(st.red_since)
        if st.status != "red" or since is None:
            return HoldDecision(False, site=site, kind=kind, gate_status=gate)
        now = now or _now()
        red_h = (now - since).total_seconds() / 3600.0
        base = HoldDecision(False, site=site, kind=kind, red_since=st.red_since,
                            red_for_h=round(red_h, 1), gate_status=gate)
        if red_h < float(cfg.get("red_for_h", HOLD_DEFAULTS["red_for_h"])):
            return base
        # Every rec marked DB-only: let it through without spending the probe
        # (it may never reach the gate).
        batch = list(recs or [])
        if batch and all(rec_declares_data_only(r, passing) for r in batch):
            base.reason = (f"all {len(batch)} rec(s) in the batch are marked DB-only "
                           f"(db_only / a data-only dispatch_kind): not held by the red {site} gate")
            return base
        probe_s = float(cfg.get("probe_every_h", HOLD_DEFAULTS["probe_every_h"])) * 3600.0
        if probe_s > 0:
            last_gate = _last_gate_verdict_at(st) or since
            doc = {}
            try:
                if storage is None:
                    from .storage import get_storage
                    storage = get_storage()
                doc = storage.read_json(_hold_state_key(site)) or {}
            except Exception:
                doc = {}
            last_probe = _parse_iso(str((doc or {}).get("last_probe_at") or ""))
            grace_s = float(cfg.get("probe_grace_h", HOLD_DEFAULTS["probe_grace_h"])) * 3600.0
            # The last probe is spent once a gate verdict follows it; one that
            # got no verdict within the grace never reached the gate.
            probe_free = (last_probe is None or last_gate >= last_probe
                          or (now - last_probe).total_seconds() >= grace_s)
            if (now - last_gate).total_seconds() >= probe_s and probe_free:
                if grant_probe:
                    try:
                        doc = doc if isinstance(doc, dict) else {}
                        doc["last_probe_at"] = _iso(now)
                        storage.write_json(_hold_state_key(site), doc)
                        base.probe_at = doc["last_probe_at"]
                    except Exception:
                        pass
                base.probe = True
                base.reason = (f"probe: the {site} gate has been red for {red_h:.0f}h and last ran "
                               f"{(now - last_gate).total_seconds() / 3600.0:.1f}h ago; letting "
                               f"this dispatch through so the gate is re-tested")
                return base
        base.hold = True
        retry_h = float(cfg.get("hold_retry_h", HOLD_DEFAULTS["hold_retry_h"]))
        base.retry_at = _iso(now + timedelta(hours=max(retry_h, 0.05)))
        base.reason = (f"the {site} deploy gate has been red for {red_h:.0f}h "
                       f"(since {st.red_since}; {st.last_reason or 'no reason recorded'}). "
                       f"App-code dispatches wait until it is green. Data-only kinds "
                       f"({', '.join(sorted(passing))}) still run, and so does a batch whose every "
                       f"rec is marked db_only: true, so prefer DB-only levers and mark them db_only")
        return base
    except Exception:
        return HoldDecision(False, site=site, kind=kind)


def gate_hold_status(site: str, *, storage: Any = None, now: Optional[datetime] = None) -> dict:
    """For a producer's prompt or brief: is app-code work for `site` being
    held right now, and the one line to tell the LLM. Never grants a probe."""
    d = gate_hold(site, "seo", storage=storage, now=now, grant_probe=False)
    out = d.to_dict()
    out["message"] = (f"DEPLOY GATE: {d.reason}" if d.hold else "")
    return out


def release_probe(site: str, *, granted_at: str, storage: Any = None) -> bool:
    """Give back the probe granted at `granted_at` when it will not reach the
    gate (the dispatch failed; the batch made no commit or skipped the
    deployer), so the next app-code dispatch re-tests the gate instead of
    waiting probe_every_h. A newer probe is left alone. Best-effort; True when
    the probe was released."""
    try:
        if not site or not granted_at:
            return False
        if storage is None:
            from .storage import get_storage
            storage = get_storage()
        key = _hold_state_key(site)
        doc = storage.read_json(key) or {}
        if not isinstance(doc, dict) or str(doc.get("last_probe_at") or "") != granted_at:
            return False
        doc.pop("last_probe_at", None)
        doc["probes_released"] = int(doc.get("probes_released") or 0) + 1
        storage.write_json(key, doc)
        return True
    except Exception:
        return False


def record_hold(site: str, *, agent_id: str, rec_ids: Iterable[str], kind: str, reason: str,
                storage: Any = None, now: Optional[datetime] = None) -> None:
    """Note a held dispatch in framework storage (newest 50), best-effort,
    so an operator or KTLO tick can see what waited and why. `parked` lists
    the recs whose defer_backoff hold release_holds() lifts on green."""
    try:
        if storage is None:
            from .storage import get_storage
            storage = get_storage()
        key = _hold_state_key(site)
        doc = storage.read_json(key) or {}
        if not isinstance(doc, dict):
            doc = {}
        ids = [str(r) for r in rec_ids]
        held = doc.get("held") if isinstance(doc.get("held"), list) else []
        held.append({"at": _iso(now or _now()), "agent_id": agent_id,
                     "rec_ids": ids[:20], "kind": kind, "reason": reason[:300]})
        doc["held"] = held[-50:]
        doc["held_total"] = int(doc.get("held_total") or 0) + 1
        parked = doc.get("parked") if isinstance(doc.get("parked"), dict) else {}
        parked[agent_id] = sorted(set(parked.get(agent_id) or []) | set(ids))[-200:]
        doc["parked"] = parked
        storage.write_json(key, doc)
    except Exception:
        pass


def release_holds(site: str, *, storage: Any = None) -> int:
    """The site's gate is green (or no longer red): lift the defer_backoff
    holds put on its held recs (record_hold's `parked`), so they, and later
    recs that reuse their ids, dispatch now instead of at their retry time.
    Best-effort; returns the number of holds lifted."""
    try:
        if not site:
            return 0
        if storage is None:
            from .storage import get_storage
            storage = get_storage()
        key = _hold_state_key(site)
        doc = storage.read_json(key) or {}
        parked = doc.get("parked") if isinstance(doc, dict) and isinstance(doc.get("parked"), dict) else {}
        if not parked:
            return 0
        from . import defer_backoff
        lifted = 0
        for agent_id, rec_ids in parked.items():
            lifted += defer_backoff.release_holds(agent_id, list(rec_ids or []), storage=storage)
        doc["parked"] = {}
        doc["released_at"] = _iso(_now())
        storage.write_json(key, doc)
        return lifted
    except Exception:
        return 0
