"""Tests for tools.logger file sinks.

Regression: ``Logger.setup_logger("main")`` used to attach BOTH the global
handler (``logs/main.log``, 50 MiB x 10) and a module handler that resolved to
the same path (``logs/<name>.log``, 20 MiB x 5). Two rotating handlers on one
file corrupt rotation: when one rolls over, the other keeps writing into the
renamed backup.
"""

import logging
import os
import tempfile
import unittest
from pathlib import Path

from tools.logger import Logger, get_logger

_TEST_LOGGER_NAMES = ("main", "web.runner")


class LoggerFileSinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        # Logger hardcodes Path("logs"); run from a scratch dir so nothing
        # touches the repository's logs/ directory.
        os.chdir(self._tmp.name)
        self.logs_dir = Path(self._tmp.name) / "logs"
        self._reset_logger_state()

    def tearDown(self) -> None:
        self._reset_logger_state()
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    @staticmethod
    def _reset_logger_state() -> None:
        """Drop every handler and class-level cache so tests start cold."""
        for name in _TEST_LOGGER_NAMES:
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logger.filters.clear()
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
        Logger._loggers.clear()
        Logger._file_handler = None
        Logger._module_handlers.clear()
        Logger._archived_logs.clear()
        Logger._logs_dir = None
        if hasattr(Logger, "_directory_initialized"):
            delattr(Logger, "_directory_initialized")

    def _handlers_for(self, logger: logging.Logger, filename: str) -> list:
        return [h for h in logger.handlers if getattr(h, "baseFilename", "").endswith(filename)]

    def test_main_logger_has_exactly_one_main_log_handler(self) -> None:
        logger = get_logger("main")

        main_handlers = self._handlers_for(logger, "main.log")
        self.assertEqual(
            len(main_handlers),
            1,
            "expected exactly one handler writing main.log, got "
            f"{len(main_handlers)}: {[getattr(h, 'baseFilename', None) for h in logger.handlers]}",
        )

    def test_main_record_appears_once_in_main_log(self) -> None:
        logger = get_logger("main")
        marker = "main-sink-single-write-marker"

        logger.info(marker)
        Logger.flush_all_handlers()

        main_log = self.logs_dir / "main.log"
        self.assertTrue(main_log.exists(), "main.log was not created")
        text = main_log.read_text(encoding="utf-8")
        self.assertEqual(text.count(marker), 1, f"marker written {text.count(marker)} times; log contents:\n{text}")

    def test_other_logger_keeps_its_own_module_file(self) -> None:
        logger = get_logger("web.runner")
        marker = "web-runner-single-write-marker"

        own_handlers = self._handlers_for(logger, "web.runner.log")
        self.assertEqual(
            len(own_handlers),
            1,
            "expected web.runner to keep one module handler, got "
            f"{[getattr(h, 'baseFilename', None) for h in logger.handlers]}",
        )

        logger.info(marker)
        Logger.flush_all_handlers()

        module_log = self.logs_dir / "web.runner.log"
        self.assertTrue(module_log.exists(), "web.runner.log was not created")
        module_text = module_log.read_text(encoding="utf-8")
        self.assertEqual(module_text.count(marker), 1)
        # Central logging still receives the record exactly once.
        main_text = (self.logs_dir / "main.log").read_text(encoding="utf-8")
        self.assertEqual(main_text.count(marker), 1)


if __name__ == "__main__":
    unittest.main()