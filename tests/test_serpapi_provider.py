#!/usr/bin/env python3

"""Unit tests for the SerpApi provider (provider/serpapi.py)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.base import AIBaseProvider
from provider.registry import ProviderRegistry, get_available_providers
from provider.serpapi import SerpapiProvider

_ACCOUNT_JSON = {
    "account_id": "5ac54d6adefb2f1dba1663f5",
    "api_key": "0123456789abcdef0123456789abcdef",
    "account_email": "demo@serpapi.com",
    "account_status": "Active",
    "plan_id": "bigdata",
    "plan_name": "Big Data Plan",
    "plan_monthly_price": 250.0,
    "plan_renewal_date": "2026-09-28",
    "searches_per_month": 30000,
    "plan_searches_left": 5958,
    "extra_credits": 0,
    "total_searches_left": 5958,
    "this_hour_searches": 87,
    "last_hour_searches": 42,
    "account_rate_limit_per_hour": 6000,
}


def _make_condition() -> Condition:
    return Condition(
        query='"SERPAPI_API_KEY"',
        patterns=Patterns(key_pattern=r"\b[0-9a-fA-F]{32}\b"),
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
    return mock.patch("provider.serpapi.request", return_value=response)


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """Build an HTTPError carrying a real response, as raise_for_status does."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://serpapi.com/account.json"
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


class TestSerpapiProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("serpapi", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("serpapi", conditions=[_make_condition()])
        self.assertIsInstance(provider, SerpapiProvider)
        self.assertEqual(provider.name, "serpapi")

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(SerpapiProvider, AIBaseProvider))


class TestSerpapiProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = SerpapiProvider(
            conditions=[_make_condition()], retries=2, timeout=5
        )

    def test_check_success_200_account_json(self):
        with _patch_request(FakeResponse(200, json.dumps(_ACCOUNT_JSON))):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertTrue(result.ok)
        # Success message must never carry the echoed key
        self.assertNotIn("0123456789abcdef", result.message or "")

    def test_check_uses_account_endpoint_and_query_param(self):
        with _patch_request(FakeResponse(200, json.dumps(_ACCOUNT_JSON))) as req_mock:
            self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(
            req_mock.call_args.args[1], "https://serpapi.com/account.json"
        )
        self.assertEqual(
            req_mock.call_args.kwargs["params"],
            {"api_key": "0123456789abcdef0123456789abcdef"},
        )

    def test_check_200_non_json_unknown(self):
        with _patch_request(FakeResponse(200, "<html>ok</html>")):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_200_without_account_fields_unknown(self):
        # search.json answers 200 even without a key — gate against false positives
        payload = {"search_metadata": {"status": "Success"}, "organic_results": []}
        with _patch_request(FakeResponse(200, json.dumps(payload))):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_401_invalid_key(self):
        error = _http_error(401, '{"error": "Invalid API key. Your API key should be here: https://serpapi.com/manage-api-key"}')
        with mock.patch("provider.serpapi.request", side_effect=error) as req_mock:
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        # 401 is in NO_RETRY_ERROR_CODES — must not retry
        self.assertEqual(req_mock.call_count, 1)

    def test_check_400_bad_request(self):
        error = _http_error(400, '{"error": "Bad request"}')
        with mock.patch("provider.serpapi.request", side_effect=error):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_check_403_no_access(self):
        error = _http_error(403, '{"error": "Forbidden"}')
        with mock.patch("provider.serpapi.request", side_effect=error):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_check_429_rate_limited(self):
        error = _http_error(429, '{"error": "Too many requests"}')
        with mock.patch("provider.serpapi.request", side_effect=error):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_500_server_error(self):
        error = _http_error(500, "Internal Server Error")
        with mock.patch("provider.serpapi.request", side_effect=error):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_timeout(self):
        with mock.patch(
            "provider.serpapi.request", side_effect=requests.exceptions.Timeout()
        ):
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)

    def test_check_network_error_after_retries(self):
        with mock.patch(
            "provider.serpapi.request",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ) as req_mock:
            result = self.provider.check(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(req_mock.call_count, 2)

    def test_check_empty_token_invalid(self):
        result = self.provider.check(token="")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


class TestSerpapiProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = SerpapiProvider(
            conditions=[_make_condition()], retries=1, timeout=5
        )

    def test_inspect_returns_audit_items_without_api_key(self):
        with _patch_request(FakeResponse(200, json.dumps(_ACCOUNT_JSON))):
            items = self.provider.inspect(token="0123456789abcdef0123456789abcdef")
        self.assertIsInstance(items, list)
        self.assertTrue(items)
        self.assertNotIn("0123456789abcdef0123456789abcdef", " ".join(items))
        self.assertTrue(any("plan_name" in item for item in items))
        self.assertFalse(any(item.startswith("api_key") for item in items))

    def test_inspect_non_200_returns_empty(self):
        with _patch_request(FakeResponse(401, '{"error": "Invalid API key"}')):  # type: ignore[arg-type]
            items = self.provider.inspect(token="0123456789abcdef0123456789abcdef")
        self.assertEqual(items, [])

    def test_inspect_empty_token_returns_empty(self):
        items = self.provider.inspect(token="")
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()