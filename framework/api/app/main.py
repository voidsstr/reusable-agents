"""reusable-agents framework API service.

FastAPI app with lifespan, token auth, CORS for iframe embedding, and
nine route modules covering agents, runs, status, directives, messages,
responses, confirmations, events, and storage browsing. Plus a WebSocket
endpoint that pushes live status updates to the UI.

Pattern lifted from nsc-assistant/dashboard/backend/main.py — same lifespan
+ signal handler shape, but with a much simpler init (no DB, no agent
discovery sidecar, no health monitor — the framework core IS the surface).

Run:
  uvicorn framework.api.app.main:app --host 0.0.0.0 --port 8090

Or via Docker (see framework/api/Dockerfile + framework/docker-compose.yml).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import get_expected_token
from .routes import (
    agents as agents_routes,
    confirmations as confirmations_routes,
    dependencies as dependencies_routes,
    directives as directives_routes,
    goals as goals_routes,
    events as events_routes,
    messages as messages_routes,
    providers as providers_routes,
    responses as responses_routes,
    runs as runs_routes,
    scripts as scripts_routes,
    status as status_routes,
    storage as storage_routes,
)
from .websocket import router as ws_router


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("framework.api")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop):
    """Install graceful-shutdown handlers (mirrors dashboard/backend/main.py)."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: logger.info(f"received {s.name}"))
        except (NotImplementedError, RuntimeError):
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("framework API starting up...")
    if not get_expected_token():
        logger.warning(
            "FRAMEWORK_API_TOKEN is not set — API auth is DISABLED. "
            "Set the env var before exposing this service to anything beyond localhost."
        )
    # Verify storage backend is reachable
    try:
        from framework.core.storage import get_storage
        s = get_storage()
        logger.info(f"storage backend: {s.name}")
    except Exception as e:
        logger.error(f"storage backend init failed: {e}")
        raise
    try:
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop)
    except Exception as e:
        logger.warning(f"could not install signal handlers: {e}")
    logger.info("framework API ready")
    yield
    logger.info("framework API shutting down...")


app = FastAPI(
    title="reusable-agents Framework API",
    description=(
        "Agent registry, run history, status, messaging, confirmations, and "
        "event log. Backed by Azure Blob Storage (or LocalFilesystem for dev)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — iframe-friendly. Pin specific origins via FRAMEWORK_API_CORS env (comma-separated).
_cors_env = os.getenv("FRAMEWORK_API_CORS", "*")
_cors_origins = [o.strip() for o in _cors_env.split(",")] if _cors_env != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Routes ----
for r in (
    # dependencies_routes must come BEFORE agents_routes — its
    # /api/agents/dependencies path would otherwise match the
    # /api/agents/{agent_id} catch-all in agents_routes.
    dependencies_routes.router,
    goals_routes.router,
    agents_routes.router,
    runs_routes.router,
    status_routes.router,
    directives_routes.router,
    messages_routes.router,
    providers_routes.router,
    responses_routes.router,
    scripts_routes.router,
    confirmations_routes.router,
    events_routes.router,
    storage_routes.router,
    ws_router,
):
    app.include_router(r)


@app.get("/api/health")
async def health():
    from framework.core import storage
    s = storage.get_storage()
    return {
        "status": "ok",
        "version": app.version,
        "storage_backend": s.name,
        "auth_enabled": bool(get_expected_token()),
    }


@app.exception_handler(404)
async def not_found_handler(_request, _exc):
    return JSONResponse({"detail": "Not Found"}, status_code=404)
