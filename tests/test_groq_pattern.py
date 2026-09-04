"""Behavioral tests for the groq key pattern (honeypot decoy exclusion).

Pins the invariants behind the 2026-09-04 fnos fix:
  - decoy keys embed base64("XgroqX") == "WGdyb3FY"; the negative lookahead
    on "WGdy" must reject full decoys AND mid-marker truncations;
  - alnum-only charset drops GTK gsk_* symbols / doc placeholders;
  - length floor stays 20+ (length-independent) — the serpapi lesson: no
    length narrowing without a measured real-key corpus;
  - no capturing groups: re.findall must return full matches, not group
    contents;
  - examples/config-groq.yaml and the defaults preset stay in lockstep.

NOTE: all candidate strings are built by concatenation at runtime — single
source lines must never contain a contiguous "gsk_"-prefixed long-alnum
literal, or GitHub push-protection's Groq key pattern blocks the push.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

# Honeypot shape: 22 random chars + base64("XgroqX") marker + 18 random chars
_DECOY_FULL = "gsk_" + "ABCDEFGHIJKLMNOPQRSTUV" + "WGdyb3FY" + "abcdefghijklmnopqr"
_DECOY_TRUNCATED = "gsk_" + "C" * 20 + "WGdy"  # fragment cut mid-marker
_CLEAN_KEY = "gsk_" + "AbCdEfGh1234567890" + "KLMNOPQRSTuvwxyzabcdefgh"

_PREFIX = "gsk_"


def _preset_pattern() -> str:
    for task in get_default_config()["tasks"]:
        if task.get("name") == "groq":
            return str(task["patterns"]["key_pattern"])
    raise AssertionError("groq preset missing from config.defaults")


def _example_pattern() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load(
        (repo_root / "examples" / "config-groq.yaml").read_text(encoding="utf-8")
    )
    return str(raw["tasks"][0]["patterns"]["key_pattern"])


class TestGroqKeyPattern(unittest.TestCase):
    def setUp(self) -> None:
        self.pattern = _preset_pattern()
        self.re = re.compile(self.pattern)

    def test_matches_clean_keys(self) -> None:
        self.assertTrue(self.re.search(_CLEAN_KEY))
        self.assertTrue(self.re.search(_PREFIX + "A" * 20))

    def test_rejects_full_decoy(self) -> None:
        self.assertIsNone(self.re.search(_DECOY_FULL))

    def test_rejects_truncated_decoy_fragment(self) -> None:
        self.assertIsNone(self.re.search(_DECOY_TRUNCATED))

    def test_rejects_underscore_symbols_and_placeholders(self) -> None:
        self.assertIsNone(self.re.search("gsk_transform_translate"))
        self.assertIsNone(self.re.search("gsk_your_actual_key_here"))

    def test_keeps_length_independent_minimum(self) -> None:
        self.assertIsNone(self.re.search(_PREFIX + "A" * 19))
        self.assertTrue(self.re.search(_PREFIX + "A" * 20))

    def test_no_capture_groups(self) -> None:
        self.assertEqual(self.re.groups, 0)

    def test_example_yaml_matches_preset(self) -> None:
        self.assertEqual(_example_pattern(), self.pattern)


if __name__ == "__main__":
    unittest.main()