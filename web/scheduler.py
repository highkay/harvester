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
from datetime import datetime, timedelta, timezone, tzinfo
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

# ---------------------------------------------------------------------------
# Missed-run catch-up (2026-09-28)
# ---------------------------------------------------------------------------
# Deferral jobs are one-shot DateTrigger jobs in the in-memory MemoryJobStore,
# so a restart wipes every pending ladder — and any cron slot that passed
# while the process was down is lost silently (measured: agnes-ai's 10:00 and
# openrouter's 08:00 firings vanished after a 10:07 restart; no run_records
# row, no error line). At startup, each ENABLED schedule's MOST RECENT cron
# fire time is compared against run_records; a provider with no run started at
# or after that fire gets ONE staggered one-shot catch-up through the normal
# _run_provider_job path (so the concurrency cap + deferral ladder apply).
# Env gate: HARVESTER_SCHEDULER_CATCHUP (default on).
_CATCHUP_ENABLED = os.environ.get(
    "HARVESTER_SCHEDULER_CATCHUP", "1"
).strip().lower() not in ("0", "false", "no", "off", "")
# Catch-up jobs are staggered by index so a restart does not fire a whole
# missed chain at once (thundering herd on a box already at load ~8).
_CATCHUP_STAGGER_SECONDS = 60


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
    # OpenCode Go subscription keys — WEEKLY (Sat 22:00), downgraded from
    # daily on 2026-09-28 after two consecutive zero-valid runs (92k links →
    # 265 materials → 0 valid each time): the corpus carries docs/placeholders
    # and dead/unsubscribed keys only. Weekly keeps the provider warm at ~1/7
    # of the GitHub-search budget; re-promote if a weekly run ever yields.
    ("opencode", "0 22 * * 6", "examples/config-opencode.yaml"),
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

# Upper bound on a single run-watch lifetime, in hours (env-configurable like
# the other HARVESTER_* knobs). Default 36 h sits above the longest measured
# prod run (28.3 h, deepseek 2026-09-25→26) so a legitimate long scan is never
# cut short, while a row whose terminal DB write was lost (sqlite locked /
# disk full in the failure path) can no longer wedge the provider's guard —
# and inflate _active_run_count — until process restart. The floor keeps a
# nonsensical env value (0/negative) from expiring healthy runs on sight.
_RUN_WATCH_MAX_HOURS = max(
    0.01, float(os.environ.get("HARVESTER_RUN_WATCH_MAX_HOURS", "36") or "36")
)


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
# Missed-run catch-up at startup
# ---------------------------------------------------------------------------

# How far back the latest-fire search walks. Production seed crons are daily
# or more frequent, so 8 days covers even a week-long outage on a weekly
# cron; rarer crons skip catch-up (their slots are sparse by definition).
_CATCHUP_LOOKBACK = timedelta(days=8)


def _previous_fire_time(cron: str, tz: tzinfo | None) -> datetime | None:
    """Most recent cron fire time at or before now, in the scheduler's tz.

    APScheduler's ``CronTrigger`` exposes no ``get_previous_fire_time``, so
    walk forward with ``get_next_fire_time`` from a bounded lookback window
    and keep the last fire at or before now. Returns None when the cron
    expression cannot be parsed (the row's own ``scan-`` job already logged
    the parse error during job rebuild) or when no fire falls in the window.
    """
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=tz)
    except (ValueError, TypeError):
        return None

    # Aware in the trigger's zone either way: tz=None means the trigger was
    # built in the machine-local zone, so "now" must carry a zone too —
    # get_next_fire_time returns aware fires and a naive now cannot compare.
    now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    previous = None
    probe = now - _CATCHUP_LOOKBACK
    while True:
        fire = trigger.get_next_fire_time(previous, probe)
        if fire is None or fire > now:
            break
        previous = fire
        probe = fire
    return previous


def _parse_utc_started_at(value: object) -> datetime | None:
    """Parse a ``run_records.started_at`` string as UTC into an aware datetime.

    TIMEZONE TRAP: ``started_at`` is written via SQLite ``datetime('now')``
    (UTC) while cron fire times are computed in the SCHEDULER's timezone
    (prod: Asia/Shanghai, prod container runs UTC while the fnos host is CST).
    Both sides must be aware before comparison — a naive string compare put a
    local-zone fire time next to a UTC timestamp and misjudged every run up to
    the tz offset (±8 h on prod). Unparseable values return None.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


async def _catchup_state(db: Any, provider: str) -> tuple[bool, datetime | None]:
    """Return ``(has_live_run, last_started_utc)`` for *provider*.

    Single query shared by BOTH catch-up decision points — arming
    (:_schedule_catchups) and execution (:_run_catchup_job) — so the two can
    never disagree about what "already satisfied" means. ``last_started`` is
    parsed via :_parse_utc_started_at (started_at is UTC; the missed-fire
    comparison happens against an aware datetime on both sides). Raises
    whatever the DB raises — each caller owns its own failure policy (arming
    skips the provider; execution proceeds toward running).
    """
    cursor = await db.execute(
        "SELECT MAX(started_at) AS last_started, "
        "SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS live_runs "
        "FROM run_records WHERE provider_name = ?",
        (provider,),
    )
    info = await cursor.fetchone()
    has_live_run = info is not None and int(info["live_runs"] or 0) > 0
    last_started = (
        _parse_utc_started_at(info["last_started"]) if info is not None else None
    )
    return has_live_run, last_started


async def _run_catchup_job(
    provider_name: str,
    config_file: str | None = None,
    missed_fire_iso: str = "",
) -> None:
    """One-shot catch-up callback: RE-CHECK the missed fire, then run.

    The re-check exists because this callback fires 60s+ after
    :_schedule_catchups armed it: the provider's own cron may have satisfied
    the missed fire during the stagger delay, and blind delegation would then
    409 on the live run and DEFER the catch-up — so the deferred retry later
    starts a DUPLICATE scan for an already-satisfied fire. Re-running the same
    :_catchup_state query at execution time closes that hole: a run started at
    or after *missed_fire_iso*, or a still-'running' row, skips the catch-up
    with an INFO log instead.

    Failure policy is deliberately FAIL-TOWARD-RUNNING at execution time (the
    opposite of arming, which skips): an unparseable/absent *missed_fire_iso*
    (legacy job args) or a broken re-check query logs and delegates anyway — a
    duplicate-risk run is safer than silently losing the missed firing, and
    the provider guard + concurrency cap in :_run_provider_job still apply.

    Going through :func:`_run_provider_job` when the re-check passes means the
    provider guard, the concurrency cap and the deferral ladder apply to a
    catch-up firing exactly as they do to a regular cron firing.
    """
    # _parse_utc_started_at: same ISO→aware-UTC parse the arming side compares
    # against (None = legacy/garbage arg → no re-check possible).
    missed_fire = (
        _parse_utc_started_at(missed_fire_iso) if missed_fire_iso else None
    )
    if missed_fire is not None:
        svc = get_scheduler_service()
        db_path = getattr(svc, "_db_path", None)
        if isinstance(db_path, str):
            try:
                db = await get_db(db_path)
                try:
                    has_live_run, last_started = await _catchup_state(
                        db, provider_name
                    )
                finally:
                    try:
                        await db.close()
                    except Exception:
                        pass
                if has_live_run:
                    logger.info(
                        f"Catch-up skipped for {provider_name}: a run is still "
                        f"live"
                    )
                    return
                if last_started is not None and last_started >= missed_fire:
                    logger.info(
                        f"Catch-up skipped for {provider_name}: a run started "
                        f"at/after the missed fire {missed_fire_iso} — "
                        f"already satisfied"
                    )
                    return
            except Exception as exc:
                logger.warning(
                    f"Catch-up re-check failed for {provider_name} ({exc}) — "
                    f"proceeding with the run"
                )

    logger.warning(
        f"Catch-up firing for {provider_name}: the cron fire at "
        f"{missed_fire_iso} (UTC) had no run when the scheduler started — "
        f"running it now"
    )
    await _run_provider_job(provider_name, config_file)


async def _schedule_catchups(
    scheduler: AsyncIOScheduler, db_path: str
) -> int:
    """Arm one-shot catch-up runs for the latest cron fire missed per provider.

    Only the MOST RECENT fire per enabled provider is caught up (older missed
    slots stay lost — a backfill storm after a long outage would hammer the
    shared GitHub search budget). A provider is skipped when a run started at
    or after that fire time exists in ``run_records``, or when it still has a
    'running' row. Returns the number of catch-up jobs scheduled.
    """
    if not _CATCHUP_ENABLED:
        return 0

    tz = getattr(scheduler, "timezone", None)
    if not isinstance(tz, tzinfo):
        tz = None

    try:
        db = await get_db(db_path)
    except Exception as exc:
        logger.warning(f"Catch-up check skipped — database unavailable ({exc})")
        return 0

    scheduled = 0
    try:
        cursor = await db.execute(
            "SELECT provider_name, cron_expression, config_file "
            "FROM schedule_config WHERE enabled = 1 ORDER BY provider_name"
        )
        rows = list(await cursor.fetchall())
        for row in rows:
            provider = row["provider_name"]
            try:
                fire = _previous_fire_time(row["cron_expression"], tz)
                if fire is None:
                    continue
                has_live_run, last_started = await _catchup_state(db, provider)
                if has_live_run:
                    logger.info(
                        f"Catch-up skipped for {provider}: a run is still live"
                    )
                    continue
                if last_started is not None and last_started >= fire:
                    continue  # ran at or after the latest fire — nothing missed
                delay = _CATCHUP_STAGGER_SECONDS * (scheduled + 1)
                missed_fire_iso = fire.astimezone(timezone.utc).isoformat()
                scheduler.add_job(
                    _run_catchup_job,
                    trigger=DateTrigger(
                        run_date=datetime.now(tz) + timedelta(seconds=delay)
                    ),
                    args=[provider, row["config_file"], missed_fire_iso],
                    id=f"catchup-{provider}-{int(time.time() * 1000)}",
                    max_instances=1,
                    replace_existing=False,
                    misfire_grace_time=None,
                )
                scheduled += 1
                logger.info(
                    f"Scheduled catch-up run for {provider}: missed cron fire "
                    f"at {missed_fire_iso}, firing in {delay}s"
                )
            except Exception as exc:
                logger.warning(f"Catch-up check failed for {provider}: {exc}")
        return scheduled
    finally:
        try:
            await db.close()
        except Exception:
            pass


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
            for job in self._scheduler.get_jobs():
                job_id = str(getattr(job, "id", ""))
                # Exact provider segment: job ids are defer-<provider>-<ms>, and
                # a bare startswith made `defer-kimi-ai-…` count as a pending
                # ladder for `kimi` (same for glm/glm-ai), which would fold a
                # colliding firing into a ladder that does not exist for it.
                if job_id.startswith(prefix) and job_id[len(prefix):].isdigit():
                    return True
            return False
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

        Bounded lifetime: the watch is NOT open-ended. Its deadline is the
        row's own ``started_at`` (UTC, via :_parse_utc_started_at) +
        ``_RUN_WATCH_MAX_HOURS``, falling back to the watcher start when
        ``started_at`` is missing or unparseable — so a row started long ago
        (e.g. stranded by a lost terminal DB write before this process even
        started watching) expires on the first poll. On expiry the guard is
        released through the same ``finally`` mechanics as a normal release,
        an ERROR is logged with the shortened run id, and a best-effort
        ``running→failed`` flip goes through the runner's ``_update_run_sync``
        with ``only_if_running=True`` (wrapped in try/except — the watcher
        never raises): the flip cannot clobber a terminal status the scan
        thread managed to land in the meantime, and if the scan thread is
        somehow still alive the runner's OWN provider guard keeps blocking a
        true duplicate scan even though the scheduler-side guard is free.
        The flip also records the provider's validated-key count (via the
        runner's ``_count_valid_keys_for_failed_run`` — best-effort, scoped to
        this provider with no temp config, exactly like the runner's own
        failure path), WITHOUT which ``web.db.find_unpushed_terminal_runs``
        (gated on ``COALESCE(valid_keys_found,0) > 0``) would never dispatch
        startup push recovery for a deadline-flipped run that holds validated
        keys on disk — the exact stranded-run case this bound exists for.
        Without this bound a stuck ``running`` row held the guard until
        process restart, silently blocking every future cron firing for the
        provider and inflating ``_active_run_count`` toward the concurrency
        cap. (Startup ``web.db.reconcile_running_runs`` remains the backstop
        for rows stranded across a restart.)
        """
        consecutive_poll_failures = 0
        watch_started_utc = datetime.now(timezone.utc)
        run_id_short = run_id[:8]
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

                started = _parse_utc_started_at(record.get("started_at"))
                deadline_base = (
                    started if started is not None else watch_started_utc
                )
                if datetime.now(timezone.utc) - deadline_base < timedelta(
                    hours=_RUN_WATCH_MAX_HOURS
                ):
                    continue

                logger.error(
                    f"Run watch deadline exceeded for {provider_name} "
                    f"({run_id_short}…): row still 'running' "
                    f"{_RUN_WATCH_MAX_HOURS:g} h past its "
                    f"{'started_at' if started is not None else 'watch start'} "
                    f"— releasing the scheduler guard and marking the run "
                    f"failed (a lost terminal DB write most likely left the "
                    f"row stuck)"
                )
                try:
                    # Record the provider's validated-key count on the flip —
                    # same helper the runner's own failure path uses, scoped to
                    # THIS provider (no temp config survives in the watcher).
                    # Best-effort: an unavailable count omits the column rather
                    # than blocking the terminal write (valid_keys_found=None is
                    # skipped by _update_run_sync). Without it the flipped row
                    # reads valid_keys_found=0 and startup push recovery
                    # (find_unpushed_terminal_runs gates on COALESCE(...)>0)
                    # would never re-dispatch a run that DOES hold valid keys.
                    valid_keys_found: int | None = None
                    try:
                        valid_keys_found = runner._count_valid_keys_for_failed_run(
                            provider_name, None
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Watch-deadline valid-key count unavailable for "
                            f"{run_id_short}… ({exc}) — flipping failed without "
                            f"a count"
                        )
                    runner._update_run_sync(
                        run_id,
                        "failed",
                        finished_at=True,
                        duration_from_started_at=True,
                        valid_keys_found=valid_keys_found,
                        error_message=(
                            "run watch deadline exceeded: row stayed 'running' "
                            f"past {_RUN_WATCH_MAX_HOURS:g} h — scheduler guard "
                            "released (see web/scheduler.py _watch_run)"
                        ),
                        only_if_running=True,
                    )
                except Exception as exc:
                    logger.warning(
                        f"Best-effort failed-flip for stuck run {run_id_short}… "
                        f"raised {exc} — releasing the guard anyway"
                    )
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

    # Re-arm the LATEST cron fire each enabled provider missed while the
    # process was down (MemoryJobStore wipes pending deferral jobs on
    # restart). Never fatal: a catch-up failure must not stop the scheduler.
    try:
        await _schedule_catchups(scheduler, db_path)
    except Exception as exc:
        logger.warning(f"Missed-run catch-up scheduling failed: {exc}")

    scheduler.start()
    logger.info(f"Scheduler started with {len(rows)} job(s)")
    return svc


async def shutdown_scheduler() -> None:
    """Shut down the global scheduler (called from app lifespan shutdown)."""
    svc = _scheduler_service
    if svc is not None:
        await svc.shutdown()
