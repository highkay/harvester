#!/usr/bin/env python3

"""TDD unit tests for web/runner.py — agnes-ai push trigger.

Given a ``PipelineRunner`` created via ``__new__`` (bypassing ``__init__``,
per the note at web/runner.py:73-76),
When ``_on_completed`` is called,
Then a background thread fires ``AgnesAIPushService.push_valid_keys`` only for
``provider_name == "agnes-ai"`` — symmetric to the existing serpapi push block,
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

    Records every construction (including ``daemon``) on a class-level list so
    tests can assert the agnes-ai push fires on a daemon thread, while still
    making the fire-and-forget background threads in ``_on_completed`` execute
    synchronously (mock call assertions become deterministic).
    """

    created: list[_SyncThread] = []

    def __init__(
        self,
        target: object = None,
        args: tuple = (),
        kwargs: dict | None = None,
        daemon: bool = True,
        name: str | None = None,
    ) -> None:
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon_flag = daemon
        _SyncThread.created.append(self)

    def start(self) -> None:
        assert callable(self.target)
        self.target(*self.args, **self.kwargs)


_REAL_IMPORT = builtins.__import__


def _import_failing_agnes(name: str, *args: Any, **kwargs: Any):
    """__import__ replacement that simulates web.agnes_ai_push being missing."""
    if name == "web.agnes_ai_push" or name.startswith("web.agnes_ai_push."):
        raise ImportError("simulated: web.agnes_ai_push not available")
    return _REAL_IMPORT(name, *args, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnCompletedAgnesAiPush(unittest.TestCase):
    """_on_completed must trigger agnes-ai push iff provider_name == 'agnes-ai'."""

    def setUp(self) -> None:
        # Bypass __init__ (no ThreadPoolExecutor / DB / workspace needed).
        self.runner = PipelineRunner.__new__(PipelineRunner)
        self.agnes_service = MagicMock()
        self.serpapi_service = MagicMock()
        self.tavily_service = MagicMock()
        self.bootstrap_service = MagicMock()
        self.gptload_service = MagicMock()
        _SyncThread.created.clear()

    def test_on_completed_agnes_ai_pushes_on_daemon_thread(self) -> None:
        # Given: services patched, threads run synchronously (and are recorded)
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.agnes_ai_push.get_agnes_ai_push_service",
                    return_value=self.agnes_service,
                ), \
                patch(
                    "web.serpapi_push.get_serpapi_push_service",
                    return_value=self.serpapi_service,
                ), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.self_bootstrap_push.get_self_bootstrap_push_service",
                    return_value=self.bootstrap_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: an agnes-ai scan completes
            self.runner._on_completed("agnes-ai", "rid-1")
        # Then: agnes push fires with (provider, run_id) on a daemon thread
        self.agnes_service.push_valid_keys.assert_called_once_with(
            "agnes-ai", "rid-1"
        )
        agnes_thread = next(
            (
                t
                for t in _SyncThread.created
                if t.target is self.agnes_service.push_valid_keys
            ),
            None,
        )
        self.assertIsNotNone(agnes_thread)
        assert agnes_thread is not None
        self.assertTrue(agnes_thread.daemon_flag)
        # Both pushes fire with the same (provider, run_id); the other
        # dedicated push hooks stay silent.
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "agnes-ai", "rid-1"
        )
        self.serpapi_service.push_valid_keys.assert_not_called()
        self.tavily_service.push_valid_keys.assert_not_called()
        self.bootstrap_service.push_valid_keys.assert_not_called()

    def test_on_completed_serpapi_skips_agnes_push(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.agnes_ai_push.get_agnes_ai_push_service",
                    return_value=self.agnes_service,
                ), \
                patch(
                    "web.serpapi_push.get_serpapi_push_service",
                    return_value=self.serpapi_service,
                ), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.self_bootstrap_push.get_self_bootstrap_push_service",
                    return_value=self.bootstrap_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a serpapi scan completes
            self.runner._on_completed("serpapi", "rid-2")
        # Then: agnes push is skipped, serpapi + gpt-load push fire
        self.agnes_service.push_valid_keys.assert_not_called()
        self.serpapi_service.push_valid_keys.assert_called_once_with(
            "serpapi", "rid-2"
        )
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "serpapi", "rid-2"
        )
        self.tavily_service.push_valid_keys.assert_not_called()
        self.bootstrap_service.push_valid_keys.assert_not_called()

    def test_on_completed_agnes_ai_import_error_does_not_raise(self) -> None:
        # Given: web.agnes_ai_push import raises ImportError, threads run inline
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ), \
                patch(
                    "builtins.__import__",
                    side_effect=_import_failing_agnes,
                ):
            # When: any scan completes
            self.runner._on_completed("agnes-ai", "rid-3")
        # Then: no exception propagates; the gpt-load push is unaffected
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "agnes-ai", "rid-3"
        )


if __name__ == "__main__":
    unittest.main()