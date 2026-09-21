#!/usr/bin/env python3

"""Config loading tests for the grok preset (``examples/config-grok.yaml``).

Guards the preset contract: it must load through the real config loader,
define exactly one ``grok`` task, carry an ``xai-`` key pattern that
compile-matches real xAI API keys, and provide non-empty search queries.

Placeholder GitHub credentials are replaced with a dummy valid-shaped token
before loading — the same injection ``web/runner.py`` performs for every
scheduled scan (the validator rejects placeholder creds, as it does for the
other committed presets).
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import yaml

from config import load_config

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
_DUMMY_TOKEN = "ghp_testHarvesterToken0123456789abcdefgh"  # valid-shape placeholder


def _rendered_path() -> str:
    """Write a temp copy of the preset with dummy credentials injected."""
    source = _EXAMPLES_DIR / "config-grok.yaml"
    if not source.exists():
        raise FileNotFoundError(f"grok preset missing: {source}")

    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw.setdefault("global", {})["github_credentials"] = {
        "sessions": [],
        "tokens": [_DUMMY_TOKEN],
        "strategy": "round_robin",
    }
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", encoding="utf-8", delete=False
    )
    with handle:
        yaml.dump(raw, handle, default_flow_style=False, allow_unicode=True)
    return handle.name


class TestConfigGrokPresetLoads(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._rendered = _rendered_path()
        cls.config = load_config(cls._rendered)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            Path(cls._rendered).unlink()
        except OSError:
            pass

    def test_exactly_one_grok_task(self) -> None:
        tasks = self.config.tasks
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].name, "grok")
        self.assertEqual(tasks[0].provider_type, "grok")

    def test_key_pattern_matches_xai_api_keys(self) -> None:
        pattern = self.config.tasks[0].patterns.key_pattern
        self.assertTrue(pattern, "key_pattern must not be empty")
        rx = re.compile(pattern)
        self.assertTrue(rx.search("xai-" + ("A9z" * 16)), "must match xai- keys")
        self.assertIsNone(rx.search("sk-" + ("A9z" * 16)), "must not match sk- keys")

    def test_conditions_non_empty_queries(self) -> None:
        conditions = self.config.tasks[0].conditions
        self.assertTrue(conditions, "grok preset needs at least one query")
        for condition in conditions:
            self.assertTrue(condition.query and condition.query.strip())


if __name__ == "__main__":
    unittest.main()