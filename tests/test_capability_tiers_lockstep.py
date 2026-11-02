#!/usr/bin/env python3

"""Capability-tier config lockstep across defaults, examples and providers.

The tier lists (``{name, probe_model}``) are declared in five places and every
one of them decides which per-tier gpt-load group a key reaches, so they must
not drift apart:

  - ``config/defaults.py`` doubao / qwen presets
  - ``examples/config-doubao.yaml``
  - ``examples/config-qwen.yaml``
  - ``examples/config-qwen-intl.yaml`` (mirrors qwen-cn with the intl base_url)

The ``thirdparty`` probe ids are live ids verified against the upstream model
lists on 2026-11-02: Ark's ``deepseek-v4-1-flash-261017`` and Bailian's
``deepseek-v4.1-flash-1017``.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

ROOT = Path(__file__).resolve().parent.parent

_DOUBAO_TIERS = [
    {"name": "seed", "probe_model": "doubao-seed-2-1-pro-260915"},
    {"name": "thirdparty", "probe_model": "deepseek-v4-1-flash-261017"},
]

_QWEN_TIERS = [
    {"name": "flagship", "probe_model": "qwen3.8-max-0902"},
    {"name": "legacy", "probe_model": "qwen-turbo"},
    {"name": "thirdparty", "probe_model": "deepseek-v4.1-flash-1017"},
]


def _preset(name: str) -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == name:
            return task
    raise AssertionError(f"{name} preset missing from config.defaults")


def _example(filename: str) -> dict:
    raw = yaml.safe_load((ROOT / "examples" / filename).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _by_name(tiers: list[dict]) -> dict:
    return {entry["name"]: entry for entry in tiers}


class TestCapabilityTiersLockstep(unittest.TestCase):
    def test_doubao_tiers_declared_in_lockstep(self):
        self.assertEqual(_preset("doubao")["capability_tiers"], _DOUBAO_TIERS)
        self.assertEqual(_example("config-doubao.yaml")["capability_tiers"], _DOUBAO_TIERS)

    def test_qwen_tiers_declared_in_lockstep(self):
        self.assertEqual(_preset("qwen")["capability_tiers"], _QWEN_TIERS)
        self.assertEqual(_example("config-qwen.yaml")["capability_tiers"], _QWEN_TIERS)
        self.assertEqual(_example("config-qwen-intl.yaml")["capability_tiers"], _QWEN_TIERS)

    def test_thirdparty_probe_ids_pinned(self):
        doubao = _by_name(_preset("doubao")["capability_tiers"])
        self.assertEqual(doubao["seed"]["probe_model"], "doubao-seed-2-1-pro-260915")
        self.assertEqual(doubao["thirdparty"]["probe_model"], "deepseek-v4-1-flash-261017")

        qwen = _by_name(_preset("qwen")["capability_tiers"])
        self.assertEqual(qwen["thirdparty"]["probe_model"], "deepseek-v4.1-flash-1017")

    def test_provider_exposes_declared_tiers(self):
        from provider.doubao import DoubaoProvider
        from provider.qwen import QwenProvider

        doubao = DoubaoProvider(conditions=[], capability_tiers=_DOUBAO_TIERS)
        self.assertEqual(doubao.tier_names, ["seed", "thirdparty"])

        qwen = QwenProvider(conditions=[], capability_tiers=_QWEN_TIERS)
        self.assertEqual(qwen.tier_names, ["flagship", "legacy", "thirdparty"])


if __name__ == "__main__":
    unittest.main()