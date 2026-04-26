"""LocalFilesystemStorage round-trip + edge cases."""
import pytest


def test_round_trip_bytes(storage):
    storage.write_bytes("a/b/c.bin", b"hello\x00world")
    assert storage.read_bytes("a/b/c.bin") == b"hello\x00world"
    assert storage.exists("a/b/c.bin")


def test_round_trip_text(storage):
    storage.write_text("greet.txt", "hi 👋")
    assert storage.read_text("greet.txt") == "hi 👋"


def test_round_trip_json(storage):
    obj = {"k": [1, 2, "x"], "nested": {"y": True}}
    storage.write_json("config.json", obj)
    assert storage.read_json("config.json") == obj


def test_read_missing_returns_none(storage):
    assert storage.read_bytes("missing") is None
    assert storage.read_text("missing") is None
    assert storage.read_json("missing") is None
    assert storage.read_jsonl("missing") == []


def test_append_jsonl_streaming(storage):
    storage.append_jsonl("log.jsonl", {"a": 1})
    storage.append_jsonl("log.jsonl", {"a": 2})
    storage.append_jsonl("log.jsonl", {"a": 3})
    assert storage.read_jsonl("log.jsonl") == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_list_prefix(storage):
    storage.write_text("agents/x/manifest.json", "{}")
    storage.write_text("agents/x/runs/r1/progress.json", "{}")
    storage.write_text("agents/y/manifest.json", "{}")
    keys = sorted(storage.list_prefix("agents/x/"))
    assert "agents/x/manifest.json" in keys
    assert "agents/x/runs/r1/progress.json" in keys
    # y/ shouldn't show up
    assert all(not k.startswith("agents/y/") for k in keys)


def test_path_traversal_rejected(storage):
    with pytest.raises(ValueError):
        storage.write_text("../escape", "")


def test_delete(storage):
    storage.write_text("doomed.txt", "x")
    assert storage.exists("doomed.txt")
    assert storage.delete("doomed.txt") is True
    assert not storage.exists("doomed.txt")
    assert storage.delete("nonexistent") is False


def test_lock_acquire(storage):
    with storage.lock("foo") as acquired:
        assert acquired is True
        # Re-entrant from same key in nested context fails (timeout)
        with storage.lock("foo", timeout_s=0.2) as acquired2:
            assert acquired2 is False
