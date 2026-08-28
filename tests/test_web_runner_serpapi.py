#!/usr/bin/env python3

"""TDD unit tests for web/runner.py — serpapi push trigger.

Given a ``PipelineRunner`` created via ``__new__`` (bypassing ``__init__``,
per the note at web/runner.py:73-76),
When ``_on_completed`` is called,
Then a background thread fires ``SerpapiPushService.push_valid_keys`` only for
``provider_name == "serpapi"`` — symmetric to the existing tavily push block,
which must keep firing, as must the gpt-load push block.
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


def _import_failing_serpapi(name: str, *args: Any, **kwargs: Any):
    """__import__ replacement that simulates web.serpapi_push being missing."""
    if name == "web.serpapi_push" or name.startswith("web.serpapi_push."):
        raise ImportError("simulated: web.serpapi_push not available")
    return _REAL_IMPORT(name, *args, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnCompletedSerpapiPush(unittest.TestCase):
    """_on_completed must trigger serpapi push iff provider_name == 'serpapi'."""

    def setUp(self) -> None:
        # Bypass __init__ (no ThreadPoolExecutor / DB / workspace needed).
        self.runner = PipelineRunner.__new__(PipelineRunner)
        self.serpapi_service = MagicMock()
        self.gptload_service = MagicMock()

    def test_on_completed_serpapi_pushes_serpapi_and_gptload(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.serpapi_push.get_serpapi_push_service",
                    return_value=self.serpapi_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a serpapi scan completes
            self.runner._on_completed("serpapi", "rid-1")
        # Then: both pushes fire with the same (provider, run_id)
        self.serpapi_service.push_valid_keys.assert_called_once_with(
            "serpapi", "rid-1"
        )
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "serpapi", "rid-1"
        )

    def test_on_completed_deepseek_skips_serpapi_push(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.serpapi_push.get_serpapi_push_service",
                    return_value=self.serpapi_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a non-serpapi scan completes
            self.runner._on_completed("deepseek", "rid-2")
        # Then: serpapi push is skipped, gpt-load push still fires
        self.serpapi_service.push_valid_keys.assert_not_called()
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "deepseek", "rid-2"
        )

    def test_on_completed_tavily_does_not_trigger_serpapi_push(self) -> None:
        # Given: services patched (incl. tavily), threads run synchronously
        tavily_service = MagicMock()
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.serpapi_push.get_serpapi_push_service",
                    return_value=self.serpapi_service,
                ), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=tavily_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a tavily scan completes
            self.runner._on_completed("tavily", "rid-3")
        # Then: serpapi push is skipped (tavily keeps its own hook)
        self.serpapi_service.push_valid_keys.assert_not_called()
        tavily_service.push_valid_keys.assert_called_once_with("tavily", "rid-3")

    def test_on_completed_serpapi_import_error_does_not_raise(self) -> None:
        # Given: web.serpapi_push import raises ImportError, threads run inline
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ), \
                patch(
                    "builtins.__import__",
                    side_effect=_import_failing_serpapi,
                ):
            # When: any scan completes
            self.runner._on_completed("serpapi", "rid-4")
        # Then: no exception propagates; the gpt-load push is unaffected
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "serpapi", "rid-4"
        )


if __name__ == "__main__":
    unittest.main()