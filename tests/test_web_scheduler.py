#!/usr/bin/env python3

"""Unit tests for web/scheduler.py — APScheduler with MemoryJobStore."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
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
    Then 4 default provider schedules are inserted.
    """

    _EXPECTED_PROVIDERS = frozenset({"deepseek", "kimi", "mimo-cn", "qwen-cn"})
    _DEFAULT_CRON = "0 3 * * *"

    def test_seeds_four_defaults_on_empty_table(self) -> None:
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

                # -- Then: 4 default rows inserted --
                db2 = await get_db(db_path)
                cursor2 = await db2.execute(
                    "SELECT provider_name, cron_expression, enabled, config_file "
                    "FROM schedule_config ORDER BY provider_name"
                )
                rows = await cursor2.fetchall()
                await db2.close()

                self.assertEqual(len(rows), 4, f"Expected 4 rows, got {len(rows)}")

                providers = {r[0] for r in rows}
                self.assertTrue(
                    self._EXPECTED_PROVIDERS.issubset(providers),
                    f"Missing providers: {self._EXPECTED_PROVIDERS - providers}",
                )

                for r in rows:
                    self.assertEqual(r[1], self._DEFAULT_CRON,
                                     f"Provider {r[0]} cron mismatch")
                    self.assertEqual(r[2], 1, f"Provider {r[0]} should be enabled")

                # Verify expected config_file paths
                config_files = {r[0]: r[3] for r in rows}
                self.assertEqual(config_files["deepseek"], "examples/config-deepseek.yaml")
                self.assertEqual(config_files["kimi"], "examples/config-kimi.yaml")
                self.assertEqual(config_files["mimo-cn"], "examples/config-mimo.yaml")
                self.assertEqual(config_files["qwen-cn"], "examples/config-qwen.yaml")

                # -- Clean up --
                if svc is not None:
                    await svc.shutdown()

        _run_async(_scenario())


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
            from web.scheduler import SchedulerService

            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = f"{tmpdir}/test.db"

                # Create schedule_config table so trigger_manual can query it
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
                    ("deepseek", "0 3 * * *", 1, "examples/config-deepseek.yaml"),
                )
                conn.commit()
                conn.close()

                mock_scheduler = MagicMock()
                mock_scheduler.get_job.return_value = MagicMock()
                svc = SchedulerService(scheduler=mock_scheduler, db_path=db_path)

                # trigger_manual fires asyncio.create_task — patch create_task
                # to close the created coroutine (suppresses the "coroutine
                # never awaited" RuntimeWarning) while patching the job itself
                # with AsyncMock so no real background scan starts.
                def _close_coro(coro: object) -> None:
                    if hasattr(coro, "close"):
                        coro.close()

                with patch("asyncio.create_task", side_effect=_close_coro), patch(
                    "web.scheduler._run_provider_job", new_callable=AsyncMock
                ):
                    result = await svc.trigger_manual("deepseek")
                    self.assertEqual(result, "triggered")

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
                    mock_runner.run_scan.assert_called_once_with("deepseek")
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
# Helper
# ---------------------------------------------------------------------------


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
