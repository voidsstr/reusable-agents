"""Token auth for the framework API.

Token comes from FRAMEWORK_API_TOKEN env var. The frontend / dashboard /
agents send `Authorization: Bearer <token>`. If FRAMEWORK_API_TOKEN is unset,
auth is *disabled* (dev-mode only — log a warning at startup).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import HTTPException, Request


logger = logging.getLogger("framework.api.auth")


def get_expected_token() -> Optional[str]:
    return os.getenv("FRAMEWORK_API_TOKEN")


async def require_token(request: Request) -> None:
    """FastAPI dependency. Raises 401 unless the bearer token matches.
    No-op when FRAMEWORK_API_TOKEN is unset (dev mode)."""
    expected = get_expected_token()
    if not expected:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth[7:].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")
