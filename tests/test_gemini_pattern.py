"""Behavioral tests for the Gemini key pattern (locked to the defaults preset).

Wave-0 (2026-10-29, `.omo/evidence/magpie-coverage/wave0-us.md` §(c)) measured
the AIzaSy corpus shape: 28 unique samples, 27 of width 39 (``AIza`` + 35),
``Sy`` at positions 5-6 on 28/28, charset includes ``-`` and ``_``.

Candidate A (``AIza[0-9A-Za-z_-]{35}`` — the pre-existing
``config/defaults.py`` gemini preset) and candidate B
(``AIzaSy[A-Za-z0-9_-]{33}``) matched the SAME 27/28 keys, so B buys no
measurable precision. Per the plan's rule ("only pick B if the measurement
proves A over-matches"), this landing keeps candidate A verbatim: zero diff to
``config/defaults.py``, zero capture groups, lockstep with the preset.

Invariants:
  - the example config carries the preset pattern byte-for-byte (lockstep);
  - ZERO capture groups (house rule for a naked prefix pattern — ``findall``
    returns the whole key, so no context can leak into the candidate);
  - the measured width is 39: a 39-char ``AIza`` value matches, the single
    measured 38-char truncated value does not (dropping a fragment is the
    intended cost, not a defect);
  - the body class keeps ``-`` and ``_`` (measured on 4 and 9 samples);
  - placeholder / prose forms without the ``AIza`` prefix never match, and no
    condition carries an override that would widen extraction past the prefix
    (that is the placeholder guard for a naked prefix pattern).

NOTE: candidate strings are concatenated at runtime so no source line carries a
contiguous secret-shaped literal.
"""

import re
import unittest
from pathlib import Path

import yaml

from config.defaults import get_default_config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLE = "config-gemini.yaml"
_PRESET = "gemini"
# The measured shape decision (wave-0 2026-10-29): candidate A, unchanged.
_MEASURED_PATTERN = "AIza[0-9A-Za-z_-]{35}"


def _body(n: int = 33) -> str:
    """A mixed alnum body of *n* chars (no source-level secret shape)."""
    seed = "aB3d" "E5fG" "7hI9" "jK1l" "M3nO" "5pQ7" "rS9t" "U1vW"
    return (seed * 2)[:n]


def _real_key() -> str:
    # Measured width-39 shape: AIza + Sy + 33 body chars.
    return "AIza" "Sy" + _body(33)


def _example_task() -> dict:
    raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
    return raw["tasks"][0]


def _preset_task() -> dict:
    for task in get_default_config()["tasks"]:
        if task.get("name") == _PRESET:
            return task
    raise AssertionError("gemini preset missing from config.defaults")


def _canonical(pattern: str) -> str:
    """Strip regex-redundant escapes before quotes (sources differ only in that)."""
    return re.sub(r"\\(['\"])", r"\1", pattern)


class TestGeminiKeyPattern(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = {"example": _example_task(), "preset": _preset_task()}
        self.patterns = {
            name: str(task["patterns"]["key_pattern"]) for name, task in self.tasks.items()
        }
        self.regexes = {name: re.compile(p) for name, p in self.patterns.items()}

    def test_zero_capture_groups(self) -> None:
        # House rule: a naked prefix pattern has NO group, so findall returns
        # the whole match (the key itself).
        for name in self.tasks:
            self.assertEqual(self.regexes[name].groups, 0, name)

    def test_pattern_is_candidate_a_measured_shape(self) -> None:
        for name, pattern in self.patterns.items():
            self.assertEqual(_canonical(pattern), _MEASURED_PATTERN, name)

    def test_defaults_examples_lockstep(self) -> None:
        self.assertEqual(
            {_canonical(p) for p in self.patterns.values()},
            {_canonical(self.patterns["preset"])},
            self.patterns,
        )
        key = _real_key()
        probes = (
            "GEMINI_API_KEY=" + key,
            'GEMINI_API_KEY="' + key + '"',
            '{"apiKey": "' + key + '"}',
            "https://generativelanguage.googleapis.com/v1beta?key=" + key,
            "x-goog-api-key: " + key,
            "AIzaSy" + _body(20),
            "your_gemini_api_key",
        )
        for name in self.tasks:
            for probe in probes:
                self.assertEqual(
                    self.regexes[name].findall(probe),
                    self.regexes["preset"].findall(probe),
                    f"{name} {probe}",
                )

    def test_measured_width_39_matches(self) -> None:
        key = _real_key()
        self.assertEqual(len(key), 39)
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall("GEMINI_API_KEY=" + key), [key], name)
            self.assertEqual(rx.findall('"' + key + '"'), [key], name)

    def test_charset_keeps_dash_and_underscore(self) -> None:
        # Measured: 4/28 samples carried '-', 9/28 carried '_'.
        for body in ("a-b_c" + _body(28), _body(20) + "-_-" + _body(10)):
            key = "AIza" + "Sy" + body
            self.assertEqual(len(key), 39, key)
            for name, rx in self.regexes.items():
                self.assertEqual(rx.findall(key), [key], name)

    def test_measured_38_width_fragment_does_not_match(self) -> None:
        # The single 38-char outlier (a truncated .env value) is below the
        # width floor: both candidates miss it, and it is not a new shape.
        short = "AIza" + "Sy" + _body(32)
        self.assertEqual(len(short), 38)
        for name, rx in self.regexes.items():
            self.assertEqual(rx.findall(short), [], name)

    def test_placeholder_and_prose_forms_never_match(self) -> None:
        for name, rx in self.regexes.items():
            for probe in (
                "your_gemini_api_key_here",
                "GEMINI_API_KEY=YOUR_KEY",
                "gemini api key",
                "AIza",
                "AIzaSy" + _body(5),
                "123456789012-abcdefghijklmnopqrstuvwxyz.apps.googleusercontent.com",
            ):
                self.assertEqual(rx.findall(probe), [], f"{name} {probe}")

    def test_conditions_do_not_widen_past_the_prefix(self) -> None:
        # Placeholder guard for a naked prefix pattern: every search condition
        # must inherit the prefix-anchored task pattern (no per-condition
        # override), and the example's dork set must carry the env anchor and
        # the domain anchor the brief requires.
        for name, task in self.tasks.items():
            for condition in task["conditions"]:
                self.assertNotIn("patterns", condition, f"{name} override on {condition['query']}")
        joined = " ".join(str(c["query"]) for c in self.tasks["example"]["conditions"])
        self.assertIn("GEMINI_API_KEY", joined)
        self.assertIn("generativelanguage.googleapis.com", joined)

    def test_example_operational_profile(self) -> None:
        task = self.tasks["example"]
        self.assertEqual(task["name"], "gemini")
        self.assertEqual(task["provider_type"], "gemini")
        self.assertTrue(task["enabled"])
        self.assertEqual(task["search_types"], ["code"])
        self.assertEqual(task["api"]["base_url"], "https://generativelanguage.googleapis.com")
        self.assertEqual(task["api"]["model_path"], "/v1beta/models")
        self.assertTrue(task["api"]["use_proxy"])
        self.assertEqual(task["api"]["default_model"], "gemini-3.5-flash")
        self.assertEqual(task["rate_limit"]["base_rate"], 0.2)
        self.assertEqual(task["rate_limit"]["burst_limit"], 2)

    def test_global_corpus_bound(self) -> None:
        raw = yaml.safe_load((_REPO_ROOT / "examples" / _EXAMPLE).read_text(encoding="utf-8"))
        self.assertEqual(raw["global"]["max_links_per_run"], 9000)


if __name__ == "__main__":
    unittest.main()
