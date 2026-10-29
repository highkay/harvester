#!/usr/bin/env python3

"""Qwen probe-model policy: qwen3.8-max-0902 pinned across defaults/examples/provider.

Measured on prod 2026-10-26: the gpt-load group ``qwensg`` (id 8) validates with
``test_model: qwen3.8-max-0902``, while the harvester scanned with the alias
``qwen3.8-max``. Both ids exist on DashScope, but a key's model whitelist is
per-id, so an alias and its dated snapshot can disagree (403 Model.AccessDenied
on one, 200 on the other). The probe must therefore name the SAME id the pool
validates with, or the two layers classify the same key differently.

This test pins the four places the model id lives so they cannot drift apart.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_MODEL = "qwen3.8-max-0902"
EXAMPLES = (
    "examples/config-qwen.yaml",
    "examples/config-qwen-intl.yaml",
)


def _qwen_preset() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("provider_type") == "qwen":
            return task
    raise AssertionError("qwen preset missing from config.defaults")


class TestQwenProbeModelLockstep(unittest.TestCase):
    def test_provider_class_default(self):
        from provider.qwen import QwenProvider

        provider = QwenProvider(conditions=[])
        self.assertEqual(provider._default_model, EXPECTED_MODEL)

    def test_defaults_preset(self):
        self.assertEqual(_qwen_preset()["api"]["default_model"], EXPECTED_MODEL)

    def test_examples(self):
        for rel in EXAMPLES:
            with self.subTest(example=rel):
                cfg = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
                self.assertEqual(
                    cfg["tasks"][0]["api"]["default_model"],
                    EXPECTED_MODEL,
                    f"{rel}: default_model drifted from the qwensg test_model",
                )

    def test_examples_stay_in_lockstep_with_each_other(self):
        models = set()
        for rel in EXAMPLES:
            cfg = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
            models.add(cfg["tasks"][0]["api"]["default_model"])
        self.assertEqual(len(models), 1, models)


if __name__ == "__main__":
    unittest.main()
