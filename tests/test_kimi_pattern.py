"""Behavioral tests for the Moonshot (kimi / kimi-ai) key patterns.

Pins the 2026-09-22 rework from the naked ``sk-[0-9A-Za-z_-]{20,}`` pattern to
context-anchored extraction. The prod measurement that forced it:

  * authentic keys (the no-quota bucket, i.e. they passed provider auth):
    229/229 ``kimi`` and 15/15 ``kimi-ai`` were exactly ``sk-`` + 48 alnum;
  * the same runs' invalid buckets held 1727 / 1001 bare ``sk-`` candidates
    with body lengths scattered 20-57 (placeholders, other providers).

Invariants:
  - the task-level pattern only fires on MOONSHOT_*/KIMI_* env assignments;
  - bare ``sk-`` runs (the flood source) never match it, whatever their length;
  - exactly ONE capture group everywhere (``findall`` returns the key string);
  - the lookahead skips sk-ant- / sk-proj- / sk-svcacct- keys;
  - only domain-named dorks carry a per-condition pattern, and every
    domain-named dork has one (Bearer/quoted/plain widening);
  - the body has no upper bound, so format rotation cannot silently truncate;
  - examples/config-kimi.yaml and examples/config-kimi-ai.yaml stay in
    lockstep (identical task pattern and identical override pattern).

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIGS = ("config-kimi.yaml", "config-kimi-ai.yaml")
# Hosts that make a dork "domain-anchored" (substring test on the query).
_DOMAIN_MARKERS = (
    "api.moonshot.cn",
    "api.moonshot.ai",
    "platform.kimi.ai",
    "moonshot.ai",
    "kimi.ai",
)
_CONDITION_COUNTS = {"config-kimi.yaml": 14, "config-kimi-ai.yaml": 14}

# Measured real-key shape: sk- + 48 alnum (mixed case + digits), concatenated.
_BODY_48 = "".join(
    ("aB3d", "E5fG", "7hI9", "jK1l", "M3nO", "5pQ7", "rS9t", "U1vW", "3xY5", "zA7b", "C9dE", "1fG3")
)
_BODY_72 = _BODY_48 + "".join(("hI5j", "K7lM", "9nO1", "pQ3r", "S5tU", "7vW9"))


def _real_key() -> str:
    return "sk-" + _BODY_48


def _long_body_key() -> str:
    return "sk-" + _BODY_72


def _task(filename: str) -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / filename).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _patterns(task: dict) -> dict:
    """{query: pattern} for the conditions that override the task pattern."""
    return {
        str(condition["query"]): str(condition["patterns"]["key_pattern"])
        for condition in task["conditions"]
        if "patterns" in condition
    }


class TestMoonshotKeyPatterns(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = {name: _task(name) for name in _CONFIGS}
        self.patterns = {name: str(task["patterns"]["key_pattern"]) for name, task in self.tasks.items()}
        self.regexes = {name: re.compile(pattern) for name, pattern in self.patterns.items()}

    def test_configs_stay_in_lockstep(self) -> None:
        self.assertEqual(len(set(self.patterns.values())), 1, self.patterns)
        overrides = {name: set(_patterns(task).values()) for name, task in self.tasks.items()}
        for name, values in overrides.items():
            self.assertTrue(values, name)
            self.assertEqual(len(values), 1, f"{name}: {values}")
        self.assertEqual(
            len({pattern for values in overrides.values() for pattern in values}), 1, overrides
        )

    def test_condition_counts_pinned(self) -> None:
        for name, task in self.tasks.items():
            self.assertEqual(len(task["conditions"]), _CONDITION_COUNTS[name], name)

    def test_matches_moonshot_env_assignments(self) -> None:
        key = _real_key()
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall('MOONSHOT_API_KEY="' + key + '"'), [key], name)
            self.assertEqual(rx.findall("KIMI_API_KEY: " + key), [key], name)
            self.assertEqual(rx.findall("moonshot_api_key=" + key), [key], name)
            self.assertEqual(rx.findall("MOONSHOT_KEY='" + key + "'"), [key], name)
            self.assertEqual(rx.findall('"MOONSHOT_API_KEY": "' + key + '"'), [key], name)

    def test_rejects_bare_sk_runs(self) -> None:
        # The invalid-bucket flood: bare sk- runs at every observed body length.
        for name, rx in self.regexes.items():
            for length in (20, 21, 32, 44, 48, 50, 52, 53, 57, 72):
                self.assertIsNone(rx.search("sk-" + _BODY_72[:length]), f"{name} len={length}")

    def test_lookahead_excludes_other_provider_keys(self) -> None:
        for name, rx in self.regexes.items():
            for prefix in ("ant", "proj", "svcacct"):
                self.assertIsNone(
                    rx.search("MOONSHOT_API_KEY=sk-" + prefix + "-" + _BODY_48), f"{name} {prefix}"
                )

    def test_single_capture_group(self) -> None:
        for name, rx in self.regexes.items():
            self.assertEqual(rx.groups, 1, name)
            for query, pattern in _patterns(self.tasks[name]).items():
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
            for query, pattern in _patterns(task).items():
                rx = re.compile(pattern)
                self.assertEqual(rx.findall("Bearer " + key), [key], f"{name} {query}")
                self.assertEqual(rx.findall('"' + key + '"'), [key], f"{name} {query}")
                self.assertEqual(rx.findall(key), [key], f"{name} {query}")

    def test_no_upper_bound_on_body(self) -> None:
        # A rotated or other-provider long body must be captured in full, never
        # truncated (a fixed width would silently drop real keys — serpapi).
        key = _long_body_key()
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall("MOONSHOT_API_KEY=" + key), [key], name)
        for name, task in self.tasks.items():
            for query, pattern in _patterns(task).items():
                self.assertEqual(re.compile(pattern).findall(key), [key], f"{name} {query}")


if __name__ == "__main__":
    unittest.main()