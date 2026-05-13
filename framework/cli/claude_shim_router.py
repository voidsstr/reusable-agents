#!/usr/bin/env python3
"""Agent-aware `claude` invocation router.

The legacy agents (article-author, benchmark-research, agent-doctor,
product-hydration, ...) hardcode `claude --print ... <prompt>` as a
subprocess. They predate `framework.core.ai_providers.ai_client_for`,
so per-agent provider overrides in `config/ai-defaults.json` have no
effect on them.

This router is invoked from the claude-pool shim BEFORE delegating to
the real pool's `exec` subcommand. It checks the `AGENT_ID` env var,
looks up the override, and if it points at an ollama provider it
translates the claude CLI args to an ollama API call and returns the
response on stdout (matching `--output-format text` format).

If the agent has no override (or the override is itself a claude/copilot
provider), the router exits non-zero and the shim falls through to the
normal pool path. That's intentional — keeps anything not explicitly
re-routed on the existing chain.

Translation supported:
  --print              → single-shot completion (no agentic loop)
  --output-format text → return choices[0].message.content
  --output-format json → return a synthetic JSON envelope
  --output-format stream-json → emit the same shape claude does (line-buffered JSON)
  --model <X>          → ignored — uses the override's model
  --max-turns <N>      → ignored — single-shot only
  --dangerously-skip-permissions → ignored
  positional prompt    → user-message body
  stdin prompt         → user-message body (when no positional)

Multi-turn / tool-use claude flows are NOT supported — those legitimately
need claude-cli and should not have ollama overrides.

Exit codes:
  0    routed to ollama, response written to stdout
  10   no override for this agent (caller should fall through to claude)
  11   override exists but provider is not ollama (fall through)
  12   ollama call returned non-OK (caller may fall through)
  2    bad args / setup error
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure the framework package resolves regardless of CWD.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))


def _eprint(msg: str) -> None:
    sys.stderr.write(f"[claude-shim-router] {msg}\n")


def _resolve_override(agent_id: str) -> tuple[str, str, str] | None:
    """Return (provider_name, base_url, model) if the agent has an ollama
    override. Returns None when no override / non-ollama override.
    """
    try:
        from framework.core.storage import get_storage
        from framework.core.ai_providers import resolve_for_agent
    except Exception as e:
        _eprint(f"framework import failed: {e}")
        return None

    s = get_storage()
    try:
        provider, model = resolve_for_agent(agent_id, s)
    except Exception as e:
        _eprint(f"resolve_for_agent({agent_id!r}) failed: {e}")
        return None
    if provider is None or provider.kind != "ollama":
        return None
    base = (provider.base_url or "").rstrip("/")
    if not base:
        _eprint(f"ollama provider {provider.name!r} has no base_url")
        return None
    return provider.name, base, (model or "qwen3:14b")


def _parse_claude_argv(argv: list[str]) -> dict:
    """Pull the bits we care about out of the claude CLI invocation.

    We're permissive: anything we don't recognize is dropped (since
    ollama doesn't have analogues).
    """
    out: dict = {
        "prompt": "",
        "output_format": "text",
        "model_hint": "",
    }
    i = 0
    pos: list[str] = []
    while i < len(argv):
        tok = argv[i]
        if tok in ("--print",):
            i += 1
            continue
        if tok == "--output-format" and i + 1 < len(argv):
            out["output_format"] = argv[i + 1]
            i += 2
            continue
        if tok == "--model" and i + 1 < len(argv):
            out["model_hint"] = argv[i + 1]
            i += 2
            continue
        if tok == "--max-turns" and i + 1 < len(argv):
            i += 2
            continue
        if tok == "--dangerously-skip-permissions":
            i += 1
            continue
        if tok == "--no-session-persistence":
            i += 1
            continue
        if tok == "--include-partial-messages":
            i += 1
            continue
        if tok == "--verbose":
            i += 1
            continue
        if tok == "--help" or tok == "-h":
            # If they're asking for help, fall through.
            return {}
        if tok.startswith("-"):
            # Unknown flag — try to skip its arg (heuristic). If we
            # get this wrong the worst case is we treat the value as a
            # positional, which we'll join into the prompt.
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 2
            else:
                i += 1
            continue
        pos.append(tok)
        i += 1

    if pos:
        out["prompt"] = " ".join(pos)
    return out


def _read_stdin_prompt() -> str:
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def _ollama_chat(base: str, model: str, prompt: str,
                 timeout_s: float = 600.0) -> tuple[int, str, str]:
    """POST /api/chat. Returns (rc, content, raw_json)."""
    import urllib.request as _u
    import urllib.error as _ue

    # Strip /v1 suffix (ai_providers stores .../v1 because OpenAI clients
    # want it; native ollama API lives at the root).
    base_native = base
    if base_native.endswith("/v1"):
        base_native = base_native[:-3]

    url = f"{base_native}/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        # Disable thinking tokens — we want the answer directly.
        "think": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = _u.Request(url, data=data,
                     headers={"Content-Type": "application/json"})
    try:
        with _u.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except _ue.HTTPError as e:
        return 12, "", f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return 12, "", f"network error: {e}"

    try:
        d = json.loads(body)
    except Exception as e:
        return 12, "", f"non-JSON response: {body[:300]} ({e})"

    msg = d.get("message", {})
    content = msg.get("content") or ""
    return 0, content, body


def main(argv: list[str]) -> int:
    agent_id = os.environ.get("AGENT_ID", "").strip()
    if not agent_id:
        # No agent context — straight passthrough.
        return 10

    override = _resolve_override(agent_id)
    if override is None:
        return 10  # no ollama override — fall through to claude

    provider_name, base, model = override

    parsed = _parse_claude_argv(argv)
    prompt = parsed.get("prompt") or _read_stdin_prompt()
    if not prompt.strip():
        _eprint("empty prompt — falling through")
        return 11

    fmt = parsed.get("output_format", "text")
    started = time.time()
    rc, content, raw = _ollama_chat(base, model, prompt)
    elapsed = time.time() - started

    if rc != 0:
        _eprint(f"{provider_name} {model} failed in {elapsed:.1f}s: {raw[:200]}")
        return rc

    _eprint(f"{provider_name} {model} OK in {elapsed:.1f}s, "
            f"{len(content)} chars (agent={agent_id})")

    if fmt == "stream-json":
        # Emit a single completed message in the streaming envelope shape
        # claude uses, so callers parsing line-by-line still see something
        # they can pick up. Best-effort — most legacy agents we route here
        # use --output-format text, not stream-json.
        events = [
            {"type": "system", "subtype": "init",
             "model": model, "session_id": "ollama-shim"},
            {"type": "assistant",
             "message": {
                 "id": "ollama-shim",
                 "model": model,
                 "role": "assistant",
                 "content": [{"type": "text", "text": content}],
             }},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": content,
             "duration_ms": int(elapsed * 1000)},
        ]
        for ev in events:
            sys.stdout.write(json.dumps(ev) + "\n")
        return 0

    if fmt == "json":
        sys.stdout.write(json.dumps({
            "result": content,
            "model": model,
            "provider": provider_name,
            "is_error": False,
            "duration_ms": int(elapsed * 1000),
        }) + "\n")
        return 0

    # Default: text
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
