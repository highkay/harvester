#!/usr/bin/env python3

"""Pin provider HTTP-status -> ErrorReason mappings (no network, mocked responses).

Every mapping here decides which result bucket a candidate key lands in via
stage/definition.py CheckStage routing:

- available          -> valid-keys.txt
- NO_QUOTA           -> no-quota-keys.txt
- NO_MODEL / NO_ACCESS / BAD_REQUEST / is_retryable()
                     -> wait-check-keys.txt (recoverable)
- INVALID_KEY / UNKNOWN / everything else
                     -> invalid-keys.txt (permanent discard)

A provider that fabricates INVALID_KEY on a transport failure (TLS EOF,
timeout) or on a routing failure (404 wrong model/deployment) permanently
burns live keys, which is exactly what these tests guard against.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from provider.anthropic import AnthropicProvider
from provider.azure import AzureOpenAIProvider
from provider.doubao import DoubaoProvider
from provider.openai_like import OpenAILikeProvider
from provider.openrouter import OpenRouterProvider
from provider.qianfan import QianFanProvider
from provider.stabilityai import StabilityAIProvider
from provider.vertex import VertexProvider

# Key-looking literals are built by concatenation (repo convention) so secret
# scanners never see a complete fake credential in one piece.
_STABILITY_KEY = "sk-" + "stabilitystatusmap0123456789"
_CLAUDE_SESSION_KEY = "sk-ant-" + "sid01-" + "sessionstatusmap0123456789"
_OPENROUTER_KEY = "sk-or-" + "v1-" + "openrouterstatusmap0123456789"
_VERTEX_TOKEN = "ya29." + "vertexstatusmap0123456789"


class FakeResponse:
    """Minimal response mock: context-manager (anthropic/openrouter) and
    close()-able (stabilityai)."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://example.invalid/probe"
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


# ---------------------------------------------------------------------------
# stabilityai: transport failures must stay retryable, only HTTP 401/403-style
# statuses may condemn the key (fix: retry loop used to start at code=401 and
# swallow every non-HTTPError with `except Exception: pass`).
# ---------------------------------------------------------------------------


class TestStabilityAIStatusMap(unittest.TestCase):
    def setUp(self):
        self.provider = StabilityAIProvider(conditions=[], retries=1, timeout=5)
        for target in ("provider.stabilityai.time.sleep", "provider.stabilityai.get_user_agent"):
            mock.patch(target, return_value="test-agent").start()
        self.addCleanup(mock.patch.stopall)

    def test_connection_error_is_network_error_not_invalid(self):
        with mock.patch(
            "provider.stabilityai.request",
            side_effect=requests.exceptions.ConnectionError("tls eof"),
        ):
            result = self.provider.check(token=_STABILITY_KEY)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_timeout_is_timeout_not_invalid(self):
        with mock.patch(
            "provider.stabilityai.request",
            side_effect=requests.exceptions.Timeout(),
        ):
            result = self.provider.check(token=_STABILITY_KEY)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)
        self.assertTrue(result.reason.is_retryable())

    def test_http_401_is_invalid_key(self):
        with mock.patch(
            "provider.stabilityai.request",
            side_effect=_http_error(401, '{"errors":[{"name":"unauthorized"}]}'),
        ):
            result = self.provider.check(token=_STABILITY_KEY)

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_http_500_is_server_error(self):
        with mock.patch(
            "provider.stabilityai.request",
            side_effect=_http_error(500, "Internal Server Error"),
        ):
            result = self.provider.check(token=_STABILITY_KEY)

        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_http_200_is_success(self):
        with mock.patch(
            "provider.stabilityai.request",
            return_value=FakeResponse(200, '{"finishReason":"SUCCESS","artifacts":[]}'),
        ):
            result = self.provider.check(token=_STABILITY_KEY)

        self.assertTrue(result.available)


# ---------------------------------------------------------------------------
# anthropic: Claude session-token path — transport failure -> NETWORK_ERROR,
# 200 non-JSON body -> UNKNOWN, INVALID_KEY only for a real 401 or an
# "Invalid authorization" body.
# ---------------------------------------------------------------------------


class TestAnthropicSessionStatusMap(unittest.TestCase):
    def setUp(self):
        self.provider = AnthropicProvider(conditions=[], retries=1, timeout=5)
        for target in ("provider.anthropic.time.sleep", "provider.anthropic.get_user_agent"):
            mock.patch(target, return_value="test-agent").start()
        self.addCleanup(mock.patch.stopall)

    def test_transport_failure_is_network_error_not_invalid(self):
        with mock.patch(
            "provider.anthropic.request",
            side_effect=requests.exceptions.ConnectionError("connection reset"),
        ):
            result = self.provider.check(token=_CLAUDE_SESSION_KEY)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_200_non_json_body_is_unknown_not_invalid(self):
        with mock.patch(
            "provider.anthropic.request",
            return_value=FakeResponse(200, "<html>interstitial page</html>"),
        ):
            result = self.provider.check(token=_CLAUDE_SESSION_KEY)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_200_org_list_is_success(self):
        body = json.dumps([{"name": "Test Org", "capabilities": ["claude_pro"]}])
        with mock.patch("provider.anthropic.request", return_value=FakeResponse(200, body)):
            result = self.provider.check(token=_CLAUDE_SESSION_KEY)

        self.assertTrue(result.available)

    def test_401_is_invalid_key(self):
        with mock.patch(
            "provider.anthropic.request",
            side_effect=_http_error(401, '{"error":{"message":"unauthorized"}}'),
        ):
            result = self.provider.check(token=_CLAUDE_SESSION_KEY)

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_403_invalid_authorization_body_is_invalid_key(self):
        body = json.dumps({"error": {"message": "Invalid authorization: session rejected"}})
        with mock.patch("provider.anthropic.request", side_effect=_http_error(403, body)):
            result = self.provider.check(token=_CLAUDE_SESSION_KEY)

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


# ---------------------------------------------------------------------------
# azure: EVERY 404 is a deployment-routing failure -> NO_MODEL (wait-check).
# 401 stays INVALID_KEY.
# ---------------------------------------------------------------------------


class TestAzureStatusMap(unittest.TestCase):
    def setUp(self):
        self.provider = AzureOpenAIProvider(conditions=[])

    def test_404_any_body_is_no_model(self):
        result = self.provider._judge(404, "not found, deployment whatever is absent")
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_404_canonical_body_is_no_model(self):
        result = self.provider._judge(404, "The API deployment for this resource does not exist")
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_404_empty_body_is_no_model(self):
        result = self.provider._judge(404, "")
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_401_is_invalid_key(self):
        result = self.provider._judge(401, '{"error":{"code":"401"}}')
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


# ---------------------------------------------------------------------------
# doubao / qianfan: 404 (unknown deployment / model route) -> NO_MODEL.
# ---------------------------------------------------------------------------


class TestDoubaoQianfanStatusMap(unittest.TestCase):
    def test_doubao_404_is_no_model(self):
        provider = DoubaoProvider(conditions=[])
        result = provider._judge(404, '{"error":{"code":"ModelNotFound","message":"unknown endpoint"}}')
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_doubao_401_is_invalid_key(self):
        provider = DoubaoProvider(conditions=[])
        result = provider._judge(401, "")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_qianfan_404_is_no_model(self):
        provider = QianFanProvider(conditions=[])
        result = provider._judge(404, '{"error_code":"NOT_FOUND"}')
        self.assertEqual(result.reason, ErrorReason.NO_MODEL)

    def test_qianfan_401_is_invalid_key(self):
        provider = QianFanProvider(conditions=[])
        result = provider._judge(401, "")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


# ---------------------------------------------------------------------------
# openrouter key-info endpoint: 401 -> INVALID_KEY, 403 -> NO_ACCESS (policy/
# region restriction on an authentic key), 200 accepted only when at least one
# known key-info field is present.
# ---------------------------------------------------------------------------


class TestOpenRouterKeyInfoStatusMap(unittest.TestCase):
    def setUp(self):
        self.provider = OpenRouterProvider(conditions=[], retries=1, timeout=5)
        # check() builds headers via openai_like._get_headers -> get_user_agent
        mock.patch("provider.openai_like.get_user_agent", return_value="test-agent").start()
        self.addCleanup(mock.patch.stopall)

    def test_200_with_known_key_info_field_is_success(self):
        body = json.dumps({"data": {"label": "harvester", "usage": 0, "limit": None, "limit_remaining": None}})
        result = self.provider._judge_key_info(200, body)
        self.assertTrue(result.available)

    def test_200_empty_data_is_unknown_not_success(self):
        result = self.provider._judge_key_info(200, '{"data":{}}')
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_200_missing_data_key_is_unknown_not_success(self):
        result = self.provider._judge_key_info(200, '{}')
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_200_exhausted_limit_remaining_is_no_quota(self):
        result = self.provider._judge_key_info(200, '{"data":{"label":"k","limit_remaining":0}}')
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_401_is_invalid_key(self):
        self.assertEqual(self.provider._judge_key_info(401, '{"error":{}}').reason, ErrorReason.INVALID_KEY)

    def test_403_is_no_access(self):
        self.assertEqual(self.provider._judge_key_info(403, '{"error":{}}').reason, ErrorReason.NO_ACCESS)

    def test_check_via_http_403_returns_no_access(self):
        with mock.patch(
            "provider.openrouter.request",
            side_effect=_http_error(403, '{"error":{"message":"forbidden in region"}}'),
        ):
            result = self.provider.check(token=_OPENROUTER_KEY)

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_check_via_http_200_empty_body_is_unknown(self):
        with mock.patch("provider.openrouter.request", return_value=FakeResponse(200, '{"data":{}}')):
            result = self.provider.check(token=_OPENROUTER_KEY)

        self.assertEqual(result.reason, ErrorReason.UNKNOWN)


# ---------------------------------------------------------------------------
# openai_like: a 200 carrying an error object fails the check, classified by
# body content — auth-flavoured markers are a definite key verdict
# (INVALID_KEY), quota/billing markers mean an authentic key without funds
# (NO_QUOTA), and everything else (transient upstream/model faults from
# wrapper gateways) is NOT a key verdict -> BAD_REQUEST -> recoverable
# wait-check. The success body is deliberately NOT required to contain
# `choices` (some OpenAI-compatible gateways return unusual success shapes).
# ---------------------------------------------------------------------------


class TestOpenAILikeStatusMap(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAILikeProvider(
            conditions=[],
            name="probe",
            base_url="https://example.invalid/v1",
            default_model="probe-model",
        )

    def test_200_auth_marker_error_body_is_invalid_key(self):
        # An auth rejection is a definite key verdict: fail -> invalid-keys.txt.
        # (Also the body the old `type`-field gate used to let pass as VALID.)
        body = '{"error":{"message":"Incorrect API key","type":"invalid_request_error"}}'
        result = self.provider._judge(200, body)
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_200_auth_marker_variants_are_invalid_key(self):
        for message in (
            "invalid api key provided",
            "Unauthorized",
            "Authentication failed for this token",
            "The api key not valid on this gateway",
        ):
            with self.subTest(message=message):
                result = self.provider._judge(200, json.dumps({"error": {"message": message}}))
                self.assertFalse(result.available)
                self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_200_quota_error_body_is_no_quota(self):
        for message in (
            "You exceeded your current quota, please check your plan",
            "insufficient_quota",
            "billing_not_active",
            "credits exhausted",
            "credit balance is too low",
        ):
            with self.subTest(message=message):
                result = self.provider._judge(200, json.dumps({"error": {"message": message}}))
                self.assertFalse(result.available)
                self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_200_generic_error_body_is_bad_request_not_invalid(self):
        # A transient upstream/model fault answered as HTTP 200 + error JSON is
        # not a key verdict; the blanket-INVALID_KEY behaviour permanently
        # burned live keys here. BAD_REQUEST routes to the recoverable
        # wait-check bucket instead.
        result = self.provider._judge(200, '{"error":{"message":"upstream gateway exploded"}}')
        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_200_chat_completion_is_success(self):
        body = json.dumps({"id": "chatcmpl-1", "choices": [{"message": {"role": "assistant", "content": "hi"}}]})
        result = self.provider._judge(200, body)
        self.assertTrue(result.available)

    def test_200_nonstandard_success_body_stays_valid(self):
        # No `choices` requirement: unusual-but-error-free 200 bodies from
        # OpenAI-compatible gateways must not regress to UNKNOWN.
        result = self.provider._judge(200, '{"result":"ok","status":"fine"}')
        self.assertTrue(result.available)

    def test_200_non_json_is_unknown(self):
        result = self.provider._judge(200, "not-json")
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)


# ---------------------------------------------------------------------------
# vertex: inspect() must collect model ids into a separate list — appending to
# the iterated raw-response list re-visited its own appended strings, aborted
# via except and left dicts in the result (later crashing `set(models)`).
# ---------------------------------------------------------------------------


class TestVertexInspectCollection(unittest.TestCase):
    def setUp(self):
        self.provider = VertexProvider(conditions=[])

    def test_inspect_collects_model_ids_from_publishers(self):
        payload = json.dumps(
            {
                "models": [
                    {
                        "name": "projects/p/locations/global/publishers/google/models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                    },
                    {"name": "short-name", "displayName": "Fallback Name"},
                ]
            }
        )
        with mock.patch("provider.vertex.http_get", return_value=payload):
            models = self.provider.inspect(token=_VERTEX_TOKEN, address="", endpoint="test-project")

        self.assertEqual(models, sorted(["gemini-2.5-pro", "Fallback Name"]))
        self.assertTrue(all(isinstance(m, str) for m in models))

    def test_inspect_general_fallback_collects_ids(self):
        fallback = json.dumps({"models": [{"name": "projects/p/locations/global/models/model-one"}]})
        with mock.patch("provider.vertex.http_get", side_effect=[""] * 8 + [fallback]):
            models = self.provider.inspect(token=_VERTEX_TOKEN, address="", endpoint="test-project")

        self.assertEqual(models, ["model-one"])


if __name__ == "__main__":
    unittest.main()
