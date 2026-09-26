#!/usr/bin/env python3

"""APScheduler integration — AsyncIOScheduler with MemoryJobStore.

Schedule definitions are persisted in the ``schedule_config`` SQLite table.
On startup jobs are rebuilt from that table.  A re-entrancy guard prevents
concurrent runs of the same provider and is held for the scan's FULL
lifetime: ``PipelineRunner.run_scan`` returns as soon as the scan *thread*
starts, so a watcher task keeps the guard until the run_records row leaves
the ``running`` state (see :meth:`SchedulerService.start_scan`).
"""

# allow: SIZE_OK — single cohesive SchedulerService class; splitting would create
# artificial seams between state (_running), CRUD, and init/shutdown.

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, tzinfo
from typing import Any

from apscheduler.jobstores.memory import MemoryJobStore  # type: ignore[import-untyped]
from apscheduler.executors.asyncio import AsyncIOExecutor  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]
from fastapi import HTTPException

from tools.logger import get_logger
from web.db import get_db

logger = get_logger("web.scheduler")

# ---------------------------------------------------------------------------
# Admission control (2026-09-26)
# ---------------------------------------------------------------------------
# The daily chain overlapping 6-8 multi-hour scans is what turned one bad
# egress/limiter hour into a 15-row pile-up (2026-09-24 incident), and a busy
# provider's firing was DROPPED outright ("already running — skipping", e.g.
# github's 6-hourly cron while its daily run was still live). Two knobs:
# a global cap on concurrent runs, and a bounded deferred retry instead of a
# dropped firing. Both are env-overridable; 0 disables the cap.
_MAX_CONCURRENT_SCANS = max(
    0, int(os.environ.get("HARVESTER_MAX_CONCURRENT_SCANS", "6") or 0)
)
# The blocking condition is another run holding the provider, and a bounded run
# is now hours, not minutes (the 120k link cap targets ~3-4 h) — so a 3 x 15 min
# ladder would just drop the firing 45 min later. 6 x 30 min covers a full run
# envelope; a longer collision still falls back to the old skip-and-log, and the
# provider's own next cron firing follows anyway.
_DEFER_DELAY_SECONDS = max(
    60, int(os.environ.get("HARVESTER_DEFER_DELAY_SECONDS", "1800") or 1800)
)
_MAX_DEFERRALS = max(0, int(os.environ.get("HARVESTER_MAX_DEFERRALS", "8") or 8))
# The ladder ESCALATES (delay x attempt: 30, 60, 90 ... 240 min ≈ 18 h total),
# which covers a full run envelope even with colliding firings, while
# `SchedulerService.has_pending_deferral()` admits ONE pending ladder per
# provider (queried from APScheduler, which self-heals when the job runs) so a
# provider's own cron cannot multiply retry chains.


async def _active_run_count(db_path: str) -> int:
    """Number of runs currently in 'running' (authoritative, cross-process).

    Read from run_records rather than the in-process runner dict so a manual
    trigger and a cron firing see the same number (and rows stranded by a
    restart are excluded by the startup reconciliation).

    Fails OPEN (0) when the count cannot be read: a broken counter must never
    refuse a scan the operator asked for, and a DB that cannot answer this
    query has bigger problems than the cap (legacy/partial schemas in tests
    are the realistic case).
    """
    try:
        db = await get_db(db_path)
    except Exception as exc:
        logger.debug(f"concurrency check unavailable ({exc}) — cap not applied")
        return 0
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM run_records WHERE status = 'running'"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.debug(f"concurrency count query failed ({exc}) — cap not applied")
        return 0
    finally:
        try:
            await db.close()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Default seed schedules
# ---------------------------------------------------------------------------

_DEFAULT_SCHEDULES: tuple[tuple[str, str, str], ...] = (
    # High-churn "sk-…" providers: GitHub-leaked keys are revoked within hours,
    # so scan every 4 hours. The four providers are staggered 15 minutes apart
    # so the GitHub search burst does not land on the same minute. Evidence:
    # kimi gained 82 valid keys in a ~2.7-hour same-day window (data-kimi
    # backups 20260810-155759 → 20260810-183753).
    ("deepseek", "0 */4 * * *", "examples/config-deepseek.yaml"),
    ("kimi", "15 */4 * * *", "examples/config-kimi.yaml"),
    ("mimo-cn", "30 */4 * * *", "examples/config-mimo.yaml"),
    ("qwen-cn", "45 */4 * * *", "examples/config-qwen.yaml"),
    # Medium-churn providers — every 6 hours.
    ("glm", "0 */6 * * *", "examples/config-glm.yaml"),
    # ModelScope scans run 6-hourly → DAILY at 11:00 (off-peak, low churn).
    ("modelscope", "0 11 * * *", "examples/config-modelscope.yaml"),
    # Tavily keys survive longer (usage-audit based) — every 6 hours.
    ("tavily", "40 */6 * * *", "examples/config-tavily.yaml"),
    # GitHub token self-bootstrap — scan every 6 hours so the instance's own
    # token pool keeps growing; staggered at :50 to avoid the 0/20/40 burst.
    ("github", "50 */6 * * *", "examples/config-github.yaml"),
    # SerpApi keys are account-based (monthly plans) — every 6 hours, at :10
    # to stagger between the :00/:20/:40/:50 six-hour burst.
    ("serpapi", "10 */6 * * *", "examples/config-serpapi.yaml"),
    # Agnès AI — every 6 hours, at :35 to avoid the existing */6 cluster
    # minutes (0/10/20/40/50) and the */4 cluster (0/15/30/45).
    ("agnes-ai", "35 */6 * * *", "examples/config-agnes-ai.yaml"),
    # Secondary regional tasks live in their own config files now (previously
    # bundled into the primary provider's config, which mixed run records,
    # push statistics and schedules together). Daily at staggered hours —
    # region-specific keys are low churn, so once a day is sufficient.
    ("glm-ai", "0 13 * * *", "examples/config-glm-ai.yaml"),
    # 2026-09-24: the four below moved out of the 14:00-17:00 Beijing window.
    # Measured on prod: the GitHub token-cooldown storm concentrates at
    # 10:00-16:00 Beijing (4529 warnings on 2026-09-24) while the morning
    # chain's long runs (github/groq/deepseek, 200k+ links each) still hold
    # the shared code-search quota — kimi-ai gathered 828 links in 8.6h and
    # qwen-intl completed with 0 links in that window. Evening starts search
    # against a recovered token pool.
    ("kimi-ai", "0 18 * * *", "examples/config-kimi-ai.yaml"),
    ("kimi-coding", "0 19 * * *", "examples/config-kimi-coding.yaml"),
    ("mimo-sg", "0 20 * * *", "examples/config-mimo-sg.yaml"),
    ("qwen-intl", "0 21 * * *", "examples/config-qwen-intl.yaml"),
    # 2026-09-22: providers with configs + prod history that were missing
    # from the seed list (prod rows came from manual/UI inserts, and the
    # seed only runs against an EMPTY schedule_config table — so fresh
    # installs grew up without these scans). Each cron mirrors prod's hour
    # chain (ollama 03, openrouter 08, nvidia beside modelscope's 11) and
    # picks a minute that collides with nothing else in this list: hour 03
    # is never hit by the */4 cluster, the */4 cluster's 08 fires at
    # :00/:15/:45, and modelscope's 11 fires at :00.
    # 2026-09-24: groq REMOVED from the seed list — Groq is a GitHub
    # secret-scanning partner with push protection, so leaked gsk_ keys are
    # auto-revoked within minutes of a public push (22 completed prod runs,
    # 0 valid keys; structural, not fixable from the scanning side).
    ("ollama", "20 3 * * *", "examples/config-ollama.yaml"),
    ("openrouter", "40 8 * * *", "examples/config-openrouter.yaml"),
    ("nvidia", "50 11 * * *", "examples/config-nvidia.yaml"),
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

# How often a guard-watcher polls the run_records row of the scan it tracks.
# Scans run for hours; a 5 s SQLite primary-key read is noise, and it bounds
# how long a finished scan keeps the scheduler-side re-entrancy guard held.
_WATCH_POLL_SECONDS = 5.0

# How many CONSECUTIVE poll failures escalate the watcher's log from debug to
# ERROR (logged once at the threshold; the guard stays held and the watcher
# keeps retrying either way).
_WATCH_POLL_FAILURE_ESCALATION = 3


async def _run_provider_job(
    provider_name: str,
    config_file: str | None = None,
    deferred_attempts: int = 0,
) -> None:
    """Execute a scheduled scan for *provider_name*.

    *config_file* is the source config YAML from ``schedule_config`` (None
    when unknown, e.g. for providers whose task name matches the default
    ``config-{provider_name}.yaml`` convention).

    Delegates to :meth:`SchedulerService.start_scan`, which holds the
    re-entrancy guard for the scan's FULL lifetime — ``run_scan`` returns as
    soon as the scan *thread* starts, so a guard released on that return
    (the pre-2026-09-22 behaviour) let ``is_running()`` claim idle during a
    live scan. When the runner module is missing (T5 not yet merged), the
    error is logged and the scan is silently skipped.
    """
    svc = get_scheduler_service()
    if svc is None:
        logger.error(f"No SchedulerService — cannot run job for {provider_name}")
        return

    try:
        await svc.start_scan(provider_name, config_file)
    except HTTPException as exc:
        # 409 from either guard (scheduler-side watcher or the runner's own
        # provider lock) or 429 from the global concurrency cap: the firing is
        # DEFERRED (bounded) instead of dropped — a 6-hourly cron colliding
        # with a live daily run used to lose that firing entirely.
        if exc.status_code in (409, 429):
            if deferred_attempts < _MAX_DEFERRALS and svc.schedule_deferred(
                provider_name, config_file, deferred_attempts
            ):
                delay = _DEFER_DELAY_SECONDS * (deferred_attempts + 1)
                logger.warning(
                    f"Provider {provider_name} deferred "
                    f"(attempt {deferred_attempts + 1}/{_MAX_DEFERRALS}, "
                    f"retry in {delay}s): {exc.detail}"
                )
                return
            if deferred_attempts < _MAX_DEFERRALS and svc.has_pending_deferral(
                provider_name
            ):
                # NOT a lost firing: an earlier firing's ladder is still queued
                # and will try again. WARNING, because a log-based accounting
                # that counted this as a dropped slot would cry wolf on a busy
                # box.
                logger.warning(
                    f"Provider {provider_name} folded into its pending deferral "
                    f"ladder ({exc.detail})"
                )
                return
        # Terminal: this firing is lost. ERROR — the line a log-based
        # accounting counts as a dropped schedule slot.
        logger.error(
            f"Provider {provider_name} firing DROPPED — deferral budget "
            f"exhausted ({exc.detail})"
        )
    except ImportError:
        logger.error(
            f"PipelineRunner not available (web.runner module missing) — "
            f"skipping scheduled scan for {provider_name}"
        )
    except Exception:
        logger.exception(f"Scheduled scan for {provider_name} failed")


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
        # provider → watcher task that keeps _running held for the scan's
        # full lifetime (armed by start_scan, self-removing on release).
        self._watch_tasks: dict[str, asyncio.Task[None]] = {}

    # -- scan launch (shared by cron jobs + manual trigger) -------------------

    async def start_scan(
        self, provider_name: str, config_file: str | None
    ) -> str:
        """Start a scan and hold the re-entrancy guard until it ends.

        Returns the runner's run_id.

        Raises:
            HTTPException(409): this service already tracks the provider, or
                the runner still holds it (earlier scan thread alive).
            ImportError / ValueError / ...: whatever
                ``PipelineRunner.run_scan`` raises, propagated AFTER the
                guard was released (a failed start must not leak the guard).

        ``run_scan`` returns as soon as the scan *thread* starts, so the
        guard is handed to a watcher task (:meth:`_watch_run`) that releases
        it when the run_records row of *run_id* leaves the ``running`` state.
        """
        if provider_name in self._running:
            raise HTTPException(
                status_code=409,
                detail=f"Provider {provider_name} is already running",
            )

        if _MAX_CONCURRENT_SCANS > 0:
            active = await _active_run_count(self._db_path)
            if active >= _MAX_CONCURRENT_SCANS:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"concurrency cap reached ({active}/{_MAX_CONCURRENT_SCANS} "
                        f"runs live) — deferring instead of stacking another scan"
                    ),
                )

        self._running.add(provider_name)
        try:
            runner = _lazy_get_runner()
            run_id: str = await runner.run_scan(provider_name, config_file)
        except Exception:
            self._running.discard(provider_name)
            raise

        self._watch_tasks[provider_name] = asyncio.create_task(
            self._watch_run(provider_name, run_id)
        )
        return run_id

    def schedule_deferred(
        self,
        provider_name: str,
        config_file: str | None,
        deferred_attempts: int,
    ) -> bool:
        """Queue a one-shot retry of a deferred firing (bounded depth).

        Returns False when the job could not be scheduled (the caller then
        falls back to the old skip-and-log behaviour).
        """
        if self.has_pending_deferral(provider_name):
            return False
        try:
            # Build the date in the SCHEDULER's timezone: a naive datetime is
            # interpreted in that zone by APScheduler, so on a host whose
            # C-library local zone differs from it (measured on the dev box:
            # +1 h local vs Asia/Shanghai; prod container UTC vs the fnos host
            # CST) a naive date fires hours off. Inside the try so a future
            # mistake degrades to "no deferral + ERROR" instead of exploding
            # the job callback.
            tz = getattr(self._scheduler, "timezone", None)
            if not isinstance(tz, tzinfo):
                tz = None
            self._scheduler.add_job(
                _run_provider_job,
                trigger=DateTrigger(
                    run_date=datetime.now(tz)
                    + timedelta(seconds=_DEFER_DELAY_SECONDS * (deferred_attempts + 1))
                ),
                args=[provider_name, config_file, deferred_attempts + 1],
                id=f"defer-{provider_name}-{int(time.time() * 1000)}",
                max_instances=1,
                replace_existing=False,
                misfire_grace_time=None,
            )
            return True
        except Exception as exc:  # pragma: no cover - scheduler-level failure
            logger.error(f"Could not defer {provider_name}: {exc}")
            return False

    def has_pending_deferral(self, provider_name: str) -> bool:
        """Whether a deferral job for *provider_name* is still queued.

        Queried from APScheduler rather than a parallel in-memory set: a
        one-shot job disappears by itself when it runs, so this cannot desync —
        a leaked flag would otherwise refuse every future deferral for that
        provider.
        """
        prefix = f"defer-{provider_name}-"
        try:
            return any(j.id.startswith(prefix) for j in self._scheduler.get_jobs())
        except Exception:
            return False

    async def _watch_run(self, provider_name: str, run_id: str) -> None:
        """Release the guard when run *run_id* leaves the ``running`` state.

        Polls ``PipelineRunner.get_run`` — the run_records row that
        ``run_scan`` inserts before returning — every ``_WATCH_POLL_SECONDS``
        and returns once the row is terminal (completed / failed / cancelled,
        i.e. anything else than ``running``, including a vanished row).
        Transient poll errors keep the guard held: the scan is presumed live
        until proven terminal; ``_WATCH_POLL_FAILURE_ESCALATION`` consecutive
        poll failures escalate the log to ERROR (once, at the threshold) while
        the guard stays held and the watcher keeps retrying.

        Known residual: if the terminal DB write itself is lost (e.g. the scan
        process dies after its last 'running' heartbeat but before the
        terminal update lands), the row stays ``running`` forever and this
        watcher holds the scheduler guard for *provider_name* until the
        process restarts — startup ``web.db.reconcile_running_runs`` flips such
        stale rows to failed and clears the state. Scheduled firings in the
        meantime are skipped with a visible warning ("already running —
        skipping"), never silently double-started.
        """
        consecutive_poll_failures = 0
        try:
            runner = _lazy_get_runner()
            while True:
                await asyncio.sleep(_WATCH_POLL_SECONDS)
                try:
                    record = await runner.get_run(run_id)
                except Exception as exc:
                    consecutive_poll_failures += 1
                    if consecutive_poll_failures == _WATCH_POLL_FAILURE_ESCALATION:
                        logger.error(
                            f"Run watch poll failed {consecutive_poll_failures} consecutive "
                            f"times ({run_id}): {exc} — keeping guard for "
                            f"{provider_name}, retrying"
                        )
                    else:
                        logger.debug(f"Run watch poll failed ({run_id}): {exc}")
                    continue
                consecutive_poll_failures = 0
                if record is None or record.get("status") != "running":
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                f"Run watch for {provider_name} ({run_id}) died — "
                f"releasing guard"
            )
        finally:
            self._running.discard(provider_name)
            self._watch_tasks.pop(provider_name, None)

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
                args=(provider_name, config_file),
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

        Awaits the scan START so failures reach the route — the old
        fire-and-forget ``create_task`` answered ``"triggered"`` (HTTP 202)
        for runs the scheduler then rejected and swallowed, leaving the UI
        claiming success with no run_records row.

        Raises:
            HTTPException(409): the provider is already running (scheduler
                guard, or the runner's own guard via :meth:`start_scan`).
            HTTPException(404): no schedule row for the provider, or the
                runner cannot find its source config template.

        Returns ``"triggered"`` once the scan thread is up and the guard
        watcher is armed; the guard stays held until the run is terminal.
        """
        if self.is_running(provider_name):
            raise HTTPException(
                status_code=409,
                detail=f"Provider {provider_name} is already running",
            )

        # Verify the provider exists in schedule_config (and read its
        # source config YAML so the manual run resolves the right file)
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "SELECT config_file FROM schedule_config WHERE provider_name = ?",
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

        config_file: str | None = row["config_file"]

        try:
            await self.start_scan(provider_name, config_file)
        except ValueError as exc:
            # runner.run_scan: no source config template for this provider
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return "triggered"

    def is_running(self, provider_name: str) -> bool:
        """Return whether *provider_name* is currently being scanned."""
        return provider_name in self._running

    async def shutdown(self) -> None:
        """Gracefully shut down the scheduler and its run watchers."""
        watchers = list(self._watch_tasks.values())
        self._watch_tasks.clear()
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
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
            logger.info(
                f"Inserted {len(_DEFAULT_SCHEDULES)} default schedule_config rows"
            )
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
        # APScheduler's default misfire_grace_time is ONE SECOND: on a box at
        # load ~8 with 6 live scans, a firing that lands >1 s late is silently
        # skipped ("Run time of job … was missed"), i.e. whole cron slots can
        # vanish exactly when the box is busiest. Any FINITE grace just moves
        # that cliff (5 min of stall still drops the firing), so use None =
        # never expire: a late firing runs and then hits the provider guard or
        # the concurrency cap, which DEFER it into the retry ladder — exactly
        # the path that makes a busy box safe. coalesce collapses a backlog into
        # one run; max_instances=1 keeps a provider job from overlapping itself.
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": None,
        },
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
                args=(provider, row["config_file"]),
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
