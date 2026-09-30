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
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


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


class TestDegradedExitRotation(unittest.TestCase):
    """A sustained-degraded exit (403/429 to EVERYTHING) must be rotated away.

    Proven live on prod (2026-09-29 13:34-16:00): 30 degraded warnings in
    2.5 h during an nvidia run while the exit kept poisoning every proxied
    request — the warn-only streak (commit 691268f) made the collapse
    explainable but never moved traffic. Rotation reuses the transport
    failover mechanics: next HARVESTER_PROXY candidate under _proxy_lock,
    masked log, one rotation per streak (re-armed for the next exit).
    Quota-flavoured 403/429 (rate-limit headers) must NEVER rotate.

    Round-2 review addendum: rotation requires MULTI-HOST evidence. Exit-level
    degradation is uniform across destinations; application-level per-key
    verdicts are single-host — nvidia validation answers 403 "Authorization
    failed" (no rate-limit headers) for EVERY invalid key, and nvidia runs are
    invalid-key dominated, so a host-agnostic streak rotated the process-wide
    egress mid-run on a daily cadence (worst case: an agnes-ai run pinned to
    socks5h for DNS-pollution reasons gets rotated onto socks5:// → mass
    NETWORK_ERROR, AGENTS.md 2026-09-04). A single-host streak stays warn-only.
    """

    PROXIES = [
        "socks5://user:pool-token@10.0.0.1:1080",
        "socks5://user:pool-token@10.0.0.2:1080",
        "socks5://user:pool-token@10.0.0.3:1080",
    ]

    def setUp(self) -> None:
        os.environ["HARVESTER_PROXY"] = ",".join(self.PROXIES)
        client.set_proxy(self.PROXIES[0])
        # degraded_rotations is a process-lifetime diagnostic counter (like
        # transport `rotations`) — assert per-test deltas, not absolutes.
        self._rot0 = client.get_egress_state()["degraded_rotations"]

    def tearDown(self) -> None:
        client.set_proxy("")
        os.environ.pop("HARVESTER_PROXY", None)

    def _rotations(self) -> int:
        return client.get_egress_state()["degraded_rotations"] - self._rot0

    def _drive(self, response, count: int = 1, url: str = API_URL) -> None:
        # One patch context PER request: when a streak rotates, _apply_proxy
        # rebinds the module-global _HTTP_SESSION inside the running context —
        # a shared multi-request context would let later requests escape to a
        # real session against the fake proxy.
        for _ in range(count):
            session = mock.MagicMock()
            session.request.return_value = response
            with mock.patch.object(client, "_HTTP_SESSION", session), mock.patch.object(
                client, "_DIRECT_SESSION", mock.MagicMock()
            ):
                try:
                    client.request("GET", url)
                except requests.exceptions.HTTPError:
                    pass  # raise_for_status fires after the status was recorded

    def _drive_degraded(self, count: int, status: int = 429, url: str = API_URL) -> None:
        self._drive(_FakeResponse(status_code=status), count, url)

    def _drive_two_hosts(self, total: int, status: int = 429) -> None:
        """Alternate degraded votes across two hosts — exit-level evidence.

        Rotation is gated on the streak spanning >=2 distinct hosts (uniform
        degradation across destinations), so rotation-expecting drives must
        cross hosts; even totals still put both hosts inside the streak.
        """
        for i in range(total):
            self._drive_degraded(1, status=status, url=API_URL if i % 2 == 0 else NVIDIA_URL)

    def test_threshold_streak_rotates_to_next_exit_with_masked_warning(self) -> None:
        with self.assertLogs("search", level="WARNING") as logs:
            self._drive_two_hosts(client._PROXY_DEGRADED_WARN_THRESHOLD)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[1])
        self.assertEqual(self._rotations(), 1)
        # Re-armed: the streak resets so the NEXT exit can warn/rotate too.
        self.assertEqual(state["degraded_streak"], 0)

        warnings = [r.getMessage() for r in logs.records if r.levelno >= 30]
        self.assertEqual(1, len(warnings))
        self.assertNotIn("pool-token", warnings[0])
        self.assertIn("10.0.0.1", warnings[0])
        self.assertIn("10.0.0.2", warnings[0])

    def test_never_rotates_twice_within_one_streak(self) -> None:
        self._drive_two_hosts(client._PROXY_DEGRADED_WARN_THRESHOLD)
        self.assertEqual(self._rotations(), 1)

        # A fresh streak that has not completed must not rotate again.
        self._drive_two_hosts(client._PROXY_DEGRADED_WARN_THRESHOLD - 1)
        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[1])
        self.assertEqual(self._rotations(), 1)

    def test_second_streak_on_new_exit_rotates_again(self) -> None:
        self._drive_two_hosts(2 * client._PROXY_DEGRADED_WARN_THRESHOLD)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[2])
        self.assertEqual(self._rotations(), 2)

    def test_rate_limited_403_resets_streak_without_rotating(self) -> None:
        # api.github.com advertises x-ratelimit-* even on 403 — quota state,
        # not exit state: it must reset the streak and never rotate.
        response = _FakeResponse(status_code=403)
        response.headers = {"x-ratelimit-remaining": "0"}

        self._drive_two_hosts(client._PROXY_DEGRADED_WARN_THRESHOLD - 1)
        self._drive(response, count=1)
        self._drive_two_hosts(client._PROXY_DEGRADED_WARN_THRESHOLD - 1)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(self._rotations(), 0)
        self.assertEqual(state["degraded_streak"], client._PROXY_DEGRADED_WARN_THRESHOLD - 1)

    def test_retry_after_429_never_rotates(self) -> None:
        response = _FakeResponse(status_code=429)
        response.headers = {"retry-after": "60"}

        self._drive(response, count=3 * client._PROXY_DEGRADED_WARN_THRESHOLD)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(self._rotations(), 0)
        self.assertEqual(state["degraded_streak"], 0)

    def test_interleaved_200_resets_streak_without_rotating(self) -> None:
        threshold = client._PROXY_DEGRADED_WARN_THRESHOLD
        self._drive_two_hosts(threshold - 1)
        self._drive(_FakeResponse(status_code=200))
        self._drive_two_hosts(threshold - 1)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(self._rotations(), 0)

    def test_single_candidate_stays_warn_only(self) -> None:
        os.environ["HARVESTER_PROXY"] = self.PROXIES[0]
        client.set_proxy(self.PROXIES[0])

        with self.assertLogs("search", level="WARNING") as logs:
            self._drive_two_hosts(client._PROXY_DEGRADED_WARN_THRESHOLD + 5)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(self._rotations(), 0)
        self.assertEqual(state["degraded_streak"], client._PROXY_DEGRADED_WARN_THRESHOLD + 5)
        # Today's diagnostic warning is preserved for the warn-only case.
        self.assertEqual(
            1,
            sum(1 for r in logs.records if "degraded responses" in r.getMessage()),
        )

    def test_single_host_flood_never_rotates(self) -> None:
        # Given an nvidia-style check phase: EVERY invalid key answers 403
        # "Authorization failed" without rate-limit headers, so header-less
        # 403 floods on ONE host are routine app-level verdicts.
        # When 30 consecutive 403s all hit integrate.api.nvidia.com,
        # Then the exit must NOT rotate — warn-only, streak stays visible.
        with self.assertLogs("search", level="WARNING") as logs:
            self._drive_degraded(
                client._PROXY_DEGRADED_WARN_THRESHOLD + 5, status=403, url=NVIDIA_URL
            )

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(self._rotations(), 0)
        self.assertEqual(state["degraded_streak"], client._PROXY_DEGRADED_WARN_THRESHOLD + 5)
        self.assertEqual(
            1,
            sum(1 for r in logs.records if "degraded responses" in r.getMessage()),
        )

    def test_multi_host_threshold_streak_rotates(self) -> None:
        # Given a uniform-degradation signature: 13 api.github.com + 12
        # integrate.api.nvidia.com header-less 403s in one streak.
        # When the streak reaches the threshold spanning 2 distinct hosts,
        # Then it rotates to the next candidate and re-arms.
        self._drive_degraded(13, status=403, url=API_URL)
        self._drive_degraded(12, status=403, url=NVIDIA_URL)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[1])
        self.assertEqual(self._rotations(), 1)
        self.assertEqual(state["degraded_streak"], 0)

    def test_second_host_completing_threshold_rotates(self) -> None:
        # Given 24 header-less 403s on one host (below threshold, no rotation),
        # When the 25th vote arrives on a SECOND host,
        # Then the threshold is reached with multi-host evidence → rotates.
        self._drive_degraded(
            client._PROXY_DEGRADED_WARN_THRESHOLD - 1, status=403, url=API_URL
        )
        self.assertEqual(self._rotations(), 0)
        self.assertEqual(client.get_egress_state()["proxy"], self.PROXIES[0])

        self._drive_degraded(1, status=403, url=NVIDIA_URL)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[1])
        self.assertEqual(self._rotations(), 1)

    def test_single_host_streaks_around_a_healthy_reset_never_rotate(self) -> None:
        # Given a single-host flood, then a healthy 200 (streak + host set
        # reset), then another single-host flood,
        # Then neither streak may rotate — the reset must not leak host
        # evidence into the next streak.
        threshold = client._PROXY_DEGRADED_WARN_THRESHOLD
        self._drive_degraded(threshold, status=403, url=NVIDIA_URL)
        self._drive(_FakeResponse(status_code=200), url=NVIDIA_URL)
        self.assertEqual(0, client.get_egress_state()["degraded_streak"])

        self._drive_degraded(threshold, status=403, url=NVIDIA_URL)

        state = client.get_egress_state()
        self.assertEqual(state["proxy"], self.PROXIES[0])
        self.assertEqual(self._rotations(), 0)
        self.assertEqual(state["degraded_streak"], threshold)


class _FakeLimiter:
    """Minimal RateLimiter stand-in for GitHubClient._limit.

    Default wait window 6.7 s models prod (github_api base_rate 0.15/s per
    credential bucket — one token is ~6.7 s away).
    """

    def __init__(self, grants_on_call: int | None, wait_time: float = 6.7):
        self.calls = 0
        self.grants_on_call = grants_on_call
        self.wait_time_value = wait_time
        self.waits: list[float] = []

    def acquire(self, service: str, tokens: int = 1) -> bool:
        self.calls += 1
        return self.grants_on_call is not None and self.calls >= self.grants_on_call

    def wait_time(self, service: str, tokens: int = 1) -> float:
        self.waits.append(self.wait_time_value)
        return self.wait_time_value

    def _get_bucket(self, service: str):
        return None


class TestLimiterDenialBudget(unittest.TestCase):
    """The process-wide github_api bucket must be waited on until a DEADLINE.

    Measured 2026-09-26 08:00: the openrouter run (ONE condition) lost its only
    search task to limiter denials and "completed" with 0 links. A single
    re-acquire was useless — the token-steal race makes ``wait_time()`` read ~0
    while ``acquire()`` still fails — so the loop waits until a total deadline
    with a per-round floor, and a round cap as a runaway guard only.
    """

    def test_immediate_grant_does_not_wait(self) -> None:
        limiter = _FakeLimiter(grants_on_call=1)
        gh = client.GitHubClient(limiter=cast(Any, limiter))

        self.assertTrue(gh._limit("github_api"))
        self.assertEqual([], limiter.waits)

    def test_grant_during_the_wait_succeeds(self) -> None:
        limiter = _FakeLimiter(grants_on_call=3)
        gh = client.GitHubClient(limiter=cast(Any, limiter))

        with mock.patch.object(client.time, "sleep"):
            self.assertTrue(gh._limit("github_api"))

        self.assertEqual(2, len(limiter.waits))
        self.assertLessEqual(
            sum(limiter.waits), client._LIMIT_WAIT_DEADLINE_SECONDS + limiter.wait_time_value
        )

    def test_exhausted_wait_denies_after_the_deadline(self) -> None:
        limiter = _FakeLimiter(grants_on_call=None)
        gh = client.GitHubClient(limiter=cast(Any, limiter))

        with mock.patch.object(client.time, "sleep") as sleep:
            denied = not gh._limit("github_api")

        self.assertTrue(denied)
        # 3 rounds x 6.7 s crosses the 15 s deadline; never more than the cap,
        # and the worst case must stay inside BasePipelineStage.stop's 30 s
        # budget (split across workers) or a stop would log zombie threads.
        self.assertEqual(3, len(limiter.waits))
        self.assertLessEqual(len(limiter.waits), client._LIMIT_WAIT_MAX_ROUNDS)
        self.assertEqual(3, sleep.call_count)
        self.assertLess(sum(limiter.waits), 30.0)
        self.assertLess(
            sum(limiter.waits),
            client._LIMIT_WAIT_DEADLINE_SECONDS + limiter.wait_time_value,
        )

    def test_zero_wait_window_still_waits_through_the_floor(self) -> None:
        # The token-steal race: wait_time() reads 0 while acquire() fails. The
        # per-round floor must keep the loop waiting instead of denying after a
        # few 0.1 s rounds — the defect that made a round-counted loop a no-op.
        limiter = _FakeLimiter(grants_on_call=None, wait_time=0.0)
        gh = client.GitHubClient(limiter=cast(Any, limiter))

        with mock.patch.object(client.time, "sleep") as sleep:
            self.assertFalse(gh._limit("github_api"))

        self.assertGreaterEqual(
            sleep.call_count,
            int(client._LIMIT_WAIT_DEADLINE_SECONDS / client._LIMIT_WAIT_ROUND_FLOOR) - 1,
        )
        self.assertTrue(
            all(call.args[0] >= client._LIMIT_WAIT_ROUND_FLOOR for call in sleep.call_args_list)
        )

    def test_no_limiter_configured_allows_everything(self) -> None:
        self.assertTrue(client.GitHubClient(limiter=None)._limit("github_api"))


class TestDegradedExitVisibility(unittest.TestCase):
    """403/429 answers reset the transport streak, so surface them separately.

    Driven through ``request()`` — the gating lives in its ``proxied`` branch,
    so calling the recorder directly would not catch a regression that moved
    the call out of that branch.
    """

    def tearDown(self) -> None:
        client.set_proxy("")

    def _drive(self, url: str, response) -> None:
        session = mock.MagicMock()
        session.request.return_value = response
        with mock.patch.object(client, "_HTTP_SESSION", session), mock.patch.object(
            client, "_DIRECT_SESSION", mock.MagicMock()
        ):
            try:
                client.request("GET", url)
            except requests.exceptions.HTTPError:
                pass  # raise_for_status fires after the status was recorded

    def test_unexplained_degraded_answers_warn_once_and_reset_on_health(self) -> None:
        client.set_proxy("socks5://10.0.0.1:1080")

        with self.assertLogs("search", level="WARNING") as logs:
            for _ in range(client._PROXY_DEGRADED_WARN_THRESHOLD):
                self._drive(API_URL, _FakeResponse(status_code=429))

        state = client.get_egress_state()
        self.assertEqual(state["degraded_streak"], client._PROXY_DEGRADED_WARN_THRESHOLD)
        self.assertEqual(state["rotations"], 0, "a degraded exit must not rotate")
        self.assertEqual(
            1, sum(1 for r in logs.records if "degraded responses" in r.getMessage())
        )

        self._drive(API_URL, _FakeResponse(status_code=200))
        self.assertEqual(0, client.get_egress_state()["degraded_streak"])

    def test_rate_limit_headers_do_not_count_as_degraded(self) -> None:
        # api.github.com advertises x-ratelimit-* even on 403 — that is quota
        # state, not exit health, and must not cry wolf.
        client.set_proxy("socks5://10.0.0.1:1080")
        response = _FakeResponse(status_code=403)
        response.headers = {"x-ratelimit-remaining": "0", "retry-after": "60"}

        self._drive(API_URL, response)

        self.assertEqual(0, client.get_egress_state()["degraded_streak"])

    def test_direct_host_status_is_not_counted(self) -> None:
        # raw.githubusercontent.com is never proxied; its 403s are routine and
        # must not feed the exit-health signal.
        client.set_proxy("socks5://10.0.0.1:1080")

        self._drive(RAW_URL, _FakeResponse(status_code=403))

        self.assertEqual(0, client.get_egress_state()["degraded_streak"])


if __name__ == "__main__":
    unittest.main()