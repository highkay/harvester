#!/usr/bin/env python3

"""Unit tests for per-run capture of newly-added valid keys (TDD: RED phase).

The production code does not exist yet:

- ``web/runner.py`` ``PipelineRunner._snapshot_valid_keys`` /
  ``_diff_new_keys`` / ``_record_new_keys``
- ``web/db.py`` ``run_new_keys`` table DDL

Every test in this module is expected to FAIL with
``AttributeError: 'PipelineRunner' object has no attribute ...`` until the
feature lands. The ``run_new_keys`` table is created inline in each temp DB
(see :func:`_create_run_new_keys_table`), because it is not part of the web
DB bootstrap DDL at RED time.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from web.crypto import _get_crypto
from web.models import mask_token
from web.runner import PipelineRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Exact DDL contract for the (not yet existing) web/db.py run_new_keys table.
_RUN_NEW_KEYS_DDL = (
    "CREATE TABLE IF NOT EXISTS run_new_keys ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "run_id TEXT NOT NULL, "
    "provider_name TEXT NOT NULL, "
    "task_name TEXT NOT NULL, "
    "key_hash TEXT NOT NULL, "
    "token_masked TEXT NOT NULL, "
    "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
    "UNIQUE(run_id, key_hash))"
)


def _make_runner(workdir: Path, db_path: str) -> PipelineRunner:
    """Build a PipelineRunner via __new__ (skips __init__/ThreadPoolExecutor)."""
    runner = PipelineRunner.__new__(PipelineRunner)
    runner._workspace = workdir
    runner._db_path = db_path
    return runner


def _create_run_new_keys_table(db_path: str) -> None:
    """Create the run_new_keys table inline in a fresh temp DB."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_RUN_NEW_KEYS_DDL)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineRunnerNewKeys(unittest.TestCase):
    """RED tests for _diff_new_keys / _snapshot_valid_keys / _record_new_keys.

    These methods exist on ``web.runner.PipelineRunner`` once the feature is
    implemented; at RED time accessing them raises AttributeError.
    """

    # --------------------------------------------------------------
    # _diff_new_keys — pure set-difference on snapshots
    # --------------------------------------------------------------

    def test_diff_returns_only_new_keys(self) -> None:
        """Given a before and after snapshot,
        When keys are added,
        Then only the added keys are returned per task (sorted).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runner = _make_runner(workdir, str(workdir / "harvester.db"))

            before = {"mimo-cn": {"A"}, "mimo-sg": set()}
            after = {"mimo-cn": {"A", "B"}, "mimo-sg": {"D"}}

            diff = runner._diff_new_keys(before, after)

            self.assertEqual(diff, {"mimo-cn": ["B"], "mimo-sg": ["D"]})

    def test_diff_empty_when_no_change(self) -> None:
        """Given identical before/after snapshots,
        When the diff is computed,
        Then the result is empty.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runner = _make_runner(workdir, str(workdir / "harvester.db"))

            before = {"github": {"A", "B"}, "kimi": {"C"}}

            self.assertEqual(runner._diff_new_keys(before, dict(before)), {})

    def test_diff_ignores_dropped_keys(self) -> None:
        """Given a key present only in 'before',
        When the diff is computed,
        Then the dropped key must NOT appear.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runner = _make_runner(workdir, str(workdir / "harvester.db"))

            diff = runner._diff_new_keys(
                before={"t": {"A", "B"}},
                after={"t": {"A"}},
            )

            self.assertEqual(diff, {})

    # --------------------------------------------------------------
    # _snapshot_valid_keys — read valid-keys.txt per named task
    # --------------------------------------------------------------

    def test_snapshot_reads_per_task_valid_keys(self) -> None:
        """Given providers/github/valid-keys.txt with dupes and blanks,
        When the github task is snapshotted,
        Then the returned set holds stripped unique non-empty lines.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            github_dir = workdir / "providers" / "github"
            github_dir.mkdir(parents=True)
            (github_dir / "valid-keys.txt").write_text(
                "key1\nkey2\n\nkey1\n", encoding="utf-8"
            )
            runner = _make_runner(workdir, str(workdir / "harvester.db"))

            snapshot = runner._snapshot_valid_keys(["github"])

            self.assertEqual(snapshot, {"github": {"key1", "key2"}})

    def test_snapshot_skips_backup_and_non_task_dirs(self) -> None:
        """Given extra valid-keys.txt files in non-requested provider dirs,
        When only the github task is snapshotted,
        Then only github is read and other dirs are ignored entirely.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            github_dir = workdir / "providers" / "github"
            backup_dir = workdir / "providers" / "backup-x"
            other_dir = workdir / "providers" / "other-task"
            github_dir.mkdir(parents=True)
            backup_dir.mkdir()
            other_dir.mkdir()
            (github_dir / "valid-keys.txt").write_text(
                "ghp_github_1\n", encoding="utf-8"
            )
            (backup_dir / "valid-keys.txt").write_text(
                "backup-secret\n", encoding="utf-8"
            )
            (other_dir / "valid-keys.txt").write_text(
                "other-secret\n", encoding="utf-8"
            )
            runner = _make_runner(workdir, str(workdir / "harvester.db"))

            snapshot = runner._snapshot_valid_keys(["github"])

            self.assertEqual(snapshot, {"github": {"ghp_github_1"}})
            self.assertNotIn("backup-x", snapshot)
            self.assertNotIn("other-task", snapshot)

    def test_snapshot_missing_providers_dir_returns_empty(self) -> None:
        """Given a workspace without any providers dir,
        When snapping an empty task list or an unknown task,
        Then the result is {} / {name: set()} respectively.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            runner = _make_runner(workdir, str(workdir / "harvester.db"))

            self.assertEqual(runner._snapshot_valid_keys([]), {})
            self.assertEqual(
                runner._snapshot_valid_keys(["nope"]), {"nope": set()}
            )

    # --------------------------------------------------------------
    # _record_new_keys — persist newly-added keys (never plaintext)
    # --------------------------------------------------------------

    def test_record_new_keys_inserts_masked_and_hash(self) -> None:
        """Given a run_new_keys table and one added >=12-char key,
        When _record_new_keys is called,
        Then one row is inserted with masked value + hash, no plaintext.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _create_run_new_keys_table(db_path)
            runner = _make_runner(workdir, db_path)

            key_c = "sk-proj-0123456789abcdef"  # >=12 chars
            before = {"github": {"A", "B"}}
            after = {"github": {"A", "B", key_c}}

            inserted = runner._record_new_keys(
                "run-1", "github", before, after
            )

            self.assertEqual(inserted, 1)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT * FROM run_new_keys").fetchall()
            finally:
                conn.close()

            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["run_id"], "run-1")
            self.assertEqual(row["provider_name"], "github")
            self.assertEqual(row["task_name"], "github")
            self.assertEqual(
                row["key_hash"], _get_crypto().hash_token(key_c)
            )
            self.assertEqual(row["token_masked"], mask_token(key_c))

            # The plaintext key must never be persisted in any column.
            column_values = [str(row[col]) for col in row.keys()]
            self.assertNotIn(key_c, column_values)

    def test_record_new_keys_no_rows_when_no_new(self) -> None:
        """Given identical before/after snapshots,
        When _record_new_keys is called,
        Then it returns 0 and inserts no rows.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _create_run_new_keys_table(db_path)
            runner = _make_runner(workdir, db_path)

            before = {"github": {"A", "B"}}

            inserted = runner._record_new_keys(
                "run-2", "github", before, dict(before)
            )

            self.assertEqual(inserted, 0)
            conn = sqlite3.connect(db_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM run_new_keys"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_record_new_keys_never_raises_on_db_error(self) -> None:
        """Given a sqlite3.connect that raises,
        When _record_new_keys is called,
        Then it returns 0 instead of propagating the error.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")
            _create_run_new_keys_table(db_path)
            runner = _make_runner(workdir, db_path)

            with patch(
                "web.runner.sqlite3.connect",
                side_effect=sqlite3.OperationalError("simulated DB failure"),
            ):
                inserted = runner._record_new_keys(
                    "run-3",
                    "github",
                    {"github": set()},
                    {"github": {"sk-proj-NEWKEY123456"}},
                )

            self.assertEqual(inserted, 0)

    # --------------------------------------------------------------
    # Integration: _execute hooks capture new keys in the right order
    # --------------------------------------------------------------

    def test_execute_hooks_capture_in_order(self) -> None:
        """Given a real _execute with every collaborator mocked,
        When the scan completes,
        Then _record_new_keys fires between _update_run_sync and
        _push_completed_tasks with the before/after snapshots.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")

            runner = _make_runner(workdir, db_path)
            runner._running = {}
            runner._locks = {}
            runner._cancel_events = {}

            temp_yaml = workdir / "runtime" / "fake-config.yaml"

            hook_calls: list[str] = []
            snapshots: list[dict[str, set[str]]] = [
                {"t": set()},
                {"t": {"X"}},
            ]

            def _update_side(*_args: object, **_kwargs: object) -> None:
                hook_calls.append("update")

            def _record_new_keys_side(
                *_args: object, **_kwargs: object
            ) -> int:
                hook_calls.append("record")
                return 0

            def _push_side(*_args: object, **_kwargs: object) -> None:
                hook_calls.append("push")

            def _snapshot_side(
                _task_names: list[str],
            ) -> dict[str, set[str]]:
                return snapshots.pop(0) if snapshots else {}

            app = MagicMock()
            app.initialize.return_value = True
            app.run.return_value = None
            app.task_manager = None  # skip completion-listener registration

            # NOTE: the two new-method patches come FIRST so the RED failure
            # names the missing attribute (not an import/setup error).
            with (
                patch.object(
                    runner,
                    "_snapshot_valid_keys",
                    side_effect=_snapshot_side,
                ),
                patch.object(
                    runner,
                    "_record_new_keys",
                    side_effect=_record_new_keys_side,
                ) as record_mock,
                patch.object(
                    runner,
                    "_get_enabled_api_tokens",
                    return_value=["ghp_dummy_token"],
                ),
                patch.object(
                    runner, "_generate_temp_yaml", return_value=temp_yaml
                ),
                patch("main.HarvesterApp", return_value=app),
                patch.object(runner, "_count_valid_keys", return_value=1),
                patch.object(
                    runner,
                    "_update_run_sync",
                    side_effect=_update_side,
                ),
                patch.object(
                    runner,
                    "_push_completed_tasks",
                    side_effect=_push_side,
                ),
                patch.object(
                    runner, "_task_names_from_config", return_value=[]
                ),
            ):
                runner._execute("t", "run-9", config_file=None)

            # (a) called exactly once with run_id/provider + snapshots
            self.assertEqual(record_mock.call_count, 1)
            rec_args, rec_kwargs = record_mock.call_args
            if rec_kwargs:
                self.assertEqual(rec_kwargs["run_id"], "run-9")
                self.assertEqual(rec_kwargs["provider_name"], "t")
                before = rec_kwargs["before"]
                after = rec_kwargs["after"]
            else:
                self.assertEqual(rec_args[0], "run-9")
                self.assertEqual(rec_args[1], "t")
                before = rec_args[2]
                after = rec_args[3]
            self.assertEqual(before, {"t": set()})
            self.assertEqual(after, {"t": {"X"}})

            # (b) hook order: update_run -> record_new_keys -> push
            self.assertIn("record", hook_calls)
            self.assertLess(
                hook_calls.index("update"), hook_calls.index("record")
            )
            self.assertLess(
                hook_calls.index("record"), hook_calls.index("push")
            )


if __name__ == "__main__":
    unittest.main()