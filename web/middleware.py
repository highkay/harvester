#!/usr/bin/env python3

"""
Web-layer security middleware — Bearer authentication and rate limiting.

Provides:
- :func:`authenticate_bearer` — constant-time Bearer token validation
- :class:`RateLimiter` — in-memory sliding-window IP rate limiter
"""

from __future__ import annotations

import secrets
import time

from fastapi import HTTPException, Request


# ---------------------------------------------------------------------------
# Bearer token authentication
# ---------------------------------------------------------------------------


def authenticate_bearer(request: Request, expected_token: str) -> bool:
    """Validate a Bearer token in the ``Authorization`` header.

    Uses :func:`secrets.compare_digest` for constant-time comparison to
    prevent timing side-channel attacks.

    Args:
        request: The FastAPI request object.
        expected_token: The expected token value to compare against.

    Returns:
        ``True`` if the token is valid.

    Raises:
        HTTPException(401): If the header is missing, malformed, or the
            token does not match.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401, detail="Invalid Authorization header format"
        )

    provided_token = parts[1]
    if not provided_token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")

    if not secrets.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid token")

    return True


# Session cookie name used by the web UI login flow.
SESSION_COOKIE_NAME = "harvester_session"


def authenticate_session(request: Request, expected_token: str) -> bool:
    """Validate the web-UI session cookie.

    The cookie carries the same pre-shared auth key as the Bearer token;
    it is set by ``POST /api/auth/login`` (httpOnly) so browser-based
    page navigation works without manually attaching headers.

    Args:
        request: The FastAPI request object.
        expected_token: The expected token value to compare against.

    Returns:
        ``True`` if the cookie matches.

    Raises:
        HTTPException(401): If the cookie is missing or does not match.
    """
    provided = request.cookies.get(SESSION_COOKIE_NAME)
    if not provided:
        raise HTTPException(
            status_code=401, detail="Not logged in — visit /login first"
        )
    if not secrets.compare_digest(provided, expected_token):
        raise HTTPException(status_code=401, detail="Invalid session")
    return True


def authenticate_either(request: Request, expected_token: str) -> bool:
    """Accept either a valid Bearer header or a valid session cookie.

    Used by ``get_current_user`` so that API clients may authenticate
    with ``Authorization: Bearer <key>`` while browsers authenticate via
    the ``harvester_session`` cookie set at login.

    Returns ``True`` on success, otherwise raises ``HTTPException(401)``
    with a message explaining which channel failed.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header:
        return authenticate_bearer(request, expected_token)

    return authenticate_session(request, expected_token)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """In-memory sliding-window rate limiter keyed by client IP.

    Tracks timestamps of attempts within a configurable window.
    When the number of attempts exceeds the maximum within the window,
    subsequent :meth:`check` calls raise ``HTTPException(429)``.

    **Not safe for multi-process deployments** — a distributed rate
    limiter (e.g. Redis-based) is recommended for production.
    """

    __slots__ = ("_max_requests", "_window_seconds", "_attempts")

    def __init__(
        self, max_requests: int = 5, window_seconds: int = 60
    ) -> None:
        """Initialise the rate limiter.

        Args:
            max_requests: Maximum allowed attempts within the window.
            window_seconds: Sliding window duration in seconds.
        """
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, client_ip: str) -> None:
        """Allow the request through or raise 429 if rate-limited.

        Args:
            client_ip: The client's IP address (from ``request.client.host``
                or ``X-Forwarded-For`` header).

        Raises:
            HTTPException(429): If the client has exceeded the rate limit.
        """
        now = time.monotonic()
        cutoff = now - self._window_seconds

        # Retrieve existing timestamps and prune expired ones
        timestamps = self._attempts.get(client_ip)
        if timestamps is None:
            timestamps = []
        else:
            timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= self._max_requests:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
            )

        timestamps.append(now)
        self._attempts[client_ip] = timestamps

    def reset(self) -> None:
        """Clear all stored attempt records.

        Intended for test isolation — not for production use.
        """
        self._attempts.clear()
