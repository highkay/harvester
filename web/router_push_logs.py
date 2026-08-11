#!/usr/bin/env python3

"""Push logs query API — ``/api/push-logs``.

Provides paginated access to the ``push_logs`` table with optional filters.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from tools.logger import get_logger

from .db import get_db, resolve_db_path
from .deps import get_current_user

logger = get_logger("web.router_push_logs")

router = APIRouter(prefix="/api/push-logs", tags=["push-logs"])


# ---------------------------------------------------------------------------
# GET /api/push-logs
# ---------------------------------------------------------------------------


@router.get("")
async def list_push_logs(
    provider: str | None = Query(None, description="Filter by provider name"),
    status: str | None = Query(None, description="Filter by push status (success/failed/partial)"),
    date_from: str | None = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: str | None = Query(None, description="Filter to date (YYYY-MM-DD)"),
    limit: int = Query(50, ge=1, le=500, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    _user: bool = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Query push_logs with optional filters and pagination.

    Returns a list of push_log entries sorted by ``pushed_at DESC``.
    """
    db_path = resolve_db_path()
    db = await get_db(db_path)

    try:
        query = "SELECT * FROM push_logs WHERE 1=1"
        params: list[Any] = []

        if provider:
            query += " AND provider_name = ?"
            params.append(provider)
        if status:
            query += " AND status = ?"
            params.append(status)
        if date_from:
            query += " AND date(pushed_at) >= ?"
            params.append(date_from)
        if date_to:
            query += " AND date(pushed_at) <= ?"
            params.append(date_to)

        query += " ORDER BY pushed_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()
