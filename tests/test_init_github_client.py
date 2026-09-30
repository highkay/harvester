#!/usr/bin/env python3

"""Unit tests for idempotent ``search.client.init_github_client``.

Every web scan constructs a Pipeline, which calls
``init_github_client(config.ratelimits)``. Before this fix that rebuilt the
process-wide ``GitHubClient`` (and its ``RateLimiter`` token buckets) on EVERY
construction, resetting the shared limiter state under all in-flight scans and
making limiter waits non-monotonic (the bucket a thread waited on could be
replaced mid-wait).

The contract under test: a call with a limits mapping content-equal to the one
the current client was built from is a truthful no-op; a changed mapping
rebuilds; concurrent equal calls build exactly once.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

import search.client as client
from core.models import RateLimitConfig


def _make_limits() -> dict:
    return {
        "github_api": RateLimitConfig(base_rate=0.15, burst_limit=3, adaptive=True),
        "github_web": RateLimitConfig(base_rate=0.5, burst_limit=2, adaptive=True),
    }


def _init_log_calls(info_mock: mock.MagicMock) -> int:
    """Count the specific 'initialized' INFO line, ignoring RateLimiter's own."""
    return sum(
        1
        for call in info_mock.call_args_list
        if call.args and "GitHub client initialized" in str(call.args[0])
    )


class TestInitGithubClientIdempotent(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_client = client._github_client
        self._orig_limits = getattr(client, "_github_client_limits", None)
        client._github_client = None
        client._github_client_limits = None

    def tearDown(self) -> None:
        # Restore module-global state so other suites are unaffected.
        client._github_client = self._orig_client
        client._github_client_limits = self._orig_limits

    def test_first_call_builds_client(self):
        self.assertIsNone(client._github_client)

        client.init_github_client(_make_limits())

        built = client._github_client
        self.assertIsNotNone(built)
        self.assertIs(client.get_github_client(), built)

    def test_equal_limits_reuses_same_instance(self):
        client.init_github_client(_make_limits())
        first = client._github_client

        # A content-equal but distinct mapping (new dict, new value objects)
        # must NOT rebuild the shared client / limiter.
        client.init_github_client(_make_limits())

        self.assertIs(client._github_client, first)

    def test_changed_limits_rebuilds(self):
        client.init_github_client(_make_limits())
        first = client._github_client

        changed = _make_limits()
        changed["github_api"] = RateLimitConfig(base_rate=0.9, burst_limit=1, adaptive=False)
        client.init_github_client(changed)

        self.assertIsNot(client._github_client, first)

    def test_initialized_log_only_on_real_build(self):
        with mock.patch.object(client.logger, "info") as info:
            client.init_github_client(_make_limits())
            self.assertEqual(_init_log_calls(info), 1)

            client.init_github_client(_make_limits())
            self.assertEqual(_init_log_calls(info), 1)

            changed = _make_limits()
            changed["github_web"] = RateLimitConfig(base_rate=0.9, burst_limit=1, adaptive=False)
            client.init_github_client(changed)
            self.assertEqual(_init_log_calls(info), 2)

    def test_concurrent_equal_limits_builds_once(self):
        build_count = {"n": 0}
        real_limiter = client.RateLimiter

        def counting_limiter(limits):
            build_count["n"] += 1
            time.sleep(0.05)  # widen the window so an unguarded impl races
            return real_limiter(limits)

        barrier = threading.Barrier(2)
        results: list = []

        def worker() -> None:
            barrier.wait()
            client.init_github_client(_make_limits())
            results.append(client._github_client)

        with mock.patch.object(client, "RateLimiter", counting_limiter):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(build_count["n"], 1)
        self.assertIs(results[0], results[1])