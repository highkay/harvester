#!/usr/bin/env python3

"""TDD unit tests for web/runner.py — github self-bootstrap push trigger
(plan T5: github-self-bootstrap).

Given a ``PipelineRunner`` created via ``__new__`` (bypassing ``__init__``,
per the note at web/runner.py:73-76),
When ``_on_completed`` is called,
Then a background thread fires ``SelfBootstrapPushService.push_valid_keys``
only for ``provider_name == "github"`` — symmetric to the existing tavily
push block, which must keep firing for ``provider_name == "tavily"``, and the
gpt-load push block, which must keep firing for every provider.
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


def _import_failing_bootstrap(
    name: str,
    globals: dict | None = None,
    locals: dict | None = None,
    fromlist: tuple = (),
    level: int = 0,
):
    """__import__ replacement simulating web.self_bootstrap_push being missing."""
    if name == "web.self_bootstrap_push" or name.startswith(
        "web.self_bootstrap_push."
    ):
        raise ImportError("simulated: web.self_bootstrap_push not available")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOnCompletedSelfBootstrapPush(unittest.TestCase):
    """_on_completed must trigger self-bootstrap push iff provider == 'github'."""

    def setUp(self) -> None:
        # Bypass __init__ (no ThreadPoolExecutor / DB / workspace needed).
        self.runner = PipelineRunner.__new__(PipelineRunner)
        self.bootstrap_service = MagicMock()
        self.tavily_service = MagicMock()
        self.gptload_service = MagicMock()

    def test_on_completed_github_pushes_bootstrap_and_gptload(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.self_bootstrap_push.get_self_bootstrap_push_service",
                    return_value=self.bootstrap_service,
                ), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a github scan completes
            self.runner._on_completed("github", "rid-1")
        # Then: self-bootstrap + gpt-load push fire with the same
        # (provider, run_id); tavily push is skipped (github != tavily)
        self.bootstrap_service.push_valid_keys.assert_called_once_with(
            "github", "rid-1"
        )
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "github", "rid-1"
        )
        self.tavily_service.push_valid_keys.assert_not_called()

    def test_on_completed_deepseek_skips_bootstrap_push(self) -> None:
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.self_bootstrap_push.get_self_bootstrap_push_service",
                    return_value=self.bootstrap_service,
                ), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a non-github scan completes
            self.runner._on_completed("deepseek", "rid-2")
        # Then: bootstrap push is skipped, gpt-load push still fires
        self.bootstrap_service.push_valid_keys.assert_not_called()
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "deepseek", "rid-2"
        )

    def test_on_completed_tavily_still_pushes_tavily(self) -> None:
        """Regression: the tavily push block must keep firing for tavily scans."""
        # Given: services patched, threads run synchronously
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.self_bootstrap_push.get_self_bootstrap_push_service",
                    return_value=self.bootstrap_service,
                ), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ):
            # When: a tavily scan completes
            self.runner._on_completed("tavily", "rid-3")
        # Then: tavily + gpt-load push fire; self-bootstrap does not
        self.tavily_service.push_valid_keys.assert_called_once_with(
            "tavily", "rid-3"
        )
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "tavily", "rid-3"
        )
        self.bootstrap_service.push_valid_keys.assert_not_called()

    def test_on_completed_bootstrap_import_error_does_not_raise(self) -> None:
        # Given: web.self_bootstrap_push import raises ImportError,
        # threads run inline
        with patch("web.runner.threading.Thread", new=_SyncThread), \
                patch(
                    "web.tavily_push.get_tavily_push_service",
                    return_value=self.tavily_service,
                ), \
                patch(
                    "web.push.get_push_service",
                    return_value=self.gptload_service,
                ), \
                patch(
                    "builtins.__import__",
                    side_effect=_import_failing_bootstrap,
                ):
            # When: a github scan completes
            self.runner._on_completed("github", "rid-4")
        # Then: no exception propagates; the gpt-load push is unaffected
        self.gptload_service.push_valid_keys.assert_called_once_with(
            "github", "rid-4"
        )


if __name__ == "__main__":
    unittest.main()
