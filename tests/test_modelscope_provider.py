#!/usr/bin/env python3

"""Unit tests for Alibaba ModelScope provider."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns
from provider.modelscope import ModelScopeProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from tools.patterns import redact_api_keys_in_text


def _make_condition() -> Condition:
    return Condition(
        query='"MODELSCOPE_API_KEY"',
        patterns=Patterns(
            key_pattern=r"[0-9A-Za-z_-]{20,}",
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
        self.provider = ModelScopeProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_success(self):
        with _patch_request(FakeResponse(200, COMPLETION_OK)):
            result = self.provider.check(token="abcdefghijklmnopqrstuvwxyz123456")

        self.assertTrue(result.available)

    def test_check_invalid_key(self):
        # ModelScope chat probe returns 401 + "Authentication failed" for bad keys.
        with _patch_request(
            FakeResponse(
                401,
                '{"error":{"message":"Authentication failed, please make sure that a valid ModelScope token is supplied."}}',
            )
        ):
            result = self.provider.check(token="invalid-token")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_401_empty_body_still_invalid(self):
        with _patch_request(FakeResponse(401, "")):
            result = self.provider.check(token="weird")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_402_no_quota(self):
        with _patch_request(FakeResponse(402, '{"error":{"message":"Insufficient balance"}}')):
            result = self.provider.check(token="nobalance")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_429_quota_no_quota(self):
        with _patch_request(FakeResponse(429, '{"error":{"message":"Quota exceeded","code":"QuotaExceeded"}}')):
            result = self.provider.check(token="nobalance-429")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_429_rate_limited(self):
        with _patch_request(FakeResponse(429, '{"error":{"type":"rate_limit_exceeded"}}')):
            result = self.provider.check(token="limited")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_400_arrearage_no_quota(self):
        # Alibaba-style zero-balance fallback: 400 + arrearage wording.
        with _patch_request(
            FakeResponse(
                400,
                '{"code":"Arrearage","message":"Access denied, please make sure your account is in good standing."}',
            )
        ):
            result = self.provider.check(token="arrearage")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_server_error(self):
        with _patch_request(FakeResponse(500, "server error")):
            result = self.provider.check(token="servererror")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)


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
