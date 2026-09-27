"""claude_pool.ready_profiles: which profiles can serve a model right now."""
import json
from datetime import datetime, timedelta, timezone

from framework.cli import claude_pool


def test_ready_profiles_respects_auth_and_family_limits(tmp_path, monkeypatch):
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state = {
        "__outage__": {},
        "p-ok": {"home": "a"},
        "p-opus-capped": {"home": "b", "limit_resets_at": {"opus": soon}},
        "p-sonnet-capped": {"home": "c", "limit_resets_at": {"sonnet": soon, "opus": past}},
        "p-no-auth": {"home": "d"},
    }
    f = tmp_path / "state.json"
    f.write_text(json.dumps(state))
    monkeypatch.setattr(claude_pool, "STATE_FILE", f)
    monkeypatch.setattr(claude_pool, "_is_usable", lambda p: p["home"] != "d")
    assert sorted(claude_pool.ready_profiles("claude-opus-5-5")) == ["p-ok", "p-sonnet-capped"]
    assert sorted(claude_pool.ready_profiles("claude-sonnet-4-6")) == ["p-ok", "p-opus-capped"]


def test_ready_profiles_unreadable_state(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_pool, "STATE_FILE", tmp_path / "missing.json")
    assert claude_pool.ready_profiles() == []
