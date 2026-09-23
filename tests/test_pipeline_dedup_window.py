#!/usr/bin/env python3

"""Per-stage dedup window must cover the queue it guards.

Oracle-audit finding (2026-09-23): ``stage/base.py`` defaults
``dedup_max_size=100_000`` and ``manager/pipeline.py::_create_stages`` never
passed it, while prod configs (and the loader defaults) size the queues to
search 100k / gather 200k / check 500k / inspect 1M. Once more than 100k
distinct ids passed a stage, early ids were evicted and the same URL/key could
be re-gathered and re-validated — burning provider rate budget and inflating
duplicate verdict lines (``ResultBuffer.add`` does not dedupe).

Fix pinned here: each stage is created with
``dedup_max_size = max(2 * <its queue size>, 1000)``.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import yaml

from config import load_config
from manager.pipeline import Pipeline
from search import client as search_client

_TASK = {
    "name": "dedupwin",
    "enabled": True,
    "provider_type": "openai",
    "use_api": True,
    "stages": {"search": True, "gather": True, "check": True, "inspect": True},
    "patterns": {"key_pattern": "sk-dedup-test-[a-zA-Z0-9]{10}"},
    "conditions": [{"query": '"SKDEDUPTESTPLACEHOLDER"'}],
}


def _build_pipeline(tmpdir: str, queue_sizes: Optional[dict]) -> Pipeline:
    """Load a temp-workspace config and construct the Pipeline offline."""
    data: dict = {
        "global": {
            "workspace": str(tmpdir),
            "proxy": "",
            # Placeholder only — the config validator requires at least one
            # credential; no test here performs network I/O.
            "github_credentials": {"sessions": ["test_session_placeholder"], "strategy": "round_robin"},
        },
        "tasks": [dict(_TASK)],
    }
    if queue_sizes is not None:
        data["pipeline"] = {"queue_sizes": dict(queue_sizes)}

    cfg_path = Path(tmpdir) / "config-dedup.yaml"
    cfg_path.write_text(yaml.dump(data), encoding="utf-8")
    config = load_config(str(cfg_path))

    with ExitStack() as stack:
        stack.enter_context(patch.object(search_client, "set_proxy"))
        stack.enter_context(patch.object(search_client, "configure_github_transport"))
        stack.enter_context(patch.object(search_client, "init_github_client"))
        return Pipeline(config, {"dedupwin": MagicMock()})


class TestDedupWindowWiring(unittest.TestCase):
    """Given configured queue sizes,
    When _create_stages builds each stage,
    Then its dedup window is exactly 2x the queue it protects."""

    def test_configured_queue_sizes_derive_dedup_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # The config validator requires an entry per stage, so all four
            # are configured — prod shape: search 100k / gather 200k / check 500k.
            pipeline = _build_pipeline(
                tmpdir,
                {"search": 100_000, "gather": 200_000, "check": 500_000, "inspect": 250_000},
            )
            # The window must strictly exceed the queue it guards.
            self.assertEqual(pipeline.stages["search"].dedup_max_size, 200_000)
            self.assertEqual(pipeline.stages["gather"].dedup_max_size, 400_000)
            self.assertEqual(pipeline.stages["check"].dedup_max_size, 1_000_000)
            self.assertEqual(pipeline.stages["inspect"].dedup_max_size, 500_000)

    def test_loader_default_queue_sizes_derive_dedup_window(self) -> None:
        """No pipeline section at all → the loader defaults
        (100k/200k/500k/1M) apply and the window is still 2x each."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = _build_pipeline(tmpdir, None)
            expected = {
                "search": 200_000,
                "gather": 400_000,
                "check": 1_000_000,
                "inspect": 2_000_000,
            }
            for name, dedup in expected.items():
                self.assertEqual(
                    pipeline.stages[name].dedup_max_size,
                    dedup,
                    f"stage {name}: dedup window must be 2x its queue",
                )

    def test_tiny_queue_clamps_to_stage_floor(self) -> None:
        """A sub-floor queue size still yields the stage-base minimum (1000),
        because stage/base.py clamps with max(1000, int(...))."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = _build_pipeline(tmpdir, {"search": 10, "gather": 10, "check": 10, "inspect": 10})
            self.assertEqual(pipeline.stages["search"].dedup_max_size, 1000)


if __name__ == "__main__":
    unittest.main()
