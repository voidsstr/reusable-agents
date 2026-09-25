"""progressive-improvement-agent crawls the pages AI assistants use and the
pages verified-human clicks come from before its static seed_urls
(crawler.seed_from_traffic). No DB: the two framework readers are faked."""
from __future__ import annotations

import importlib.util as iu
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
PI_DIR = _ROOT / "agents" / "progressive-improvement-agent"


@pytest.fixture
def pi(monkeypatch):
    monkeypatch.syspath_prepend(str(PI_DIR))
    spec = iu.spec_from_file_location("pi_agent_traffic_seeds_under_test",
                                      PI_DIR / "agent.py")
    mod = iu.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ImportError as e:  # bs4 / yaml etc. not installed here
        pytest.skip(f"PI agent deps unavailable: {e}")
    return mod


class _Conn:
    def rollback(self):
        pass

    def close(self):
        pass

    def cursor(self):
        conn = self

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a):
                pass
        return _C()


def _agent(decisions):
    return types.SimpleNamespace(
        agent_id="site-progressive-improvement-agent",
        decide=lambda kind, msg, **kw: decisions.append(msg))


def test_seeds_ai_then_human_pages_deduped(pi, monkeypatch):
    from framework.core import ai_traffic, human_clicks
    monkeypatch.setenv("SITE_DSN", "postgresql://x")
    monkeypatch.setitem(sys.modules, "psycopg2",
                        types.SimpleNamespace(connect=lambda *a, **k: _Conn()))
    monkeypatch.setattr(ai_traffic, "landed_paths", lambda conn, **kw: [
        {"path": "/"}, {"path": "/blog/best-a"}, {"path": ""}][:kw["limit"]])
    seen_kw = {}

    def fake_paths(conn, spec, **kw):
        seen_kw.update(kw)
        return [{"path": "/blog/best-a", "clicks": 9}, {"path": "/blog/best-b", "clicks": 3}]
    monkeypatch.setattr(human_clicks, "human_click_paths", fake_paths)
    decisions: list[str] = []
    out = pi.ProgressiveImprovementAgent._traffic_seed_paths(_agent(decisions), {
        "dsn_env": "SITE_DSN", "ai_top_n": 3,
        "human_clicks": {"table": "clicks", "group_col": "referer",
                         "match": {"source": "amazon"}, "top_n": 5},
    })
    assert out == ["/", "/blog/best-a", "/blog/best-b"]
    assert seen_kw["table"] == "clicks" and seen_kw["limit"] == 5
    assert seen_kw["match"] == {"source": "amazon"}


def test_failures_degrade_to_no_seeds(pi, monkeypatch):
    from framework.core import ai_traffic
    decisions: list[str] = []
    # no DSN → skipped, logged
    monkeypatch.delenv("SITE_DSN", raising=False)
    assert pi.ProgressiveImprovementAgent._traffic_seed_paths(
        _agent(decisions), {"dsn_env": "SITE_DSN"}) == []
    assert any("not set" in d for d in decisions)
    # reader raises → other source still used, nothing propagates
    monkeypatch.setenv("SITE_DSN", "postgresql://x")
    monkeypatch.setitem(sys.modules, "psycopg2",
                        types.SimpleNamespace(connect=lambda *a, **k: _Conn()))

    def boom(*a, **k):
        raise RuntimeError("statement timeout")
    monkeypatch.setattr(ai_traffic, "landed_paths", boom)
    assert pi.ProgressiveImprovementAgent._traffic_seed_paths(
        _agent(decisions), {"dsn_env": "SITE_DSN"}) == []
    assert any("AI-landed seeds unavailable" in d for d in decisions)
    # disabled / empty block → no DB touch at all
    assert pi.ProgressiveImprovementAgent._traffic_seed_paths(_agent(decisions), {}) == []
    assert pi.ProgressiveImprovementAgent._traffic_seed_paths(
        _agent(decisions), {"enabled": False, "dsn_env": "SITE_DSN"}) == []
