#!/usr/bin/env python3

"""Unit tests for Kimi (Moonshot AI) provider."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns
from provider.kimi import KimiProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from tools.patterns import redact_api_keys_in_text


def _make_condition() -> Condition:
    return Condition(
        query='"MOONSHOT_API_KEY"',
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
            {"id": "kimi-k3", "object": "model", "owned_by": "moonshot"},
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
    return mock.patch("provider.kimi.request", return_value=response)


class TestKimiProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("kimi", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("kimi", conditions=[_make_condition()])
        self.assertIsInstance(provider, KimiProvider)
        self.assertEqual(provider.name, "kimi")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(KimiProvider, OpenAILikeProvider))


class TestKimiProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = KimiProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_success(self):
        with _patch_request(FakeResponse(200, MODELS_RESPONSE)):
            result = self.provider.check(token="sk-abcdefghijklmnopqrstuvwxyz123456")

        self.assertTrue(result.available)

    def test_check_invalid_key(self):
        with _patch_request(
            FakeResponse(
                401,
                '{"error":{"message":"Invalid Authentication","type":"invalid_authentication_error"}}',
            )
        ):
            result = self.provider.check(token="sk-invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_incorrect_api_key(self):
        with _patch_request(
            FakeResponse(
                401,
                '{"error":{"message":"Incorrect API key provided","type":"incorrect_api_key_error"}}',
            )
        ):
            result = self.provider.check(token="sk-wrong")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_rate_limited(self):
        with _patch_request(FakeResponse(429, '{"error":{"type":"rate_limit_reached_error"}}')):
            result = self.provider.check(token="sk-limited")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_no_quota(self):
        with _patch_request(FakeResponse(429, '{"error":{"type":"exceeded_current_quota_error"}}')):
            result = self.provider.check(token="sk-valid-nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_server_error(self):
        with _patch_request(FakeResponse(500, "server error")):
            result = self.provider.check(token="sk-servererror")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_models_ok_then_402_no_quota(self):
        # Models gate passes (200) but the chat probe surfaces 402 Payment
        # Required -> zero balance keys must be filtered to NO_QUOTA, not valid.
        probe_402 = '{"error":{"message":"Insufficient Balance","type":"insufficient_quota"}}'
        with mock.patch(
            "provider.kimi.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(402, probe_402)],
        ):
            result = self.provider.check(token="sk-nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_models_ok_then_429_quota_no_quota(self):
        # Some gateways express zero balance as 429 with a quota error body.
        probe_429 = '{"error":{"type":"exceeded_current_quota_error"}}'
        with mock.patch(
            "provider.kimi.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(429, probe_429)],
        ):
            result = self.provider.check(token="sk-nobalance-429")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_models_ok_then_probe_ok_valid(self):
        # Both stages pass -> key is genuinely usable.
        completion_ok = json.dumps(
            {"id": "chatcmpl-1", "object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        )
        with mock.patch(
            "provider.kimi.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(200, completion_ok)],
        ):
            result = self.provider.check(token="sk-withbalance")

        self.assertTrue(result.available)

    def test_check_requests_bypass_proxy(self):
        # Provider validation must never be routed through the GitHub SOCKS5
        # proxy: every outbound request carries use_proxy=False.
        with mock.patch(
            "provider.kimi.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(200, COMPLETION_OK)],
        ) as req_mock:
            result = self.provider.check(token="sk-withbalance")

        self.assertTrue(result.available)
        calls = req_mock.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIs(call.kwargs.get("use_proxy"), False)


class TestKimiProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = KimiProvider(conditions=[_make_condition()])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_models(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=MODELS_RESPONSE):
            models = self.provider.inspect(token="sk-valid")

        self.assertEqual(models, ["kimi-k3"])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_empty(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=""):
            models = self.provider.inspect(token="sk-valid")

        self.assertEqual(models, [])


class TestKimiRedaction(unittest.TestCase):
    def test_redact_sk_key(self):
        key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_api_keys_in_text(f"token {key} here")
        self.assertNotIn(key, redacted)
        self.assertIn("...", redacted)


class TestKimiLiveCheck(unittest.TestCase):
    """Live integration test - only runs if KIMI_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("KIMI_LIVE_TEST") == "1", "Set KIMI_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check_and_output(self, _ua):
        provider = KimiProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("KIMI_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set KIMI_TEST_KEYS=key1,key2 to run live validation")

        valid_keys = []
        for key in keys:
            result = provider.check(token=key)
            if result.available:
                valid_keys.append(key)
                print(f"[VALID] {key[:8]}...")
            else:
                print(f"[INVALID] {key[:8]}... reason={result.reason}")

        output_path = os.path.join(os.path.dirname(__file__), "..", "data-kimi", "valid-keys.txt")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for key in valid_keys:
                f.write(key + "\n")

        print(f"\nWrote {len(valid_keys)} valid key(s) to {os.path.abspath(output_path)}")


if __name__ == "__main__":
    unittest.main()