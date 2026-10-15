"""Behavioral tests for the Brave key patterns.

Pins the 2026-10-15 self-match fix: the wide domain-anchored override pattern's
bare-quote branch used to capture the literal header name from curl-style
lines (``-H "x-subscription-token: <token>"`` — the name is exactly 20
charset-legal chars). A ``(?i)``-scoped negative lookahead after the opening
alternation now skips it, while the explicit ``X-Subscription-Token`` branch
still consumes name+separator and captures the token.

Invariants:
  - the curl -H line above captures the TOKEN, never the header name;
  - Bearer and quoted-token forms still capture the token;
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the config/defaults.py brave preset stays in lockstep with
    examples/config-brave.yaml (task pattern + wide override, parsed values);
  - the task-level pattern only fires on BRAVE_* env assignments, not bare
    prose.

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-brave.yaml"
_PRESET = "brave"


def _real_key() -> str:
    return "".join(("aB3d", "E5fG", "7hI9", "jK1l", "M3nO", "5pQ7", "rS9t", "U1vW", "3xY5", "zA7b"))


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("brave preset missing from config.defaults")


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


class TestBraveKeyPattern(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = {"example": _example_task(), "preset": _preset_task()}
        self.task_patterns = {
            name: str(task["patterns"]["key_pattern"]) for name, task in self.tasks.items()
        }
        self.wide_patterns = {name: _wide_pattern(task) for name, task in self.tasks.items()}
        self.wide_regexes = {name: re.compile(p) for name, p in self.wide_patterns.items()}

    def test_header_line_self_match_rejected(self) -> None:
        # The confirmed defect: the bare-quote branch ate the header name
        # (exactly 20 charset-legal chars). The capture must be the token.
        key = _real_key()
        line = '-H "x-subscription-token: ' + key + '"'
        for name, rx in self.wide_regexes.items():
            captures = rx.findall(line)
            self.assertEqual(captures, [key], name)
            self.assertNotIn("x-subscription-token", captures, name)

    def test_header_name_branch_still_captures_token(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("X-Subscription-Token: " + key), [key], name)
            self.assertEqual(rx.findall('X-Subscription-Token: "' + key + '"'), [key], name)

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
            '-H "x-subscription-token: ' + key + '",',
            "X-Subscription-Token: " + key,
            "Authorization: Bearer " + key,
            '"' + key + '"',
            "BRAVE_API_KEY=" + key,
            'brave_api_key="' + key + '"',
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
            self.assertEqual(rx.findall("BRAVE_API_KEY=" + key), [key], name)
            self.assertEqual(rx.findall('brave_api_key="' + key + '"'), [key], name)
            # Bare prose without an assignment must not match.
            self.assertIsNone(rx.search("brave api key"), name)


if __name__ == "__main__":
    unittest.main()
