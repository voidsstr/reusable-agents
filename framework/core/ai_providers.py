"""Central AI provider registry + uniform client interface.

The framework lets an operator configure multiple AI providers
(Azure OpenAI, Anthropic, Ollama, GitHub Copilot, OpenAI direct, etc.)
and pick one as the default. Each agent's manifest can override the
default. Agents call `self.ai_client()` and get a uniform interface
regardless of which backend is wired up.

Why a central config: keys live in ONE place per deployment instead of
scattered across each agent's home repo. The dashboard's iframed UI
shows them all and lets the user switch defaults / per-agent overrides
without editing files.

Storage:
  config/ai-providers.json    — the registry (maps name → provider config)
  config/ai-defaults.json     — global default + per-agent overrides

API keys:
  Stored as ENV-VAR REFERENCES, not raw secrets:
    api_key_env: "AZURE_OPENAI_API_KEY"
  The framework reads `os.environ[api_key_env]` at call time. Operators
  set the env vars on the host (in `.env` or systemd service files).
  This keeps secrets out of the storage layer.

  For dev/test: providers may have inline `api_key` fields. The API
  surface marks them as DANGEROUS and warns in the UI. Production should
  always use api_key_env.

Provider kinds (and the SDK each uses):
  azure_openai   openai SDK with azure_endpoint
  anthropic      anthropic SDK
  ollama         direct HTTP to /api/chat (no SDK needed)
  copilot        HTTP to a copilot-api proxy (e.g., port 4141 in nsc-assistant)
  openai         openai SDK (api.openai.com)

Uniform interface: AIClient.chat(messages, model=None, **kw) → text response.
The framework translates the messages into each backend's native format.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.ai")


PROVIDERS_KEY = "config/ai-providers.json"
DEFAULTS_KEY = "config/ai-defaults.json"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass
class Provider:
    name: str                                # operator-chosen id; primary key
    kind: str                                # azure_openai | anthropic | ollama | copilot | openai
    base_url: str = ""                       # endpoint URL (Azure resource or Ollama host)
    api_key_env: str = ""                    # env var name holding the key (preferred)
    api_key: str = ""                        # inline key (dev only — flagged in UI)
    api_version: str = ""                    # Azure-specific; ignored elsewhere
    deployment: str = ""                     # Azure-specific deployment name (overrides model)
    available_models: list[str] = field(default_factory=list)
    default_model: str = ""
    description: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Provider":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def resolve_key(self) -> str:
        """Return the actual API key — env var first, inline fallback."""
        if self.api_key_env:
            v = os.environ.get(self.api_key_env, "")
            if v:
                return v
        return self.api_key

    def has_key(self) -> bool:
        return bool(self.resolve_key())


# ---------------------------------------------------------------------------
# Registry CRUD
# ---------------------------------------------------------------------------

def _read_providers(s: StorageBackend) -> dict[str, dict]:
    raw = s.read_json(PROVIDERS_KEY)
    return raw if isinstance(raw, dict) else {}


def _write_providers(s: StorageBackend, providers: dict[str, dict]) -> None:
    s.write_json(PROVIDERS_KEY, providers,
                 cache_control="public, max-age=86400")


def list_providers(storage: Optional[StorageBackend] = None) -> list[Provider]:
    s = storage or get_storage()
    return [Provider.from_dict(d) for d in _read_providers(s).values()]


def get_provider(name: str, storage: Optional[StorageBackend] = None) -> Optional[Provider]:
    s = storage or get_storage()
    d = _read_providers(s).get(name)
    return Provider.from_dict(d) if d else None


def upsert_provider(provider: Provider, storage: Optional[StorageBackend] = None) -> Provider:
    s = storage or get_storage()
    if not provider.name:
        raise ValueError("provider.name is required")
    if provider.kind not in {"azure_openai", "anthropic", "ollama", "copilot", "openai", "claude-cli"}:
        raise ValueError(f"unsupported provider kind: {provider.kind!r}")
    with s.lock(PROVIDERS_KEY):
        providers = _read_providers(s)
        providers[provider.name] = provider.to_dict()
        _write_providers(s, providers)
    return provider


def delete_provider(name: str, storage: Optional[StorageBackend] = None) -> bool:
    s = storage or get_storage()
    with s.lock(PROVIDERS_KEY):
        providers = _read_providers(s)
        if name not in providers:
            return False
        del providers[name]
        _write_providers(s, providers)
    return True


# ---------------------------------------------------------------------------
# Defaults / per-agent overrides
# ---------------------------------------------------------------------------

@dataclass
class Defaults:
    """The framework-wide default provider + per-agent overrides.

    Stored at config/ai-defaults.json:
      {
        "default_provider": "azure-openai-prod",
        "default_model": "gpt-4o-mini",
        "agent_overrides": {
          "implementer":   {"provider": "anthropic-claude", "model": "claude-opus-4-7"},
          "market-research":   {"provider": "ollama-local",     "model": "qwen3:8b"}
        }
      }
    """
    default_provider: str = ""
    default_model: str = ""
    agent_overrides: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Defaults":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def read_defaults(storage: Optional[StorageBackend] = None) -> Defaults:
    s = storage or get_storage()
    return Defaults.from_dict(s.read_json(DEFAULTS_KEY) or {})


def write_defaults(defaults: Defaults, storage: Optional[StorageBackend] = None) -> None:
    s = storage or get_storage()
    s.write_json(DEFAULTS_KEY, defaults.to_dict(),
                 cache_control="public, max-age=86400")


def set_default_provider(provider_name: str, model: str = "",
                         storage: Optional[StorageBackend] = None) -> None:
    s = storage or get_storage()
    d = read_defaults(s)
    d.default_provider = provider_name
    if model:
        d.default_model = model
    write_defaults(d, s)


def set_agent_override(agent_id: str, provider: str = "", model: str = "",
                       clear: bool = False,
                       storage: Optional[StorageBackend] = None) -> None:
    s = storage or get_storage()
    d = read_defaults(s)
    if clear:
        d.agent_overrides.pop(agent_id, None)
    else:
        existing = d.agent_overrides.get(agent_id, {})
        if provider: existing["provider"] = provider
        if model:    existing["model"] = model
        d.agent_overrides[agent_id] = existing
    write_defaults(d, s)


def resolve_for_agent(agent_id: str,
                      storage: Optional[StorageBackend] = None) -> tuple[Optional[Provider], str]:
    """Return (provider, model) for the given agent.

    Order of precedence:
      1. agent_overrides[agent_id] in defaults
      2. defaults.default_provider + defaults.default_model
      3. None, "" — caller decides what to do
    """
    s = storage or get_storage()
    d = read_defaults(s)
    override = d.agent_overrides.get(agent_id, {})
    provider_name = override.get("provider") or d.default_provider
    model = override.get("model") or d.default_model
    if not provider_name:
        return None, model
    provider = get_provider(provider_name, s)
    if provider and not model:
        model = provider.default_model
    return provider, model


# ---------------------------------------------------------------------------
# Uniform client interface
# ---------------------------------------------------------------------------

class AIClient:
    """Returned by `ai_client_for(...)`. Backend-specific subclasses below.

    Public entry point is `chat(...)`. Subclasses implement `_chat(...)`.
    The base class wraps every call with framework-level live LLM stream
    capture: each request + response (or error) is appended as a JSONL
    record at agents/<id>/runs/<run_ts>/llm-output.jsonl in the framework
    storage backend. The dashboard's Live LLM tab reads from there. This
    is uniform across providers — operators see prompts + responses for
    Anthropic/OpenAI/Ollama/Copilot/AzureOpenAI/claude-cli alike, no
    per-provider work required.
    """

    def __init__(self, provider: Provider, model: str = ""):
        self.provider = provider
        self.model = model or provider.default_model

    # OpenAI-shape providers (azure_openai, copilot, openai) support
    # tool-calling through this base hook. Set True on the subclass.
    supports_tools: bool = False

    def chat(self, messages: list[dict], *, model: str = "",
             temperature: float = 0.0, max_tokens: int = 1024,
             tools: Optional[list[dict]] = None,
             tool_runner: Optional[callable] = None,
             max_tool_iterations: int = 12,
             **kwargs) -> str:
        """Public entry. Wraps the provider-specific _chat() with
        framework-level live LLM stream capture.

        When `tools` is provided AND the subclass advertises
        `supports_tools = True`, the request is dispatched through
        `_chat_with_tools()` which runs an iterative tool-calling loop
        (model → tool_calls → execute → re-call → repeat). Subclasses
        that don't support tools (claude-cli, ollama, anthropic via
        text completions) ignore the parameter — claude-cli has its own
        tools natively, ollama lacks function-calling at the HTTP level
        in our minimal client, and the anthropic SDK has a different
        tool shape we'd need a separate path for.
        """
        from . import llm_stream  # avoid circular import at module load
        import time as _time
        chosen_model = model or self.model
        stream = llm_stream.stream_for_current_run()
        if stream:
            stream.request(
                model=chosen_model, messages=messages,
                provider=self.provider.name,
                kind_provider=self.provider.kind,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        t0 = _time.time()
        try:
            if tools and self.supports_tools:
                text = self._chat_with_tools(
                    messages, model=model, temperature=temperature,
                    max_tokens=max_tokens, tools=tools,
                    tool_runner=tool_runner,
                    max_iterations=max_tool_iterations,
                    stream=stream, **kwargs,
                )
            else:
                text = self._chat(messages, model=model, temperature=temperature,
                                  max_tokens=max_tokens, **kwargs)
        except Exception as e:
            if stream:
                stream.error(str(e), duration_s=_time.time() - t0)
            try:
                from . import llm_usage
                input_text = "\n".join(
                    (m.get("content") if isinstance(m.get("content"), str) else "")
                    for m in messages or []
                )
                agent_id = getattr(stream, "agent_id", "") if stream else ""
                run_ts = getattr(stream, "run_ts", "") if stream else ""
                llm_usage.record_call(
                    agent_id=agent_id, run_ts=run_ts,
                    provider=self.provider.name, kind_provider=self.provider.kind,
                    model=chosen_model, input_text=input_text,
                    output_text="", duration_s=_time.time() - t0, is_error=True,
                )
            except Exception:
                pass
            raise
        if stream:
            stream.response(
                text or "", duration_s=_time.time() - t0,
                provider=self.provider.name,
                kind_provider=self.provider.kind,
                model=chosen_model,
            )

        # Framework-level usage tracking — append a row to the monthly
        # llm-usage JSONL so the dashboard can aggregate cost+calls per
        # provider/model. Best-effort: never break the agent if logging
        # fails. Concatenate user+system from messages for the input
        # estimate; subclasses don't expose a separate prompt object.
        try:
            from . import llm_usage  # avoid circular at module load
            input_text = "\n".join(
                (m.get("content") if isinstance(m.get("content"), str) else "")
                for m in messages or []
            )
            agent_id = ""
            run_ts = ""
            if stream:
                agent_id = getattr(stream, "agent_id", "") or ""
                run_ts = getattr(stream, "run_ts", "") or ""
            llm_usage.record_call(
                agent_id=agent_id, run_ts=run_ts,
                provider=self.provider.name, kind_provider=self.provider.kind,
                model=chosen_model, input_text=input_text,
                output_text=text or "",
                duration_s=_time.time() - t0,
                is_error=False,
            )
        except Exception as _e:
            pass

        return text

    def _chat_with_tools(self, messages, *, model, temperature, max_tokens,
                          tools, tool_runner, max_iterations, stream, **kwargs):
        """Default no-op — subclasses that support tools override this.
        Falls back to `_chat()` if a subclass forgets to override."""
        return self._chat(messages, model=model, temperature=temperature,
                          max_tokens=max_tokens, **kwargs)

    def _chat(self, messages: list[dict], *, model: str = "",
              temperature: float = 0.0, max_tokens: int = 1024,
              **kwargs) -> str:
        """Subclass override — invoke the backend, return text. Don't
        touch the LLM stream from here; the public `chat()` wrapper
        handles request/response/error logging uniformly."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<AIClient provider={self.provider.name} kind={self.provider.kind} model={self.model}>"


def _openai_tool_loop(create_completion, *, messages, tools, tool_runner,
                       max_iterations, stream):
    """Shared tool-calling loop for OpenAI-shape providers.

    `create_completion(messages, tools)` is a callable the caller binds
    to its provider-specific SDK client. It must return an
    OpenAI-style ChatCompletion object with `.choices[0].message`
    (carrying optional `.tool_calls`). We append the assistant's
    response and any tool result messages into a working copy of
    `messages` and re-call until the model stops requesting tools or
    we hit `max_iterations`.

    Why a hard iteration cap: stuck loops happen — a model can
    repeatedly retry a failing search. The cap prevents an agent from
    burning its entire wall-clock budget in tool calls. 12 is enough
    for typical research (1 search + 4-6 fetches).
    """
    from . import llm_stream as _ls
    import json as _json

    runner = tool_runner or __import__(
        "framework.core.tools", fromlist=["default_runner"]
    ).default_runner

    working: list[dict] = list(messages)
    for iteration in range(max_iterations):
        resp = create_completion(working, tools)
        msg = resp.choices[0].message
        # `tool_calls` may be None or empty list — both mean "done"
        tcs = getattr(msg, "tool_calls", None) or []
        if not tcs:
            return msg.content or ""

        # Append the assistant's message verbatim. The OpenAI SDK
        # returns Pydantic objects; convert to dict for the next call.
        # `model_dump()` is openai-python v1+; fallback to .dict().
        try:
            assistant_dump = msg.model_dump(exclude_unset=True)
        except AttributeError:
            assistant_dump = msg.dict(exclude_unset=True)  # type: ignore[attr-defined]
        # Strip None content if present — some servers reject it.
        if assistant_dump.get("content") is None and assistant_dump.get("tool_calls"):
            assistant_dump["content"] = ""
        working.append(assistant_dump)

        # Execute every tool call sequentially. Could parallelize but
        # research workloads are small; sequential keeps logging
        # readable and rate-limits one call at a time.
        for tc in tcs:
            name = tc.function.name
            try:
                args = _json.loads(tc.function.arguments or "{}")
            except _json.JSONDecodeError:
                args = {}
            if stream:
                try:
                    stream.chunk(f"\n[tool_call {iteration+1}/{max_iterations}] "
                                 f"{name}({_json.dumps(args)[:300]})\n")
                except Exception:
                    pass
            output = runner(name, args)
            if stream:
                try:
                    preview = output[:400] + ("…" if len(output) > 400 else "")
                    stream.chunk(f"[tool_result] {preview}\n")
                except Exception:
                    pass
            working.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": output,
            })

    # Out of iterations — return whatever the last assistant text was,
    # or a clear signal so the caller can detect truncation.
    return ("[ERROR] max_tool_iterations reached without final answer. "
            "Last assistant content: " + (msg.content or "(none)"))


class _AzureOpenAIClient(AIClient):
    supports_tools = True

    def _client(self):
        from openai import AzureOpenAI  # type: ignore
        return AzureOpenAI(
            api_key=self.provider.resolve_key(),
            azure_endpoint=self.provider.base_url,
            api_version=self.provider.api_version or "2024-08-01-preview",
        )

    def _chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        client = self._client()
        resp = client.chat.completions.create(
            model=self.provider.deployment or model or self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    def _chat_with_tools(self, messages, *, model, temperature, max_tokens,
                          tools, tool_runner, max_iterations, stream, **kwargs):
        client = self._client()
        deploy = self.provider.deployment or model or self.model
        def _do(working, tools_):
            return client.chat.completions.create(
                model=deploy, messages=working,
                temperature=temperature, max_tokens=max_tokens,
                tools=tools_, tool_choice="auto",
            )
        return _openai_tool_loop(_do, messages=messages, tools=tools,
                                  tool_runner=tool_runner,
                                  max_iterations=max_iterations, stream=stream)


class _AnthropicClient(AIClient):
    def _chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        from anthropic import Anthropic  # type: ignore
        client = Anthropic(api_key=self.provider.resolve_key())
        # Anthropic separates system from messages
        system = ""
        clean = []
        for m in messages:
            if m.get("role") == "system":
                system += (m.get("content") or "") + "\n"
            else:
                clean.append({"role": m["role"], "content": m["content"]})
        kw = {
            "model": model or self.model or "claude-opus-4-7",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": clean,
        }
        if system.strip():
            kw["system"] = system.strip()
        resp = client.messages.create(**kw)
        # Concatenate all text blocks
        return "".join(b.text for b in resp.content if hasattr(b, "text"))


class _OllamaClient(AIClient):
    def _chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        import urllib.request, urllib.error
        base = (self.provider.base_url or "http://localhost:11434").rstrip("/")
        target_model = model or self.model or "qwen3:8b"

        # ── Preflight reachability check (2026-05-12) ───────────────────
        # The remote ollama box (e.g. ollama-4080 at 192.168.1.82:11434)
        # is reached cross-LAN. Silent failures were happening when the
        # remote ollama wasn't running — the agent's chat call would
        # eventually raise URLError or ConnectionError and the fallback
        # chain would transparently re-route to a metered provider.
        # No alert, no signal that the user's "cost-savings split" was
        # broken. We probe /api/tags with a tight 3s timeout here. If
        # unreachable, fire an operator alert (24h cooldown) THEN raise
        # so the fallback chain can pick a backup.
        if not _ollama_reachable(base, timeout_s=3.0):
            _alert_ollama_unreachable(
                agent_id=getattr(self.provider, "agent_id", None)
                         or getattr(self, "agent_id", None) or "?",
                provider_name=self.provider.name,
                base_url=base,
            )
            raise ConnectionError(
                f"ollama unreachable at {base} (provider={self.provider.name})"
            )

        # Serialize all ollama use across the framework — chat AND
        # code-editor backends fight for VRAM. We hold the global
        # ollama_dispatch_lock for the duration of one chat call. Lock
        # is reentrant per-process, file-locked, releases on crash.
        # `on_timeout="proceed"` so a stuck mate doesn't kill chat —
        # we'd rather race than hang indefinitely. Chat calls are
        # bounded by urlopen's 300s timeout below.
        try:
            from .locks import ollama_dispatch_lock as _ollama_lock
            from .code_editor import ensure_ollama_model_loaded as _ensure
        except Exception:
            _ollama_lock = None
            _ensure = None

        def _do_call() -> str:
            url = base + "/api/chat"
            body = json.dumps({
                "model": target_model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }).encode()
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as r:
                payload = json.loads(r.read().decode())
            return (payload.get("message") or {}).get("content", "")

        if _ollama_lock is None:
            return _do_call()

        # Tight 5-min lock-acquire timeout — chat wants a fast verdict.
        # If we can't grab the lock that fast, run anyway (proceed) so
        # we don't block higher-level agents.
        with _ollama_lock(timeout_s=300, on_timeout="proceed"):
            if _ensure is not None:
                try:
                    _ensure(target_model, base_url=base)
                except Exception:
                    # Preflight is best-effort; ollama will load on
                    # demand if we skipped here.
                    pass
            return _do_call()


class _CopilotClient(AIClient):
    """Talks to a copilot-api proxy (OpenAI-compatible) — typical setup is
    `npx copilot-api` running on localhost:4141."""
    supports_tools = True

    def _client(self):
        from openai import OpenAI  # type: ignore
        return OpenAI(
            api_key=self.provider.resolve_key() or "dummy",
            base_url=self.provider.base_url or "http://localhost:4141/v1",
        )

    def _chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        client = self._client()
        resp = client.chat.completions.create(
            model=model or self.model or "gpt-4o-mini",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    def _chat_with_tools(self, messages, *, model, temperature, max_tokens,
                          tools, tool_runner, max_iterations, stream, **kwargs):
        client = self._client()
        chosen = model or self.model or "gpt-4o-mini"
        def _do(working, tools_):
            return client.chat.completions.create(
                model=chosen, messages=working,
                temperature=temperature, max_tokens=max_tokens,
                tools=tools_, tool_choice="auto",
            )
        return _openai_tool_loop(_do, messages=messages, tools=tools,
                                  tool_runner=tool_runner,
                                  max_iterations=max_iterations, stream=stream)


class _OpenAIClient(AIClient):
    supports_tools = True

    def _client(self):
        from openai import OpenAI  # type: ignore
        return OpenAI(api_key=self.provider.resolve_key())

    def _chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        client = self._client()
        resp = client.chat.completions.create(
            model=model or self.model or "gpt-4o-mini",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    def _chat_with_tools(self, messages, *, model, temperature, max_tokens,
                          tools, tool_runner, max_iterations, stream, **kwargs):
        client = self._client()
        chosen = model or self.model or "gpt-4o-mini"
        def _do(working, tools_):
            return client.chat.completions.create(
                model=chosen, messages=working,
                temperature=temperature, max_tokens=max_tokens,
                tools=tools_, tool_choice="auto",
            )
        return _openai_tool_loop(_do, messages=messages, tools=tools,
                                  tool_runner=tool_runner,
                                  max_iterations=max_iterations, stream=stream)


class _ClaudeCliClient(AIClient):
    """Shells out to the `claude` CLI in --print mode. Uses the user's
    Claude Max session token (no API key) — the same auth `claude setup-token`
    establishes for interactive sessions.

    Why this exists: Claude Max is billed per-subscription, not per-API-call,
    so for one-shot text generation (analysis, audit prompts) we want this
    path instead of the Anthropic API.

    Caller is responsible for ensuring `claude` is on PATH and authenticated
    on the host that runs the agent.
    """
    def _chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        import subprocess
        # Build a single prompt from messages — claude -p takes one string.
        # Concatenate system + user/assistant turns with role headers; the
        # model handles them fine for one-shot prompts.
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "user").upper()
            content = m.get("content", "")
            if role == "SYSTEM":
                parts.append(f"# SYSTEM\n{content}")
            elif role == "USER":
                parts.append(f"# USER\n{content}")
            elif role == "ASSISTANT":
                parts.append(f"# ASSISTANT\n{content}")
            else:
                parts.append(content)
        prompt = "\n\n".join(parts)

        chosen = model or self.model or "claude-opus-4-7"
        # The CLI accepts both aliases ("opus", "sonnet") + full ids.
        # We pass the configured value through verbatim.
        # Default --max-turns 1 is fine for one-shot text generation. Bump
        # it for callers that need tool use (web_search etc.) — the CLI
        # counts each tool invocation as a turn.
        max_turns = int(kwargs.get("max_turns", 1))
        # CLAUDE_CLI_CMD explicit override (highest priority).
        # Otherwise auto-discover the claude-pool shim — when present, it
        # round-robins across authenticated/non-rate-limited Max profiles
        # so chat agents share the load instead of all hammering one
        # account. Falls back to bare `claude` (single default account)
        # when the pool isn't installed.
        claude_bin = os.environ.get("CLAUDE_CLI_CMD", "")
        if not claude_bin:
            pool_shim = os.path.expanduser(
                os.environ.get("CLAUDE_POOL_ROOT",
                               "~/.reusable-agents/claude-pool") + "/bin/claude"
            )
            if os.path.isfile(pool_shim) and os.access(pool_shim, os.X_OK):
                claude_bin = pool_shim
            else:
                claude_bin = "claude"
        cmd = [
            claude_bin,
            "--print",
            "--output-format", "text",
            "--no-session-persistence",
            "--model", chosen,
            "--max-turns", str(max_turns),
            "--dangerously-skip-permissions",
            prompt,
        ]
        # Stream claude --print output line-by-line through the parent's
        # stdout AND capture into a buffer. Why both:
        #   * The buffer is what we return to the caller (the agent code
        #     parses it as JSON / text).
        #   * The parent's stdout is what the host-worker redirects to
        #     /tmp/reusable-agents-logs/<agent_id>-<run_ts>.log, which the
        #     dashboard's Live LLM panel tails.
        # Previously subprocess.run(capture_output=True) PIPED everything
        # into the Python string only, leaving the job log empty for any
        # Python-driven agent (vs the bash-shell implementer that wrote
        # straight to stdout).
        timeout_s = kwargs.get("timeout", 600)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
            )
        except FileNotFoundError as e:
            raise RuntimeError("claude CLI not on PATH — install Claude Code first") from e

        out_buf: list[str] = []
        err_buf: list[str] = []
        prefix = f"[claude-cli {os.environ.get('AGENT_ID','?')} {chosen}]"

        # Use the framework's LLM stream for live dashboard updates. Each
        # line claude emits also lands as a `chunk` record at
        # agents/<id>/runs/<run_ts>/llm-output.jsonl in framework storage,
        # so the dashboard's Live LLM tab shows reasoning + tool calls
        # in near-real-time (regardless of which host ran the agent).
        from . import llm_stream as _ls
        live_stream = _ls.stream_for_current_run()
        import time as _time
        t_chunk_start = _time.time()
        # Throttle chunks: write to storage at most every 2s OR when
        # ≥4KB has accumulated. Avoids hammering Azure blob with one
        # request per line.
        _CHUNK_FLUSH_S = 2.0
        _CHUNK_FLUSH_BYTES = 4096

        def _pump(stream, buf, sink, kind):
            chunk_buf: list[str] = []
            chunk_bytes = 0
            last_flush = _time.time()
            for line in iter(stream.readline, ""):
                buf.append(line)
                # Echo to parent stdout/stderr so host-worker's redirect
                # captures it (legacy log path; still useful for grep).
                try:
                    sink.write(f"{prefix} {line}" if not line.startswith(prefix) else line)
                    sink.flush()
                except Exception:
                    pass
                # Append to LLM stream throttled.
                if live_stream and kind == "stdout":
                    chunk_buf.append(line)
                    chunk_bytes += len(line)
                    now = _time.time()
                    if chunk_bytes >= _CHUNK_FLUSH_BYTES or (now - last_flush) >= _CHUNK_FLUSH_S:
                        try:
                            live_stream.chunk("".join(chunk_buf),
                                              elapsed_s=now - t_chunk_start)
                        except Exception:
                            pass
                        chunk_buf = []
                        chunk_bytes = 0
                        last_flush = now
            # Final flush of anything left.
            if live_stream and kind == "stdout" and chunk_buf:
                try:
                    live_stream.chunk("".join(chunk_buf),
                                      elapsed_s=_time.time() - t_chunk_start)
                except Exception:
                    pass
            stream.close()

        import threading
        t_out = threading.Thread(target=_pump, args=(proc.stdout, out_buf, sys.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=_pump, args=(proc.stderr, err_buf, sys.stderr, "stderr"), daemon=True)
        t_out.start(); t_err.start()
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            t_out.join(timeout=2); t_err.join(timeout=2)
            raise RuntimeError(f"claude CLI timed out after {timeout_s}s")
        t_out.join(timeout=5); t_err.join(timeout=5)

        # Build a duck-typed result so the rest of this method's logic works.
        class _R:
            returncode = rc
            stdout = "".join(out_buf)
            stderr = "".join(err_buf)
        proc = _R()
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "")[:500]
            raise RuntimeError(
                f"claude CLI exited rc={proc.returncode}: {err}"
            )
        return (proc.stdout or "").strip()


_CLIENT_CLASSES = {
    "azure_openai": _AzureOpenAIClient,
    "anthropic":    _AnthropicClient,
    "ollama":       _OllamaClient,
    "copilot":      _CopilotClient,
    "openai":       _OpenAIClient,
    "claude-cli":   _ClaudeCliClient,
}


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------

# When the primary provider raises one of these, fall back. We match on
# strings because each backend raises its own typed errors and we don't
# want to import every SDK at the module level just for isinstance checks.
_FALLBACK_TRIGGER_SUBSTRINGS = (
    "rate limit", "rate-limit", "rate_limit",
    "timed out", "timeout", "timedout",
    "429", "503", "502", "504",
    "claude cli timed out",
    "all 3 profile(s) rate-limited",
    "no auth", "rc=1:", "rc=124:",  # claude-cli wrapper exit codes
    "exhausted", "quota", "overloaded",
)

# Kinds we consider for fallback, in preference order. Skipped if no
# provider of that kind is registered or if the registered one has no
# usable credentials.
DEFAULT_FALLBACK_KINDS = ("copilot", "ollama", "azure_openai", "openai", "anthropic")


def _is_fallback_trigger(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(sub in s for sub in _FALLBACK_TRIGGER_SUBSTRINGS)


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Best-effort extraction of a server-suggested retry-after delay from
    a rate-limit exception. Azure OpenAI surfaces this through the
    response headers as the `Retry-After` field (seconds) and the SDK
    embeds the message in the exception string. Returns None if we can't
    find a usable hint, in which case the caller picks its own backoff."""
    s = str(exc)
    import re as _re
    # openai SDK: "Please retry after 23 seconds"
    m = _re.search(r"retry after\s+(\d+)\s*second", s, _re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # generic "Retry-After: 23" headers (some providers echo the header)
    m = _re.search(r"retry[- ]after[:\s]+(\d+)", s, _re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # Azure throttle message: "Try again in 12 seconds"
    m = _re.search(r"try again in\s+(\d+)\s*second", s, _re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _build_fallback_chain(primary: "AIClient",
                           storage: Optional[StorageBackend] = None) -> list["AIClient"]:
    """Return [primary] + any registered providers of a different kind
    that have credentials, in DEFAULT_FALLBACK_KINDS order. We exclude
    other providers of the SAME kind as primary because if claude-cli is
    rate-limited, another claude-cli provider would hit the same pool."""
    chain: list[AIClient] = [primary]
    seen_kinds = {primary.provider.kind}
    seen_names = {primary.provider.name}
    s = storage or get_storage()
    all_providers = list_providers(s)
    for kind in DEFAULT_FALLBACK_KINDS:
        if kind in seen_kinds:
            continue
        for p in all_providers:
            if p.name in seen_names or p.kind != kind:
                continue
            # Skip providers that need credentials and don't have them.
            if p.kind in {"openai", "azure_openai", "anthropic"} and not p.has_key():
                continue
            cls = _CLIENT_CLASSES.get(p.kind)
            if cls is None:
                continue
            chain.append(cls(p, model=p.default_model))
            seen_kinds.add(p.kind)
            seen_names.add(p.name)
            break
    return chain


# ─────────────────────────────────────────────────────────────────────
# Operator-alert hooks for the LLM fallback chain.
# Two events the operator cares about (added 2026-05-12 after Copilot
# Plus premium quota was found at -3,760/1,500 with no auto-billing):
#
#   1. Pre-paid provider rejects a request (claude pool rate-limited,
#      copilot premium quota exceeded, etc.) — operator should know
#      their plan is exhausted so they can decide whether to:
#        a. Wait for the natural reset window
#        b. Re-arrange chain (drop the dead backend)
#        c. Manually flip producers to a different provider
#
#   2. Chain falls through to a METERED provider (azure_openai,
#      openai, anthropic API). Each call costs real money — the
#      operator wants to know when this starts happening so they can
#      cap or shift workload.
#
# Both alerts use notify_operator() with daily cooldown so the
# operator gets ~1 email per (event, provider) per day, not per call.
# ─────────────────────────────────────────────────────────────────────

# Providers that bill per-token (the operator pays directly via API).
# Pre-paid plans (claude-cli via Claude Max, copilot via subscription,
# ollama free local) are NOT in this set.
_METERED_KINDS = frozenset((
    "azure_openai",   # Azure OpenAI — token-metered via the user's Azure subscription
    "openai",         # OpenAI direct API — token-metered
    "anthropic",      # Anthropic direct API — token-metered (not Claude Max!)
))


def _ollama_reachable(base_url: str, *, timeout_s: float = 3.0) -> bool:
    """Cheap GET /api/tags reachability probe. Returns True if the
    server responds 2xx within timeout_s. Used by _OllamaClient._chat
    to fail fast (and alert) when the local ollama box is down,
    instead of letting urlopen hang for 300s."""
    import urllib.request, urllib.error
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _alert_ollama_unreachable(*, agent_id: str, provider_name: str,
                               base_url: str) -> None:
    """Operator-alert when a chat agent's ollama provider is unreachable.

    The cost-savings split (5090 + 4080 ollama boxes) only works if both
    ollama instances are running. When the remote box (typically
    ollama-4080 at 192.168.1.82:11434) is down, ten chat agents
    silently fall through the fallback chain to METERED providers
    (Azure OpenAI / OpenAI / Anthropic). No signal otherwise — usage
    logs don't distinguish "intended fallback" from "endpoint dead".

    Cooldown: 24h per (agent, provider) so the operator gets one
    summary alert, not one per chat call.
    """
    try:
        from .resilience import notify_operator
        notify_operator(
            agent_id=agent_id,
            error=RuntimeError(
                f"Ollama provider '{provider_name}' unreachable at "
                f"{base_url}. Chat fallback chain will route to a paid "
                f"provider instead."
            ),
            context={
                "category": "ollama-unreachable",
                "provider": provider_name,
                "base_url": base_url,
                "impact": (
                    "Cost-savings split is broken: agents configured to "
                    "use this local ollama box are bouncing to metered "
                    "providers (Azure / OpenAI / Anthropic) or to "
                    "another claude-cli session, increasing burn rate "
                    "and risk of Claude Max throttling."
                ),
                "recovery": (
                    "Bring ollama back up on the host. Typical fixes: "
                    "(a) systemctl --user start ollama on the box, "
                    "(b) ollama serve & if not running under systemd, "
                    "(c) check the host is reachable on the LAN, "
                    "(d) verify /api/tags responds: "
                    f"curl {base_url}/api/tags"
                ),
            },
            severity="high",
            cooldown_s=86400,
        )
    except Exception:
        pass


def _alert_metered_fallback(*, agent_id: str, winner_kind: str,
                              winner_name: str, primary_name: str,
                              last_err: str) -> None:
    """Operator-alert when chain_with_fallback selects a per-token
    metered provider AFTER the pre-paid primary failed."""
    try:
        from .resilience import notify_operator
        # One alert per (agent, winner_kind) per day. The operator only
        # needs to know "X is now hitting Azure" once per day per agent
        # — not for every single call.
        notify_operator(
            agent_id=agent_id,
            error=RuntimeError(
                f"Falling through to METERED provider: {winner_name} "
                f"({winner_kind}). Primary {primary_name} failed: {last_err}"
            ),
            context={
                "category": "metered-provider-fallback",
                "winner_provider": winner_name,
                "winner_kind": winner_kind,
                "primary_provider": primary_name,
                "last_error": last_err,
                "impact": (
                    "Each call to this provider bills against your "
                    "metered subscription (Azure OpenAI / OpenAI / "
                    "Anthropic direct API). Your pre-paid plan "
                    "(Claude Max / Copilot Plus / etc.) is exhausted "
                    "or unavailable."
                ),
                "recovery": (
                    "Either (a) wait for the pre-paid plan to reset, "
                    "(b) edit config/code-editor-config.json and "
                    "config/ai-defaults.json to remove the metered "
                    "provider from the chain, or (c) pause heavy "
                    "agents until quota recovers."
                ),
            },
            severity="medium",
            cooldown_s=86400,  # 24h — one alert per agent+provider per day
        )
    except Exception:
        # Non-fatal — never let an alert path break the actual LLM call.
        pass


def _alert_provider_quota_exhausted(*, agent_id: str, provider_kind: str,
                                      provider_name: str, err: str) -> None:
    """Operator-alert when a provider rejects a call with what looks
    like a quota/rate-limit error. Distinct from the metered-fallback
    alert because this fires the moment the FIRST provider goes
    sideways, not when the chain successfully recovers."""
    # Only alert for pre-paid providers — we don't care about metered
    # rate-limits (those are tactical, not strategic).
    if provider_kind in _METERED_KINDS:
        return
    try:
        from .resilience import notify_operator
        notify_operator(
            agent_id=agent_id,
            error=RuntimeError(
                f"Pre-paid provider rejected: {provider_name} "
                f"({provider_kind}). Error: {err}"
            ),
            context={
                "category": "provider-quota-exhausted",
                "provider": provider_name,
                "provider_kind": provider_kind,
                "error": err,
                "impact": (
                    "This agent's primary pre-paid provider is "
                    "currently unavailable. Subsequent calls will "
                    "fall through the chain — possibly to metered "
                    "providers (see the metered-fallback alert if "
                    "that fires)."
                ),
                "recovery": (
                    "Check provider state — for Claude Max: run "
                    "`PYTHONPATH=. python3 framework/cli/claude_pool.py "
                    "status` to see profile rate-limits + reset windows. "
                    "For Copilot Plus: curl http://localhost:4141/usage "
                    "to see premium-request quota_remaining. The chain "
                    "will keep working with whatever's left, but heavy "
                    "agents may start hitting metered providers."
                ),
            },
            severity="medium",
            cooldown_s=86400,  # 24h — one alert per agent+provider per day
        )
    except Exception:
        pass


def chat_with_fallback(agent_id: str,
                        messages: list[dict],
                        *,
                        tools: Optional[list[dict]] = None,
                        tool_runner: Optional[callable] = None,
                        max_tool_iterations: int = 12,
                        max_tokens: int = 2000,
                        temperature: float = 0.0,
                        max_attempts: Optional[int] = None,
                        storage: Optional[StorageBackend] = None,
                        override_provider: Optional[str] = None,
                        override_model: Optional[str] = None,
                        **kwargs) -> tuple[str, "AIClient"]:
    """Call the agent's primary provider; on rate-limit/timeout, fall back
    to other registered providers of different kinds in
    DEFAULT_FALLBACK_KINDS order. Returns (text, client_used) so the
    caller can record which provider actually produced the answer.

    Tools are passed to every client in the chain. Clients that don't
    advertise `supports_tools` ignore them (text-only behavior); clients
    that do (copilot/openai/azure_openai) drive the tool loop.

    On hard non-recoverable errors (auth failure, bad request) the call
    re-raises immediately rather than burning through the chain — those
    won't get better by switching providers.
    """
    # Prompt-bloat guardrail: stop runaway-context bugs before we burn
    # tokens. ~4 chars/token rough estimate; refuse anything over the
    # configured limit (default 200k chars ≈ 50k tokens). Loud failure
    # in the run log + decision trail beats a quiet $5 spike.
    _prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
    _prompt_cap = int(os.environ.get("AI_PROMPT_CHAR_CAP", "200000"))
    if _prompt_chars > _prompt_cap:
        raise ValueError(
            f"prompt-budget exceeded: {_prompt_chars} chars > AI_PROMPT_CHAR_CAP="
            f"{_prompt_cap} (~{_prompt_chars // 4} tokens). Reduce inputs or "
            f"raise the cap via env. agent_id={agent_id}"
        )

    primary = ai_client_for(agent_id, storage=storage,
                             override_provider=override_provider,
                             override_model=override_model)
    chain = _build_fallback_chain(primary, storage=storage)
    if max_attempts is not None:
        chain = chain[: max(1, max_attempts)]

    last_err: Optional[BaseException] = None
    import time as _time
    for i, client in enumerate(chain):
        # Up to 2 attempts on the same provider before falling over: the
        # first call, then one retry honouring any server-suggested
        # Retry-After. Azure 429s are typically window-bounded and clear
        # within a few seconds — falling straight to a different model
        # (different quality + cost profile) on a transient throttle is
        # worse than a brief wait. Retry only on rate-limit-shaped
        # errors and only when the suggested wait is short (< 30s) so
        # we don't sit on long quota exhaustions.
        for attempt in range(2):
            try:
                text = client.chat(
                    messages,
                    tools=tools,
                    tool_runner=tool_runner,
                    max_tool_iterations=max_tool_iterations,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
                if i > 0 or attempt > 0:
                    logger.warning(
                        "ai-fallback: agent=%s recovered on chain[%d] attempt=%d "
                        "(provider=%s kind=%s) after primary=%s failed: %s",
                        agent_id, i + 1, attempt + 1,
                        client.provider.name, client.provider.kind,
                        primary.provider.name, last_err,
                    )
                # Operator-alert: chain fell through to a METERED provider.
                # The user pays per token for these. Fire a daily-capped
                # alert so the operator knows their pre-paid plans (Claude
                # Max, Copilot Plus) are exhausted and metered billing is
                # actively running. The free tier providers (claude-cli,
                # copilot, ollama) don't trigger this alert.
                if i > 0 and client.provider.kind in _METERED_KINDS:
                    _alert_metered_fallback(
                        agent_id=agent_id,
                        winner_kind=client.provider.kind,
                        winner_name=client.provider.name,
                        primary_name=primary.provider.name,
                        last_err=str(last_err)[:300],
                    )
                return text, client
            except Exception as e:  # noqa: BLE001 — we re-raise for non-fallback cases
                if not _is_fallback_trigger(e):
                    # Hard error — don't burn the chain on a bad prompt.
                    raise
                last_err = e
                wait = _retry_after_seconds(e)
                if attempt == 0 and wait is not None and 0 < wait <= 30:
                    logger.warning(
                        "ai-fallback: agent=%s provider=%s rate-limited; "
                        "honouring Retry-After=%.1fs before retrying same provider",
                        agent_id, client.provider.name, wait,
                    )
                    _time.sleep(wait + 0.5)
                    continue
                logger.warning(
                    "ai-fallback: agent=%s provider=%s kind=%s failed (%s): %s",
                    agent_id, client.provider.name, client.provider.kind,
                    type(e).__name__, str(e)[:300],
                )
                # Operator-alert: a pre-paid provider just rejected us.
                # Different shape from the metered-fallback alert above —
                # this fires the FIRST time a quota is detected so the
                # operator knows their plan is full. Quota-exhausted vs.
                # transient-rate-limit differ in the error text; we fire
                # for both since the operational impact is the same.
                _alert_provider_quota_exhausted(
                    agent_id=agent_id,
                    provider_kind=client.provider.kind,
                    provider_name=client.provider.name,
                    err=str(e)[:300],
                )
                break  # fall over to next provider in chain

    # All clients exhausted.
    raise RuntimeError(
        f"ai-fallback exhausted: tried {len(chain)} providers; "
        f"last error: {last_err}"
    )


def ai_client_for(agent_id: str,
                  *,
                  override_provider: Optional[str] = None,
                  override_model: Optional[str] = None,
                  storage: Optional[StorageBackend] = None) -> AIClient:
    """Return an AIClient configured for `agent_id`.

    Resolution order (2026-05-11: operator-config beats manifest defaults):
      1. override_provider / override_model arguments (e.g., agent run-time choice)
      2. defaults.json agent_overrides[agent_id]    ← operator-level config
      3. agent's manifest `metadata.ai.provider`     ← code-level default
      4. defaults.json default_provider / default_model

    Rationale: manifests pin provider as a code-author preference, but
    operators need a runtime escape hatch when a provider runs out or a
    cheaper local alternative is good enough. Previously manifest beat
    agent_overrides, which meant config changes were silently ignored
    — the 2026-05-09→11 incident where `specpicks-ebay-product-sync-agent`
    burned 9,763 claude-pool calls despite a config override to
    ollama-4080 happened because of this. Swapped here.
    Raises if no provider can be resolved.
    """
    s = storage or get_storage()

    # Try the override path first
    provider: Optional[Provider] = None
    model = override_model or ""
    if override_provider:
        provider = get_provider(override_provider, s)

    # Then operator-config (agent_overrides in defaults.json)
    if provider is None:
        provider, default_model = resolve_for_agent(agent_id, s)
        if provider is not None and not model:
            model = default_model

    # Then the agent's manifest (code-level default)
    if provider is None:
        from .registry import get_agent
        manifest = get_agent(agent_id, s)
        if manifest:
            ai_cfg = (manifest.metadata or {}).get("ai") or {}
            if ai_cfg.get("provider"):
                provider = get_provider(ai_cfg["provider"], s)
                if not model and ai_cfg.get("model"):
                    model = ai_cfg["model"]

    if provider is None:
        raise RuntimeError(
            f"No AI provider configured for agent '{agent_id}'. "
            f"Configure one via the framework UI or POST /api/providers."
        )

    cls = _CLIENT_CLASSES.get(provider.kind)
    if cls is None:
        raise RuntimeError(f"unsupported provider kind: {provider.kind!r}")
    return cls(provider, model=model)
