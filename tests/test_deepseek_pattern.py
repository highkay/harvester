"""Behavioral tests for the DeepSeek key patterns.

Pins the 2026-09-22 rework from the naked ``sk-[0-9A-Za-z_-]{20,}`` pattern to
context-anchored extraction (mirrors the committed kimi fix). The prod
measurement that forced it:

  * authentic DeepSeek keys are ``sk-`` + 32 hex (n=401 measured, prod
    2026-09-22); a run yielded 477 authentic keys against 2572 invalid
    candidates (sk-ant- 186, sk-or-… and other vendors' bare ``sk-`` runs).

Invariants:
  - the task-level pattern only fires on DEEPSEEK_* env assignments;
  - bare ``sk-`` runs (the flood source) never match it, whatever their length;
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the lookahead skips sk-ant- / sk-proj- / sk-svcacct- keys;
  - only domain-named dorks carry a per-condition pattern, and every
    domain-named dork has one (Bearer/quoted/plain widening);
  - the body has no upper bound: the 32-hex shape is documented, not enforced
    (serpapi lesson — a fixed width silently drops rotated keys);
  - the config/defaults.py deepseek preset stays in lockstep with
    examples/config-deepseek.yaml (task pattern, condition order, overrides),
    and the config-full.yaml deepseek task pattern matches the dedicated one.

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-deepseek.yaml"
_FULL = "config-full.yaml"
_PRESET = "deepseek"
# Queries containing these markers are "domain-named" and MUST widen the
# extraction with a per-condition override; everything else inherits the
# anchored task pattern.
_DOMAIN_MARKERS = ("api.deepseek.com", '"deepseek" "api_key"')
_CONDITION_COUNT = 15

# Measured real-key shape: sk- + 32 hex, built in 4-char chunks.
_BODY_32 = "".join(("0a1b", "2c3d", "4e5f", "6071", "8293", "a4b5", "c6d7", "e8f9"))
# Longer mixed-alnum body for the no-upper-bound checks (kimi-measured shape).
_BODY_48 = "".join(
    ("aB3d", "E5fG", "7hI9", "jK1l", "M3nO", "5pQ7", "rS9t", "U1vW", "3xY5", "zA7b", "C9dE", "1fG3")
)
_BODY_72 = _BODY_48 + "".join(("hI5j", "K7lM", "9nO1", "pQ3r", "S5tU", "7vW9"))


def _real_key() -> str:
    return "sk-" + _BODY_32


def _long_body_key() -> str:
    return "sk-" + _BODY_72


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("deepseek preset missing from config.defaults")


def _full_task(name: str) -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _FULL).read_text(encoding="utf-8"))
    for task in raw["tasks"]:
        if task.get("name") == name:
            return task
    raise AssertionError(f"{name} task missing from config-full.yaml")


def _condition_patterns(task: dict) -> dict:
    """{query: pattern} for the conditions that override the task pattern."""
    return {
        str(condition["query"]): str(condition["patterns"]["key_pattern"])
        for condition in task["conditions"]
        if "patterns" in condition
    }


class TestDeepSeekKeyPattern(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = {"example": _example_task(), "preset": _preset_task()}
        self.patterns = {
            name: str(task["patterns"]["key_pattern"]) for name, task in self.tasks.items()
        }
        self.regexes = {name: re.compile(pattern) for name, pattern in self.patterns.items()}

    def test_task_patterns_stay_in_lockstep(self) -> None:
        self.assertEqual(len(set(self.patterns.values())), 1, self.patterns)
        overrides = {name: set(_condition_patterns(task).values()) for name, task in self.tasks.items()}
        for name, values in overrides.items():
            self.assertTrue(values, name)
            self.assertEqual(len(values), 1, f"{name}: {values}")
        self.assertEqual(
            len({pattern for values in overrides.values() for pattern in values}), 1, overrides
        )

    def test_config_full_task_pattern_matches_dedicated(self) -> None:
        full = str(_full_task(_PRESET)["patterns"]["key_pattern"])
        self.assertEqual(full, self.patterns["example"])

    def test_condition_structure_pinned(self) -> None:
        for name, task in self.tasks.items():
            self.assertEqual(len(task["conditions"]), _CONDITION_COUNT, name)
        self.assertEqual(
            [str(c["query"]) for c in self.tasks["preset"]["conditions"]],
            [str(c["query"]) for c in self.tasks["example"]["conditions"]],
        )

    def test_matches_deepseek_env_assignments(self) -> None:
        key = _real_key()
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall('DEEPSEEK_API_KEY="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("DEEPSEEK_API_KEY: " + key), [key], name)
            self.assertEqual(rx.findall("deepseek_api_key=" + key), [key], name)
            self.assertEqual(rx.findall("DEEPSEEK_KEY='" + key + "'"), [key], name)
            self.assertEqual(rx.findall('"DEEPSEEK_API_KEY": "' + key + '"'), [key], name)

    def test_rejects_bare_sk_runs(self) -> None:
        # The invalid-bucket flood: bare sk- runs at every observed body length.
        for name, rx in self.regexes.items():
            for length in (20, 24, 32, 48, 52, 72):
                self.assertIsNone(rx.search("sk-" + _BODY_72[:length]), f"{name} len={length}")

    def test_lookahead_excludes_other_provider_keys(self) -> None:
        for name, rx in self.regexes.items():
            for prefix in ("ant", "proj", "svcacct"):
                self.assertIsNone(
                    rx.search("DEEPSEEK_API_KEY=sk-" + prefix + "-" + _BODY_32), f"{name} {prefix}"
                )

    def test_single_capture_group(self) -> None:
        for name, rx in self.regexes.items():
            self.assertEqual(rx.groups, 1, name)
            for query, pattern in _condition_patterns(self.tasks[name]).items():
                self.assertEqual(re.compile(pattern).groups, 1, f"{name} {query}")

    def test_override_only_on_domain_dorks(self) -> None:
        for name, task in self.tasks.items():
            for condition in task["conditions"]:
                query = str(condition["query"])
                has_domain = any(marker in query for marker in _DOMAIN_MARKERS)
                self.assertEqual(has_domain, "patterns" in condition, f"{name}: {query}")

    def test_override_matches_bearer_quoted_and_bare(self) -> None:
        key = _real_key()
        for name, task in self.tasks.items():
            for query, pattern in _condition_patterns(task).items():
                rx = re.compile(pattern)
                self.assertEqual(rx.findall("Bearer " + key), [key], f"{name} {query}")
                self.assertEqual(rx.findall('"' + key + '"'), [key], f"{name} {query}")
                self.assertEqual(rx.findall(key), [key], f"{name} {query}")

    def test_no_upper_bound_on_body(self) -> None:
        # A rotated or longer body must be captured in full, never truncated
        # (the 32-hex measurement is documented, not enforced — serpapi lesson).
        key = _long_body_key()
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall("DEEPSEEK_API_KEY=" + key), [key], name)
        for name, task in self.tasks.items():
            for query, pattern in _condition_patterns(task).items():
                self.assertEqual(re.compile(pattern).findall(key), [key], f"{name} {query}")


if __name__ == "__main__":
    unittest.main()
