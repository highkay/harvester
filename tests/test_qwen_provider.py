#!/usr/bin/env python3

"""Unit tests for Alibaba Cloud Qwen (DashScope) provider."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns
from provider.qwen import QwenProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from tools.patterns import redact_api_keys_in_text


def _make_condition() -> Condition:
    return Condition(
        query='"DASHSCOPE_API_KEY"',
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
            {"id": "qwen-turbo", "object": "model", "owned_by": "aliyun"},
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
    return mock.patch("provider.qwen.request", return_value=response)


class TestQwenProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("qwen", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("qwen", conditions=[_make_condition()])
        self.assertIsInstance(provider, QwenProvider)
        self.assertEqual(provider.name, "qwen")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(QwenProvider, OpenAILikeProvider))


class TestQwenProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = QwenProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_success(self):
        # models 200 + probe 200 -> fully usable key
        with mock.patch(
            "provider.qwen.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(200, COMPLETION_OK)],
        ):
            result = self.provider.check(token="sk-abcdefghijklmnopqrstuvwxyz123456")

        self.assertTrue(result.available)

    def test_check_invalid_key(self):
        # DashScope returns 401 + code=invalid_api_key
        with _patch_request(
            FakeResponse(
                401,
                '{"error":{"message":"Incorrect API key provided","type":"invalid_request_error","code":"invalid_api_key"}}',
            )
        ):
            result = self.provider.check(token="sk-invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_401_empty_body_still_invalid(self):
        with _patch_request(FakeResponse(401, "")):
            result = self.provider.check(token="sk-weird")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_models_ok_then_402_no_quota(self):
        # Models gate passes but probe surfaces 402 -> zero balance.
        probe_402 = '{"error":{"message":"Insufficient balance","type":"insufficient_quota"}}'
        with mock.patch(
            "provider.qwen.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(402, probe_402)],
        ):
            result = self.provider.check(token="sk-nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_models_ok_then_429_quota_no_quota(self):
        # DashScope expresses zero balance as 429 + quota error.
        probe_429 = '{"error":{"message":"Throttling.QuotaExceeded","code":"Throttling.QuotaExceeded"}}'
        with mock.patch(
            "provider.qwen.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(429, probe_429)],
        ):
            result = self.provider.check(token="sk-nobalance-429")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_models_ok_then_400_arrearage_no_quota(self):
        # DashScope signals zero balance with HTTP 400 + code=Arrearage
        # (NOT 402, unlike OpenAI/DeepSeek).
        probe_400 = (
            '{"code":"Arrearage","param":null,'
            '"message":"Access denied, please make sure your account is in good standing.",'
            '"type":"Arrearage"}'
        )
        with mock.patch(
            "provider.qwen.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(400, probe_400)],
        ):
            result = self.provider.check(token="sk-arrearage")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_400_invalid_parameter_bad_request(self):
        # A 400 that is NOT arrearage stays a BAD_REQUEST (not NO_QUOTA).
        probe_400 = '{"code":"InvalidParameter","message":"bad param","type":"InvalidParameter"}'
        with mock.patch(
            "provider.qwen.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(400, probe_400)],
        ):
            result = self.provider.check(token="sk-badparam")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_check_rate_limited(self):
        with _patch_request(FakeResponse(429, '{"error":{"type":"rate_limit_exceeded"}}')):
            result = self.provider.check(token="sk-limited")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_server_error(self):
        with _patch_request(FakeResponse(500, "server error")):
            result = self.provider.check(token="sk-servererror")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)


class TestQwenProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = QwenProvider(conditions=[_make_condition()])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_models(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=MODELS_RESPONSE):
            models = self.provider.inspect(token="sk-valid")

        self.assertEqual(models, ["qwen-turbo"])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_empty(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=""):
            models = self.provider.inspect(token="sk-valid")

        self.assertEqual(models, [])


class TestQwenRedaction(unittest.TestCase):
    def test_redact_sk_key(self):
        key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_api_keys_in_text(f"token {key} here")
        self.assertNotIn(key, redacted)
        self.assertIn("...", redacted)


class TestQwenLiveCheck(unittest.TestCase):
    """Live integration test - only runs if QWEN_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("QWEN_LIVE_TEST") == "1", "Set QWEN_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check_and_output(self, _ua):
        provider = QwenProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("QWEN_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set QWEN_TEST_KEYS=key1,key2 to run live validation")

        valid_keys = []
        for key in keys:
            result = provider.check(token=key)
            if result.available:
                valid_keys.append(key)
                print(f"[VALID] {key[:8]}...")
            else:
                print(f"[INVALID] {key[:8]}... reason={result.reason}")

        output_path = os.path.join(os.path.dirname(__file__), "..", "data-qwen", "valid-keys.txt")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for key in valid_keys:
                f.write(key + "\n")

        print(f"\nWrote {len(valid_keys)} valid key(s) to {os.path.abspath(output_path)}")


if __name__ == "__main__":
    unittest.main()
