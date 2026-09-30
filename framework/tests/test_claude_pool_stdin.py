"""A prompt too big for argv travels on stdin, and a failover must replay it.

2026-09-30: the growth-strategist memo prompt passed the kernel's 128 KiB
per-argument limit and every run died with E2BIG before claude started.
ai_providers now sends such prompts on stdin with CLAUDE_POOL_BUFFER_STDIN=1,
and the pool shim buffers them so the second profile gets the same prompt.
"""
from __future__ import annotations

import os
import stat

from framework.cli import claude_pool


def _fake_claude(tmp_path):
    script = tmp_path / "fake-claude"
    script.write_text('#!/usr/bin/env bash\necho "stdin=$(wc -c)"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_each_dispatch_gets_the_whole_buffered_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_pool, "_resolve_real_claude", lambda: _fake_claude(tmp_path))
    prompt = "x" * 300_000  # well past MAX_ARG_STRLEN
    for _attempt in range(2):  # first profile, then the failover
        rc, out = claude_pool._run_one_dispatch("p", str(tmp_path), ["--print"],
                                                stdin_data=prompt)
        assert rc == 0
        assert "stdin=300000" in out


def test_without_buffer_stdin_is_inherited(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_pool, "_resolve_real_claude", lambda: _fake_claude(tmp_path))
    with open(os.devnull) as devnull:
        monkeypatch.setattr("sys.stdin", devnull)
        rc, out = claude_pool._run_one_dispatch("p", str(tmp_path), ["--print", "hi"])
    assert rc == 0
    assert "stdin=" in out
