#!/usr/bin/env python3

"""
FastAPI application factory for the Harvester web layer.

Provides ``create_app()`` which wires up the lifespan, health-check,
and dependency injection.  Parallel tasks will create ``web.db`` and
``web.scheduler`` — those imports are deferred with ``ImportError``
tolerance so the app starts without them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tools.logger import get_logger

from .deps import get_settings
from .router_schedule import router as schedule_router

logger = get_logger("web.app")

# ---------------------------------------------------------------------------
# Lazy imports (modules created by parallel tasks)
# ---------------------------------------------------------------------------

try:
    from web.db import init_db  # type: ignore[import-untyped]
except ImportError:
    init_db = None  # type: ignore[assignment]
    logger.info("web.db not available — database will not be initialised")

try:
    from web.scheduler import init_scheduler  # type: ignore[import-untyped]
except ImportError:
    init_scheduler = None  # type: ignore[assignment]
    logger.info("web.scheduler not available — scheduler will not be initialised")

try:
    from web.router_runs import router as runs_router  # type: ignore[import-untyped]
except ImportError:
    runs_router = None  # type: ignore[assignment]
    logger.info("web.router_runs not available — /api/runs will not be mounted")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Application lifespan: startup and shutdown hooks."""

    settings = get_settings()

    # --- Startup ---
    logger.info(f"Web server starting on {settings.host}:{settings.port}")

    if init_db is not None:
        await init_db(settings.db_path)
    else:
        logger.info("Skipping init_db — module not installed")

    try:
        from web.db import reconcile_running_runs  # type: ignore[import-untyped]
        reconciled = await reconcile_running_runs(settings.db_path)
        if reconciled:
            logger.info(f"Reconciled {reconciled} interrupted run(s) as failed")
    except Exception as exc:
        logger.warning(f"run reconciliation skipped: {exc}")

    if init_scheduler is not None:
        await init_scheduler(settings)
    else:
        logger.info("Skipping init_scheduler — module not installed")

    yield  # application runs here

    # --- Shutdown ---
    logger.info("Web server shutting down")
    if init_scheduler is not None:
        from web.scheduler import shutdown_scheduler  # type: ignore[import-untyped]
        await shutdown_scheduler()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:  # type: ignore[type-arg]
    """Create and configure the FastAPI application.

    Returns a fully wired app ready for ``uvicorn.run()``.
    """
    app = FastAPI(
        title="Harvester Web",
        description="Web API for the Harvester data-acquisition framework",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # --- CORS ---
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Routers (modular)
    # ------------------------------------------------------------------

    # --- UI 管理界面 (Jinja2, Wave 3 T7) — 挂载首位以保证 GET / 落入仪表盘 ---
    from web.router_ui import router as ui_router  # type: ignore[import-untyped]
    app.include_router(ui_router)

    app.include_router(schedule_router)

    # --- Authentication ---
    from web.router_auth import router as auth_router  # type: ignore[import-untyped]
    app.include_router(auth_router)

    # ------------------------------------------------------------------
    # Routes (inline)
    # ------------------------------------------------------------------

    @app.get("/")
    async def root():
        """Root endpoint — basic API identity."""
        return {
            "service": "Harvester Web API",
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health")
    async def health():
        """Health-check endpoint."""
        return {"status": "ok"}

    # --- Token management ---
    from web.router_tokens import router as token_router  # type: ignore[import-untyped]
    app.include_router(token_router)

    # --- Run management ---
    if runs_router is not None:
        app.include_router(runs_router)

    # --- gpt-load push module (Wave 3 T6) ---
    try:
        from web.router_push_logs import router as push_logs_router  # type: ignore[import-untyped]
        app.include_router(push_logs_router)
    except ImportError:
        logger.info("web.router_push_logs not available — /api/push-logs will not be mounted")

    try:
        from web.router_gpt_load import router as gpt_load_router  # type: ignore[import-untyped]
        app.include_router(gpt_load_router)
    except ImportError:
        logger.info("web.router_gpt_load not available — /api/gpt-load will not be mounted")

    return app
