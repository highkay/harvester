#!/usr/bin/env python3

"""Finding A regression: a rate-limiter denial must never masquerade as "zero results".

Before the fix, ``GitHubClient.get_with_headers`` answered a ``_limit`` denial
with ``("", {})`` at DEBUG level, so a denied first search page was
indistinguishable from a genuinely empty dork: ``SearchStage`` gated its
pagination/refinement on ``total > 0`` and silently dropped the whole dork
(its 10-page tail + its refinement branch) with an INFO line identical to a
real empty result. ``_limit`` really can deny — it now waits in bounded rounds
until a total deadline (`_LIMIT_WAIT_DEADLINE_SECONDS`), because a single
re-acquire can lose the refilled token to a competing search thread (prod:
github_api base_rate 0.15 / burst 3 per credential, 6-8 concurrent search
threads), and the token-steal race makes ``wait_time()`` read ~0 while
``acquire()`` still fails.

Now the denial raises retryable ``ConnectionError`` at WARNING, an unparseable
(non-JSON) search body raises too, and only a well-formed ``total_count == 0``
response reaches the first-page gate as a genuine zero-result dork.
"""

from __future__ import annotations

import unittest
from unittest import mock

from config.schemas import StageConfig, TaskConfig
from core.enums import PipelineStage
from core.models import SearchTask
from search import client
from search.client import GitHubClient
from stage.base import StageResources
from stage.definition import SearchStage
from tools.retry import RetryCore

_API_URL = "https://api.github.com/search/code?q=test&per_page=100&page=1"


def _denying_limiter() -> mock.MagicMock:
    """Limiter whose acquire always fails; wait window is a realistic 6.7 s.

    Prod github_api base_rate is 0.15/s per credential bucket, so one token is
    ~6.7 s away — the fixture must model that, not a 0.0 window (with the
    per-round floor a 0.0 window would just spin the floor down to the
    deadline). Tests must patch ``time.sleep``: the bounded wait is real time.
    """
    limiter = mock.MagicMock()
    limiter.acquire.return_value = False
    limiter.wait_time.return_value = 6.7
    return limiter


def _no_transport_globals():
    """Quota tracker / response cache off, so the denial path is reached directly."""
    return (
        mock.patch.object(client, "_QUOTA_TRACKER", None),
        mock.patch.object(client, "_RESPONSE_CACHE", None),
    )


class TestRateLimiterDenialRaises(unittest.TestCase):
    """get_with_headers: denial -> retryable ConnectionError + WARNING, request never sent."""

    def test_denial_raises_connection_error(self):
        # Given a client whose rate limiter denies the token
        gh = GitHubClient(limiter=_denying_limiter())
        quota, cache = _no_transport_globals()
        with quota, cache, mock.patch.object(client.time, "sleep"), mock.patch.object(
            gh, "_http_get"
        ) as http_get:
            # When a rate-limited request is attempted
            with self.assertRaises(ConnectionError) as ctx:
                gh.get_with_headers(url=_API_URL, credential="tok")

        # Then it fails loudly and retryably — and the request was never sent
        self.assertIn("rate limiter denied", str(ctx.exception))
        self.assertTrue(RetryCore.should_retry_error(ctx.exception, attempt=0, max_retries=3))
        http_get.assert_not_called()

    def test_denial_logs_warning_not_debug(self):
        gh = GitHubClient(limiter=_denying_limiter())
        quota, cache = _no_transport_globals()
        with quota, cache, mock.patch.object(client.time, "sleep"), mock.patch.object(
            client.logger, "warning"
        ) as warn, mock.patch.object(client.logger, "debug") as debug:
            with self.assertRaises(ConnectionError):
                gh.get_with_headers(url=_API_URL, credential="tok")

        warn.assert_called_once()
        self.assertIn("denied", warn.call_args[0][0])
        debug.assert_not_called()


class TestSearchApiFailureModes(unittest.TestCase):
    """search_api_with_count: fetch failures raise; a genuine zero-result dork returns total=0."""

    def test_denial_propagates_through_search_api(self):
        gh = GitHubClient(limiter=_denying_limiter())
        quota, cache = _no_transport_globals()
        with quota, cache, mock.patch.object(client.time, "sleep"), mock.patch.object(
            client, "get_github_client", return_value=gh
        ):
            with self.assertRaises(ConnectionError):
                client.search_api_with_count(query="q", token="tok", page=1)

    def test_unparseable_body_raises_instead_of_zero_results(self):
        # A non-blank body that is not JSON is a fetch failure (rate-limit
        # interstitial / proxy junk), never a zero-result dork.
        gh = mock.MagicMock()
        gh.get.return_value = "<html>Search failed. Please try again later.</html>"
        with mock.patch.object(client, "get_github_client", return_value=gh):
            with self.assertRaises(ConnectionError):
                client.search_api_with_count(query="q", token="tok", page=1)

    def test_genuine_zero_result_returns_empty_with_total_zero(self):
        gh = mock.MagicMock()
        gh.get.return_value = '{"total_count": 0, "items": []}'
        with mock.patch.object(client, "get_github_client", return_value=gh):
            results, total, content = client.search_api_with_count(query="q", token="tok", page=1)

        self.assertEqual([], results)
        self.assertEqual(0, total)
        self.assertIn("total_count", content)


class _Auth:
    """Minimal IAuthProvider stub."""

    def get_session(self):
        return ""

    def get_token(self):
        return "token"

    def get_credential(self, prefer_token: bool = True):
        return "token", "api"

    def get_user_agent(self) -> str:
        return "test-agent"


def _make_stage() -> SearchStage:
    resources = StageResources(
        limiter=mock.MagicMock(),
        providers={},
        config=mock.MagicMock(),
        task_configs={"test": TaskConfig(name="test", provider_type="openai_like", stages=StageConfig())},
        auth=_Auth(),
    )
    return SearchStage(resources, handler=lambda _output: None)


def _task() -> SearchTask:
    return SearchTask(provider="test", query='"SOME_DORK"', regex="", page=1, use_api=True, max_pages=1000)


class TestFirstPageGateDistinguishesFailureFromZero(unittest.TestCase):
    """SearchStage first-page gate: denial -> raise (requeue), zero -> clean empty output."""

    def test_denied_first_page_raises_instead_of_empty_output(self):
        stage = _make_stage()
        with mock.patch.object(
            client, "search_with_count", side_effect=ConnectionError("rate limiter denied for github_api")
        ):
            with self.assertRaises(ConnectionError):
                stage._search_worker(_task())

    def test_genuine_zero_result_returns_output_without_page_tasks(self):
        stage = _make_stage()
        with mock.patch.object(client, "search_with_count", return_value=([], 0, '{"total_count": 0, "items": []}')), (
            mock.patch.object(client, "get_link_index", return_value=None)
        ):
            output = stage._search_worker(_task())

        # A genuinely empty dork succeeds (no requeue) and adds no page/refined tasks
        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual([], output.new_tasks)

    def test_nonzero_total_still_emits_page_tasks(self):
        stage = _make_stage()
        with mock.patch.object(client, "search_with_count", return_value=(["https://github.com/o/r"], 350, "")), (
            mock.patch.object(client, "get_link_index", return_value=None)
        ):
            output = stage._search_worker(_task())

        assert output is not None
        search_tasks = [t for t, name in output.new_tasks if name == PipelineStage.SEARCH.value]
        pages = sorted(t.page for t in search_tasks if isinstance(t, SearchTask) and t.query == '"SOME_DORK"')
        self.assertEqual([2, 3, 4], pages)  # ceil(350/100)=4 -> pages 2..4
        gather_tasks = [t for t, name in output.new_tasks if name == PipelineStage.GATHER.value]
        self.assertEqual(1, len(gather_tasks))


if __name__ == "__main__":
    unittest.main()
