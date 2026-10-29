"""Behavioral tests for the CommandCode key patterns.

The key shape is undetermined (wave-0 2026-10-29: official docs contain zero
``sk-`` hits; six GitHub code-search queries x 10 samples carried no real key
literal), so extraction is env-anchored (``CMD_API_KEY`` /
``COMMANDCODE_API_KEY``) with a wide body class, and domain-anchored
conditions (``api.commandcode.ai``) override with a Bearer/quoted variadic.

Invariants:
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the task-level pattern only fires on env-name assignments, not bare prose;
  - the wide override captures Bearer and quoted tokens but never the
    ``Authorization`` header name itself (13 chars < the 20-char floor);
  - obvious placeholders (``your_...``) never match;
  - the config/defaults.py commandcode preset stays in lockstep with
    examples/config-commandcode.yaml (task pattern + wide override).

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-commandcode.yaml"
_PRESET = "commandcode"


def _real_key() -> str:
    # Shape undetermined; tests use a 32-char mixed body (no prefix).
    # Adjacent literals keep any single source token from being a
    # contiguous secret-shaped string.
    return "aB3d" "E5fG" "7hI9" "jK1l" "M3nO" "5pQ7" "rS9t" "U1vW"


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("commandcode preset missing from config.defaults")


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


class TestCommandCodeKeyPattern(unittest.TestCase):
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
            self.assertEqual(rx.findall("CMD_API_KEY=" + key), [key], name)
            self.assertEqual(rx.findall('commandcode_api_key="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("CommandCode-Api-Key: " + key), [key], name)
            # Bare prose without an assignment must not match.
            self.assertIsNone(rx.search("commandcode api key"), name)

    def test_task_pattern_rejects_placeholders_and_short(self) -> None:
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            self.assertEqual(rx.findall('CMD_API_KEY=your_commandcode_key_here'), [], name)
            self.assertEqual(rx.findall("CMD_API_KEY=short123"), [], name)

    def test_wide_pattern_matches_bearer_and_quoted(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Authorization: Bearer " + key), [key], name)
            self.assertEqual(rx.findall('"' + key + '"'), [key], name)

    def test_wide_pattern_rejects_header_name_and_short(self) -> None:
        # "Authorization" is 13 chars — under the 20-char floor, so the header
        # name can never self-match; short word-like prose is excluded too.
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Bearer Authorization"), [], name)
            self.assertEqual(rx.findall('"commandcode"'), [], name)

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
            "CMD_API_KEY=" + key,
            'COMMANDCODE_API_KEY="' + key + '"',
            "Authorization: Bearer " + key,
            '"' + key + '"',
            'CMD_API_KEY=your_commandcode_key_here',
            "Bearer short123",
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
