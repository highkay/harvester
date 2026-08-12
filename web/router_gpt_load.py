#!/usr/bin/env python3

"""gpt-load management API — ``/api/gpt-load`` and ``/api/mappings``.

Manages gpt-load instance configurations, proxies group listings,
and provides provider-to-group mapping CRUD.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
import requests
from fastapi import APIRouter, Depends, HTTPException

from tools.logger import get_logger

from .crypto import decrypt_str, encrypt_str
from .db import get_db, resolve_db_path
from .deps import get_current_user
from .models import GptLoadConfigCreate, MappingUpdate

logger = get_logger("web.router_gpt_load")

router = APIRouter(prefix="/api/gpt-load", tags=["gpt-load"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_auth_key(auth_key: str) -> str:
    """Mask an AUTH_KEY for display. Shows first 4 + last 4 chars."""
    if len(auth_key) <= 8:
        return "***"
    return f"{auth_key[:4]}...{auth_key[-4:]}"


# ---------------------------------------------------------------------------
# GET /api/gpt-load — list all gpt-load configs (auth_key masked)
# ---------------------------------------------------------------------------


@router.get("")
async def list_configs(
    _user: bool = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all gpt-load instance configurations (auth_key masked)."""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT id, name, base_url, auth_key_encrypted, enabled, "
            "created_at, updated_at FROM gpt_load_config ORDER BY id"
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            try:
                plain = decrypt_str(d["auth_key_encrypted"])
                d["auth_key_masked"] = _mask_auth_key(plain)
            except ValueError:
                d["auth_key_masked"] = "***"
            del d["auth_key_encrypted"]
            result.append(d)
        return result
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# POST /api/gpt-load — add a new gpt-load config
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_config(
    body: GptLoadConfigCreate,
    _user: bool = Depends(get_current_user),
) -> dict[str, Any]:
    """Add a new gpt-load instance configuration.

    The *auth_key* is encrypted before storage.
    """
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        auth_enc = encrypt_str(body.auth_key)
        cursor = await db.execute(
            "INSERT INTO gpt_load_config (name, base_url, auth_key_encrypted) "
            "VALUES (?, ?, ?)",
            (body.name, body.base_url, auth_enc),
        )
        await db.commit()
        new_id = cursor.lastrowid

        return {
            "id": new_id,
            "name": body.name,
            "base_url": body.base_url,
            "auth_key_masked": _mask_auth_key(body.auth_key),
        }
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/gpt-load/{id} — get a single config
# ---------------------------------------------------------------------------


@router.get("/{config_id}")
async def get_config(
    config_id: int,
    _user: bool = Depends(get_current_user),
) -> dict[str, Any]:
    """Get a single gpt-load configuration by ID."""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT id, name, base_url, auth_key_encrypted, enabled, "
            "created_at, updated_at FROM gpt_load_config WHERE id = ?",
            (config_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Config not found")

        d = dict(row)
        try:
            plain = decrypt_str(d["auth_key_encrypted"])
            d["auth_key_masked"] = _mask_auth_key(plain)
        except ValueError:
            d["auth_key_masked"] = "***"
        del d["auth_key_encrypted"]
        return d
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# DELETE /api/gpt-load/{id} — delete a config
# ---------------------------------------------------------------------------


@router.delete("/{config_id}")
async def delete_config(
    config_id: int,
    _user: bool = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a gpt-load configuration by ID."""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "DELETE FROM gpt_load_config WHERE id = ?", (config_id,)
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Config not found")
        return {"id": config_id, "deleted": True}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# GET /api/gpt-load/{id}/groups — proxy groups from the real gpt-load instance
# ---------------------------------------------------------------------------


@router.get("/{config_id}/groups")
async def list_groups(
    config_id: int,
    _user: bool = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Fetch group list from the real gpt-load instance via its API.

    Proxies ``GET {base_url}/api/groups`` with decrypted auth_key.
    Useful for connectivity testing and manual mapping configuration.
    """
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT base_url, auth_key_encrypted FROM gpt_load_config WHERE id = ?",
            (config_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Config not found")

        base_url = row[0].rstrip("/")
        try:
            auth_key = decrypt_str(row[1])
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to decrypt auth_key: {exc}",
            )
    finally:
        await db.close()

    # Proxy the request
    try:
        resp = requests.get(
            f"{base_url}/api/groups",
            headers={"Authorization": f"Bearer {auth_key}"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"gpt-load returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        # gpt-load groups API returns { code: 0, data: [...] }
        return data.get("data", [])
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to reach gpt-load: {exc}",
        )


# ---------------------------------------------------------------------------
# GET /api/mappings — list all provider→group mappings
# ---------------------------------------------------------------------------


@router.get("/mappings")
async def list_mappings(
    _user: bool = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all provider-to-group mappings."""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT m.id, m.provider_name, m.gpt_load_config_id, "
            "m.group_id, m.group_name, m.enabled, "
            "c.name AS config_name, c.base_url "
            "FROM provider_group_mapping m "
            "LEFT JOIN gpt_load_config c ON c.id = m.gpt_load_config_id "
            "ORDER BY m.provider_name"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# PUT /api/mappings — create or update a provider→group mapping
# ---------------------------------------------------------------------------


@router.put("/mappings")
async def upsert_mapping(
    body: MappingUpdate,
    _user: bool = Depends(get_current_user),
) -> dict[str, Any]:
    """Create or update a provider-to-group mapping.

    Body: ``{"gpt_load_config_id": 1, "group_id": 2}``.
    The *provider_name* is required as a query parameter.
    """
    raise HTTPException(
        status_code=400,
        detail="provider_name query parameter is required. "
        "Use PUT /api/mappings?provider_name=<name>",
    )


@router.put("/mappings/{provider_name}")
async def upsert_mapping_for_provider(
    provider_name: str,
    body: MappingUpdate,
    _user: bool = Depends(get_current_user),
) -> dict[str, Any]:
    """Create or update the mapping for a specific provider.

    Body: ``{"gpt_load_config_id": 1, "group_id": 2}``.
    """
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        # Verify the config exists
        cursor = await db.execute(
            "SELECT id, name FROM gpt_load_config WHERE id = ?",
            (body.gpt_load_config_id,),
        )
        cfg = await cursor.fetchone()
        if cfg is None:
            raise HTTPException(
                status_code=400,
                detail=f"gpt_load_config id={body.gpt_load_config_id} not found",
            )

        # Determine group_name — try to use config name + group_id as label
        group_name = f"{cfg[1]}-group-{body.group_id}"

        # Upsert (max_size: per-batch push cap, default 10000)
        await db.execute(
            """INSERT INTO provider_group_mapping
               (provider_name, gpt_load_config_id, group_id, group_name, max_size)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(provider_name) DO UPDATE SET
               gpt_load_config_id = excluded.gpt_load_config_id,
               group_id = excluded.group_id,
               group_name = excluded.group_name,
               max_size = excluded.max_size""",
            (
                provider_name,
                body.gpt_load_config_id,
                body.group_id,
                group_name,
                body.max_size,
            ),
        )
        await db.commit()

        # Fetch the updated row
        cursor = await db.execute(
            "SELECT id, provider_name, gpt_load_config_id, group_id, group_name, "
            "max_size, enabled "
            "FROM provider_group_mapping WHERE provider_name = ?",
            (provider_name,),
        )
        updated = await cursor.fetchone()
        return dict(updated) if updated else {}
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# DELETE /api/mappings/{provider_name} — delete a mapping
# ---------------------------------------------------------------------------


@router.delete("/mappings/{provider_name}")
async def delete_mapping(
    provider_name: str,
    _user: bool = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a provider-to-group mapping."""
    db_path = resolve_db_path()
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "DELETE FROM provider_group_mapping WHERE provider_name = ?",
            (provider_name,),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Mapping not found")
        return {"provider_name": provider_name, "deleted": True}
    finally:
        await db.close()
