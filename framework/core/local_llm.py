"""Fleet local-LLM contract: one resident Ollama model at one context size.

Every framework call site that talks to the local Ollama daemon gets its
model, `options.num_ctx` and endpoint from here, so the fleet cannot drift
back to a mix of models.

Why one model at one num_ctx: Ollama reloads a model whenever a request
names a different model OR a different `options.num_ctx`. On 2026-09-23
that churn was 103 cold loads a day, and those 10-20 GB load/unload swings
are the suspected trigger of the RTX 5090 Xid-79 bus drops. A single model
(qwen3.8:27b handles text AND vision; it won or tied every role in the
2026-09-23 blind benchmark) requested at a single num_ctx never reloads.

Env knobs (all optional; the code defaults ARE the fleet decision):
  FLEET_LOCAL_MODEL         the one resident model          (qwen3.8:27b)
  OLLAMA_NUM_CTX            the one context size            (65536)
  FLEET_LOCAL_MODEL_STRICT  "1" (default): a request for any other model,
                            or for a non-local Ollama host, is rewritten to
                            the fleet model on 127.0.0.1:11434 and logged
                            once. "0" honours the caller's model and host.

Strict is the default because the stale values live in storage config
(`config/ai-defaults.json` agent overrides, provider `default_model`,
manifest `ai_calls`) that code cannot fix, and one stale caller is enough
to bring the reload churn back.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger("framework.local_llm")

DEFAULT_MODEL = "qwen3.8:27b"
DEFAULT_NUM_CTX = 65536
DEFAULT_BASE_URL = "http://127.0.0.1:11434"

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_warned: set[tuple[str, str]] = set()


def fleet_model() -> str:
    return (os.environ.get("FLEET_LOCAL_MODEL") or "").strip() or DEFAULT_MODEL


def fleet_num_ctx() -> int:
    try:
        v = int(os.environ.get("OLLAMA_NUM_CTX", str(DEFAULT_NUM_CTX)))
    except ValueError:
        v = DEFAULT_NUM_CTX
    return v if v > 0 else DEFAULT_NUM_CTX


def strict() -> bool:
    return os.environ.get("FLEET_LOCAL_MODEL_STRICT", "1").strip().lower() not in (
        "0", "false", "no", "off")


def is_local_url(url: str) -> bool:
    """True for an empty URL (callers default it to local) or a loopback host."""
    if not (url or "").strip():
        return True
    host = urlparse(url if "://" in url else f"http://{url}").hostname or ""
    return host in _LOCAL_HOSTS or host.startswith("127.")


def _warn_once(kind: str, requested: str, used: str, caller: str) -> None:
    key = (kind, requested)
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "local-llm: %s %r requested%s; using %r (fleet runs one resident "
        "model on the local daemon; FLEET_LOCAL_MODEL_STRICT=0 to honour it)",
        kind, requested, f" by {caller}" if caller else "", used,
    )


def resolve_model(requested: str = "", *, caller: str = "") -> str:
    """The model to actually send to Ollama for a caller asking for `requested`."""
    fleet = fleet_model()
    req = (requested or "").strip()
    if not req or req == fleet or not strict():
        return req or fleet
    _warn_once("model", req, fleet, caller)
    return fleet


def resolve_base_url(requested: str = "", *, caller: str = "") -> str:
    """The Ollama base URL to use. Remote hosts (the retired RTX 4080 box at
    192.168.1.82) are replaced by the local daemon unless strict is off."""
    req = (requested or "").strip().rstrip("/")
    if not req:
        return DEFAULT_BASE_URL
    if is_local_url(req) or not strict():
        return req
    _warn_once("ollama host", req, DEFAULT_BASE_URL, caller)
    return DEFAULT_BASE_URL
