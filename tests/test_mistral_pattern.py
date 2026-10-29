"""Behavioral tests for the Mistral key patterns.

Pins the 2026-10-29 measured format: the key has NO prefix — 4/4 real corpus
values were exactly 32 chars of mixed ``[A-Za-z0-9]``, no ``-``, no ``_``. A
prefix-less 32-char alnum string is not greppable on its own (hashes and ids
share the shape), so extraction is env-anchored (``MISTRAL_API_KEY``) plus
domain-anchored (``api.mistral.ai``) — the serpapi env+URL anchoring precedent.

Invariants:
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the task-level pattern only fires on ``mistral*api*key`` assignments,
    not on bare prose;
  - the wide override matches Bearer / quoted forms;
  - the wide body class is exactly 32 alnum chars — a 33-char token must not
    yield its 32-char prefix (trailing boundary guard);
  - placeholders (``your_mistral_api_key`` / ``XXXXXXXXX`` / ``<mistral
    token>``) never match;
  - config/defaults.py's mistral preset stays in lockstep with
    examples/config-mistral.yaml (task pattern + wide override).

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-mistral.yaml"
_PRESET = "mistral"


def _real_key() -> str:
    # Measured shape: 32 mixed alnum chars, no prefix. Adjacent literals keep
    # any single source token from being a contiguous secret-shaped string.
    return "bgtV" "x9Qm" "Pz7L" "rW2n" "Kd8H" "sT4v" "Nc6F" "yJ3p"


def _long_key() -> str:
    # 36 chars: a wider token must not be truncated to its first 32.
    return _real_key() + "wQ5z" "Mv7R"


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("mistral preset missing from config.defaults")


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


class TestMistralKeyPattern(unittest.TestCase):
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

    def test_measured_shape_matches(self) -> None:
        # 32 mixed alnum chars, no prefix — the measured real-key shape.
        key = _real_key()
        self.assertEqual(len(key), 32)
        for name, pattern in self.task_patterns.items():
            self.assertEqual(re.compile(pattern).findall("MISTRAL_API_KEY=" + key), [key], name)
            self.assertEqual(re.compile(pattern).findall('MISTRAL_API_KEY="' + key + '"'), [key], name)

    def test_task_pattern_env_anchored(self) -> None:
        key = _real_key()
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            self.assertEqual(rx.findall("MISTRAL_API_KEY=" + key), [key], name)
            self.assertEqual(rx.findall("MISTRAL_API_KEY: " + key), [key], name)
            self.assertEqual(rx.findall('"mistral_api_key": "' + key + '"'), [key], name)
            self.assertEqual(rx.findall("mistral_api_key = '" + key + "'"), [key], name)
            # Bare prose without an assignment must not match.
            self.assertIsNone(rx.search("mistral api key"), name)
            # A bare 32-char alnum run is never captured without the env name.
            self.assertEqual(rx.findall("token " + key), [], name)

    def test_wide_pattern_matches_bearer_and_quoted(self) -> None:
        key = _real_key()
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Authorization: Bearer " + key), [key], name)
            self.assertEqual(rx.findall('"' + key + '"'), [key], name)

    def test_wide_pattern_rejects_header_name_and_bare_runs(self) -> None:
        for name, rx in self.wide_regexes.items():
            # "Authorization" is 13 chars and not alnum-32; no self-match.
            self.assertEqual(rx.findall("Bearer Authorization"), [], name)
            # A bare hash-shaped run with no Bearer/quote anchor is not a key.
            self.assertEqual(rx.findall("sha256 " + _real_key()), [], name)

    def test_exact_32_body_is_not_truncated(self) -> None:
        # A 36-char token must not yield its first 32 chars as a "key".
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            self.assertEqual(rx.findall("MISTRAL_API_KEY=" + _long_key()), [], name)
        for name, rx in self.wide_regexes.items():
            self.assertEqual(rx.findall("Bearer " + _long_key()), [], name)

    def test_task_pattern_rejects_placeholders(self) -> None:
        for name, pattern in self.task_patterns.items():
            rx = re.compile(pattern)
            for placeholder in (
                "your_mistral_api_key",
                "your-mistral-key",
                "XXXXXXXXX",
                "<mistral token>",
                "your_api_key",
                "test0123456789012345678901234567",
                "dummy01234567890123456789012345",
                "example012345678901234567890123",
                "changeme01234567890123456789012",
                "short0123456789012345",  # < 32 chars
            ):
                self.assertEqual(rx.findall("MISTRAL_API_KEY=" + placeholder), [], f"{name} {placeholder}")

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
            "MISTRAL_API_KEY=" + key,
            'mistral_api_key="' + key + '"',
            "Authorization: Bearer " + key,
            '"' + key + '"',
            "MISTRAL_API_KEY=your_mistral_api_key",
            "Bearer " + _long_key(),
            "sha256 " + key,
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