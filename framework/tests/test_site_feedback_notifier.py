"""agents/site-feedback-notifier: cursor, delivery and failure behaviour.

LocalFilesystemStorage, a fake feedback table and a recording mail sender:
never touches a database, the network, production storage or an LLM. Runs
call run() directly, so no agent-doctor trigger is ever written."""
from __future__ import annotations

import datetime as dt
import importlib.util as iu
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agents" / "site-feedback-notifier"
_spec = iu.spec_from_file_location("site_feedback_notifier_under_test", AGENT_DIR / "agent.py")
sfn = iu.module_from_spec(_spec)
_spec.loader.exec_module(sfn)

from framework.core.agent_base import _is_no_op_summary  # noqa: E402

SITE = "demosite"
DSN_ENV = "SFN_TEST_DSN"
UTC = dt.timezone.utc


def ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def row(i: str, created: str, msg: str = "hello", kind: str = "BUG", **kw):
    return {"id": i, "kind": kind, "severity": kw.get("severity", "LOW"), "message": msg,
            "pageUrl": kw.get("pageUrl", "/product/X1"), "pageTitle": kw.get("pageTitle", "X1"),
            "createdAt": ts(created), "has_shot": kw.get("has_shot", False)}


class FakeTable:
    def __init__(self, rows=None, fail: Exception | None = None):
        self.rows = list(rows or [])
        self.fail = fail
        self.calls = []

    def __call__(self, dsn, table, app_key, since, limit):
        self.calls.append({"dsn": dsn, "table": table, "app_key": app_key, "since": since, "limit": limit})
        if self.fail:
            raise self.fail
        out = [r for r in self.rows if since is None or r["createdAt"] >= since]
        return sorted(out, key=lambda r: (r["createdAt"], r["id"]))[:limit]


class Sender:
    def __init__(self, ok=True, detail="graph:send_as"):
        self.ok, self.detail, self.sent = ok, detail, []

    def __call__(self, **kw):
        self.sent.append(kw)
        return self.ok, self.detail


@pytest.fixture
def env(storage, monkeypatch):
    for var in ("FEEDBACK_NOTIFIER_DRY_RUN", "FEEDBACK_NOTIFIER_SITES", "FRAMEWORK_OPERATOR_EMAIL",
                "IMPLEMENTER_FROM", "AGENT_ID", "AGENT_FORCE_RUN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPERATOR_EMAIL", "ops@example.com")
    monkeypatch.setenv("OPERATOR_FROM_EMAIL", "bot@example.com")
    monkeypatch.setenv("FEEDBACK_NOTIFIER_NOW", "2026-11-22T18:00:00+00:00")
    monkeypatch.setenv(DSN_ENV, "postgresql://unused-in-tests")
    return storage


def write_config(storage, **site_over):
    entry = {"enabled": True, "label": "Demo Site", "dsn_env": DSN_ENV, "app_key": "demo",
             "admin_url": "https://demo.example/admin/hub?tab=feedback",
             "start_from": "2026-11-22T14:00:00Z"}
    entry.update(site_over)
    storage.write_json(sfn.CONFIG_KEY, {"schema_version": "1", "by_site": {SITE: entry}})


def run_agent(storage, table, sender, state=None):
    a = sfn.SiteFeedbackNotifier(storage=storage, run_ts="20261122T180000Z")
    a.auto_recovery_enabled = False
    a.fetch_rows = table
    a.send_email = sender
    a.state = dict(state or {})
    orig = subprocess.Popen

    def _no_subprocess(*x, **k):
        raise AssertionError("subprocess started during an agent run")
    subprocess.Popen = _no_subprocess
    try:
        return a.run()
    finally:
        subprocess.Popen = orig
        a._stop_heartbeat()


def test_first_run_reports_rows_after_start_from_and_escapes_html(env):
    write_config(env)
    table = FakeTable([
        row("a", "2026-11-22T13:59:00", "before start_from"),
        row("b", "2026-11-22T15:00:00", "<script>alert(1)</script> & co", kind="IDEA", severity=None),
        row("c", "2026-11-22T16:30:00", "second", has_shot=True),
    ])
    sender = Sender()
    res = run_agent(env, table, sender)
    assert res.status == "success", res.summary
    assert len(sender.sent) == 1
    mail = sender.sent[0]
    assert mail["to"] == ["ops@example.com"] and mail["sender"] == "bot@example.com"
    assert mail["bypass_digest"] is True
    assert "[Demo Site] 2 new reader feedback submissions" in mail["subject"]
    body = mail["body_html"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in body and "<script>" not in body
    assert "before start_from" not in body
    assert "screenshot attached" in body and "admin/hub?tab=feedback" in body
    st = res.next_state["sites"][SITE]
    assert st["cursor_ts"].startswith("2026-11-22T16:30:00") and st["cursor_ids"] == ["c"]
    assert res.metrics["feedback_new"] == 2 and res.metrics["feedback_undelivered"] == 0
    assert res.metrics["feedback_notify_lag_min"] == 180.0  # oldest emailed row: 15:00 -> 18:00


def test_second_run_sends_nothing_and_is_a_no_op(env):
    write_config(env)
    table = FakeTable([row("b", "2026-11-22T15:00:00"), row("c", "2026-11-22T16:30:00")])
    sender = Sender()
    first = run_agent(env, table, sender)
    second = run_agent(env, table, sender, state=first.next_state)
    assert len(sender.sent) == 1
    assert second.status == "success" and _is_no_op_summary(second.summary), second.summary
    assert second.metrics["feedback_new"] == 0


def test_rows_sharing_the_cursor_instant_are_neither_lost_nor_repeated(env):
    write_config(env)
    table = FakeTable([row("b", "2026-11-22T16:30:00")])
    sender = Sender()
    first = run_agent(env, table, sender)
    table.rows.append(row("a2", "2026-11-22T16:30:00", "same instant, new id"))
    second = run_agent(env, table, sender, state=first.next_state)
    assert len(sender.sent) == 2
    assert "same instant, new id" in sender.sent[1]["body_html"]
    assert "hello" not in sender.sent[1]["body_html"]  # row b is not repeated
    assert sorted(second.next_state["sites"][SITE]["cursor_ids"]) == ["a2", "b"]
    third = run_agent(env, table, sender, state=second.next_state)
    assert len(sender.sent) == 2 and third.metrics["feedback_new"] == 0  # b is not re-sent


def test_undelivered_email_fails_and_keeps_the_cursor(env):
    write_config(env)
    table = FakeTable([row("b", "2026-11-22T15:00:00")])
    res = run_agent(env, table, Sender(ok=False, detail="graph: 401"))
    assert res.status == "failure" and "not delivered" in res.summary
    assert res.metrics["feedback_undelivered"] == 1
    # cursor still at start_from, so the next run retries the same row
    assert res.next_state["sites"][SITE]["cursor_ts"].startswith("2026-11-22T14:00:00")
    retry = Sender()
    res2 = run_agent(env, table, retry, state=res.next_state)
    assert res2.status == "success" and len(retry.sent) == 1


def test_digest_dropped_mail_is_not_delivery(env, monkeypatch):
    calls = {}

    def fake_send_via_msmtp(**kw):
        calls.update(kw)
        return True, "digest disabled: dropped"
    import shared.site_quality as sq
    monkeypatch.setattr(sq, "send_via_msmtp", fake_send_via_msmtp)
    ok, _ = sfn.default_send_email(subject="s", body_html="b", to=["x@example.com"],
                                   sender="y@example.com", msmtp_account="automation",
                                   bypass_digest=True)
    assert ok is False and calls["bypass_digest"] is True


def test_no_recipient_fails_instead_of_going_quiet(env, monkeypatch):
    monkeypatch.delenv("OPERATOR_EMAIL")
    write_config(env)
    res = run_agent(env, FakeTable([row("b", "2026-11-22T15:00:00")]), Sender())
    assert res.status == "failure" and "no recipient" in res.summary


def test_one_read_error_warns_two_in_a_row_fail(env):
    write_config(env)
    bad = FakeTable(fail=TimeoutError("timeout exceeded when trying to connect"))
    first = run_agent(env, bad, Sender())
    assert first.status == "success" and first.metrics["site_errors"] == 1
    second = run_agent(env, bad, Sender(), state=first.next_state)
    assert second.status == "failure" and "2 run(s) in a row" in second.summary


def test_unreadable_storage_config_fails_closed(env, monkeypatch):
    write_config(env)

    def boom(key):
        raise ValueError("archived")
    monkeypatch.setattr(env, "read_json", boom)
    res = run_agent(env, FakeTable([]), Sender())
    assert res.status == "failure" and "config unreadable" in res.summary


def test_site_missing_from_config_fails(env):
    write_config(env)
    state = {"sites": {"oldsite": {"cursor_ts": "2026-11-22T10:00:00+00:00", "cursor_ids": []}}}
    res = run_agent(env, FakeTable([]), Sender(), state=state)
    assert res.status == "failure" and "oldsite" in res.summary
    assert "oldsite" in res.next_state["sites"]  # its cursor is kept


def test_dry_run_sends_nothing_and_keeps_state(env, monkeypatch):
    monkeypatch.setenv("FEEDBACK_NOTIFIER_DRY_RUN", "1")
    write_config(env)
    sender = Sender()
    res = run_agent(env, FakeTable([row("b", "2026-11-22T15:00:00")]), sender, state={"x": 1})
    assert sender.sent == [] and res.next_state == {"x": 1}
    assert all(k.startswith("dry_run_") for k in res.metrics)


def test_bad_table_identifier_is_a_site_error(env):
    write_config(env, table='Feedback"; DROP TABLE x; --')
    res = run_agent(env, FakeTable([]), Sender())
    assert res.status == "failure" and "plain identifier" in res.summary


def test_goals_bind_to_emitted_metrics(env):
    goals = json.loads((AGENT_DIR / "goals.json").read_text())["goals"]
    write_config(env)
    res = run_agent(env, FakeTable([row("b", "2026-11-22T15:00:00")]), Sender())
    for g in goals:
        assert g["target_metric"] in res.metrics, g["id"]


def test_no_site_name_literals_in_agent_code():
    src = (AGENT_DIR / "agent.py").read_text().lower()
    for name in ("specpicks", "aisleprompt", "runtimepicks"):
        assert not re.search(rf"\b{name}\b", src), name
