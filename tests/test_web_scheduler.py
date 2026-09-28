#!/usr/bin/env python3

"""Unit tests for web/scheduler.py — APScheduler with MemoryJobStore."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is on sys.path so "web" and "tools" resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_async(coro):
    """Helper to run an async test from a sync unittest method."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test 1: Cron expression validation
# ---------------------------------------------------------------------------


class TestCronValidation(unittest.TestCase):
    """Given cron expressions,
    When validated via CronTrigger.from_crontab,
    Then valid expressions pass and invalid ones raise ValueError.
    """

    def test_valid_cron_passes(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        # Must not raise
        trigger = CronTrigger.from_crontab("0 3 * * *")
        self.assertIsNotNone(trigger)

    def test_garbage_string_raises_valueerror(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        with self.assertRaises(ValueError):
            CronTrigger.from_crontab("not-a-cron")


# ---------------------------------------------------------------------------
# Test 2: Seed data on empty database
# ---------------------------------------------------------------------------


class TestSeedData(unittest.TestCase):
    """Given an empty schedule_config table,
    When init_scheduler is called,
    Then 18 default provider schedules are inserted.
    """

    _EXPECTED_PROVIDERS = frozenset(
        {
            "deepseek",
            "kimi",
            "kimi-ai",
            "kimi-coding",
            "mimo-cn",
            "mimo-sg",
            "qwen-cn",
            "qwen-intl",
            "glm",
            "glm-ai",
            "modelscope",
            "tavily",
            "github",
            "serpapi",
            "agnes-ai",
            # 2026-09-22: seeded providers that prod only had via manual/UI
            # inserts (the seed list ran against an EMPTY table on prod long
            # before these existed, so fresh installs never scheduled them).
            # 2026-09-24: groq removed — GitHub secret-scanning partner +
            # push protection auto-revokes leaked gsk_ keys within minutes,
            # so 22 completed prod runs yielded 0 valid keys (structural).
            "ollama",
            "openrouter",
            "nvidia",
            # 2026-09-28: opencode seeded — endpoint live-verified the same
            # day (public /models list, auth-gated chat probe).
            "opencode",
        }
    )
    _EXPECTED_CRONS = {
        "deepseek": "0 */4 * * *",
        "kimi": "15 */4 * * *",
        "mimo-cn": "30 */4 * * *",
        "qwen-cn": "45 */4 * * *",
        "glm": "0 */6 * * *",
        "modelscope": "0 11 * * *",
        "tavily": "40 */6 * * *",
        "github": "50 */6 * * *",
        "serpapi": "10 */6 * * *",
        "agnes-ai": "35 */6 * * *",
        "glm-ai": "0 13 * * *",
        # 2026-09-24: moved off the 14:00-17:00 Beijing window — measured prod
        # token-cooldown storm concentrated at 10:00-16:00 Beijing (4529
        # warnings on 2026-09-24), which starved these four providers' search
        # stages (qwen-intl completed with 0 links). Evening starts land after
        # the morning chain's search phases have drained the pool.
        "kimi-ai": "0 18 * * *",
        "kimi-coding": "0 19 * * *",
        "mimo-sg": "0 20 * * *",
        "qwen-intl": "0 21 * * *",
        "ollama": "20 3 * * *",
        "openrouter": "40 8 * * *",
        "nvidia": "50 11 * * *",
        "opencode": "0 22 * * *",
    }
    _EXPECTED_CONFIG_FILES = {
        "deepseek": "examples/config-deepseek.yaml",
        "kimi": "examples/config-kimi.yaml",
        "mimo-cn": "examples/config-mimo.yaml",
        "qwen-cn": "examples/config-qwen.yaml",
        "glm": "examples/config-glm.yaml",
        "modelscope": "examples/config-modelscope.yaml",
        "tavily": "examples/config-tavily.yaml",
        "github": "examples/config-github.yaml",
        "serpapi": "examples/config-serpapi.yaml",
        "agnes-ai": "examples/config-agnes-ai.yaml",
        "glm-ai": "examples/config-glm-ai.yaml",
        "kimi-ai": "examples/config-kimi-ai.yaml",
        "kimi-coding": "examples/config-kimi-coding.yaml",
        "mimo-sg": "examples/config-mimo-sg.yaml",
        "qwen-intl": "examples/config-qwen-intl.yaml",
        "ollama": "examples/config-ollama.yaml",
        "openrouter": "examples/config-openrouter.yaml",
        "nvidia": "examples/config-nvidia.yaml",
        "opencode": "examples/config-opencode.yaml",
    }

    def test_seeds_defaults_on_empty_table(self) -> None:
        from web.db import init_db, get_db

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = f"{tmpdir}/test.db"

                # -- Given: an empty database with the schedule_config table --
                await init_db(db_path)

                # Verify truly empty
                db = await get_db(db_path)
                cursor = await db.execute("SELECT COUNT(*) FROM schedule_config")
                row = await cursor.fetchone()
                self.assertEqual(row[0], 0, "schedule_config should be empty before seed")
                await db.close()

                # -- When: init_scheduler runs against this DB --
                from web.scheduler import init_scheduler

                settings = _make_settings(db_path)
                svc = await init_scheduler(settings)

                # -- Then: 19 default rows inserted --
                db2 = await get_db(db_path)
                cursor2 = await db2.execute(
                    "SELECT provider_name, cron_expression, enabled, config_file "
                    "FROM schedule_config ORDER BY provider_name"
                )
                rows = await cursor2.fetchall()
                await db2.close()

                self.assertEqual(
                    len(rows),
                    len(self._EXPECTED_PROVIDERS),
                    f"Expected {len(self._EXPECTED_PROVIDERS)} rows, got {len(rows)}",
                )

                providers = {r[0] for r in rows}
                self.assertEqual(
                    providers,
                    self._EXPECTED_PROVIDERS,
                    f"Provider set mismatch: {providers ^ self._EXPECTED_PROVIDERS}",
                )

                for r in rows:
                    self.assertEqual(
                        r[1],
                        self._EXPECTED_CRONS[r[0]],
                        f"Provider {r[0]} cron mismatch",
                    )
                    self.assertEqual(r[2], 1, f"Provider {r[0]} should be enabled")
                    self.assertEqual(
                        r[3],
                        self._EXPECTED_CONFIG_FILES[r[0]],
                        f"Provider {r[0]} config_file mismatch",
                    )

                # -- Clean up --
                if svc is not None:
                    await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 2b: The seed constant itself (count, uniqueness, exact new tuples)
# ---------------------------------------------------------------------------


class TestSeedListShape(unittest.TestCase):
    """Given the _DEFAULT_SCHEDULES seed constant,
    When inspected directly,
    Then providers are unique, every cron parses, every config file exists on
    disk, and the 2026-09-22 additions carry the exact tuples pinned here.
    """

    # (provider, cron, config_file) — the 2026-09-22 seed additions, plus the
    # github self-bootstrap entry whose cron AGENTS.md documents (already
    # seeded before the change; pinned so it cannot drift silently).
    _PINNED_ENTRIES = {
        "ollama": ("20 3 * * *", "examples/config-ollama.yaml"),
        "openrouter": ("40 8 * * *", "examples/config-openrouter.yaml"),
        "nvidia": ("50 11 * * *", "examples/config-nvidia.yaml"),
        "github": ("50 */6 * * *", "examples/config-github.yaml"),
    }

    def test_groq_is_not_seeded(self) -> None:
        from web.scheduler import _DEFAULT_SCHEDULES

        providers = {entry[0] for entry in _DEFAULT_SCHEDULES}
        self.assertNotIn(
            "groq",
            providers,
            "groq must not be seeded — GitHub partner auto-revocation makes "
            "its scans structurally zero-yield (see AGENTS.md groq section)",
        )

    def test_no_duplicate_provider_names(self) -> None:
        from web.scheduler import _DEFAULT_SCHEDULES

        providers = [entry[0] for entry in _DEFAULT_SCHEDULES]
        duplicates = {p for p in providers if providers.count(p) > 1}
        self.assertEqual(
            duplicates, set(), f"duplicate providers in seed list: {duplicates}"
        )

    def test_pinned_tuples_exact(self) -> None:
        from web.scheduler import _DEFAULT_SCHEDULES

        entries = {p: (cron, cfg) for p, cron, cfg in _DEFAULT_SCHEDULES}
        for provider, expected in self._PINNED_ENTRIES.items():
            self.assertIn(provider, entries, f"{provider} missing from seed list")
            self.assertEqual(
                entries[provider], expected, f"{provider} seed tuple drifted"
            )

    def test_every_seed_cron_parses(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        from web.scheduler import _DEFAULT_SCHEDULES

        for provider, cron, _cfg in _DEFAULT_SCHEDULES:
            try:
                CronTrigger.from_crontab(cron)  # raises ValueError on garbage
            except ValueError:
                self.fail(f"{provider}: invalid cron expression {cron!r}")

    def test_every_seed_config_file_exists(self) -> None:
        from pathlib import Path

        from web.scheduler import _DEFAULT_SCHEDULES

        repo_root = Path(__file__).resolve().parents[1]
        for provider, _cron, cfg in _DEFAULT_SCHEDULES:
            self.assertTrue(
                (repo_root / cfg).is_file(),
                f"{provider}: seed config {cfg} does not exist",
            )


# ---------------------------------------------------------------------------
# Test 3: Re-entrancy guard on trigger_manual
# ---------------------------------------------------------------------------


class TestReentrancyGuard(unittest.TestCase):
    """Given a SchedulerService with a _running set,
    When trigger_manual is called for an already-running provider,
    Then HTTPException(409) is raised.
    When trigger_manual is called for a non-running provider,
    Then "triggered" is returned.
    """

    def test_raises_409_when_already_running(self) -> None:
        from fastapi import HTTPException

        async def _scenario() -> None:
            from web.scheduler import SchedulerService

            mock_scheduler = MagicMock()
            svc = SchedulerService(
                scheduler=mock_scheduler,
                db_path=":memory:",
            )
            svc._running.add("deepseek")

            with self.assertRaises(HTTPException) as ctx:
                await svc.trigger_manual("deepseek")

            self.assertEqual(ctx.exception.status_code, 409)

        _run_async(_scenario())

    def test_returns_triggered_when_not_running(self) -> None:
        async def _scenario() -> None:
            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )

                # trigger_manual now AWAITS the scan start (via start_scan);
                # the fake runner stands in for web.runner.PipelineRunner and
                # the watcher task it arms is cancelled by shutdown().
                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")

                await svc.shutdown()

        _run_async(_scenario())

    def test_is_running_returns_correct_bool(self) -> None:
        from unittest.mock import MagicMock

        from web.scheduler import SchedulerService

        mock_scheduler = MagicMock()
        svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

        self.assertFalse(svc.is_running("deepseek"))
        svc._running.add("deepseek")
        self.assertTrue(svc.is_running("deepseek"))


# ---------------------------------------------------------------------------
# Test 3b: Guard held for the scan's FULL lifetime (2026-09-22 overlap bug)
# ---------------------------------------------------------------------------


class TestGuardHeldForScanLifetime(unittest.TestCase):
    """Regression contract for the anti-overlap guard:

    ``PipelineRunner.run_scan`` returns as soon as the scan *thread* starts.
    The old ``_run_provider_job`` released ``_running`` in its ``finally``,
    so during a live scan ``is_running()`` claimed idle, ``trigger_manual``
    answered 202 "triggered", and the runner's own 409 was swallowed by a
    broad except — the UI reported success for a run that never happened.

    Fixed contract (pinned below): the scheduler guard lives while the
    run_records row reads 'running' (watcher task polls ``get_run``),
    start failures release the guard and propagate to the caller.
    """

    def test_guard_held_while_run_is_live_and_second_trigger_409s(self) -> None:
        from fastapi import HTTPException

        async def _scenario() -> None:
            fake = _FakeRunner()  # status stays "running" until flipped

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ):
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")

                    # Regression: the guard SURVIVES run_scan returning ...
                    self.assertTrue(svc.is_running("deepseek"))
                    # ... and stays held across watcher polls of a live run.
                    for _ in range(5):
                        await asyncio.sleep(0.02)
                    self.assertTrue(svc.is_running("deepseek"))

                    # A second manual trigger while live must fail honestly.
                    with self.assertRaises(HTTPException) as ctx:
                        await svc.trigger_manual("deepseek")
                    self.assertEqual(ctx.exception.status_code, 409)
                    self.assertEqual(
                        len(fake.run_scan_calls), 1, "no second scan may start"
                    )

                await svc.shutdown()

        _run_async(_scenario())

    def test_guard_released_when_run_reaches_terminal_status(self) -> None:
        async def _scenario() -> None:
            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake), patch(
                    "web.scheduler._WATCH_POLL_SECONDS", 0.01
                ):
                    await svc.trigger_manual("deepseek")
                    self.assertTrue(svc.is_running("deepseek"))

                    # -- When: the run_records row turns terminal --
                    fake.status = "completed"
                    for _ in range(200):
                        await asyncio.sleep(0.01)
                        if not svc.is_running("deepseek"):
                            break

                    # -- Then: guard released, provider triggerable again --
                    self.assertFalse(svc.is_running("deepseek"))
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")
                    self.assertEqual(len(fake.run_scan_calls), 2)

                await svc.shutdown()

        _run_async(_scenario())

    def test_trigger_manual_propagates_runner_409(self) -> None:
        """The runner's own guard (scan thread alive but watcher released, or
        a runner row from a pre-fix process) must surface as 409 — never a
        false "triggered"."""
        from fastapi import HTTPException

        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.run_scan_error = HTTPException(
                status_code=409, detail="Provider 'deepseek' is already running"
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    with self.assertRaises(HTTPException) as ctx:
                        await svc.trigger_manual("deepseek")
                    self.assertEqual(ctx.exception.status_code, 409)
                    # A failed start must not leak the scheduler guard.
                    self.assertFalse(svc.is_running("deepseek"))

                await svc.shutdown()

        _run_async(_scenario())

    def test_trigger_manual_maps_missing_config_template_to_404(self) -> None:
        """run_scan raises ValueError when the source YAML is missing — the
        route must see a 404, not a silent success or a bare 500."""
        from fastapi import HTTPException

        async def _scenario() -> None:
            fake = _FakeRunner()
            fake.run_scan_error = ValueError(
                "No example config for provider 'deepseek': expected examples/none.yaml"
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/none.yaml"
                )
                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    with self.assertRaises(HTTPException) as ctx:
                        await svc.trigger_manual("deepseek")
                    self.assertEqual(ctx.exception.status_code, 404)
                    self.assertIn("No example config", str(ctx.exception.detail))
                    self.assertFalse(svc.is_running("deepseek"))

                await svc.shutdown()

        _run_async(_scenario())

    def test_run_provider_job_holds_guard_for_scheduled_scan(self) -> None:
        """The cron path must hold the guard too — the next firing during a
        live scan skips instead of double-starting."""
        async def _scenario() -> None:
            import web.scheduler
            from web.scheduler import _run_provider_job

            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )
                web.scheduler._scheduler_service = svc
                try:
                    with patch(
                        "web.scheduler._lazy_get_runner", return_value=fake
                    ), patch("web.scheduler._WATCH_POLL_SECONDS", 0.01):
                        await _run_provider_job("deepseek", "examples/config-deepseek.yaml")

                        self.assertEqual(
                            fake.run_scan_calls,
                            [("deepseek", "examples/config-deepseek.yaml")],
                        )
                        self.assertTrue(svc.is_running("deepseek"))

                        # A concurrent firing while live must skip (no 2nd scan).
                        await _run_provider_job("deepseek", "examples/config-deepseek.yaml")
                        self.assertEqual(len(fake.run_scan_calls), 1)

                        fake.status = "failed"
                        for _ in range(200):
                            await asyncio.sleep(0.01)
                            if not svc.is_running("deepseek"):
                                break
                        self.assertFalse(svc.is_running("deepseek"))
                finally:
                    web.scheduler._scheduler_service = None
                await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 4: next_run_time exists after add_job
# ---------------------------------------------------------------------------


class TestNextRunTime(unittest.TestCase):
    """Given a real AsyncIOScheduler,
    When a job is added via update_schedule,
    Then get_job returns a non-None job for that provider.
    """

    def test_get_job_returns_non_none_after_add(self) -> None:
        async def _scenario() -> None:
            import sqlite3

            from apscheduler.executors.asyncio import AsyncIOExecutor
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from web.scheduler import SchedulerService

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = f"{tmpdir}/test.db"

                # Manually create the schedule_config table
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schedule_config ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "provider_name TEXT NOT NULL UNIQUE, "
                    "cron_expression TEXT NOT NULL DEFAULT '0 3 * * *', "
                    "enabled INTEGER NOT NULL DEFAULT 1, "
                    "config_file TEXT NOT NULL, "
                    "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                    "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
                    ")"
                )
                conn.execute(
                    "INSERT INTO schedule_config "
                    "(provider_name, cron_expression, enabled, config_file) "
                    "VALUES (?, ?, ?, ?)",
                    ("test-prov", "0 3 * * *", 1, "examples/config-test.yaml"),
                )
                conn.commit()
                conn.close()

                # Match production init_scheduler config: async job callback
                # (_run_provider_job) must run on AsyncIOExecutor, otherwise it
                # is never awaited and the job silently does nothing.
                scheduler = AsyncIOScheduler(executors={"default": AsyncIOExecutor()})
                scheduler.start()
                svc = SchedulerService(scheduler=scheduler, db_path=db_path)

                await svc.update_schedule(
                    provider_name="test-prov",
                    cron_expression="*/5 * * * *",
                    enabled=True,
                    config_file="examples/config-test.yaml",
                )

                job = scheduler.get_job("scan-test-prov")
                self.assertIsNotNone(job, "Job should exist after update_schedule")

                scheduler.shutdown(wait=False)

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 5: Mock PipelineRunner — job callback invokes runner
# ---------------------------------------------------------------------------


class TestJobCallbackInvokesRunner(unittest.TestCase):
    """Given a mocked PipelineRunner,
    When the job callback fires for a provider,
    Then get_runner().run_scan() is called with provider_name.
    """

    def test_job_callback_calls_runner(self) -> None:
        async def _scenario() -> None:
            mock_runner = MagicMock()
            mock_runner.run_scan = AsyncMock()

            # Set up a scheduler service so _run_provider_job doesn't bail early
            from web.scheduler import SchedulerService

            mock_scheduler = MagicMock()
            svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

            import web.scheduler
            web.scheduler._scheduler_service = svc

            try:
                with patch(
                    "web.scheduler._lazy_get_runner", return_value=mock_runner
                ) as mock_get_runner:
                    from web.scheduler import _run_provider_job

                    await _run_provider_job("deepseek")

                    mock_get_runner.assert_called_once()
                    mock_runner.run_scan.assert_called_once_with("deepseek", None)
            finally:
                web.scheduler._scheduler_service = None

        _run_async(_scenario())

    def test_job_callback_passes_config_file_to_runner(self) -> None:
        """Given a mocked runner,
        When _run_provider_job fires with an explicit config_file,
        Then run_scan is called with (provider_name, config_file).
        """
        async def _scenario() -> None:
            mock_runner = MagicMock()
            mock_runner.run_scan = AsyncMock()

            from web.scheduler import SchedulerService

            mock_scheduler = MagicMock()
            svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

            import web.scheduler
            web.scheduler._scheduler_service = svc

            try:
                with patch(
                    "web.scheduler._lazy_get_runner", return_value=mock_runner
                ):
                    from web.scheduler import _run_provider_job

                    await _run_provider_job("mimo-cn", "examples/config-mimo.yaml")

                    mock_runner.run_scan.assert_called_once_with(
                        "mimo-cn", "examples/config-mimo.yaml"
                    )
            finally:
                web.scheduler._scheduler_service = None

        _run_async(_scenario())

    def test_job_callback_handles_missing_runner_module(self) -> None:
        async def _scenario() -> None:
            from web.scheduler import SchedulerService
            mock_scheduler = MagicMock()
            svc = SchedulerService(scheduler=mock_scheduler, db_path=":memory:")

            import web.scheduler
            web.scheduler._scheduler_service = svc

            try:
                with patch(
                    "web.scheduler._lazy_get_runner",
                    side_effect=ImportError("No module named 'web.runner'"),
                ):
                    from web.scheduler import _run_provider_job

                    # Must not raise
                    await _run_provider_job("deepseek")
            finally:
                web.scheduler._scheduler_service = None

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 6: config_file threading through scheduler jobs
# ---------------------------------------------------------------------------


class TestConfigFileThreading(unittest.TestCase):
    """Given schedule rows carrying config_file,
    When update_schedule / trigger_manual run,
    Then config_file is threaded into the job args and the job call.
    """

    _SCHEDULE_TABLE_DDL = (
        "CREATE TABLE IF NOT EXISTS schedule_config ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "provider_name TEXT NOT NULL UNIQUE, "
        "cron_expression TEXT NOT NULL DEFAULT '0 3 * * *', "
        "enabled INTEGER NOT NULL DEFAULT 1, "
        "config_file TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )

    def test_update_schedule_passes_config_file_in_job_args(self) -> None:
        """update_schedule must add the job with args=(provider, config_file)."""
        async def _scenario() -> None:
            from web.scheduler import SchedulerService

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = f"{tmpdir}/test.db"
                conn = sqlite3.connect(db_path)
                conn.execute(self._SCHEDULE_TABLE_DDL)
                conn.commit()
                conn.close()

                mock_scheduler = MagicMock()
                mock_scheduler.get_job.return_value = None
                svc = SchedulerService(scheduler=mock_scheduler, db_path=db_path)

                await svc.update_schedule(
                    provider_name="mimo-cn",
                    cron_expression="0 3 * * *",
                    enabled=True,
                    config_file="examples/config-mimo.yaml",
                )

                mock_scheduler.add_job.assert_called_once()
                self.assertEqual(
                    mock_scheduler.add_job.call_args.kwargs["args"],
                    ("mimo-cn", "examples/config-mimo.yaml"),
                )

        _run_async(_scenario())

    def test_trigger_manual_passes_config_file_to_job(self) -> None:
        """trigger_manual must read config_file from schedule_config and pass
        it to the runner's run_scan (via start_scan)."""
        async def _scenario() -> None:
            fake = _FakeRunner()

            with tempfile.TemporaryDirectory() as tmpdir:
                svc = _make_service_with_row(
                    tmpdir, "deepseek", "examples/config-deepseek.yaml"
                )

                with patch("web.scheduler._lazy_get_runner", return_value=fake):
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")
                    self.assertEqual(
                        fake.run_scan_calls,
                        [("deepseek", "examples/config-deepseek.yaml")],
                    )

                await svc.shutdown()

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Test 7: Missed-run catch-up at startup (2026-09-28 deferral-loss class)
# ---------------------------------------------------------------------------


class TestMissedRunCatchup(unittest.TestCase):
    """Given the scheduler restarting after cron slots passed while down,
    When _schedule_catchups runs over the enabled schedule_config rows,
    Then each provider whose LATEST fire has no run_records row started at or
    after it (in UTC — started_at is UTC, cron fires are scheduler-tz) and no
    live 'running' row gets ONE staggered one-shot catch-up job through the
    _run_provider_job path; disabled providers and providers with a run since
    the fire are skipped; the whole feature is env-gated.
    """

    @staticmethod
    async def _make_db(
        db_path: str,
        schedules: list[tuple[str, str, int, str]],
        run_inserts: list[tuple[str, str, str, str]] = (),
    ) -> str:
        """Full-schema DB with the given schedule rows + run_records rows.

        ``run_inserts`` entries are (id, provider, status, started_at_sql)
        where started_at_sql is a test-controlled SQLite expression.
        """
        from web.db import init_db

        await init_db(db_path)
        conn = sqlite3.connect(db_path)
        try:
            for provider, cron, enabled, cfg in schedules:
                conn.execute(
                    "INSERT INTO schedule_config "
                    "(provider_name, cron_expression, enabled, config_file) "
                    "VALUES (?, ?, ?, ?)",
                    (provider, cron, int(enabled), cfg),
                )
            for run_id, provider, status, started_sql in run_inserts:
                conn.execute(
                    "INSERT INTO run_records "
                    "(id, provider_name, config_file, status, started_at) "
                    f"VALUES (?, ?, 'c.yaml', ?, {started_sql})",
                    (run_id, provider, status),
                )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_schedules_catchup_for_missed_enabled_provider(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[
                        ("deepseek", "0 3 * * *", 1, "examples/config-deepseek.yaml")
                    ],
                )
                sched = MagicMock()

                with self.assertLogs("web.scheduler", level="INFO") as logs:
                    count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 1)
                sched.add_job.assert_called_once()
                kwargs = sched.add_job.call_args.kwargs
                self.assertTrue(str(kwargs["id"]).startswith("catchup-deepseek-"))
                self.assertEqual(kwargs["args"][0], "deepseek")
                self.assertEqual(kwargs["args"][1], "examples/config-deepseek.yaml")
                # ISO UTC missed-fire reason travels to the WARNING callback.
                self.assertIn("T", kwargs["args"][2])
                self.assertEqual(kwargs["max_instances"], 1)
                self.assertIsNone(kwargs["misfire_grace_time"])
                # One-shot DateTrigger at now + 60s (first stagger slot).
                run_date = kwargs["trigger"].run_date
                expected = datetime.now(run_date.tzinfo) + timedelta(seconds=60)
                self.assertLess(abs((run_date - expected).total_seconds()), 5)
                self.assertTrue(
                    any(
                        "Scheduled catch-up" in r.getMessage()
                        for r in logs.records
                    ),
                    [r.getMessage() for r in logs.records],
                )

        _run_async(_scenario())

    def test_skipped_when_run_exists_after_fire_time(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                # A row started NOW is at/after ANY latest fire (fires are
                # never in the future), whatever the current wall clock.
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 1, "cfg.yaml")],
                    run_inserts=[("r1", "deepseek", "completed", "datetime('now')")],
                )
                sched = MagicMock()

                count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_skipped_when_provider_disabled(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 0, "cfg.yaml")],
                )
                sched = MagicMock()

                self.assertEqual(await _schedule_catchups(sched, db_path), 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_skipped_when_provider_currently_running(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                # The 'running' row started BEFORE the latest */5 fire, so
                # ONLY the live-run check can suppress this catch-up.
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "*/5 * * * *", 1, "cfg.yaml")],
                    run_inserts=[
                        ("r1", "deepseek", "running", "datetime('now','-1 day')")
                    ],
                )
                sched = MagicMock()

                self.assertEqual(await _schedule_catchups(sched, db_path), 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_staggered_in_provider_order(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[
                        # Insertion order deliberately NOT alphabetical:
                        # ordering comes from ORDER BY provider_name.
                        ("kimi", "0 3 * * *", 1, "examples/config-kimi.yaml"),
                        ("deepseek", "0 3 * * *", 1, "examples/config-deepseek.yaml"),
                    ],
                )
                sched = MagicMock()

                count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 2)
                calls = sched.add_job.call_args_list
                self.assertEqual(calls[0].kwargs["args"][0], "deepseek")
                self.assertEqual(calls[1].kwargs["args"][0], "kimi")
                first = calls[0].kwargs["trigger"].run_date
                second = calls[1].kwargs["trigger"].run_date
                now = datetime.now(second.tzinfo)
                self.assertLess(
                    abs((first - (now + timedelta(seconds=60))).total_seconds()), 5
                )
                self.assertLess(
                    abs((second - (now + timedelta(seconds=120))).total_seconds()), 5
                )

        _run_async(_scenario())

    def test_env_gate_off_disables_feature(self) -> None:
        import web.scheduler as scheduler_mod
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[("deepseek", "0 3 * * *", 1, "cfg.yaml")],
                )
                sched = MagicMock()

                with patch.object(scheduler_mod, "_CATCHUP_ENABLED", False):
                    count = await _schedule_catchups(sched, db_path)

                self.assertEqual(count, 0)
                sched.add_job.assert_not_called()

        _run_async(_scenario())

    def test_utc_started_at_converted_against_scheduler_tz_fire(self) -> None:
        """TIMEZONE TRAP: run_records.started_at is UTC while cron fires are
        computed in the scheduler tz. With a +5:30 zone, a run started AT the
        latest fire (stored as its UTC instant, 5:30 behind the local fire
        string) must count as RUN — a naive string compare would see
        '12:30' < '18:00' and schedule a spurious catch-up."""
        from apscheduler.triggers.cron import CronTrigger

        from web.scheduler import _schedule_catchups

        # Fixed-offset zone (no tzdata dependency on Windows CI): UTC+5:30.
        ist = timezone(timedelta(hours=5, minutes=30))

        def _fire_utc(now: datetime) -> datetime:
            """Latest '0 * * * *' fire at or before *now*, computed via the
            documented get_next_fire_time chain (independent of the module
            helper under test)."""
            trigger = CronTrigger.from_crontab("0 * * * *", timezone=ist)
            local_now = now.astimezone(ist)
            fire = trigger.get_next_fire_time(
                None, local_now - timedelta(minutes=120)
            )
            assert fire is not None
            while True:
                nxt = trigger.get_next_fire_time(fire, fire)
                if nxt is None or nxt > local_now:
                    break
                fire = nxt
            return fire.astimezone(timezone.utc)

        async def _scenario() -> None:
            now = datetime.now(timezone.utc)
            fire_utc = _fire_utc(now)
            ran_at_fire = fire_utc.strftime("%Y-%m-%d %H:%M:%S")
            before_fire = (fire_utc - timedelta(minutes=10)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                # Case 1: a run row started AT the latest fire (UTC string)
                # → correctly converted → SKIP.
                db_ran = await self._make_db(
                    os.path.join(tmpdir, "ran.db"),
                    schedules=[("p", "0 * * * *", 1, "cfg.yaml")],
                    run_inserts=[("r1", "p", "completed", f"'{ran_at_fire}'")],
                )
                sched_ran = MagicMock()
                sched_ran.timezone = ist
                self.assertEqual(await _schedule_catchups(sched_ran, db_ran), 0)
                sched_ran.add_job.assert_not_called()

                # Case 2: the only row started BEFORE the fire → catch-up,
                # and the WARNING reason arg quotes the missed fire in UTC.
                db_missed = await self._make_db(
                    os.path.join(tmpdir, "missed.db"),
                    schedules=[("p", "0 * * * *", 1, "cfg.yaml")],
                    run_inserts=[("r1", "p", "completed", f"'{before_fire}'")],
                )
                sched_missed = MagicMock()
                sched_missed.timezone = ist
                self.assertEqual(await _schedule_catchups(sched_missed, db_missed), 1)
                reason = sched_missed.add_job.call_args.kwargs["args"][2]
                self.assertEqual(reason, fire_utc.isoformat())

        _run_async(_scenario())

    def test_invalid_cron_row_does_not_break_the_pass(self) -> None:
        from web.scheduler import _schedule_catchups

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = await self._make_db(
                    os.path.join(tmpdir, "h.db"),
                    schedules=[
                        ("broken", "not-a-cron", 1, "cfg.yaml"),
                        ("deepseek", "0 3 * * *", 1, "cfg.yaml"),
                    ],
                )
                sched = MagicMock()

                # The unparseable row is skipped (its scan- job logged the
                # parse error during rebuild); the healthy row still catches up.
                self.assertEqual(await _schedule_catchups(sched, db_path), 1)
                self.assertEqual(
                    sched.add_job.call_args.kwargs["args"][0], "deepseek"
                )

        _run_async(_scenario())

    def test_catchup_job_logs_warning_and_delegates(self) -> None:
        import web.scheduler as scheduler_mod
        from web.scheduler import _run_catchup_job

        async def _scenario() -> None:
            with patch.object(
                scheduler_mod, "_run_provider_job", new=AsyncMock()
            ) as mock_run:
                with self.assertLogs("web.scheduler", level="WARNING") as logs:
                    await _run_catchup_job(
                        "deepseek", "cfg.yaml", "2026-09-28T02:00:00+00:00"
                    )
            mock_run.assert_awaited_once_with("deepseek", "cfg.yaml")
            messages = [r.getMessage() for r in logs.records]
            self.assertTrue(
                any("Catch-up firing" in m for m in messages), messages
            )

        _run_async(_scenario())

    def test_init_scheduler_invokes_catchups(self) -> None:
        import types

        import web.scheduler as scheduler_mod
        from web.scheduler import init_scheduler

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "sched.db")
                # Pre-seed ONE far-future row so the seed list is skipped.
                await self._make_db(
                    db_path,
                    schedules=[
                        ("probe", "0 0 1 1 *", 1, "examples/config-deepseek.yaml")
                    ],
                )
                settings = types.SimpleNamespace(db_path=db_path)

                with patch.object(
                    scheduler_mod, "_schedule_catchups", new=AsyncMock(return_value=0)
                ) as mock_catchups:
                    svc = await init_scheduler(settings)
                    try:
                        mock_catchups.assert_awaited_once()
                        self.assertEqual(
                            mock_catchups.await_args.args[1], db_path
                        )
                    finally:
                        await svc.shutdown()
                        # Don't leak the stopped service into the module
                        # global — later tests call the real
                        # shutdown_scheduler and would hit a dead instance.
                        scheduler_mod._scheduler_service = None

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_SCHEDULE_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS schedule_config ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "provider_name TEXT NOT NULL UNIQUE, "
    "cron_expression TEXT NOT NULL DEFAULT '0 3 * * *', "
    "enabled INTEGER NOT NULL DEFAULT 1, "
    "config_file TEXT NOT NULL, "
    "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
    "updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")"
)


class _FakeRunner:
    """Stand-in for web.runner.PipelineRunner.

    ``run_scan`` resolves with a fixed run_id as soon as it is called — like
    the real runner, which returns once the scan *thread* starts. The raised
    error (if any) mirrors run_scan's failure modes: HTTPException(409) from
    the runner's own guard, ValueError for a missing config template.
    ``get_run`` reports the mutable ``status`` so tests can keep the watcher
    polling ('running') and then flip it terminal to observe guard release.
    """

    def __init__(self, run_id: str = "run-fake-1") -> None:
        self.run_id = run_id
        self.status = "running"
        self.run_scan_calls: list[tuple[str, str | None]] = []
        self.run_scan_error: Exception | None = None

    async def run_scan(
        self, provider_name: str, config_file: str | None = None
    ) -> str:
        self.run_scan_calls.append((provider_name, config_file))
        if self.run_scan_error is not None:
            raise self.run_scan_error
        return self.run_id

    async def get_run(self, run_id: str) -> dict[str, object] | None:
        return {"status": self.status}


def _make_service_with_row(tmpdir: str, provider: str, config_file: str):
    """Build a SchedulerService over a fresh DB holding one schedule row."""
    from web.scheduler import SchedulerService

    db_path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEDULE_TABLE_DDL)
    conn.execute(
        "INSERT INTO schedule_config "
        "(provider_name, cron_expression, enabled, config_file) "
        "VALUES (?, ?, ?, ?)",
        (provider, "0 3 * * *", 1, config_file),
    )
    conn.commit()
    conn.close()
    return SchedulerService(scheduler=MagicMock(), db_path=db_path)


def _make_settings(db_path: str):
    """Create a minimal settings-like object for tests."""
    from dataclasses import dataclass, field

    @dataclass
    class _Settings:
        db_path: str = ""
        host: str = "127.0.0.1"
        port: int = 8000
        cors_origins: list = field(default_factory=lambda: ["*"])
        web_auth_key: str = "test-key"
        gpt_load_base_url: str = ""
        gpt_load_auth_key: str = ""
        encryption_key: str | None = None

    return _Settings(db_path=db_path)


if __name__ == "__main__":
    unittest.main()
