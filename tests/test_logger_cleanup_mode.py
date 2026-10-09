#!/usr/bin/env python3

"""Pins the tri-state log-cleanup mode (2026-10-09 root fix).

Default ``keep``: logger initialisation NEVER touches pre-existing log files —
a one-shot in-container probe must not delete or rename the live app's sinks
(that trap caused the 09-29/09-30 forensic gap + deleted-inode disk usage).
``delete``/``archive`` keep the legacy CLI fresh-run behaviour behind the
HARVESTER_LOG_CLEANUP_MODE env opt-in.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import logger as logger_mod
from tools.logger import Logger


class _LoggerDirTestCase(unittest.TestCase):
    """Base: point Logger._logs_dir at a temp dir and reset the init guard."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_dir = Logger._logs_dir
        self._had_initialized = hasattr(Logger, "_directory_initialized")
        self._saved_initialized = getattr(Logger, "_directory_initialized", None)
        self._saved_archived = set(Logger._archived_logs)
        if self._had_initialized:
            del Logger._directory_initialized
        Logger._logs_dir = self.dir
        Logger._archived_logs = set()

    def tearDown(self) -> None:
        Logger._logs_dir = self._saved_dir
        if self._had_initialized:
            Logger._directory_initialized = self._saved_initialized
        elif hasattr(Logger, "_directory_initialized"):
            del Logger._directory_initialized
        Logger._archived_logs = self._saved_archived
        self._tmp.cleanup()

    def _seed_log(self, name: str = "main.log") -> Path:
        p = self.dir / name
        p.write_text("precious forensic history\n", encoding="utf-8")
        return p


class TestKeepModeIsDefault(_LoggerDirTestCase):
    def test_directory_init_keeps_existing_logs(self) -> None:
        f = self._seed_log()
        with patch.object(logger_mod, "LOG_CLEANUP_MODE", "keep"):
            Logger._ensure_logs_directory()
        self.assertTrue(f.exists(), "keep mode must not delete existing logs")
        self.assertEqual(
            f.read_text(encoding="utf-8"), "precious forensic history\n"
        )

    def test_module_log_handling_is_noop(self) -> None:
        f = self._seed_log("web.app.log")
        with patch.object(logger_mod, "LOG_CLEANUP_MODE", "keep"):
            Logger._handle_module_log("web.app")
        self.assertTrue(f.exists(), "keep mode must not touch module logs")
        # no rename side effects either
        self.assertEqual(list(self.dir.glob("*.log")), [f])


class TestDeleteModeOptIn(_LoggerDirTestCase):
    def test_directory_init_deletes(self) -> None:
        f = self._seed_log()
        with patch.object(logger_mod, "LOG_CLEANUP_MODE", "delete"):
            Logger._ensure_logs_directory()
        self.assertFalse(f.exists())

    def test_module_log_deleted(self) -> None:
        f = self._seed_log("manager.log")
        with patch.object(logger_mod, "LOG_CLEANUP_MODE", "delete"):
            Logger._handle_module_log("manager")
        self.assertFalse(f.exists())


class TestArchiveModeOptIn(_LoggerDirTestCase):
    def test_directory_init_archives(self) -> None:
        f = self._seed_log()
        with patch.object(logger_mod, "LOG_CLEANUP_MODE", "archive"):
            Logger._ensure_logs_directory()
        self.assertFalse(f.exists())
        self.assertEqual(len(list(self.dir.glob("main-*.log"))), 1)

    def test_module_log_archived(self) -> None:
        f = self._seed_log("manager.log")
        with patch.object(logger_mod, "LOG_CLEANUP_MODE", "archive"):
            Logger._handle_module_log("manager")
        self.assertFalse(f.exists())
        self.assertEqual(len(list(self.dir.glob("manager-*.log"))), 1)


class TestConstantWiring(unittest.TestCase):
    def test_alias_derivation_consistent(self) -> None:
        import constant.system as cs

        self.assertIn(cs.LOG_CLEANUP_MODE, ("delete", "archive", "keep"))
        self.assertEqual(
            cs.DEFAULT_LOG_CLEANUP_DELETE, cs.LOG_CLEANUP_MODE == "delete"
        )

    def test_env_unset_defaults_to_keep(self) -> None:
        """The SHIPPED default must be keep — the probe trap must stay dead."""
        import constant.system as cs

        env = {
            k: v for k, v in os.environ.items()
            if k != "HARVESTER_LOG_CLEANUP_MODE"
        }
        try:
            with patch.dict(os.environ, env, clear=True):
                reloaded = importlib.reload(cs)
                self.assertEqual(reloaded.LOG_CLEANUP_MODE, "keep")
                self.assertFalse(reloaded.DEFAULT_LOG_CLEANUP_DELETE)
        finally:
            importlib.reload(cs)

    def test_invalid_env_falls_back_to_keep(self) -> None:
        import constant.system as cs

        try:
            with patch.dict(
                os.environ, {"HARVESTER_LOG_CLEANUP_MODE": " shred "}, clear=False
            ):
                reloaded = importlib.reload(cs)
                self.assertEqual(reloaded.LOG_CLEANUP_MODE, "keep")
        finally:
            importlib.reload(cs)

    def test_env_delete_opt_in(self) -> None:
        import constant.system as cs

        try:
            with patch.dict(
                os.environ, {"HARVESTER_LOG_CLEANUP_MODE": "DELETE"}, clear=False
            ):
                reloaded = importlib.reload(cs)
                self.assertEqual(reloaded.LOG_CLEANUP_MODE, "delete")
                self.assertTrue(reloaded.DEFAULT_LOG_CLEANUP_DELETE)
        finally:
            importlib.reload(cs)


if __name__ == "__main__":
    unittest.main()
