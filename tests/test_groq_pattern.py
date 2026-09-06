"""Behavioral tests for the groq key pattern (decoy + placeholder exclusion).

Pins the invariants behind the 2026-09-04 egress fix and the 2026-09-06
placeholder audit:
  - decoy keys embed base64("XgroqX") == "WGdyb3FY"; the negative lookahead
    on "WGdy" must reject full decoys AND mid-marker truncations;
  - doc placeholders (abc123-style sequences, marker words, 4-char runs)
    are rejected at extraction time — 2026-09-06 prod measured them as
    100% of the groq check pool;
  - alnum-only charset drops GTK gsk_* symbols / underscore placeholders;
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
_CLEAN_KEY = "gsk_" + "Zq7Vb3Nk9Rd1Tf5Gx2Hj8Mp4Ws"
_CLEAN_20 = "gsk_" + "Zq7Vb3Nk9Rd1Tf5Gx2Hj"
_CLEAN_19 = "gsk_" + "Zq7Vb3Nk9Rd1Tf5Gx2H"



# Doc placeholders observed in the 2026-09-06 fnos prod pool (split literals
# so no contiguous secret-shaped string exists in source).
_PLACEHOLDERS = [
    "gsk_" + "abc123xyz456def789ghi012jkl345mno678pqr901stu234",
    "gsk_" + "1234567890abcdef1234567890abcdef1234567890abcdef",
    "gsk_" + "abcdefghijklmnopqrstuvwxyz01",
    "gsk_" + "test1234567890123456",
    "gsk_" + "secretKeyString123456",
    "gsk_" + "xxxxxxxxxxLOCALHOSTXXXXXXXXXXXX",
    "gsk_" + "A" * 40,
    "gsk_" + "B" * 40,
    "gsk_" + "x" * 30,
]


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
        self.assertTrue(self.re.search(_CLEAN_20))

    def test_rejects_full_decoy(self) -> None:
        self.assertIsNone(self.re.search(_DECOY_FULL))

    def test_rejects_truncated_decoy_fragment(self) -> None:
        self.assertIsNone(self.re.search(_DECOY_TRUNCATED))

    def test_rejects_underscore_symbols_and_placeholders(self) -> None:
        self.assertIsNone(self.re.search("gsk_transform_translate"))
        self.assertIsNone(self.re.search("gsk_your_actual_key_here"))

    def test_rejects_doc_placeholders(self) -> None:
        for candidate in _PLACEHOLDERS:
            self.assertIsNone(self.re.search(candidate), candidate)

    def test_keeps_length_independent_minimum(self) -> None:
        self.assertIsNone(self.re.search(_CLEAN_19))
        self.assertTrue(self.re.search(_CLEAN_20))

    def test_no_capture_groups(self) -> None:
        self.assertEqual(self.re.groups, 0)

    def test_example_yaml_matches_preset(self) -> None:
        self.assertEqual(_example_pattern(), self.pattern)


if __name__ == "__main__":
    unittest.main()