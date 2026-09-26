#!/usr/bin/env python3

"""Unit tests for search.client egress routing (per-host direct, pool, failover).

Context (production, 2026-09-24): raw.githubusercontent.com answered 8/8 @0.6 s
from the container DIRECT, but 0-1/8 through the WARP socks rotation, while
every gather fetch used the proxied session — and that session's exit is
chosen process-wide by the newest scan. One degraded exit therefore produced
~340k SOCKS failures in 28 h and pinned 15 concurrent runs in 'running'.

These tests pin the three defences against that class:
  * per-host direct routing (raw fetches never take the proxy hop),
  * the direct-host timeout floor and the sized/blocking connection pool,
  * transport-failure failover across the HARVESTER_PROXY rotation.
"""

from __future__ import annotations

import os
import unittest
from typing import Any, cast
from unittest import mock

import requests

import search.client as client

RAW_URL = "https://raw.githubusercontent.com/o/r/abc123/conf/app.env"
API_URL = "https://api.github.com/search/code?q=x&page=2"


class _FakeResponse:
    """Minimal requests.Response stand-in for request()/raise_for_status()."""

    def __init__(self, status_code: int = 200, content: bytes = b"{}"):
        self.status_code = status_code
        self.content = content
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class TestEgressRouting(unittest.TestCase):
    def tearDown(self) -> None:
        client.set_proxy("")
        os.environ.pop("HARVESTER_PROXY", None)

    def test_raw_host_routes_direct_and_api_host_proxied(self) -> None:
        self.assertFalse(client.effective_use_proxy(RAW_URL))
        self.assertTrue(client.effective_use_proxy(API_URL))
        # An explicit use_proxy=False (domestic provider validation) wins.
        self.assertFalse(client.effective_use_proxy(API_URL, use_proxy=False))

    def test_timeout_floor_applies_only_to_direct_hosts(self) -> None:
        self.assertEqual(client.effective_timeout(API_URL, 10), 10)
        self.assertGreaterEqual(
            client.effective_timeout(RAW_URL, 10), client._DIRECT_TIMEOUT_FLOOR
        )

    def test_request_raw_uses_direct_session_with_floored_timeout(self) -> None:
        http_session = mock.MagicMock()
        direct_session = mock.MagicMock()
        direct_session.request.return_value = _FakeResponse()
        with mock.patch.object(client, "_HTTP_SESSION", http_session), mock.patch.object(
            client, "_DIRECT_SESSION", direct_session
        ):
            client.request("GET", RAW_URL, timeout=10)

        http_session.request.assert_not_called()
        self.assertGreaterEqual(
            direct_session.request.call_args.kwargs["timeout"],
            client._DIRECT_TIMEOUT_FLOOR,
        )

    def test_request_api_uses_proxied_session(self) -> None:
        http_session = mock.MagicMock()
        http_session.request.return_value = _FakeResponse()
        direct_session = mock.MagicMock()
        with mock.patch.object(client, "_HTTP_SESSION", http_session), mock.patch.object(
            client, "_DIRECT_SESSION", direct_session
        ):
            client.request("GET", API_URL)

        direct_session.request.assert_not_called()
        http_session.request.assert_called_once()

    def test_new_session_mounts_sized_blocking_pool(self) -> None:
        session = client._new_session()
        for prefix in ("https://", "http://"):
            adapter = session.get_adapter(f"{prefix}example.com")
            self.assertEqual(
                getattr(adapter, "_pool_maxsize", None), client._POOL_MAXSIZE
            )
            self.assertEqual(
                getattr(adapter, "_pool_connections", None), client._POOL_CONNECTIONS
            )
            self.assertTrue(getattr(adapter, "_pool_block", None))

    def test_proxy_rotation_parses_and_trims_env(self) -> None:
        os.environ["HARVESTER_PROXY"] = " socks5://a:1 , socks5://b:2 ,, "
        self.assertEqual(client.proxy_rotation(), ["socks5://a:1", "socks5://b:2"])
        os.environ.pop("HARVESTER_PROXY", None)
        self.assertEqual(client.proxy_rotation(), [])

    def test_mask_proxy_hides_userinfo(self) -> None:
        self.assertEqual(
            client._mask_proxy("http://Default.acct:secret-token@1.2.3.4:2260"),
            "http://1.2.3.4:2260",
        )
        self.assertEqual(client._mask_proxy("socks5://192.168.1.18:1091"), "socks5://192.168.1.18:1091")

    def test_set_proxy_rejects_unknown_scheme(self) -> None:
        with self.assertRaises(ValueError):
            client.set_proxy("ftp://192.168.1.18:1080")


class TestProxyFailover(unittest.TestCase):
    PROXIES = [
        "socks5://10.0.0.1:1080",
        "socks5://10.0.0.2:1080",
        "socks5://10.0.0.3:1080",
    ]

    def setUp(self) -> None:
        os.environ["HARVESTER_PROXY"] = ",".join(self.PROXIES)
        client.set_proxy(self.PROXIES[0])

    def tearDown(self) -> None:
        client.set_proxy("")
        os.environ.pop("HARVESTER_PROXY", None)

    def _request_against(self, session: mock.MagicMock) -> None:
        with mock.patch.object(client, "_HTTP_SESSION", session):
            try:
                client.request("GET", API_URL)
            except Exception:
                pass

    def test_candidates_are_picked_exit_then_rotation(self) -> None:
        self.assertEqual(client.get_egress_state()["candidates"], self.PROXIES)

    def test_three_consecutive_transport_failures_rotate_to_next_exit(self) -> None:
        failing = mock.MagicMock()
        failing.request.side_effect = requests.exceptions.ConnectionError("tunnel down")

        for _ in range(client._PROXY_FAILOVER_THRESHOLD):
            self._request_against(failing)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[1])
        self.assertEqual(state["rotations"], 1)
        self.assertEqual(state["consecutive_failures"], 0)

    def test_success_resets_the_failure_streak(self) -> None:
        failing = mock.MagicMock()
        failing.request.side_effect = requests.exceptions.ConnectionError("tunnel down")
        healthy = mock.MagicMock()
        healthy.request.return_value = _FakeResponse()

        # Two failures, one success, two failures: never three in a row.
        for session, count in ((failing, 2), (healthy, 1), (failing, 2)):
            for _ in range(count):
                self._request_against(session)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(state["rotations"], 0)

    def test_http_error_response_still_counts_as_reachable(self) -> None:
        # A 500 means the exit passed traffic — it must not trigger failover.
        erroring = mock.MagicMock()
        erroring.request.return_value = _FakeResponse(status_code=500)

        for _ in range(client._PROXY_FAILOVER_THRESHOLD + 1):
            self._request_against(erroring)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(state["rotations"], 0)
        self.assertEqual(state["consecutive_failures"], 0)

    def test_single_candidate_never_rotates_but_rearms(self) -> None:
        os.environ["HARVESTER_PROXY"] = self.PROXIES[0]
        client.set_proxy(self.PROXIES[0])

        failing = mock.MagicMock()
        failing.request.side_effect = requests.exceptions.ConnectionError("tunnel down")
        for _ in range(6):
            self._request_against(failing)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(state["rotations"], 0)
        self.assertEqual(state["candidates"], [self.PROXIES[0]])

    def test_set_proxy_resets_streak_and_candidates(self) -> None:
        failing = mock.MagicMock()
        failing.request.side_effect = requests.exceptions.ConnectionError("tunnel down")
        self._request_against(failing)
        self.assertEqual(client.get_egress_state()["consecutive_failures"], 1)

        client.set_proxy(self.PROXIES[2])
        state = client.get_egress_state()
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(state["candidates"][0], self.PROXIES[2])
        self.assertEqual(sorted(state["candidates"]), sorted(self.PROXIES))


class _FakeLimiter:
    """Minimal RateLimiter stand-in for GitHubClient._limit."""

    def __init__(self, grants_on_call: int | None):
        self.calls = 0
        self.grants_on_call = grants_on_call
        self.waits: list[float] = []

    def acquire(self, service: str, tokens: int = 1) -> bool:
        self.calls += 1
        return self.grants_on_call is not None and self.calls >= self.grants_on_call

    def wait_time(self, service: str, tokens: int = 1) -> float:
        self.waits.append(0.01)
        return 0.01

    def _get_bucket(self, service: str):
        return None


class TestLimiterDenialBudget(unittest.TestCase):
    """The process-wide github_api bucket must be waited on, not one-shot denied.

    Measured 2026-09-26 08:00: the openrouter run (ONE condition) lost its only
    search task to three consecutive one-shot denials and "completed" with
    0 links; a denial must now follow bounded waiting.
    """

    def test_immediate_grant_does_not_wait(self) -> None:
        limiter = _FakeLimiter(grants_on_call=1)
        gh = client.GitHubClient(limiter=cast(Any, limiter))

        self.assertTrue(gh._limit("github_api"))
        self.assertEqual([], limiter.waits)

    def test_grant_after_a_wait_succeeds(self) -> None:
        limiter = _FakeLimiter(grants_on_call=3)
        gh = client.GitHubClient(limiter=cast(Any, limiter))

        with mock.patch.object(client.time, "sleep"):
            self.assertTrue(gh._limit("github_api"))

        self.assertLessEqual(len(limiter.waits), client._LIMIT_MAX_WAIT_ROUNDS)

    def test_exhausted_wait_denies_after_bounded_rounds(self) -> None:
        limiter = _FakeLimiter(grants_on_call=None)
        gh = client.GitHubClient(limiter=cast(Any, limiter))

        with mock.patch.object(client.time, "sleep"), self.assertLogs("search", level="INFO") as logs:
            self.assertFalse(gh._limit("github_api"))

        self.assertEqual(client._LIMIT_MAX_WAIT_ROUNDS, len(limiter.waits))
        self.assertTrue(
            any("wait exhausted" in r.getMessage() for r in logs.records)
        )
        # The denial WARNING itself is emitted once, by get_with_headers.
        self.assertFalse(any(r.levelname == "WARNING" for r in logs.records))

    def test_no_limiter_configured_allows_everything(self) -> None:
        self.assertTrue(client.GitHubClient(limiter=None)._limit("github_api"))


class TestDegradedExitVisibility(unittest.TestCase):
    """403/429 answers reset the transport streak, so surface them separately."""

    def tearDown(self) -> None:
        client.set_proxy("")

    def test_repeated_degraded_answers_warn_once_and_reset_on_health(self) -> None:
        client.set_proxy("socks5://10.0.0.1:1080")

        with self.assertLogs("search", level="WARNING") as logs:
            for _ in range(client._PROXY_DEGRADED_WARN_THRESHOLD):
                client._note_proxy_status(429)

        state = client.get_egress_state()
        self.assertEqual(state["degraded_streak"], client._PROXY_DEGRADED_WARN_THRESHOLD)
        self.assertEqual(state["rotations"], 0, "a degraded exit must not rotate")
        self.assertEqual(
            1, sum(1 for r in logs.records if "degraded responses" in r.getMessage())
        )

        client._note_proxy_status(200)
        self.assertEqual(0, client.get_egress_state()["degraded_streak"])

    def test_direct_session_status_never_counted(self) -> None:
        # only proxied answers are tallied; a direct raw 4xx must not move it
        client.set_proxy("socks5://10.0.0.1:1080")
        client._note_proxy_status(403)
        self.assertEqual(1, client.get_egress_state()["degraded_streak"])


if __name__ == "__main__":
    unittest.main()