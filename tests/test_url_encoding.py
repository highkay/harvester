#!/usr/bin/env python3

"""Regression tests for tools.utils.encoding_url URL wire encoding.

Measured on production 2026-09-30 (48h window, .omo/evidence/ulw-20260930-rootfix/g1):
96.3% of 3484 ``File not found (HTTP 404)`` lines were FALSE 404s — the old
``encoding_url`` applied punycode (``xn--…``) to CJK characters in the URL
*path* (``code/第四章/…`` -> ``code/xn--wbs215fqga/…``). Punycode is only
valid for IDNA *hostnames*; GitHub raw 404s the mangled path while the file
exists (toggle proof: punycode URL -> 404, percent-encoded URL -> 200,
30/30 host, 10/10 container).

These tests pin the real contract:
1. Non-ASCII in path/query/fragment is percent-encoded as UTF-8 — never
   punycoded (zero ``xn--`` outside the hostname).
2. Non-ASCII hostnames ARE IDNA-encoded label-wise; userinfo/port survive.
3. Existing valid %-escapes (``%23``, ``%E7%…``) are never double-encoded.
4. The transform is idempotent and byte-identical for well-formed ASCII URLs.
"""

from __future__ import annotations

import unittest

from tools.utils import encoding_url

# Production sample from q1_samples.txt (class A false 404), live-verified 200.
PROD_CJK_PATH = (
    "https://raw.githubusercontent.com/datawhalechina/handy-multi-agent/"
    "44b0e8edc3753f1273e31d965bd9ac1757ca01d0/code/第四章/4.6.ipynb"
)
PROD_CJK_PATH_ENCODED = (
    "https://raw.githubusercontent.com/datawhalechina/handy-multi-agent/"
    "44b0e8edc3753f1273e31d965bd9ac1757ca01d0/code/%E7%AC%AC%E5%9B%9B%E7%AB%A0/4.6.ipynb"
)


class TestEncodingUrlPath(unittest.TestCase):
    """Path-segment encoding: UTF-8 %-escapes, never punycode."""

    def test_cjk_path_is_percent_encoded_with_zero_punycode(self):
        # Given the production sample URL with a CJK path segment
        url = PROD_CJK_PATH
        # When encoded
        result = encoding_url(url)
        # Then the CJK segment is UTF-8 percent-escaped and no punycode appears
        self.assertEqual(result, PROD_CJK_PATH_ENCODED)
        self.assertIn("%E7%AC%AC%E5%9B%9B%E7%AB%A0", result)
        self.assertNotIn("xn--", result)

    def test_encoding_is_idempotent(self):
        # Given an already-encoded (wire) URL
        once = encoding_url(PROD_CJK_PATH)
        # When the output is fed back through encoding_url
        twice = encoding_url(once)
        # Then the result is byte-identical (and still punycode-free)
        self.assertEqual(twice, once)
        self.assertNotIn("xn--", twice)

    def test_existing_escapes_are_never_double_encoded(self):
        # Given a URL carrying both a reserved-char escape and a UTF-8 escape
        url = "https://raw.githubusercontent.com/o/r/sha/dir%231/%E7%AC%AC/file.txt"
        # When encoded
        result = encoding_url(url)
        # Then %23 and %E7%AC%AC survive verbatim — no %25 re-encoding
        self.assertIn("dir%231", result)
        self.assertIn("%E7%AC%AC", result)
        self.assertNotIn("%25", result)

    def test_raw_space_in_path_becomes_percent20(self):
        # Given a leaked file path with a raw space (1325 prod 404 lines had one)
        url = "https://raw.githubusercontent.com/o/r/sha/docs/my notes.txt"
        # When encoded
        result = encoding_url(url)
        # Then the space is %-escaped
        self.assertEqual(
            result, "https://raw.githubusercontent.com/o/r/sha/docs/my%20notes.txt"
        )


class TestEncodingUrlHostname(unittest.TestCase):
    """Hostname encoding: IDNA label-wise, everything else untouched."""

    def test_non_ascii_hostname_is_idna_encoded_with_port_and_path(self):
        # Given a URL with a CJK hostname, port and CJK path
        url = "https://例子.中国:8080/路径/文件.txt"
        # When encoded
        result = encoding_url(url)
        # Then the host is IDNA (xn--) per label, port preserved, path percent-encoded
        self.assertEqual(
            result,
            "https://xn--fsqu00a.xn--fiqs8s:8080/%E8%B7%AF%E5%BE%84/%E6%96%87%E4%BB%B6.txt",
        )


class TestEncodingUrlUnchangedInputs(unittest.TestCase):
    """Pure-ASCII and edge inputs must pass through unharmed."""

    def test_pure_ascii_urls_are_byte_identical(self):
        # Given well-formed ASCII URLs (api.github.com search shape and raw shape)
        urls = (
            "https://api.github.com/search/code?q=%22T3BlbkFJ%22&per_page=100",
            "https://raw.githubusercontent.com/o/re.po/sha/a-b_c~d.py?x=1&y=2#L10",
            "https://api.github.com/user",
        )
        for url in urls:
            with self.subTest(url=url):
                # When encoded
                # Then the output is byte-identical
                self.assertEqual(encoding_url(url), url)

    def test_empty_string_returns_empty_string(self):
        # Given an empty URL
        # When encoded
        # Then an empty string comes back
        self.assertEqual(encoding_url(""), "")


class TestEncodingUrlQuery(unittest.TestCase):
    """Query/fragment encoding."""

    def test_cjk_query_value_is_percent_encoded(self):
        # Given a URL with CJK characters in a query value
        url = "https://example.com/api?q=第四章&n=2"
        # When encoded
        result = encoding_url(url)
        # Then the query value is UTF-8 percent-escaped, structure intact
        self.assertEqual(result, "https://example.com/api?q=%E7%AC%AC%E5%9B%9B%E7%AB%A0&n=2")
        self.assertNotIn("xn--", result)


if __name__ == "__main__":
    unittest.main()
