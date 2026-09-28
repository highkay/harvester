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
import time
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


# ---------------------------------------------------------------------------
# BUG 3 — unpushed-completion loss: startup push recovery
# ---------------------------------------------------------------------------


class TestFindUnpushedTerminalRuns(unittest.TestCase):
    """Given a mix of terminal/live/pushed/old/zero-key run rows,
    When find_unpushed_terminal_runs runs,
    Then exactly the terminal, valid (>0), in-window, push_logs-less rows come
    back — oldest first."""

    def test_selects_only_terminal_valid_unpushed_in_window(self) -> None:
        from web.db import find_unpushed_terminal_runs, init_db

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                db_path = os.path.join(tmpdir, "h.db")
                await init_db(db_path)
                conn = sqlite3.connect(db_path)
                try:
                    def ins(
                        run_id: str,
                        status: str,
                        valid: int,
                        finished_sql: str,
                    ) -> None:
                        conn.execute(
                            "INSERT INTO run_records (id, provider_name, "
                            "config_file, status, valid_keys_found, finished_at) "
                            f"VALUES (?, 'p', 'c.yaml', ?, ?, {finished_sql})",
                            (run_id, status, valid),
                        )

                    ins("r-hit", "completed", 5, "datetime('now','-1 hours')")
                    ins("r-hit-failed", "failed", 2, "datetime('now','-2 hours')")
                    ins("r-pushed", "completed", 5, "datetime('now','-1 hours')")
                    ins("r-zero", "completed", 0, "datetime('now','-1 hours')")
                    ins("r-old", "completed", 5, "datetime('now','-25 hours')")
                    ins("r-cancelled", "cancelled", 5, "datetime('now','-1 hours')")
                    conn.execute(
                        "INSERT INTO run_records (id, provider_name, "
                        "config_file, status) VALUES "
                        "('r-running','p','c.yaml','running')"
                    )
                    conn.execute(
                        "INSERT INTO push_logs (run_id, provider_name, "
                        "gpt_load_config_id, group_id, keys_count, added_count, "
                        "ignored_count, status) VALUES "
                        "('r-pushed','p',0,1,5,5,0,'success')"
                    )
                    conn.commit()
                finally:
                    conn.close()

                rows = await find_unpushed_terminal_runs(db_path)
                self.assertEqual(
                    {r["id"] for r in rows}, {"r-hit", "r-hit-failed"}
                )
                self.assertEqual(rows[0]["id"], "r-hit-failed", "oldest first")
                self.assertEqual(rows[0]["provider_name"], "p")
                self.assertEqual(rows[0]["config_file"], "c.yaml")
                self.assertEqual(rows[0]["status"], "failed")
                self.assertEqual(rows[0]["valid_keys_found"], 2)

                # The window is a parameter: 48 h admits the old row.
                wide = await find_unpushed_terminal_runs(db_path, window_hours=48)
                self.assertIn("r-old", {r["id"] for r in wide})

        _run_async(_scenario())


class TestPushRecovery(unittest.TestCase):
    """Given terminal runs whose pushes never fired (process died between the
    terminal row write and the daemon push threads),
    When recover_unpushed_runs runs at startup,
    Then each qualifying run is re-dispatched through _push_completed_tasks in
    a daemon thread — and nothing is dispatched for runs that already have a
    push_logs row, recorded 0 valid keys, or finished outside the window.
    The method never raises."""

    @staticmethod
    def _insert_terminal(
        db_path: str,
        run_id: str,
        *,
        status: str = "completed",
        valid: int = 5,
        finished_sql: str = "datetime('now','-1 hours')",
        config_file: str = "c.yaml",
        pushed: bool = False,
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO run_records (id, provider_name, config_file, "
                "status, valid_keys_found, finished_at) "
                f"VALUES (?, 'test-provider', ?, ?, ?, {finished_sql})",
                (run_id, config_file, status, valid),
            )
            if pushed:
                conn.execute(
                    "INSERT INTO push_logs (run_id, provider_name, "
                    "gpt_load_config_id, group_id, keys_count, added_count, "
                    "ignored_count, status) VALUES "
                    "(?, 'test-provider', 0, 1, 5, 5, 0, 'success')",
                    (run_id,),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _capture_dispatch(runner: PipelineRunner):
        """Patch _push_completed_tasks with a recorder + completion event."""
        calls: list[tuple[str, str, object]] = []
        done = threading.Event()

        def fake_push(provider, run_id, config_path):
            calls.append((provider, run_id, config_path))
            done.set()

        patcher = patch.object(
            runner, "_push_completed_tasks", side_effect=fake_push
        )
        patcher.start()
        return patcher, calls, done

    def test_recovers_terminal_valid_run_without_push_row(self) -> None:
        from web.db import init_db

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                workdir = Path(tmpdir)
                db_path = str(workdir / "h.db")
                await init_db(db_path)
                gone = str(workdir / "runtime" / "config-gone.yaml")
                self._insert_terminal(db_path, "run-p-1", config_file=gone)
                runner = _make_runner(workdir, db_path)

                patcher, calls, done = self._capture_dispatch(runner)
                try:
                    recovered = await runner.recover_unpushed_runs()
                    self.assertEqual(recovered, 1)
                    self.assertTrue(done.wait(timeout=5), "dispatch never ran")
                    self.assertEqual(calls[0][0], "test-provider")
                    self.assertEqual(calls[0][1], "run-p-1")
                    self.assertEqual(str(calls[0][2]), gone)
                finally:
                    patcher.stop()

        _run_async(_scenario())

    def test_skipped_when_push_logs_row_exists(self) -> None:
        from web.db import init_db

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                workdir = Path(tmpdir)
                db_path = str(workdir / "h.db")
                await init_db(db_path)
                # ANY push_logs row skips the run (manual salvage writes one).
                self._insert_terminal(db_path, "run-p-2", pushed=True)
                runner = _make_runner(workdir, db_path)

                patcher, calls, _done = self._capture_dispatch(runner)
                try:
                    self.assertEqual(await runner.recover_unpushed_runs(), 0)
                    self.assertEqual(calls, [])
                finally:
                    patcher.stop()

        _run_async(_scenario())

    def test_skipped_when_valid_keys_zero(self) -> None:
        from web.db import init_db

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                workdir = Path(tmpdir)
                db_path = str(workdir / "h.db")
                await init_db(db_path)
                self._insert_terminal(db_path, "run-p-3", valid=0)
                runner = _make_runner(workdir, db_path)

                patcher, calls, _done = self._capture_dispatch(runner)
                try:
                    self.assertEqual(await runner.recover_unpushed_runs(), 0)
                    self.assertEqual(calls, [])
                finally:
                    patcher.stop()

        _run_async(_scenario())

    def test_skipped_when_finished_outside_window(self) -> None:
        from web.db import init_db

        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                workdir = Path(tmpdir)
                db_path = str(workdir / "h.db")
                await init_db(db_path)
                self._insert_terminal(
                    db_path,
                    "run-p-4",
                    finished_sql="datetime('now','-25 hours')",
                )
                runner = _make_runner(workdir, db_path)

                patcher, calls, _done = self._capture_dispatch(runner)
                try:
                    self.assertEqual(await runner.recover_unpushed_runs(), 0)
                    self.assertEqual(calls, [])
                finally:
                    patcher.stop()

        _run_async(_scenario())

    def test_never_raises_on_unreadable_db(self) -> None:
        """A dead/missing database (or a schema without push_logs) must log a
        warning and return 0 — startup recovery is best-effort by contract."""
        async def _scenario() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                workdir = Path(tmpdir)
                # No schema at all: connecting creates an empty file whose
                # run_records query raises OperationalError.
                runner = _make_runner(workdir, str(workdir / "missing.db"))
                self.assertEqual(await runner.recover_unpushed_runs(), 0)

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# BUG 4 — runtime orphan sweep at runner startup
# ---------------------------------------------------------------------------


class TestRuntimeOrphanSweep(unittest.TestCase):
    """Given leftover runtime/config-*.yaml files of killed runs,
    When the sweep runs (runner startup),
    Then only config-*.yaml files older than the cutoff are deleted."""

    def test_sweep_deletes_only_old_config_yamls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runtime = workdir / "runtime"
            runtime.mkdir()
            old = runtime / "config-deepseek-aaa.yaml"
            fresh = runtime / "config-deepseek-bbb.yaml"
            other = runtime / "notes.yaml"
            for p in (old, fresh, other):
                p.write_text("x: 1\n", encoding="utf-8")
            stale = time.time() - 7200
            os.utime(old, (stale, stale))
            os.utime(other, (stale, stale))

            runner = _make_runner(workdir, str(workdir / "h.db"))
            runner._sweep_orphan_runtime_configs()

            self.assertFalse(old.exists(), "stale orphan must be removed")
            self.assertTrue(fresh.exists(), "fresh config must survive")
            self.assertTrue(other.exists(), "non-config files are not touched")

    def test_sweep_survives_missing_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runner = _make_runner(workdir, str(workdir / "h.db"))
            runner._sweep_orphan_runtime_configs()  # must not raise

    def test_constructor_sweeps_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runtime = workdir / "runtime"
            runtime.mkdir()
            old = runtime / "config-x-1.yaml"
            old.write_text("t: 1\n", encoding="utf-8")
            stale = time.time() - 7200
            os.utime(old, (stale, stale))

            env = {
                "HARVESTER_WORKSPACE": str(workdir),
                "HARVESTER_DB_PATH": str(workdir / "h.db"),
            }
            runner: PipelineRunner | None = None
            try:
                with patch.dict(os.environ, env, clear=False):
                    runner = PipelineRunner()
                self.assertFalse(old.exists())
            finally:
                if runner is not None:
                    runner._executor.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
