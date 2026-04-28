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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    auth_enabled, check_request_auth, get_expected_token, is_public_path,
    oauth_enabled,
)
from .routes import (
    agents as agents_routes,
    confirmations as confirmations_routes,
    dependencies as dependencies_routes,
    directives as directives_routes,
    dispatch as dispatch_routes,
    goals as goals_routes,
    events as events_routes,
    messages as messages_routes,
    oauth as oauth_routes,
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
    if not auth_enabled():
        logger.warning(
            "Auth is DISABLED — neither FRAMEWORK_API_TOKEN nor "
            "GOOGLE_OAUTH_CLIENT_ID is set. Do NOT expose this service "
            "beyond localhost."
        )
    else:
        modes = []
        if get_expected_token():
            modes.append("bearer-token")
        if oauth_enabled():
            modes.append("google-oauth")
        logger.info(f"auth modes enabled: {', '.join(modes)}")
    # Verify storage backend is reachable
    try:
        from framework.core.storage import get_storage
        s = get_storage()
        logger.info(f"storage backend: {s.name}")
    except Exception as e:
        logger.error(f"storage backend init failed: {e}")
        raise
    # Start the snapshot updater background thread — it writes a single
    # registry/agent-snapshot.json blob every 5s so GET /api/agents can
    # serve from one read instead of N status.json reads.
    try:
        from framework.core import snapshot_updater
        interval = float(os.getenv("SNAPSHOT_UPDATER_INTERVAL_S", "5.0"))
        snapshot_updater.start(interval_s=interval)
        logger.info(f"snapshot_updater started (interval={interval}s)")
    except Exception as e:
        logger.warning(f"snapshot_updater start failed: {e}")
    # Startup ghost-run reap: any agent that was mid-run when the API last
    # restarted (or whose host-worker died) gets flipped to failure here.
    try:
        from framework.core.ghost_reaper import reap_all
        reaped = reap_all()
        if reaped:
            logger.warning(f"ghost-reaper: flipped {len(reaped)} stale runs to failure: {reaped}")
        else:
            logger.info("ghost-reaper: no stale runs at startup")
    except Exception as e:
        logger.warning(f"ghost-reaper startup sweep failed: {e}")
    # Periodic ghost-run sweep — every 60s, catch agents whose host-worker
    # died mid-run between API list calls.
    try:
        from framework.core import ghost_reaper as _gr
        import threading
        def _sweep_loop():
            import time
            while True:
                time.sleep(60)
                try:
                    _gr.reap_all()
                except Exception:
                    pass
        threading.Thread(target=_sweep_loop, daemon=True, name="ghost-reaper").start()
        logger.info("ghost-reaper periodic sweep started (60s)")
    except Exception as e:
        logger.warning(f"ghost-reaper periodic sweep start failed: {e}")
    try:
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop)
    except Exception as e:
        logger.warning(f"could not install signal handlers: {e}")
    logger.info("framework API ready")
    yield
    logger.info("framework API shutting down...")
    try:
        from framework.core import snapshot_updater
        snapshot_updater.stop()
    except Exception as e:
        logger.warning(f"snapshot_updater stop failed: {e}")


app = FastAPI(
    title="reusable-agents Framework API",
    description=(
        "Agent registry, run history, status, messaging, confirmations, and "
        "event log. Backed by Azure Blob Storage (or LocalFilesystem for dev)."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware order (Starlette: LIFO — last added runs FIRST on the inbound
# request). We want: SessionMiddleware → auth_middleware → endpoint, so
# request.session is populated by the time auth runs. Therefore:
#   1. Define auth_middleware via decorator FIRST (executed at module load).
#   2. Then add_middleware(SessionMiddleware) — which gets added LATER and
#      thus runs FIRST on the request.

# (1) Auth gate. Raises 401 / redirects to login as appropriate.
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if is_public_path(request.url.path):
        return await call_next(request)
    if not auth_enabled():
        return await call_next(request)
    try:
        check_request_auth(request)
    except Exception as e:
        # Browser navigation (Accept: text/html) → redirect to login.
        accept = request.headers.get("Accept", "")
        if oauth_enabled() and "text/html" in accept and request.method == "GET":
            from fastapi.responses import RedirectResponse
            from urllib.parse import quote
            return RedirectResponse(
                url=f"/api/auth/google/login?next={quote(request.url.path)}",
                status_code=302,
            )
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# (2) CORS — iframe-friendly. Runs after Session (inbound) so Session is
# already populated. Pin via FRAMEWORK_API_CORS env (comma-separated).
_cors_env = os.getenv("FRAMEWORK_API_CORS", "*")
_cors_origins = [o.strip() for o in _cors_env.split(",")] if _cors_env != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# (3) Session — added LAST so it runs FIRST and populates request.session
# before auth_middleware reads it.
_session_secret = os.getenv("SESSION_SECRET", "")
if oauth_enabled() and len(_session_secret) < 32:
    raise RuntimeError(
        "SESSION_SECRET must be set to at least 32 random bytes when "
        "GOOGLE_OAUTH_CLIENT_ID is configured. Generate with: openssl rand -hex 32"
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret or "dev-only-not-secret-do-not-use-in-prod",
    session_cookie="ra_session",
    https_only=oauth_enabled(),  # Secure cookie in prod
    same_site="lax",             # allow OAuth redirect carry-through
    max_age=60 * 60 * 24 * 14,   # 14 days
)


# ---- Routes ----
for r in (
    oauth_routes.router,  # FIRST so /api/auth/* paths aren't shadowed by dynamic routes
    # dependencies_routes must come BEFORE agents_routes — its
    # /api/agents/dependencies path would otherwise match the
    # /api/agents/{agent_id} catch-all in agents_routes.
    dependencies_routes.router,
    dispatch_routes.router,
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
        "auth_enabled": auth_enabled(),
        "auth_modes": [
            *(["bearer-token"] if get_expected_token() else []),
            *(["google-oauth"] if oauth_enabled() else []),
        ],
    }


@app.exception_handler(404)
async def not_found_handler(_request, _exc):
    return JSONResponse({"detail": "Not Found"}, status_code=404)
