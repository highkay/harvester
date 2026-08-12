#!/usr/bin/env python3

"""APScheduler integration — AsyncIOScheduler with MemoryJobStore.

Schedule definitions are persisted in the ``schedule_config`` SQLite table.
On startup jobs are rebuilt from that table.  A re-entrancy guard prevents
concurrent runs of the same provider.
"""

# allow: SIZE_OK — single cohesive SchedulerService class; splitting would create
# artificial seams between state (_running), CRUD, and init/shutdown.

from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.jobstores.memory import MemoryJobStore  # type: ignore[import-untyped]
from apscheduler.executors.asyncio import AsyncIOExecutor  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from fastapi import HTTPException

from tools.logger import get_logger
from web.db import get_db

logger = get_logger("web.scheduler")

# ---------------------------------------------------------------------------
# Default seed schedules
# ---------------------------------------------------------------------------

_DEFAULT_SCHEDULES: tuple[tuple[str, str, str], ...] = (
    ("deepseek", "0 3 * * *", "examples/config-deepseek.yaml"),
    ("kimi", "0 3 * * *", "examples/config-kimi.yaml"),
    ("mimo-cn", "0 3 * * *", "examples/config-mimo.yaml"),
    ("qwen-cn", "0 3 * * *", "examples/config-qwen.yaml"),
)


# ---------------------------------------------------------------------------
# Lazy import of PipelineRunner (T5 may not be done yet)
# ---------------------------------------------------------------------------


def _lazy_get_runner() -> Any:
    """Return the PipelineRunner callable, or raise ImportError if unavailable."""
    from web.runner import get_runner  # type: ignore[import-untyped]

    return get_runner()


# ---------------------------------------------------------------------------
# Job callback
# ---------------------------------------------------------------------------


async def _run_provider_job(provider_name: str) -> None:
    """Execute a scheduled scan for *provider_name*.

    Uses a re-entrancy guard via ``SchedulerService._running`` to skip if the
    same provider is already executing via the scheduler or a manual trigger.
    When the runner module is missing (T5 not yet merged), the error is logged
    and the scan is silently skipped.
    """
    svc = get_scheduler_service()
    if svc is None:
        logger.error(f"No SchedulerService — cannot run job for {provider_name}")
        return

    if svc.is_running(provider_name):
        logger.warning(f"Provider {provider_name} is already running — skipping")
        return

    svc._running.add(provider_name)
    try:
        runner = _lazy_get_runner()
        await runner.run(provider_name)
    except ImportError:
        logger.error(
            f"PipelineRunner not available (web.runner module missing) — "
            f"skipping scheduled scan for {provider_name}"
        )
    except Exception:
        logger.exception(f"Scheduled scan for {provider_name} failed")
    finally:
        svc._running.discard(provider_name)


# ---------------------------------------------------------------------------
# SchedulerService
# ---------------------------------------------------------------------------

_scheduler_service: SchedulerService | None = None


class SchedulerService:
    """Manages APScheduler lifecycle and schedule_config CRUD.

    Instances are created by :func:`init_scheduler` and accessed via
    :func:`get_scheduler_service`.
    """

    def __init__(
        self,
        scheduler: AsyncIOScheduler,
        db_path: str,
    ) -> None:
        self._scheduler = scheduler
        self._db_path = db_path
        self._running: set[str] = set()

    # -- schedule_config CRUD ------------------------------------------------

    async def get_schedules(self) -> list[dict[str, object]]:
        """Return all schedule configs with ``next_run_time`` for each."""
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "SELECT provider_name, cron_expression, enabled, config_file, "
                "created_at, updated_at FROM schedule_config ORDER BY provider_name"
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        result: list[dict[str, object]] = []
        for row in rows:
            entry: dict[str, object] = {
                "provider_name": row["provider_name"],
                "cron_expression": row["cron_expression"],
                "enabled": bool(row["enabled"]),
                "config_file": row["config_file"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            job = self._scheduler.get_job(f"scan-{row['provider_name']}")
            next_run = getattr(job, "next_run_time", None)
            entry["next_run_time"] = str(next_run) if next_run else None
            result.append(entry)
        return result

    async def get_schedule(self, provider_name: str) -> dict[str, object]:
        """Return a single schedule config row, or raise 404."""
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "SELECT provider_name, cron_expression, enabled, config_file, "
                "created_at, updated_at FROM schedule_config "
                "WHERE provider_name = ?",
                (provider_name,),
            )
            row = await cursor.fetchone()
        finally:
            await db.close()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Schedule not found for provider: {provider_name}",
            )

        entry: dict[str, object] = {
            "provider_name": row["provider_name"],
            "cron_expression": row["cron_expression"],
            "enabled": bool(row["enabled"]),
            "config_file": row["config_file"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        job = self._scheduler.get_job(f"scan-{provider_name}")
        next_run = getattr(job, "next_run_time", None)
        entry["next_run_time"] = str(next_run) if next_run else None
        return entry

    async def update_schedule(
        self,
        provider_name: str,
        cron_expression: str,
        enabled: bool,
        config_file: str,
    ) -> dict[str, object]:
        """Validate cron, upsert schedule_config, add/remove APScheduler job.

        Raises HTTPException(400) for invalid cron.
        """
        # Validate cron expression
        try:
            CronTrigger.from_crontab(cron_expression)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid cron expression: {cron_expression} — {exc}",
            ) from exc

        db = await get_db(self._db_path)
        try:
            await db.execute(
                "INSERT INTO schedule_config "
                "(provider_name, cron_expression, enabled, config_file) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(provider_name) DO UPDATE SET "
                "cron_expression = excluded.cron_expression, "
                "enabled = excluded.enabled, "
                "config_file = excluded.config_file, "
                "updated_at = datetime('now')",
                (provider_name, cron_expression, int(enabled), config_file),
            )
            await db.commit()
        finally:
            await db.close()

        # Rebuild the scheduler job
        job_id = f"scan-{provider_name}"
        if enabled:
            self._scheduler.add_job(
                _run_provider_job,
                CronTrigger.from_crontab(cron_expression),
                id=job_id,
                args=(provider_name,),
                replace_existing=True,
            )
        else:
            # Remove existing job if disabled
            try:
                self._scheduler.remove_job(job_id)
            except Exception:
                pass

        job = self._scheduler.get_job(job_id) if enabled else None
        next_run = getattr(job, "next_run_time", None)
        return {
            "provider_name": provider_name,
            "cron_expression": cron_expression,
            "enabled": enabled,
            "config_file": config_file,
            "next_run_time": str(next_run) if next_run else None,
        }

    async def delete_schedule(self, provider_name: str) -> bool:
        """Delete a provider schedule: remove DB row + APScheduler job.

        Returns ``True`` if a row was deleted, ``False`` if not found.
        """
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "DELETE FROM schedule_config WHERE provider_name = ?",
                (provider_name,),
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        finally:
            await db.close()

        # Remove the APScheduler job regardless of DB result (best-effort)
        try:
            self._scheduler.remove_job(f"scan-{provider_name}")
        except Exception:
            pass

        return deleted

    async def trigger_manual(self, provider_name: str) -> str:
        """Immediately run a provider scan (manual trigger).

        Raises HTTPException(409) if the provider is already running.
        Returns ``"triggered"`` on success.
        """
        if self.is_running(provider_name):
            raise HTTPException(
                status_code=409,
                detail=f"Provider {provider_name} is already running",
            )

        # Verify the provider exists in schedule_config
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "SELECT 1 FROM schedule_config WHERE provider_name = ?",
                (provider_name,),
            )
            exists = await cursor.fetchone()
        finally:
            await db.close()

        if exists is None:
            raise HTTPException(
                status_code=404,
                detail=f"Schedule not found for provider: {provider_name}",
            )

        # Fire and forget — the re-entrancy guard in _run_provider_job handles it
        asyncio.create_task(_run_provider_job(provider_name))
        return "triggered"

    def is_running(self, provider_name: str) -> bool:
        """Return whether *provider_name* is currently being scanned."""
        return provider_name in self._running

    async def shutdown(self) -> None:
        """Gracefully shut down the underlying AsyncIOScheduler."""
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")


def get_scheduler_service() -> SchedulerService | None:
    """Return the global SchedulerService singleton, or None if not initialised."""
    return _scheduler_service


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


async def _seed_default_schedules(db_path: str) -> None:
    """Insert default schedule rows when the table is empty."""
    db = await get_db(db_path)
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM schedule_config")
        row = await cursor.fetchone()
        if row and row[0] == 0:
            for provider_name, cron, config_file in _DEFAULT_SCHEDULES:
                await db.execute(
                    "INSERT INTO schedule_config "
                    "(provider_name, cron_expression, enabled, config_file) "
                    "VALUES (?, ?, 1, ?)",
                    (provider_name, cron, config_file),
                )
            await db.commit()
            logger.info("Inserted 4 default schedule_config rows")
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Public init / shutdown entry points (called from app lifespan)
# ---------------------------------------------------------------------------


async def init_scheduler(settings: object) -> SchedulerService:
    """Initialise the scheduler from a settings object.

    *settings* must have a ``db_path`` attribute (e.g. ``WebSettings``).

    1. Seeds default schedule_config rows if the table is empty.
    2. Creates an ``AsyncIOScheduler`` with ``MemoryJobStore``.
    3. Loads all enabled schedules and adds APScheduler jobs.
    4. Starts the scheduler.
    """
    global _scheduler_service

    db_path: str = getattr(settings, "db_path")
    await _seed_default_schedules(db_path)

    scheduler = AsyncIOScheduler(
        jobstores={"default": MemoryJobStore()},
        # Job callback (_run_provider_job) is an async coroutine — must run on
        # the asyncio executor, NOT the default ThreadPoolExecutor, or it is
        # never awaited and scheduled scans silently do nothing.
        executors={"default": AsyncIOExecutor()},
    )
    svc = SchedulerService(scheduler=scheduler, db_path=db_path)
    _scheduler_service = svc

    # Load and rebuild jobs from the database
    db = await get_db(db_path)
    try:
        cursor = await db.execute(
            "SELECT provider_name, cron_expression, enabled, config_file "
            "FROM schedule_config WHERE enabled = 1"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    for row in rows:
        provider = row["provider_name"]
        cron = row["cron_expression"]
        try:
            scheduler.add_job(
                _run_provider_job,
                CronTrigger.from_crontab(cron),
                id=f"scan-{provider}",
                args=(provider,),
                replace_existing=True,
            )
            logger.info(f"Scheduled {provider} with cron '{cron}'")
        except (ValueError, TypeError) as exc:
            logger.error(f"Invalid cron for {provider}: {cron} — {exc}")

    scheduler.start()
    logger.info(f"Scheduler started with {len(rows)} job(s)")
    return svc


async def shutdown_scheduler() -> None:
    """Shut down the global scheduler (called from app lifespan shutdown)."""
    svc = _scheduler_service
    if svc is not None:
        await svc.shutdown()
