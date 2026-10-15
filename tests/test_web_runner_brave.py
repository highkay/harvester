#!/usr/bin/env python3

"""TDD unit tests for web/runner.py — brave push trigger.

Given a ``PipelineRunner`` created via ``__new__`` (bypassing ``__init__``,
per the note at web/runner.py:73-76),
When ``_on_completed`` is called,
Then a background thread fires ``BravePushService.push_valid_keys`` only for
``provider_name == "brave"`` — symmetric to the existing tavily / serpapi push
blocks, and the generic gpt-load push block must keep firing for every
provider (it self-skips without a ``provider_group_mapping`` row).
"""

from __future__ import annotations

import builtins
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from web.runner import PipelineRunner


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _SyncThread:
    """Fake threading.Thread whose start() runs the target inline.

    Makes the fire-and-forget background threads in ``_on_completed``
    execute synchronously, so mock call assertions are deterministic.
    """

    def __init__(
        self,
        target: object = None,
        args: tuple = (),
        kwargs: dict | None = None,
        daemon: bool = True,
        name: str | None = None,
    ) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        assert callable(self._target)
        self._target(*self._args, **self._kwargs)


_REAL_IMPORT = builtins.__import__


def _import_failing_brave(name: str, *args: Any, **kwargs: Any):
    """__import__ replacement that simulates web.brave_push being missing."""
    if name == "web.brave_push" or name.startswith("web.brave_push."):
        raise ImportError("simulated: web.brave_push not available")
    return _REAL_IMPORT(name, *args, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnCompletedBravePush(unittest.TestCase):
    """_on_completed must trigger brave push iff provider_name == 'brave'."""

    def setUp(self) -> None:
        # Bypass __init__ (no ThreadPoolExecutor / DB / workspace needed).
        self.runner = PipelineRunner.__new__(PipelineRunner)
        self.brave_service = MagicMock()
        self.gptload_service = MagicMock()

    def test_on_completed_brave_pushes_brave_and_gptload(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.brave_push.get_brave_push_service",
                    return_value=self.brave_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a brave scan completes
            self.runner._on_completed("brave", "rid-1")
        # Then: both pushes fire with the same (provider, run_id)
        self.brave_service.push_valid_keys.assert_called_once_with("brave", "rid-1")
        self.gptload_service.push_valid_keys.assert_called_once_with("brave", "rid-1")

    def test_on_completed_deepseek_skips_brave_push(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.brave_push.get_brave_push_service",
                    return_value=self.brave_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a non-brave scan completes
            self.runner._on_completed("deepseek", "rid-2")
        # Then: brave push is skipped, gpt-load push still fires
        self.brave_service.push_valid_keys.assert_not_called()
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "deepseek", "rid-2"
        )

    def test_on_completed_brave_import_error_does_not_raise(self) -> None:
        # Given: web.brave_push import raises ImportError, threads run inline
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ), \
                patch(
                    "builtins.__import__",
                    side_effect=_import_failing_brave,
                ):
            # When: any scan completes
            self.runner._on_completed("brave", "rid-3")
        # Then: no exception propagates; the gpt-load push is unaffected
        self.gptload_service.push_valid_keys.assert_called_once_with("brave", "rid-3")


if __name__ == "__main__":
    unittest.main()
