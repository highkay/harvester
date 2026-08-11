#!/usr/bin/env python3

"""Token management API routes — ``/api/tokens``.

All endpoints accept a placeholder *user* dependency (T9 will replace
with real authentication).  Token values are **never** returned in plaintext
except through :meth:`TokenService.get_enabled_tokens` which is only consumed
by the pipeline engine.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .deps import get_current_user, get_settings
from .models import TokenBulkImport, TokenCreate, TokenOut
from .token_service import get_token_service

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


# ---------------------------------------------------------------------------
# Helper — resolve the token service singleton
# ---------------------------------------------------------------------------


def _svc():
    """Return the TokenService singleton, resolving db_path from settings."""
    settings = get_settings()
    return get_token_service(settings.db_path)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TokenOut])
async def list_tokens(user: bool = Depends(get_current_user)):
    """List all stored tokens (masked — plaintext is never returned)."""
    svc = _svc()
    rows = await svc.list_tokens()
    return [TokenOut(**row) for row in rows]


@router.post("", response_model=TokenOut, status_code=201)
async def create_token(
    body: TokenCreate, user: bool = Depends(get_current_user)
):
    """Add a single GitHub token (api or session)."""
    svc = _svc()
    try:
        result = await svc.add_token(
            token_type=body.token_type,
            token_value=body.token_value,
            label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return TokenOut(
        id=result["id"],
        token_type=body.token_type,
        token_masked=result["token_masked"],
        label=body.label,
        enabled=True,
        created_at="",
    )


@router.post("/import")
async def import_tokens(
    body: TokenBulkImport, user: bool = Depends(get_current_user)
):
    """Bulk-import tokens (one per line)."""
    svc = _svc()
    result = await svc.add_tokens_bulk(
        token_type=body.token_type,
        tokens_text=body.tokens_text,
    )
    return result


@router.patch("/{token_id}")
async def toggle_token(
    token_id: int,
    body: dict,
    user: bool = Depends(get_current_user),
):
    """Enable or disable a token by id."""
    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(status_code=400, detail="Missing 'enabled' field")

    svc = _svc()
    ok = await svc.set_token_enabled(token_id, bool(enabled))
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"id": token_id, "enabled": enabled}


@router.delete("/{token_id}")
async def delete_token(
    token_id: int, user: bool = Depends(get_current_user)
):
    """Delete a token by id."""
    svc = _svc()
    ok = await svc.delete_token(token_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"id": token_id, "deleted": True}


@router.get("/stats")
async def token_stats(user: bool = Depends(get_current_user)):
    """Return token counts from the database (not runtime state)."""
    svc = _svc()
    return await svc.get_stats()
