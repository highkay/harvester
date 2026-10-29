#!/usr/bin/env python3

"""Unit tests for the SiliconFlow provider (provider/siliconflow.py).

Pins the judge taxonomy against the wave-0 (2026-10-29) measurements and the
documented error-code reference (docs.siliconflow.cn "API request error
troubleshooting"):

  - 401: wrong API key -> INVALID_KEY (measured body:
    ``{"code":30014,"data":null,"message":"Token is invalid."}``);
  - 402: account in arrears -> NO_QUOTA;
  - 403: insufficient permission (real-name verification) -> NO_ACCESS
    (wait-check), unless the body itself rejects the key;
  - 404 / 400 code 20012 (model does not exist) -> NO_MODEL (wait-check) —
    a routing failure must never condemn a key;
  - 429: a REFILLABLE rate limit (RPM/RPD/TPM/TPD/IPM/IPD) stays
    RATE_LIMITED; only a balance/arrears marker is NO_QUOTA;
  - an unparseable body is retryable (SERVER_ERROR), never UNKNOWN (which
    CheckStage files as a permanent invalid discard).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.openai_like import OpenAILikeProvider
from provider.registry import ProviderRegistry, get_available_providers
from provider.siliconflow import SiliconFlowProvider

_KEY = "sk-" + "qkvtmzprlxwbnwzchvjdqslrcfgynbakthkrwprszmdxjxvq"
_MODEL = "Qwen/Qwen2.5-7B-Instruct"

_COMPLETION_OK = json.dumps(
    {
        "id": "chatcmpl-sf",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
)

# Measured wave-0 2026-10-29 (api.siliconflow.cn/v1/models without a key or
# with a bogus key — identical 401 body).
_MEASURED_401 = '{"code":30014,"data":null,"message":"Token is invalid."}'

_MODELS_OK = json.dumps({"object": "list", "data": [{"id": _MODEL, "object": "model"}]})

UA_PATCH = "provider.openai_like.get_user_agent"


def _make_condition() -> Condition:
    return Condition(
        query='"SILICONFLOW_API_KEY"',
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{32,}"),
        description="test",
        enabled=True,
    )


def _patch_chat(code: int, message: str):
    """Patch the single chat probe that AIBaseProvider.check issues."""
    return mock.patch("provider.base.chat", return_value=(code, message))


class TestSiliconFlowRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("siliconflow", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("siliconflow", conditions=[_make_condition()])
        self.assertIsInstance(provider, SiliconFlowProvider)
        self.assertEqual(provider.name, "siliconflow")

    def test_is_openai_like_subclass(self):
        self.assertTrue(issubclass(SiliconFlowProvider, OpenAILikeProvider))

    def test_exported_from_provider_package(self):
        import provider as provider_package

        self.assertIn("SiliconFlowProvider", provider_package.__all__)
        self.assertIs(provider_package.SiliconFlowProvider, SiliconFlowProvider)


class TestSiliconFlowCheck(unittest.TestCase):
    def setUp(self):
        self.provider = SiliconFlowProvider(conditions=[_make_condition()])
        self._ua = mock.patch(UA_PATCH, return_value="test-agent").start()
        self.addCleanup(mock.patch.stopall)

    def _check(self, code: int, message: str):
        with _patch_chat(code, message):
            return self.provider.check(token=_KEY)

    def test_200_completion_is_valid(self):
        self.assertTrue(self._check(200, _COMPLETION_OK).available)

    def test_measured_401_body_is_invalid_key(self):
        self.assertEqual(self._check(401, _MEASURED_401).reason, ErrorReason.INVALID_KEY)

    def test_401_empty_body_is_invalid_key(self):
        # Status-first: a 401 condemns the key even with no parseable body.
        self.assertEqual(self._check(401, "").reason, ErrorReason.INVALID_KEY)

    def test_429_rate_limit_body_is_rate_limited(self):
        body = json.dumps({"code": 20015, "message": "Requests rate limit exceeded, please try again later.", "data": None})
        result = self._check(429, body)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)
        self.assertTrue(result.reason.is_retryable(), "a throttled key must stay recoverable")

    def test_429_tpm_marker_is_rate_limited(self):
        body = json.dumps({"code": 20015, "message": "TPM limit reached for this model.", "data": None})
        self.assertEqual(self._check(429, body).reason, ErrorReason.RATE_LIMITED)

    def test_bare_429_is_rate_limited(self):
        self.assertEqual(self._check(429, "").reason, ErrorReason.RATE_LIMITED)

    def test_429_insufficient_balance_is_no_quota(self):
        body = json.dumps({"code": 30001, "message": "Insufficient balance, please top up.", "data": None})
        self.assertEqual(self._check(429, body).reason, ErrorReason.NO_QUOTA)

    def test_429_chinese_balance_marker_is_no_quota(self):
        # Real wire bodies are raw UTF-8, not \uXXXX-escaped.
        body = json.dumps({"code": 30001, "message": "余额不足，请充值", "data": None}, ensure_ascii=False)
        self.assertEqual(self._check(429, body).reason, ErrorReason.NO_QUOTA)

    def test_402_arrears_is_no_quota(self):
        body = json.dumps({"code": 30002, "message": "Account in arrears. Please top up and try again.", "data": None})
        self.assertEqual(self._check(402, body).reason, ErrorReason.NO_QUOTA)

    def test_404_model_is_no_model(self):
        result = self._check(404, '{"code":20012,"message":"Model does not exist.","data":null}')
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_404_unknown_model_is_wait_check_not_invalid(self):
        # The probe-model trap: a stale/withdrawn default_model answers 404 and
        # must route to wait-check (recoverable), never invalid-keys.txt.
        result = self._check(404, "")
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)
        self.assertNotEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_400_model_does_not_exist_is_no_model(self):
        body = '{"code":20012,"message":"Model does not exist. Please check it carefully.","data":null}'
        self.assertEqual(self._check(400, body).reason, ErrorReason.NO_MODEL)

    def test_403_real_name_gate_is_no_access(self):
        body = json.dumps(
            {"code": 30003, "message": "Insufficient permissions: this model requires real-name authentication.", "data": None}
        )
        self.assertEqual(self._check(403, body).reason, ErrorReason.NO_ACCESS)

    def test_403_key_rejection_body_is_invalid_key(self):
        self.assertEqual(self._check(403, '{"code":30014,"message":"Token is invalid."}').reason, ErrorReason.INVALID_KEY)

    def test_unparseable_200_body_is_retryable_not_unknown(self):
        result = self._check(200, "<html>Bad Gateway</html>")
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())
        self.assertNotEqual(result.reason, ErrorReason.UNKNOWN)

    def test_garbage_5xx_body_is_retryable(self):
        self.assertTrue(self._check(502, "<html>offline</html>").reason.is_retryable())

    def test_empty_token_makes_no_request(self):
        with mock.patch("provider.base.chat") as chat:
            result = self.provider.check(token="")
        self.assertFalse(result.available)
        chat.assert_not_called()


class TestSiliconFlowProbeShape(unittest.TestCase):
    def setUp(self):
        self._ua = mock.patch(UA_PATCH, return_value="test-agent").start()
        self.addCleanup(mock.patch.stopall)

    def test_probe_uses_default_model_and_bearer_auth(self):
        provider = SiliconFlowProvider(conditions=[_make_condition()])
        with mock.patch("provider.base.chat", return_value=(200, _COMPLETION_OK)) as chat:
            provider.check(token=_KEY)

        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["model"], _MODEL)
        self.assertEqual(kwargs["url"], "https://api.siliconflow.cn/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer " + _KEY)

    def test_probe_honours_use_proxy_false(self):
        provider = SiliconFlowProvider(conditions=[_make_condition()], use_proxy=False)
        with mock.patch("provider.base.chat", return_value=(200, _COMPLETION_OK)) as chat:
            provider.check(token=_KEY)

        self.assertIs(chat.call_args.kwargs["use_proxy"], False)

    def test_inspect_lists_models(self):
        provider = SiliconFlowProvider(conditions=[_make_condition()])
        with mock.patch("provider.openai_like.http_get", return_value=_MODELS_OK):
            self.assertEqual(provider.inspect(token=_KEY), [_MODEL])


if __name__ == "__main__":
    unittest.main()