#!/usr/bin/env python3

"""Admission control: global concurrency cap + deferred retry of busy firings.

Two 2026-09-26 changes to web/scheduler.py:
* a global cap on concurrent runs (run_records-counted, cross-process), and
* a bounded deferred retry when a firing hits 409 (provider still running) or
  429 (cap) — the old behaviour dropped the firing outright, which is how
  github's 6-hourly cron silently disappeared while its daily run was live.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from web import scheduler as scheduler_mod
from web.db import init_db
from web.scheduler import SchedulerService, _run_provider_job


def _run_async(coro):
    return asyncio.run(coro)


def _db_with_rows(*statuses: str) -> str:
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "harvester.db")
    _run_async(init_db(db_path))
    conn = sqlite3.connect(db_path)
    try:
        for status in statuses:
            conn.execute(
                "INSERT INTO run_records (id, provider_name, config_file, status, started_at) "
                "VALUES (?, 'p', 'c.yaml', ?, datetime('now'))",
                (os.urandom(8).hex(), status),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


class TestActiveRunCount(unittest.TestCase):
    def test_counts_only_running_rows(self) -> None:
        db_path = _db_with_rows("running", "running", "completed", "failed")
        self.assertEqual(2, _run_async(scheduler_mod._active_run_count(db_path)))


class TestConcurrencyCap(unittest.TestCase):
    def _service(self, db_path: str) -> SchedulerService:
        svc = SchedulerService.__new__(SchedulerService)
        svc._db_path = db_path
        svc._running = set()
        svc._watch_tasks = {}
        svc._scheduler = MagicMock()
        return svc

    def test_cap_reached_raises_429_without_starting(self) -> None:
        db_path = _db_with_rows("running")
        svc = self._service(db_path)

        with patch.object(scheduler_mod, "_MAX_CONCURRENT_SCANS", 1), patch.object(
            scheduler_mod, "_lazy_get_runner"
        ) as runner:
            with self.assertRaises(HTTPException) as ctx:
                _run_async(svc.start_scan("p", None))

        self.assertEqual(429, ctx.exception.status_code)
        self.assertIn("concurrency cap", str(ctx.exception.detail))
        runner.assert_not_called()
        self.assertEqual(set(), svc._running, "a refused start must not hold the guard")

    def test_below_cap_is_not_refused(self) -> None:
        db_path = _db_with_rows()
        svc = self._service(db_path)
        runner = MagicMock()
        runner.run_scan = AsyncMock(return_value="run-1")

        with patch.object(scheduler_mod, "_MAX_CONCURRENT_SCANS", 6), patch.object(
            scheduler_mod, "_lazy_get_runner", return_value=runner
        ), patch.object(svc, "_watch_run", AsyncMock(return_value=None)):
            run_id = _run_async(svc.start_scan("p", None))

        self.assertEqual("run-1", run_id)
        runner.run_scan.assert_awaited_once()


class TestDeferral(unittest.TestCase):
    def _service(self) -> SchedulerService:
        svc = SchedulerService.__new__(SchedulerService)
        svc._scheduler = MagicMock()
        return svc

    def test_schedule_deferred_adds_one_shot_job(self) -> None:
        svc = self._service()
        add_job = cast(Any, svc._scheduler).add_job

        self.assertTrue(svc.schedule_deferred("github", "examples/config-github.yaml", 0))

        kwargs = add_job.call_args.kwargs
        self.assertEqual(["github", "examples/config-github.yaml", 1], kwargs["args"])
        self.assertEqual(1, kwargs["max_instances"])

    def test_job_defers_instead_of_skipping_on_409(self) -> None:
        svc = MagicMock()
        svc.start_scan = AsyncMock(
            side_effect=HTTPException(status_code=409, detail="Provider p is already running")
        )
        svc.schedule_deferred = MagicMock(return_value=True)

        with patch.object(scheduler_mod, "get_scheduler_service", return_value=svc):
            with self.assertLogs("web.scheduler", level="WARNING") as logs:
                _run_async(_run_provider_job("p", "cfg.yaml"))

        svc.schedule_deferred.assert_called_once_with("p", "cfg.yaml", 0)
        messages = [r.getMessage() for r in logs.records]
        self.assertTrue(any("deferred" in m for m in messages), messages)
        self.assertFalse(any("skipping" in m for m in messages), messages)

    def test_deferral_is_bounded_then_skips(self) -> None:
        svc = MagicMock()
        svc.start_scan = AsyncMock(
            side_effect=HTTPException(status_code=429, detail="concurrency cap reached")
        )
        svc.schedule_deferred = MagicMock(return_value=True)

        with patch.object(scheduler_mod, "get_scheduler_service", return_value=svc), patch.object(
            scheduler_mod, "_MAX_DEFERRALS", 3
        ):
            with self.assertLogs("web.scheduler", level="WARNING") as logs:
                _run_async(_run_provider_job("p", "cfg.yaml", deferred_attempts=3))

        svc.schedule_deferred.assert_not_called()
        self.assertTrue(any("skipping" in r.getMessage() for r in logs.records))


if __name__ == "__main__":
    unittest.main()
