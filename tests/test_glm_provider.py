#!/usr/bin/env python3

"""Unit tests for GLM (Zhipu AI / BigModel) provider."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns
from provider.glm import GLMProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from tools.patterns import redact_api_keys_in_text


def _make_condition() -> Condition:
    return Condition(
        query='"ZHIPUAI_API_KEY"',
        patterns=Patterns(
            key_pattern=r"[0-9a-f]{32}\.[0-9a-f]{32}",
        ),
        description="test",
        enabled=True,
    )


COMPLETION_OK = json.dumps(
    {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
)

UA_PATCH = "provider.openai_like.get_user_agent"


class TestGLMProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("glm", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("glm", conditions=[_make_condition()])
        self.assertIsInstance(provider, GLMProvider)
        self.assertEqual(provider.name, "glm")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(GLMProvider, OpenAILikeProvider))


class TestGLMProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = GLMProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_success(self):
        with mock.patch("provider.base.chat", return_value=(200, COMPLETION_OK)):
            result = self.provider.check(token="abcdef0123456789abcdef0123456789.abcdef0123456789abcdef0123456789")

        self.assertTrue(result.available)

    def test_check_invalid_key_no_auth_header(self):
        with mock.patch(
            "provider.base.chat",
            return_value=(401, '{"error":{"code":"1001","message":"Header 中未收到 Authentication 参数"}}'),
        ):
            result = self.provider.check(token="bad.a")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_invalid_key_auth_failed(self):
        with mock.patch(
            "provider.base.chat",
            return_value=(401, '{"error":{"code":"1000","message":"身份验证失败"}}'),
        ):
            result = self.provider.check(token="bad.a")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_model_not_found_auth_passed(self):
        # 400 code 1211 means auth PASSED but the model is unknown.
        with mock.patch(
            "provider.base.chat",
            return_value=(400, '{"error":{"code":"1211","message":"模型不存在"}}'),
        ):
            result = self.provider.check(token="valid.nomodel")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_check_no_balance(self):
        with mock.patch(
            "provider.base.chat",
            return_value=(429, '{"error":{"code":"1113","message":"余额不足"}}'),
        ):
            result = self.provider.check(token="valid.nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_rate_limited(self):
        with mock.patch(
            "provider.base.chat",
            return_value=(429, '{"error":{"code":"1302","message":"请求过于频繁"}}'),
        ):
            result = self.provider.check(token="valid.limited")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_server_error(self):
        with mock.patch(
            "provider.base.chat",
            return_value=(500, '{"error":{"code":"1234","message":"server error"}}'),
        ):
            result = self.provider.check(token="valid.server")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)


class TestGLMProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = GLMProvider(conditions=[_make_condition()])

    def test_inspect_returns_empty(self):
        # Zhipu has no /models endpoint, so inspect should return [] with no network call.
        models = self.provider.inspect(token="abc.a")
        self.assertEqual(models, [])


class TestGLMRedaction(unittest.TestCase):
    def test_redact_hex_dot_key(self):
        key = "abcdef0123456789abcdef0123456789.abcdef0123456789abcdef0123456789"
        redacted = redact_api_keys_in_text(f"key {key}")
        self.assertNotIn(key, redacted)
        self.assertIn("...", redacted)


class TestGLMLiveCheck(unittest.TestCase):
    """Live integration test - only runs if GLM_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("GLM_LIVE_TEST") == "1", "Set GLM_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check_and_output(self, _ua):
        provider = GLMProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("GLM_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set GLM_TEST_KEYS=key1,key2 to run live validation")

        valid_keys = []
        for key in keys:
            result = provider.check(token=key)
            if result.available:
                valid_keys.append(key)
                print(f"[VALID] {key[:8]}...")
            else:
                print(f"[INVALID] {key[:8]}... reason={result.reason}")

        output_path = os.path.join(os.path.dirname(__file__), "..", "data-glm", "valid-keys.txt")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for key in valid_keys:
                f.write(key + "\n")

        print(f"\nWrote {len(valid_keys)} valid key(s) to {os.path.abspath(output_path)}")


if __name__ == "__main__":
    unittest.main()