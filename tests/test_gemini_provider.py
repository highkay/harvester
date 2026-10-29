#!/usr/bin/env python3

"""Unit tests for the Gemini provider (provider/gemini.py).

The provider is pre-existing and unmodified by this landing; these tests pin
the verdict taxonomy that the wave-1 config and the scheduler seed rely on, all
of it measured 2026-10-29 (`.omo/evidence/magpie-coverage/wave0-us.md` §(c)):

  - the check path is the models.list gate (``GET /v1beta/models?pageSize=1``
    with ``x-goog-api-key``), never a chat completion and never a model id;
  - no key -> 403, any bad key -> 400 ``API_KEY_INVALID`` (query and header
    forms alike), never 200 — so the list endpoint is NOT a public trap;
  - a 200 carrying a ``models`` list means valid.

Two classifications are PINNED AS THEY SHIP (provider/gemini.py is out of scope
for this landing) and recorded as risks in docs/agents/providers/gemini.md:

  - a 429 whose body carries ``RESOURCE_EXHAUSTED`` maps to ``NO_QUOTA`` (the
    no-quota bucket), not ``RATE_LIMITED`` — a free-tier per-minute limit is
    therefore filed as quota instead of retryable;
  - an unparseable 200 body maps to ``UNKNOWN`` (CheckStage files UNKNOWN in
    invalid-keys.txt, a permanent discard) rather than a retryable verdict.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.base import AIBaseProvider
from provider.gemini import GeminiProvider
from provider.registry import ProviderRegistry, get_available_providers

_TOKEN = "AIza" "Sy" + "aB3d" "E5fG" "7hI9" "jK1l" "M3nO" "5pQ7" "rS9t" "U1vW" "2x"


# ---------------------------------------------------------------------------
# Response fixtures (body shapes measured 2026-10-29)
# ---------------------------------------------------------------------------

# Measured: bad key -> 400 with reason API_KEY_INVALID.
INVALID_KEY_400 = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "API key not valid. Please pass a valid API key.",
            "errors": [
                {
                    "message": "API key not valid. Please pass a valid API key.",
                    "domain": "global",
                    "reason": "API_KEY_INVALID",
                }
            ],
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_INVALID",
                    "domain": "googleapis.com",
                    "metadata": {"service": "generativelanguage.googleapis.com"},
                }
            ],
        }
    }
)

# Measured: no key -> 403 with the "unregistered callers" message (the wave-0
# evidence quotes the error object up to the message, with no status field).
UNREGISTERED_CALLERS_403 = json.dumps(
    {
        "error": {
            "code": 403,
            "message": "Method doesn't allow unregistered callers (callers without "
                       "established identity). Please use API Key or other form of "
                       "client identification before making this call.",
        }
    }
)

# The same 403 with Google's usual PERMISSION_DENIED status, which the shipped
# provider maps to a key verdict (see the pinned test below).
UNREGISTERED_CALLERS_PERMISSION_DENIED_403 = json.dumps(
    {
        "error": {
            "code": 403,
            "message": "Method doesn't allow unregistered callers (callers without "
                       "established identity). Please use API Key or other form of "
                       "client identification before making this call.",
            "status": "PERMISSION_DENIED",
        }
    }
)

LEAKED_KEY_403 = json.dumps(
    {
        "error": {
            "code": 403,
            "message": "Your API key was reported as leaked. Please use another API key.",
            "status": "PERMISSION_DENIED",
        }
    }
)

# Measured gemini shape for a free-tier limit / quota exhaustion.
RESOURCE_EXHAUSTED_429 = json.dumps(
    {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted (e.g. check quota).",
            "status": "RESOURCE_EXHAUSTED",
        }
    }
)

# A 429 without the quota markers: a plain throttle (status is NOT
# RESOURCE_EXHAUSTED, so the provider falls through to the retryable branch).
RATE_LIMITED_429 = json.dumps(
    {"error": {"code": 429, "message": "Too many requests, please slow down.", "status": "RATE_LIMIT_EXCEEDED"}}
)

MODEL_GONE_404 = json.dumps(
    {"error": {"code": 404, "message": "models/gemini-gone is not found", "status": "NOT_FOUND"}}
)

MODELS_200 = json.dumps(
    {
        "models": [
            {
                "name": "models/gemini-3.5-flash",
                "displayName": "Gemini 3.5 Flash",
                "supportedGenerationMethods": ["generateContent"],
            }
        ]
    }
)


def _make_condition() -> Condition:
    return Condition(
        query='"AIza"',
        patterns=Patterns(key_pattern="AIza[0-9A-Za-z_-]{35}"),
        description="test",
        enabled=True,
    )


class FakeResponse:
    """Minimal context-manager response for mocking request()."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """Build the HTTPError search.client.request raises for a non-2xx answer."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://generativelanguage.googleapis.com/v1beta/models"
    response._content = text.encode("utf-8")
    response.reason = "error"
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


def _provider(retries: int = 1, timeout: int = 5, **kwargs) -> GeminiProvider:
    return GeminiProvider(conditions=[_make_condition()], retries=retries, timeout=timeout, **kwargs)


def _patch(response=None, error=None):
    """Patch provider.gemini.request; a non-2xx answer raises like the real one."""
    calls: list = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        if error is not None:
            raise error
        return response

    return mock.patch("provider.gemini.request", side_effect=fake), calls


def _check(response=None, error=None, **kwargs):
    patcher, calls = _patch(response=response, error=error)
    provider = _provider(**kwargs)
    with mock.patch("provider.gemini.time.sleep", return_value=None), patcher:
        result = provider.check(token=_TOKEN)
    return result, calls


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestGeminiProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("gemini", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("gemini", conditions=[_make_condition()])
        self.assertIsInstance(provider, GeminiProvider)

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(GeminiProvider, AIBaseProvider))

    def test_provider_name_and_result_folder(self):
        provider = _provider()
        self.assertEqual(provider.name, "gemini")
        self.assertEqual(provider.result.folder, "gemini")

    def test_exported_from_provider_package(self):
        import provider as provider_package

        self.assertIn("GeminiProvider", provider_package.__all__)
        self.assertIs(provider_package.GeminiProvider, GeminiProvider)


# ---------------------------------------------------------------------------
# Probe shape — the models.list gate, keyed by x-goog-api-key
# ---------------------------------------------------------------------------


class TestGeminiProbeShape(unittest.TestCase):
    def test_probe_is_get_on_models_with_page_size_one(self):
        _, calls = _check(response=FakeResponse(200, MODELS_200))
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://generativelanguage.googleapis.com/v1beta/models")
        self.assertEqual(call["kwargs"]["params"], {"pageSize": 1})
        self.assertIsNone(call["kwargs"].get("json"))

    def test_probe_carries_key_in_goog_header(self):
        _, calls = _check(response=FakeResponse(200, MODELS_200))
        headers = calls[0]["kwargs"]["headers"]
        self.assertEqual(headers["x-goog-api-key"], _TOKEN)
        self.assertNotIn("Authorization", headers)

    def test_probe_never_sends_a_model_id_or_chat_call(self):
        _, calls = _check(response=FakeResponse(200, MODELS_200))
        call = calls[0]
        self.assertNotIn("/chat/completions", call["url"])
        self.assertNotIn("models/", call["url"])
        self.assertNotIn("model", call["kwargs"].get("params", {}))

    def test_probe_uses_proxy_by_default(self):
        _, calls = _check(response=FakeResponse(200, MODELS_200))
        self.assertTrue(calls[0]["kwargs"]["use_proxy"])

    def test_empty_token_is_invalid_without_a_request(self):
        provider = _provider()
        with mock.patch("provider.gemini.request") as request_mock:
            for token in ("", "   "):
                result = provider.check(token=token)
                self.assertFalse(result.available)
                self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        request_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Verdict taxonomy
# ---------------------------------------------------------------------------


class TestGeminiVerdicts(unittest.TestCase):
    def test_200_with_models_list_is_valid(self):
        result, _ = _check(response=FakeResponse(200, MODELS_200))
        self.assertTrue(result.available)

    def test_400_api_key_invalid_is_invalid_key(self):
        result, _ = _check(error=_http_error(400, INVALID_KEY_400))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertFalse(result.reason.is_retryable())

    def test_400_api_key_invalid_never_lands_in_wait(self):
        # The plan's failure scenario: a 400 API_KEY_INVALID fixture must be a
        # permanent key verdict, not a retryable one.
        result, _ = _check(error=_http_error(400, INVALID_KEY_400))
        self.assertNotEqual(result.reason, ErrorReason.BAD_REQUEST)
        self.assertNotEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_400_expired_key_is_invalid_key(self):
        body = json.dumps({"error": {"code": 400, "message": "API key expired. Please renew the API key."}})
        result, _ = _check(error=_http_error(400, body))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_403_leaked_key_is_invalid_key(self):
        result, _ = _check(error=_http_error(403, LEAKED_KEY_403))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_403_unregistered_callers_without_status_is_not_a_key_verdict(self):
        # A 403 carrying only the unregistered-callers message has no key marker,
        # so it falls through to the base judge's NO_ACCESS (wait-check). The
        # check path never reaches this shape with an empty token: check()
        # rejects that client-side (see test_empty_token_is_invalid_without_a_request).
        result, _ = _check(error=_http_error(403, UNREGISTERED_CALLERS_403))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_403_permission_denied_marker_is_invalid_key_as_shipped(self):
        # PINNED AS SHIPPED: the provider's 403 branch treats PERMISSION_DENIED
        # as a key verdict. Google attaches that status to unregistered-caller
        # 403s as well, so a proxy/edge 403 of this shape would discard the
        # candidate. Recorded as a risk in docs/agents/providers/gemini.md.
        result, _ = _check(error=_http_error(403, UNREGISTERED_CALLERS_PERMISSION_DENIED_403))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_404_model_gone_is_no_model(self):
        result, _ = _check(error=_http_error(404, MODEL_GONE_404))
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_429_resource_exhausted_is_no_quota(self):
        # PINNED AS SHIPPED (see module docstring): the provider maps the
        # measured free-tier RESOURCE_EXHAUSTED body to the NO_QUOTA bucket,
        # never to INVALID_KEY — the key itself is not rejected.
        result, _ = _check(response=FakeResponse(429, RESOURCE_EXHAUSTED_429))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)
        self.assertNotEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_429_plain_throttle_is_rate_limited_and_retryable(self):
        result, _ = _check(response=FakeResponse(429, RATE_LIMITED_429))
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)
        self.assertTrue(result.reason.is_retryable())

    def test_500_is_retryable_server_error(self):
        result, _ = _check(response=FakeResponse(500, "server error"))
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_transport_failure_is_retryable(self):
        for error, expected in (
            (requests.exceptions.ConnectionError("Connection aborted."), ErrorReason.NETWORK_ERROR),
            (requests.exceptions.Timeout("read timeout"), ErrorReason.TIMEOUT),
        ):
            result, _ = _check(error=error)
            self.assertFalse(result.available)
            self.assertEqual(result.reason, expected)
            self.assertTrue(result.reason.is_retryable())

    def test_200_unparseable_body_is_unknown_known_risk(self):
        # PINNED AS SHIPPED (see module docstring): an unparseable 200 body
        # (e.g. a proxy interstitial) is UNKNOWN, which CheckStage files in
        # invalid-keys.txt. Recorded as a risk in
        # docs/agents/providers/gemini.md; provider/gemini.py is out of scope
        # for this landing. Update this test together with that fix.
        for body in ("", "<html><body>Attention Required!</body></html>"):
            result, _ = _check(response=FakeResponse(200, body))
            self.assertFalse(result.available)
            self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_200_without_models_key_is_treated_as_valid(self):
        # The shipped shape check is only "200 + JSON object": an empty model
        # list still authenticates the key (the gate answers 400 for a bad key).
        for body in (json.dumps({"models": []}), json.dumps({})):
            result, _ = _check(response=FakeResponse(200, body))
            self.assertTrue(result.available, body)


if __name__ == "__main__":
    unittest.main()
