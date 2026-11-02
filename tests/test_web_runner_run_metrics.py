#!/usr/bin/env python3

"""Per-run JSON metrics bridge (plan todo 1 / GAP-4, GAP-6 baseline).

Given a completed scan,
When ``PipelineRunner`` finishes ``_execute``,
Then a best-effort ``{workspace}/run-metrics/{run_id}.json`` captures the
existing gather/verdict counters plus the ``cache_hits`` / ``live_probes``
extension points for the verdict-cache integration (todo 6) — and a
metrics-write failure NEVER fails or affects the run outcome.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from state.models import CheckCacheStats, StatsSource
from web.runner import PipelineRunner


def _run_async(coro):
    return asyncio.run(coro)


_RUN_RECORDS_DDL = """CREATE TABLE IF NOT EXISTS run_records (
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


def _init_db(db_path: str) -> None:
    from web.db import init_db

    _run_async(init_db(db_path))


def _make_runner(workdir: Path, db_path: str) -> PipelineRunner:
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
        return dict(
            conn.execute(
                "SELECT * FROM run_records WHERE id = ?", (run_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def _metrics_app(
    *,
    valid: int = 0,
    invalid: int = 0,
    no_quota: int = 0,
    wait_check: int = 0,
    material: int = 0,
    links: int = 10,
    gather: dict | None = None,
) -> MagicMock:
    """MagicMock app backed by StatsSource + real gather-count managers."""
    app = MagicMock()
    app.initialize.return_value = True
    app.task_manager.stats.return_value = SimpleNamespace(
        resource=StatsSource(
            valid=valid,
            invalid=invalid,
            no_quota=no_quota,
            wait_check=wait_check,
            material=material,
            links=links,
        )
    )
    managers = MagicMock()
    managers.values.return_value = (
        [SimpleNamespace(gather_counters=lambda: dict(gather))] if gather else []
    )
    app.task_manager.pipeline.result_manager.managers = managers
    return app


def _run_execute(runner: PipelineRunner, app: MagicMock, run_id: str) -> None:
    fake_yaml = runner._workspace / "runtime" / "fake.yaml"
    with (
        patch.object(runner, "_get_enabled_api_tokens", return_value=["ghp_dummy"]),
        patch.object(runner, "_generate_temp_yaml", return_value=fake_yaml),
        patch("main.HarvesterApp", return_value=app),
        patch.object(runner, "_push_completed_tasks"),
    ):
        runner._execute("test-provider", run_id)


class TestVerdictCounterSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runner = _make_runner(
            Path(self._tmp.name), str(Path(self._tmp.name) / "x.db")
        )

    def test_reads_verdict_buckets_and_recomputes_total(self) -> None:
        app = _metrics_app(
            valid=4, invalid=3, no_quota=2, wait_check=1, material=9
        )
        snap = self.runner._verdict_counter_snapshot(app)
        self.assertEqual(
            snap,
            {
                "valid": 4,
                "invalid": 3,
                "no_quota": 2,
                "wait_check": 1,
                "material": 9,
                "total": 10,
            },
        )

    def test_unavailable_stats_yield_empty(self) -> None:
        app = MagicMock()
        app.task_manager = None
        self.assertEqual(self.runner._verdict_counter_snapshot(app), {})
        self.assertEqual(self.runner._verdict_counter_snapshot(None), {})


class TestCaptureRunMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runner = _make_runner(
            Path(self._tmp.name), str(Path(self._tmp.name) / "x.db")
        )

    def test_payload_uses_check_stage_cache_counters(self) -> None:
        """Todo 6: the CheckStage counters split checked into live + hits."""
        app = _metrics_app(valid=3, invalid=4, no_quota=1, wait_check=2)
        cache = CheckCacheStats(
            live_probes=6,
            cache_hits=4,
            cache_hits_invalid=3,
            cache_hits_no_quota=1,
        )
        app.task_manager.pipeline.stages = {
            "check": SimpleNamespace(get_stats=lambda: SimpleNamespace(cache=cache))
        }

        payload = self.runner._capture_run_metrics(app, "run-cache", "mimo-cn")

        self.assertEqual(payload["verdict"]["total"], 10)
        self.assertEqual(payload["checked"], 10)
        self.assertEqual(payload["live_probes"], 6)
        self.assertEqual(payload["cache_hits"], 4)
        self.assertEqual(payload["live_probes"] + payload["cache_hits"], payload["checked"])
        self.assertEqual(payload["cache"]["cache_hits_invalid"], 3)
        self.assertEqual(payload["cache"]["cache_hits_no_quota"], 1)

    def test_payload_has_gather_verdict_and_cache_extension_points(self) -> None:
        app = _metrics_app(
            valid=4,
            invalid=3,
            no_quota=2,
            wait_check=1,
            material=9,
            gather={"gather_ok": 10, "gather_empty": 2, "gather_304": 1},
        )
        payload = self.runner._capture_run_metrics(app, "run-1", "doubao")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["provider_name"], "doubao")
        self.assertEqual(payload["gather"]["gather_ok"], 10)
        self.assertEqual(payload["verdict"]["total"], 10)
        self.assertEqual(payload["checked"], 10)
        self.assertEqual(payload["live_probes"], 10)
        self.assertEqual(payload["cache_hits"], 0, "no verdict cache yet")


class TestPersistRunMetrics(unittest.TestCase):
    def test_writes_json_file_and_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runner = _make_runner(workdir, str(workdir / "x.db"))
            payload = {
                "schema_version": 1,
                "run_id": "run-x",
                "provider_name": "doubao",
                "cache_hits": 0,
                "live_probes": 3,
            }
            self.assertTrue(runner._persist_run_metrics(payload))
            dest = workdir / "run-metrics" / "run-x.json"
            self.assertTrue(dest.exists())
            self.assertEqual(json.loads(dest.read_text(encoding="utf-8")), payload)

    def test_unwritable_dir_returns_false_no_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            blocker = workdir / "blocker"
            blocker.write_text("a file, not a directory", encoding="utf-8")
            # _workspace == a file -> run-metrics mkdir fails with OSError.
            runner = _make_runner(workdir, str(workdir / "x.db"))
            runner._workspace = blocker
            self.assertFalse(runner._persist_run_metrics({"run_id": "run-y"}))


class TestRunMetricsEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        workdir = Path(self._tmp.name)
        self.workdir = workdir
        self.db_path = str(workdir / "harvester.db")
        _init_db(self.db_path)
        self.runner = _make_runner(workdir, self.db_path)

    def _metrics_path(self, run_id: str) -> Path:
        return self.workdir / "run-metrics" / f"{run_id}.json"

    def test_complete_scan_writes_metrics_file(self) -> None:
        _insert_run(self.db_path, "run-m-1")
        app = _metrics_app(
            valid=4,
            invalid=3,
            no_quota=2,
            wait_check=1,
            material=9,
            links=120,
            gather={"gather_ok": 10, "gather_empty": 2},
        )
        _run_execute(self.runner, app, "run-m-1")

        row = _read_run(self.db_path, "run-m-1")
        self.assertEqual(row["status"], "completed")

        dest = self._metrics_path("run-m-1")
        self.assertTrue(dest.exists(), "per-run metrics must be written")
        payload = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_id"], "run-m-1")
        self.assertEqual(payload["provider_name"], "test-provider")
        self.assertEqual(payload["cache_hits"], 0)
        self.assertEqual(payload["live_probes"], 10)
        self.assertEqual(payload["verdict"]["valid"], 4)
        self.assertEqual(payload["gather"]["gather_ok"], 10)

    def test_metrics_write_oserror_leaves_scan_completed(self) -> None:
        """A metrics-write failure must never fail or affect the run outcome."""
        _insert_run(self.db_path, "run-m-2")
        app = _metrics_app(valid=1, invalid=0, links=20)

        with patch.object(
            self.runner,
            "_persist_run_metrics",
            side_effect=OSError("disk full"),
        ):
            _run_execute(self.runner, app, "run-m-2")

        row = _read_run(self.db_path, "run-m-2")
        self.assertEqual(row["status"], "completed", "metrics failure must not fail the run")
        self.assertFalse(
            self._metrics_path("run-m-2").exists(),
            "no metrics file on write failure",
        )

    def test_unavailable_stats_still_write_zero_metrics(self) -> None:
        """Dead stats -> metrics payload is all-zeros, still written, run ok."""
        _insert_run(self.db_path, "run-m-3")
        app = MagicMock()
        app.initialize.return_value = True
        app.task_manager = None

        _run_execute(self.runner, app, "run-m-3")

        row = _read_run(self.db_path, "run-m-3")
        self.assertEqual(row["status"], "completed")
        payload = json.loads(self._metrics_path("run-m-3").read_text(encoding="utf-8"))
        self.assertEqual(payload["cache_hits"], 0)
        self.assertEqual(payload["live_probes"], 0)
        self.assertEqual(payload["verdict"], {})


class TestSweepOrphanRunMetrics(unittest.TestCase):
    """Given a run-metrics dir of completed runs,
    When the startup sweep runs,
    Then stale (older than the retention horizon) JSON is deleted, fresh JSON
    and non-JSON siblings survive, and a missing/unreadable dir never raises."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workdir = Path(self._tmp.name)
        self.runner = _make_runner(self.workdir, str(self.workdir / "x.db"))

    def _mk(self, name: str, age_seconds: float) -> Path:
        metrics_dir = self.workdir / "run-metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        path = metrics_dir / name
        path.write_text("{}", encoding="utf-8")
        stale = time.time() - age_seconds
        os.utime(path, (stale, stale))
        return path

    def test_deletes_stale_retains_fresh_and_non_json(self) -> None:
        old = self._mk("old.json", 31 * 24 * 3600)
        fresh = self._mk("fresh.json", 0)
        other = self.workdir / "run-metrics" / "notes.txt"
        other.write_text("not json", encoding="utf-8")

        self.runner._sweep_orphan_run_metrics()

        self.assertFalse(old.exists(), "stale metrics JSON must be removed")
        self.assertTrue(fresh.exists(), "fresh metrics JSON must be retained")
        self.assertTrue(other.exists(), "non-JSON siblings are never touched")

    def test_survives_missing_metrics_dir(self) -> None:
        self.runner._sweep_orphan_run_metrics()  # must not raise

    def test_never_raises_on_glob_error(self) -> None:
        with patch("pathlib.Path.glob", side_effect=OSError("boom")):
            self.runner._sweep_orphan_run_metrics()  # must not raise


if __name__ == "__main__":
    unittest.main()