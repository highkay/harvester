#!/usr/bin/env python3

"""Exa scan rate policy: hardened pace + defaults/examples lockstep.

Exa ships no measured block trip point yet, so it starts at the hardened
provider pace (0.2 req/s = 1 per 5 s) shared by tavily/kimi/mimo/qwen/glm and
brave — cheap insurance against manufacturing a 429 storm, and the same value
the plan pins. Lockstep is pinned here for the defaults preset and the exa
example config; the test reads the PARSED yaml values, not the file text, so
a silently dropped rate key fails loudly.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_RATE = 0.2
EXPECTED_BURST = 2
EXAMPLE = "examples/config-exa.yaml"


def _exa_preset() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("provider_type") == "exa":
            return task
    raise AssertionError("exa preset missing from config.defaults")


class TestExaRateLockstep(unittest.TestCase):
    def test_defaults_preset_ships_the_hardened_pace(self):
        limit = _exa_preset()["rate_limit"]
        self.assertEqual((limit["base_rate"], limit["burst_limit"]), (EXPECTED_RATE, EXPECTED_BURST))
        self.assertTrue(limit["adaptive"])

    def test_example_ships_the_same_pace(self):
        cfg = yaml.safe_load((ROOT / EXAMPLE).read_text(encoding="utf-8"))
        global_limit = cfg["ratelimits"]["exa"]
        self.assertEqual(
            (global_limit["base_rate"], global_limit["burst_limit"]),
            (EXPECTED_RATE, EXPECTED_BURST),
            f"{EXAMPLE}: ratelimits.exa drifted from the hardened pace",
        )
        task_limit = cfg["tasks"][0]["rate_limit"]
        self.assertEqual(
            (task_limit["base_rate"], task_limit["burst_limit"]),
            (EXPECTED_RATE, EXPECTED_BURST),
            f"{EXAMPLE}: task rate_limit drifts from ratelimits.exa",
        )

    def test_example_matches_the_defaults_preset(self):
        preset = _exa_preset()["rate_limit"]
        cfg = yaml.safe_load((ROOT / EXAMPLE).read_text(encoding="utf-8"))
        self.assertEqual(cfg["tasks"][0]["rate_limit"], preset, f"{EXAMPLE} vs config/defaults.py")


if __name__ == "__main__":
    unittest.main()