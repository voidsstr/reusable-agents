"""The deployer's pre-deploy gate: test -> (repair -> retest)* -> green.

deployer.py calls `GateRunner.run()` once it holds the site's deploy lock. The
decisions (what failed, whether an LLM may touch it, what a repair may ship,
backoff, alerts) live in framework/core/deploy_repair.py. This module owns the
processes:

- The gate runs from a clean worktree of the exact commit that will ship,
  never the live working tree, where other agents' half-done edits sit. The
  image it builds is the image that ships (see deployer.py, build stage).
  Repair runs ONLY in such an isolated worktree. Without one (git could not
  make it, or build.from_clean_checkout is false) the gate still blocks, but
  nothing resets, cleans or edits the shared live tree.
- Every subprocess is streamed into a log file with a heartbeat line a
  minute. The backlog-dispatcher kills implementer scopes whose log is silent
  for 30 minutes, and a full suite plus a repair can take longer than that.
  On timeout the whole process group is killed, so Playwright and chromium
  don't outlive it.
- Playwright's JSON report and test-results are kept per run under
  ~/.reusable-agents/deployer/gate-artifacts/, outside the run dir (which is
  synced to blob storage), and pruned.
- A failed run is confirmed before it counts: the spec files that failed are
  re-run whole at --workers=1. Re-running only the failed tests would let a
  failed beforeAll or serial neighbour hide tests that never ran.
- A repair is an Opus session through the claude-pool (`claude --print`) with
  cwd = the worktree and agents/deployer/REPAIR.md as its runbook. It commits
  in the worktree. deploy_repair.check_repair() vets the commits, and after
  the re-run deploy_repair.verify_repaired() checks that every failing test now
  passes. A rejected attempt is reset and the next attempt is told why.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core import deploy_repair as dr  # noqa: E402

RUNBOOK = Path(__file__).resolve().parent / "REPAIR.md"

# Never handed to the test run or the repair session. The gate tests against
# the local fixture DB; a repair has no business with production data or
# cloud credentials. ANTHROPIC_* / CLAUDE_CODE_OAUTH_TOKEN would also take
# `claude` off the pool's per-profile credentials.
_SECRET_ENV = re.compile(
    r"^(DATABASE_URL.*|.*_DSN|.*CONNECTION_STRING.*|.*SECRET.*|.*PASSWORD.*|.*PASSWD.*|"
    r".*_KEY|.*_KEYS|.*ACCESS_KEY.*|.*_TOKEN|.*TOKEN_.*|.*CREDENTIAL.*|.*OAUTH.*|"
    r"AZURE_.*|AWS_.*|ARM_.*|GOOGLE_.*|GH_.*|GITHUB_.*|ANTHROPIC_.*|OPENAI_.*|EBAY_.*|"
    r"MSMTP.*|SMTP_.*|GRAPH_.*|FRAMEWORK_API.*|AMAZON_.*)$", re.I)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _image_exists(image_id: str) -> bool:
    if not image_id:
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", image_id], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def scrubbed_env(extra: Optional[dict] = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.match(k)}
    # The claude-pool shim gives the repair session a per-profile HOME, and
    # Playwright looks for its browsers under $HOME/.cache: point it at the
    # real cache so the session's own test re-runs can launch chromium.
    browsers = os.path.expanduser("~/.cache/ms-playwright")
    if "PLAYWRIGHT_BROWSERS_PATH" not in env and os.path.isdir(browsers):
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    env.update(extra or {})
    return env


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

# Lines worth echoing into the dispatch log as they happen; everything goes
# to the per-run log file.
_ECHO = re.compile(r"^\[suite\]|^\s+\d+\) |passed \(|failed|flaky|did not run|"
                   r"^\[claude-pool\]|ROOT CAUSE|FIX:|VERIFIED", re.I)


def run_streamed(name: str, cmd: str, *, cwd: str, env: dict, timeout_s: int,
                 log_path: Path, stdin_path: Optional[Path] = None,
                 heartbeat_s: int = 60) -> tuple[int, str]:
    """Run `bash -c cmd` in its own process group, tee stdout+stderr into
    log_path, print a heartbeat to stderr every heartbeat_s, and kill the
    group on timeout (rc 124). Returns (rc, last ~64 KB of output)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tail: deque[str] = deque()
    tail_bytes = [0]
    last_line = [""]
    log(f"[deployer:{name}] cwd={cwd} cmd={cmd[:240]}")
    stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=cwd, env=env, stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, text=True, errors="replace", bufsize=1)
    finally:
        if stdin_path:
            stdin.close()

    def pump() -> None:
        with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
            for line in proc.stdout:  # type: ignore[union-attr]
                fh.write(line)
                fh.flush()
                s = line.rstrip("\n")
                if s.strip():
                    last_line[0] = s
                tail.append(line)
                tail_bytes[0] += len(line)
                while tail_bytes[0] > 65536 and tail:
                    tail_bytes[0] -= len(tail.popleft())
                if _ECHO.search(s):
                    log(f"[deployer:{name}] {s[:300]}")

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    start = time.monotonic()
    next_beat = start + heartbeat_s
    rc: Optional[int] = None
    while rc is None:
        try:
            rc = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if now - start > timeout_s:
                log(f"[deployer:{name}] TIMEOUT after {int(now - start)}s; killing the process group")
                _kill_group(proc)
                rc = 124
                break
            if now >= next_beat:
                next_beat = now + heartbeat_s
                el = int(now - start)
                log(f"[deployer:{name}] running {el // 60}m{el % 60:02d}s · last: {last_line[0][:180]}")
    reader.join(timeout=10)
    log(f"[deployer:{name}] rc={rc} ({int(time.monotonic() - start)}s) log={log_path}")
    return int(rc), "".join(tail)


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def acquire_lock(lock: Any, timeout_s: int, what: str) -> None:
    """Wait for a FileLock, printing a line a minute (its own heartbeat goes to
    an unconfigured logger, so it never reaches the dispatch log)."""
    if lock.try_acquire():
        return
    holder = ""
    try:
        holder = Path(lock.path).read_text()[:120].strip()
    except OSError:
        pass
    log(f"[deployer] waiting for {Path(lock.path).name} ({what}); holder: {holder}")
    start = time.monotonic()
    next_beat = start + 60
    while True:
        if lock.try_acquire():
            log(f"[deployer] {Path(lock.path).name} acquired after {int(time.monotonic() - start)}s")
            return
        now = time.monotonic()
        if now - start > timeout_s:
            raise TimeoutError(f"could not acquire {Path(lock.path).name} within {timeout_s}s")
        if now >= next_beat:
            next_beat = now + 60
            log(f"[deployer] still waiting for {Path(lock.path).name} ({int(now - start)}s)")
        time.sleep(5)


def _git(repo: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check)


def rev(repo: str, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


WORKTREE_BASE = Path(os.path.expanduser("~/.reusable-agents/deploy-worktrees"))


def sweep_stale_worktrees(repo_root: Optional[str], site_id: str) -> None:
    """Remove worktrees left by runs that died without their finally (a kill,
    an OOM, a reboot): `git worktree prune` keeps any directory that still
    exists, and nothing else ever removes them."""
    if not repo_root or not WORKTREE_BASE.is_dir():
        return
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", site_id or "site") + "-"
    for d in WORKTREE_BASE.iterdir():
        if not d.name.startswith(prefix):
            continue
        pid = d.name[len(prefix):]
        if pid.isdigit() and Path(f"/proc/{pid}").exists():
            continue
        log(f"[deployer] removing stale worktree {d} (its run is gone)")
        _git(repo_root, "worktree", "remove", "--force", str(d))
        shutil.rmtree(d, ignore_errors=True)
    _git(repo_root, "worktree", "prune")


def checkout(repo_root: Optional[str], site_id: str, *, enabled: bool = True
             ) -> tuple[Optional[str], str, Any, bool]:
    """A detached worktree of HEAD: (path, sha, cleanup, isolated). It shares
    the object store and holds exactly what is committed. Falls back to the
    live tree (path = repo_root, isolated = False, a warning) when git cannot
    make one; the caller must then never repair."""
    noop = lambda: None  # noqa: E731
    if not repo_root or not os.path.isdir(os.path.join(repo_root, ".git")):
        return repo_root, "", noop, False
    sha = rev(repo_root)
    if not enabled:
        return repo_root, sha, noop, False
    WORKTREE_BASE.mkdir(parents=True, exist_ok=True)
    wt = WORKTREE_BASE / f"{re.sub(r'[^A-Za-z0-9_-]', '_', site_id or 'site')}-{os.getpid()}"
    _git(repo_root, "worktree", "prune")
    r = _git(repo_root, "worktree", "add", "--detach", "--force", str(wt), sha)
    if r.returncode != 0:
        log(f"[deployer] WARN clean checkout unavailable ({r.stderr.strip()[:200]}); "
            f"testing and building the live working tree, repair disabled")
        return repo_root, sha, noop, False
    dirty = _git(repo_root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        log(f"[deployer] testing and shipping {sha[:12]} from {wt}; NOT including "
            f"{len(dirty.splitlines())} uncommitted tracked change(s) in {repo_root}")

    def cleanup() -> None:
        _git(repo_root, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)

    return str(wt), sha, cleanup, True


def land(repo_root: str, branch: str, base_sha: str, head_sha: str) -> tuple[bool, str]:
    """Put the repair commits base..head on the live branch. Fast-forward when
    the branch has not moved; otherwise cherry-pick onto it. Never touches
    anyone's uncommitted work: a conflict aborts the pick."""
    if head_sha == base_sha:
        return True, "nothing to land"
    if _git(repo_root, "merge-base", "--is-ancestor", base_sha, head_sha).returncode != 0:
        return False, f"{head_sha[:12]} does not descend from {base_sha[:12]}"
    cur = _git(repo_root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if branch and cur != branch:
        return False, f"live repo is on {cur or 'a detached HEAD'}, not {branch}"
    for attempt in range(6):
        live = rev(repo_root)
        if live == base_sha:
            r = _git(repo_root, "merge", "--ff-only", head_sha)
            how = "fast-forwarded"
        else:
            r = _git(repo_root, "cherry-pick", "--allow-empty", f"{base_sha}..{head_sha}")
            how = "cherry-picked onto the moved branch"
            if r.returncode != 0 and "index.lock" not in (r.stderr or ""):
                _git(repo_root, "cherry-pick", "--abort")
        if r.returncode == 0:
            return True, f"{how} ({rev(repo_root)[:12]})"
        if "index.lock" in (r.stderr or ""):
            time.sleep(3)
            continue
        return False, (r.stderr or r.stdout).strip()[-400:]
    return False, "git index stayed locked by another process"


def reverted_batch_files(repo: str, batch_base: str, gate_base: str, head: str) -> list[str]:
    """Files the batch changed (batch_base..gate_base) that the repair put back
    exactly as they were before the batch. Their recs did not ship."""
    if not (batch_base and gate_base and head) or head == gate_base:
        return []
    if _git(repo, "cat-file", "-e", batch_base).returncode != 0:
        return []
    batch = set(_git(repo, "diff", "--name-only", batch_base, gate_base).stdout.split())
    repaired = set(_git(repo, "diff", "--name-only", gate_base, head).stdout.split())
    out = []
    for f in sorted(batch & repaired):
        if _git(repo, "diff", "--quiet", batch_base, head, "--", f).returncode == 0:
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# The gate loop
# ---------------------------------------------------------------------------

def artifacts_root(site_id: str) -> Path:
    d = Path(os.path.expanduser(os.environ.get(
        "DEPLOY_GATE_ARTIFACTS", "~/.reusable-agents/deployer/gate-artifacts"))) / site_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_artifacts(site_id: str, keep: int = 20) -> None:
    root = artifacts_root(site_id)
    runs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for p in runs[:-keep]:
        shutil.rmtree(p, ignore_errors=True)


_REPORTER_ARG = re.compile(r"\s--reporter(?:=|\s+)\S+")


@dataclass
class GateRun:
    label: str
    rc: int
    classification: dr.Classification
    art_dir: Path
    image_id: str = ""
    seconds: int = 0
    report: Optional[dr.Report] = None


@dataclass
class GateOutcome:
    ok: bool
    sha: str
    image_id: str = ""
    repaired: bool = False
    status_reason: str = ""
    runs: list = field(default_factory=list)          # summaries
    repairs: list = field(default_factory=list)       # summaries
    flaky: list = field(default_factory=list)
    classification: Optional[dr.Classification] = None
    signature: str = ""
    landed: str = ""
    deferred: bool = False                            # repair could not start (pool)

    def summary(self) -> dict:
        c = self.classification
        return {
            "ok": self.ok, "sha": self.sha, "repaired": self.repaired,
            "reason": self.status_reason, "signature": self.signature,
            "kind": c.kind if c else "", "rc": c.rc if c else None,
            "failures": dr.summarize_failures(c.failures if c else []),
            "runs": self.runs, "repairs": self.repairs, "flaky": self.flaky,
            "landed": self.landed, "image_id": self.image_id, "deferred": self.deferred,
        }


class GateRunner:
    def __init__(self, *, site_id: str, tag: str, test_cmd: str, test_cwd: Optional[str],
                 worktree: str, repo_root: str, branch: str, scope: Any,
                 cfg: dict, state: dr.GateState, run_dir: Path,
                 test_timeout_s: Optional[int] = None, isolated: bool = True,
                 allow_repair: bool = True):
        self.site_id = site_id
        self.tag = tag
        self.test_cmd = _REPORTER_ARG.sub("", " " + test_cmd).strip()
        self.test_cwd = test_cwd
        self.wt = worktree
        self.repo_root = repo_root
        self.branch = branch
        self.scope = scope
        self.cfg = cfg
        self.state = state
        self.run_dir = run_dir
        self.isolated = isolated
        self.allow_repair = allow_repair
        self.test_timeout_s = int(test_timeout_s or cfg.get("test_timeout_s") or 1800)
        self.root = artifacts_root(site_id) / f"{tag}-{os.getpid()}"
        self.start = time.monotonic()

    # -- helpers -------------------------------------------------------------
    def _cwd(self) -> str:
        if self.test_cwd and not os.path.isabs(self.test_cwd):
            return os.path.join(self.wt, self.test_cwd)
        return self.test_cwd or self.wt

    def _left(self) -> float:
        return float(self.cfg.get("max_wall_s", 5400)) - (time.monotonic() - self.start)

    def run_gate(self, label: str, extra: str = "") -> GateRun:
        art = self.root / label
        art.mkdir(parents=True, exist_ok=True)
        env = scrubbed_env({
            "GATE_SOURCE_REPO": self.repo_root,
            "GATE_ARTIFACT_DIR": str(art),
            "PLAYWRIGHT_JSON_OUTPUT_FILE": str(art / "report.json"),
            "FORCE_COLOR": "0",
            "PLAYWRIGHT_HTML_OPEN": "never",
        })
        cmd = f"{self.test_cmd} {extra} --reporter=line,json --output {art / 'test-results'}".strip()
        t0 = time.monotonic()
        rc, out = run_streamed(f"test:{label}", cmd, cwd=self._cwd(), env=env,
                               timeout_s=self.test_timeout_s, log_path=art / "output.log")
        report = dr.parse_report(art / "report.json")
        c = dr.classify(rc, out, report)
        image = ""
        iu = art / "image-under-test.txt"
        if iu.is_file():
            image = iu.read_text().strip()
        return GateRun(label, rc, c, art, image, int(time.monotonic() - t0), report)

    def _summ(self, g: GateRun) -> dict:
        r = g.report
        return {"label": g.label, "rc": g.rc, "kind": g.classification.kind,
                "reason": g.classification.reason, "seconds": g.seconds,
                "counts": ({"expected": r.expected, "unexpected": r.unexpected,
                            "flaky": r.flaky, "skipped": r.skipped,
                            "did_not_run": r.did_not_run} if r else None),
                "artifacts": str(g.art_dir)}

    def _confirm(self, g: GateRun, outcome: GateOutcome) -> GateRun:
        """Re-run the failing spec files whole, alone, at --workers=1. Flaky
        only if that run is clean AND every failing / not-run test passed in
        it; anything else keeps the main run's code classification."""
        files = g.report.failing_files if g.report else []
        if not files:
            return g
        conf = self.run_gate(f"{g.label}-confirm", " ".join(files) + " --workers=1")
        outcome.runs.append(self._summ(conf))
        ok, _ = dr.verify_repaired(g.classification, conf.report)
        if conf.rc == 0 and conf.classification.ok and ok:
            outcome.flaky += [f.title for f in g.classification.failures]
            log(f"[deployer:gate] {len(g.classification.failures)} failing test(s) passed when "
                f"their files ran alone at --workers=1; treating as flaky")
            # The confirm run rebuilds the image from the same checkout, which
            # moves the test tag and leaves the main run's image dangling, so
            # it can be pruned before the deployer tags it (2026-10-23:
            # `docker tag sha256:46e7…` rc=1 after a green gate). Ship the main
            # run's image while it exists, else the confirm run's (same sha).
            image = g.image_id if _image_exists(g.image_id) else (conf.image_id or g.image_id)
            return GateRun(g.label, 0, dr.Classification(
                "flaky", f"{len(g.classification.failures)} test(s) failed only under parallel load", 0),
                g.art_dir, image, g.seconds, g.report)
        return g

    def _context(self, g: GateRun) -> tuple[str, str, str, str]:
        try:
            out_tail = (g.art_dir / "output.log").read_text(errors="replace")[-12000:]
        except OSError:
            out_tail = ""
        try:
            clog = (g.art_dir / "container.log").read_text(errors="replace")[-6000:]
        except OSError:
            clog = ""
        since = self.state.last_deployed_sha or self.state.last_green_sha
        known = bool(since) and _git(self.wt, "merge-base", "--is-ancestor", since, "HEAD").returncode == 0
        if known:
            log_txt = _git(self.wt, "log", "--format=%h %an %ad %s", "--date=short", f"{since}..HEAD").stdout
            diffstat = _git(self.wt, "diff", "--stat", since, "HEAD").stdout
        else:
            log_txt = _git(self.wt, "log", "--format=%h %an %ad %s", "--date=short", "-15").stdout
            diffstat = ""
        return out_tail, clog, log_txt[-4000:], diffstat[-3000:]

    def repair(self, g: GateRun, attempt: int, previous: list[str], timeout_s: int) -> dict:
        """One LLM repair session. Returns {made, kind, note, before, after}."""
        out_tail, clog, unshipped, diffstat = self._context(g)
        first = next((f for f in g.classification.failures if f.file and not f.did_not_run), None)
        if first:
            title = re.sub(r"[\"$`\\]", ".", first.title.split(" › ")[-1])[:120]
            rerun = (f"cd {self._cwd()} && GATE_SOURCE_REPO={self.repo_root} "
                     f"{self.test_cmd} --reporter=line {first.file} -g \"{title}\"")
        else:
            rerun = f"cd {self._cwd()} && GATE_SOURCE_REPO={self.repo_root} {self.test_cmd} --reporter=line"
        prompt = dr.build_repair_prompt(
            site=self.site_id, worktree=self.wt, runbook=RUNBOOK.read_text(),
            classification=g.classification, output_tail=out_tail, container_log_tail=clog,
            unshipped_log=unshipped, diffstat=diffstat, attempt=attempt,
            max_attempts=int(self.cfg.get("max_attempts", 3)), previous=previous,
            rerun_hint=rerun, allow_test_edits=bool(self.cfg.get("allow_test_edits", True)))
        rdir = self.root / f"repair-{attempt}"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "prompt.md").write_text(prompt)
        claude = self.cfg.get("claude_bin") or os.environ.get("DEPLOY_REPAIR_CLAUDE_BIN") or "claude"
        cmd = (f"{claude} --dangerously-skip-permissions --print --output-format text "
               f"--model {self.cfg.get('model', 'claude-opus-5-5')} "
               f"--max-turns {int(self.cfg.get('max_turns', 150))}")
        before = rev(self.wt)
        env = scrubbed_env({"CLAUDE_POOL_FAIL_FAST": "1", "FORCE_COLOR": "0",
                            "DEPLOY_GATE_REPAIR": "1"})
        rc, out = run_streamed(f"repair:{attempt}", cmd, cwd=self.wt, env=env, timeout_s=timeout_s,
                               log_path=rdir / "session.log", stdin_path=rdir / "prompt.md")
        if rc != 75:                       # a session ran; it counts toward the daily cap
            self.state.record_repair_session()
        # Anything the session left uncommitted, ignored files included, is not
        # part of the repair and must not reach the image this checkout builds.
        _git(self.wt, "reset", "--hard", "HEAD")
        _git(self.wt, "clean", "-ffdx")
        after = rev(self.wt)
        tail = " ".join(out.strip().splitlines()[-3:])[:300]
        if rc == 75:
            if after != before:
                _git(self.wt, "reset", "--hard", before)
            return {"made": False, "kind": "pool", "before": before, "after": before,
                    "note": f"attempt {attempt}: the claude-pool had no capacity (rc 75); no session ran"}
        ok, problems, tests_changed = dr.check_repair(self.wt, before, after,
                                                     scope=self.scope, cfg=self.cfg)
        if not ok:
            if after != before:
                _git(self.wt, "reset", "--hard", before)
            return {"made": False, "kind": "rejected", "before": before, "after": before,
                    "note": f"attempt {attempt}: rejected ({'; '.join(problems)[:500]}); session said: {tail}"}
        commits = _git(self.wt, "log", "--format=%h %s", f"{before}..{after}").stdout.strip()
        note = f"attempt {attempt}: committed {commits.replace(chr(10), ' | ')[:300]}"
        if tests_changed:
            note += f" (test files changed with justification: {', '.join(tests_changed[:4])})"
        return {"made": True, "kind": "commit", "before": before, "after": after, "note": note}

    # -- the loop --------------------------------------------------------------
    def run(self) -> GateOutcome:
        """Run the gate until green, out of budget, or not repairable. Records
        red states; the caller records green (after landing a repair)."""
        base = rev(self.wt)
        outcome = GateOutcome(ok=False, sha=base)
        previous: list[str] = []
        infra_left = int(self.cfg.get("infra_retries", 2))
        blip_checked = False
        rc6_seen = 0
        repairs = 0
        n = 0
        targets: dict[str, dr.TestFailure] = {}       # every failure seen this run
        last_commit: Optional[dict] = None            # the newest accepted repair
        g: Optional[GateRun] = None
        rerun_needed = True
        try:
            while True:
                if rerun_needed:
                    n += 1
                    g = self.run_gate(f"run-{n}")
                    outcome.runs.append(self._summ(g))
                    if (g.classification.kind == "code" and g.classification.failures
                            and self.cfg.get("confirm_failures", True)):
                        g = self._confirm(g, outcome)
                rerun_needed = True
                c = g.classification
                outcome.classification = c
                if g.report and g.report.flaky_titles:
                    outcome.flaky += g.report.flaky_titles
                for f in c.failures:
                    if f.title != "<global error>":
                        targets.setdefault(f.key, f)
                if c.ok:
                    # A green run after a repair must prove the failing tests
                    # pass now, not that they stopped running.
                    if last_commit is not None:
                        before = dr.Classification("code", "", 1, list(targets.values()))
                        ok, problems = dr.verify_repaired(before, g.report)
                        if not ok:
                            note = f"{previous[-1] if previous else 'repair'} -> re-run rejected: {'; '.join(problems)[:400]}"
                            log(f"[deployer:gate] {note}")
                            previous.append(note)
                            outcome.repairs.append({"attempt": repairs, "accepted": False, "note": note})
                            _git(self.wt, "reset", "--hard", last_commit["before"])
                            last_commit = None
                            g = GateRun(g.label, 1, dr.Classification(
                                "code", "the repair made the failing tests disappear", 1,
                                list(targets.values())), g.art_dir, "", g.seconds, g.report)
                            rerun_needed = False
                            continue
                    outcome.ok = True
                    outcome.sha = rev(self.wt)
                    outcome.image_id = g.image_id
                    outcome.repaired = repairs > 0 and outcome.sha != base
                    outcome.status_reason = c.reason
                    return outcome
                sig = dr.failure_signature(c)
                outcome.signature = sig
                if c.kind == "infra" and c.rc == 6:
                    rc6_seen += 1
                    if rc6_seen > 1:
                        blip_checked = True        # it already failed twice
                        c = dr.Classification("code", "the container never became healthy, twice "
                                              "(the app's own boot is the likely cause)", 6)
                        g = GateRun(g.label, 6, c, g.art_dir, "", g.seconds, g.report)
                        outcome.classification = c
                        sig = dr.failure_signature(c)
                        outcome.signature = sig
                if c.kind == "infra" and infra_left > 0 and self._left() > 600:
                    infra_left -= 1
                    wait = int(self.cfg.get("infra_retry_wait_s", 60))
                    log(f"[deployer:gate] infrastructure failure ({c.reason}); retrying in {wait}s")
                    time.sleep(wait)
                    continue
                if c.kind != "code":
                    outcome.status_reason = f"{c.kind}: {c.reason} (no code repair for this class)"
                    self.state.record_red(c, sig, attempts=0, exhausted=False, cfg=self.cfg)
                    return outcome
                # A build or boot failure has no test report to confirm with:
                # re-run once before an LLM sees it (a registry or disk blip).
                if not c.failures and not blip_checked and repairs == 0:
                    blip_checked = True
                    log(f"[deployer:gate] {c.reason}; re-running once before repair")
                    continue
                if not (self.cfg.get("enabled", True) and self.allow_repair):
                    why = "repair is disabled" if self.allow_repair else "repair is off for this run"
                    outcome.status_reason = f"code: {c.reason}; {why}"
                    self.state.record_red(c, sig, attempts=0, exhausted=False, cfg=self.cfg)
                    return outcome
                if not self.isolated:
                    outcome.status_reason = (f"code: {c.reason}; no clean checkout, so no repair "
                                             f"(it would run in the shared live tree)")
                    self.state.record_red(c, sig, attempts=0, exhausted=False, cfg=self.cfg)
                    return outcome
                may, why = self.state.may_repair(sig, self.cfg)
                if not may:
                    outcome.status_reason = f"code: {c.reason}; {why}"
                    self.state.record_red(c, sig, attempts=0, exhausted=False, cfg=self.cfg)
                    return outcome
                # The re-test must fit after the session: reserve it first.
                retest_s = min(self.test_timeout_s, max(300, int(1.5 * max(g.seconds, 60)) + 120))
                session_s = min(int(self.cfg.get("attempt_timeout_s", 2400)), int(self._left()) - retest_s)
                if repairs >= int(self.cfg.get("max_attempts", 3)) or session_s < 300:
                    outcome.status_reason = (f"code: {c.reason}; {repairs} repair attempt(s) did not get "
                                             f"the gate green" + ("" if session_s >= 300 else " (time budget spent)"))
                    self.state.record_red(c, sig, attempts=repairs, exhausted=repairs > 0, cfg=self.cfg)
                    return outcome
                repairs += 1
                log(f"[deployer:gate] {c.reason} (signature {sig}); repair attempt {repairs} "
                    f"(session budget {session_s}s, re-test reserved {retest_s}s)")
                res = self.repair(g, repairs, previous, session_s)
                previous.append(res["note"])
                outcome.repairs.append({"attempt": repairs, "accepted": res["made"], "note": res["note"]})
                log(f"[deployer:gate] {res['note']}")
                if res["kind"] == "pool":
                    outcome.deferred = True
                    outcome.status_reason = f"code: {c.reason}; repair deferred, the claude-pool has no capacity"
                    self.state.record_deferred(c, sig, wait_s=int(self.cfg.get("pool_retry_s", 900)),
                                               why="claude-pool had no capacity")
                    return outcome
                if res["made"]:
                    last_commit = res
                # A rejected or empty attempt left the code as it was: the next
                # attempt starts from the same failure without re-testing.
                rerun_needed = res["made"]
        finally:
            self.state.save()
            prune_artifacts(self.site_id)
