#!/usr/bin/env python3

"""
Dependency-injection helpers for the web layer.

Provides FastAPI-compatible dependency functions for settings and
authentication.  The ``get_current_user`` dependency validates Bearer
tokens against the configured ``web_auth_key``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from tools.logger import get_logger

from .config import WebSettings
from .middleware import authenticate_either

logger = get_logger("web.deps")

# ---------------------------------------------------------------------------
# Singleton cache for settings (avoid re-parsing env on every request)
# ---------------------------------------------------------------------------
_settings: WebSettings | None = None


def get_settings() -> WebSettings:
    """Return the global WebSettings instance (lazy singleton)."""
    global _settings
    if _settings is None:
        _settings = WebSettings()
        logger.info(f"WebSettings initialised: host={_settings.host} port={_settings.port}")
    return _settings


# ---------------------------------------------------------------------------
# Bearer token authentication dependency
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> bool:
    """Validate authentication via Bearer header OR session cookie.

    Called via ``Depends(get_current_user)`` on protected endpoints.
    FastAPI auto-injects the ``Request`` object.

    - API clients authenticate with ``Authorization: Bearer <web_auth_key>``.
    - Browser UI authenticates with the ``harvester_session`` cookie set
      by ``POST /api/auth/login``.

    Returns ``True`` if either channel is valid, otherwise raises
    ``HTTPException(401)``.
    """
    settings = get_settings()
    return authenticate_either(request, settings.web_auth_key)
