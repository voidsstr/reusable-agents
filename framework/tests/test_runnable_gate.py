"""Tests for the runnable_modes trigger gate + confirmation_flow propagation."""
from __future__ import annotations

from fastapi.testclient import TestClient

from framework.core.registry import AgentManifest, register_agent


def _setup(monkeypatch, tmp_path):
    """Point storage at tmp_path BEFORE any registry calls."""
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("AGENT_STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    monkeypatch.setenv("AGENT_TRIGGER_QUEUE_DIR", str(tmp_path / "queue"))
    monkeypatch.delenv("FRAMEWORK_API_TOKEN", raising=False)
    from framework.core.storage import reset_default_backend
    reset_default_backend()


def _client():
    from framework.api.app.main import app
    return TestClient(app)


def test_summary_includes_new_fields(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    register_agent(AgentManifest(
        id="x", name="X", category="ops",
        runnable_modes=["chained"],
        confirmation_flow={"enabled": True, "kind": "upstream-gated"},
    ))
    c = _client()
    r = c.get("/api/agents")
    assert r.status_code == 200
    by = {a["id"]: a for a in r.json()}
    assert "x" in by
    assert by["x"]["runnable_modes"] == ["chained"]
    assert by["x"]["confirmation_flow"]["kind"] == "upstream-gated"


def test_trigger_blocked_when_manual_not_allowed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    register_agent(AgentManifest(
        id="chained-only", name="Chain", category="ops",
        runnable_modes=["chained"],
        entry_command="echo nope",
    ))
    c = _client()
    r = c.post("/api/agents/chained-only/trigger")
    assert r.status_code == 409
    detail = r.json()["detail"].lower()
    assert "queue-driven" in detail or "manually runnable" in detail


def test_trigger_allowed_when_manual_present(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    register_agent(AgentManifest(
        id="manual-ok", name="Manual", category="ops",
        runnable_modes=["cron", "manual"],
        entry_command="echo ok",
    ))
    c = _client()
    r = c.post("/api/agents/manual-ok/trigger")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_trigger_default_runnable_modes_allows_manual(monkeypatch, tmp_path):
    """Legacy manifests (default factory ['cron','manual']) should allow manual."""
    _setup(monkeypatch, tmp_path)
    register_agent(AgentManifest(
        id="legacy", name="Legacy", category="ops",
        entry_command="echo ok",
    ))
    c = _client()
    r = c.post("/api/agents/legacy/trigger")
    assert r.status_code == 200


def test_register_propagates_runnable_modes_and_confirmation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    c = _client()
    payload = {
        "id": "round-trip",
        "name": "Round Trip",
        "category": "research",
        "runnable_modes": ["cron"],
        "confirmation_flow": {
            "enabled": True,
            "kind": "email-recommendations",
            "description": "round-trip test",
            "owner_email": "test@example.com",
        },
        "entry_command": "echo hi",
        "autowire_cron": False,
    }
    r = c.post("/api/agents/register", json=payload)
    assert r.status_code == 200
    summary = r.json()
    assert summary["runnable_modes"] == ["cron"]
    assert summary["confirmation_flow"]["kind"] == "email-recommendations"
    detail = c.get("/api/agents/round-trip").json()
    assert detail["confirmation_flow"]["owner_email"] == "test@example.com"


def test_chained_only_agent_with_seo_implementer_pattern(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    register_agent(AgentManifest(
        id="implementer-test", name="SEO Implementer", category="seo",
        runnable_modes=["chained"],
        confirmation_flow={
            "enabled": True, "kind": "upstream-gated",
            "description": "Driven by responder-agent dispatches.",
        },
        entry_command="echo apply",
    ))
    c = _client()
    listing = c.get("/api/agents").json()
    by = {a["id"]: a for a in listing}
    assert by["implementer-test"]["runnable_modes"] == ["chained"]
    r = c.post("/api/agents/implementer-test/trigger")
    assert r.status_code == 409
