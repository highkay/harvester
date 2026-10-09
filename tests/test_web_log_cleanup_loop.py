#!/usr/bin/env python3

"""Pins the periodic web-mode log-age cleanup (2026-10-09).

The startup-only sweep let stale rotations accumulate over the prod process's
multi-week lifetime (measured: 12 files >7d old after 8 days uptime). The
lifespan now owns a daily ``_log_cleanup_loop`` task; these tests pin that it
sweeps on interval and survives a failed sweep. Timing is wall-clock-bounded
(no ms-precision sleep races — the py3.12/Windows lesson).
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch


class TestLogCleanupLoop(unittest.TestCase):
    def test_sweeps_periodically_until_cancelled(self) -> None:
        from web.app import _log_cleanup_loop

        async def _scenario() -> None:
            calls: list[int] = []
            with patch(
                "web.app.Logger.cleanup_old_logs",
                side_effect=lambda days: calls.append(days),
            ):
                task = asyncio.create_task(
                    _log_cleanup_loop(days=7, interval=0.01)
                )
                deadline = time.monotonic() + 2.0
                while not calls and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.assertGreaterEqual(len(calls), 1, "loop never swept")
            self.assertEqual(calls[0], 7)

        asyncio.run(_scenario())

    def test_failed_sweep_does_not_kill_loop(self) -> None:
        from web.app import _log_cleanup_loop

        async def _scenario() -> None:
            calls: list[int] = []

            def _flaky(days: int) -> None:
                calls.append(days)
                if len(calls) == 1:
                    raise OSError("disk hiccup")

            with patch("web.app.Logger.cleanup_old_logs", side_effect=_flaky):
                task = asyncio.create_task(
                    _log_cleanup_loop(days=7, interval=0.01)
                )
                deadline = time.monotonic() + 2.0
                while len(calls) < 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.assertGreaterEqual(
                len(calls), 2, "loop died after the first failure"
            )

        asyncio.run(_scenario())


if __name__ == "__main__":
    unittest.main()
