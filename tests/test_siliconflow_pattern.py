"""Behavioral tests for the SiliconFlow key patterns.

Pins the 2026-10-29 measured format: ``sk-`` + 48 lowercase alnum chars,
observed across the ``SILICONFLOW_API_KEY`` / ``api.siliconflow.cn`` and
``"siliconflow" "sk-"`` corpus fragments (11 of 12 non-placeholder hits were 48
lowercase letters, 1 carried digits; a 20-char dummy and hyphenated
placeholders were rejected). Extraction is env-anchored at the task level, with
domain dorks overriding to a Bearer/quoted variadic.

Invariants:
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the task-level pattern only fires on ``siliconflow*api*key`` assignments,
    not on bare prose;
  - the wide override matches Bearer / quoted / bare forms;
  - placeholder names (``sk-your...`` / ``sk-test...`` / ``sk-dummy...``) and
    short bodies (< the 32-char floor) never match;
  - the body has NO upper bound (serpapi lesson: a fixed width silently drops
    keys when the provider rotates formats);
  - config/defaults.py's siliconflow preset stays in lockstep with
    examples/config-siliconflow.yaml (task pattern + wide override).

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-siliconflow.yaml"
_PRESET = "siliconflow"

# Measured shape: sk- + 48 lowercase letters, built in 4-char chunks.
_BODY_48 = "".join(  # noqa: FLY002 - chunks keep source lines non-secret-shaped
    ("qkvt", "mzpr", "lxwb", "nwzc", "hvjd", "qslr", "cfgy", "nbak", "thkr", "wprs", "zmdx", "jxvq")
)
_BODY_72 = _BODY_48 + "".join(("lqnm", "btzw", "wkmx", "pxsr", "dvqc", "hgcn"))  # noqa: FLY002


def _real_key() -> str:
    return "sk-" + _BODY_48


def _long_key() -> str:
    return "sk-" + _BODY_72


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("siliconflow preset missing from config.defaults")


def _canonical(pattern: str) -> str:
    """Strip regex-redundant escapes before quotes (sources differ only in that)."""
    return re.sub(r"\\(['\"])", r"\1", pattern)


def _wide_pattern(task: dict) -> str:
    """The single domain-anchored override pattern for the task."""
    overrides = {
        str(condition["query"]): str(condition["patterns"]["key_pattern"])
        for condition in task["conditions"]
        if "patterns" in condition
    }
    assert len(set(overrides.values())) == 1, overrides
    return next(iter(set(overrides.values())))


class TestSiliconFlowKeyPattern(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = {"example": _example_task(), "preset": _preset_task()}
        self.task_patterns = {
            name: str(task["patterns"]["key_pattern"]) for name, task in self.tasks.items()
        }
        self.wide_patterns = {name: _wide_pattern(task) for name, task in self.tasks.items()}
        self.wide_regexes = {name: re.compile(p) for name, p in self.wide_patterns.items()}

    def test_single_capture_group(self) -> None:
        for name in self.tasks:
            self.assertEqual(re.compile(self.task_patterns[name]).groups, 1, name)
            self.assertEqual(self.wide_regexes[name].groups, 1, name)

    def test_task_pattern_env_anchored(self) -> None:
        key = _real_key()
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            self.assertEqual(rx.findall("SILICONFLOW_API_KEY=" + key), [key], name)
            self.assertEqual(rx.findall('SILICONFLOW_API_KEY="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("SILICONFLOW_API_KEY: " + key), [key], name)
            self.assertEqual(rx.findall('"siliconflow_api_key": "' + key + '"'), [key], name)
            self.assertEqual(rx.findall("siliconflow_api_key=" + key), [key], name)
            # Bare prose without an assignment must not match.
            self.assertIsNone(rx.search("siliconflow api key"), name)

    def test_measured_shape_matches(self) -> None:
        # 48 lowercase alnum body (the measured real-key shape).
        key = _real_key()
        self.assertEqual(len(_BODY_48), 48)
        for name, pattern in self.task_patterns.items():
            self.assertEqual(re.compile(pattern).findall("SILICONFLOW_API_KEY=" + key), [key], name)

    def test_task_pattern_rejects_placeholders_and_short(self) -> None:
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            for placeholder in (
                "sk-your-key-here",
                "sk-your_siliconflow_api_key",
                "sk-test0123456789012345678901234567",
                "sk-dummy0123456789012345678901234567",
                "sk-placeholder0123456789012345678901",
                "sk-short0123456789",  # < 32-char floor
            ):
                self.assertEqual(rx.findall("SILICONFLOW_API_KEY=" + placeholder), [], f"{name} {placeholder}")

    def test_wide_pattern_matches_bearer_quoted_and_bare(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Authorization: Bearer " + key), [key], name)
            self.assertEqual(rx.findall('"' + key + '"'), [key], name)
            self.assertEqual(rx.findall(":" + key), [key], name)

    def test_wide_pattern_rejects_header_name_and_placeholders(self) -> None:
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Bearer Authorization"), [], name)
            self.assertEqual(rx.findall('"sk-your-key-here"'), [], name)
            self.assertEqual(rx.findall('"' + "sk-short0123456789" + '"'), [], name)

    def test_no_upper_bound_on_body(self) -> None:
        # A rotated/longer body must be captured in full (serpapi lesson: the
        # 48-char measurement is documented, not enforced).
        key = _long_key()
        for name, pattern in self.task_patterns.items():
            self.assertEqual(re.compile(pattern).findall("SILICONFLOW_API_KEY=" + key), [key], name)
            self.assertEqual(self.wide_regexes[name].findall(key), [key], name)

    def test_defaults_examples_lockstep(self) -> None:
        # The two sources may differ only in redundant-escape style inside raw
        # strings — compare canonical forms, then prove behavioral equivalence.
        self.assertEqual(
            {_canonical(p) for p in self.task_patterns.values()},
            {_canonical(self.task_patterns["preset"])},
            self.task_patterns,
        )
        self.assertEqual(
            {_canonical(p) for p in self.wide_patterns.values()},
            {_canonical(self.wide_patterns["preset"])},
            self.wide_patterns,
        )
        key = _real_key()
        probes = (
            "SILICONFLOW_API_KEY=" + key,
            'siliconflow_api_key="' + key + '"',
            "Authorization: Bearer " + key,
            '"' + key + '"',
            "SILICONFLOW_API_KEY=sk-your-key-here",
            "Bearer sk-short0123456789",
        )
        for name in self.tasks:
            for probe in probes:
                self.assertEqual(
                    re.compile(self.task_patterns[name]).findall(probe),
                    re.compile(self.task_patterns["preset"]).findall(probe),
                    f"{name} task {probe}",
                )
                self.assertEqual(
                    self.wide_regexes[name].findall(probe),
                    self.wide_regexes["preset"].findall(probe),
                    f"{name} wide {probe}",
                )


if __name__ == "__main__":
    unittest.main()