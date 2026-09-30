#!/usr/bin/env python3

"""Regression tests for the gather-stage GitHub blob -> raw URL rewrite.

Measured on production 2026-09-23: the gather stage fetched the GitHub HTML
blob page for every link (268,952 bytes, 444 backslash-escaped quotes because
the file body sits JSON-escaped inside the page's ``rawLines`` payload) while
the equivalent raw file is 3,159 bytes with plain quotes — an 85x bandwidth
amplification, and the quote-anchored context patterns shipped in the
hardening pass (101 of 125 ``key_pattern``s carry a ``["']``-style anchor)
cannot match a quoted assignment in the escaped blob text.

These tests pin:
1. ``github_blob_to_raw`` maps only ``https://github.com/<o>/<r>/blob/<ref>/<path>``
   to ``https://raw.githubusercontent.com/<o>/<r>/<ref>/<path>``. The URL is
   parsed STRUCTURALLY (prefix + regex), not via ``urlsplit``: a ``#`` that is
   genuinely part of the path (directory ``C#Code/``, AutoSDK snapshot
   filenames like ``_#G.Models….verified.cs`` — production false-404 class B,
   ulw-20260930 g1) must NOT be treated as a fragment. Only a trailing line
   anchor ``#L<num>`` / ``#L<num>-L<num>`` is stripped; every other ``#``
   stays path content and is percent-encoded (``%23``) for the wire, while
   pre-existing valid escapes round-trip unchanged (``%23`` never becomes
   ``%2523``). Everything else — non-github URLs, huggingface ``resolve``
   URLs, issue/commit pages, query-string URLs, malformed input — returns
   byte-identical, and ``links.txt`` / dedup keep the ORIGINAL blob URL.
2. ``AcquisitionStage._acquisition_worker`` fetches the raw URL with a real
   User-Agent, while ``links.txt`` / dedup keying keep the ORIGINAL blob URL.
3. The shipped task patterns from ``examples/config-deepseek.yaml`` and
   ``examples/config-kimi.yaml`` extract a quoted assignment from the RAW file
   body and do NOT extract it from the blob-escaped (``\\"``) form.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Optional, Tuple
from unittest import mock

import yaml

from config.schemas import StageConfig, TaskConfig
from core.models import AcquisitionTask, Service
from search import client
from stage.base import StageOutput, StageResources
from stage.definition import AcquisitionStage, github_blob_to_raw
from tools.utils import encoding_url

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestGithubBlobToRaw(unittest.TestCase):
    """Unit tests for the pure URL mapping helper."""

    def test_blob_url_maps_to_raw(self):
        # Given a canonical blob URL
        url = "https://github.com/owner/repo/blob/abc123def/config/app.env"
        # When mapped
        # Then it becomes the raw.githubusercontent.com equivalent
        self.assertEqual(
            github_blob_to_raw(url),
            "https://raw.githubusercontent.com/owner/repo/abc123def/config/app.env",
        )

    def test_line_fragments_are_stripped(self):
        # Given blob URLs with #L12 / #L12-L40 fragments
        url = "https://github.com/owner/repo/blob/main/src/app.py#L12-L40"
        # When mapped
        result = github_blob_to_raw(url)
        # Then the fragment never reaches the request
        self.assertEqual(result, "https://raw.githubusercontent.com/owner/repo/main/src/app.py")
        self.assertNotIn("#", result)
        single = github_blob_to_raw("https://github.com/owner/repo/blob/main/src/app.py#L12")
        self.assertEqual(single, "https://raw.githubusercontent.com/owner/repo/main/src/app.py")

    def test_percent_encoded_path_is_decoded(self):
        # Given a percent-encoded blob path
        url = "https://github.com/owner/repo/blob/main/dir%20one/app%40.env"
        # When mapped
        # Then the raw path is percent-decoded
        self.assertEqual(
            github_blob_to_raw(url),
            "https://raw.githubusercontent.com/owner/repo/main/dir one/app@.env",
        )

    def test_raw_hash_in_path_is_encoded_not_treated_as_fragment(self):
        # Given a blob URL whose PATH genuinely contains '#' (directory "C#Code/"
        # — production evidence ulw-20260930 g1 CLASS B, chadesiel999/denoise-all)
        url = "https://github.com/o/r/blob/main/C#Code/f.cs"
        # When mapped
        # Then '#' stays path content, percent-encoded for the wire; the old
        # urlsplit() implementation truncated the path to ".../C" (false 404)
        self.assertEqual(
            github_blob_to_raw(url),
            "https://raw.githubusercontent.com/o/r/main/C%23Code/f.cs",
        )

    def test_mid_path_hash_segment_autosdk_shape(self):
        # Given the AutoSDK snapshot filename shape ('#' mid-path, after a dir)
        url = (
            "https://github.com/tryAGI/AutoSDK/blob/58008900abcdef0123/"
            "src/tests/Snapshots/NewtonsoftJson/_#G.Models.DeepSeekModelModel.g.verified.cs"
        )
        # When mapped
        # Then the whole filename survives with '#' encoded (was: truncated to "_")
        self.assertEqual(
            github_blob_to_raw(url),
            "https://raw.githubusercontent.com/tryAGI/AutoSDK/58008900abcdef0123/"
            "src/tests/Snapshots/NewtonsoftJson/_%23G.Models.DeepSeekModelModel.g.verified.cs",
        )

    def test_pre_encoded_hash_survives_not_double_encoded(self):
        # Given a blob path already carrying the valid escape %23
        url = "https://github.com/o/r/blob/main/C%23Code/f.cs"
        # When mapped
        # Then %23 round-trips unchanged (no decode-to-'#', no %2523)
        self.assertEqual(
            github_blob_to_raw(url),
            "https://raw.githubusercontent.com/o/r/main/C%23Code/f.cs",
        )

    def test_line_anchor_range_stripped(self):
        # Given a trailing #L10-L20 line anchor
        url = "https://github.com/owner/repo/blob/main/src/app.py#L10-L20"
        # When mapped
        result = github_blob_to_raw(url)
        # Then only the anchor is removed
        self.assertEqual(result, "https://raw.githubusercontent.com/owner/repo/main/src/app.py")
        self.assertNotIn("#", result)

    def test_non_line_anchor_hashes_stay_path_content(self):
        # Given trailing '#' fragments that are NOT GitHub line anchors
        cases = {
            "https://github.com/o/r/blob/main/app.py#Lfoo": "https://raw.githubusercontent.com/o/r/main/app.py%23Lfoo",
            "https://github.com/o/r/blob/main/app.py#L12x": "https://raw.githubusercontent.com/o/r/main/app.py%23L12x",
            "https://github.com/o/r/blob/main/README.md#section": "https://raw.githubusercontent.com/o/r/main/README.md%23section",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                # When mapped
                # Then the '#' is ambiguous-but-not-a-line-anchor => path content, encoded
                self.assertEqual(github_blob_to_raw(url), expected)

    def test_combined_cjk_dir_hash_segment_and_line_anchor(self):
        # Given a CJK directory + raw-'#' segment + a trailing line anchor
        url = "https://github.com/o/r/blob/main/代码/C#Code/f.cs#L5"
        # When mapped
        # Then the anchor is stripped, '#' -> %23, CJK -> UTF-8 percent-escapes
        self.assertEqual(
            github_blob_to_raw(url),
            "https://raw.githubusercontent.com/o/r/main/%E4%BB%A3%E7%A0%81/C%23Code/f.cs",
        )

    def test_hash_bearing_non_blob_and_query_urls_unchanged(self):
        # Given URLs that carry '#' but must stay byte-identical
        urls = [
            "https://example.com/a/b#C/x",  # non-github
            "https://huggingface.co/datasets/o/n/resolve/main/C#Code/f.env",  # hf resolve
            "https://github.com/o/r/blob/main/app.env?plain=1#L5",  # query string wins
            "https://github.com/o/r/issues/5#C1",  # non-blob github page
            "not-a-url#C",  # malformed
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(github_blob_to_raw(url), url)

    def test_trailing_whitespace_is_tolerated(self):
        # Given a blob URL with a trailing newline / spaces (links.txt shape)
        url = "https://github.com/owner/repo/blob/main/app.env\n"
        padded = "  https://github.com/owner/repo/blob/main/app.env  "
        # When mapped
        expected = "https://raw.githubusercontent.com/owner/repo/main/app.env"
        # Then the raw URL carries no whitespace
        self.assertEqual(github_blob_to_raw(url), expected)
        self.assertEqual(github_blob_to_raw(padded), expected)

    def test_non_blob_github_urls_unchanged(self):
        # Given issue / commit / tree / repo pages (rendered HTML with literal quotes)
        urls = [
            "https://github.com/owner/repo/issues/5#issuecomment-1",
            "https://github.com/owner/repo/commit/abc123",
            "https://github.com/owner/repo/tree/main/config",
            "https://github.com/owner/repo",
            "https://github.com/owner/repo/blob",
            "https://api.github.com/repos/owner/repo/contents/app.env",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(github_blob_to_raw(url), url)

    def test_blob_url_with_query_string_unchanged(self):
        # Given a blob URL carrying a query string (?plain=1)
        url = "https://github.com/owner/repo/blob/main/app.env?plain=1"
        # When mapped
        # Then it is returned byte-identical
        self.assertEqual(github_blob_to_raw(url), url)

    def test_huggingface_resolve_urls_unchanged(self):
        # Given an HF resolve URL (already the raw form for the hf backend)
        url = "https://huggingface.co/datasets/owner/name/resolve/main/data/app.env"
        self.assertEqual(github_blob_to_raw(url), url)

    def test_plain_urls_unchanged(self):
        urls = [
            "https://example.com/some/page",
            "https://raw.githubusercontent.com/owner/repo/main/app.env",
            "http://github.com/owner/repo/blob/main/app.env",  # http, not https
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(github_blob_to_raw(url), url)

    def test_malformed_urls_unchanged(self):
        urls = [
            "",
            "not-a-url",
            "https://github.com/owner/repo/blob/main/",  # empty path after ref
            "https://github.com/owner/repo/blob/",  # no ref/path
            "https://github.com/[::1/blob/main/x",  # urlsplit ValueError
        ]
        for url in urls:
            with self.subTest(url=repr(url)):
                self.assertEqual(github_blob_to_raw(url), url)


class _Auth:
    """Minimal IAuthProvider stub (the acquisition worker needs no credentials)."""

    def get_session(self) -> Optional[str]:
        return ""

    def get_token(self) -> Optional[str]:
        return "token"

    def get_credential(self, prefer_token: bool = True) -> Tuple[str, str]:
        return "token", "api"

    def get_user_agent(self) -> str:
        return "test-agent"


class _RecordingHandler:
    def __init__(self) -> None:
        self.outputs = []

    def __call__(self, output) -> None:  # noqa: ANN001 - matches OutputHandler shape
        self.outputs.append(output)


def _make_stage() -> AcquisitionStage:
    resources = StageResources(
        limiter=mock.MagicMock(),
        providers={},
        config=mock.MagicMock(),
        task_configs={"deepseek": TaskConfig(name="deepseek", provider_type="deepseek", stages=StageConfig())},
        auth=_Auth(),
    )
    return AcquisitionStage(resources, handler=_RecordingHandler())


class TestAcquisitionWorkerFetchTarget(unittest.TestCase):
    """The worker fetches the raw URL with a real UA but keys links by the blob URL."""

    def _run(self, url: str) -> Tuple[StageOutput, dict]:
        stage = _make_stage()
        task = AcquisitionTask(provider="deepseek", url=url, key_pattern="sk-x")
        with mock.patch.object(client, "collect", return_value=[]) as collect_mock, mock.patch(
            "stage.definition.get_user_agent", return_value="test-agent"
        ):
            output = stage._acquisition_worker(task)
        self.assertIsNotNone(output)
        _, kwargs = collect_mock.call_args
        assert output is not None
        return output, kwargs

    def test_blob_link_is_fetched_as_raw_with_user_agent(self):
        # Given a task whose link is a GitHub blob page
        blob = "https://github.com/owner/repo/blob/abc123def/config/app.env"
        # When the acquisition worker runs
        _output, kwargs = self._run(blob)
        # Then collect fetches the RAW url, with a real browser User-Agent
        self.assertEqual(kwargs["url"], "https://raw.githubusercontent.com/owner/repo/abc123def/config/app.env")
        self.assertEqual(kwargs["headers"]["User-Agent"], "test-agent")
        self.assertIn("Accept", kwargs["headers"])

    def test_non_github_link_is_fetched_unchanged(self):
        # Given a task whose link is not a GitHub blob page
        url = "https://example.com/notes/env.txt"
        # When the acquisition worker runs
        _output, kwargs = self._run(url)
        # Then the fetch target is byte-identical
        self.assertEqual(kwargs["url"], url)

    def test_links_output_keeps_original_blob_url(self):
        # Given a task whose link is a GitHub blob page
        blob = "https://github.com/owner/repo/blob/main/app.env"
        # When the acquisition worker runs
        output, _kwargs = self._run(blob)
        # Then links.txt / dedup keep the ORIGINAL blob URL (only the fetch changes)
        self.assertEqual(output.links, [("deepseek", [blob])])

    def test_dedup_id_keeps_original_blob_url(self):
        # Given a stage and a blob-link task
        stage = _make_stage()
        task = AcquisitionTask(provider="deepseek", url="https://github.com/owner/repo/blob/main/app.env")
        # When the dedup id is generated
        # Then it is keyed on the original blob URL so link-index dedup is stable
        self.assertEqual(stage._generate_id(task), f"gather:deepseek:{task.url}")


def _load_task_pattern(config_relpath: str) -> str:
    with open(REPO_ROOT / config_relpath, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return str(data["tasks"][0]["patterns"]["key_pattern"])


def _quoted_assignment_lines(env_name: str, key: str) -> Tuple[str, str]:
    """Return (raw_file_text, blob_escaped_text) for ``ENV="<key>"``.

    The blob form mirrors the measured production HTML: the file body sits
    JSON-escaped inside ``rawLines``, so every quote arrives as ``\\"``.
    """
    raw = f'{env_name}="{key}"\n'
    escaped = f'{env_name}=\\"{key}\\"\n'
    return raw, escaped


class TestExtractionInvariant(unittest.TestCase):
    """The shipped anchored patterns match RAW file bodies, not blob-escaped text."""

    def test_deepseek_pattern_matches_quoted_assignment_in_raw_body(self):
        # Given the shipped deepseek task pattern and a quoted .env line (RAW form)
        pattern = _load_task_pattern("examples/config-deepseek.yaml")
        key = "sk-" + "0123456789abcdef" * 2  # synthetic sk- + 32 hex (measured shape)
        raw, escaped = _quoted_assignment_lines("DEEPSEEK_API_KEY", key)
        # When extraction runs over the raw body
        services = client.collect(key_pattern=pattern, text=raw)
        # Then the key is recovered
        self.assertIn(key, {s.key for s in services})
        # And the blob-escaped form of the same line yields nothing (the defect)
        self.assertEqual(_found_keys(pattern, escaped), set())

    def test_kimi_pattern_matches_quoted_assignment_in_raw_body(self):
        # Given the shipped kimi task pattern and a quoted MOONSHOT_API_KEY line
        pattern = _load_task_pattern("examples/config-kimi.yaml")
        key = "sk-" + "Ab12Cd34" * 6  # synthetic sk- + 48 alnum (measured shape)
        raw, escaped = _quoted_assignment_lines("MOONSHOT_API_KEY", key)
        # When extraction runs over the raw body
        services = client.collect(key_pattern=pattern, text=raw)
        # Then the key is recovered
        self.assertIn(key, {s.key for s in services})
        # And the blob-escaped form yields nothing
        self.assertEqual(_found_keys(pattern, escaped), set())


def _found_keys(pattern: str, text: str) -> set[str]:
    services: list[Service] = client.collect(key_pattern=pattern, text=text)
    return {s.key for s in services}


class TestBlobToRawToWireChain(unittest.TestCase):
    """End-to-end pin: blob URL -> ``github_blob_to_raw`` -> ``encoding_url``.

    The two false-404 mechanisms fixed on 2026-09-30 compose at gather time:
    F2 keeps a path-embedded ``#`` as path content, and F1 (``encoding_url``)
    percent-encodes non-ASCII instead of punycoding it. If either regresses,
    the wire URL 404s while the file exists (prod proof: ulw-20260930 g1).
    """

    def test_cjk_blob_url_produces_fetchable_wire_url(self):
        # Given a blob URL with a CJK directory (prod class-A shape)
        blob = "https://github.com/o/r/blob/main/code/第四章/4.6.ipynb"
        # When mapped and wire-encoded
        raw = github_blob_to_raw(blob)
        wire = encoding_url(raw)
        # Then the path carries UTF-8 escapes, never punycode, and the chain
        # is idempotent (encoding_url must not double-encode F2's output)
        self.assertNotIn("xn--", wire)
        self.assertIn("%E7%AC%AC%E5%9B%9B%E7%AB%A0", wire)
        self.assertEqual(wire, encoding_url(wire))
        self.assertTrue(wire.startswith("https://raw.githubusercontent.com/o/r/main/"))

    def test_cjk_dir_and_hash_segment_and_anchor_compose(self):
        # Given a blob URL with CJK + a literal '#' path segment + a line anchor
        blob = "https://github.com/o/r/blob/main/代码/C#Code/f.cs#L5"
        # When mapped and wire-encoded
        wire = encoding_url(github_blob_to_raw(blob))
        # Then '#' survives as %23 (never truncated), CJK is %-escaped,
        # the line anchor is stripped, and nothing is double-encoded
        self.assertEqual(
            wire,
            "https://raw.githubusercontent.com/o/r/main/%E4%BB%A3%E7%A0%81/C%23Code/f.cs",
        )
        self.assertEqual(wire, encoding_url(wire))


if __name__ == "__main__":
    unittest.main()
