#!/usr/bin/env python3

"""Periodic snapshots must actually start.

Oracle-audit finding (2026-09-23): ``Pipeline.__init__`` called
``MultiResultManager.start_periodic_snapshots``, but ``managers`` is populated
LAZILY (on the first add_result/get_manager), so the call iterated an empty
dict, logged "No periodic snapshots started" and no ``snapshot-*`` thread ever
existed — every prod config's ``persistence.snapshot_interval: 300`` was inert
(measured: 0 snapshot threads in production).

Fix pinned here: the snapshot start moved to ``Pipeline._on_start`` and the
per-provider ResultManagers are created there eagerly (the same managers
``backup_all_existing_files``/results would create lazily anyway), so starting
the pipeline spawns a real ``snapshot-<provider>`` thread — and nothing is
started for an empty provider set or in simple mode.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import yaml

from config import load_config
from manager.pipeline import Pipeline
from search import client as search_client

# Same filename mapping shape as storage/persistence.py's own __main__ demo.
_FILENAMES = {
    "valid": "valid-keys.txt",
    "no_quota": "no-quota-keys.txt",
    "wait_check": "wait-check-keys.txt",
    "invalid": "invalid-keys.txt",
    "material": "material.txt",
    "summary": "summary.json",
    "links": "links.txt",
}


class _FakeResult:
    """Result-storage descriptor double (class instance: the persistence
    layer keeps weak references, which SimpleNamespace cannot serve)."""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.filenames = dict(_FILENAMES)


class _FakeProvider:
    """A provider double carrying exactly what ResultManager reads."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.result = _FakeResult(name)


def _fake_provider(name: str) -> _FakeProvider:
    return _FakeProvider(name)


def _build_pipeline(tmpdir: str, providers: Dict[str, Any], persistence: dict | None = None) -> Pipeline:
    """Load a temp-workspace config (all stages disabled — these tests only
    exercise the lifecycle hooks, no workers) and build the Pipeline offline."""
    data: dict = {
        "global": {
            "workspace": str(tmpdir),
            "proxy": "",
            # Placeholder only — the config validator requires at least one
            # credential; no test here performs network I/O.
            "github_credentials": {"sessions": ["test_session_placeholder"], "strategy": "round_robin"},
        },
        "tasks": [
            {
                "name": "snapprov",
                "enabled": True,
                "provider_type": "openai",
                "use_api": True,
                "stages": {"search": False, "gather": False, "check": False, "inspect": False},
                "patterns": {"key_pattern": "sk-snap-test-[a-zA-Z0-9]{10}"},
                "conditions": [{"query": '"SKSNAPTESTPLACEHOLDER"'}],
            }
        ],
    }
    if persistence is not None:
        data["persistence"] = persistence

    cfg_path = Path(tmpdir) / "config-snap.yaml"
    cfg_path.write_text(yaml.dump(data), encoding="utf-8")
    config = load_config(str(cfg_path))

    with ExitStack() as stack:
        stack.enter_context(patch.object(search_client, "set_proxy"))
        stack.enter_context(patch.object(search_client, "configure_github_transport"))
        stack.enter_context(patch.object(search_client, "init_github_client"))
        return Pipeline(config, providers)


def _snapshot_thread_names() -> set:
    return {t.name for t in threading.enumerate() if t.name.startswith("snapshot-")}


class TestPeriodicSnapshotsStart(unittest.TestCase):
    """Given a pipeline with known providers,
    When the pipeline starts,
    Then each provider's ResultManager exists and its snapshot thread runs."""

    def setUp(self) -> None:
        self._pipelines: list[Pipeline] = []

    def tearDown(self) -> None:
        # _on_stop early-returns for stage-less pipelines, so the eagerly
        # created ResultManagers (flush + snapshot threads) are stopped here.
        for pipeline in self._pipelines:
            pipeline.result_manager.stop_all()

    def _track(self, pipeline: Pipeline) -> Pipeline:
        self._pipelines.append(pipeline)
        return pipeline

    def test_start_spawns_snapshot_thread_for_each_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = self._track(_build_pipeline(tmpdir, {"snapprov": _fake_provider("snapprov")}))

            # Before start: managers stay lazily empty (API unchanged for
            # other callers) and no snapshot thread exists.
            self.assertEqual(pipeline.result_manager.managers, {})
            self.assertNotIn("snapshot-snapprov", _snapshot_thread_names())

            pipeline.start()

            self.assertIn("snapprov", pipeline.result_manager.managers)
            names = _snapshot_thread_names()
            self.assertIn("snapshot-snapprov", names)
            thread = next(t for t in threading.enumerate() if t.name == "snapshot-snapprov")
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon)

    def test_no_providers_starts_nothing(self) -> None:
        """An empty provider set produces no results → no managers, no
        snapshot threads (and start() must not raise)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = self._track(_build_pipeline(tmpdir, {}))
            pipeline.start()
            self.assertEqual(pipeline.result_manager.managers, {})
            self.assertEqual(_snapshot_thread_names(), set())

    def test_simple_mode_skips_snapshots(self) -> None:
        """persistence.format=txt → simple mode → snapshots stay off."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = self._track(
                _build_pipeline(
                    tmpdir,
                    {"snapprov": _fake_provider("snapprov")},
                    persistence={"format": "txt"},
                )
            )
            self.assertTrue(pipeline.config.persistence.simple)
            pipeline.start()
            self.assertNotIn("snapshot-snapprov", _snapshot_thread_names())


if __name__ == "__main__":
    unittest.main()
