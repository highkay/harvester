#!/usr/bin/env python3

"""Unit tests for the CommandCode provider.

Pins the 2026-10-29 wave-0 trap verdict: ``GET /provider/v1/models`` returns
the same 18,461-byte 200 for no-key and bad-key, so a models-200 proves
NOTHING — the judge must never consult it and must validate via a minimal
chat completion (``max_tokens=1``); a bad key answers 401 ``UNAUTHORIZED``.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.commandcode import CommandCodeProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers


def _make_condition() -> Condition:
    return Condition(
        query='"COMMANDCODE_API_KEY"',
        patterns=Patterns(
            key_pattern=r"[A-Za-z0-9_\-]{20,}",
        ),
        description="test",
        enabled=True,
    )


# Real bad-key response measured 2026-10-29 (wave-0 CN probe pack).
UNAUTHORIZED_401 = json.dumps(
    {
        "error": {
            "message": "Invalid 'Authorization' header or token.",
            "type": "authentication_error",
            "code": "UNAUTHORIZED",
        }
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


class TestCommandCodeProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("commandcode", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("commandcode", conditions=[_make_condition()])
        self.assertIsInstance(provider, CommandCodeProvider)
        self.assertEqual(provider.name, "commandcode")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(CommandCodeProvider, OpenAILikeProvider))

    def test_base_url_uses_provider_v1(self):
        provider = CommandCodeProvider(conditions=[_make_condition()])
        # The base class normalizes base_url with a trailing slash.
        self.assertEqual(provider._base_url, "https://api.commandcode.ai/provider/v1/")
        self.assertEqual(provider.completion_path, "chat/completions")


class TestCommandCodeProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = CommandCodeProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_never_consults_models_endpoint(self):
        # The models list is a public trap (same 18,461-byte 200 for no-key
        # and bad-key): the judge must go straight to the chat probe. Any GET
        # to /models during check() fails this test.
        seen_urls = []

        def spy(method, url, **kwargs):
            seen_urls.append((method, url))
            return FakeResponse(200, COMPLETION_OK)

        with mock.patch("provider.commandcode.request", side_effect=spy):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertTrue(result.available)
        self.assertEqual(len(seen_urls), 1)
        method, url = seen_urls[0]
        self.assertEqual(method, "POST")
        self.assertIn("/chat/completions", url)
        self.assertNotIn("/models", url)

    def test_check_bad_key_401_invalid(self):
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(401, UNAUTHORIZED_401),
        ):
            result = self.provider.check(token="deadbeefdeadbeefdeadbeefdeadbeef")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_completion_ok_valid(self):
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(200, COMPLETION_OK),
        ):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertTrue(result.available)

    def test_check_429_quota_body_no_quota(self):
        body = '{"error":{"message":"insufficient quota","type":"insufficient_quota"}}'
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(429, body),
        ):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_429_rate_limit(self):
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(429, '{"error":{"type":"rate_limit_exceeded"}}'),
        ):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_404_model_gone_routes_to_no_model(self):
        # The probe model id disappeared upstream -> wait-check, not invalid.
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(404, '{"error":{"message":"model not found"}}'),
        ):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_check_server_error_retryable(self):
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(500, "server error"),
        ):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_check_garbage_200_never_unknown(self):
        # Unparseable 200 body: no key verdict possible -> retryable
        # SERVER_ERROR (wait-check), never UNKNOWN (permanent discard).
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(200, "<html>captive portal</html>"),
        ):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_timeout_retryable(self):
        import requests

        with mock.patch(
            "provider.commandcode.request",
            side_effect=requests.exceptions.Timeout(),
        ):
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)

    def test_check_requests_use_proxy_default_true(self):
        with mock.patch(
            "provider.commandcode.request",
            return_value=FakeResponse(200, COMPLETION_OK),
        ) as req_mock:
            result = self.provider.check(token="aB3dE5fG7hI9jK1lM3nO5pQ7")

        self.assertTrue(result.available)
        for call in req_mock.call_args_list:
            self.assertIs(call.kwargs.get("use_proxy"), True)


class TestCommandCodeLiveCheck(unittest.TestCase):
    """Live integration test - only runs if COMMANDCODE_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("COMMANDCODE_LIVE_TEST") == "1", "Set COMMANDCODE_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check(self, _ua):
        provider = CommandCodeProvider(conditions=[_make_condition()])

        keys = [k.strip() for k in os.environ.get("COMMANDCODE_TEST_KEYS", "").split(",") if k.strip()]
        if not keys:
            self.skipTest("Set COMMANDCODE_TEST_KEYS=key1,key2 to run live validation")

        for key in keys:
            result = provider.check(token=key)
            print(f"[{'VALID' if result.available else 'INVALID'}] {key[:6]}... reason={result.reason}")


if __name__ == "__main__":
    unittest.main()
