"""Behavioral tests for the Xiaomi MiMo (Token Plan) key patterns.

Pins the 2026-09-22 rework from the naked ``tp-[0-9A-Za-z_-]{20,}`` pattern to
context-anchored extraction (mirrors the committed kimi fix). The prod
measurement that forced it:

  * the authentic mimo Token Plan key is ``tp-`` + 48 alnum (n=1 measured,
    prod 2026-09-22) against 142 invalid candidates per run — ``tp-secret-`` /
    ``tp-your-`` style hyphenated placeholders and other noise.

Invariants:
  - the task-level pattern only fires on MIMO_*/XIAOMIMIMO_* env assignments;
  - bare ``tp-`` runs never match it, whatever their length;
  - the captured family stays ``tp-`` ONLY: ``sk-`` keys are never captured,
    anchored or not (re-flood guard — sk- is a different, unmeasured vendor
    surface: pay-as-you-go mimo keys on api.xiaomimimo.com are NOT scanned);
  - hyphenated placeholders (tp-secret-, tp-your-) cannot match the alnum body;
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - only dorks naming a xiaomimimo.com host carry a per-condition override,
    and every host-named dork has one; overrides also capture ``tp-`` only;
  - the body has no upper bound (serpapi lesson — the 48-alnum shape is
    documented, not enforced);
  - examples/config-mimo.yaml and examples/config-mimo-sg.yaml keep the same
    task pattern and the same override pattern, and the config/defaults.py
    mimo preset stays in lockstep with the CN example.

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIGS = ("config-mimo.yaml", "config-mimo-sg.yaml")
_PRESET = "mimo"
_CN_EXAMPLE = "config-mimo.yaml"
# Any dork naming a xiaomimimo.com host (token-plan-cn/-sgp included by
# substring) is "domain-named" and MUST widen with a per-condition override.
_DOMAIN_MARKERS = ("xiaomimimo.com",)
_CONDITION_COUNTS = {"config-mimo.yaml": 15, "config-mimo-sg.yaml": 14}

# Measured real-key shape: tp- + 48 alnum, built in 4-char chunks.
_BODY_48 = "".join(
    ("kR7t", "2mPq", "9xVb", "N4wZ", "6hJd", "Q8sL", "3cFg", "Y1nB", "5tHk", "W7pR", "2zMd", "8jXv")
)
_BODY_72 = _BODY_48 + "".join(("L4qN", "6bTz", "9wKm", "3pXs", "R7dV", "1hGc"))


def _real_key() -> str:
    return "tp-" + _BODY_48


def _long_body_key() -> str:
    return "tp-" + _BODY_72


def _task(filename: str) -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / filename).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("mimo preset missing from config.defaults")


def _condition_patterns(task: dict) -> dict:
    """{query: pattern} for the conditions that override the task pattern."""
    return {
        str(condition["query"]): str(condition["patterns"]["key_pattern"])
        for condition in task["conditions"]
        if "patterns" in condition
    }


class TestMimoKeyPatterns(unittest.TestCase):
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

    def test_matches_mimo_env_assignments(self) -> None:
        key = _real_key()
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall('MIMO_API_KEY="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("MIMO_API_KEY: " + key), [key], name)
            self.assertEqual(rx.findall("mimo_api_key=" + key), [key], name)
            self.assertEqual(rx.findall("XIAOMIMIMO_API_KEY='" + key + "'"), [key], name)
            self.assertEqual(rx.findall("MIMO_TOKEN=" + key), [key], name)
            self.assertEqual(rx.findall('"MIMO_API_KEY": "' + key + '"'), [key], name)

    def test_rejects_bare_tp_runs(self) -> None:
        for name, rx in self.regexes.items():
            for length in (20, 24, 32, 48, 72):
                self.assertIsNone(rx.search("tp-" + _BODY_72[:length]), f"{name} len={length}")

    def test_never_captures_sk_family(self) -> None:
        # The captured family is tp- only: sk- keys must never be extracted by
        # the task pattern (even env-anchored) nor by the host-dork overrides
        # — widening to sk- would re-flood the check stage with other vendors.
        sk = "sk-" + _BODY_48
        for name, rx in self.regexes.items():
            self.assertIsNone(rx.search("MIMO_API_KEY=" + sk), name)
            self.assertIsNone(rx.search("MIMO_API_KEY=sk-ant-" + _BODY_48), name)
        for name, task in self.tasks.items():
            for query, pattern in _condition_patterns(task).items():
                self.assertIsNone(
                    re.compile(pattern).search("Bearer " + sk), f"{name} {query}"
                )

    def test_rejects_hyphenated_placeholders(self) -> None:
        # The measured invalid flood: tp-secret- / tp-your- style doc
        # placeholders cannot satisfy the alnum-only body (the hyphen cuts
        # the run before the 16-char floor).
        placeholders = (
            "tp-your-" + "api-key-here",
            "tp-secret-" + "token-value",
            "tp-abc123-" + "def456-ghi789",
        )
        for name, rx in self.regexes.items():
            for placeholder in placeholders:
                self.assertIsNone(rx.search("MIMO_API_KEY=" + placeholder), f"{name} {placeholder}")

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
        # (the 48-alnum measurement is documented, not enforced — serpapi).
        key = _long_body_key()
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall("MIMO_API_KEY=" + key), [key], name)
        for name, task in self.tasks.items():
            for query, pattern in _condition_patterns(task).items():
                self.assertEqual(re.compile(pattern).findall(key), [key], f"{name} {query}")


if __name__ == "__main__":
    unittest.main()
