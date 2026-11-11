"""Deploy-gate repair: classification, report parsing, the guards on what a
repair may ship, per-site state, config layering, landing, and the gate loop
itself (agents/deployer/gate_runner.GateRunner) with stubbed test runs and
repair sessions."""
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

def _t(status, msg="", expected="passed"):
    res = {"status": {"unexpected": "failed", "skipped": "skipped"}.get(status, "passed"), "retry": 0,
           "attachments": [{"name": "screenshot", "path": "/x/shot.png", "contentType": "image/png"}]}
    if msg:
        res["errors"] = [{"message": "\x1b[31m" + msg + "\x1b[39m"}]
    return {"projectName": "chromium", "status": status, "expectedStatus": expected, "results": [res]}


def _report_data(specs, *, errors=None):
    counts = {"expected": 0, "unexpected": 0, "flaky": 0, "skipped": 0}
    for sp in specs:
        for t in sp["tests"]:
            counts[t["status"]] += 1
    return {"suites": [{"title": "ui/all-pages.spec.ts", "file": "ui/all-pages.spec.ts", "specs": [],
                        "suites": [{"title": "all pages", "file": "ui/all-pages.spec.ts", "specs": specs}]}],
            "errors": errors or [], "stats": counts}


def _spec(title, status, *, line=1, file="ui/all-pages.spec.ts", msg="", expected="passed"):
    return {"title": title, "file": file, "line": line, "tests": [_t(status, msg, expected)]}


def _write(tmp_path, data, name="report.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_parse_report_lists_failures_with_describe_path_and_clean_error(tmp_path):
    rep = dr.parse_report(_write(tmp_path, _report_data([
        _spec("home has one h1", "expected"),
        _spec("/pc-gaming shows no CSS as text", "unexpected", line=40,
              msg="Expected: []\nReceived: ['.ssr-x{a:b;}']")])))
    assert rep.unexpected == 1 and rep.expected == 1 and rep.did_not_run == 0
    (f,) = rep.failures
    assert f.title == "all pages › /pc-gaming shows no CSS as text"
    assert f.file == "ui/all-pages.spec.ts" and f.line == 40
    assert "\x1b" not in f.error and "Received" in f.error
    assert f.attachments == ["/x/shot.png"]
    assert len(rep.passed_keys) == 1 and len(rep.all_keys) == 2


def test_parse_report_counts_tests_that_did_not_run_but_not_intentional_skips(tmp_path):
    rep = dr.parse_report(_write(tmp_path, _report_data([
        _spec("beforeAll victim", "unexpected", msg="hook timed out"),
        _spec("never ran", "skipped", expected="passed"),
        _spec("skipped on purpose", "skipped", expected="skipped")])))
    assert rep.did_not_run == 1
    dnr = [f for f in rep.failures if f.did_not_run]
    assert [f.title for f in dnr] == ["all pages › never ran"]
    assert rep.failing_files == ["ui/all-pages.spec.ts"]


def test_parse_report_missing_or_garbage_is_none(tmp_path):
    assert dr.parse_report(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert dr.parse_report(bad) is None


def test_parse_report_keeps_global_errors_as_failures(tmp_path):
    rep = dr.parse_report(_write(tmp_path, _report_data([], errors=[{"message": "Error: Cannot find module 'x'"}])))
    assert rep.global_errors and rep.failures[0].title == "<global error>"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _rep(tmp_path, specs, name="r.json"):
    return dr.parse_report(_write(tmp_path, _report_data(specs), name))


def test_green_and_flaky(tmp_path):
    assert dr.classify(0, "", _rep(tmp_path, [_spec("a", "expected")])).kind == "pass"
    flaky = dr.classify(0, "", _rep(tmp_path, [_spec("a", "flaky")], "f.json"))
    assert flaky.kind == "flaky" and flaky.ok


def test_test_failures_are_code(tmp_path):
    c = dr.classify(1, "1 failed", _rep(tmp_path, [_spec("a", "unexpected", msg="boom")]))
    assert c.kind == "code" and len(c.failures) == 1 and not c.ok


def test_did_not_run_is_code_even_with_rc0(tmp_path):
    c = dr.classify(0, "", _rep(tmp_path, [_spec("a", "expected"), _spec("b", "skipped")]))
    assert c.kind == "code" and "did not run" in c.reason


@pytest.mark.parametrize("rc", [2, 4, 6, 7, 79, 124, 143])
def test_suite_infrastructure_exit_codes_never_reach_an_llm(rc):
    assert dr.classify(rc, "", None).kind == "infra"


def test_no_tests_is_config_whatever_the_exit_code(tmp_path):
    assert dr.classify(78, "No tests found", None).kind == "config"
    assert dr.classify(1, "Error: No tests found", None).kind == "config"
    empty = _rep(tmp_path, [])
    assert dr.classify(1, "Error: No tests found.\nMake sure that arguments are regular expressions",
                       empty).kind == "config"


def test_build_and_boot_failures_are_code_unless_the_machine_failed():
    assert dr.classify(3, "error TS2304: Cannot find name 'x'", None).kind == "code"
    assert dr.classify(5, "TypeError: undefined is not a function", None).kind == "code"
    assert dr.classify(3, "failed to solve: no space left on device", None).kind == "infra"
    assert dr.classify(5, "Error response from daemon: Conflict. The container name", None).kind == "infra"
    assert dr.classify(3, "npm error code ETIMEDOUT\nnpm error network request failed", None).kind == "infra"
    assert dr.classify(3, "npm ERR! network request to registry.npmjs.org failed", None).kind == "infra"


def test_browser_that_cannot_launch_is_infra(tmp_path):
    rep = _rep(tmp_path, [_spec("a", "unexpected", msg="browserType.launch: Executable doesn't exist at /x/chrome"),
                          _spec("b", "unexpected", msg="browserType.launch: Executable doesn't exist at /x/chrome")])
    assert dr.classify(1, "", rep).kind == "infra"


def test_rc1_without_failing_tests_is_infra_not_code():
    assert dr.classify(1, "Error response from daemon: No such container: x", None).kind == "infra"
    assert dr.classify(1, "something odd", None).kind == "infra"


def test_signature_is_stable_across_runs_and_order():
    a = dr.Classification("code", "x", 1, [dr.TestFailure("t1", "a.ts"), dr.TestFailure("t2", "b.ts")])
    b = dr.Classification("code", "y", 1, [dr.TestFailure("t2", "b.ts"), dr.TestFailure("t1", "a.ts")])
    assert dr.failure_signature(a) == dr.failure_signature(b)
    assert dr.failure_signature(dr.Classification("infra", "x", 4)) != dr.failure_signature(a)


# ---------------------------------------------------------------------------
# verify_repaired: failing tests must PASS, not disappear
# ---------------------------------------------------------------------------

def test_verify_repaired_accepts_real_fixes_and_rejects_vanished_tests(tmp_path):
    before = dr.classify(1, "", _rep(tmp_path, [_spec("a", "unexpected", msg="x"), _spec("b", "skipped"),
                                                _spec("c", "expected")], "before.json"))
    fixed = _rep(tmp_path, [_spec("a", "expected"), _spec("b", "flaky"), _spec("c", "expected")], "fixed.json")
    assert dr.verify_repaired(before, fixed) == (True, [])
    gone = _rep(tmp_path, [_spec("c", "expected")], "gone.json")
    ok, problems = dr.verify_repaired(before, gone)
    assert not ok and "did not pass" in problems[0]
    still = _rep(tmp_path, [_spec("a", "expected"), _spec("b", "skipped"), _spec("c", "expected")], "still.json")
    ok, problems = dr.verify_repaired(before, still)
    assert not ok and any("not-run" in p for p in problems)
    assert dr.verify_repaired(before, None)[0] is False


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


def test_head_that_is_not_a_descendant_is_rejected(repo):
    older = _git(repo, "rev-parse", "HEAD")
    base = _commit(repo, "src/page.ts", "export const h1 = 'v2';\n", "the commit under test")
    ok, problems, _ = dr.check_repair(str(repo), base, older)
    assert not ok and "not a descendant" in problems[0]


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


@pytest.mark.parametrize("path", [
    "tests/run-local-suite.sh", "tests/playwright.config.ts", "tests/pw.config.ts",
    "tests/ui/pages.manifest.ts", "db/migrations/20261019_x.sql", "docker-compose.test.yml",
])
def test_gate_machinery_and_migrations_are_protected(repo, path):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, path, "changed\n", "x\n\nTEST-CHANGE-JUSTIFICATION: y")
    ok, problems, _ = dr.check_repair(str(repo), base, head)
    assert not ok and any("machinery" in p for p in problems), problems


def test_deleting_a_test_file_is_rejected(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "tests/ui/hub.spec.ts")
    _git(repo, "commit", "-q", "-m", "rm\n\nTEST-CHANGE-JUSTIFICATION: obsolete")
    ok, problems, _ = dr.check_repair(str(repo), base, _git(repo, "rev-parse", "HEAD"))
    assert not ok and any("delete a test file" in p for p in problems)


def test_scope_policy_is_enforced(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "mobile/app.ts", "x\n", "deploy-gate repair: mobile")
    scope = ScopePolicy.from_site_config({"implementer": {"excluded_paths": ["mobile/**"]}})
    ok, problems, _ = dr.check_repair(str(repo), base, head, scope=scope)
    assert not ok and any("scope" in p for p in problems)


# ---------------------------------------------------------------------------
# Gate state: per-signature backoff, daily cap, deferral, alerts
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_GATE_STATE_DIR", str(tmp_path / "gate-state"))
    return tmp_path / "gate-state"


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
CODE = dr.Classification("code", "1 test failed", 1, [dr.TestFailure("t", "a.ts")])
INFRA = dr.Classification("infra", "container did not start", 4)


def test_backoff_is_per_signature_and_survives_other_reds(state_dir):
    st = dr.GateState.load("site-a")
    assert st.may_repair("sig1", now=NOW)[0]
    st.record_red(CODE, "sig1", attempts=3, exhausted=True, cfg=dr.DEFAULTS, now=NOW)
    assert st.entry("sig1")["cycles"] == 1
    ok, why = st.may_repair("sig1", now=NOW + timedelta(minutes=5))
    assert not ok and "backing off" in why
    # An infra red in between must not reset the code failure's backoff.
    st.record_red(INFRA, "infra1", attempts=0, exhausted=False, cfg=dr.DEFAULTS, now=NOW + timedelta(minutes=6))
    assert not st.may_repair("sig1", now=NOW + timedelta(minutes=7))[0]
    assert st.entry("sig1")["cycles"] == 1
    assert st.may_repair("sig1", now=NOW + timedelta(seconds=dr.DEFAULTS["backoff_s"][1] + 1))[0]
    assert st.may_repair("sig2", now=NOW + timedelta(minutes=5))[0]
    st.save()
    again = dr.GateState.load("site-a")
    assert again.signature == "infra1" and again.entry("sig1")["cycles"] == 1 and again.status == "red"


def test_daily_repair_cap_spans_signatures(state_dir):
    st = dr.GateState.load("site-cap")
    for i in range(3):
        st.record_repair_session(now=NOW + timedelta(minutes=i))
    cfg = {**dr.DEFAULTS, "max_repairs_per_day": 3}
    ok, why = st.may_repair("brand-new-signature", cfg, now=NOW + timedelta(minutes=10))
    assert not ok and "24 h" in why
    assert st.may_repair("brand-new-signature", cfg, now=NOW + timedelta(hours=25))[0]


def test_deferral_does_not_climb_the_backoff(state_dir):
    st = dr.GateState.load("site-pool")
    st.record_deferred(CODE, "sig", wait_s=900, why="claude-pool had no capacity", now=NOW)
    e = st.entry("sig")
    assert e["cycles"] == 0 and st.status == "red"
    assert not st.may_repair("sig", now=NOW + timedelta(minutes=10))[0]
    assert st.may_repair("sig", now=NOW + timedelta(minutes=16))[0]
    assert not st.should_alert("sig", dr.DEFAULTS, now=NOW + timedelta(minutes=16))


def test_green_clears_failures(state_dir):
    st = dr.GateState.load("site-b")
    st.record_red(CODE, "s1", attempts=3, exhausted=True, cfg=dr.DEFAULTS)
    st.record_green("abc123", repaired=True)
    assert st.status == "green" and st.failures == {} and st.last_green_sha == "abc123"


def test_alert_rules(state_dir):
    st = dr.GateState.load("site-c")
    st.record_red(CODE, "s1", attempts=1, exhausted=False, cfg=dr.DEFAULTS, now=NOW)
    assert not st.should_alert("s1", dr.DEFAULTS, NOW)          # no exhausted cycle yet
    st.record_red(CODE, "s1", attempts=3, exhausted=True, cfg=dr.DEFAULTS, now=NOW)
    assert st.should_alert("s1", dr.DEFAULTS, NOW)
    st.record_alert("s1", NOW)
    assert not st.should_alert("s1", dr.DEFAULTS, NOW + timedelta(hours=2))
    assert st.should_alert("s1", dr.DEFAULTS, NOW + timedelta(hours=25))
    # A non-code red alerts once the site has been red for alert_red_for_s.
    st.record_red(INFRA, "i1", attempts=0, exhausted=False, cfg=dr.DEFAULTS, now=NOW)
    assert not st.should_alert("i1", dr.DEFAULTS, NOW + timedelta(minutes=30))
    assert st.should_alert("i1", dr.DEFAULTS, NOW + timedelta(hours=3))
    # A green repair that cannot land alerts at once.
    st.record_red(dr.Classification("landing", "x", 1), "landing-s1", attempts=0, exhausted=True,
                  cfg=dr.DEFAULTS, now=NOW)
    assert st.should_alert("landing-s1", dr.DEFAULTS, NOW)


def test_old_state_files_load(state_dir):
    (state_dir).mkdir(parents=True, exist_ok=True)
    (state_dir / "site-old.json").write_text(json.dumps({
        "site": "site-old", "status": "red", "signature": "old1", "cycles": 2,
        "next_repair_at": "2026-10-19T14:00:00+00:00", "red_since": "2026-10-19T10:00:00+00:00"}))
    st = dr.GateState.load("site-old")
    assert st.entry("old1")["cycles"] == 2
    assert not st.may_repair("old1", now=NOW)[0]


# ---------------------------------------------------------------------------
# Config layering
# ---------------------------------------------------------------------------

def test_config_layers_notes_and_kill_switch(storage, monkeypatch):
    assert dr.load_config("x")["max_attempts"] == dr.DEFAULTS["max_attempts"]
    assert dr.load_config("x", {"max_attempts": 5})["max_attempts"] == 5
    storage.write_json(dr.CONFIG_KEY, {"max_attempts": 2, "_note": "why", "by_site": {"x": {"max_attempts": 1}}})
    cfg = dr.load_config("x", {"max_attempts": 5}, storage=storage)
    assert cfg["max_attempts"] == 1 and "_note" not in cfg
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


def test_land_refuses_the_wrong_branch_and_non_descendants(repo, tmp_path):
    gr = _load_gate_runner()
    base = _git(repo, "rev-parse", "HEAD")
    wt = _worktree(repo, tmp_path / "wt")
    head = _commit(wt, "src/page.ts", "x\n", "repair")
    ok, detail = gr.land(str(repo), "main", base, head)
    assert not ok and "not main" in detail
    ok, detail = gr.land(str(repo), "master", head, base)
    assert not ok and "does not descend" in detail


def test_reverted_batch_files_detects_a_repair_that_undid_the_batch(repo, tmp_path):
    gr = _load_gate_runner()
    batch_base = _git(repo, "rev-parse", "HEAD")
    gate_base = _commit(repo, "src/page.ts", "export const h1 = 'batch change';\n", "the batch")
    wt = _worktree(repo, tmp_path / "wt")
    head = _commit(wt, "src/page.ts", "export const h1 = 'Hub';\n", "repair: undo the batch")
    assert gr.reverted_batch_files(str(repo), batch_base, gate_base, head) == ["src/page.ts"]
    head2 = _commit(wt, "src/page.ts", "export const h1 = 'fixed differently';\n", "repair 2")
    assert gr.reverted_batch_files(str(repo), batch_base, gate_base, head2) == []


# ---------------------------------------------------------------------------
# Streaming, env scrubbing, command shaping
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


def test_scrubbed_env_drops_credentials_and_points_playwright_at_the_real_cache(monkeypatch, tmp_path):
    gr = _load_gate_runner()
    secrets = ("DATABASE_URL", "DATABASE_URL_SPEC", "AZURE_STORAGE_CONNECTION_STRING", "AZURE_OPENAI_KEY",
               "AWS_SECRET_ACCESS_KEY", "FRAMEWORK_API_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
               "CLAUDE_CODE_OAUTH_TOKEN", "JWT_SECRET", "EBAY_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET")
    for k in secrets:
        monkeypatch.setenv(k, "x")
    home = tmp_path / "home"
    (home / ".cache" / "ms-playwright").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    env = gr.scrubbed_env({"GATE_SOURCE_REPO": "/r"})
    assert env["HOME"] == str(home) and env["GATE_SOURCE_REPO"] == "/r" and "PATH" in env
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(home / ".cache" / "ms-playwright")
    for k in secrets:
        assert k not in env, k


def test_reporter_args_are_stripped_from_the_site_command():
    gr = _load_gate_runner()
    assert gr._REPORTER_ARG.sub("", " bash run-local-suite.sh --reporter=line --grep @smoke").strip() \
        == "bash run-local-suite.sh --grep @smoke"
    assert gr._REPORTER_ARG.sub("", " bash run-local-suite.sh --reporter line").strip() \
        == "bash run-local-suite.sh"


# ---------------------------------------------------------------------------
# The gate loop, with stubbed test runs and repair sessions
# ---------------------------------------------------------------------------

def _runner(gr, repo, tmp_path, monkeypatch, *, runs, repair=None, isolated=True, cfg=None):
    """A GateRunner whose run_gate returns the given reports in order and
    whose repair() calls `repair(runner, attempt)`."""
    monkeypatch.setenv("DEPLOY_GATE_ARTIFACTS", str(tmp_path / "art"))
    monkeypatch.setenv("DEPLOY_GATE_STATE_DIR", str(tmp_path / "gs"))
    state = dr.GateState.load("site")
    r = gr.GateRunner(site_id="site", tag="t", test_cmd="bash run-local-suite.sh", test_cwd=None,
                      worktree=str(repo), repo_root=str(repo), branch="master", scope=None,
                      cfg={**dr.DEFAULTS, "infra_retry_wait_s": 0, **(cfg or {})}, state=state,
                      run_dir=tmp_path, isolated=isolated)
    calls = {"gate": [], "repair": 0}
    queue = list(runs)

    def run_gate(label, extra=""):
        rc, specs = queue.pop(0)
        art = tmp_path / "art" / label
        art.mkdir(parents=True, exist_ok=True)
        rep = dr.parse_report(_write(art, _report_data(specs)))
        calls["gate"].append((label, extra))
        return gr.GateRun(label, rc, dr.classify(rc, "", rep), art, "sha256:img", 10, rep)

    def do_repair(g, attempt, previous, timeout_s):
        calls["repair"] += 1
        return repair(r, attempt) if repair else {"made": False, "kind": "rejected", "note": "nothing",
                                                  "before": gr.rev(str(repo)), "after": gr.rev(str(repo))}
    monkeypatch.setattr(r, "run_gate", run_gate)
    monkeypatch.setattr(r, "repair", do_repair)
    return r, calls


FAIL = (1, [_spec("a", "unexpected", msg="boom"), _spec("c", "expected")])
GREEN = (0, [_spec("a", "expected"), _spec("c", "expected")])


def _commit_repair(repo):
    def fn(runner, attempt):
        before = _git(repo, "rev-parse", "HEAD")
        after = _commit(repo, "src/page.ts", f"export const h1 = 'fix {attempt}';\n", "deploy-gate repair")
        return {"made": True, "kind": "commit", "note": f"attempt {attempt}: committed", "before": before, "after": after}
    return fn


def test_loop_green_first_try(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[GREEN])
    out = r.run()
    assert out.ok and not out.repaired and calls["repair"] == 0


def test_loop_confirm_that_passes_cleanly_is_flaky(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, (0, [_spec("a", "expected"), _spec("c", "expected")])])
    out = r.run()
    assert out.ok and out.flaky and calls["repair"] == 0
    assert "--workers=1" in calls["gate"][1][1] and "ui/all-pages.spec.ts" in calls["gate"][1][1]
    assert "--last-failed" not in calls["gate"][1][1]


@pytest.mark.parametrize("main_image_survives,shipped", [(True, "sha256:main"), (False, "sha256:confirm")])
def test_loop_flaky_ships_an_image_that_still_exists(repo, tmp_path, monkeypatch, main_image_survives, shipped):
    # 2026-10-23: the confirm run rebuilt the image, the main run's image was
    # pruned, and the deployer's `docker tag <main image>` failed after a green gate.
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, (0, [_spec("a", "expected"), _spec("c", "expected")])])
    stub = r.run_gate

    def run_gate(label, extra=""):
        g = stub(label, extra)
        return g.__class__(g.label, g.rc, g.classification, g.art_dir,
                           "sha256:confirm" if label.endswith("-confirm") else "sha256:main", g.seconds, g.report)
    monkeypatch.setattr(r, "run_gate", run_gate)
    monkeypatch.setattr(gr, "_image_exists", lambda image: main_image_survives and image == "sha256:main")
    out = r.run()
    assert out.ok and out.flaky and out.image_id == shipped


def test_loop_confirm_cannot_hide_tests_that_did_not_run(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    main = (1, [_spec("hook victim", "unexpected", msg="beforeAll timed out"), _spec("regression", "skipped")])
    confirm = (0, [_spec("hook victim", "expected")])          # the not-run test is absent
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[main, confirm], cfg={"enabled": False})
    out = r.run()
    assert not out.ok and out.classification.kind == "code"
    assert "did not run" in out.classification.reason


def test_loop_repairs_then_ships_when_the_failing_tests_pass(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, FAIL, GREEN], repair=_commit_repair(repo))
    out = r.run()
    assert out.ok and out.repaired and calls["repair"] == 1
    assert out.sha == _git(repo, "rev-parse", "HEAD")


def test_loop_rejects_a_repair_that_makes_the_failing_test_disappear(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    vanished = (0, [_spec("c", "expected")])                 # 'a' no longer runs
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, FAIL, vanished],
                       repair=_commit_repair(repo), cfg={"max_attempts": 1})
    base = _git(repo, "rev-parse", "HEAD")
    out = r.run()
    assert not out.ok and calls["repair"] == 1
    assert _git(repo, "rev-parse", "HEAD") == base            # the repair was reset
    assert any("re-run rejected" in x["note"] for x in out.repairs)


def test_loop_pool_without_capacity_is_a_deferral_not_a_spent_cycle(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    pool = lambda runner, attempt: {"made": False, "kind": "pool", "note": "no capacity",  # noqa: E731
                                    "before": "", "after": ""}
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, FAIL], repair=pool)
    out = r.run()
    assert not out.ok and out.deferred
    e = r.state.entry(out.signature)
    assert e["cycles"] == 0 and e["next_repair_at"]


def test_loop_never_repairs_without_an_isolated_checkout(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, FAIL], isolated=False)
    out = r.run()
    assert not out.ok and calls["repair"] == 0 and "no clean checkout" in out.status_reason


def test_loop_spent_attempts_climb_the_backoff(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, FAIL], cfg={"max_attempts": 2})
    out = r.run()
    assert not out.ok and calls["repair"] == 2
    assert r.state.entry(out.signature)["cycles"] == 1
    assert not r.state.may_repair(out.signature)[0]


def test_loop_infra_is_retried_then_blocks_without_repair(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[(4, []), (4, []), (4, [])])
    out = r.run()
    assert not out.ok and calls["repair"] == 0 and out.classification.kind == "infra"
    assert len(calls["gate"]) == 3


# ---------------------------------------------------------------------------
# 2026-11-11: a repair followed by a flaky confirm must ship
# ---------------------------------------------------------------------------

def test_loop_repair_then_flaky_confirm_ships(repo, tmp_path, monkeypatch):
    # 2026-11-11 aisleprompt: the repair fixed the real failure, the re-run
    # then failed only tests that time out under parallel load, and their
    # spec files passed alone (361 passed, 0 failed). The confirm's GateRun
    # carried the MAIN report, so verify_repaired saw unexpected > 0 and
    # threw the correct repair away.
    gr = _load_gate_runner()
    rerun = (1, [_spec("a", "expected"), _spec("c", "unexpected", msg="Timeout 45000ms exceeded")])
    confirm = (0, [_spec("a", "expected"), _spec("c", "expected")])
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, FAIL, rerun, confirm],
                       repair=_commit_repair(repo))
    base = _git(repo, "rev-parse", "HEAD")
    out = r.run()
    assert out.ok and out.repaired, (out.status_reason, out.repairs)
    assert calls["repair"] == 1
    assert out.sha == _git(repo, "rev-parse", "HEAD") != base
    assert "all pages › c" in out.flaky
    assert [lab for lab, _ in calls["gate"]] == ["run-1", "run-1-confirm", "run-2", "run-2-confirm"]


def test_loop_repair_then_flaky_confirm_still_rejects_a_vanished_test(repo, tmp_path, monkeypatch):
    # The confirm path must not become a way round "failing tests must PASS":
    # 'a' failed before the repair and is in neither the re-run nor the confirm.
    gr = _load_gate_runner()
    rerun = (1, [_spec("c", "unexpected", msg="Timeout 45000ms exceeded")])
    confirm = (0, [_spec("c", "expected")])
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=[FAIL, FAIL, rerun, confirm],
                       repair=_commit_repair(repo), cfg={"max_attempts": 1})
    base = _git(repo, "rev-parse", "HEAD")
    out = r.run()
    assert not out.ok and calls["repair"] == 1
    assert _git(repo, "rev-parse", "HEAD") == base            # the repair was reset
    note = next(x["note"] for x in out.repairs if "re-run rejected" in x["note"])
    assert "re-run or its confirm run" in note and "all pages › a" in note
    assert "disappear" not in out.status_reason


def test_verify_repaired_with_a_confirm_report(tmp_path):
    before = dr.Classification("code", "", 1, [dr.TestFailure("all pages › a", "ui/all-pages.spec.ts",
                                                               project="chromium")])
    main = _rep(tmp_path, [_spec("a", "expected"), _spec("c", "unexpected", msg="t")], "main.json")
    assert not dr.verify_repaired(before, main)[0]                 # the main report alone
    clean = _rep(tmp_path, [_spec("a", "expected"), _spec("c", "expected")], "clean.json")
    assert dr.verify_repaired(before, main, clean) == (True, [])
    # The confirm run must be clean...
    dirty = _rep(tmp_path, [_spec("c", "unexpected", msg="t")], "dirty.json")
    ok, problems = dr.verify_repaired(before, main, dirty)
    assert not ok and any("confirm run still has" in p for p in problems)
    # ...and must have passed every test the re-run failed.
    other = _rep(tmp_path, [_spec("a", "expected")], "other.json")
    ok, problems = dr.verify_repaired(before, main, other)
    assert not ok and any("did not pass in the confirm run" in p for p in problems)


# ---------------------------------------------------------------------------
# Environment reds: timeouts on tests that were quick in the last green run
# ---------------------------------------------------------------------------

def _t_timed(status, msg="", *, ms=300, timeout=30000):
    t = _t(status, msg)
    t["timeout"] = timeout
    t["results"][0]["duration"] = ms
    return t


def _tspec(title, status, *, msg="", ms=300, timeout=30000, file="ui/all-pages.spec.ts", expected="passed"):
    t = _t_timed(status, msg, ms=ms, timeout=timeout)
    t["expectedStatus"] = expected
    return {"title": title, "file": file, "line": 1, "tests": [t]}


API_TIMEOUT = "TimeoutError: apiRequestContext.post: Timeout 45000ms exceeded.\nCall log:\n  - POST /api/x"
TEST_TIMEOUT = ("Test timeout of 120000ms exceeded.\n"
                "TimeoutError: apiRequestContext.post: Timeout 120000ms exceeded.")
LOCATOR_TIMEOUT = "Error: locator.click: Timeout 5000ms exceeded.\nwaiting for getByRole('button')"
ENV_ON = {**dr.DEFAULTS, "environment_timeouts": True}


def _key(title, file="ui/all-pages.spec.ts"):
    return f"{file}::chromium::all pages › {title}"


def test_parse_report_records_passing_durations_and_test_timeouts(tmp_path):
    rep = _rep(tmp_path, [_tspec("quick", "expected", ms=1234),
                          _tspec("slow", "unexpected", msg=API_TIMEOUT, ms=45040, timeout=60000)])
    assert rep.durations == {_key("quick"): 1.23}
    (f,) = rep.failures
    assert f.timeout_ms == 60000


def test_environment_classification_is_off_by_default(tmp_path):
    rep = _rep(tmp_path, [_tspec("import", "unexpected", msg=API_TIMEOUT)])
    green = {_key("import"): 0.4}
    assert dr.classify(1, "", rep, last_green=green, cfg=dict(dr.DEFAULTS)).kind == "code"
    assert dr.classify(1, "", rep, last_green=green).kind == "code"


def test_timeouts_on_tests_that_were_quick_in_the_last_green_are_environment(tmp_path):
    rep = _rep(tmp_path, [_tspec("guest import", "unexpected", msg=API_TIMEOUT),
                          _tspec("site import", "unexpected", msg=TEST_TIMEOUT, timeout=120000),
                          _tspec("home", "expected")])
    green = {_key("guest import"): 0.3, _key("site import"): 2.5, _key("home"): 1.0}
    c = dr.classify(1, "", rep, last_green=green, cfg=ENV_ON)
    assert c.kind == "environment" and not c.ok and len(c.failures) == 2
    assert "0.3-2.5s in the last green run" in c.reason and "45-120s" in c.reason


@pytest.mark.parametrize("why,specs,green", [
    ("slow in the last green too",
     [_tspec("import", "unexpected", msg=API_TIMEOUT)], {_key("import"): 30.0}),
    ("no green duration for it",
     [_tspec("import", "unexpected", msg=API_TIMEOUT)], {_key("other"): 0.3}),
    ("an assertion failure beside the timeout",
     [_tspec("import", "unexpected", msg=API_TIMEOUT), _tspec("picks", "unexpected", msg="expect(x).toBe(y)")],
     {_key("import"): 0.3, _key("picks"): 0.5}),
    ("an element wait timing out is how a UI regression fails",
     [_tspec("button", "unexpected", msg=LOCATOR_TIMEOUT)], {_key("button"): 0.8}),
    ("a test that did not run in another file",
     [_tspec("import", "unexpected", msg=API_TIMEOUT),
      _tspec("never ran", "skipped", file="ui/other.spec.ts")],
     {_key("import"): 0.3}),
])
def test_reds_that_are_not_environment_stay_code(tmp_path, why, specs, green):
    rep = _rep(tmp_path, specs)
    assert dr.classify(1, "", rep, last_green=green, cfg=ENV_ON).kind == "code", why


def test_a_serial_neighbour_of_a_timed_out_test_may_not_have_run(tmp_path):
    rep = _rep(tmp_path, [_tspec("import", "unexpected", msg=API_TIMEOUT), _tspec("next in group", "skipped")])
    c = dr.classify(1, "", rep, last_green={_key("import"): 0.3}, cfg=ENV_ON)
    assert c.kind == "environment"


def test_environment_alerts_at_once_and_does_not_climb_the_backoff(state_dir):
    st = dr.GateState.load("site-env")
    env = dr.Classification("environment", "3 test(s) now time out", 1, [dr.TestFailure("t", "a.ts")])
    st.record_red(env, "env1", attempts=0, exhausted=False, cfg=dr.DEFAULTS, now=NOW)
    assert st.should_alert("env1", dr.DEFAULTS, NOW)
    st.record_alert("env1", NOW)
    assert not st.should_alert("env1", dr.DEFAULTS, NOW + timedelta(hours=3))
    assert st.entry("env1")["cycles"] == 0 and st.may_repair("env1", now=NOW)[0]


def _env_runner(gr, repo, tmp_path, monkeypatch, runs, **kw):
    r, calls = _runner(gr, repo, tmp_path, monkeypatch, runs=runs,
                       cfg={"environment_timeouts": True, "env_retry_wait_s": 0, **kw.pop("cfg", {})}, **kw)
    r.state.last_green_durations = {_key("a"): 0.3, _key("c"): 1.0}
    stub = r.run_gate

    def run_gate(label, extra=""):
        g = stub(label, extra)
        # The stub classified without the durations; classify the way run_gate does.
        c = dr.classify(g.rc, "", g.report, last_green=r.state.last_green_durations, cfg=r.cfg)
        return g.__class__(g.label, g.rc, c, g.art_dir, g.image_id, g.seconds, g.report)
    monkeypatch.setattr(r, "run_gate", run_gate)
    return r, calls


ENV_FAIL = (1, [_tspec("a", "unexpected", msg=API_TIMEOUT), _tspec("c", "expected")])


def test_loop_environment_red_is_confirmed_retried_once_then_blocks_without_repair(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _env_runner(gr, repo, tmp_path, monkeypatch,
                           runs=[ENV_FAIL, ENV_FAIL, ENV_FAIL, ENV_FAIL], repair=_commit_repair(repo))
    out = r.run()
    assert not out.ok and calls["repair"] == 0
    assert out.classification.kind == "environment"
    assert out.status_reason.startswith("environment:") and "no code repair" in out.status_reason
    assert [lab for lab, _ in calls["gate"]] == ["run-1", "run-1-confirm", "run-2", "run-2-confirm"]
    assert r.state.entry(out.signature)["kind"] == "environment"
    assert r.state.should_alert(out.signature, r.cfg)


def test_loop_known_environment_red_is_not_retried_again(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _env_runner(gr, repo, tmp_path, monkeypatch, runs=[ENV_FAIL, ENV_FAIL])
    first = dr.classify(1, "", dr.parse_report(_write(tmp_path, _report_data(ENV_FAIL[1]), "e.json")),
                        last_green=r.state.last_green_durations, cfg=r.cfg)
    r.state.record_red(first, dr.failure_signature(first), attempts=0, exhausted=False, cfg=r.cfg)
    out = r.run()
    assert not out.ok and calls["repair"] == 0 and len(calls["gate"]) == 2


def test_loop_environment_timeouts_that_pass_alone_ship_as_flaky(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _env_runner(gr, repo, tmp_path, monkeypatch,
                           runs=[ENV_FAIL, (0, [_tspec("a", "expected", ms=29600), _tspec("c", "expected")])])
    out = r.run()
    assert out.ok and out.flaky and calls["repair"] == 0


def test_loop_green_records_the_durations_for_the_next_environment_check(repo, tmp_path, monkeypatch):
    gr = _load_gate_runner()
    r, calls = _runner(gr, repo, tmp_path, monkeypatch,
                       runs=[(0, [_tspec("a", "expected", ms=420), _tspec("c", "expected", ms=1500)])])
    r.state.last_green_durations = {_key("gone"): 3.0, _key("a"): 9.9}
    assert r.run().ok
    assert r.state.last_green_durations == {_key("a"): 0.42, _key("c"): 1.5}
    again = dr.GateState.load("site")
    assert again.last_green_durations == {_key("a"): 0.42, _key("c"): 1.5}


def test_green_durations_keep_an_older_value_for_a_test_still_in_the_suite(state_dir, tmp_path):
    st = dr.GateState.load("site-d")
    st.last_green_durations = {_key("a"): 0.3, _key("old"): 1.0}
    main = _rep(tmp_path, [_tspec("a", "unexpected", msg=API_TIMEOUT), _tspec("c", "expected", ms=700)], "m.json")
    st.record_green_durations(main, None)
    assert st.last_green_durations == {_key("a"): 0.3, _key("c"): 0.7}


# ---------------------------------------------------------------------------
# Deploy log (outlives the 30-event history) and old state files
# ---------------------------------------------------------------------------

def test_deploy_log_outlives_the_history_and_merges_legacy_events(state_dir):
    # A state file from before the deploy log: its deploys are only history events.
    (state_dir).mkdir(parents=True, exist_ok=True)
    (state_dir / "site-log.json").write_text(json.dumps({"site": "site-log", "history": [
        {"at": "2026-11-09T13:54:37+00:00", "event": "deployed", "sha": "9c52b0916f4f",
         "tag": "20261109-1348"},
        {"at": "2026-11-09T16:26:39+00:00", "event": "red", "signature": "s"}]}))
    st = dr.GateState.load("site-log")
    assert [d["tag"] for d in st.deploys] == ["20261109-1348"]
    t0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(40):
        st.record_deployed(f"{i:040d}", f"20261110-{i:04d}", now=t0 + timedelta(hours=i))
    assert len(st.history) == 30 and len(st.deploys) == 41
    st.save()
    log = dr.GateState.load("site-log").deploy_log()
    assert len(log) == 41 and log[0]["tag"] == "20261109-1348"
    assert log[-1]["sha"] == f"{39:040d}"                      # the full sha, not history's 12
    assert [d["at"] for d in log] == sorted(d["at"] for d in log)
    assert all(e.get("event") != "deployed" or e["tag"] != "20261109-1348" for e in st.history)


def test_state_written_before_the_deploy_log_loads(state_dir):
    (state_dir).mkdir(parents=True, exist_ok=True)
    (state_dir / "site-v1.json").write_text(json.dumps({
        "site": "site-v1", "status": "green", "history": [
            {"at": "2026-11-11T15:55:19+00:00", "event": "deployed", "sha": "31f1273cea7c",
             "tag": "20261111-1547"}]}))
    st = dr.GateState.load("site-v1")
    assert st.last_green_durations == {}
    assert st.deploy_log() == [{"at": "2026-11-11T15:55:19+00:00", "sha": "31f1273cea7c",
                                "tag": "20261111-1547"}]


# ---------------------------------------------------------------------------
# One shared list of kinds and paths that never deploy
# ---------------------------------------------------------------------------

def test_data_only_kinds_are_one_list_with_an_env_override(monkeypatch):
    monkeypatch.delenv("DATA_ONLY_KINDS", raising=False)
    assert dr.data_only_kinds() == {"article-author", "article-refresh", "catalog-audit", "h2h",
                                    "product-hydration"}
    assert dr.kind_skips_deploy("h2h") and dr.kind_skips_deploy(" catalog-audit ")
    assert not dr.kind_skips_deploy("seo") and not dr.kind_skips_deploy("")
    monkeypatch.setenv("DATA_ONLY_KINDS", "h2h, catalog-audit")
    assert dr.data_only_kinds() == {"h2h", "catalog-audit"}
    assert not dr.kind_skips_deploy("article-author")


@pytest.mark.parametrize("paths,skips", [
    (["db/migrations/20261111T120000Z_price-rows.sql"], True),
    (["db/migrations/a.sql", "changes/rec-001.summary.md", "docs/x.md", "agents/shelf-audit-agent/site.yaml"], True),
    (["db/migrations/a.sql", "src/simple-server.ts"], False),
    (["frontend/src/App.tsx"], False),
    (["src/agents/helper.ts"], False),              # only the top-level agents/ dir
    (["README.md"], False),
    ([], False),                                    # no evidence: deploy as before
])
def test_no_deploy_reason(monkeypatch, paths, skips):
    monkeypatch.delenv("DEPLOY_SKIP_PATHS", raising=False)
    why = dr.no_deploy_reason(paths)
    assert bool(why) is skips, why
    if skips:
        assert "nothing in the app image changed" in why


def test_no_deploy_paths_env_override(monkeypatch):
    monkeypatch.setenv("DEPLOY_SKIP_PATHS", "none")
    assert dr.no_deploy_reason(["db/migrations/a.sql"]) == ""
    monkeypatch.setenv("DEPLOY_SKIP_PATHS", "content/**")
    assert dr.no_deploy_reason(["content/a.md"]) and not dr.no_deploy_reason(["db/migrations/a.sql"])


def _run_sh_function(name):
    text = (_REPO / "agents" / "implementer" / "run.sh").read_text()
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _bash(script, env=None, cwd=None):
    import os
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, cwd=cwd)


def test_run_sh_uses_the_shared_kind_list_not_its_own(tmp_path):
    text = (_REPO / "agents" / "implementer" / "run.sh").read_text()
    assert 'elif [ "$DISPATCH_KIND" = "catalog-audit" ]' not in text
    assert 'if [ "$DISPATCH_KIND" = "h2h" ]; then\n    echo "[implementer] H2H dispatch' not in text
    fn = _run_sh_function("_deploy_skip_for_kind")
    for kind, want in [("h2h", "yes"), ("article-refresh", "yes"), ("product-hydration", "yes"),
                       ("seo", ""), ("", "")]:
        r = _bash(f'set -euo pipefail\nREPO_ROOT="{_REPO}"\n{fn}\n_deploy_skip_for_kind "{kind}"')
        assert r.returncode == 0 and r.stdout.strip() == want, (kind, r.stdout, r.stderr)
    r = _bash(f'set -euo pipefail\nREPO_ROOT="/nonexistent"\n{fn}\n_deploy_skip_for_kind h2h',
              cwd=str(tmp_path))
    assert r.returncode == 0 and r.stdout.strip() == ""      # broken helper: deploy, as for app code


def test_run_sh_skips_the_deployer_for_migration_only_commits(repo):
    fn = _run_sh_function("_no_deploy_reason_for_commits")
    base = _git(repo, "rev-parse", "HEAD")
    mig = _commit(repo, "db/migrations/20261111_x.sql", "UPDATE t SET x = 1;\n", "db-only lever")
    summ = _commit(repo, "changes/rec-001.summary.md", "done\n", "summary")

    def reason(b, h, repo_path=str(repo)):
        r = _bash(f'set -euo pipefail\nREPO_ROOT="{_REPO}"\n{fn}\n'
                  f'_no_deploy_reason_for_commits "{repo_path}" "{b}" "{h}"')
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()
    assert "db/" in reason(base, summ) and "changes/" in reason(base, summ)
    app = _commit(repo, "src/page.ts", "export const h1 = 'x';\n", "app change")
    assert reason(base, app) == ""                            # app code in the range: deploy
    assert reason(mig, summ) != ""
    assert reason("", summ) == "" and reason("0" * 40, summ) == ""   # unknown base: deploy


def test_run_sh_helpers_ignore_a_framework_package_in_the_cwd(tmp_path, repo):
    # `python3 -c` puts the cwd ahead of PYTHONPATH: run from another framework
    # checkout (an older one without these functions), the helpers used to
    # import that one, fail, and fall back to deploying.
    shadow = tmp_path / "shadow"
    (shadow / "framework" / "core").mkdir(parents=True)
    (shadow / "framework" / "__init__.py").write_text("")
    (shadow / "framework" / "core" / "__init__.py").write_text("")
    (shadow / "framework" / "core" / "deploy_repair.py").write_text("raise ImportError('shadow')\n")
    fn = _run_sh_function("_deploy_skip_for_kind")
    r = _bash(f'set -euo pipefail\nREPO_ROOT="{_REPO}"\n{fn}\n_deploy_skip_for_kind h2h', cwd=str(shadow))
    assert r.returncode == 0 and r.stdout.strip() == "yes", r.stderr
    fn = _run_sh_function("_no_deploy_reason_for_commits")
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "db/migrations/20261111_y.sql", "SELECT 1;\n", "db-only")
    r = _bash(f'set -euo pipefail\nREPO_ROOT="{_REPO}"\n{fn}\n'
              f'_no_deploy_reason_for_commits "{repo}" "{base}" "{head}"', cwd=str(shadow))
    assert r.returncode == 0 and "db/" in r.stdout, r.stderr
