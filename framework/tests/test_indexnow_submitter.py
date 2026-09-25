"""Runs agents/indexnow-submitter/submit.test.ts (unit + end-to-end).

The IndexNow worker is TypeScript; its tests are too. This wrapper finds a
ts-node the same way agent.py does (INDEXNOW_TS_APP_DIR's node_modules, else
the global modules) and skips when none is installed.

Regression cover for the 2026-09-25 audit: bulkOnly sets re-sent every tick,
404/noindex URLs submitted, the `-vs-` compose separator dropped, and the
force-submit queue never read.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_AGENT_DIR = _HERE.parents[2] / "agents" / "indexnow-submitter"


def _node_env() -> dict | None:
    if not shutil.which("npx"):
        return None
    app = os.environ.get("INDEXNOW_TS_APP_DIR", "/home/voidsstr/development/specpicks")
    candidates = [Path(app) / "node_modules"]
    try:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
        if root:
            candidates.append(Path(root))
    except Exception:
        pass
    for nm in candidates:
        if (nm / "ts-node").is_dir() and (nm / "pg").is_dir():
            env = {**os.environ, "NODE_PATH": str(nm)}
            env["PATH"] = f"{nm / '.bin'}:{env.get('PATH', '')}"
            return env
    return None


def test_indexnow_submit_ts():
    env = _node_env()
    if env is None:
        pytest.skip("ts-node + pg not installed (npm i -g ts-node typescript pg)")
    proc = subprocess.run(
        ["npx", "--no-install", "ts-node", "--transpile-only", "--compiler-options",
         '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true}',
         "submit.test.ts"],
        cwd=_AGENT_DIR, env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "all passed" in out, out


def _load_agent_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("indexnow_agent", _AGENT_DIR / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_coverage_gap_appends_and_skips_ledgered_urls(tmp_path, monkeypatch):
    """The coverage writer used to OVERWRITE the queue every run (erasing
    queue-publish.py entries) and re-queue the same permanent gap forever."""
    import time
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "_STATE_DIR", tmp_path)
    q = tmp_path / "s.force-submit.txt"
    q.write_text("https://s.test/published-by-hook")            # no trailing newline
    now_ms = int(time.time() * 1000)
    (tmp_path / "s.indexnow-ledger.tsv").write_text(
        f"https://s.test/sent-yesterday\tsent\t{now_ms - 86_400_000}\t\n"
        f"https://s.test/404-yesterday\trej:http-404\t{now_ms - 86_400_000}\t\n"
        f"https://s.test/sent-long-ago\tsent\t{now_ms - 30 * 86_400_000}\t\n"
    )
    n = mod._append_force_queue("s", [
        "https://s.test/sent-yesterday", "https://s.test/404-yesterday",
        "https://s.test/sent-long-ago", "https://s.test/new", "https://s.test/new",
        "https://s.test/published-by-hook",
    ])
    assert n == 2
    assert q.read_text().splitlines() == [
        "https://s.test/published-by-hook", "https://s.test/sent-long-ago", "https://s.test/new",
    ]
    # A second run with the same permanent gap queues nothing new.
    assert mod._append_force_queue("s", ["https://s.test/new"]) == 0


def test_coverage_check_is_throttled(tmp_path, monkeypatch):
    mod = _load_agent_module()
    monkeypatch.setattr(mod, "_STATE_DIR", tmp_path)
    assert mod._coverage_due("s", 6) is True
    assert mod._coverage_due("s", 6) is False
