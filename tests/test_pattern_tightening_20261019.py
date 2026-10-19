"""Behavioral tests for the 2026-10-19 measured pattern tightening.

Every width floor / charset below was measured whole-corpus on prod
(Agent E, 2026-10-19): the tightened capture keeps 100% of the current
valid keys while cutting candidate volume 25-99%. Floors, no ceilings —
the serpapi fixed-width lesson (a fixed width silently drops every key if
the provider rotates the format) is why no upper bound appears anywhere.

Covered:
  - capture behavior (measured-shape keys match; junk/short/vendor-other
    strings do not) for ollama, openrouter, tavily, nvidia, agnes-ai, glm,
    glm-ai;
  - examples/ <-> config/defaults.py preset lockstep for the providers that
    HAVE a preset (openrouter, tavily, agnes-ai, glm);
  - the P4 size-triplet dork additions and P2/P5 dork removals/additions.

NOTE: all candidate strings are built by concatenation at runtime so no
source line carries a contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HEX32 = "".join(("0123456789ab", "cdef01234567", "89abcdef"))


def _example_task(provider: str) -> dict:
    raw = yaml.safe_load(
        (_REPO_ROOT / "examples" / f"config-{provider}.yaml").read_text(encoding="utf-8")
    )
    return raw["tasks"][0]


def _preset_task(provider: str) -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == provider:
            return task
    raise AssertionError(f"{provider} preset missing from config.defaults")


def _task_pattern(task: dict) -> str:
    return str(task["patterns"]["key_pattern"])


def _queries(task: dict) -> list[str]:
    return [str(condition["query"]) for condition in task["conditions"]]


class TestTightenedCaptures(unittest.TestCase):
    def test_hex32_fixture_is_exactly_32(self) -> None:
        self.assertEqual(len(_HEX32), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in _HEX32))

    def test_ollama_measured_shape(self) -> None:
        rx = re.compile(_task_pattern(_example_task("ollama")))
        good = _HEX32 + "." + "aB3d" + "E5fG" + "7hI9" + "jK1l" + "M3nO" + "5pQ7-_"
        self.assertEqual(rx.findall("OLLAMA_API_KEY=" + good), [good])
        self.assertIsNone(rx.search("OLLAMA_API_KEY=" + _HEX32 + "." + "short"))
        self.assertIsNone(rx.search("OLLAMA_API_KEY=" + "Z" * 32 + "." + "aB3dE5fG7hI9jK1l"))

    def test_openrouter_floor_64(self) -> None:
        rx = re.compile(_task_pattern(_example_task("openrouter")))
        good = "sk-or-v1-" + "".join(("aB3d", "E5fG", "7hI9", "jK1l", "M3nO", "5pQ7",
                                      "rS9t", "U1vW", "3xY5", "zA7b", "C8d2", "E4f6",
                                      "H8j0", "K2l4", "M6n8", "O0p1"))
        self.assertEqual(len(good) - 9, 64)
        self.assertEqual(rx.findall(good), [good])
        self.assertIsNone(rx.search("sk-or-v1-" + "aB3dE5fG7hI9jK1l"))

    def test_tavily_floor_32_and_slug_rejection(self) -> None:
        rx = re.compile(_task_pattern(_example_task("tavily")))
        good = "tvly-dev-" + "".join(("aB3d", "E5fG", "7hI9", "jK1l",
                                      "M3nO", "5pQ7", "rS9t", "U1vW"))
        self.assertEqual(rx.findall(good), [good])
        self.assertIsNone(rx.search("tvly-rec"))
        self.assertIsNone(rx.search("tavily-web-search--research"))

    def test_nvidia_floor_64(self) -> None:
        rx = re.compile(_task_pattern(_example_task("nvidia")))
        good = "nvapi-" + "".join(("aB3d", "E5fG", "7hI9", "jK1l", "M3nO", "5pQ7",
                                   "rS9t", "U1vW", "3xY5", "zA7b", "C8d2", "E4f6",
                                   "H8j0", "K2l4", "M6n8", "O0p1"))
        self.assertEqual(rx.findall(good), [good])
        self.assertIsNone(rx.search("nvapi-" + "aB3dE5fG7hI9jK1l"))

    def test_agnes_ai_floor_48_and_vendor_exclusions(self) -> None:
        rx = re.compile(_task_pattern(_example_task("agnes-ai")))
        body = "".join(("aB3d", "E5fG", "7hI9", "jK1l", "M3nO", "5pQ7",
                        "rS9t", "U1vW", "3xY5", "zA7b", "C8d2", "E4f6"))
        good = "sk-" + body
        self.assertEqual(len(body), 48)
        self.assertEqual(rx.findall("AGNES_API_KEY=" + good), [good])
        for vendor in ("ant-", "proj-", "svcacct-", "or-", "kimi-"):
            self.assertIsNone(rx.search("AGNES_API_KEY=sk-" + vendor + body))
        self.assertIsNone(rx.search("AGNES_API_KEY=sk-" + body[:16]))

    def test_glm_body_charset_admits_dash_underscore(self) -> None:
        for provider in ("glm", "glm-ai"):
            rx = re.compile(_task_pattern(_example_task(provider)))
            plain = _HEX32 + "." + "".join(("aB3d", "E5fG", "7hI9", "jK1l"))
            with_marks = _HEX32 + "." + "".join(("aB3_", "E5fG", "7h-9", "jK1l"))
            self.assertEqual(rx.findall(plain), [plain], provider)
            self.assertEqual(rx.findall(with_marks), [with_marks], provider)


def _canonical(pattern: str) -> str:
    return re.sub(r"\\(['\"])", r"\1", pattern)


class TestLockstepWithPresets(unittest.TestCase):
    def test_presets_match_examples(self) -> None:
        for provider in ("openrouter", "tavily", "agnes-ai", "glm"):
            self.assertEqual(
                _canonical(_task_pattern(_preset_task(provider))),
                _canonical(_task_pattern(_example_task(provider))),
                provider,
            )


class TestDorkComposition(unittest.TestCase):
    def test_size_triplets_present(self) -> None:
        expected = {
            "tavily": ['"tvly-" extension:env', '"tvly-" extension:json'],
            "nvidia": ['"nvapi-" extension:env', '"nvapi-" extension:json'],
            "serpapi": ['"SERPAPI_API_KEY" extension:env'],
            "ollama": ['"OLLAMA_API_KEY" extension:env'],
        }
        for provider, bases in expected.items():
            queries = _queries(_example_task(provider))
            for base in bases:
                for size in ("size:1..1000", "size:1000..10000", "size:>10000"):
                    self.assertIn(f"{base} {size}", queries, f"{provider} {base} {size}")

    def test_prose_md_dorks_removed(self) -> None:
        for provider in ("tavily", "nvidia"):
            queries = _queries(_example_task(provider))
            self.assertNotIn('"tvly-" extension:md', queries)
            self.assertNotIn('"nvapi-" extension:md', queries)

    def test_opencode_broad_dorks_removed(self) -> None:
        queries = _queries(_example_task("opencode"))
        for dropped in ('"opencode.ai" "api_key"', '"opencode.ai/zen"',
                        '"opencode.ai" "Authorization"'):
            self.assertNotIn(dropped, queries)
        self.assertIn('"OPENCODE_API_KEY" extension:env', queries)
        self.assertIn('"OPENCODE_API_KEY" extension:yaml', queries)

    def test_openrouter_class_dorks_added(self) -> None:
        queries = _queries(_example_task("openrouter"))
        for added in ('"sk-or-v1" extension:env', '"sk-or-v1" extension:yaml',
                      '"OPENROUTER_API_KEY" extension:env'):
            self.assertIn(added, queries)

    def test_brave_dork5_filename_form(self) -> None:
        queries = _queries(_example_task("brave"))
        self.assertIn('"brave" "subscription token" filename:.env', queries)
        self.assertNotIn('"brave" "subscription token" extension:env', queries)
        self.assertIn('"BRAVE_SEARCH_API_KEY" extension:env', queries)
        self.assertIn('"BRAVE_SEARCH_API_KEY" extension:yaml', queries)

    def test_glm_presets_redaction_charset_widened(self) -> None:
        from tools.patterns import COMPILED_API_KEY_PATTERNS

        glm_rx = None
        for pattern in COMPILED_API_KEY_PATTERNS:
            if "[0-9a-f]{32}" in pattern.pattern:
                glm_rx = pattern
                break
        assert glm_rx is not None
        with_marks = _HEX32 + "." + "".join(("aB3_", "E5fG", "7h-9", "jK1l"))
        self.assertTrue(glm_rx.search(with_marks))


if __name__ == "__main__":
    unittest.main()
