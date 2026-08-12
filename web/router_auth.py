#!/usr/bin/env python3

"""
Authentication router — ``POST /api/auth/login``.

Provides a simple Bearer-token login endpoint with rate limiting.
The returned token must be sent as ``Authorization: Bearer <token>``
on subsequent protected ``/api/*`` requests.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .deps import get_settings
from .middleware import RateLimiter, SESSION_COOKIE_NAME

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Rate limiter — 5 attempts per 60 s per IP
# ---------------------------------------------------------------------------

_login_limiter = RateLimiter(max_requests=5, window_seconds=60)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Login request body — the pre-shared auth key."""

    auth_key: str = Field(min_length=1, description="Pre-shared authentication key")


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


@router.post("/login")
async def login(request: Request, response: Response, body: LoginRequest) -> dict[str, str]:
    """Authenticate with the pre-shared auth key.

    On success, returns the Bearer token for API clients AND sets the
    ``harvester_session`` httpOnly cookie so browser navigation to the
    web UI works without manually attaching headers.  On failure,
    returns 401.  Rate-limited at 5 attempts per 60 seconds per IP.

    Example request body:

    .. code-block:: json

        {"auth_key": "your-secret-key"}
    """
    # Resolve client IP (honour X-Forwarded-For if present)
    client_ip = (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )

    # Rate-limit check
    _login_limiter.check(client_ip)

    settings = get_settings()

    # Constant-time comparison against the configured auth key
    if not secrets.compare_digest(body.auth_key, settings.web_auth_key):
        raise HTTPException(status_code=401, detail="Invalid authentication key")

    # Set the UI session cookie (httpOnly, path-scoped to the app root)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=settings.web_auth_key,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,  # 7 days
        path="/",
    )

    return {"token": settings.web_auth_key}
