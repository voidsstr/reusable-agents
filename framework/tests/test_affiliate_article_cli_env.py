"""Regression tests for the guide engine's first live dry runs (2026-11-22).

1. A dry run swaps in an in-process read-only storage backend
   (STORAGE_BACKEND=article-engine-dry-run). The claude-pool shim runs as a
   child process and cannot resolve that name: it exited rc=1 and the engine
   discarded a finished draft. isolated_cli() must hand the child the parent's
   real backend for the duration of the call, then restore the dry-run one.
2. The facts-pack guard refused a compat assumption's params.detail key. Only
   Amazon's detail_page(_url) is commerce data; a plain "detail" is ours.
"""
import os
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents" / "affiliate-article-agent"))

import article_engine  # noqa: E402
from framework.core import facts_pack as fp  # noqa: E402


@pytest.fixture
def env_guard():
    keys = ("STORAGE_BACKEND", "AAE_PARENT_STORAGE_BACKEND", "CLAUDE_CLI_CMD", article_engine.REAL_CLAUDE_ENV)
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _settings():
    return types.SimpleNamespace()


def test_child_cli_gets_the_parents_storage_backend_during_a_dry_run(env_guard, monkeypatch, tmp_path):
    wrapper = tmp_path / "claude-isolated"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(article_engine, "cli_wrapper", lambda settings: str(wrapper))
    os.environ["AAE_PARENT_STORAGE_BACKEND"] = "azure"
    os.environ["STORAGE_BACKEND"] = "article-engine-dry-run"
    with article_engine.isolated_cli(_settings()):
        assert os.environ["STORAGE_BACKEND"] == "azure"
        assert os.environ["CLAUDE_CLI_CMD"] == str(wrapper)
    assert os.environ["STORAGE_BACKEND"] == "article-engine-dry-run"


def test_unset_parent_backend_is_unset_for_the_child(env_guard, monkeypatch, tmp_path):
    wrapper = tmp_path / "claude-isolated"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(article_engine, "cli_wrapper", lambda settings: str(wrapper))
    os.environ["AAE_PARENT_STORAGE_BACKEND"] = ""
    os.environ["STORAGE_BACKEND"] = "article-engine-dry-run"
    with article_engine.isolated_cli(_settings()):
        assert "STORAGE_BACKEND" not in os.environ
    assert os.environ["STORAGE_BACKEND"] == "article-engine-dry-run"


def test_live_runs_keep_their_backend(env_guard, monkeypatch, tmp_path):
    wrapper = tmp_path / "claude-isolated"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(article_engine, "cli_wrapper", lambda settings: str(wrapper))
    os.environ.pop("AAE_PARENT_STORAGE_BACKEND", None)
    os.environ["STORAGE_BACKEND"] = "azure"
    with article_engine.isolated_cli(_settings()):
        assert os.environ["STORAGE_BACKEND"] == "azure"
    assert os.environ["STORAGE_BACKEND"] == "azure"


def test_wrapper_closes_stdin_and_names_only_real_tools():
    text = (ROOT / "agents" / "affiliate-article-agent" / "bin" / "claude-isolated").read_text()
    assert '"$@" < /dev/null' in text
    deny = [ln for ln in text.splitlines() if "--disallowedTools" in ln and not ln.lstrip().startswith("#")]
    assert len(deny) == 1 and "MultiEdit" not in deny[0]
    assert '--tools ""' in text and "--strict-mcp-config" in text


@pytest.mark.parametrize("key,forbidden", [
    ("detail", False), ("params", False), ("page_detail", False), ("usable_wh", False),
    ("detail_page_url", True), ("detail_page", True), ("list_price", True), ("title", True),
    ("offer_state", True), ("currency", True),
])
def test_forbidden_column_names(key, forbidden):
    assert bool(re.search(fp.DEFAULT_FORBIDDEN_COLUMN_RE, key)) is forbidden


def test_nested_compat_assumption_detail_is_not_commerce():
    row = {"compat": [{"verdict": "fits", "assumptions": [{"rule": "max_voltage_cold",
                                                          "params": {"detail": "n x Voc x (1 + tc x (Tmin - 25))"}}]}]}
    assert fp.commerce_violations(row) == []
    assert fp.commerce_violations({"x": [{"detail_page_url": "https://example.invalid/p"}]})
