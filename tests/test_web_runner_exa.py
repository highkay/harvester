#!/usr/bin/env python3

"""TDD unit tests for web/runner.py — exa push trigger.

Given a ``PipelineRunner`` created via ``__new__`` (bypassing ``__init__``,
per the note at web/runner.py:73-76),
When ``_on_completed`` is called,
Then a background thread fires ``ExaPushService.push_valid_keys`` only for
``provider_name == "exa"`` — symmetric to the existing tavily / brave push
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


def _import_failing_exa(name: str, *args: Any, **kwargs: Any):
    """__import__ replacement that simulates web.exa_push being missing."""
    if name == "web.exa_push" or name.startswith("web.exa_push."):
        raise ImportError("simulated: web.exa_push not available")
    return _REAL_IMPORT(name, *args, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnCompletedExaPush(unittest.TestCase):
    """_on_completed must trigger exa push iff provider_name == 'exa'."""

    def setUp(self) -> None:
        # Bypass __init__ (no ThreadPoolExecutor / DB / workspace needed).
        self.runner = PipelineRunner.__new__(PipelineRunner)
        self.exa_service = MagicMock()
        self.gptload_service = MagicMock()

    def test_on_completed_exa_pushes_exa_and_gptload(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.exa_push.get_exa_push_service",
                    return_value=self.exa_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: an exa scan completes
            self.runner._on_completed("exa", "rid-1")
        # Then: both pushes fire with the same (provider, run_id)
        self.exa_service.push_valid_keys.assert_called_once_with("exa", "rid-1")
        self.gptload_service.push_valid_keys.assert_called_once_with("exa", "rid-1")

    def test_on_completed_deepseek_skips_exa_push(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.exa_push.get_exa_push_service",
                    return_value=self.exa_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a non-exa scan completes
            self.runner._on_completed("deepseek", "rid-2")
        # Then: exa push is skipped, gpt-load push still fires
        self.exa_service.push_valid_keys.assert_not_called()
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "deepseek", "rid-2"
        )

    def test_on_completed_exa_import_error_does_not_raise(self) -> None:
        # Given: web.exa_push import raises ImportError, threads run inline
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ), \
                patch(
                    "builtins.__import__",
                    side_effect=_import_failing_exa,
                ):
            # When: any scan completes
            self.runner._on_completed("exa", "rid-3")
        # Then: no exception propagates; the gpt-load push is unaffected
        self.gptload_service.push_valid_keys.assert_called_once_with("exa", "rid-3")


if __name__ == "__main__":
    unittest.main()
