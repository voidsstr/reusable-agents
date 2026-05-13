"""AI provider config CRUD + per-agent assignments.

The dashboard UI uses this to:
  - List providers + which one is the default
  - Add / edit / delete providers (Azure OpenAI, Anthropic, Ollama, etc.)
  - Set the global default provider + model
  - Set per-agent overrides ("for SEO Implementer, use claude-opus-4-7")
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from framework.core import ai_providers

from ..auth import require_token


router = APIRouter(prefix="/api/providers", tags=["providers"], dependencies=[Depends(require_token)])


# ── Pydantic models ────────────────────────────────────────────────────────

class ProviderModel(BaseModel):
    name: str
    kind: str          # azure_openai | anthropic | ollama | copilot | openai
    base_url: str = ""
    api_key_env: str = ""
    api_key: str = ""
    api_version: str = ""
    deployment: str = ""
    available_models: list[str] = Field(default_factory=list)
    default_model: str = ""
    description: str = ""
    metadata: dict = Field(default_factory=dict)
    has_key: bool = False           # computed — DOES NOT round-trip on PUT


class DefaultsModel(BaseModel):
    default_provider: str = ""
    default_model: str = ""
    agent_overrides: dict = Field(default_factory=dict)


class SetDefaultRequest(BaseModel):
    provider_name: str
    model: str = ""


class SetAgentOverrideRequest(BaseModel):
    agent_id: str
    provider: str = ""
    model: str = ""
    clear: bool = False


def _to_model(p: ai_providers.Provider, mask_key: bool = True) -> ProviderModel:
    d = p.to_dict()
    if mask_key and d.get("api_key"):
        d["api_key"] = "***"  # never return raw inline key
    d["has_key"] = p.has_key()
    return ProviderModel(**d)


# ── Provider CRUD ──────────────────────────────────────────────────────────

@router.get("", response_model=list[ProviderModel])
def list_all():
    return [_to_model(p) for p in ai_providers.list_providers()]


@router.put("/{name}", response_model=ProviderModel)
def upsert(name: str, req: ProviderModel):
    if name != req.name:
        raise HTTPException(status_code=400, detail="path id must match body name")
    p = ai_providers.Provider.from_dict(req.dict())
    return _to_model(ai_providers.upsert_provider(p))


@router.delete("/{name}")
def delete(name: str):
    if not ai_providers.delete_provider(name):
        raise HTTPException(status_code=404, detail="provider not found")
    return {"ok": True}


@router.get("/{name}", response_model=ProviderModel)
def get_one(name: str):
    p = ai_providers.get_provider(name)
    if p is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return _to_model(p)


# ── Defaults + agent overrides ─────────────────────────────────────────────

@router.get("/defaults/all", response_model=DefaultsModel)
def get_defaults():
    d = ai_providers.read_defaults()
    return DefaultsModel(**d.to_dict())


@router.post("/defaults/set", response_model=DefaultsModel)
def set_default(req: SetDefaultRequest):
    if not ai_providers.get_provider(req.provider_name):
        raise HTTPException(status_code=404, detail=f"provider {req.provider_name!r} not found")
    ai_providers.set_default_provider(req.provider_name, req.model)
    return DefaultsModel(**ai_providers.read_defaults().to_dict())


@router.post("/defaults/agent-override", response_model=DefaultsModel)
def set_override(req: SetAgentOverrideRequest):
    if req.provider and not ai_providers.get_provider(req.provider):
        raise HTTPException(status_code=404, detail=f"provider {req.provider!r} not found")
    ai_providers.set_agent_override(
        req.agent_id, provider=req.provider, model=req.model, clear=req.clear,
    )
    return DefaultsModel(**ai_providers.read_defaults().to_dict())


@router.get("/resolve/{agent_id}")
def resolve(agent_id: str):
    """What provider + model would be used if `agent_id` called ai_client()?

    Also returns the full fallback chain so the dashboard can show
    operators exactly where this agent's traffic will land if the
    primary provider rate-limits / times out.
    """
    provider, model = ai_providers.resolve_for_agent(agent_id)

    # Build the same chain that chat_with_fallback would walk: primary
    # then DEFAULT_FALLBACK_KINDS in order, picking the first registered
    # provider of each kind that has credentials.
    fallback_chain: list[dict] = []
    if provider is not None:
        try:
            from framework.core.ai_providers import (
                DEFAULT_FALLBACK_KINDS, list_providers,
                _CLIENT_CLASSES, ai_client_for,
            )
            primary_client = ai_client_for(agent_id)
            chain_clients = ai_providers._build_fallback_chain(primary_client)
            for c in chain_clients:
                fallback_chain.append({
                    "provider": c.provider.name,
                    "kind": c.provider.kind,
                    "model": c.model,
                })
        except Exception as e:
            # Don't fail the whole endpoint if chain resolution errors —
            # the primary is still useful information.
            fallback_chain = [{"provider": provider.name, "kind": provider.kind,
                               "model": model, "error": str(e)[:120]}]

    return {
        "agent_id": agent_id,
        "provider": _to_model(provider).dict() if provider else None,
        "model": model,
        "fallback_chain": fallback_chain,
    }


# ── Per-call AI config (named calls per agent) ─────────────────────────────

class AgentCallConfig(BaseModel):
    """One named call routing — a subset of the agent's AI traffic that goes
    to a specific provider + model. e.g., name="audit" → claude-cli/opus,
    name="summary" → ollama-local/qwen3:8b.
    """
    name: str                        # call name, e.g. "audit", "summary"
    provider: str                    # one of the registered providers
    model: str = ""                  # optional — defaults to provider default
    description: str = ""


class AgentCallsModel(BaseModel):
    agent_id: str
    calls: dict[str, AgentCallConfig] = Field(default_factory=dict)


@router.get("/agent-calls/{agent_id}", response_model=AgentCallsModel)
def get_agent_calls(agent_id: str):
    """Read the per-call AI routing for an agent."""
    from framework.core import registry as _reg
    m = _reg.get_agent(agent_id)
    if m is None:
        raise HTTPException(status_code=404, detail="agent not found")
    raw = ((m.metadata or {}).get("ai_calls") or {})
    calls = {
        k: AgentCallConfig(name=k, provider=v.get("provider", ""),
                           model=v.get("model", ""),
                           description=v.get("description", ""))
        for k, v in raw.items() if isinstance(v, dict)
    }
    return AgentCallsModel(agent_id=agent_id, calls=calls)


@router.put("/agent-calls/{agent_id}/{call_name}", response_model=AgentCallsModel)
def upsert_agent_call(agent_id: str, call_name: str, req: AgentCallConfig):
    """Add or update a named call routing. Mutates manifest.metadata.ai_calls."""
    if call_name != req.name:
        raise HTTPException(status_code=400, detail="path call_name must match body name")
    if not ai_providers.get_provider(req.provider):
        raise HTTPException(status_code=404, detail=f"provider {req.provider!r} not found")
    from framework.core import registry as _reg
    m = _reg.get_agent(agent_id)
    if m is None:
        raise HTTPException(status_code=404, detail="agent not found")
    md = dict(m.metadata or {})
    calls = dict(md.get("ai_calls") or {})
    calls[call_name] = {
        "provider": req.provider, "model": req.model,
        "description": req.description,
    }
    md["ai_calls"] = calls
    _reg.update_agent(agent_id, {"metadata": md})
    return get_agent_calls(agent_id)


@router.delete("/agent-calls/{agent_id}/{call_name}", response_model=AgentCallsModel)
def delete_agent_call(agent_id: str, call_name: str):
    """Remove a named call routing — falls back to agent default after this."""
    from framework.core import registry as _reg
    m = _reg.get_agent(agent_id)
    if m is None:
        raise HTTPException(status_code=404, detail="agent not found")
    md = dict(m.metadata or {})
    calls = dict(md.get("ai_calls") or {})
    if call_name not in calls:
        raise HTTPException(status_code=404, detail="call not configured")
    del calls[call_name]
    md["ai_calls"] = calls
    _reg.update_agent(agent_id, {"metadata": md})
    return get_agent_calls(agent_id)


# ── Claude Max pool ────────────────────────────────────────────────────────
# Reads ~/.reusable-agents/claude-pool/state.json (mounted read-only at
# /claude-pool inside the container) and dispatches profile tests via the
# host-worker trigger queue so the real claude binary runs on the host.

_POOL_ROOT = Path(os.getenv("CLAUDE_POOL_ROOT",
                            os.path.expanduser("~/.reusable-agents/claude-pool")))
_POOL_LOG_DIR = Path(os.getenv("AGENT_LOG_DIR", "/tmp/reusable-agents-logs"))
_POOL_QUEUE_DIR = Path(os.getenv("AGENT_TRIGGER_QUEUE_DIR", "/tmp/agent-trigger-queue"))
_POOL_PY = os.getenv("CLAUDE_POOL_PY",
    str(Path(__file__).parent.parent.parent.parent / "framework" / "cli" / "claude_pool.py"))
_POOL_TEST_AGENT_ID = "pool-test"


def _read_pool_state() -> dict:
    state_file = _POOL_ROOT / "state.json"
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def _profile_is_authenticated(home_host: str) -> bool:
    """Check creds file under the container-side pool root using the profile dir name."""
    profile_dir = Path(home_host).name  # e.g. "profile-1"
    return (_POOL_ROOT / profile_dir / ".claude" / ".credentials.json").exists()


def _profile_state_str(p: dict, authenticated: bool) -> str:
    if not authenticated:
        return "no-auth"
    rs = p.get("limit_resets_at") or ""
    if rs:
        try:
            when = datetime.fromisoformat(rs.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < when:
                return "rate-limited"
        except Exception:
            pass
    return "ready"


class PoolProfile(BaseModel):
    id: str
    home: str
    authenticated: bool
    in_use: int = 0
    total_uses: int = 0
    last_used_at: str = ""
    label: str = ""
    state: str
    limit_resets_at: str = ""
    limit_last_message: str = ""


@router.get("/claude-pool/profiles", response_model=list[PoolProfile])
def list_pool_profiles():
    """List all Claude Max pool profiles with live auth + rate-limit status."""
    state = _read_pool_state()
    profiles = []
    for k in sorted(k for k in state.keys() if not k.startswith("__")):
        p = state[k]
        if not isinstance(p, dict):
            continue
        authenticated = _profile_is_authenticated(p.get("home", ""))
        profiles.append(PoolProfile(
            id=p.get("id", k),
            home=p.get("home", ""),
            authenticated=authenticated,
            in_use=p.get("in_use", 0),
            total_uses=p.get("total_uses", 0),
            last_used_at=p.get("last_used_at", ""),
            label=p.get("label", ""),
            state=_profile_state_str(p, authenticated),
            limit_resets_at=p.get("limit_resets_at") or "",
            limit_last_message=(p.get("limit_last_message") or "")[:200],
        ))
    return profiles


class PoolTestJob(BaseModel):
    job_id: str
    log_path: str


@router.post("/claude-pool/test/{profile_id}", response_model=PoolTestJob)
def enqueue_pool_test(profile_id: str):
    """Dispatch a quick ping test for one pool profile via the host-worker queue.

    The host-worker runs `claude_pool.py test-profile <id>` and writes output to
    /tmp/reusable-agents-logs/pool-test-<job_id>.log. Poll GET
    /claude-pool/test-result/{job_id} for the outcome.
    """
    state = _read_pool_state()
    if profile_id not in state:
        raise HTTPException(status_code=404, detail=f"profile {profile_id!r} not found")

    run_id = f"{profile_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    _POOL_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    job = {
        "agent_id": _POOL_TEST_AGENT_ID,
        "run_id": run_id,
        "entry_command": f"python3 {_POOL_PY} test-profile {profile_id}",
        "triggered_by": "manual:pool-test",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    job_path = _POOL_QUEUE_DIR / f"{_POOL_TEST_AGENT_ID}-{run_id}.json"
    tmp = job_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2))
    tmp.rename(job_path)

    log_path = str(_POOL_LOG_DIR / f"{_POOL_TEST_AGENT_ID}-{run_id}.log")
    return PoolTestJob(job_id=run_id, log_path=log_path)


class PoolTestResult(BaseModel):
    done: bool
    ok: bool = False
    output: str = ""


@router.get("/claude-pool/test-result/{job_id}", response_model=PoolTestResult)
def get_pool_test_result(job_id: str):
    """Read the outcome of a dispatched pool test.

    Returns done=False while the host-worker is still running. Once the
    [pool-test] RESULT: line appears, done=True and ok reflects success.
    """
    if not re.match(r"^[\w.-]+$", job_id):
        raise HTTPException(status_code=400, detail="invalid job_id")

    log_path = _POOL_LOG_DIR / f"{_POOL_TEST_AGENT_ID}-{job_id}.log"
    if not log_path.exists():
        return PoolTestResult(done=False)

    try:
        content = log_path.read_text(errors="replace")
    except Exception:
        return PoolTestResult(done=False)

    done = bool(re.search(r"\[pool-test\] RESULT:", content))
    ok = bool(re.search(r"\[pool-test\] RESULT: ok", content))
    summary_lines = [ln for ln in content.splitlines() if "[pool-test]" in ln]
    return PoolTestResult(done=done, ok=ok, output="\n".join(summary_lines))


# ── Code-editor chain (used by the implementer, separate from chat providers) ─

class CodeEditorChainModel(BaseModel):
    chain: list[str]
    backends: dict[str, dict]


@router.get("/code-editor/config", response_model=CodeEditorChainModel)
def get_code_editor_config():
    """Return the implementer's code-editor backend chain.

    The implementer uses a SEPARATE provider system from chat agents
    (`framework.core.code_editor`, not `framework.core.ai_providers`).
    The chain is configured at storage path `config/code-editor-config.json`
    and falls back to DEFAULT_CONFIG in the module.

    The dashboard's Agent LLMs page surfaces this so operators can see
    the implementer's full routing in one place.
    """
    from framework.core.code_editor import load_config
    cfg = load_config()
    return CodeEditorChainModel(
        chain=list(cfg.get("default_chain") or []),
        backends=cfg.get("backends") or {},
    )
