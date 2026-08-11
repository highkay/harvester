#!/usr/bin/env python3

"""Pydantic v2 data models for FastAPI request/response schemas.

Token management, gpt-load config, provider mapping, and schedule models.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Token masking utility
# ---------------------------------------------------------------------------


def mask_token(value: str) -> str:
    """Return a human-safe masked token for display.

    - Length ≥ 12 → ``value[:6] + "..." + value[-4:]``
    - Shorter → ``"***"``
    """
    if len(value) < 12:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


class TokenCreate(BaseModel):
    """Request body for creating a single GitHub token."""

    model_config = ConfigDict(frozen=True)

    token_type: Literal["api", "session"]
    token_value: str = Field(min_length=8)
    label: str = ""


class TokenOut(BaseModel):
    """Response body for a stored GitHub token (masked)."""

    model_config = ConfigDict(frozen=True)

    id: int
    token_type: str
    token_masked: str
    label: str
    enabled: bool
    created_at: str


class TokenBulkImport(BaseModel):
    """Request body for bulk-importing tokens (one per line)."""

    model_config = ConfigDict(frozen=True)

    token_type: Literal["api", "session"]
    tokens_text: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# gpt-load config
# ---------------------------------------------------------------------------


class GptLoadConfigCreate(BaseModel):
    """Request body for adding a gpt-load instance configuration."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    auth_key: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Provider ↔ gpt-load mapping
# ---------------------------------------------------------------------------


class MappingUpdate(BaseModel):
    """Request body for updating a provider-to-group mapping."""

    model_config = ConfigDict(frozen=True)

    gpt_load_config_id: int
    group_id: int


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


class CronUpdate(BaseModel):
    """Request body for updating a provider's cron schedule."""

    model_config = ConfigDict(frozen=True)

    cron_expression: str = Field(min_length=1)
    enabled: bool
