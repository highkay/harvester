#!/usr/bin/env python3

"""
Router for scan-run management — ``/api/runs`` endpoints.

Provides CRUD-style access to the ``run_records`` table and allows
manual cancellation of running scans.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from .deps import get_current_user
from .runner import get_runner

router = APIRouter(
    prefix="/api/runs",
    tags=["runs"],
)

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class RunItem(BaseModel):
    """Public-facing run record (subset of DB columns)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    provider_name: str
    config_file: str
    status: str
    started_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None
    valid_keys_found: int = 0
    total_keys_checked: int = 0
    error_message: str | None = None
    created_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[RunItem])
async def list_runs(
    provider: str | None = Query(None, description="Filter by provider name"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    _user: bool = Depends(get_current_user),
) -> list[dict]:
    """List scan run records (with optional filters and pagination)."""
    runner = get_runner()
    return await runner.list_runs(
        provider=provider, status=status, limit=limit, offset=offset
    )


@router.get("/{run_id}", response_model=RunItem)
async def get_run(
    run_id: str,
    _user: bool = Depends(get_current_user),
) -> dict:
    """Get a single run record by ID."""
    runner = get_runner()
    result = await runner.get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return result


@router.post("/{run_id}/cancel", response_model=dict)
async def cancel_run(
    run_id: str,
    _user: bool = Depends(get_current_user),
) -> dict:
    """Cancel a running scan.

    Only runs in 'running' status can be cancelled.
    """
    runner = get_runner()
    cancelled = await runner.cancel_run(run_id)
    if not cancelled:
        raise HTTPException(
            status_code=400,
            detail=f"Run '{run_id}' is not in a cancellable state",
        )
    return {"ok": True, "run_id": run_id, "message": "Cancelled"}
