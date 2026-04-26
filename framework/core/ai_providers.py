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
    s.write_json(PROVIDERS_KEY, providers)


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
    if provider.kind not in {"azure_openai", "anthropic", "ollama", "copilot", "openai"}:
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
          "seo-implementer":   {"provider": "anthropic-claude", "model": "claude-opus-4-7"},
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
    s.write_json(DEFAULTS_KEY, defaults.to_dict())


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
    """Returned by `ai_client_for(...)`. Backend-specific subclasses below."""

    def __init__(self, provider: Provider, model: str = ""):
        self.provider = provider
        self.model = model or provider.default_model

    def chat(self, messages: list[dict], *, model: str = "",
             temperature: float = 0.0, max_tokens: int = 1024,
             **kwargs) -> str:
        """messages: list of {role, content} like OpenAI's format.
        Returns the assistant's text response. Subclasses override."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<AIClient provider={self.provider.name} kind={self.provider.kind} model={self.model}>"


class _AzureOpenAIClient(AIClient):
    def chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        from openai import AzureOpenAI  # type: ignore
        client = AzureOpenAI(
            api_key=self.provider.resolve_key(),
            azure_endpoint=self.provider.base_url,
            api_version=self.provider.api_version or "2024-08-01-preview",
        )
        # Azure uses deployment name as the model id
        resp = client.chat.completions.create(
            model=self.provider.deployment or model or self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""


class _AnthropicClient(AIClient):
    def chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
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
    def chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        import urllib.request, urllib.error
        url = (self.provider.base_url or "http://localhost:11434").rstrip("/") + "/api/chat"
        body = json.dumps({
            "model": model or self.model or "qwen3:8b",
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }).encode()
        req = urllib.request.Request(url, data=body,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = json.loads(r.read().decode())
        return (payload.get("message") or {}).get("content", "")


class _CopilotClient(AIClient):
    """Talks to a copilot-api proxy (OpenAI-compatible) — typical setup is
    `npx copilot-api` running on localhost:4141."""
    def chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        from openai import OpenAI  # type: ignore
        client = OpenAI(
            api_key=self.provider.resolve_key() or "dummy",
            base_url=self.provider.base_url or "http://localhost:4141/v1",
        )
        resp = client.chat.completions.create(
            model=model or self.model or "gpt-4o-mini",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""


class _OpenAIClient(AIClient):
    def chat(self, messages, *, model="", temperature=0.0, max_tokens=1024, **kwargs):
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=self.provider.resolve_key())
        resp = client.chat.completions.create(
            model=model or self.model or "gpt-4o-mini",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""


_CLIENT_CLASSES = {
    "azure_openai": _AzureOpenAIClient,
    "anthropic":    _AnthropicClient,
    "ollama":       _OllamaClient,
    "copilot":      _CopilotClient,
    "openai":       _OpenAIClient,
}


def ai_client_for(agent_id: str,
                  *,
                  override_provider: Optional[str] = None,
                  override_model: Optional[str] = None,
                  storage: Optional[StorageBackend] = None) -> AIClient:
    """Return an AIClient configured for `agent_id`.

    Resolution order:
      1. override_provider / override_model arguments (e.g., agent run-time choice)
      2. agent's manifest `metadata.ai.provider` / `metadata.ai.model`
      3. defaults.json agent_overrides[agent_id]
      4. defaults.json default_provider / default_model
    Raises if no provider can be resolved.
    """
    s = storage or get_storage()

    # Try the override path first
    provider: Optional[Provider] = None
    model = override_model or ""
    if override_provider:
        provider = get_provider(override_provider, s)

    # Then the agent's manifest
    if provider is None:
        from .registry import get_agent
        manifest = get_agent(agent_id, s)
        if manifest:
            ai_cfg = (manifest.metadata or {}).get("ai") or {}
            if ai_cfg.get("provider"):
                provider = get_provider(ai_cfg["provider"], s)
                if not model and ai_cfg.get("model"):
                    model = ai_cfg["model"]

    # Finally fall back to global defaults + per-agent overrides
    if provider is None:
        provider, default_model = resolve_for_agent(agent_id, s)
        if not model:
            model = default_model

    if provider is None:
        raise RuntimeError(
            f"No AI provider configured for agent '{agent_id}'. "
            f"Configure one via the framework UI or POST /api/providers."
        )

    cls = _CLIENT_CLASSES.get(provider.kind)
    if cls is None:
        raise RuntimeError(f"unsupported provider kind: {provider.kind!r}")
    return cls(provider, model=model)
