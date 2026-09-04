#!/usr/bin/env python3

"""Config loading tests for the agnes-ai preset (``examples/config-agnes-ai.yaml``).

Guards the preset contract: it must load through the real config loader,
define exactly one ``agnes-ai`` task, carry an env-anchored ``sk-`` key
pattern that compile-matches Agnes API keys while rejecting Anthropic/OpenAI
project/service-account keys, widen to ``Bearer``/quoted extraction on the
domain-anchored conditions, and provide non-empty search queries.

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
    source = _EXAMPLES_DIR / "config-agnes-ai.yaml"
    if not source.exists():
        raise FileNotFoundError(f"agnes-ai preset missing: {source}")

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


class TestConfigAgnesAIPresetLoads(unittest.TestCase):
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

    def test_exactly_one_agnes_ai_task(self) -> None:
        tasks = self.config.tasks
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].name, "agnes-ai")
        self.assertEqual(tasks[0].provider_type, "agnes-ai")

    def test_key_pattern_matches_agnes_ai_keys(self) -> None:
        pattern = self.config.tasks[0].patterns.key_pattern
        self.assertTrue(pattern, "key_pattern must not be empty")
        rx = re.compile(pattern)
        key = "sk-" + ("A9gx" * 6)  # sk- + 24 alnum chars
        self.assertTrue(rx.search(f"AGNES_API_KEY={key}"), "must match env assignment")
        anthropic = "sk-ant-api03-" + ("A9gx" * 6)
        self.assertIsNone(
            rx.search(f"AGNES_API_KEY={anthropic}"),
            "must not match sk-ant- anthropic keys",
        )

    def test_domain_condition_matches_bearer_keys(self) -> None:
        conditions = self.config.tasks[0].conditions
        domain = [
            condition
            for condition in conditions
            if condition.query == '"apihub.agnes-ai.com"'
        ]
        self.assertTrue(domain, "preset needs the domain-anchored condition")
        pattern = domain[0].patterns.key_pattern
        self.assertTrue(pattern, "domain condition key_pattern must not be empty")
        rx = re.compile(pattern)
        key = "sk-" + ("A9gx" * 6)
        self.assertTrue(rx.search(f"Bearer {key}"), "must match Bearer sk- keys")
        self.assertIsNone(
            rx.search(f"Bearer sk-proj-{('A9gx' * 6)[4:]}"),
            "must not match sk-proj- project keys",
        )

    def test_conditions_non_empty_queries(self) -> None:
        conditions = self.config.tasks[0].conditions
        self.assertTrue(conditions, "agnes-ai preset needs at least one query")
        for condition in conditions:
            self.assertTrue(condition.query and condition.query.strip())


if __name__ == "__main__":
    unittest.main()