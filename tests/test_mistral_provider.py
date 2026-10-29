#!/usr/bin/env python3

"""Unit tests for the Mistral provider (provider/mistral.py).

Pins the judge taxonomy against the wave-0 (2026-10-29) measurements:

  - 401: the measured gate body ``{"detail":"Invalid API Key"}`` -> INVALID_KEY
    (no-key and bad-key both answered 401 in 1.4-1.6 s through both prod socks
    exits, so the models endpoint is a real auth gate);
  - 429: split by body — a rate-limit body stays RATE_LIMITED (retryable), only
    quota/billing markers reach NO_QUOTA;
  - 200 with an error body: split by marker set — auth markers INVALID_KEY,
    quota markers NO_QUOTA, anything else BAD_REQUEST (wait-check);
  - 404 / 422 (and a model-missing 400) -> NO_MODEL / BAD_REQUEST: a retired
    probe id must never condemn a key (its liveness is unverified);
  - empty or unparseable bodies are retryable (SERVER_ERROR), never UNKNOWN
    (CheckStage files UNKNOWN as a permanent invalid discard).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.mistral import MistralProvider
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers

_KEY = "bgtV" "x9Qm" "Pz7L" "rW2n" "Kd8H" "sT4v" "Nc6F" "yJ3p"
_MODEL = "mistral-small-latest"

_COMPLETION_OK = json.dumps(
    {
        "id": "cmpl-mistral",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
)

# Measured wave-0 2026-10-29 (api.mistral.ai/v1/models without a key or with a
# bogus 32-char key — identical body on both socks exits).
_MEASURED_401 = '{"detail":"Invalid API Key"}'

_MODELS_OK = json.dumps({"object": "list", "data": [{"id": _MODEL, "object": "model"}]})

UA_PATCH = "provider.openai_like.get_user_agent"


def _make_condition() -> Condition:
    return Condition(
        query='"MISTRAL_API_KEY"',
        patterns=Patterns(key_pattern=r"[A-Za-z0-9]{32}"),
        description="test",
        enabled=True,
    )


def _patch_chat(code: int, message: str):
    """Patch the single chat probe that AIBaseProvider.check issues."""
    return mock.patch("provider.base.chat", return_value=(code, message))


class TestMistralRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("mistral", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("mistral", conditions=[_make_condition()])
        self.assertIsInstance(provider, MistralProvider)
        self.assertEqual(provider.name, "mistral")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(MistralProvider, OpenAILikeProvider))


class TestMistralCheck(unittest.TestCase):
    def setUp(self):
        self.provider = MistralProvider(conditions=[_make_condition()])
        self._ua = mock.patch(UA_PATCH, return_value="test-agent").start()
        self.addCleanup(mock.patch.stopall)

    def _check(self, code: int, message: str):
        with _patch_chat(code, message):
            return self.provider.check(token=_KEY)

    def test_200_completion_is_valid(self):
        self.assertTrue(self._check(200, _COMPLETION_OK).available)

    def test_measured_401_body_is_invalid_key(self):
        result = self._check(401, _MEASURED_401)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertFalse(result.reason.is_retryable())

    def test_401_empty_body_is_invalid_key(self):
        # Status-first: a 401 condemns the key even with no parseable body.
        self.assertEqual(self._check(401, "").reason, ErrorReason.INVALID_KEY)

    def test_429_rate_limit_body_is_rate_limited(self):
        body = json.dumps(
            {"object": "error", "message": "Requests rate limit exceeded, please try again later.", "type": "rate_limited"}
        )
        result = self._check(429, body)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)
        self.assertTrue(result.reason.is_retryable(), "a throttled key must stay recoverable")

    def test_bare_429_is_rate_limited(self):
        self.assertEqual(self._check(429, "").reason, ErrorReason.RATE_LIMITED)

    def test_429_quota_body_is_no_quota(self):
        body = json.dumps(
            {"object": "error", "message": "You have reached your monthly quota.", "type": "quota_exceeded"}
        )
        self.assertEqual(self._check(429, body).reason, ErrorReason.NO_QUOTA)

    def test_429_billing_marker_is_no_quota(self):
        body = json.dumps({"detail": "Insufficient balance on this account."})
        self.assertEqual(self._check(429, body).reason, ErrorReason.NO_QUOTA)

    def test_402_is_no_quota(self):
        self.assertEqual(self._check(402, '{"detail":"Payment required"}').reason, ErrorReason.NO_QUOTA)

    def test_404_probe_model_is_no_model(self):
        # The probe id's liveness is UNVERIFIED: a retired model must route to
        # wait-check, never to invalid-keys.txt.
        result = self._check(404, '{"detail":"Model not found"}')
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)
        self.assertNotEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_422_unknown_model_is_no_model(self):
        body = json.dumps({"object": "error", "message": "Invalid model: mistral-small-latest", "type": "invalid_model"})
        self.assertEqual(self._check(422, body).reason, ErrorReason.NO_MODEL)

    def test_400_model_missing_is_no_model(self):
        body = json.dumps({"message": "Model does not exist", "type": "model_not_found"})
        self.assertEqual(self._check(400, body).reason, ErrorReason.NO_MODEL)

    def test_400_without_model_marker_is_bad_request(self):
        # A malformed probe is not a key verdict -> wait-check.
        result = self._check(400, '{"detail":"Malformed request"}')
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)
        self.assertNotEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_200_auth_error_body_is_invalid_key(self):
        body = json.dumps({"object": "error", "message": "Invalid API Key", "type": "authentication_error"})
        self.assertEqual(self._check(200, body).reason, ErrorReason.INVALID_KEY)

    def test_200_quota_error_body_is_no_quota(self):
        body = json.dumps({"error": {"message": "Insufficient quota for this workspace"}})
        self.assertEqual(self._check(200, body).reason, ErrorReason.NO_QUOTA)

    def test_200_other_error_body_is_bad_request(self):
        # Transient upstream fault wrapped in a 200: no key verdict possible.
        body = json.dumps({"object": "error", "message": "Upstream timeout", "type": "server_error"})
        self.assertEqual(self._check(200, body).reason, ErrorReason.BAD_REQUEST)

    def test_empty_200_body_is_retryable_not_unknown(self):
        result = self._check(200, "")
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())
        self.assertNotEqual(result.reason, ErrorReason.UNKNOWN)

    def test_unparseable_200_body_is_retryable_not_unknown(self):
        result = self._check(200, "<html>Bad Gateway</html>")
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertNotEqual(result.reason, ErrorReason.UNKNOWN)

    def test_garbage_5xx_body_is_retryable(self):
        self.assertTrue(self._check(502, "<html>offline</html>").reason.is_retryable())

    def test_empty_token_makes_no_request(self):
        with mock.patch("provider.base.chat") as chat:
            result = self.provider.check(token="")
        self.assertFalse(result.available)
        chat.assert_not_called()


class TestMistralProbeShape(unittest.TestCase):
    def setUp(self):
        self._ua = mock.patch(UA_PATCH, return_value="test-agent").start()
        self.addCleanup(mock.patch.stopall)

    def test_probe_uses_default_model_and_bearer_auth(self):
        provider = MistralProvider(conditions=[_make_condition()])
        with mock.patch("provider.base.chat", return_value=(200, _COMPLETION_OK)) as chat:
            provider.check(token=_KEY)

        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["model"], _MODEL)
        self.assertEqual(kwargs["url"], "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer " + _KEY)

    def test_probe_honours_use_proxy(self):
        provider = MistralProvider(conditions=[_make_condition()], use_proxy=True)
        with mock.patch("provider.base.chat", return_value=(200, _COMPLETION_OK)) as chat:
            provider.check(token=_KEY)

        self.assertIs(chat.call_args.kwargs["use_proxy"], True)

    def test_inspect_lists_models(self):
        provider = MistralProvider(conditions=[_make_condition()])
        with mock.patch("provider.openai_like.http_get", return_value=_MODELS_OK):
            self.assertEqual(provider.inspect(token=_KEY), [_MODEL])


if __name__ == "__main__":
    unittest.main()