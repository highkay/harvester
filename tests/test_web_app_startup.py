#!/usr/bin/env python3

"""Startup hardening tests for web/app.py.

1. Log age cleanup: ``tools.logger.cleanup_old_logs`` only ran from
   ``init_logging``, which only the CLI (main.py) calls — web-mode logs/ had
   no age cleanup. Pinned: web startup runs it once with the CLI default
   retention (7 days) and never raises.

2. Lifespan ordering: the unpushed-run push recovery must run AFTER db init +
   run reconciliation and BEFORE ``init_scheduler`` — the catch-up runs the
   scheduler arms would let a new scan's ``_on_start`` back up and reset
   ``providers/<p>/valid-keys.txt`` before the recovery threads read it.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from web.app import _cleanup_old_web_logs, _lifespan


class TestWebLogCleanup(unittest.TestCase):
    """Given the web startup path,
    When _cleanup_old_web_logs runs,
    Then Logger.cleanup_old_logs is called with the CLI default (7 days) and
    any failure is swallowed."""

    def test_calls_cleanup_with_cli_default_days(self) -> None:
        with patch("web.app.Logger") as mock_logger:
            _cleanup_old_web_logs()

        mock_logger.cleanup_old_logs.assert_called_once_with(days=7)

    def test_never_raises(self) -> None:
        with patch("web.app.Logger") as mock_logger:
            mock_logger.cleanup_old_logs.side_effect = RuntimeError("boom")
            _cleanup_old_web_logs()  # must not raise


class TestLifespanRecoveryOrder(unittest.TestCase):
    """Given the app lifespan with every heavy collaborator stubbed,
    When startup completes,
    Then push recovery runs after init_db/reconciliation and before
    init_scheduler."""

    def test_push_recovery_runs_before_scheduler_init(self) -> None:
        order: list[str] = []

        async def fake_init_db(path):
            order.append("init_db")

        async def fake_reconcile(path):
            order.append("reconcile")
            return 0

        runner = MagicMock()

        async def fake_recover():
            order.append("recover")
            return 0

        runner.recover_unpushed_runs = fake_recover

        async def fake_init_scheduler(settings):
            order.append("scheduler")
            return MagicMock()

        settings = SimpleNamespace(host="127.0.0.1", port=8000, db_path=":memory:")

        async def _scenario() -> None:
            with (
                patch("web.app.Logger"),
                patch("web.app.get_settings", return_value=settings),
                patch("web.app.init_db", new=fake_init_db),
                patch("web.db.reconcile_running_runs", new=fake_reconcile),
                patch("web.runner.get_runner", return_value=runner),
                patch("web.app.init_scheduler", new=fake_init_scheduler),
                # Isolation: earlier tests in the same process may have left
                # web.scheduler._scheduler_service pointing at an already-
                # shut-down service; the lifespan's real shutdown_scheduler
                # would then raise SchedulerNotRunningError.
                patch("web.scheduler._scheduler_service", None),
            ):
                async with _lifespan(MagicMock()):
                    order.append("serve")

        asyncio.run(_scenario())

        self.assertEqual(
            order, ["init_db", "reconcile", "recover", "scheduler", "serve"]
        )


if __name__ == "__main__":
    unittest.main()
