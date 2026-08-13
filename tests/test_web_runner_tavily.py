#!/usr/bin/env python3

"""TDD unit tests for web/runner.py — tavily push trigger (plan Todo 2).

Given a ``PipelineRunner`` created via ``__new__`` (bypassing ``__init__``,
per the note at web/runner.py:73-76),
When ``_on_completed`` is called,
Then a background thread fires ``TavilyPushService.push_valid_keys`` only for
``provider_name == "tavily"`` — symmetric to the existing gpt-load push block,
which must keep firing for every provider.
"""

from __future__ import annotations

import builtins
import unittest
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


def _import_failing_tavily(name: str, *args: object, **kwargs: object):
    """__import__ replacement that simulates web.tavily_push being missing."""
    if name == "web.tavily_push" or name.startswith("web.tavily_push."):
        raise ImportError("simulated: web.tavily_push not available")
    return _REAL_IMPORT(name, *args, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnCompletedTavilyPush(unittest.TestCase):
    """_on_completed must trigger tavily push iff provider_name == 'tavily'."""

    def setUp(self) -> None:
        # Bypass __init__ (no ThreadPoolExecutor / DB / workspace needed).
        self.runner = PipelineRunner.__new__(PipelineRunner)
        self.tavily_service = MagicMock()
        self.gptload_service = MagicMock()

    def test_on_completed_tavily_pushes_tavily_and_gptload(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a tavily scan completes
            self.runner._on_completed("tavily", "rid-1")
        # Then: both pushes fire with the same (provider, run_id)
        self.tavily_service.push_valid_keys.assert_called_once_with(
            "tavily", "rid-1"
        )
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "tavily", "rid-1"
        )

    def test_on_completed_deepseek_skips_tavily_push(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a non-tavily scan completes
            self.runner._on_completed("deepseek", "rid-2")
        # Then: tavily push is skipped, gpt-load push still fires
        self.tavily_service.push_valid_keys.assert_not_called()
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "deepseek", "rid-2"
        )

    def test_on_completed_tavily_import_error_does_not_raise(self) -> None:
        # Given: web.tavily_push import raises ImportError, threads run inline
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ), \
                patch(
                    "builtins.__import__",
                    side_effect=_import_failing_tavily,
                ):
            # When: any scan completes
            self.runner._on_completed("tavily", "rid-3")
        # Then: no exception propagates; the gpt-load push is unaffected
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "tavily", "rid-3"
        )


if __name__ == "__main__":
    unittest.main()
