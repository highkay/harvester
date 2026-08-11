#!/usr/bin/env python3

"""Schedule management API router — CRUD + manual trigger for provider schedules.

Mounted at ``/api/schedule`` by :func:`web.app.create_app`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from tools.logger import get_logger
from web.deps import get_current_user
from web.models import CronUpdate
from web.scheduler import get_scheduler_service

logger = get_logger("web.router_schedule")

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


# ---------------------------------------------------------------------------
# Auth placeholder — T9 will replace get_current_user with real auth
# ---------------------------------------------------------------------------


async def _require_auth(_user: bool = Depends(get_current_user)) -> None:
    """Placeholder auth dependency.  Always passes (T9)."""
    return


# ---------------------------------------------------------------------------
# GET /api/schedule
# ---------------------------------------------------------------------------


@router.get("")
async def list_schedules(
    _auth: None = Depends(_require_auth),
) -> list[dict[str, object]]:
    """Return all provider schedules with computed ``next_run_time``."""
    svc = get_scheduler_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    return await svc.get_schedules()


# ---------------------------------------------------------------------------
# GET /api/schedule/{provider_name}
# ---------------------------------------------------------------------------


@router.get("/{provider_name}")
async def get_schedule(
    provider_name: str,
    _auth: None = Depends(_require_auth),
) -> dict[str, object]:
    """Return a single provider schedule."""
    svc = get_scheduler_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    return await svc.get_schedule(provider_name)


# ---------------------------------------------------------------------------
# PUT /api/schedule/{provider_name}
# ---------------------------------------------------------------------------


@router.put("/{provider_name}")
async def update_schedule(
    provider_name: str,
    body: CronUpdate,
    config_file: str | None = None,
    _auth: None = Depends(_require_auth),
) -> dict[str, object]:
    """Update or create a provider schedule.

    Body (JSON): ``cron_expression`` (str), ``enabled`` (bool).
    Query param: ``config_file`` (optional — defaults to existing value or empty string).
    """
    svc = get_scheduler_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")

    # Resolve config_file: body override > existing row > empty string
    resolved_config = config_file
    if resolved_config is None:
        try:
            existing = await svc.get_schedule(provider_name)
            resolved_config = str(existing.get("config_file", ""))
        except HTTPException:
            resolved_config = ""

    return await svc.update_schedule(
        provider_name=provider_name,
        cron_expression=body.cron_expression,
        enabled=body.enabled,
        config_file=resolved_config,
    )


# ---------------------------------------------------------------------------
# POST /api/schedule/{provider_name}/run
# ---------------------------------------------------------------------------


@router.post("/{provider_name}/run", status_code=202)
async def trigger_manual_run(
    provider_name: str,
    _auth: None = Depends(_require_auth),
) -> dict[str, str]:
    """Manually trigger a provider scan immediately.

    Returns 409 Conflict if the provider is already running.
    """
    svc = get_scheduler_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="Scheduler not initialised")
    result = await svc.trigger_manual(provider_name)
    return {"status": result, "provider_name": provider_name}
