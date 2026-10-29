#!/usr/bin/env python3

"""Unit tests for the Huawei MaaS provider (provider/huawei_maas.py).

Pins the 2026-10-29 wave-0 CN gate evidence: no key -> 400
ModelArts.81002 ("Failed to get the authorization header."), bad key -> 401
ModelArts.81003 ("Invalid authorization header."). The 400/81002 missing-
header artifact carries no key verdict and must land in the retryable
wait-check bucket (SERVER_ERROR), never INVALID_KEY; 404 -> NO_MODEL
(wait-check); unparseable bodies are retryable, never UNKNOWN.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.huawei_maas import HuaweiMaasProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers


def _make_condition() -> Condition:
    return Condition(
        query='"api.modelarts-maas.com"',
        patterns=Patterns(
            key_pattern=r"[A-Za-z0-9_\-]{20,}",
        ),
        description="test",
        enabled=True,
    )


_TOKEN = "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW"

# Real gate bodies measured 2026-10-29 (wave-0 CN probe pack, prod container).
_NO_KEY_400 = json.dumps(
    {
        "error": {
            "code": "ModelArts.81002",
            "message": "Failed to get the authorization header.",
        },
        "error_code": "ModelArts.81002",
    }
)
_BAD_KEY_401 = json.dumps(
    {
        "error": {
            "code": "ModelArts.81003",
            "message": "Invalid authorization header.",
        },
        "error_code": "ModelArts.81003",
    }
)
_COMPLETION_OK = json.dumps(
    {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
)

UA_PATCH = "provider.openai_like.get_user_agent"
CHAT_PATCH = "provider.base.chat"


class TestHuaweiMaasProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("huawei-maas", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("huawei-maas", conditions=[_make_condition()])
        self.assertIsInstance(provider, HuaweiMaasProvider)
        self.assertEqual(provider.name, "huawei-maas")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(HuaweiMaasProvider, OpenAILikeProvider))

    def test_base_url_is_openai_v1(self):
        provider = HuaweiMaasProvider(conditions=[_make_condition()])
        # The base class normalizes base_url with a trailing slash.
        self.assertEqual(provider._base_url, "https://api.modelarts-maas.com/openai/v1/")
        self.assertEqual(provider.completion_path, "chat/completions")


class TestHuaweiMaasProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = HuaweiMaasProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_bad_key_401_invalid(self):
        # Wave-0 measured: a bad key answers 401 ModelArts.81003.
        with mock.patch(CHAT_PATCH, return_value=(401, _BAD_KEY_401)):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_400_missing_header_artifact_is_retryable(self):
        # Wave-0 measured: a MISSING authorization header answers 400
        # ModelArts.81002. That gate artifact says nothing about the key —
        # it must route to the retryable wait-check bucket (SERVER_ERROR),
        # never the permanent INVALID_KEY discard.
        with mock.patch(CHAT_PATCH, return_value=(400, _NO_KEY_400)):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_check_completion_ok_valid(self):
        with mock.patch(CHAT_PATCH, return_value=(200, _COMPLETION_OK)):
            result = self.provider.check(token=_TOKEN)

        self.assertTrue(result.available)

    def test_check_404_model_gone_routes_to_no_model(self):
        # The probe model id disappeared upstream -> wait-check, not invalid.
        with mock.patch(CHAT_PATCH, return_value=(404, '{"error":{"message":"model not found"}}')):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_check_429_quota_body_no_quota(self):
        body = '{"error":{"message":"insufficient quota","type":"insufficient_quota"}}'
        with mock.patch(CHAT_PATCH, return_value=(429, body)):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_429_rate_limit(self):
        with mock.patch(CHAT_PATCH, return_value=(429, '{"error":{"type":"rate_limit_exceeded"}}')):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_server_error_retryable(self):
        with mock.patch(CHAT_PATCH, return_value=(500, "server error")):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_check_garbage_200_never_unknown(self):
        # Unparseable 200 body: no key verdict possible -> retryable
        # SERVER_ERROR (wait-check), never UNKNOWN (permanent discard).
        with mock.patch(CHAT_PATCH, return_value=(200, "<html>captive portal</html>")):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_garbage_500_body_retryable(self):
        with mock.patch(CHAT_PATCH, return_value=(502, "<html>bad gateway</html>")):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_timeout_retryable(self):
        with mock.patch(CHAT_PATCH, return_value=(0, "timeout")):
            result = self.provider.check(token=_TOKEN)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)

    def test_check_empty_token_is_bad_request_without_a_request(self):
        with mock.patch(CHAT_PATCH, return_value=(200, _COMPLETION_OK)) as chat_mock:
            result = self.provider.check(token="")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)
        chat_mock.assert_not_called()

    def test_check_posts_bearer_to_openai_v1_chat_completions(self):
        with mock.patch(CHAT_PATCH, return_value=(200, _COMPLETION_OK)) as chat_mock:
            result = self.provider.check(token=_TOKEN)

        self.assertTrue(result.available)
        self.assertEqual(chat_mock.call_count, 1)
        kwargs = chat_mock.call_args.kwargs
        self.assertEqual(kwargs["url"], "https://api.modelarts-maas.com/openai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer " + _TOKEN)

    def test_check_requests_use_proxy_false(self):
        # Domestic endpoint: the config sets use_proxy: false.
        provider = HuaweiMaasProvider(conditions=[_make_condition()], use_proxy=False)
        with mock.patch(CHAT_PATCH, return_value=(200, _COMPLETION_OK)) as chat_mock:
            result = provider.check(token=_TOKEN)

        self.assertTrue(result.available)
        self.assertIs(chat_mock.call_args.kwargs.get("use_proxy"), False)


class TestHuaweiMaasLiveCheck(unittest.TestCase):
    """Live integration test - only runs if HUAWEI_MAAS_LIVE_TEST=1 is set."""

    @unittest.skipUnless(
        os.environ.get("HUAWEI_MAAS_LIVE_TEST") == "1", "Set HUAWEI_MAAS_LIVE_TEST=1 to run live tests"
    )
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check(self, _ua):
        provider = HuaweiMaasProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("HUAWEI_MAAS_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set HUAWEI_MAAS_TEST_KEYS=key1,key2 to run live validation")

        for key in keys:
            result = provider.check(token=key)
            print(f"[{'VALID' if result.available else 'INVALID'}] {key[:6]}... reason={result.reason}")


if __name__ == "__main__":
    unittest.main()
