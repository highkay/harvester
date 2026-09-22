#!/usr/bin/env python3

"""Lifecycle tests for web/runner.py + web/db.py scan-run correctness:

BUG 1 — failed/reconciled runs must still record ``duration_seconds`` and a
``valid_keys_found`` count scoped to their OWN provider/task directories
(never the first ``valid-keys.txt`` found under ``providers/``).

BUG 2 — cancel is cooperative only: ``cancel_run`` must not release the
provider guard (the scan thread's ``finally`` is the single owner),
terminal writes must not overwrite a 'cancelled' row, and a cancel that
arrives during startup must not be dropped.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from web.runner import PipelineRunner


# ---------------------------------------------------------------------------
# Helpers (same style as tests/test_web_runner.py)
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine from a sync unittest method."""
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
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_RUN_RECORDS_DDL)
        conn.commit()
    finally:
        conn.close()


def _insert_run(
    db_path: str,
    run_id: str,
    provider_name: str = "test-provider",
    status: str = "running",
    started_at_sql: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        if started_at_sql is None:
            conn.execute(
                "INSERT INTO run_records "
                "(id, provider_name, config_file, status) VALUES (?, ?, ?, ?)",
                (run_id, provider_name, "fake.yaml", status),
            )
        else:
            # started_at_sql is a test-controlled SQLite expression, e.g.
            # datetime('now','-90 seconds')
            conn.execute(
                "INSERT INTO run_records "
                "(id, provider_name, config_file, status, started_at) "
                f"VALUES (?, ?, ?, ?, {started_at_sql})",
                (run_id, provider_name, "fake.yaml", status),
            )
        conn.commit()
    finally:
        conn.close()


def _read_run(db_path: str, run_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM run_records WHERE id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()


def _make_runner(workdir: Path, db_path: str) -> PipelineRunner:
    """Build a PipelineRunner via __new__ (skips __init__/ThreadPoolExecutor)."""
    runner = PipelineRunner.__new__(PipelineRunner)
    runner._workspace = workdir
    runner._init_yaml_source_dir = str(workdir)
    runner._db_path = db_path
    runner._running = {}
    runner._locks = {}
    runner._cancel_events = {}
    return runner


def _write_valid_keys(workdir: Path, provider_dir: str, count: int) -> None:
    d = workdir / "providers" / provider_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "valid-keys.txt").write_text(
        "".join(f"key-{provider_dir}-{i}\n" for i in range(count)),
        encoding="utf-8",
    )


def _boom(*_args: object, **_kwargs: object):
    """Sentinel side_effect: reaching it fails the test."""
    raise AssertionError("scan must not start for a pre-cancelled run")


# ---------------------------------------------------------------------------
# BUG 1 — failure path records duration + own-provider valid keys
# ---------------------------------------------------------------------------


class TestFailedRunRecord(unittest.TestCase):
    """Given a scan that fails,
    When the failure branch writes the run row,
    Then duration_seconds is derived from started_at and valid_keys_found
    counts ONLY this run's own provider/task directories.
    """

    def test_failed_execute_records_duration_and_own_provider_keys(self) -> None:
        """Fail before any config exists → count falls back to the provider
        name; a foreign provider dir with more keys must NOT be read."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            # started 90 s in the past → duration must land near 90, not NULL
            _insert_run(
                db_path,
                "run-fail-1",
                started_at_sql="datetime('now','-90 seconds')",
            )

            _write_valid_keys(workdir, "test-provider", 3)
            _write_valid_keys(workdir, "zzz-other-provider", 99)

            runner = _make_runner(workdir, db_path)

            # Fail at the token gate — before temp YAML generation, so the
            # failure branch has no config and must scope by provider name.
            with patch.object(
                runner,
                "_get_enabled_api_tokens",
                side_effect=RuntimeError("no tokens"),
            ):
                runner._execute("test-provider", "run-fail-1")

            row = _read_run(db_path, "run-fail-1")
            self.assertEqual(row["status"], "failed")
            self.assertIn("no tokens", row["error_message"])
            self.assertIsNotNone(row["duration_seconds"])
            self.assertGreaterEqual(row["duration_seconds"], 85.0)
            self.assertLess(row["duration_seconds"], 300.0)
            # Own provider dir (3 keys) — never the foreign dir (99 keys).
            self.assertEqual(row["valid_keys_found"], 3)

    def test_failed_execute_scopes_to_config_task_names(self) -> None:
        """Fail AFTER the temp config exists → count uses the config's task
        names, not the provider-name directory and not foreign directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-fail-2", provider_name="sched-provider")

            _write_valid_keys(workdir, "task-a", 2)
            _write_valid_keys(workdir, "sched-provider", 7)
            _write_valid_keys(workdir, "zzz-other", 50)

            fake_yaml = workdir / "runtime" / "fake-config.yaml"
            fake_yaml.parent.mkdir(parents=True, exist_ok=True)
            fake_yaml.write_text(
                yaml.dump({"tasks": [{"name": "task-a"}]}), encoding="utf-8"
            )

            runner = _make_runner(workdir, db_path)
            app = MagicMock()
            app.initialize.return_value = False  # → RuntimeError in _execute

            with (
                patch.object(
                    runner,
                    "_get_enabled_api_tokens",
                    return_value=["ghp_dummy"],
                ),
                patch.object(
                    runner, "_generate_temp_yaml", return_value=fake_yaml
                ),
                patch("main.HarvesterApp", return_value=app),
            ):
                runner._execute("sched-provider", "run-fail-2")

            row = _read_run(db_path, "run-fail-2")
            self.assertEqual(row["status"], "failed")
            self.assertIn("initialize() failed", row["error_message"])
            self.assertIsNotNone(row["duration_seconds"])
            # task-a (2 keys) — not sched-provider (7) or zzz-other (50).
            self.assertEqual(row["valid_keys_found"], 2)

    def test_update_run_sync_success_path_unchanged(self) -> None:
        """A conditional completed-write on a 'running' row behaves exactly
        like the old unconditional one (duration + valid keys recorded)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-ok-1")
            runner = _make_runner(workdir, db_path)

            runner._update_run_sync(
                run_id="run-ok-1",
                status="completed",
                finished_at=True,
                duration_seconds=1.5,
                valid_keys_found=42,
                only_if_running=True,
            )

            row = _read_run(db_path, "run-ok-1")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["duration_seconds"], 1.5)
            self.assertEqual(row["valid_keys_found"], 42)
            self.assertIsNotNone(row["finished_at"])


class TestCountValidKeysScoping(unittest.TestCase):
    """Given a workspace with several provider directories,
    When the _count_valid_keys file fallback runs,
    Then only the named task directories are read."""

    def _app_with_workspace(self, workspace: Path):
        """Minimal app double whose stats path is unavailable."""
        return SimpleNamespace(
            task_manager=None,
            config=SimpleNamespace(
                global_config=SimpleNamespace(workspace=str(workspace))
            ),
        )

    def test_fallback_reads_only_named_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            _write_valid_keys(workdir, "mine", 4)
            _write_valid_keys(workdir, "aaa-foreign", 100)
            runner = _make_runner(workdir, str(workdir / "x.db"))

            count = runner._count_valid_keys(
                self._app_with_workspace(workdir), ["mine"]
            )
            self.assertEqual(count, 4)

    def test_fallback_zero_when_own_dir_missing(self) -> None:
        """Old behaviour returned the FIRST dir found; a missing own-dir
        with a fat foreign dir must now yield 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            _write_valid_keys(workdir, "aaa-foreign", 100)
            runner = _make_runner(workdir, str(workdir / "x.db"))

            count = runner._count_valid_keys(
                self._app_with_workspace(workdir), ["mine"]
            )
            self.assertEqual(count, 0)

    def test_fallback_sums_multi_task_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            _write_valid_keys(workdir, "task-a", 2)
            _write_valid_keys(workdir, "task-b", 3)
            _write_valid_keys(workdir, "zzz-other", 77)
            runner = _make_runner(workdir, str(workdir / "x.db"))

            count = runner._count_valid_keys(
                self._app_with_workspace(workdir), ["task-a", "task-b"]
            )
            self.assertEqual(count, 5)

    def test_stats_path_still_wins(self) -> None:
        """In-memory stats remain the primary source (unchanged behaviour)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            _write_valid_keys(workdir, "mine", 4)
            runner = _make_runner(workdir, str(workdir / "x.db"))

            tm = MagicMock()
            tm.stats.return_value = SimpleNamespace(
                resource=SimpleNamespace(valid=5)
            )
            app = SimpleNamespace(
                task_manager=tm,
                config=SimpleNamespace(
                    global_config=SimpleNamespace(workspace=str(workdir))
                ),
            )
            self.assertEqual(runner._count_valid_keys(app, ["mine"]), 5)


# ---------------------------------------------------------------------------
# BUG 2 — cancel: guard ownership, conditional terminal writes, early cancel
# ---------------------------------------------------------------------------


class TestCancelGuardOwnership(unittest.TestCase):
    """Given a running scan,
    When cancel_run is called,
    Then the provider guard stays held and the cancel event is set."""

    def test_cancel_keeps_running_guard_and_sets_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-c-1")

            runner = _make_runner(workdir, db_path)
            event = threading.Event()
            runner._running = {"test-provider": "run-c-1"}
            runner._cancel_events = {"run-c-1": event}

            async def run_test() -> None:
                result = await runner.cancel_run("run-c-1")
                self.assertTrue(result)

                # (a) The guard must NOT be released by cancel_run — the
                # scan thread's finally block is the single owner.
                self.assertEqual(
                    runner._running, {"test-provider": "run-c-1"}
                )
                self.assertTrue(event.is_set())

                run = await runner.get_run("run-c-1")
                assert run is not None
                self.assertEqual(run["status"], "cancelled")
                self.assertIsNotNone(run["finished_at"])

            _run_async(run_test())

    def test_cancel_returns_false_when_row_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-c-2", status="completed")

            runner = _make_runner(workdir, db_path)
            self.assertFalse(_run_async(runner.cancel_run("run-c-2")))

    def test_finally_pop_scoped_to_own_run_id(self) -> None:
        """The scan thread's finally must release ONLY its own guard entry.

        A foreign run_id in _running (guard handed to another scan) must
        survive run A's exit — the old unconditional pop caused cascading
        guard loss.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-A")
            runner = _make_runner(workdir, db_path)
            runner._running = {"test-provider": "run-B"}  # foreign entry

            with patch.object(
                runner,
                "_get_enabled_api_tokens",
                side_effect=RuntimeError("boom"),
            ):
                runner._execute("test-provider", "run-A")

            self.assertEqual(runner._running, {"test-provider": "run-B"})
            # own entry case: it IS released
            runner._running = {"test-provider": "run-A"}
            with patch.object(
                runner,
                "_get_enabled_api_tokens",
                side_effect=RuntimeError("boom"),
            ):
                runner._execute("test-provider", "run-A")
            self.assertEqual(runner._running, {})


class TestConditionalTerminalWrites(unittest.TestCase):
    """Given a row already flipped to 'cancelled',
    When the scan thread writes its terminal status,
    Then the cancelled row is not overwritten (completed OR failed)."""

    def test_completed_write_cannot_flip_cancelled_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-t-1", status="cancelled")
            runner = _make_runner(workdir, db_path)

            runner._update_run_sync(
                run_id="run-t-1",
                status="completed",
                finished_at=True,
                duration_seconds=9.9,
                valid_keys_found=7,
                only_if_running=True,
            )

            row = _read_run(db_path, "run-t-1")
            self.assertEqual(row["status"], "cancelled")
            self.assertIsNone(row["duration_seconds"])
            self.assertEqual(row["valid_keys_found"], 0)

    def test_failed_write_cannot_flip_cancelled_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-t-2", status="cancelled")
            runner = _make_runner(workdir, db_path)

            runner._update_run_sync(
                run_id="run-t-2",
                status="failed",
                finished_at=True,
                error_message="RuntimeError: late failure",
                duration_from_started_at=True,
                only_if_running=True,
            )

            row = _read_run(db_path, "run-t-2")
            self.assertEqual(row["status"], "cancelled")
            self.assertIsNone(row["error_message"])
            self.assertIsNone(row["duration_seconds"])

    def test_execute_completion_after_cancel_keeps_cancelled_row(self) -> None:
        """End-to-end: cancel lands mid-run, the scan still finishes, and
        its completed-write must not flip the row back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-t-3")
            runner = _make_runner(workdir, db_path)
            runner._running = {"test-provider": "run-t-3"}

            app = MagicMock()
            app.initialize.return_value = True
            app.task_manager = None
            fake_yaml = workdir / "runtime" / "fake.yaml"  # need not exist

            async def run_test() -> None:
                # cancel first (row → cancelled), then let the scan finish
                cancelled = await runner.cancel_run("run-t-3")
                self.assertTrue(cancelled)

                with (
                    patch.object(
                        runner,
                        "_get_enabled_api_tokens",
                        return_value=["ghp_dummy"],
                    ),
                    patch.object(
                        runner, "_generate_temp_yaml", return_value=fake_yaml
                    ),
                    patch("main.HarvesterApp", return_value=app),
                    patch.object(runner, "_count_valid_keys", return_value=6),
                    patch.object(runner, "_record_new_keys", return_value=0),
                    patch.object(runner, "_push_completed_tasks"),
                ):
                    runner._execute("test-provider", "run-t-3")

                # The pipeline DID run to completion...
                app.run.assert_called_once()
                # ...yet the row stayed 'cancelled'.
                row = _read_run(db_path, "run-t-3")
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(row["valid_keys_found"], 0)
                # Guard released by the thread's own finally.
                self.assertEqual(runner._running, {})

            _run_async(run_test())


class TestEarlyCancel(unittest.TestCase):
    """Given a cancel that arrives during startup,
    When run_scan / _execute run,
    Then the cancel is honoured and never dropped."""

    def test_run_scan_registers_event_before_thread_body(self) -> None:
        """The event exists the moment run_scan returns, so cancel_run can
        find it even if the thread body never started."""
        _SAMPLE_YAML_TASKS = yaml.dump(
            {"global": {}, "tasks": [{"name": "test-provider"}]}
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            (workdir / "config-test-provider.yaml").write_text(
                _SAMPLE_YAML_TASKS, encoding="utf-8"
            )
            (workdir / "runtime").mkdir()
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)

            runner = _make_runner(workdir, db_path)
            body_started = threading.Event()
            captured: dict[str, object] = {}

            def fake_execute(
                provider_name: str, run_id: str, config_file: str | None = None
            ) -> None:
                # The thread body must see the SAME event run_scan made.
                captured["event"] = runner._cancel_events.get(run_id)
                body_started.set()
                with runner._provider_lock(provider_name):
                    if runner._running.get(provider_name) == run_id:
                        runner._running.pop(provider_name, None)

            async def run_test() -> None:
                with patch.object(runner, "_execute", side_effect=fake_execute):
                    run_id = await runner.run_scan("test-provider")

                # Event exists immediately after run_scan — before any
                # guarantee about the thread body having started.
                self.assertIn(run_id, runner._cancel_events)

                for _ in range(50):
                    if body_started.wait(timeout=0.05):
                        break
                self.assertIs(
                    captured["event"], runner._cancel_events.get(run_id)
                )

                # The row is 'running' and the event is registered → the
                # cancel must land (previously dropped in this window).
                self.assertTrue(await runner.cancel_run(run_id))
                ev = captured["event"]
                assert isinstance(ev, threading.Event)
                self.assertTrue(ev.is_set())

            _run_async(run_test())

    def test_execute_honours_pre_set_cancel_event(self) -> None:
        """A cancel set before the thread body starts must stop the scan
        before ANY work happens (no tokens read, no app initialized)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            _insert_run(db_path, "run-e-1", status="cancelled")

            runner = _make_runner(workdir, db_path)
            runner._running = {"test-provider": "run-e-1"}
            event = threading.Event()
            event.set()  # cancel landed during startup
            runner._cancel_events = {"run-e-1": event}

            with (
                patch.object(
                    runner, "_get_enabled_api_tokens", side_effect=_boom
                ) as tokens_mock,
                patch.object(
                    runner, "_generate_temp_yaml", side_effect=_boom
                ) as yaml_mock,
            ):
                runner._execute("test-provider", "run-e-1")

            # The pre-set event must short-circuit BEFORE any work — the
            # sentinels were never even reached (an AssertionError raised
            # inside _execute would be swallowed by its except branch, so
            # the call counts are the real pin).
            self.assertEqual(tokens_mock.call_count, 0)
            self.assertEqual(yaml_mock.call_count, 0)

            # Row untouched (conditional failed-write is a no-op).
            row = _read_run(db_path, "run-e-1")
            self.assertEqual(row["status"], "cancelled")
            self.assertIsNone(row["error_message"])
            # Guard released, event cleaned up.
            self.assertEqual(runner._running, {})
            self.assertNotIn("run-e-1", runner._cancel_events)

    def test_run_scan_insert_failure_does_not_leak_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            (workdir / "config-test-provider.yaml").write_text(
                "tasks: []\n", encoding="utf-8"
            )
            db_path = str(workdir / "harvester.db")
            _init_db(db_path)
            runner = _make_runner(workdir, db_path)

            async def run_test() -> None:
                with patch.object(
                    runner,
                    "_insert_run_record",
                    side_effect=sqlite3.OperationalError("insert failed"),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        await runner.run_scan("test-provider")
                self.assertEqual(runner._cancel_events, {})
                self.assertEqual(runner._running, {})

            _run_async(run_test())


# ---------------------------------------------------------------------------
# BUG 1 — reconcile_running_runs (web/db.py) records duration + own keys
# ---------------------------------------------------------------------------


class TestReconcileDurationAndKeys(unittest.TestCase):
    """Given a 'running' row left behind by a dead process,
    When reconcile_running_runs runs,
    Then the row gets duration_seconds (from started_at) and a
    valid_keys_found scoped to its OWN provider directory."""

    def test_reconcile_sets_duration_and_own_provider_keys(self) -> None:
        from web.db import init_db, reconcile_running_runs

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "test.db")
            _write_valid_keys(workdir, "deepseek", 2)
            _write_valid_keys(workdir, "zzz-other", 9)

            async def _scenario() -> int:
                await init_db(db_path)
                # started 120 s ago, then the "process died"
                from web.db import get_db

                db = await get_db(db_path)
                await db.execute(
                    "INSERT INTO run_records "
                    "(id, provider_name, config_file, status, started_at) "
                    "VALUES (?, ?, ?, ?, datetime('now','-120 seconds'))",
                    ("r1", "deepseek", "x.yaml", "running"),
                )
                await db.commit()
                await db.close()

                with patch.dict(
                    os.environ, {"HARVESTER_WORKSPACE": str(workdir)}
                ):
                    return await reconcile_running_runs(db_path)

            self.assertEqual(_run_async(_scenario()), 1)

            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT status, duration_seconds, valid_keys_found, "
                    "error_message, finished_at FROM run_records WHERE id='r1'"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["duration_seconds"])
            self.assertGreaterEqual(row["duration_seconds"], 115.0)
            self.assertLess(row["duration_seconds"], 600.0)
            # Own provider dir only.
            self.assertEqual(row["valid_keys_found"], 2)
            self.assertEqual(row["error_message"], "interrupted by service restart")
            self.assertIsNotNone(row["finished_at"])

    def test_reconcile_missing_provider_file_counts_zero(self) -> None:
        from web.db import init_db, reconcile_running_runs

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "test.db")
            _write_valid_keys(workdir, "zzz-other", 9)

            async def _scenario() -> int:
                await init_db(db_path)
                from web.db import get_db

                db = await get_db(db_path)
                await db.execute(
                    "INSERT INTO run_records "
                    "(id, provider_name, config_file, status) "
                    "VALUES (?, ?, ?, ?)",
                    ("r2", "deepseek", "x.yaml", "running"),
                )
                await db.commit()
                await db.close()

                with patch.dict(
                    os.environ, {"HARVESTER_WORKSPACE": str(workdir)}
                ):
                    return await reconcile_running_runs(db_path)

            self.assertEqual(_run_async(_scenario()), 1)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT valid_keys_found, duration_seconds "
                    "FROM run_records WHERE id='r2'"
                ).fetchone()
            finally:
                conn.close()
            assert row is not None
            self.assertEqual(row[0], 0)
            self.assertIsNotNone(row[1])


if __name__ == "__main__":
    unittest.main()
