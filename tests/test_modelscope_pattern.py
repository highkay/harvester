"""Behavioral tests for the modelscope key pattern (ms- anchored extraction).

Pins the invariants of the 2026-09 rework from the naked
``[0-9A-Za-z_-]{20,}`` pattern to context-anchored ``ms-`` extraction:

  - the task-level pattern only fires on MODELSCOPE_* env-name assignments;
  - extracted values MUST carry the ``ms-`` prefix (length-independent,
    floor ``ms-`` + 8 — no measured real-key corpus yet, keep it broad);
  - exactly ONE capture group: ``re.findall`` returns the key string, never
    a tuple, and no bare long-alnum branch can match without an anchor;
  - domain-anchored conditions widen to Bearer/quoted and oauth2 URL forms;
  - examples/config-modelscope.yaml and the defaults preset stay in
    lockstep (task pattern AND every domain condition pattern identical);
  - env dorks inherit the task pattern (no per-condition override) and the
    dropped "MODELSCOPE_TOKEN" anchor stays dropped.

NOTE: all candidate strings are built by concatenation at runtime so no
source line carries a contiguous secret-shaped ``ms-``-prefixed literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_DOMAIN_QUERIES = [
    '"modelscope.cn" "Authorization"',
    '"oauth2:" "modelscope.cn"',
    '"api-inference.modelscope.cn"',
    '"api-inference.modelscope.cn" "Authorization"',
]
_BEARER_QUERY = '"modelscope.cn" "Authorization"'
_OAUTH2_QUERY = '"oauth2:" "modelscope.cn"'


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == "modelscope":
            return task
    raise AssertionError("modelscope preset missing from config.defaults")


def _example_task() -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load(
        (repo_root / "examples" / "config-modelscope.yaml").read_text(encoding="utf-8")
    )
    return raw["tasks"][0]


def _example_pattern() -> str:
    return str(_example_task()["patterns"]["key_pattern"])


def _condition_patterns(task: dict) -> dict:
    return {
        str(condition["query"]): str(condition["patterns"]["key_pattern"])
        for condition in task["conditions"]
        if "patterns" in condition
    }


def _domain_pattern(task: dict, query: str) -> str:
    return str(_condition_patterns(task)[query])


class TestModelScopeKeyPattern(unittest.TestCase):
    def setUp(self) -> None:
        self.pattern = str(_preset_task()["patterns"]["key_pattern"])
        self.re = re.compile(self.pattern)

    def test_matches_ms_prefixed_env_assignment(self) -> None:
        key = "ms-" + "Ab3xYz9" + "pQ"
        quoted = f'MODELSCOPE_API_KEY="{key}"'
        found = self.re.findall(quoted)
        self.assertEqual(found, [key])
        self.assertIsInstance(found[0], str)
        # Colon/unquoted assignment shape must match too.
        colon = "MODELSCOPE_SDK_TOKEN: " + key
        self.assertEqual(self.re.findall(colon), [key])

    def test_rejects_bare_hex_without_env_anchor(self) -> None:
        bare = "A1b2" + "C3d4" + "E5f6" + "G7h8" + "I9j0" + "K1l2" + "M3n4" + "O5p6"
        self.assertIsNone(self.re.search(bare))

    def test_rejects_non_ms_env_value(self) -> None:
        value = "Xy3kP9" + "mQ2vR8" + "tN5wZ" + "cL1"
        self.assertIsNone(self.re.search("MODELSCOPE_API_KEY=" + value))

    def test_requires_ms_prefix(self) -> None:
        keyless = "Zq7Vb3" + "Nk9Rd1" + "Tf5Gx2" + "Hj8"
        self.assertIsNone(self.re.search("MODELSCOPE_SDK_TOKEN: " + "'" + keyless + "'"))

    def test_single_capture_group(self) -> None:
        self.assertEqual(self.re.groups, 1)
        for query, pattern in _condition_patterns(_preset_task()).items():
            self.assertEqual(re.compile(pattern).groups, 1, query)

    def test_length_floor_eight(self) -> None:
        self.assertIsNone(
            self.re.search("MODELSCOPE_API_KEY=" + '"' + "ms-" + "Ab3xYz9" + '"')
        )
        self.assertIsNotNone(
            self.re.search("MODELSCOPE_API_KEY=" + '"' + "ms-" + "Ab3xYz9" + "pQ" + '"')
        )

    def test_no_bare_branch(self) -> None:
        bare = "K9z-Q8y_P7" + "xW6vU5tS4rQ" + "3pO2nM1l"
        self.assertIsNone(self.re.search(bare))

    def test_domain_bearer_pattern_matches(self) -> None:
        key = "ms-" + "Kq8Rt2Wx" + "5Nv"
        rx = re.compile(_domain_pattern(_preset_task(), _BEARER_QUERY))
        self.assertEqual(rx.findall("Bearer " + key), [key])
        self.assertEqual(rx.findall('"' + key + '"'), [key])
        self.assertEqual(rx.findall(key), [key])

    def test_domain_oauth2_pattern_matches(self) -> None:
        key = "ms-" + "Pq4Zs7Xc" + "2Mn"
        rx = re.compile(_domain_pattern(_preset_task(), _OAUTH2_QUERY))
        url = "oauth2:" + key + "@api-inference.modelscope.cn"
        self.assertEqual(rx.findall(url), [key])
        self.assertIsNone(rx.search("Bearer " + key))

    def test_example_yaml_matches_preset(self) -> None:
        self.assertEqual(_example_pattern(), self.pattern)
        preset_cond = _condition_patterns(_preset_task())
        example_cond = _condition_patterns(_example_task())
        self.assertEqual(set(preset_cond), set(_DOMAIN_QUERIES))
        for query, pattern in example_cond.items():
            self.assertEqual(pattern, preset_cond[query], query)

    def test_condition_structure_pinned(self) -> None:
        for task in (_preset_task(), _example_task()):
            conditions = task["conditions"]
            queries = [str(condition["query"]) for condition in conditions]
            self.assertEqual(len(conditions), 20, queries)
            self.assertNotIn('"MODELSCOPE_TOKEN"', queries)
            self.assertEqual(set(_condition_patterns(task)), set(_DOMAIN_QUERIES))
            for condition in conditions:
                if str(condition["query"]) not in _DOMAIN_QUERIES:
                    self.assertNotIn("patterns", condition, condition["query"])


if __name__ == "__main__":
    unittest.main()