#!/usr/bin/env python3

"""Unit tests for the Doubao (Volcengine Ark) provider.

Pins the status-map contract measured 2026-10-29 (wave-0 CN probe pack,
.omo/evidence/magpie-coverage/wave0-cn.md probe b):
  - 404 (unknown deployment / model id) -> NO_MODEL (wait-check, recoverable),
    never INVALID_KEY — a wrong probe id must not condemn the key;
  - 401 AuthenticationError -> INVALID_KEY (the measured bad-key response);
  - 402 / arrearage body -> NO_QUOTA (authentic key without funds);
  - garbage / 5xx -> retryable (SERVER_ERROR family), never a permanent
    discard.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.doubao import DoubaoProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers


def _make_condition() -> Condition:
    return Condition(
        query='"ARK_API_KEY"',
        patterns=Patterns(
            key_pattern=r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        ),
        description="test",
        enabled=True,
    )


def _key() -> str:
    return "7fb7a1c2-4d3e-8f9a-bcde-1234abcdef56"


# Bad-key responses measured 2026-10-29 (wave0-cn.md probe b): no key and a
# format-broken key both answer 401 AuthenticationError.
AUTH_ERROR_401 = json.dumps(
    {
        "error": {
            "code": "AuthenticationError",
            "message": "the API key or AK/SK in the request is missing or invalid",
        }
    }
)

# Ark arrearage / quota-exhausted shapes: 402 with a billing-flavoured body,
# or 429 carrying an insufficient-quota marker.
ARREARAGE_402 = json.dumps(
    {
        "error": {
            "code": "Arrearage",
            "message": "Your account is in arrearage; please recharge and retry",
        }
    }
)
QUOTA_429 = json.dumps({"error": {"code": "RateLimitExceeded", "message": "insufficient_quota"}})

COMPLETION_OK = json.dumps(
    {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
)

CHAT_PATCH = "provider.base.chat"
UA_PATCH = "provider.openai_like.get_user_agent"


class TestDoubaoProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("doubao", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("doubao", conditions=[_make_condition()])
        self.assertIsInstance(provider, DoubaoProvider)
        self.assertEqual(provider.name, "doubao")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(DoubaoProvider, OpenAILikeProvider))

    def test_measured_probe_model_default(self):
        # Wave-0 (2026-10-29) measured doubao-seed-2-1-pro-260915 as the
        # latest seed-2.1-pro id in the pay-as-you-go /api/v3 catalog.
        provider = DoubaoProvider(conditions=[_make_condition()])
        self.assertEqual(provider._default_model, "doubao-seed-2-1-pro-260915")

    def test_ark_paths(self):
        provider = DoubaoProvider(conditions=[_make_condition()])
        self.assertEqual(provider._base_url, "https://ark.cn-beijing.volces.com/")
        self.assertEqual(provider.completion_path, "api/v3/chat/completions")
        self.assertEqual(provider.model_path, "api/v3/models")


class TestDoubaoProviderJudge(unittest.TestCase):
    """Direct _judge contract (no network)."""

    def setUp(self):
        self.provider = DoubaoProvider(conditions=[_make_condition()])

    def test_404_is_no_model_wait(self):
        result = self.provider._judge(
            404, '{"error":{"code":"ModelNotFound","message":"unknown endpoint"}}'
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_401_is_invalid_key(self):
        result = self.provider._judge(401, AUTH_ERROR_401)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_402_arrearage_is_no_quota(self):
        result = self.provider._judge(402, ARREARAGE_402)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_429_quota_body_is_no_quota(self):
        result = self.provider._judge(429, QUOTA_429)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_429_rate_limit_is_retryable(self):
        result = self.provider._judge(429, '{"error":{"message":"rate_limit_exceeded"}}')
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)
        self.assertTrue(result.reason.is_retryable())

    def test_5xx_is_retryable_server_error(self):
        result = self.provider._judge(500, "server error")
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_empty_200_body_is_retryable(self):
        # Transport artefact, not a key verdict: SERVER_ERROR (wait-check),
        # never UNKNOWN (permanent discard).
        result = self.provider._judge(200, "")
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_200_with_error_body_is_not_valid(self):
        result = self.provider._judge(200, '{"error":{"message":"invalid api key"}}')
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


class TestDoubaoProviderCheck(unittest.TestCase):
    """check() through the mocked chat transport."""

    def setUp(self):
        self.provider = DoubaoProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def _check(self, code: int, message: str):
        with mock.patch(CHAT_PATCH, return_value=(code, message)):
            return self.provider.check(token=_key())

    def test_check_404_routes_to_no_model(self):
        result = self._check(404, '{"error":{"code":"ModelNotFound"}}')
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_check_401_routes_to_invalid_key(self):
        result = self._check(401, AUTH_ERROR_401)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_402_arrearage_routes_to_no_quota(self):
        result = self._check(402, ARREARAGE_402)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_completion_ok_valid(self):
        result = self._check(200, COMPLETION_OK)
        self.assertTrue(result.available)

    def test_check_garbage_500_retryable(self):
        result = self._check(502, "bad gateway")
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_check_uses_measured_default_model(self):
        with mock.patch(CHAT_PATCH, return_value=(200, COMPLETION_OK)) as chat_mock:
            self.provider.check(token=_key())
        self.assertEqual(chat_mock.call_args.kwargs.get("model"), "doubao-seed-2-1-pro-260915")


class TestDoubaoLiveCheck(unittest.TestCase):
    """Live integration test - only runs if DOUBAO_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("DOUBAO_LIVE_TEST") == "1", "Set DOUBAO_LIVE_TEST=1 to run live tests")
    def test_live_check(self):
        provider = DoubaoProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("DOUBAO_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set DOUBAO_TEST_KEYS=key1,key2 to run live validation")

        for key in keys:
            result = provider.check(token=key)
            print(f"[{'VALID' if result.available else 'INVALID'}] {key[:6]}... reason={result.reason}")


if __name__ == "__main__":
    unittest.main()
