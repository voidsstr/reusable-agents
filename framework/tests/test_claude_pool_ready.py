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


class _Stop(Exception):
    pass


def _exec_args(model="opus"):
    import argparse
    return argparse.Namespace(claude_args=["--print", "--model", model, "hi"])


def _patch_pool(monkeypatch, ready):
    monkeypatch.setattr(claude_pool, "_discover_profiles_from_disk", lambda: 0)
    monkeypatch.setattr(claude_pool, "ready_profiles", lambda model="": list(ready))
    # Reaching profile selection means admission passed; stop there.
    monkeypatch.setattr(claude_pool, "_open_state_locked", lambda: (_ for _ in ()).throw(_Stop()))


def test_reserve_refuses_low_priority_call_when_headroom_is_short(monkeypatch):
    import pytest
    _patch_pool(monkeypatch, ["p4"])
    monkeypatch.setenv("CLAUDE_POOL_RESERVE", "1")
    with pytest.raises(SystemExit) as e:
        claude_pool.cmd_exec(_exec_args())
    assert e.value.code == claude_pool.RESERVE_EXIT


def test_reserve_admits_when_more_profiles_than_reserve(monkeypatch):
    import pytest
    _patch_pool(monkeypatch, ["p1", "p4"])
    monkeypatch.setenv("CLAUDE_POOL_RESERVE", "1")
    with pytest.raises(_Stop):
        claude_pool.cmd_exec(_exec_args())


def test_no_reserve_never_refuses(monkeypatch):
    import pytest
    _patch_pool(monkeypatch, [])
    monkeypatch.delenv("CLAUDE_POOL_RESERVE", raising=False)
    with pytest.raises(_Stop):
        claude_pool.cmd_exec(_exec_args())
