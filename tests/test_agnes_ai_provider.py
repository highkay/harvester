#!/usr/bin/env python3

"""Unit tests for the Agnes AI provider (provider/agnes_ai.py)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.agnes_ai import AgnesAIProvider
from provider.base import AIBaseProvider
from provider.registry import ProviderRegistry, get_available_providers

_TOKEN = "sk-agnes-0123456789abcdef"

_CHAT_JSON = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
}


def _make_condition() -> Condition:
    return Condition(
        query='"agnes-ai"',
        patterns=Patterns(key_pattern=r"sk-[a-zA-Z0-9]{20}"),
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
    return mock.patch("provider.agnes_ai.request", return_value=response)


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """Build an HTTPError carrying a real response, as raise_for_status does."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://apihub.agnes-ai.com/v1/chat/completions"
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


class TestAgnesAIProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("agnes-ai", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("agnes-ai", conditions=[_make_condition()])
        self.assertIsInstance(provider, AgnesAIProvider)
        self.assertEqual(provider.name, "agnes-ai")

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(AgnesAIProvider, AIBaseProvider))


class TestAgnesAIProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = AgnesAIProvider(
            conditions=[_make_condition()], retries=2, timeout=5
        )

    def test_check_success_200_chat_json(self):
        with _patch_request(FakeResponse(200, json.dumps(_CHAT_JSON))):
            result = self.provider.check(token=_TOKEN)
        self.assertTrue(result.ok)
        # Success message must never echo the raw key
        self.assertNotIn(_TOKEN, result.message or "")

    def test_check_posts_chat_completion_with_bearer(self):
        with _patch_request(FakeResponse(200, json.dumps(_CHAT_JSON))) as req_mock:
            self.provider.check(token=_TOKEN)
        args = req_mock.call_args
        self.assertEqual(args.args[0], "POST")
        self.assertEqual(
            args.args[1], "https://apihub.agnes-ai.com/v1/chat/completions"
        )
        self.assertEqual(
            args.kwargs["headers"].get("Authorization"), f"Bearer {_TOKEN}"
        )
        payload = json.loads(args.kwargs["data"].decode("utf8"))
        self.assertEqual(payload["model"], "agnes-2.5-flash")
        self.assertEqual(payload["stream"], False)
        self.assertEqual(payload["max_tokens"], 1)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "ping"}])

    def test_check_200_non_json_unknown(self):
        # Proxies may answer 200 with junk — a bare 200 is NOT proof of validity
        with _patch_request(FakeResponse(200, "<html>ok</html>")):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_401_invalid_key(self):
        error = _http_error(401, '{"error": {"message": "invalid token"}}')
        with mock.patch("provider.agnes_ai.request", side_effect=error) as req_mock:
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        # 401 is in NO_RETRY_ERROR_CODES — must not retry
        self.assertEqual(req_mock.call_count, 1)

    def test_check_400_invalid_token_message_invalid_key(self):
        # Body message match must classify INVALID_KEY regardless of code
        error = _http_error(400, '{"error": "无效的令牌"}')
        with mock.patch("provider.agnes_ai.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_402_no_quota(self):
        error = _http_error(402, '{"error": "insufficient quota"}')
        with mock.patch("provider.agnes_ai.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_429_rate_limited(self):
        error = _http_error(429, '{"error": "rate limited"}')
        with mock.patch("provider.agnes_ai.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_403_no_access(self):
        error = _http_error(403, '{"error": "forbidden"}')
        with mock.patch("provider.agnes_ai.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_check_400_bad_request(self):
        error = _http_error(400, '{"error": "bad request"}')
        with mock.patch("provider.agnes_ai.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_check_500_server_error(self):
        error = _http_error(500, "Internal Server Error")
        with mock.patch("provider.agnes_ai.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_timeout(self):
        with mock.patch(
            "provider.agnes_ai.request", side_effect=requests.exceptions.Timeout()
        ):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)

    def test_check_network_error_after_retries(self):
        with mock.patch(
            "provider.agnes_ai.request",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ) as req_mock:
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(req_mock.call_count, 2)

    def test_check_empty_token_invalid(self):
        result = self.provider.check(token="")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_whitespace_token_invalid(self):
        result = self.provider.check(token="   ")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


class TestAgnesAIProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = AgnesAIProvider(
            conditions=[_make_condition()], retries=1, timeout=5
        )

    def test_inspect_returns_model_ids(self):
        body = {
            "object": "list",
            "data": [{"id": "agnes-2.5-flash"}, {"id": "gpt-4o"}],
        }
        with _patch_request(FakeResponse(200, json.dumps(body))):
            models = self.provider.inspect(token=_TOKEN)
        self.assertEqual(models, ["agnes-2.5-flash", "gpt-4o"])

    def test_inspect_non_200_returns_empty(self):
        with _patch_request(FakeResponse(401, '{"error": {"message": "invalid"}}')):
            models = self.provider.inspect(token=_TOKEN)
        self.assertEqual(models, [])

    def test_inspect_200_non_json_returns_empty(self):
        with _patch_request(FakeResponse(200, "<html>ok</html>")):
            models = self.provider.inspect(token=_TOKEN)
        self.assertEqual(models, [])

    def test_inspect_empty_token_returns_empty(self):
        models = self.provider.inspect(token="")
        self.assertEqual(models, [])


if __name__ == "__main__":
    unittest.main()