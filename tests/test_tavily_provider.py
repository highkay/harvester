#!/usr/bin/env python3

"""Unit tests for the Tavily provider (provider/tavily.py).

Covers the /usage validation logic including the quota-exhaustion check:
a key whose ``account.plan_usage >= account.plan_limit`` is classified
NO_QUOTA even though /usage returns HTTP 200, because such keys return
402 on actual /search calls and are useless in the proxy pool.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.base import AIBaseProvider
from provider.registry import ProviderRegistry, get_available_providers
from provider.tavily import TavilyProvider


# ---------------------------------------------------------------------------
# Response fixtures (measured from real api.tavily.com /usage responses)
# ---------------------------------------------------------------------------

_USAGE_WITH_REMAINING = {
    "key": {
        "usage": 61,
        "limit": None,
        "search_usage": 60,
        "crawl_usage": 0,
        "extract_usage": 1,
        "map_usage": 0,
        "research_usage": 0,
    },
    "account": {
        "current_plan": "Researcher",
        "plan_usage": 61,
        "plan_limit": 1000,
        "search_usage": 60,
        "crawl_usage": 0,
        "extract_usage": 1,
        "map_usage": 0,
        "research_usage": 0,
        "paygo_usage": 0,
        "paygo_limit": None,
    },
}

_USAGE_EXHAUSTED = {
    "key": {
        "usage": 1000,
        "limit": None,
        "search_usage": 1000,
        "crawl_usage": 0,
        "extract_usage": 0,
        "map_usage": 0,
        "research_usage": 0,
    },
    "account": {
        "current_plan": "Researcher",
        "plan_usage": 1000,
        "plan_limit": 1000,
        "search_usage": 1000,
        "crawl_usage": 0,
        "extract_usage": 0,
        "map_usage": 0,
        "research_usage": 0,
        "paygo_usage": 0,
        "paygo_limit": None,
    },
}

_USAGE_OVER_LIMIT = {
    "key": {
        "usage": 1001,
        "limit": None,
        "search_usage": 1001,
    },
    "account": {
        "current_plan": "Researcher",
        "plan_usage": 1001,
        "plan_limit": 1000,
    },
}

_USAGE_PER_KEY_EXHAUSTED = {
    "key": {
        "usage": 1000,
        "limit": 1000,
        "search_usage": 995,
    },
    "account": {
        "current_plan": "Researcher",
        "plan_usage": 500,
        "plan_limit": 10000,
    },
}

_USAGE_EXHAUSTED_WITH_PAYGO = {
    "key": {"usage": 1000, "limit": None},
    "account": {
        "current_plan": "Researcher",
        "plan_usage": 1000,
        "plan_limit": 1000,
        "paygo_usage": 10,
        "paygo_limit": 500,
    },
}

_USAGE_NO_PLAN_LIMIT = {
    "key": {"usage": 42, "limit": None},
    "account": {
        "current_plan": "Enterprise",
        "plan_usage": 42,
        "plan_limit": None,
    },
}


def _make_condition() -> Condition:
    return Condition(
        query='"tvly-"',
        patterns=Patterns(key_pattern=r"(?:tvly|tavily)-[0-9A-Za-z_-]{20,}"),
        description="test",
        enabled=True,
    )


class FakeResponse:
    """Minimal context-manager response for mocking request()."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_request(response: FakeResponse):
    return mock.patch("provider.tavily.request", return_value=response)


def _patch_two_stage(usage_response=None, search_response=None, usage_error=None, search_error=None):
    """Patch provider.tavily.request, dispatching by HTTP method.

    check() is two-stage since 2026-09-23: GET = the free /usage pre-filter,
    POST = the authoritative /search probe. Returns (patcher, calls) so a test
    can assert which stages actually ran.
    """
    calls = {"GET": 0, "POST": 0}

    def fake(method, url, **kwargs):
        calls[method] = calls.get(method, 0) + 1
        if method == "POST":
            if search_error is not None:
                raise search_error
            return search_response
        if usage_error is not None:
            raise usage_error
        return usage_response

    return mock.patch("provider.tavily.request", side_effect=fake), calls


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.tavily.com/usage"
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestTavilyProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("tavily", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("tavily", conditions=[_make_condition()])
        self.assertIsInstance(provider, TavilyProvider)

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(TavilyProvider, AIBaseProvider))


# ---------------------------------------------------------------------------
# _is_quota_exhausted (unit-level)
# ---------------------------------------------------------------------------


class TestIsQuotaExhausted(unittest.TestCase):
    def test_plan_exhausted(self):
        self.assertTrue(TavilyProvider._is_quota_exhausted(_USAGE_EXHAUSTED))

    def test_plan_over_limit(self):
        self.assertTrue(TavilyProvider._is_quota_exhausted(_USAGE_OVER_LIMIT))

    def test_plan_has_remaining(self):
        self.assertFalse(TavilyProvider._is_quota_exhausted(_USAGE_WITH_REMAINING))

    def test_plan_limit_null_not_exhausted(self):
        self.assertFalse(TavilyProvider._is_quota_exhausted(_USAGE_NO_PLAN_LIMIT))

    def test_per_key_limit_exhausted(self):
        self.assertTrue(TavilyProvider._is_quota_exhausted(_USAGE_PER_KEY_EXHAUSTED))

    def test_exhausted_but_paygo_available_not_exhausted(self):
        self.assertFalse(
            TavilyProvider._is_quota_exhausted(_USAGE_EXHAUSTED_WITH_PAYGO)
        )

    def test_empty_dict_not_exhausted(self):
        self.assertFalse(TavilyProvider._is_quota_exhausted({}))

    def test_no_account_key_not_exhausted(self):
        self.assertFalse(
            TavilyProvider._is_quota_exhausted({"key": {"usage": 0, "limit": None}})
        )


# ---------------------------------------------------------------------------
# check() via mocked HTTP
# ---------------------------------------------------------------------------


_SEARCH_OK = '{"query": "harvester key validation probe", "results": [{"title": "probe"}]}'


class TestTavilyProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = TavilyProvider(
            conditions=[_make_condition()], retries=2, timeout=5
        )

    def test_check_success_with_remaining_quota(self):
        """Valid = /usage healthy AND the /search probe returning a results array."""
        patcher, calls = _patch_two_stage(
            usage_response=FakeResponse(200, json.dumps(_USAGE_WITH_REMAINING)),
            search_response=FakeResponse(200, _SEARCH_OK),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertTrue(result.ok)
        self.assertNotIn("testkey", result.message or "")
        self.assertEqual(calls, {"GET": 1, "POST": 1})

    def test_check_exhausted_quota_returns_no_quota(self):
        """Key with plan_usage >= plan_limit must be NO_QUOTA, not valid."""
        with _patch_request(FakeResponse(200, json.dumps(_USAGE_EXHAUSTED))):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)
        self.assertFalse(result.ok)

    def test_check_over_limit_returns_no_quota(self):
        with _patch_request(FakeResponse(200, json.dumps(_USAGE_OVER_LIMIT))):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_per_key_limit_exhausted_returns_no_quota(self):
        with _patch_request(FakeResponse(200, json.dumps(_USAGE_PER_KEY_EXHAUSTED))):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_exhausted_with_paygo_is_valid(self):
        """Plan exhausted but paygo credits available → the /search probe decides."""
        patcher, _ = _patch_two_stage(
            usage_response=FakeResponse(200, json.dumps(_USAGE_EXHAUSTED_WITH_PAYGO)),
            search_response=FakeResponse(200, _SEARCH_OK),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertTrue(result.ok)

    def test_check_no_plan_limit_is_valid(self):
        """No plan limit in /usage → the /search probe decides."""
        patcher, _ = _patch_two_stage(
            usage_response=FakeResponse(200, json.dumps(_USAGE_NO_PLAN_LIMIT)),
            search_response=FakeResponse(200, _SEARCH_OK),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertTrue(result.ok)

    def test_check_200_without_results_is_unknown(self):
        """A 200 that carries no results array is not a search answer."""
        patcher, _ = _patch_two_stage(
            usage_response=FakeResponse(200, json.dumps(_USAGE_WITH_REMAINING)),
            search_response=FakeResponse(200, json.dumps(_USAGE_WITH_REMAINING)),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_200_non_json_unknown(self):
        with _patch_request(FakeResponse(200, "<html>ok</html>")):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_200_non_dict_json_unknown(self):
        with _patch_request(FakeResponse(200, "[1, 2, 3]")):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_401_invalid_key(self):
        error = _http_error(401, '{"detail": "Invalid API key"}')
        with mock.patch("provider.tavily.request", side_effect=error) as req_mock:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertEqual(req_mock.call_count, 1)

    def test_check_403_no_access(self):
        error = _http_error(403, '{"detail": "Forbidden"}')
        with mock.patch("provider.tavily.request", side_effect=error):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_check_402_no_quota(self):
        error = _http_error(
            402,
            '{"detail": {"error": "This request exceeds your plan\'s set usage limit"}}',
        )
        with mock.patch("provider.tavily.request", side_effect=error):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_429_rate_limited(self):
        error = _http_error(429, '{"detail": "Too many requests"}')
        with mock.patch("provider.tavily.request", side_effect=error):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_400_bad_request(self):
        error = _http_error(400, '{"detail": "Bad request"}')
        with mock.patch("provider.tavily.request", side_effect=error):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_check_500_server_error(self):
        error = _http_error(500, "Internal Server Error")
        with mock.patch("provider.tavily.request", side_effect=error):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_timeout(self):
        with mock.patch(
            "provider.tavily.request", side_effect=requests.exceptions.Timeout()
        ):
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)

    def test_check_network_error_after_retries(self):
        """Both stages use the provider retry budget (2 usage + 2 search tries)."""
        patcher, calls = _patch_two_stage(
            usage_error=requests.exceptions.ConnectionError("boom"),
            search_error=requests.exceptions.ConnectionError("boom"),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(calls, {"GET": 2, "POST": 2})

    def test_check_empty_token_invalid(self):
        result = self.provider.check(token="")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


# ---------------------------------------------------------------------------
# /search probe stage — the disabled-account regression (measured 2026-09-23)
# ---------------------------------------------------------------------------

_DISABLED_ACCOUNT_402 = (
    '{"detail":{"error":"Your account is currently disabled. This is likely due to '
    'unpaid pay-as-you-go balance. Please update your payment method or contact '
    'support@tavily.com"}}'
)


class TestTavilySearchProbe(unittest.TestCase):
    """Measured 2026-09-23: /usage answered 200 with ``plan_usage 0/1000 paygo
    0/20000`` for a key whose POST /search returned 402 "account is currently
    disabled ... unpaid pay-as-you-go balance". Classifying such a key VALID
    poisoned the proxy pool: the proxy cannot see the state either (its own
    ``GetUsage`` reads only key.usage / account.plan_limit levels) and returned
    the 402 to every client request (measured 16x402 + 3x503, no 200, in one
    30-minute window).
    """

    def setUp(self):
        self.provider = TavilyProvider(conditions=[_make_condition()], retries=2, timeout=5)

    def test_disabled_account_is_no_quota_not_valid(self):
        patcher, calls = _patch_two_stage(
            usage_response=FakeResponse(200, json.dumps(_USAGE_WITH_REMAINING)),
            search_error=_http_error(402, _DISABLED_ACCOUNT_402),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")

        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)
        self.assertEqual(calls, {"GET": 1, "POST": 1}, "healthy /usage must still be probed on /search")

    def test_healthy_account_passes_the_search_probe(self):
        patcher, calls = _patch_two_stage(
            usage_response=FakeResponse(200, json.dumps(_USAGE_WITH_REMAINING)),
            search_response=FakeResponse(200, '{"results":[{"title":"probe"}]}'),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")

        self.assertTrue(result.ok)
        self.assertEqual(calls, {"GET": 1, "POST": 1})

    def test_spent_plan_never_spends_a_search_credit(self):
        """Stage 1 catches plan/key-limit exhaustion for free — no /search call."""
        patcher, calls = _patch_two_stage(
            usage_response=FakeResponse(200, json.dumps(_USAGE_EXHAUSTED)),
            search_response=FakeResponse(200, "{}"),
        )
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")

        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)
        self.assertEqual(calls, {"GET": 1, "POST": 0})

    def test_invalid_key_short_circuits_before_the_probe(self):
        patcher, calls = _patch_two_stage(usage_error=_http_error(401, '{"detail":"Invalid API key"}'))
        with patcher:
            result = self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertEqual(calls, {"GET": 1, "POST": 0})

    def test_search_probe_payload_is_minimal(self):
        """One validated key costs at most 1 search credit: 1 result, basic depth."""
        seen = {}

        def fake(method, url, **kwargs):
            if method == "POST":
                seen["url"] = url
                seen["body"] = kwargs.get("json")
                return FakeResponse(200, "{}")
            return FakeResponse(200, json.dumps(_USAGE_WITH_REMAINING))

        with mock.patch("provider.tavily.request", side_effect=fake):
            self.provider.check(token="tvly-dev-testkey0123456789ABCDEF")

        self.assertTrue(seen["url"].endswith("/search"))
        self.assertEqual(
            seen["body"],
            {"query": "harvester key validation probe", "max_results": 1, "search_depth": "basic"},
        )


class TestJudgeSearch(unittest.TestCase):
    def setUp(self):
        self.provider = TavilyProvider(conditions=[_make_condition()])

    def test_200_is_valid(self):
        self.assertTrue(self.provider._judge_search(200, '{"results":[]}').ok)

    def test_401_is_invalid(self):
        result = self.provider._judge_search(401, '{"detail":"Invalid API key"}')
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_403_is_no_access(self):
        self.assertEqual(self.provider._judge_search(403, "").reason, ErrorReason.NO_ACCESS)

    def test_402_disabled_account_is_no_quota(self):
        self.assertEqual(self.provider._judge_search(402, _DISABLED_ACCOUNT_402).reason, ErrorReason.NO_QUOTA)

    def test_432_and_433_are_no_quota(self):
        for code in (432, 433):
            with self.subTest(code=code):
                self.assertEqual(self.provider._judge_search(code, "").reason, ErrorReason.NO_QUOTA)

    def test_429_is_rate_limited(self):
        result = self.provider._judge_search(429, '{"detail":"Too many requests"}')
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_400_is_bad_request(self):
        self.assertEqual(self.provider._judge_search(400, '{"detail":"Bad request"}').reason,
                         ErrorReason.BAD_REQUEST)

    def test_5xx_is_server_error(self):
        self.assertEqual(self.provider._judge_search(503, "unavailable").reason, ErrorReason.SERVER_ERROR)


# ---------------------------------------------------------------------------
# inspect()
# ---------------------------------------------------------------------------


class TestTavilyProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = TavilyProvider(
            conditions=[_make_condition()], retries=1, timeout=5
        )

    def test_inspect_returns_audit_items(self):
        with mock.patch(
            "provider.tavily.http_get", return_value=json.dumps(_USAGE_WITH_REMAINING)
        ):
            items = self.provider.inspect(token="tvly-dev-testkey0123456789ABCDEF")
        self.assertIsInstance(items, list)
        self.assertTrue(items)
        self.assertTrue(any("plan_usage" in item for item in items))
        self.assertTrue(any("current_plan" in item for item in items))

    def test_inspect_empty_token_returns_empty(self):
        items = self.provider.inspect(token="")
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
