"""Behavioral tests for the Qwen (DashScope) key patterns.

Pins the 2026-09-22 rework from the naked ``sk-[0-9A-Za-z_-]{20,}`` pattern to
context-anchored extraction (mirrors the committed kimi fix). The prod
measurement that forced it:

  * authentic qwen-cn keys are ``sk-`` + 32 hex (n=315 measured, prod
    2026-09-22) against 1441 invalid bare-``sk-`` candidates per run
    (placeholders and other vendors' keys sharing the prefix).

Invariants:
  - the task-level pattern only fires on DASHSCOPE_*/QWEN_* env assignments;
  - bare ``sk-`` runs (the flood source) never match it, whatever their length;
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the lookahead skips sk-ant- / sk-proj- / sk-svcacct- keys;
  - only dorks naming the scanned endpoint host (dashscope.aliyuncs.com for
    qwen-cn, dashscope-intl.aliyuncs.com for qwen-intl) carry a per-condition
    override, and every host-named dork has one;
  - the body has no upper bound (serpapi lesson — the 32-hex shape is
    documented, not enforced);
  - examples/config-qwen.yaml and examples/config-qwen-intl.yaml keep the same
    task pattern and the same override pattern, and the config/defaults.py
    qwen preset stays in lockstep with the CN example.

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIGS = ("config-qwen.yaml", "config-qwen-intl.yaml")
_PRESET = "qwen"
_CN_EXAMPLE = "config-qwen.yaml"
# Hosts that make a dork "domain-named" (substring test on the query).
_DOMAIN_MARKERS = {
    "config-qwen.yaml": ("dashscope.aliyuncs.com",),
    "config-qwen-intl.yaml": ("dashscope-intl.aliyuncs.com",),
}
_CONDITION_COUNTS = {"config-qwen.yaml": 14, "config-qwen-intl.yaml": 14}

# Measured real-key shape: sk- + 32 hex, built in 4-char chunks.
_BODY_32 = "".join(("9f8e", "7d6c", "5b4a", "3928", "1706", "f5e4", "d3c2", "b1a0"))
# Longer mixed-alnum body for the no-upper-bound checks.
_BODY_48 = "".join(
    ("pQ2r", "S5tU", "7vW9", "xY1z", "A3bC", "5dE7", "fG9h", "I1jK", "3lM5", "nO7p", "Q9rS", "1tU3")
)
_BODY_72 = _BODY_48 + "".join(("vW5x", "Y7zA", "9bC1", "dE3f", "G5hI", "7jK9"))


def _real_key() -> str:
    return "sk-" + _BODY_32


def _long_body_key() -> str:
    return "sk-" + _BODY_72


def _task(filename: str) -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / filename).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("qwen preset missing from config.defaults")


def _condition_patterns(task: dict) -> dict:
    """{query: pattern} for the conditions that override the task pattern."""
    return {
        str(condition["query"]): str(condition["patterns"]["key_pattern"])
        for condition in task["conditions"]
        if "patterns" in condition
    }


class TestQwenKeyPatterns(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = {name: _task(name) for name in _CONFIGS}
        self.tasks["preset"] = _preset_task()
        self.patterns = {
            name: str(task["patterns"]["key_pattern"]) for name, task in self.tasks.items()
        }
        self.regexes = {name: re.compile(pattern) for name, pattern in self.patterns.items()}

    def test_configs_and_preset_stay_in_lockstep(self) -> None:
        self.assertEqual(len(set(self.patterns.values())), 1, self.patterns)
        overrides = {name: set(_condition_patterns(task).values()) for name, task in self.tasks.items()}
        for name, values in overrides.items():
            self.assertTrue(values, name)
            self.assertEqual(len(values), 1, f"{name}: {values}")
        self.assertEqual(
            len({pattern for values in overrides.values() for pattern in values}), 1, overrides
        )

    def test_condition_structure_pinned(self) -> None:
        for name in _CONFIGS:
            self.assertEqual(len(self.tasks[name]["conditions"]), _CONDITION_COUNTS[name], name)
        self.assertEqual(
            [str(c["query"]) for c in self.tasks["preset"]["conditions"]],
            [str(c["query"]) for c in self.tasks[_CN_EXAMPLE]["conditions"]],
        )

    def test_matches_dashscope_and_qwen_env_assignments(self) -> None:
        key = _real_key()
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall('DASHSCOPE_API_KEY="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("DASHSCOPE_API_KEY: " + key), [key], name)
            self.assertEqual(rx.findall("dashscope_api_key=" + key), [key], name)
            self.assertEqual(rx.findall("QWEN_API_KEY='" + key + "'"), [key], name)
            self.assertEqual(rx.findall("qwen_api_key=" + key), [key], name)
            self.assertEqual(rx.findall('"DASHSCOPE_API_KEY": "' + key + '"'), [key], name)

    def test_rejects_bare_sk_runs(self) -> None:
        # The invalid-bucket flood: bare sk- runs at every observed body length.
        for name, rx in self.regexes.items():
            for length in (20, 24, 32, 48, 52, 72):
                self.assertIsNone(rx.search("sk-" + _BODY_72[:length]), f"{name} len={length}")

    def test_lookahead_excludes_other_provider_keys(self) -> None:
        for name, rx in self.regexes.items():
            for prefix in ("ant", "proj", "svcacct"):
                self.assertIsNone(
                    rx.search("DASHSCOPE_API_KEY=sk-" + prefix + "-" + _BODY_32), f"{name} {prefix}"
                )

    def test_single_capture_group(self) -> None:
        for name, rx in self.regexes.items():
            self.assertEqual(rx.groups, 1, name)
            for query, pattern in _condition_patterns(self.tasks[name]).items():
                self.assertEqual(re.compile(pattern).groups, 1, f"{name} {query}")

    def test_override_only_on_domain_dorks(self) -> None:
        for name, task in self.tasks.items():
            markers = _DOMAIN_MARKERS.get(name, _DOMAIN_MARKERS[_CN_EXAMPLE])
            for condition in task["conditions"]:
                query = str(condition["query"])
                has_domain = any(marker in query for marker in markers)
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
            self.assertEqual(rx.findall("DASHSCOPE_API_KEY=" + key), [key], name)
        for name, task in self.tasks.items():
            for query, pattern in _condition_patterns(task).items():
                self.assertEqual(re.compile(pattern).findall(key), [key], f"{name} {query}")


if __name__ == "__main__":
    unittest.main()
