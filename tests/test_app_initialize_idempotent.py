#!/usr/bin/env python3

"""HarvesterApp.initialize() must be idempotent.

Oracle-audit finding (2026-09-23): web/runner.py calls ``app.initialize()``
and then ``app.run()``, which calls ``initialize()`` again. The old
implementation unconditionally re-loaded the config, re-ran
``init_managers()``, rebuilt the shared GitHub client/limiter
(``init_github_client`` — resetting adaptive backoff) and constructed a NEW
``TaskManager`` (→ new ``Pipeline`` → new ``MultiResultManager`` → a second
pair of sqlite connections), silently DISCARDING the TaskManager the runner
had registered its completion listener on.

These tests pin the contract: the second call returns True, keeps the very
same ``TaskManager`` / ``MultiResultManager`` objects, and produces no
duplicate global side effects. A failed initialization must NOT latch the
guard — a retry with a valid config still initializes.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from main import HarvesterApp
from search import client as search_client

# Placeholder query only — no test in this file performs network I/O.
_TASK = {
    "name": "idemtest",
    "enabled": True,
    "provider_type": "openai",
    "use_api": True,
    "stages": {"search": True, "gather": True, "check": True, "inspect": True},
    "patterns": {"key_pattern": "sk-idem-test-[a-zA-Z0-9]{10}"},
    "conditions": [{"query": '"SKIDEMTESTPLACEHOLDER"'}],
}


def _write_config(tmpdir: str, *, task_enabled: bool = True) -> Path:
    """Write a minimal single-task YAML config rooted at a temp workspace."""
    task = dict(_TASK, enabled=task_enabled)
    cfg = Path(tmpdir) / "config-idempotent.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "global": {
                    "workspace": str(tmpdir),
                    "proxy": "",
                    # Placeholder only — the config validator requires at
                    # least one credential; no test here performs network I/O.
                    "github_credentials": {"sessions": ["test_session_placeholder"], "strategy": "round_robin"},
                },
                "tasks": [task],
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _offline_stack(stack: ExitStack, gh_client: MagicMock, init_managers: MagicMock) -> None:
    """Patch the global/network seams touched by initialize(); no real I/O."""
    stack.enter_context(patch("main.init_managers", init_managers))
    stack.enter_context(patch.object(search_client, "set_proxy"))
    stack.enter_context(patch.object(search_client, "configure_github_transport"))
    stack.enter_context(patch.object(search_client, "init_github_client", gh_client))


class TestInitializeIdempotent(unittest.TestCase):
    """Given an initialized HarvesterApp,
    When initialize() is called a second time (the runner → run() path),
    Then it is a truthy no-op: same TaskManager, same MultiResultManager,
    and no repeated global side effects.
    """

    def test_second_call_is_noop_and_returns_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = HarvesterApp(str(_write_config(tmpdir)))
            init_managers = MagicMock()
            gh_client = MagicMock()

            with ExitStack() as stack:
                _offline_stack(stack, gh_client, init_managers)

                self.assertTrue(app.initialize())
                first_task_manager = app.task_manager
                assert first_task_manager is not None
                first_pipeline = first_task_manager.pipeline
                assert first_pipeline is not None
                first_result_manager = first_pipeline.result_manager

                # The runner already called initialize(); run() calls it again.
                self.assertTrue(app.initialize())

                self.assertIs(app.task_manager, first_task_manager)
                # Type narrowing for the member access below; the identities
                # above are the actual assertions.
                assert app.task_manager is not None and app.task_manager.pipeline is not None
                self.assertIs(app.task_manager.pipeline, first_pipeline)
                self.assertIs(app.task_manager.pipeline.result_manager, first_result_manager)

            # Global side effects ran exactly once across both calls.
            self.assertEqual(init_managers.call_count, 1)
            self.assertEqual(gh_client.call_count, 1)

    def test_fresh_app_still_initializes(self) -> None:
        """The guard is per-instance: a NEW app object initializes normally
        (web/runner.py builds one HarvesterApp per scan)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = HarvesterApp(str(_write_config(tmpdir)))
            init_managers = MagicMock()
            gh_client = MagicMock()

            with ExitStack() as stack:
                _offline_stack(stack, gh_client, init_managers)
                self.assertTrue(app.initialize())

            self.assertIsNotNone(app.task_manager)
            self.assertEqual(init_managers.call_count, 1)
            self.assertEqual(gh_client.call_count, 1)

    def test_failed_initialize_does_not_latch_and_retry_works(self) -> None:
        """Given a config that fails to load,
        When initialize() fails and is retried with a valid config,
        Then the retry performs a REAL initialization (the guard only latches
        on success)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            app = HarvesterApp(str(Path(tmpdir) / "missing.yaml"))
            init_managers = MagicMock()
            gh_client = MagicMock()

            with ExitStack() as stack:
                _offline_stack(stack, gh_client, init_managers)

                self.assertFalse(app.initialize())
                self.assertEqual(init_managers.call_count, 0)

                app.config_path = str(_write_config(tmpdir))
                self.assertTrue(app.initialize())
                self.assertIsNotNone(app.task_manager)
                self.assertEqual(init_managers.call_count, 1)


if __name__ == "__main__":
    unittest.main()
