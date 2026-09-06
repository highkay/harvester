#!/usr/bin/env python3

"""Unit tests for Alibaba ModelScope provider (hub users/me probe)."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.modelscope import ModelScopeProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from tools.patterns import redact_api_keys_in_text


def _make_condition() -> Condition:
    return Condition(
        query='"MODELSCOPE_API_KEY"',
        patterns=Patterns(
            key_pattern=r"ms-[0-9A-Za-z_-]{20,}",
        ),
        description="test",
        enabled=True,
    )


_USERS_ME_OK = {
    "success": True,
    "data": {"username": "u", "email": "e", "nickname": "n"},
}

_USERS_ME_401 = {
    "success": False,
    "code": "InvalidAuthentication",
    "message": "Invalid authentication: missing or invalid Authorization header",
}

_USERS_ME_URL = "https://modelscope.cn/openapi/v1/users/me"

UA_PATCH = "provider.openai_like.get_user_agent"
SLEEP_PATCH = "provider.modelscope.time.sleep"


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
    return mock.patch("provider.modelscope.request", return_value=response)


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """Build an HTTPError carrying a real response, as raise_for_status does."""
    response = requests.Response()
    response.status_code = status_code
    response.url = _USERS_ME_URL
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


class TestModelScopeProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("modelscope", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("modelscope", conditions=[_make_condition()])
        self.assertIsInstance(provider, ModelScopeProvider)
        self.assertEqual(provider.name, "modelscope")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(ModelScopeProvider, OpenAILikeProvider))


class TestModelScopeProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = ModelScopeProvider(conditions=[_make_condition()], retries=3, timeout=5)
        self._patchers = [
            mock.patch(UA_PATCH, return_value="test-agent"),
            mock.patch(SLEEP_PATCH),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()

    def test_check_success_valid(self):
        with _patch_request(FakeResponse(200, json.dumps(_USERS_ME_OK))):
            result = self.provider.check(token="ms-faketoken1234567890abcdef")

        self.assertTrue(result.available)

    def test_check_200_missing_success_unknown(self):
        # Presence-only trap: a 200 body without success+data is NOT proof.
        with _patch_request(FakeResponse(200, json.dumps(_USERS_ME_401))):
            result = self.provider.check(token="ms-faketoken1234567890abcdef")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_200_non_json_unknown(self):
        with _patch_request(FakeResponse(200, "<html>ok</html>")):
            result = self.provider.check(token="ms-faketoken1234567890abcdef")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_401_invalid_authentication(self):
        error = _http_error(401, json.dumps(_USERS_ME_401))
        with mock.patch("provider.modelscope.request", side_effect=error) as req_mock:
            result = self.provider.check(token="ms-faketoken1234567890abcdef")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        # 401 is in NO_RETRY_ERROR_CODES — must not retry
        self.assertEqual(req_mock.call_count, 1)

    def test_check_401_empty_body(self):
        error = _http_error(401, "")
        with mock.patch("provider.modelscope.request", side_effect=error):
            result = self.provider.check(token="weird")

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_403_no_access(self):
        error = _http_error(403, '{"message": "Forbidden"}')
        with mock.patch("provider.modelscope.request", side_effect=error) as req_mock:
            result = self.provider.check(token="ms-blocked")

        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)
        # NO_ACCESS is an immediate-return reason — must not retry
        self.assertEqual(req_mock.call_count, 1)

    def test_check_429_rate_limited(self):
        error = _http_error(429, '{"message": "Too Many Requests"}')
        with mock.patch("provider.modelscope.request", side_effect=error) as req_mock:
            result = self.provider.check(token="ms-limited")

        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)
        # RATE_LIMITED is an immediate-return reason — must not retry
        self.assertEqual(req_mock.call_count, 1)

    def test_check_500_retries_then_network_error(self):
        error = _http_error(500, "Internal Server Error")
        with mock.patch("provider.modelscope.request", side_effect=error) as req_mock:
            result = self.provider.check(token="ms-servererror")

        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(req_mock.call_count, 3)

    def test_check_connection_error_network(self):
        with mock.patch(
            "provider.modelscope.request",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ) as req_mock:
            result = self.provider.check(token="ms-faketoken1234567890abcdef")

        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(req_mock.call_count, 3)

    def test_check_timeout(self):
        with mock.patch(
            "provider.modelscope.request", side_effect=requests.exceptions.Timeout()
        ):
            result = self.provider.check(token="ms-faketoken1234567890abcdef")

        self.assertEqual(result.reason, ErrorReason.TIMEOUT)

    def test_check_empty_token_invalid(self):
        result = self.provider.check(token="")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_uses_hub_me_endpoint(self):
        with _patch_request(FakeResponse(200, json.dumps(_USERS_ME_OK))) as req_mock:
            self.provider.check(token="ms-faketoken1234567890abcdef")

        self.assertEqual(req_mock.call_args.args[0], "GET")
        self.assertEqual(req_mock.call_args.args[1], _USERS_ME_URL)
        headers = req_mock.call_args.kwargs["headers"]
        self.assertEqual(headers["authorization"], "Bearer ms-faketoken1234567890abcdef")


class TestModelScopeRedaction(unittest.TestCase):
    def test_redact_token_env_assignment(self):
        key = "abcdefghijklmnopqrstuvwxyz1234567890"
        redacted = redact_api_keys_in_text(f"MODELSCOPE_API_KEY={key}")
        self.assertNotIn(key, redacted)


class TestModelScopeLiveCheck(unittest.TestCase):
    """Live integration test - only runs if MODELSCOPE_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("MODELSCOPE_LIVE_TEST") == "1", "Set MODELSCOPE_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check_and_output(self, _ua):
        provider = ModelScopeProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("MODELSCOPE_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set MODELSCOPE_TEST_KEYS=key1,key2 to run live validation")

        valid_keys = []
        for key in keys:
            result = provider.check(token=key)
            if result.available:
                valid_keys.append(key)
                print(f"[VALID] {key[:8]}...")
            else:
                print(f"[INVALID] {key[:8]}... reason={result.reason}")

        output_path = os.path.join(os.path.dirname(__file__), "..", "data-modelscope", "valid-keys.txt")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for key in valid_keys:
                f.write(key + "\n")

        print(f"\nWrote {len(valid_keys)} valid key(s) to {os.path.abspath(output_path)}")


if __name__ == "__main__":
    unittest.main()