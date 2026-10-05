"""Tests for framework/cli/site_consistency_audit.py `_get` — a dropped
connection is retried before a page is reported unreachable, and an HTTP
error status is not. No network: the module's opener is stubbed."""
from __future__ import annotations

import urllib.error

from framework.cli import site_consistency_audit as sca


class _Resp:
    status = 200
    headers = {"Content-Type": "text/html"}

    def read(self, _n):
        return b"<html><h1>ok</h1></html>"

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, _rq, timeout):
        self.calls += 1
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


def _stub(monkeypatch, outcomes):
    op = _Opener(outcomes)
    monkeypatch.setattr(sca, "_OPENER", op)
    monkeypatch.setattr(sca, "RETRY_PAUSE_S", 0)
    return op


def test_one_dropped_connection_is_retried(monkeypatch):
    op = _stub(monkeypatch, [urllib.error.URLError("[Errno 104] Connection reset by peer"),
                             _Resp()])
    status, _hd, body, _t = sca._get("https://example.com/benchmarks/x", 5)
    assert (status, op.calls) == (200, 2)
    assert "<h1>ok</h1>" in body


def test_persistent_failure_reports_attempts_and_reason(monkeypatch):
    op = _stub(monkeypatch, [urllib.error.URLError("timed out")] * 2)
    status, hd, _b, _t = sca._get("https://example.com/down", 5)
    assert (status, op.calls, hd["_attempts"]) == (0, 2, 2)
    assert hd["_err"] == "URLError: timed out"
    rec = sca.audit_page("https://example.com/down", "", status, hd, 0.1, {})
    assert [f["kind"] for f in rec["findings"]] == ["unreachable"]
    assert "after 2 attempt(s)" in rec["findings"][0]["message"]


def test_http_error_status_is_not_retried(monkeypatch):
    err = urllib.error.HTTPError("https://example.com/gone", 404, "Not Found", {}, None)
    op = _stub(monkeypatch, [err, _Resp()])
    status, _hd, _b, _t = sca._get("https://example.com/gone", 5)
    assert (status, op.calls) == (404, 1)


def test_retries_zero_restores_single_attempt(monkeypatch):
    op = _stub(monkeypatch, [urllib.error.URLError("refused"), _Resp()])
    status, _hd, _b, _t = sca._get("https://example.com/x", 5, retries=0)
    assert (status, op.calls) == (0, 1)
