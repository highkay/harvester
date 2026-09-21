"""Behavioral tests for the persistence.auto_restore gate in TaskManager.

Root cause pinned 2026-09-06 on fnos prod: every groq run re-ingested the
previous run's links.txt/material.txt/invalid-keys.txt (and the shared
queue_state) before back-up, so no run ever searched a fresh corpus —
nightly links.txt sets were byte-identical while the pool stayed dead.
`persistence.auto_restore` was parsed by the config loader but had no
consumer, so flipping it did nothing. This pins the now-working gate:
auto_restore=true keeps the recovery path, false skips it but still
backs up existing result files.
"""

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from manager.task import TaskManager


def _build(  # noqa: D417 - not a public API; no Args/Raises needed
    auto_restore: bool,
) -> Any:
    tm: Any = TaskManager.__new__(TaskManager)
    tm.config = SimpleNamespace(persistence=SimpleNamespace(auto_restore=auto_restore))
    tm.providers = {"groq": object()}
    tm._filter_recovery = MagicMock(return_value={"search": []})
    queue_manager = MagicMock()
    queue_manager.load_all_queues.return_value = {"search": [], "gather": []}
    result_manager = MagicMock()
    recovered = MagicMock()
    recovered.total_check_tasks.return_value = 0
    recovered.total_acquisition_tasks.return_value = 0
    result_manager.recover_all_tasks.return_value = recovered
    pipeline = MagicMock()
    pipeline.queue_manager = queue_manager
    pipeline.result_manager = result_manager
    tm.pipeline = pipeline
    tm._add_recovered_tasks = MagicMock()
    tm._create_initial_tasks = MagicMock(return_value=[])
    return tm


class TestAutoRestoreGate(unittest.TestCase):
    def test_auto_restore_true_recovers_then_backs_up(self) -> None:
        tm = _build(True)
        tm._on_start()
        tm.pipeline.queue_manager.load_all_queues.assert_called_once()
        tm.pipeline.result_manager.recover_all_tasks.assert_called_once()
        tm._add_recovered_tasks.assert_called_once()
        tm.pipeline.result_manager.backup_all_existing_files.assert_called_once()

    def test_auto_restore_false_skips_recovery_but_backs_up(self) -> None:
        tm = _build(False)
        tm._on_start()
        tm.pipeline.queue_manager.load_all_queues.assert_not_called()
        tm.pipeline.result_manager.recover_all_tasks.assert_not_called()
        tm._add_recovered_tasks.assert_not_called()
        tm.pipeline.result_manager.backup_all_existing_files.assert_called_once()

    def test_auto_restore_missing_config_defaults_false(self) -> None:
        """The default flipped to False on 2026-09-21 (replay ratchet: a
        recurring run re-queued the accumulated link pool every time —
        deepseek reached 267k links / 14.5h runs). With no persistence section
        at all the manager must start clean, still backing up old files."""
        tm = _build(True)
        tm.config = SimpleNamespace()  # no persistence section at all
        tm._on_start()
        tm.pipeline.queue_manager.load_all_queues.assert_not_called()
        tm.pipeline.result_manager.recover_all_tasks.assert_not_called()
        tm.pipeline.result_manager.backup_all_existing_files.assert_called_once()


if __name__ == "__main__":
    unittest.main()