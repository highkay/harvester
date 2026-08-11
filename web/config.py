#!/usr/bin/env python3

"""
Web layer configuration.

Uses plain dataclass + os.environ (no pydantic-settings dependency)
to keep the scaffold lightweight.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from tools.logger import get_logger

logger = get_logger("web.config")


def _generate_auth_key() -> str:
    """Generate a random hex auth key and emit a warning."""
    key = secrets.token_hex(32)
    logger.warning(
        f"WEB_AUTH_KEY not set — generated random key (length={len(key)}). "
        "Set WEB_AUTH_KEY in the environment for persistence across restarts."
    )
    return key


def _default_db_path() -> str:
    """Return the default database path: <workspace>/harvester.db."""
    workspace = os.getenv("HARVESTER_WORKSPACE", str(Path.cwd() / "data"))
    return str(Path(workspace) / "harvester.db")


@dataclass
class WebSettings:
    """Web-server configuration read from environment variables.

    All fields have sensible defaults; secrets can be set via env.
    """

    # --- Auth ---
    web_auth_key: str = field(
        default_factory=lambda: os.getenv("WEB_AUTH_KEY") or _generate_auth_key()
    )

    # --- GPT Load service ---
    gpt_load_base_url: str = field(
        default_factory=lambda: os.getenv(
            "GPT_LOAD_BASE_URL", "http://192.168.1.18:43001"
        )
    )
    gpt_load_auth_key: str = field(
        default_factory=lambda: os.getenv("GPT_LOAD_AUTH_KEY", "")
    )

    # --- Database ---
    db_path: str = field(
        default_factory=lambda: os.getenv("HARVESTER_DB_PATH") or _default_db_path()
    )

    # --- Server ---
    host: str = field(
        default_factory=lambda: os.getenv("WEB_HOST", "0.0.0.0")
    )
    port: int = field(
        default_factory=lambda: int(os.getenv("WEB_PORT", "8000"))
    )

    # --- CORS ---
    cors_origins: list[str] = field(
        default_factory=lambda: os.getenv(
            "WEB_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
        ).split(",")
    )

    # --- Encryption ---
    encryption_key: str | None = field(
        default_factory=lambda: os.getenv("ENCRYPTION_KEY") or None
    )
