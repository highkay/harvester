#!/usr/bin/env python3

"""Unit tests for Xiaomi MiMo provider."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns
from provider.mimo import MiMoProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from tools.patterns import redact_api_keys_in_text


def _make_condition() -> Condition:
    return Condition(
        query='"MIMO_API_KEY"',
        patterns=Patterns(
            key_pattern=r"(?:tp|sk)-[0-9A-Za-z_-]{20,}",
        ),
        description="test",
        enabled=True,
    )


MODELS_RESPONSE = json.dumps(
    {
        "object": "list",
        "data": [
            {"id": "mimo-v2.5-pro", "object": "model", "owned_by": "xiaomi"},
        ],
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
    return mock.patch("provider.mimo.request", return_value=response)


class TestMiMoProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("mimo", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("mimo", conditions=[_make_condition()])
        self.assertIsInstance(provider, MiMoProvider)
        self.assertEqual(provider.name, "mimo")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(MiMoProvider, OpenAILikeProvider))


class TestMiMoProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = MiMoProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_success(self):
        with _patch_request(FakeResponse(200, MODELS_RESPONSE)):
            result = self.provider.check(token="tp-abcdefghijklmnopqrstuvwxyz123456")

        self.assertTrue(result.available)

    def test_check_invalid_key(self):
        # MiMo returns 401 with type=invalid_key for bad keys
        with _patch_request(
            FakeResponse(
                401,
                '{"error":{"message":"Invalid API Key","type":"invalid_key"}}',
            )
        ):
            result = self.provider.check(token="tp-invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_401_empty_body_still_invalid(self):
        # Status-code-first: 401 with a non-JSON/empty body is still an invalid key
        with _patch_request(FakeResponse(401, "")):
            result = self.provider.check(token="tp-weird")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_rate_limited(self):
        with _patch_request(FakeResponse(429, '{"error":{"type":"rate_limit_reached_error"}}')):
            result = self.provider.check(token="tp-limited")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_no_quota(self):
        with _patch_request(FakeResponse(429, '{"error":{"type":"exceeded_current_quota_error"}}')):
            result = self.provider.check(token="tp-valid-nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_server_error(self):
        with _patch_request(FakeResponse(500, "server error")):
            result = self.provider.check(token="tp-servererror")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_models_ok_then_402_no_quota(self):
        # Models gate passes (200) but the chat probe surfaces 402 Payment
        # Required -> zero balance keys must be filtered to NO_QUOTA, not valid.
        probe_402 = '{"error":{"message":"Insufficient Balance","type":"insufficient_quota"}}'
        with mock.patch(
            "provider.mimo.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(402, probe_402)],
        ):
            result = self.provider.check(token="tp-nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_models_ok_then_429_quota_no_quota(self):
        # Some gateways express zero balance as 429 with a quota error body.
        probe_429 = '{"error":{"type":"exceeded_current_quota_error"}}'
        with mock.patch(
            "provider.mimo.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(429, probe_429)],
        ):
            result = self.provider.check(token="tp-nobalance-429")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_models_ok_then_probe_ok_valid(self):
        # Both stages pass -> key is genuinely usable.
        completion_ok = json.dumps(
            {"id": "chatcmpl-1", "object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        )
        with mock.patch(
            "provider.mimo.request",
            side_effect=[FakeResponse(200, MODELS_RESPONSE), FakeResponse(200, completion_ok)],
        ):
            result = self.provider.check(token="tp-withbalance")

        self.assertTrue(result.available)


class TestMiMoProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = MiMoProvider(conditions=[_make_condition()])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_models(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=MODELS_RESPONSE):
            models = self.provider.inspect(token="tp-valid")

        self.assertEqual(models, ["mimo-v2.5-pro"])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_empty(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=""):
            models = self.provider.inspect(token="tp-valid")

        self.assertEqual(models, [])


class TestMiMoRedaction(unittest.TestCase):
    def test_redact_tp_key(self):
        key = "tp-abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_api_keys_in_text(f"token {key} here")
        self.assertNotIn(key, redacted)
        self.assertIn("...", redacted)

    def test_redact_sk_key(self):
        key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_api_keys_in_text(f"token {key} here")
        self.assertNotIn(key, redacted)


class TestMiMoLiveCheck(unittest.TestCase):
    """Live integration test - only runs if MIMO_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("MIMO_LIVE_TEST") == "1", "Set MIMO_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check_and_output(self, _ua):
        provider = MiMoProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("MIMO_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set MIMO_TEST_KEYS=key1,key2 to run live validation")

        valid_keys = []
        for key in keys:
            result = provider.check(token=key)
            if result.available:
                valid_keys.append(key)
                print(f"[VALID] {key[:8]}...")
            else:
                print(f"[INVALID] {key[:8]}... reason={result.reason}")

        output_path = os.path.join(os.path.dirname(__file__), "..", "data-mimo", "valid-keys.txt")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for key in valid_keys:
                f.write(key + "\n")

        print(f"\nWrote {len(valid_keys)} valid key(s) to {os.path.abspath(output_path)}")


if __name__ == "__main__":
    unittest.main()
