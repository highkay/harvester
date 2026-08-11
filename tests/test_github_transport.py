#!/usr/bin/env python3

"""Unit tests for ohmygh/gx-inspired GitHub transport helpers."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from search.github.cache import ResponseCache, classify_ttl
from search.github.index import LinkIndex
from search.github.quota import QuotaTracker, resource_from_url
from search.github.transport import (
    BUILTIN_SEEDS,
    EdgePool,
    EdgePoolConfigRuntime,
)
from search.client import (
    _enrich_search_content_for_extract,
    _extract_api_links,
    normalize_search_type,
)


class TestResponseCache(unittest.TestCase):
    def test_roundtrip_and_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResponseCache(tmp, max_entries=5, enabled=True)
            try:
                key = ResponseCache.make_key("GET", "https://api.github.com/search/code?q=a", "tok")
                cache.put(
                    key,
                    "https://api.github.com/search/code?q=a",
                    '{"ok":1}',
                    {"ETag": '"e1"', "X-RateLimit-Remaining": "9"},
                    ttl=60,
                )
                entry = cache.get(key)
                self.assertIsNotNone(entry)
                assert entry is not None
                self.assertTrue(entry.fresh)
                self.assertEqual(entry.body, '{"ok":1}')
                self.assertEqual(entry.etag, '"e1"')
            finally:
                cache.close()

    def test_classify_ttl(self):
        self.assertEqual(classify_ttl("https://api.github.com/search/code?q=x", 60, 300), 60)
        self.assertEqual(classify_ttl("https://api.github.com/rate_limit", 60, 300), 300)


class TestQuotaTracker(unittest.TestCase):
    def test_update_and_remaining(self):
        q = QuotaTracker(enabled=True)
        q.update_from_headers(
            "secret",
            {
                "X-RateLimit-Resource": "search",
                "X-RateLimit-Limit": "30",
                "X-RateLimit-Remaining": "7",
                "X-RateLimit-Reset": str(int(time.time()) + 120),
            },
        )
        self.assertEqual(q.remaining("secret", "search"), 7)
        self.assertEqual(resource_from_url("https://api.github.com/search/code?q=1"), "search")
        self.assertEqual(resource_from_url("https://api.github.com/rate_limit"), "core")


class TestLinkIndex(unittest.TestCase):
    def test_add_filter_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx = LinkIndex(tmp, enabled=True)
            try:
                urls = [
                    "https://github.com/a/b/blob/main/.env",
                    "https://github.com/c/d/issues/1",
                ]
                new1 = idx.add_many(urls, provider="tavily", search_type="code", query='"tvly-"')
                self.assertEqual(new1, 2)
                new2 = idx.add_many(urls, provider="tavily", search_type="code", query='"tvly-"')
                self.assertEqual(new2, 0)
                self.assertEqual(
                    idx.filter_new(urls + ["https://github.com/new"], "tavily"),
                    ["https://github.com/new"],
                )
                hits = idx.search("tvly", provider="tavily")
                self.assertGreaterEqual(len(hits), 1)
                self.assertEqual(idx.stats("tavily")["count"], 2)
            finally:
                idx.close()


class TestEdgePool(unittest.TestCase):
    def test_builtin_pool_and_async_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "edges.json")
            cfg = EdgePoolConfigRuntime(
                enabled=True,
                source="builtin",
                verify=False,
                max_edges=4,
                cache_path=cache_path,
                refresh_interval=3600,
            )
            pool = EdgePool(cfg)
            self.assertGreaterEqual(pool.size, 1)
            ip = pool.next_ip()
            self.assertIsNotNone(ip)
            pool.mark_success(ip or BUILTIN_SEEDS[0], rtt_ms=12.0)
            pool.refresh_async(force=True)
            # Give daemon a moment
            time.sleep(0.2)
            self.assertTrue(os.path.isfile(cache_path) or pool.size >= 1)

    def test_hosts_feed_parse(self):
        payload = {
            "version": 1,
            "domains": {"api.github.com": ["20.27.177.116", "not-an-ip", "20.205.243.168"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = EdgePoolConfigRuntime(
                enabled=True,
                source="http",
                verify=False,
                max_edges=8,
                cache_path=os.path.join(tmp, "e.json"),
            )
            pool = EdgePool(cfg)
            with mock.patch.object(pool, "_fetch_hosts_feed", return_value=payload["domains"]["api.github.com"]):
                n = pool.refresh(force=True)
            self.assertGreaterEqual(n, 2)
            self.assertIn("20.27.177.116", pool.snapshot())


class TestSearchHelpers(unittest.TestCase):
    def test_normalize_search_type(self):
        self.assertEqual(normalize_search_type("ISSUES"), "issues")
        self.assertEqual(normalize_search_type("nope"), "code")

    def test_extract_links_skips_api_urls(self):
        items = [
            {"html_url": "https://github.com/o/r/issues/1"},
            {"url": "https://api.github.com/repos/o/r/issues/1"},
        ]
        links = _extract_api_links(items, "issues")
        self.assertEqual(links, ["https://github.com/o/r/issues/1"])

    def test_enrich_content(self):
        items = [
            {
                "title": "leak",
                "body": "tvly-abc123",
                "text_matches": [{"fragment": "key=tvly-xyz"}],
            }
        ]
        text = _enrich_search_content_for_extract("{}", items, "issues")
        self.assertIn("tvly-abc123", text)
        self.assertIn("tvly-xyz", text)


if __name__ == "__main__":
    unittest.main()
