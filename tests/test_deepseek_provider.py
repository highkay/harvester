#!/usr/bin/env python3

"""Unit tests for DeepSeek provider."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.deepseek import DeepSeekProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from tools.patterns import redact_api_keys_in_text


def _make_condition() -> Condition:
    return Condition(
        query='"DEEPSEEK_API_KEY"',
        patterns=Patterns(
            key_pattern=r"sk-[0-9A-Za-z_-]{20,}",
        ),
        description="test",
        enabled=True,
    )


MODELS_RESPONSE = json.dumps(
    {
        "object": "list",
        "data": [
            {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
            {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
        ],
    }
)

COMPLETION_OK = json.dumps(
    {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
)

UA_PATCH = "provider.openai_like.get_user_agent"


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
    return mock.patch("provider.deepseek.request", return_value=response)


def _patch_request_sequence(*responses: FakeResponse):
    """Mock request() with one response per call (models gate, then completion probe)."""
    return mock.patch("provider.deepseek.request", side_effect=list(responses))


class TestDeepSeekProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("deepseek", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("deepseek", conditions=[_make_condition()])
        self.assertIsInstance(provider, DeepSeekProvider)
        self.assertEqual(provider.name, "deepseek")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(DeepSeekProvider, OpenAILikeProvider))


class TestDeepSeekProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = DeepSeekProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_success(self):
        with _patch_request(FakeResponse(200, MODELS_RESPONSE)):
            result = self.provider.check(token="sk-abcdefghijklmnopqrstuvwxyz123456")

        self.assertTrue(result.available)

    def test_check_invalid_key_with_empty_body(self):
        # DeepSeek 401 body may be empty/non-JSON; must still be INVALID_KEY.
        with _patch_request(FakeResponse(401, "")):
            result = self.provider.check(token="sk-invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_invalid_key_with_json_body(self):
        with _patch_request(
            FakeResponse(
                401,
                '{"error":{"message":"Authentication Fails (no such user)","type":"authentication_error",'
                '"param":null,"code":"invalid_request_error"}}',
            )
        ):
            result = self.provider.check(token="sk-invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_rate_limited(self):
        with _patch_request(FakeResponse(429, '{"error":"rate limit reached"}')):
            result = self.provider.check(token="sk-valid-but-limited")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_no_quota(self):
        with _patch_request(FakeResponse(402, '{"error":{"message":"Insufficient Balance"}}')):
            result = self.provider.check(token="sk-valid-nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_server_error(self):
        with _patch_request(FakeResponse(500, "internal server error")):
            result = self.provider.check(token="sk-servererror")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_unparseable_success_body_is_unknown(self):
        with _patch_request(FakeResponse(200, "not-json")):
            result = self.provider.check(token="sk-weird")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_models_ok_completion_no_quota(self):
        # /models returns 200 even for zero-balance accounts; only the
        # completion probe surfaces the 402 Payment Required.
        with _patch_request_sequence(
            FakeResponse(200, MODELS_RESPONSE),
            FakeResponse(402, '{"error":{"message":"Insufficient Balance"}}'),
        ):
            result = self.provider.check(token="sk-valid-nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_models_ok_completion_success(self):
        with _patch_request_sequence(
            FakeResponse(200, MODELS_RESPONSE),
            FakeResponse(200, COMPLETION_OK),
        ):
            result = self.provider.check(token="sk-abcdefghijklmnopqrstuvwxyz123456")

        self.assertTrue(result.available)

    def test_check_models_ok_completion_invalid(self):
        with _patch_request_sequence(
            FakeResponse(200, MODELS_RESPONSE),
            FakeResponse(401, ""),
        ):
            result = self.provider.check(token="sk-invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_completion_probe_uses_minimal_payload(self):
        # The balance probe must be a real completion (POST) with max_tokens=1
        # so zero-balance keys are detected without burning tokens.
        with _patch_request_sequence(
            FakeResponse(200, MODELS_RESPONSE),
            FakeResponse(200, COMPLETION_OK),
        ) as req_mock:
            self.provider.check(token="sk-abcdefghijklmnopqrstuvwxyz123456")

        calls = req_mock.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0], "GET")
        self.assertEqual(calls[1].args[0], "POST")

        payload = json.loads(calls[1].kwargs["data"])
        self.assertEqual(payload["max_tokens"], 1)
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertIn("messages", payload)

    def test_check_requests_bypass_proxy(self):
        # Domestic endpoint configured with use_proxy=False must never be
        # routed through the GitHub SOCKS5 proxy: every outbound request
        # carries use_proxy=False.
        provider = DeepSeekProvider(conditions=[_make_condition()], use_proxy=False)
        with _patch_request_sequence(
            FakeResponse(200, MODELS_RESPONSE),
            FakeResponse(200, COMPLETION_OK),
        ) as req_mock:
            provider.check(token="sk-abcdefghijklmnopqrstuvwxyz123456")

        calls = req_mock.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIs(call.kwargs.get("use_proxy"), False)

    def test_check_requests_use_proxy_defaults_to_true(self):
        # Without a use_proxy extra, validation requests must keep the
        # default (proxy) routing: every outbound request carries
        # use_proxy=True.
        with _patch_request_sequence(
            FakeResponse(200, MODELS_RESPONSE),
            FakeResponse(200, COMPLETION_OK),
        ) as req_mock:
            self.provider.check(token="sk-abcdefghijklmnopqrstuvwxyz123456")

        calls = req_mock.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIs(call.kwargs.get("use_proxy"), True)


class TestDeepSeekProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = DeepSeekProvider(conditions=[_make_condition()])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_models(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=MODELS_RESPONSE):
            models = self.provider.inspect(token="sk-valid")

        self.assertEqual(models, ["deepseek-v4-flash", "deepseek-v4-pro"])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_empty(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=""):
            models = self.provider.inspect(token="sk-valid")

        self.assertEqual(models, [])


class TestDeepSeekRedaction(unittest.TestCase):
    def test_redact_sk_key(self):
        key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_api_keys_in_text(f"token {key} here")
        self.assertNotIn(key, redacted)
        self.assertIn("...", redacted)


class TestDeepSeekLiveCheck(unittest.TestCase):
    """Live integration test - only runs if DEEPSEEK_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("DEEPSEEK_LIVE_TEST") == "1", "Set DEEPSEEK_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check_and_output(self, _ua):
        provider = DeepSeekProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("DEEPSEEK_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set DEEPSEEK_TEST_KEYS=key1,key2 to run live validation")

        valid_keys = []
        for key in keys:
            result = provider.check(token=key)
            if result.available:
                valid_keys.append(key)
                print(f"[VALID] {key[:8]}...")
            else:
                print(f"[INVALID] {key[:8]}... reason={result.reason}")

        output_path = os.path.join(os.path.dirname(__file__), "..", "data-deepseek", "valid-keys.txt")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for key in valid_keys:
                f.write(key + "\n")

        print(f"\nWrote {len(valid_keys)} valid key(s) to {os.path.abspath(output_path)}")


if __name__ == "__main__":
    unittest.main()