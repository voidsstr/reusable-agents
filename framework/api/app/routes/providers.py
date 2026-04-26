"""AI provider config CRUD + per-agent assignments.

The dashboard UI uses this to:
  - List providers + which one is the default
  - Add / edit / delete providers (Azure OpenAI, Anthropic, Ollama, etc.)
  - Set the global default provider + model
  - Set per-agent overrides ("for SEO Implementer, use claude-opus-4-7")
"""
from __future__ import annotations

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
    """What provider + model would be used if `agent_id` called ai_client()?"""
    provider, model = ai_providers.resolve_for_agent(agent_id)
    return {
        "agent_id": agent_id,
        "provider": _to_model(provider).dict() if provider else None,
        "model": model,
    }
