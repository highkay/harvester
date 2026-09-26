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
from datetime import datetime, timedelta, timezone
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
        svc._scheduler.get_jobs.return_value = []
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

    def test_second_ladder_for_the_same_provider_is_refused(self) -> None:
        svc = self._service()
        cast(Any, svc._scheduler).get_jobs.return_value = [
            MagicMock(id="defer-p-1699999999999")
        ]

        self.assertFalse(svc.schedule_deferred("p", "cfg.yaml", 1))
        cast(Any, svc._scheduler).add_job.assert_not_called()
        self.assertTrue(svc.has_pending_deferral("p"))
        self.assertFalse(svc.has_pending_deferral("other"))

    def test_sibling_provider_ladder_does_not_count_as_pending(self) -> None:
        # defer-kimi-ai-* must NOT look like a pending ladder for kimi (same
        # glm/glm-ai): a false positive here silently loses the colliding
        # firing via the "folded" branch.
        svc = self._service()
        cast(Any, svc._scheduler).get_jobs.return_value = [
            MagicMock(id="defer-kimi-ai-1699999999999")
        ]

        self.assertFalse(svc.has_pending_deferral("kimi"))
        self.assertTrue(svc.has_pending_deferral("kimi-ai"))

    def test_ladder_check_reads_scheduler_state_not_a_flag(self) -> None:
        svc = self._service()

        # No jobs queued -> no pending ladder -> scheduling succeeds.
        self.assertTrue(svc.schedule_deferred("p", "cfg.yaml", 0))
        # Once APScheduler reports the job, the check flips with no bookkeeping
        # of ours (a leaked flag used to refuse every future deferral).
        cast(Any, svc._scheduler).get_jobs.return_value = [MagicMock(id="defer-p-1")]
        self.assertTrue(svc.has_pending_deferral("p"))

    def test_delay_escalates_and_the_date_is_timezone_aware(self) -> None:
        svc = self._service()
        # A REAL fixed-offset zone: the production branch (`datetime.now(tz)`)
        # then runs, so a regression back to a naive date is caught here
        # instead of only on a host whose local zone differs from the
        # scheduler's.
        cast(Any, svc._scheduler).timezone = timezone(timedelta(hours=5))
        add_job = cast(Any, svc._scheduler).add_job

        svc.schedule_deferred("p", "cfg.yaml", 2)
        run_date = add_job.call_args.kwargs["trigger"].run_date

        self.assertIsNotNone(run_date.tzinfo, "the deferral date must be aware")
        expected = datetime.now(run_date.tzinfo) + timedelta(
            seconds=scheduler_mod._DEFER_DELAY_SECONDS * 3
        )
        self.assertLess(abs((run_date - expected).total_seconds()), 5)

    def test_terminal_drop_is_logged_as_error(self) -> None:
        svc = MagicMock()
        svc.start_scan = AsyncMock(
            side_effect=HTTPException(status_code=409, detail="provider live")
        )
        svc.schedule_deferred = MagicMock(return_value=False)

        with patch.object(scheduler_mod, "get_scheduler_service", return_value=svc), patch.object(
            scheduler_mod, "_MAX_DEFERRALS", 3
        ):
            with self.assertLogs("web.scheduler", level="ERROR") as logs:
                _run_async(_run_provider_job("p", "cfg.yaml", deferred_attempts=3))

        self.assertTrue(any("firing DROPPED" in r.getMessage() for r in logs.records))

class TestSchedulerJobDefaults(unittest.TestCase):
    """APScheduler defaults to a ONE-SECOND misfire grace.

    On a loaded box a cron firing landing >1 s late is silently skipped
    ("Run time of job ... was missed"), so whole slots can vanish exactly when
    the box is busiest. `init_scheduler` must set a real grace window, coalesce
    and single-instance defaults for every job.
    """

    def test_cron_jobs_built_with_grace_and_coalesce(self) -> None:
        import types

        from web.db import init_db
        from web.scheduler import init_scheduler

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "sched.db")
            _run_async(init_db(db_path))
            # Pre-seed ONE far-future row so _seed_default_schedules is skipped
            # (it only fills an EMPTY table) and no real cron can fire here.
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "INSERT INTO schedule_config (provider_name, cron_expression, "
                    "enabled, config_file) VALUES ('probe', '0 0 1 1 *', 1, "
                    "'examples/config-deepseek.yaml')"
                )
                conn.commit()
            finally:
                conn.close()
            settings = types.SimpleNamespace(db_path=db_path)

            async def scenario():
                svc = await init_scheduler(settings)
                try:
                    jobs = [j for j in svc._scheduler.get_jobs() if j.id == "scan-probe"]
                    self.assertEqual(1, len(jobs), "expected only the pre-seeded probe job")
                    return jobs[0]
                finally:
                    await svc.shutdown()

            job = _run_async(scenario())

        self.assertIsNone(
            job.misfire_grace_time,
            "a finite grace would still drop a firing after a long stall — "
            "the cap/guard deferral ladder is what must see it",
        )
        self.assertTrue(job.coalesce)
        self.assertEqual(1, job.max_instances)


if __name__ == "__main__":
    unittest.main()
