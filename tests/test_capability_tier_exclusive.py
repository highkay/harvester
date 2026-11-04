#!/usr/bin/env python3

"""Exclusive capability-tier mode: the first OK tier wins and nothing else.

``fan_out`` (default) admits a key into every OK tier; ``exclusive`` walks the
declared order and stops at the first OK tier so a key lands in exactly one
group. glm uses exclusive for the 5.3/4.7 split.
"""

from __future__ import annotations

import unittest
from unittest import mock

from constant.capability import TIER_NO_QUOTA, TIER_OK, TIER_RETRYABLE
from provider.capability import probe_tiers


class _Stub:
    def __init__(self, tiers, mode):
        self.capability_tiers = tiers
        self.tier_mode = mode


TIERS = [
    {"name": "flash53", "probe_model": "glm-5.3-flash"},
    {"name": "flash47", "probe_model": "glm-4.7-flash"},
]


class TestExclusiveTierMode(unittest.TestCase):
    def test_exclusive_stops_at_first_ok(self):
        calls: list[str] = []

        def fake(provider, token, model):
            calls.append(model)
            return TIER_OK

        with mock.patch("provider.capability.probe_tier", side_effect=fake):
            tiers = probe_tiers(_Stub(TIERS, "exclusive"), "k")
        self.assertEqual(tiers, {"flash53": TIER_OK})
        self.assertEqual(calls, ["glm-5.3-flash"])

    def test_exclusive_falls_back_when_first_tier_fails(self):
        calls: list[str] = []

        def fake(provider, token, model):
            calls.append(model)
            return TIER_NO_QUOTA if model == "glm-5.3-flash" else TIER_OK

        with mock.patch("provider.capability.probe_tier", side_effect=fake):
            tiers = probe_tiers(_Stub(TIERS, "exclusive"), "k")
        self.assertEqual(tiers, {"flash53": TIER_NO_QUOTA, "flash47": TIER_OK})
        self.assertEqual(calls, ["glm-5.3-flash", "glm-4.7-flash"])

    def test_exclusive_records_every_failed_tier(self):
        with mock.patch("provider.capability.probe_tier", return_value=TIER_RETRYABLE):
            tiers = probe_tiers(_Stub(TIERS, "exclusive"), "k")
        self.assertEqual(set(tiers), {"flash53", "flash47"})
        self.assertTrue(all(v == TIER_RETRYABLE for v in tiers.values()))

    def test_fan_out_probes_every_tier(self):
        with mock.patch("provider.capability.probe_tier", return_value=TIER_OK):
            tiers = probe_tiers(_Stub(TIERS, "fan_out"), "k")
        self.assertEqual(set(tiers), {"flash53", "flash47"})


class TestTierModeWiring(unittest.TestCase):
    def test_provider_exposes_mode_and_ignores_unknown(self):
        from provider.glm import GLMProvider

        exclusive = GLMProvider(conditions=[], capability_tiers=TIERS, tier_mode="exclusive")
        self.assertEqual(exclusive.tier_mode, "exclusive")
        fallback = GLMProvider(conditions=[], capability_tiers=TIERS, tier_mode="bogus")
        self.assertEqual(fallback.tier_mode, "fan_out")

    def test_task_config_rejects_unknown_mode(self):
        from config.schemas import TaskConfig

        with self.assertRaises(ValueError):
            TaskConfig(name="glm", tier_mode="both")

    def test_loader_reads_tier_mode(self):
        import yaml

        from config.loader import ConfigLoader

        with open("examples/config-glm.yaml", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        task_data = raw["tasks"][0]
        task = ConfigLoader()._parse_task_config(task_data)
        self.assertEqual(task.tier_mode, "exclusive")


if __name__ == "__main__":
    unittest.main()
