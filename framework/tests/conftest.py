"""Shared pytest fixtures for the framework test suite.

All tests run against LocalFilesystemStorage in a tmp_path. No Azure
SDK required, no network needed.
"""
import os
import sys
import pytest

# Add repo root so `framework.*` imports work from any CWD
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Fresh LocalFilesystemStorage scoped to a tmp dir."""
    from framework.core.storage import LocalFilesystemStorage, reset_default_backend
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("AGENT_STORAGE_LOCAL_PATH", str(tmp_path / "storage"))
    reset_default_backend()
    return LocalFilesystemStorage(str(tmp_path / "storage"))


@pytest.fixture
def agent_id():
    return "test-agent"
