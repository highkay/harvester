#!/usr/bin/env python3

"""The 'no-work run' marker (and failure status) for scans that did nothing.

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
        # No stage snapshot -> no proof the search leg ran -> marker only.
        with self.assertLogs("web.runner", level="ERROR") as logs:
            message, failed = self.runner._no_work_degradation(
                "openrouter", "run-1", 0, 0, 0
            )

        self.assertIsNotNone(message)
        assert message is not None  # for type checkers
        self.assertIn("no-work run", message)
        self.assertIn("provider=openrouter", message)
        self.assertIn("links_total=0", message)
        self.assertFalse(failed)
        self.assertTrue(any("no-work run" in r.getMessage() for r in logs.records))

    def test_zero_work_with_search_activity_is_a_failure(self) -> None:
        # The 2026-09-26 08:00 openrouter case: one dork, three denials, zero
        # output. It must NOT look healthy in run_records.
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
            message, failed = self.runner._no_work_degradation(
                "openrouter", "run-1", 0, 0, 0, app
            )

        self.assertTrue(failed)
        assert message is not None
        self.assertIn("recorded as FAILED", message)
        self.assertIn("search_failed=3", message)

    def test_work_or_validated_keys_suppress_the_marker(self) -> None:
        cases = (
            ("validated keys exist", 0, 0, 3),
            ("materials were extracted", 0, 5, 0),
            ("links were discovered", 1200, 0, 0),
        )
        for label, links, materials, valid in cases:
            with self.subTest(label=label):
                message, failed = self.runner._no_work_degradation(
                    "p", "r", links, materials, valid
                )
                self.assertIsNone(message)
                self.assertFalse(failed)

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
            message, failed = self.runner._no_work_degradation(
                "openrouter", "run-1", 0, 0, 0, app
            )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("search_failed=3", message)
        self.assertTrue(failed)

    def test_faulty_app_degrades_to_unavailable_counters(self) -> None:
        with self.assertLogs("web.runner", level="ERROR"):
            message, failed = self.runner._no_work_degradation(
                "p", "r", 0, 0, 0, object()
            )
        assert message is not None
        self.assertIn("stage counters unavailable", message)
        self.assertFalse(failed)

    def test_unavailable_stats_are_not_flagged(self) -> None:
        # links/materials are None when the pipeline stats could not be read —
        # unknown must never read as "did no work".
        for links, materials in ((None, None), (None, 0)):
            message, failed = self.runner._no_work_degradation(
                "p", "r", links, materials, 0
            )
            self.assertIsNone(message)
            self.assertFalse(failed)


if __name__ == "__main__":
    unittest.main()