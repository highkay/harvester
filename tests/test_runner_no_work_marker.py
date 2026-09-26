#!/usr/bin/env python3

"""The 'no-work run' marker for completed scans that did nothing at all.

Measured 2026-09-26 08:00 (openrouter): the run had ONE condition; its single
search task was denied by the process-wide ``github_api`` rate limiter on all
three attempts, the retry budget dropped it (now logged, see
tests/test_stage_retry_requeue.py), and the pipeline finished in 54 s with
links=0 / materials=0 / valid=0 — status 'completed' and ``error_message``
NULL. The zero-yield tripwire requires >1000 links, so run_records showed
nothing at all. This marker records the reason (status stays 'completed').
"""

from __future__ import annotations

import types
import unittest

from web.runner import PipelineRunner


class TestNoWorkMarker(unittest.TestCase):
    def setUp(self) -> None:
        # No __init__ side effects: the marker only touches the logger.
        self.runner = PipelineRunner.__new__(PipelineRunner)

    def test_zero_work_run_records_reason(self) -> None:
        with self.assertLogs("web.runner", level="ERROR") as logs:
            message = self.runner._no_work_degradation(
                "openrouter", "run-1", 0, 0, 0
            )

        self.assertIsNotNone(message)
        assert message is not None  # for type checkers
        self.assertIn("no-work run", message)
        self.assertIn("provider=openrouter", message)
        self.assertIn("links_total=0", message)
        self.assertTrue(any("no-work run" in r.getMessage() for r in logs.records))

    def test_work_or_validated_keys_suppress_the_marker(self) -> None:
        cases = (
            ("validated keys exist", 0, 0, 3),
            ("materials were extracted", 0, 5, 0),
            ("links were discovered", 1200, 0, 0),
        )
        for label, links, materials, valid in cases:
            with self.subTest(label=label):
                self.assertIsNone(
                    self.runner._no_work_degradation(
                        "p", "r", links, materials, valid
                    )
                )

    def test_stage_counters_are_quoted_when_available(self) -> None:
        app = types.SimpleNamespace(
            task_manager=types.SimpleNamespace(
                pipeline=types.SimpleNamespace(
                    stages={
                        "search": types.SimpleNamespace(
                            get_stats=lambda: types.SimpleNamespace(
                                tasks=types.SimpleNamespace(completed=0, failed=3)
                            )
                        )
                    }
                )
            )
        )

        with self.assertLogs("web.runner", level="ERROR"):
            message = self.runner._no_work_degradation(
                "openrouter", "run-1", 0, 0, 0, app
            )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("search_failed=3", message)

    def test_faulty_app_degrades_to_unavailable_counters(self) -> None:
        with self.assertLogs("web.runner", level="ERROR"):
            message = self.runner._no_work_degradation(
                "p", "r", 0, 0, 0, object()
            )
        assert message is not None
        self.assertIn("stage counters unavailable", message)

    def test_unavailable_stats_are_not_flagged(self) -> None:
        # links/materials are None when the pipeline stats could not be read —
        # unknown must never read as "did no work".
        self.assertIsNone(self.runner._no_work_degradation("p", "r", None, None, 0))
        self.assertIsNone(self.runner._no_work_degradation("p", "r", None, 0, 0))


if __name__ == "__main__":
    unittest.main()