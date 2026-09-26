#!/usr/bin/env python3

"""GLM probe-model policy: glm-4.5-flash pinned across defaults/examples/provider.

Reverses the 2026-09-21 "glm-5.3-flash only" policy. Measured on prod
2026-09-24 (bucket re-probes of the no-quota files): every sampled no-quota
key answered 200 for glm-4.5-flash while glm-5.3-flash answered 429 code 1113
("余额不足或无可用资源包"). Zhipu's pricing page confirms glm-5.3-flash is a
PAID model (¥0.8/¥2.8 per M tokens); the free flash tier is glm-4.5-flash /
glm-4.7-flash. Leaked keys are overwhelmingly free-tier, so probing 5.3-flash
classified every live key as no-quota and the provider yielded 0 valid forever.
The pool relaxed to glm-4.5-flash, so the probe model follows — this test pins
the four places the model id lives so they cannot drift apart again.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_MODEL = "glm-4.5-flash"
EXAMPLES = (
    "examples/config-glm.yaml",
    "examples/config-glm-ai.yaml",
)


def _glm_preset() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("provider_type") == "glm":
            return task
    raise AssertionError("glm preset missing from config.defaults")


class TestGLMProbeModelLockstep(unittest.TestCase):
    def test_provider_class_default(self):
        from provider.glm import GLMProvider

        provider = GLMProvider(conditions=[])
        self.assertEqual(provider._default_model, EXPECTED_MODEL)

    def test_defaults_preset(self):
        self.assertEqual(_glm_preset()["api"]["default_model"], EXPECTED_MODEL)

    def test_examples(self):
        for rel in EXAMPLES:
            with self.subTest(example=rel):
                cfg = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
                self.assertEqual(
                    cfg["tasks"][0]["api"]["default_model"],
                    EXPECTED_MODEL,
                    f"{rel}: default_model drifted from the pool policy",
                )

    def test_config_full_template(self):
        cfg = yaml.safe_load((ROOT / "examples/config-full.yaml").read_text(encoding="utf-8"))
        glm_tasks = [t for t in cfg["tasks"] if t.get("provider_type") == "glm"]
        self.assertTrue(glm_tasks, "config-full.yaml lost its glm task")
        for task in glm_tasks:
            self.assertEqual(task["api"]["default_model"], EXPECTED_MODEL)


if __name__ == "__main__":
    unittest.main()
