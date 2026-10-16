"""Behavioral tests for the Exa key patterns.

Pins the header-name self-match property for the exa wide override: unlike
brave's ``X-Subscription-Token`` (exactly 20 charset-legal chars, which the
bare-quote branch once captured from curl-style ``-H`` lines and now needs a
negative lookahead), the exa header name ``x-api-key`` is 9 chars — under the
wide override's 20-char capture floor — so it can never self-match. That
property is pinned here numerically in addition to the behavioral battery.

Invariants:
  - the curl -H line above captures the TOKEN, never the header name;
  - the header name is shorter than the wide override's capture floor;
  - Bearer and quoted-token forms still capture the token;
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the config/defaults.py exa preset stays in lockstep with
    examples/config-exa.yaml (task pattern + wide override, parsed values);
  - the task-level pattern only fires on EXA_* env assignments, not empty
    assignments or bare prose.

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-exa.yaml"
_PRESET = "exa"


def _real_key() -> str:
    return "".join(("aB3dE5fG-", "7hI9jK1l-", "M3nO5pQ7-", "rS9tU1vW-", "3xY5zA7b"))


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("exa preset missing from config.defaults")


def _canonical(pattern: str) -> str:
    """Strip regex-redundant escapes before quotes (sources differ only in that)."""
    return re.sub(r"\\(['\"])", r"\1", pattern)


def _wide_pattern(task: dict) -> str:
    """The single domain/header-anchored override pattern for the task."""
    overrides = {
        str(condition["query"]): str(condition["patterns"]["key_pattern"])
        for condition in task["conditions"]
        if "patterns" in condition
    }
    assert len(set(overrides.values())) == 1, overrides
    return next(iter(set(overrides.values())))


class TestExaKeyPattern(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = {"example": _example_task(), "preset": _preset_task()}
        self.task_patterns = {
            name: str(task["patterns"]["key_pattern"]) for name, task in self.tasks.items()
        }
        self.wide_patterns = {name: _wide_pattern(task) for name, task in self.tasks.items()}
        self.wide_regexes = {name: re.compile(p) for name, p in self.wide_patterns.items()}

    def test_header_line_self_match_rejected(self) -> None:
        # The exa analogue of the brave defect: the bare-quote branch must
        # never capture the header name from a quoted curl-style line. The
        # capture must be the token.
        key = _real_key()
        line = '-H "x-api-key: ' + key + '"'
        for name, rx in self.wide_regexes.items():
            captures = rx.findall(line)
            self.assertEqual(captures, [key], name)
            self.assertNotIn("x-api-key", captures, name)

    def test_header_name_below_wide_capture_floor(self) -> None:
        # x-api-key is 9 chars, under the wide override's 20-char capture
        # floor — the property that makes the self-match impossible in the
        # first place (brave's 20-char name needed an explicit lookahead).
        self.assertLess(len("x-api-key"), 20)
        for name, pattern in self.wide_patterns.items():
            self.assertEqual(re.compile(pattern).groups, 1, name)
            self.assertIsNone(re.compile(pattern).match("x-api-key"), name)

    def test_header_name_branch_still_captures_token(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("x-api-key: " + key), [key], name)
            self.assertEqual(rx.findall('x-api-key: "' + key + '"'), [key], name)

    def test_wide_pattern_matches_quoted_token(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall('"' + key + '"'), [key], name)

    def test_wide_pattern_matches_bearer(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Authorization: Bearer " + key), [key], name)

    def test_single_capture_group(self) -> None:
        for name in self.tasks:
            self.assertEqual(re.compile(self.task_patterns[name]).groups, 1, name)
            self.assertEqual(self.wide_regexes[name].groups, 1, name)

    def test_defaults_examples_lockstep(self) -> None:
        # The two sources carry the same regexes but differ in redundant-escape
        # style inside raw strings (\' vs ') — compare canonical forms, then
        # prove behavioral equivalence on the probe battery.
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
            '-H "x-api-key: ' + key + '",',
            "x-api-key: " + key,
            "Authorization: Bearer " + key,
            '"' + key + '"',
            "EXA_API_KEY=" + key,
            'exa_api_key="' + key + '"',
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

    def test_task_pattern_env_anchored(self) -> None:
        key = _real_key()
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            self.assertEqual(rx.findall("EXA_API_KEY=" + key), [key], name)
            self.assertEqual(rx.findall('exa_api_key="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("EXA_KEY=" + key), [key], name)
            # Empty assignment: the value floor (16) must refuse to match.
            self.assertEqual(rx.findall("EXA_API_KEY="), [], name)
            # Bare prose without an assignment must not match.
            self.assertIsNone(rx.search("exa api key"), name)


if __name__ == "__main__":
    unittest.main()