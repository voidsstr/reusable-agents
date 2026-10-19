"""Deploy-gate repair: classification, report parsing, the guard on what a
repair may ship, per-site backoff, config layering, and landing repair
commits on the live branch (agents/deployer/gate_runner.land)."""
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from framework.core import deploy_repair as dr
from framework.core.implementer_scope import ScopePolicy

_REPO = Path(__file__).resolve().parents[2]


def _load_gate_runner():
    name = "gate_runner_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _REPO / "agents" / "deployer" / "gate_runner.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod   # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Playwright JSON report
# ---------------------------------------------------------------------------

def _report(tmp_path, *, failing=True, flaky=False, global_error=None):
    def test(status, msg=""):
        res = {"status": "passed" if status != "unexpected" else "failed", "retry": 0,
               "attachments": [{"name": "screenshot", "path": "/x/shot.png", "contentType": "image/png"}]}
        if msg:
            res["errors"] = [{"message": "\x1b[31m" + msg + "\x1b[39m"}]
        return {"projectName": "chromium", "status": status, "results": [res]}

    specs = [{"title": "home has one h1", "file": "ui/all-pages.spec.ts", "line": 12,
              "tests": [test("expected")]}]
    if failing:
        specs.append({"title": "/pc-gaming shows no CSS as text", "file": "ui/all-pages.spec.ts",
                      "line": 40, "tests": [test("unexpected", "Expected: []\nReceived: ['.ssr-x{a:b;}']")]})
    if flaky:
        specs.append({"title": "deals loads", "file": "ui/deals.spec.ts", "line": 3,
                      "tests": [test("flaky")]})
    data = {
        "suites": [{"title": "ui/all-pages.spec.ts", "file": "ui/all-pages.spec.ts", "specs": [],
                    "suites": [{"title": "all pages", "file": "ui/all-pages.spec.ts", "specs": specs}]}],
        "errors": [{"message": global_error}] if global_error else [],
        "stats": {"expected": 1, "unexpected": 1 if failing else 0, "flaky": 1 if flaky else 0,
                  "skipped": 0},
    }
    p = tmp_path / "report.json"
    p.write_text(json.dumps(data))
    return p


def test_parse_report_lists_failures_with_describe_path_and_clean_error(tmp_path):
    rep = dr.parse_report(_report(tmp_path))
    assert rep.unexpected == 1 and rep.expected == 1
    (f,) = rep.failures
    assert f.title == "all pages › /pc-gaming shows no CSS as text"
    assert f.file == "ui/all-pages.spec.ts" and f.line == 40
    assert "\x1b" not in f.error and "Received" in f.error
    assert f.attachments == ["/x/shot.png"]


def test_parse_report_missing_or_garbage_is_none(tmp_path):
    assert dr.parse_report(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert dr.parse_report(bad) is None


def test_parse_report_keeps_global_errors_as_failures(tmp_path):
    rep = dr.parse_report(_report(tmp_path, failing=False, global_error="Error: Cannot find module 'x'"))
    assert rep.global_errors and rep.failures[0].title == "<global error>"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_green_and_flaky(tmp_path):
    assert dr.classify(0, "", dr.parse_report(_report(tmp_path, failing=False))).kind == "pass"
    flaky = dr.classify(0, "", dr.parse_report(_report(tmp_path, failing=False, flaky=True)))
    assert flaky.kind == "flaky" and flaky.ok


def test_test_failures_are_code(tmp_path):
    c = dr.classify(1, "1 failed", dr.parse_report(_report(tmp_path)))
    assert c.kind == "code" and len(c.failures) == 1 and not c.ok


@pytest.mark.parametrize("rc", [2, 4, 6, 7, 79, 124, 143])
def test_suite_infrastructure_exit_codes_never_reach_an_llm(rc):
    assert dr.classify(rc, "", None).kind == "infra"


def test_no_tests_is_config():
    assert dr.classify(78, "No tests found", None).kind == "config"


def test_build_and_boot_failures_are_code_unless_the_machine_failed():
    assert dr.classify(3, "error TS2304: Cannot find name 'x'", None).kind == "code"
    assert dr.classify(5, "TypeError: undefined is not a function", None).kind == "code"
    assert dr.classify(3, "failed to solve: no space left on device", None).kind == "infra"
    assert dr.classify(5, "Error response from daemon: Conflict. The container name", None).kind == "infra"


def test_rc1_without_failing_tests_is_infra_not_code():
    assert dr.classify(1, "Error response from daemon: No such container: x", None).kind == "infra"
    assert dr.classify(1, "something odd", None).kind == "infra"


def test_signature_is_stable_across_runs_and_order(tmp_path):
    a = dr.Classification("code", "x", 1, [dr.TestFailure("t1", "a.ts"), dr.TestFailure("t2", "b.ts")])
    b = dr.Classification("code", "y", 1, [dr.TestFailure("t2", "b.ts"), dr.TestFailure("t1", "a.ts")])
    assert dr.failure_signature(a) == dr.failure_signature(b)
    assert dr.failure_signature(dr.Classification("infra", "x", 4)) != dr.failure_signature(a)


# ---------------------------------------------------------------------------
# What a repair may ship
# ---------------------------------------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "site"
    r.mkdir()
    _git(r, "init", "-q", "-b", "master")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "src").mkdir()
    (r / "src" / "page.ts").write_text("export const h1 = 'Hub';\n")
    (r / "tests" / "ui").mkdir(parents=True)
    (r / "tests" / "ui" / "hub.spec.ts").write_text(
        "test('hub', async () => {\n  expect(a).toBe(1);\n  expect(b).toBe(2);\n});\n")
    (r / "tests" / "run-local-suite.sh").write_text("#!/bin/bash\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _commit(repo, path, text, msg):
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def test_app_fix_is_accepted(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "src/page.ts", "export const h1 = 'PC Gaming';\n", "deploy-gate repair: h1")
    ok, problems, tests = dr.check_repair(str(repo), base, head)
    assert ok, problems
    assert tests == []


def test_no_commit_is_rejected(repo):
    base = _git(repo, "rev-parse", "HEAD")
    ok, problems, _ = dr.check_repair(str(repo), base, base)
    assert not ok and "no commit" in problems[0]


def test_adding_a_skip_is_rejected(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "tests/ui/hub.spec.ts",
                   "test('hub', async () => {\n  test.skip(true, 'flaky');\n  expect(a).toBe(1);\n"
                   "  expect(b).toBe(2);\n});\n",
                   "fix\n\nTEST-CHANGE-JUSTIFICATION: flaky")
    ok, problems, tests = dr.check_repair(str(repo), base, head)
    assert not ok and any("skip" in p for p in problems)
    assert tests == ["tests/ui/hub.spec.ts"]


def test_fewer_expects_is_rejected_even_with_justification(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "tests/ui/hub.spec.ts",
                   "test('hub', async () => {\n  expect(a).toBe(1);\n});\n",
                   "fix\n\nTEST-CHANGE-JUSTIFICATION: b was wrong")
    ok, problems, _ = dr.check_repair(str(repo), base, head)
    assert not ok and any("fewer expect" in p for p in problems)


def test_test_edit_needs_a_justification_line(repo):
    base = _git(repo, "rev-parse", "HEAD")
    new = ("test('hub', async () => {\n  expect(a).toBe(1);\n  expect(b).toBe(3);\n});\n")
    head = _commit(repo, "tests/ui/hub.spec.ts", new, "fix the test")
    ok, problems, _ = dr.check_repair(str(repo), base, head)
    assert not ok and any("JUSTIFICATION" in p for p in problems)
    _git(repo, "reset", "-q", "--hard", base)
    head = _commit(repo, "tests/ui/hub.spec.ts", new,
                   "fix the test\n\nTEST-CHANGE-JUSTIFICATION: b is 3 since the redesign")
    ok, problems, tests = dr.check_repair(str(repo), base, head)
    assert ok, problems
    assert tests == ["tests/ui/hub.spec.ts"]


def test_test_edits_can_be_switched_off(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "tests/ui/hub.spec.ts",
                   "test('hub', async () => {\n  expect(a).toBe(1);\n  expect(b).toBe(3);\n});\n",
                   "fix\n\nTEST-CHANGE-JUSTIFICATION: b is 3")
    ok, problems, _ = dr.check_repair(str(repo), base, head, cfg={**dr.DEFAULTS, "allow_test_edits": False})
    assert not ok and any("not allowed" in p for p in problems)


def test_deleting_a_test_file_or_touching_the_gate_is_rejected(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "tests/ui/hub.spec.ts")
    _git(repo, "commit", "-q", "-m", "rm\n\nTEST-CHANGE-JUSTIFICATION: obsolete")
    head = _git(repo, "rev-parse", "HEAD")
    ok, problems, _ = dr.check_repair(str(repo), base, head)
    assert not ok and any("delete a test file" in p for p in problems)
    _git(repo, "reset", "-q", "--hard", base)
    head = _commit(repo, "tests/run-local-suite.sh", "#!/bin/bash\nexit 0\n",
                   "x\n\nTEST-CHANGE-JUSTIFICATION: y")
    ok, problems, _ = dr.check_repair(str(repo), base, head)
    assert not ok and any("machinery" in p for p in problems)


def test_scope_policy_is_enforced(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "mobile/app.ts", "x\n", "deploy-gate repair: mobile")
    scope = ScopePolicy.from_site_config({"implementer": {"excluded_paths": ["mobile/**"]}})
    ok, problems, _ = dr.check_repair(str(repo), base, head, scope=scope)
    assert not ok and any("scope" in p for p in problems)


# ---------------------------------------------------------------------------
# Gate state: backoff + alerts
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_GATE_STATE_DIR", str(tmp_path / "gate-state"))
    return tmp_path / "gate-state"


def test_backoff_applies_only_to_the_same_signature(state_dir):
    st = dr.GateState.load("site-a")
    c = dr.Classification("code", "1 test failed", 1, [dr.TestFailure("t", "a.ts")])
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert st.may_repair("sig1", now)[0]
    st.record_red(c, "sig1", attempts=3, exhausted=True, cfg=dr.DEFAULTS, now=now)
    assert st.cycles == 1
    ok, why = st.may_repair("sig1", now + timedelta(minutes=5))
    assert not ok and "backing off" in why
    assert st.may_repair("sig1", now + timedelta(seconds=dr.DEFAULTS["backoff_s"][1] + 1))[0]
    assert st.may_repair("sig2", now + timedelta(minutes=5))[0]
    st.save()
    again = dr.GateState.load("site-a")
    assert again.signature == "sig1" and again.cycles == 1 and again.status == "red"


def test_a_new_signature_resets_cycles_and_green_clears(state_dir):
    st = dr.GateState.load("site-b")
    c = dr.Classification("code", "x", 1)
    st.record_red(c, "s1", attempts=3, exhausted=True, cfg=dr.DEFAULTS)
    st.record_red(c, "s1", attempts=3, exhausted=True, cfg=dr.DEFAULTS)
    assert st.cycles == 2
    st.record_red(c, "s2", attempts=1, exhausted=False, cfg=dr.DEFAULTS)
    assert st.cycles == 0 and st.signature == "s2"
    st.record_green("abc123", repaired=True)
    assert st.status == "green" and st.cycles == 0 and not st.next_repair_at
    assert st.last_green_sha == "abc123"


def test_alert_once_per_signature_then_after_realert(state_dir):
    st = dr.GateState.load("site-c")
    c = dr.Classification("code", "x", 1)
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    st.record_red(c, "s1", attempts=1, exhausted=False, cfg=dr.DEFAULTS, now=now)
    assert not st.should_alert("s1", dr.DEFAULTS, now)       # no exhausted cycle yet
    st.record_red(c, "s1", attempts=3, exhausted=True, cfg=dr.DEFAULTS, now=now)
    assert st.should_alert("s1", dr.DEFAULTS, now)
    st.record_alert("s1", now)
    assert not st.should_alert("s1", dr.DEFAULTS, now + timedelta(hours=2))
    assert st.should_alert("s1", dr.DEFAULTS, now + timedelta(hours=25))


# ---------------------------------------------------------------------------
# Config layering
# ---------------------------------------------------------------------------

def test_config_layers_and_kill_switch(storage, monkeypatch):
    assert dr.load_config("x")["max_attempts"] == dr.DEFAULTS["max_attempts"]
    assert dr.load_config("x", {"max_attempts": 5})["max_attempts"] == 5
    storage.write_json(dr.CONFIG_KEY, {"max_attempts": 2, "by_site": {"x": {"max_attempts": 1}}})
    assert dr.load_config("x", {"max_attempts": 5}, storage=storage)["max_attempts"] == 1
    assert dr.load_config("y", {"max_attempts": 5}, storage=storage)["max_attempts"] == 2
    storage.write_json(dr.CONFIG_KEY, {"enabled": False})
    assert dr.load_config("x", storage=storage)["enabled"] is False
    monkeypatch.setenv("DEPLOY_REPAIR_DISABLED", "1")
    assert dr.load_config("x")["enabled"] is False


def test_broken_storage_means_defaults():
    class Broken:
        def read_json(self, key):
            raise RuntimeError("blob archived")
    assert dr.load_config("x", storage=Broken())["enabled"] is True


# ---------------------------------------------------------------------------
# Landing repair commits on the live branch
# ---------------------------------------------------------------------------

def _worktree(repo, path):
    _git(repo, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path


def test_land_fast_forwards_when_the_branch_did_not_move(repo, tmp_path):
    gr = _load_gate_runner()
    base = _git(repo, "rev-parse", "HEAD")
    wt = _worktree(repo, tmp_path / "wt")
    head = _commit(wt, "src/page.ts", "export const h1 = 'fixed';\n", "deploy-gate repair: h1")
    ok, detail = gr.land(str(repo), "master", base, head)
    assert ok and "fast-forwarded" in detail
    assert _git(repo, "rev-parse", "HEAD") == head


def test_land_cherry_picks_onto_a_moved_branch_and_keeps_others_dirty_work(repo, tmp_path):
    gr = _load_gate_runner()
    base = _git(repo, "rev-parse", "HEAD")
    wt = _worktree(repo, tmp_path / "wt")
    head = _commit(wt, "src/page.ts", "export const h1 = 'fixed';\n", "deploy-gate repair: h1")
    other = _commit(repo, "src/other.ts", "x\n", "another agent's commit")
    (repo / "notes.txt").write_text("someone's uncommitted work\n")
    ok, detail = gr.land(str(repo), "master", base, head)
    assert ok and "cherry-picked" in detail, detail
    assert (repo / "src" / "page.ts").read_text() == "export const h1 = 'fixed';\n"
    assert _git(repo, "rev-parse", "HEAD~1") == other
    assert (repo / "notes.txt").exists()


def test_land_conflict_aborts_cleanly(repo, tmp_path):
    gr = _load_gate_runner()
    base = _git(repo, "rev-parse", "HEAD")
    wt = _worktree(repo, tmp_path / "wt")
    head = _commit(wt, "src/page.ts", "export const h1 = 'fixed';\n", "deploy-gate repair")
    _commit(repo, "src/page.ts", "export const h1 = 'conflicting';\n", "someone else edited it")
    before = _git(repo, "rev-parse", "HEAD")
    ok, detail = gr.land(str(repo), "master", base, head)
    assert not ok
    assert _git(repo, "rev-parse", "HEAD") == before
    assert not (repo / ".git" / "CHERRY_PICK_HEAD").exists()


def test_land_refuses_the_wrong_branch(repo, tmp_path):
    gr = _load_gate_runner()
    base = _git(repo, "rev-parse", "HEAD")
    ok, detail = gr.land(str(repo), "main", base, "deadbeef")
    assert not ok and "not main" in detail


# ---------------------------------------------------------------------------
# Streaming + timeouts
# ---------------------------------------------------------------------------

def test_run_streamed_captures_output_and_kills_on_timeout(tmp_path):
    gr = _load_gate_runner()
    rc, out = gr.run_streamed("t", "echo '[suite] hello'; echo second", cwd=str(tmp_path),
                              env={"PATH": "/usr/bin:/bin"}, timeout_s=30,
                              log_path=tmp_path / "a.log", heartbeat_s=1)
    assert rc == 0 and "hello" in out and "second" in (tmp_path / "a.log").read_text()
    rc, _ = gr.run_streamed("t", "sleep 30 & sleep 30; wait", cwd=str(tmp_path),
                            env={"PATH": "/usr/bin:/bin"}, timeout_s=3,
                            log_path=tmp_path / "b.log", heartbeat_s=1)
    assert rc == 124


def test_scrubbed_env_drops_credentials(monkeypatch):
    gr = _load_gate_runner()
    for k in ("DATABASE_URL", "DATABASE_URL_SPEC", "AZURE_STORAGE_CONNECTION_STRING",
              "FRAMEWORK_API_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JWT_SECRET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("HOME", "/home/t")
    env = gr.scrubbed_env({"GATE_SOURCE_REPO": "/r"})
    assert env["HOME"] == "/home/t" and env["GATE_SOURCE_REPO"] == "/r"
    for k in ("DATABASE_URL", "DATABASE_URL_SPEC", "AZURE_STORAGE_CONNECTION_STRING",
              "FRAMEWORK_API_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JWT_SECRET"):
        assert k not in env


def test_reporter_args_are_stripped_from_the_site_command():
    gr = _load_gate_runner()
    assert gr._REPORTER_ARG.sub("", " bash run-local-suite.sh --reporter=line --grep @smoke").strip() \
        == "bash run-local-suite.sh --grep @smoke"
    assert gr._REPORTER_ARG.sub("", " bash run-local-suite.sh --reporter line").strip() \
        == "bash run-local-suite.sh"
