#!/usr/bin/env python3

"""Zero-yield tripwire (audit JOB 1b/1c): per-run totals + degradation marker.

Given a completed web scan,
When the pipeline walked >1000 links but extraction produced 0 materials
(the historical "healthy-looking scan, zero yield" failure class),
Then ``run_records`` persists ``links_total``/``materials_total``, the status
STAYS 'completed' (a fifth terminal value would break the DB CHECK
constraint, the UI filters and the scheduler), and ``error_message`` carries
a ``zero-yield degradation`` marker that the run-detail UI surfaces and one
ERROR line is logged.  A run WITH materials must not be marked.

Also pins the web/db.py migration: a pre-existing production-shape database
gains the two columns on ``init_db`` without losing rows.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from web.db import init_db as _init_db_async
from web.runner import PipelineRunner


def _run_async(coro):
    return asyncio.run(coro)


# The production schema BEFORE this feature — no links/materials columns.
_LEGACY_RUN_RECORDS_DDL = """CREATE TABLE IF NOT EXISTS run_records (
    id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL,
    config_file TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_seconds REAL,
    valid_keys_found INTEGER DEFAULT 0,
    total_keys_checked INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""


def _make_runner(workdir: Path, db_path: str) -> PipelineRunner:
    """Build a PipelineRunner via __new__ (same style as lifecycle tests)."""
    runner = PipelineRunner.__new__(PipelineRunner)
    runner._workspace = workdir
    runner._init_yaml_source_dir = str(workdir)
    runner._db_path = db_path
    runner._running = {}
    runner._locks = {}
    runner._cancel_events = {}
    return runner


def _insert_run(db_path: str, run_id: str, provider: str = "test-provider") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO run_records "
            "(id, provider_name, config_file, status) VALUES (?, ?, ?, ?)",
            (run_id, provider, "fake.yaml", "running"),
        )
        conn.commit()
    finally:
        conn.close()


def _read_run(db_path: str, run_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM run_records WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _app_with_stats(
    links: int | None, material: int | None, valid: int = 0, counters=None
) -> MagicMock:
    """MagicMock HarvesterApp whose task_manager reports the given totals.

    ``links=None`` simulates unavailable stats (task_manager dead) — the
    runner must then skip the columns instead of writing junk.
    """
    app = MagicMock()
    app.initialize.return_value = True
    if links is None:
        app.task_manager.stats.return_value = SimpleNamespace(resource=None)
    else:
        app.task_manager.stats.return_value = SimpleNamespace(
            resource=SimpleNamespace(valid=valid, links=links, material=material)
        )
    managers = MagicMock()
    managers.values.return_value = (
        [SimpleNamespace(gather_counters=lambda: dict(counters))] if counters else []
    )
    app.task_manager.pipeline.result_manager.managers = managers
    return app


def _run_execute(runner: PipelineRunner, app: MagicMock, run_id: str) -> None:
    """Drive runner._execute through the completed path with app mocked out."""
    fake_yaml = runner._workspace / "runtime" / "fake.yaml"  # need not exist
    with (
        patch.object(runner, "_get_enabled_api_tokens", return_value=["ghp_dummy"]),
        patch.object(runner, "_generate_temp_yaml", return_value=fake_yaml),
        patch("main.HarvesterApp", return_value=app),
        patch.object(runner, "_push_completed_tasks"),
    ):
        runner._execute("test-provider", run_id)


# ---------------------------------------------------------------------------
# db.py migration
# ---------------------------------------------------------------------------


class TestRunRecordsTotalColumnsMigration(unittest.TestCase):
    """Given an existing production-shape DB (no links/materials columns),
    When init_db runs,
    Then both columns are added, existing rows survive with default 0.
    """

    def test_legacy_db_gains_columns_via_init_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "harvester.db")
            conn = sqlite3.connect(db_path)
            conn.execute(_LEGACY_RUN_RECORDS_DDL)
            conn.execute(
                "INSERT INTO run_records "
                "(id, provider_name, config_file, status) "
                "VALUES ('legacy-1', 'glm', 'c.yaml', 'completed')"
            )
            conn.commit()
            conn.close()

            _run_async(_init_db_async(db_path))

            row = _read_run(db_path, "legacy-1")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["links_total"], 0)
            self.assertEqual(row["materials_total"], 0)

    def test_init_db_is_rerunnable(self) -> None:
        """Second init_db on a migrated DB must not raise or duplicate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "harvester.db")
            _run_async(_init_db_async(db_path))
            _run_async(_init_db_async(db_path))
            _insert_run(db_path, "run-mig")
            self.assertEqual(_read_run(db_path, "run-mig")["links_total"], 0)


# ---------------------------------------------------------------------------
# _count_pipeline_totals unit
# ---------------------------------------------------------------------------


class TestCountPipelineTotals(unittest.TestCase):
    def setUp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.runner = _make_runner(Path(tmpdir), str(Path(tmpdir) / "x.db"))

    def test_reads_links_and_materials_from_stats(self) -> None:
        app = _app_with_stats(links=4200, material=17)
        self.assertEqual(self.runner._count_pipeline_totals(app), (4200, 17))

    def test_unavailable_stats_yield_none_pair(self) -> None:
        """(None, None) makes _update_run_sync skip the columns entirely."""
        app = MagicMock()
        app.task_manager = None
        self.assertEqual(self.runner._count_pipeline_totals(app), (None, None))

    def test_non_int_counter_yields_none_pair(self) -> None:
        """int() failure (e.g. a garbage counter) must degrade to (None, None),
        never propagate — MagicMock is NOT usable here because it answers
        int() with 1; a plain object genuinely fails conversion."""
        app = _app_with_stats(links=10, material=1)
        app.task_manager.stats.return_value.resource.links = object()
        self.assertEqual(self.runner._count_pipeline_totals(app), (None, None))


# ---------------------------------------------------------------------------
# Zero-yield tripwire through the real _execute completion path
# ---------------------------------------------------------------------------


class TestZeroYieldTripwire(unittest.TestCase):
    """Given a completed run,
    When links_total > 1000 and materials_total == 0,
    Then one ERROR line + error_message marker land while status stays
    'completed'; a run with materials is never marked.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        workdir = Path(self._tmp.name)
        self.db_path = str(workdir / "harvester.db")
        _run_async(_init_db_async(self.db_path))
        self.runner = _make_runner(workdir, self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zero_yield_run_is_marked_but_stays_completed(self) -> None:
        _insert_run(self.db_path, "run-zero-1")
        app = _app_with_stats(
            links=5000,
            material=0,
            counters={
                "gather_ok": 4990,
                "gather_empty": 5,
                "gather_error_404": 3,
                "gather_error_other": 2,
            },
        )

        with patch("web.runner.logger") as log:
            _run_execute(self.runner, app, "run-zero-1")

        row = _read_run(self.db_path, "run-zero-1")
        # Status must NOT gain a fifth terminal value
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["links_total"], 5000)
        self.assertEqual(row["materials_total"], 0)
        # Degradation marker visible in the run-detail UI / runs API
        self.assertIsNotNone(row["error_message"])
        self.assertIn("zero-yield degradation", row["error_message"])
        self.assertIn("provider=test-provider", row["error_message"])
        self.assertIn("links_total=5000", row["error_message"])
        self.assertIn("gather_ok=4990", row["error_message"])

        # Exactly ONE ERROR line naming the degradation
        error_calls = [c for c in log.error.call_args_list if "zero-yield" in str(c)]
        self.assertEqual(len(error_calls), 1)

    def test_healthy_run_with_materials_is_not_marked(self) -> None:
        _insert_run(self.db_path, "run-ok-1")
        app = _app_with_stats(links=5000, material=37, valid=4)

        with patch("web.runner.logger") as log:
            _run_execute(self.runner, app, "run-ok-1")

        row = _read_run(self.db_path, "run-ok-1")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["links_total"], 5000)
        self.assertEqual(row["materials_total"], 37)
        self.assertEqual(row["valid_keys_found"], 4)
        self.assertIsNone(row["error_message"])
        self.assertFalse(
            any("zero-yield" in str(c) for c in log.error.call_args_list)
        )

    def test_small_corpus_below_threshold_is_not_marked(self) -> None:
        """links_total == 1000 exactly must NOT trip (threshold is >1000)."""
        _insert_run(self.db_path, "run-small")
        app = _app_with_stats(links=1000, material=0)

        _run_execute(self.runner, app, "run-small")

        row = _read_run(self.db_path, "run-small")
        self.assertEqual(row["status"], "completed")
        self.assertIsNone(row["error_message"])

    def test_marker_without_counters_says_unavailable(self) -> None:
        _insert_run(self.db_path, "run-nocounters")
        app = _app_with_stats(links=2000, material=0)  # managers → []

        with patch("web.runner.logger"):
            _run_execute(self.runner, app, "run-nocounters")

        row = _read_run(self.db_path, "run-nocounters")
        self.assertIn("zero-yield degradation", row["error_message"])
        self.assertIn("gather counters unavailable", row["error_message"])

    def test_unavailable_stats_skip_columns_and_tripwire(self) -> None:
        """Dead task_manager → totals stay default, no marker, still completed.

        This is also the pre-migration compatibility pin: the UPDATE omits
        the new columns when the totals are unknown.
        """
        _insert_run(self.db_path, "run-nostats")
        app = MagicMock()
        app.initialize.return_value = True
        app.task_manager = None

        _run_execute(self.runner, app, "run-nostats")

        row = _read_run(self.db_path, "run-nostats")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["links_total"], 0)  # column default, not written
        self.assertEqual(row["materials_total"], 0)
        self.assertIsNone(row["error_message"])

    def test_update_run_sync_skips_none_totals_on_legacy_schema(self) -> None:
        """A legacy DB (no new columns) still accepts writes without totals."""
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_db = str(Path(tmpdir) / "legacy.db")
            conn = sqlite3.connect(legacy_db)
            conn.execute(_LEGACY_RUN_RECORDS_DDL)
            conn.commit()
            conn.close()
            _insert_run(legacy_db, "run-legacy")
            runner = _make_runner(Path(tmpdir), legacy_db)

            runner._update_run_sync(
                run_id="run-legacy",
                status="completed",
                finished_at=True,
                duration_seconds=1.0,
                valid_keys_found=2,
                links_total=None,
                materials_total=None,
                only_if_running=True,
            )

            row = _read_run(legacy_db, "run-legacy")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["valid_keys_found"], 2)
            self.assertNotIn("links_total", row)


if __name__ == "__main__":
    unittest.main()
