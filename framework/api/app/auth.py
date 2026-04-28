"""Auth for the framework API — supports two modes simultaneously:

1. **Bearer token** (legacy): `Authorization: Bearer <FRAMEWORK_API_TOKEN>`.
   Used by CLI tools, agents, register-agents.sh. Bypasses the OAuth flow.

2. **Google OAuth + session cookie** (interactive users):
   `/api/auth/google/login` → Google → `/api/auth/google/callback` →
   signed session cookie. Email must be in ALLOWED_EMAILS.

A request is authorized if EITHER auth mode succeeds. If neither
FRAMEWORK_API_TOKEN nor GOOGLE_OAUTH_CLIENT_ID is set, auth is disabled
(dev mode — logs a warning at startup).

Env vars:
  FRAMEWORK_API_TOKEN       — bearer token for service-to-service calls
  GOOGLE_OAUTH_CLIENT_ID    — Google OAuth 2.0 client id
  GOOGLE_OAUTH_CLIENT_SECRET — Google OAuth 2.0 client secret
  ALLOWED_EMAILS            — comma-separated email allowlist
  SESSION_SECRET            — 32+ byte random for cookie signing
  PUBLIC_BASE_URL           — full https URL of this deployment, used to
                              build the Google redirect URI
                              (default: derived from request.url)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import HTTPException, Request


logger = logging.getLogger("framework.api.auth")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_expected_token() -> Optional[str]:
    return os.getenv("FRAMEWORK_API_TOKEN")


def oauth_enabled() -> bool:
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"))


def auth_enabled() -> bool:
    return bool(get_expected_token() or oauth_enabled())


def allowed_emails() -> set[str]:
    raw = os.getenv("ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# ---------------------------------------------------------------------------
# Auth check (used as middleware AND as FastAPI dependency)
# ---------------------------------------------------------------------------

# Routes that bypass auth: the OAuth flow itself, healthchecks, and the
# UI's static assets (the SPA bundle is public; protected data is fetched
# via /api/* which IS gated).
_PUBLIC_PREFIXES = (
    "/api/auth/",
    "/api/health",
    "/healthz",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/static/",
    "/assets/",
    "/favicon",
)


def is_public_path(path: str) -> bool:
    if path == "/" or path == "/index.html":
        return True
    for p in _PUBLIC_PREFIXES:
        if path.startswith(p):
            return True
    return False


def check_request_auth(request: Request) -> Optional[str]:
    """Return the authenticated principal (email or 'service-token') if
    auth passes, None if no auth is configured. Raise 401 if auth is
    configured but fails.

    Order: bearer token first (cheapest), then session cookie.
    """
    if not auth_enabled():
        return None

    # 1. Bearer token
    expected = get_expected_token()
    if expected:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
            if token == expected:
                return "service-token"
            # Wrong token — explicit 401, don't fall through to session
            raise HTTPException(status_code=401, detail="invalid bearer token")

    # 2. Session cookie (set by OAuth callback)
    session = getattr(request, "session", None)
    if session is not None:
        email = session.get("email")
        if email and email.lower() in allowed_emails():
            return email
        if email:
            # Session present but email not allowed — boot them out
            raise HTTPException(status_code=403, detail=f"{email} is not authorized")

    raise HTTPException(status_code=401, detail="authentication required")


async def require_token(request: Request) -> None:
    """FastAPI dependency. Kept for backward compat with routes that
    explicitly Depends(require_token). Modern routes don't need this —
    the auth middleware in main.py covers everything."""
    check_request_auth(request)
