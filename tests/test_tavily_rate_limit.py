#!/usr/bin/env python3

"""Tavily bulk-validation rate policy: hardened pace + defaults/examples lockstep.

Measured 2026-09-23 (prod): tavily blocks an egress by CUMULATIVE DISTINCT-KEY
VOLUME, not by inter-request delay. TavilyProxyManager's own 1276-key ``/usage``
sweep failed 1249/1251 with ``429 {"detail":{"error":"Your request has been
blocked due to excessive requests"}}`` at both 0 s and 6 s pacing, while a 25-key
sample at 6 s/key through a WARP exit passed 56% (8% exhausted, 0 dead). The
validated safe pace for bulk work is therefore >=5-6 s/key — the 0.2 req/s the
hardened providers (kimi / mimo / qwen / glm) already use — not the 1.0-2.0 req/s
tavily shipped with, which holds a single exit at 1-2 req/s for hours and
manufactures the 429 -> RATE_LIMITED -> false-wait storm.

Lockstep is pinned here for the defaults preset and every tavily example config;
the companion behaviour test (retryable verdicts must reach the limiter's
backoff path) lives in tests/test_check_stage_verdicts.py.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_RATE = 0.2
EXPECTED_BURST = 2
EXAMPLES = (
    "examples/config-tavily.yaml",
    "examples/config-tavily-validate-existing.yaml",
    "examples/config-tavily-transport-smoke.yaml",
)


def _tavily_preset() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("provider_type") == "tavily":
            return task
    raise AssertionError("tavily preset missing from config.defaults")


class TestTavilyRateLockstep(unittest.TestCase):
    def test_defaults_preset_ships_the_hardened_pace(self):
        limit = _tavily_preset()["rate_limit"]
        self.assertEqual((limit["base_rate"], limit["burst_limit"]), (EXPECTED_RATE, EXPECTED_BURST))
        self.assertTrue(limit["adaptive"])

    def test_every_example_ships_the_same_pace(self):
        for rel in EXAMPLES:
            with self.subTest(example=rel):
                cfg = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
                global_limit = cfg["ratelimits"]["tavily"]
                self.assertEqual(
                    (global_limit["base_rate"], global_limit["burst_limit"]),
                    (EXPECTED_RATE, EXPECTED_BURST),
                    f"{rel}: ratelimits.tavily drifted from the measured safe pace",
                )
                task_limit = cfg["tasks"][0]["rate_limit"]
                self.assertEqual(
                    (task_limit["base_rate"], task_limit["burst_limit"]),
                    (EXPECTED_RATE, EXPECTED_BURST),
                    f"{rel}: task rate_limit drifts from ratelimits.tavily",
                )

    def test_examples_match_the_defaults_preset(self):
        preset = _tavily_preset()["rate_limit"]
        for rel in EXAMPLES:
            with self.subTest(example=rel):
                cfg = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
                self.assertEqual(cfg["tasks"][0]["rate_limit"], preset, f"{rel} vs config/defaults.py")


if __name__ == "__main__":
    unittest.main()