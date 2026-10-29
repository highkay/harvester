"""Behavioral tests for the Doubao (Volcengine Ark) key patterns.

Key shape MEASURED 2026-10-29 (lane sampler over GitHub code search,
text_matches, shapes only): every real literal captured under ARK_API_KEY /
volces Bearer contexts is a UUID v4 — lowercase hex 8-4-4-4-12 (16/16
samples). Extraction is env-anchored (``ARK_API_KEY`` /
``VOLCENGINE_API_KEY``) with a fixed-width UUID body; domain-anchored
conditions (``ark.cn-beijing.volces.com``) override with a Bearer/quoted
variadic.

Invariants:
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the task-level pattern only fires on env-name assignments, not bare prose;
  - the all-zeros placeholder UUID never matches (negative lookahead);
  - word placeholders (``your-ark-key-here``) cannot match the hex charset;
  - ep- deployment ids cannot match ('p' is not a hex digit);
  - the wide override captures Bearer and quoted UUIDs;
  - the config/defaults.py doubao preset stays in lockstep with
    examples/config-doubao.yaml (task pattern + wide override).

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-doubao.yaml"
_PRESET = "doubao"


def _real_key() -> str:
    # Measured shape: UUID v4, lowercase hex 8-4-4-4-12.
    return "7fb7" "a1c2" "-4d3e" "-8f9a" "-bcde" "-1234" "abcd" "ef56"


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("doubao preset missing from config.defaults")


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


class TestDoubaoKeyPattern(unittest.TestCase):
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
            self.assertEqual(rx.findall("ARK_API_KEY=" + key), [key], name)
            self.assertEqual(rx.findall('ark_api_key="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("VOLCENGINE_API_KEY: " + key), [key], name)
            # Bare prose without an assignment must not match.
            self.assertIsNone(rx.search("ark api key " + key), name)

    def test_task_pattern_rejects_placeholders(self) -> None:
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            # All-zeros placeholder UUID is dropped by the lookahead.
            self.assertEqual(
                rx.findall("ARK_API_KEY=00000000-0000-0000-0000-000000000000"), [], name
            )
            # Word placeholders cannot match the hex charset.
            self.assertEqual(rx.findall("ARK_API_KEY=your-ark-key-here"), [], name)
            # Truncated / non-UUID strings do not match.
            self.assertEqual(rx.findall("ARK_API_KEY=deadbeef"), [], name)

    def test_task_pattern_rejects_ep_endpoint_ids(self) -> None:
        # ep- deployment ids are model/endpoint identifiers, not keys; 'p'
        # is not a hex digit so they must never be captured.
        ep = "ep-20240101abcde-fghij"
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            self.assertEqual(rx.findall("ARK_API_KEY=" + ep), [], name)
            self.assertEqual(rx.findall("model: " + ep), [], name)

    def test_wide_pattern_matches_bearer_and_quoted(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Authorization: Bearer " + key), [key], name)
            self.assertEqual(rx.findall('"' + key + '"'), [key], name)

    def test_wide_pattern_rejects_placeholder_uuid(self) -> None:
        for name, rx in self.wide_regexes.items():
            self.assertEqual(
                rx.findall("Bearer 00000000-0000-0000-0000-000000000000"), [], name
            )
            self.assertEqual(rx.findall('"not-a-uuid"'), [], name)

    def test_defaults_examples_lockstep(self) -> None:
        # The two sources carry the same regexes but may differ in
        # redundant-escape style — compare canonical forms, then prove
        # behavioral equivalence on the probe battery.
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
            "ARK_API_KEY=" + key,
            'VOLCENGINE_API_KEY="' + key + '"',
            "Authorization: Bearer " + key,
            '"' + key + '"',
            "ARK_API_KEY=00000000-0000-0000-0000-000000000000",
            "ARK_API_KEY=your-ark-key-here",
            "Bearer deadbeef",
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
