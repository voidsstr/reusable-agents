"""Google OAuth 2.0 flow.

Endpoints:
  GET  /api/auth/google/login    — start: redirect to Google's consent screen
  GET  /api/auth/google/callback — Google calls back here with ?code=...
  POST /api/auth/logout          — clear session cookie
  GET  /api/auth/me              — current session user (or 401)

Requires env: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
ALLOWED_EMAILS (comma-separated), SESSION_SECRET (32+ bytes).

The redirect URI registered with Google must be:
  <PUBLIC_BASE_URL>/api/auth/google/callback

If PUBLIC_BASE_URL is unset, it's derived from the incoming request's
scheme + host. Behind a TLS-terminating proxy (Container Apps, nginx),
make sure X-Forwarded-Proto is honored.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth import allowed_emails, oauth_enabled


logger = logging.getLogger("framework.api.oauth")

router = APIRouter()

# Module-level OAuth client; lazily configured on first use so that
# importing this module doesn't fail when the env isn't set.
_oauth: Optional[OAuth] = None


def _get_oauth() -> OAuth:
    global _oauth
    if _oauth is not None:
        return _oauth
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="OAuth is not configured")
    o = OAuth()
    o.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    _oauth = o
    return o


def _redirect_uri(request: Request) -> str:
    """Build the absolute callback URL. Honors PUBLIC_BASE_URL if set
    (essential when the API sits behind TLS-terminating ingress that
    doesn't forward the original scheme reliably)."""
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return f"{base}/api/auth/google/callback"
    # Fallback: derive from request. Works for local dev.
    return str(request.url_for("oauth_callback"))


@router.get("/api/auth/google/login")
async def oauth_login(request: Request, next: str = "/"):
    """Redirect the browser to Google's consent screen."""
    if not oauth_enabled():
        raise HTTPException(status_code=503, detail="OAuth is not configured")
    oauth = _get_oauth()
    # Stash the post-login destination so the callback can redirect back
    request.session["post_login_redirect"] = next
    redirect_uri = _redirect_uri(request)
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/api/auth/google/callback", name="oauth_callback")
async def oauth_callback(request: Request):
    """Google sends the user back here with ?code=...; exchange for a
    token, verify the email, set the session cookie, then redirect to
    the original target (or '/')."""
    if not oauth_enabled():
        raise HTTPException(status_code=503, detail="OAuth is not configured")
    oauth = _get_oauth()
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        logger.warning(f"oauth callback error: {e}")
        raise HTTPException(status_code=400, detail=f"oauth failed: {e.error}")

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").lower()
    if not email:
        raise HTTPException(status_code=400, detail="no email returned from Google")
    if not userinfo.get("email_verified"):
        raise HTTPException(status_code=403, detail="email is not verified")

    if email not in allowed_emails():
        logger.warning(f"oauth: rejected unauthorized email {email}")
        # Don't leak the allowlist in the response; just say no.
        raise HTTPException(status_code=403, detail=f"{email} is not authorized")

    request.session["email"] = email
    request.session["name"] = userinfo.get("name") or email
    request.session["picture"] = userinfo.get("picture") or ""

    target = request.session.pop("post_login_redirect", "/") or "/"
    # Defensive: only allow same-origin redirects
    if not target.startswith("/"):
        target = "/"
    return RedirectResponse(url=target, status_code=302)


@router.post("/api/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})


@router.get("/api/auth/me")
async def me(request: Request):
    email = request.session.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="not signed in")
    return {
        "email": email,
        "name": request.session.get("name", email),
        "picture": request.session.get("picture", ""),
    }
