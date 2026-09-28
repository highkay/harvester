#!/usr/bin/env python3

"""Unit tests for the OpenCode provider (provider/opencode.py).

Mirrors tests/test_agnes_ai_provider.py but adds opencode-specific error.type
parsing tests: the gateway returns 401 for both invalid keys (AuthError) AND
quota exhaustion (CreditsError/MonthlyLimitError/UserLimitError) — only the
error.type field distinguishes them. ModelError is also 401 but maps to
BAD_REQUEST (model validated before auth; pinning glm-5.3 avoids this in
practice).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.base import AIBaseProvider
from provider.opencode import OpenCodeProvider
from provider.registry import ProviderRegistry, get_available_providers

_TOKEN = "sk-opencode0123456789abcdef"

_CHAT_JSON = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}}],
}


def _make_condition() -> Condition:
    return Condition(
        query='"opencode"',
        patterns=Patterns(key_pattern=r"sk-[a-zA-Z0-9]{20}"),
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


def _patch_request(response: FakeResponse):
    return mock.patch("provider.opencode.request", return_value=response)


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """Build an HTTPError carrying a real response, as raise_for_status does."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://opencode.ai/zen/go/v1/chat/completions"
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


def _err_body(error_type: str, message: str = "") -> str:
    """Build an opencode error body: {"type":"error","error":{"type":..,"message":..}}."""
    return json.dumps(
        {"type": "error", "error": {"type": error_type, "message": message}}
    )


class TestOpenCodeProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("opencode", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("opencode", conditions=[_make_condition()])
        self.assertIsInstance(provider, OpenCodeProvider)
        self.assertEqual(provider.name, "opencode")

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(OpenCodeProvider, AIBaseProvider))


class TestOpenCodeProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = OpenCodeProvider(
            conditions=[_make_condition()], retries=2, timeout=5
        )

    def test_check_success_200_chat_json(self):
        with _patch_request(FakeResponse(200, json.dumps(_CHAT_JSON))):
            result = self.provider.check(token=_TOKEN)
        self.assertTrue(result.ok)
        # Success message must never echo the raw key
        self.assertNotIn(_TOKEN, result.message or "")

    def test_check_posts_chat_completion_with_bearer(self):
        with _patch_request(FakeResponse(200, json.dumps(_CHAT_JSON))) as req_mock:
            self.provider.check(token=_TOKEN)
        args = req_mock.call_args
        self.assertEqual(args.args[0], "POST")
        self.assertEqual(
            args.args[1], "https://opencode.ai/zen/go/v1/chat/completions"
        )
        self.assertEqual(
            args.kwargs["headers"].get("Authorization"), f"Bearer {_TOKEN}"
        )
        self.assertEqual(
            args.kwargs["headers"].get("Content-Type"), "application/json"
        )
        payload = json.loads(args.kwargs["data"].decode("utf8"))
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertEqual(payload["stream"], False)
        self.assertEqual(payload["max_tokens"], 1)
        self.assertEqual(payload["messages"], [{"role": "user", "content": "ping"}])

    def test_check_sends_session_header(self):
        """The gateway 400s chat probes without x-opencode-session BEFORE
        judging the key (MissingSessionID, measured on prod 2026-09-28), so
        every check must carry one."""
        with _patch_request(FakeResponse(200, json.dumps(_CHAT_JSON))) as req_mock:
            self.provider.check(token=_TOKEN)
        headers = req_mock.call_args.kwargs["headers"]
        self.assertTrue(
            headers.get("x-opencode-session"), "session header must be present"
        )

    def test_check_401_server_error_shape_invalid_key(self):
        """Routed dead key: 401 {"error":{"type":"server_error","message":
        "Upstream request failed: Invalid credential"}} (no top-level wrapper)
        — measured 2026-09-28 on the real corpus."""
        body = json.dumps(
            {"error": {"type": "server_error",
                       "message": "Upstream request failed: Invalid credential"}}
        )
        with _patch_request(FakeResponse(401, body)):
            result = self.provider.check(token=_TOKEN)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_403_subscription_lapsed_no_access(self):
        """Routed authentic key with lapsed subscription: 403 server_error
        'An active OpenCode Go subscription ...' — auth-valid but plan-gated,
        so NO_ACCESS (wait-check), never INVALID_KEY."""
        body = json.dumps(
            {"error": {"type": "server_error",
                       "message": "Upstream request failed: An active OpenCode "
                                  "Go subscription is required"}}
        )
        with _patch_request(FakeResponse(403, body)):
            result = self.provider.check(token=_TOKEN)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)


    def test_check_200_non_json_unknown(self):
        # Proxies may answer 200 with junk — a bare 200 is NOT proof of validity
        with _patch_request(FakeResponse(200, "<html>ok</html>")):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_check_200_dict_error_is_server_error(self):
        """House precedent (openai_like 2026-09-23): a 200 whose parseable body
        carries a non-empty top-level ``error`` dict is a soft error, NOT an
        accepted key — SERVER_ERROR (retryable -> wait-check), never valid and
        never the permanent-discard bucket."""
        body = json.dumps(
            {"id": "x", "error": {"type": "server_error", "message": "upstream boom"}}
        )
        with _patch_request(FakeResponse(200, body)):
            result = self.provider.check(token=_TOKEN)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable(), "must route to wait-check, not invalid")

    def test_check_200_string_error_is_server_error(self):
        """The native soft-error shape can be a plain STRING ({"error": "..."}),
        exactly as openai_like guards — a string error must not be pooled."""
        body = json.dumps({"error": "model overloaded, retry later"})
        with _patch_request(FakeResponse(200, body)):
            result = self.provider.check(token=_TOKEN)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_check_200_empty_error_string_stays_success(self):
        """An empty/falsy ``error`` is NOT a soft error — the guard only fires
        on a non-empty value, so a normal success body is still accepted."""
        with _patch_request(FakeResponse(200, json.dumps({**_CHAT_JSON, "error": ""}))):
            result = self.provider.check(token=_TOKEN)
        self.assertTrue(result.ok)

    def test_check_200_normal_json_success(self):
        # A clean chat-completion body with no top-level error stays success.
        with _patch_request(FakeResponse(200, json.dumps(_CHAT_JSON))):
            result = self.provider.check(token=_TOKEN)
        self.assertTrue(result.ok)

    def test_check_401_autherror_invalid_key(self):
        error = _http_error(401, _err_body("AuthError", "Invalid API key."))
        with mock.patch("provider.opencode.request", side_effect=error) as req_mock:
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        # 401 is in NO_RETRY_ERROR_CODES — must not retry
        self.assertEqual(req_mock.call_count, 1)

    def test_check_401_creditserror_no_quota(self):
        # opencode returns 401 (not 402) for insufficient balance
        error = _http_error(401, _err_body("CreditsError", "insufficient balance"))
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_401_monthlylimiterror_no_quota(self):
        error = _http_error(401, _err_body("MonthlyLimitError", "monthly limit"))
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_401_userlimiterror_no_quota(self):
        error = _http_error(401, _err_body("UserLimitError", "user limit"))
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_check_401_modelerror_bad_request(self):
        # Model validated BEFORE auth — classify as BAD_REQUEST, not INVALID_KEY
        error = _http_error(
            401, _err_body("ModelError", "Model foo is not supported")
        )
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_check_401_no_error_type_defaults_invalid_key(self):
        # A bare 401 without a parseable error.type defaults to INVALID_KEY
        error = _http_error(401, "Forbidden")
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_403_no_access(self):
        error = _http_error(403, _err_body("RegionError", "geo-blocked"))
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_check_429_rate_limited(self):
        error = _http_error(429, _err_body("RateLimitError", "rate limited"))
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_400_bad_request(self):
        error = _http_error(400, '{"error": "bad request"}')
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_check_500_server_error(self):
        error = _http_error(500, "Internal Server Error")
        with mock.patch("provider.opencode.request", side_effect=error):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_timeout(self):
        with mock.patch(
            "provider.opencode.request", side_effect=requests.exceptions.Timeout()
        ):
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)

    def test_check_network_error_after_retries(self):
        with mock.patch(
            "provider.opencode.request",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ) as req_mock:
            result = self.provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(req_mock.call_count, 2)

    def test_check_empty_token_invalid(self):
        result = self.provider.check(token="")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_whitespace_token_invalid(self):
        result = self.provider.check(token="   ")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


class TestOpenCodeProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = OpenCodeProvider(
            conditions=[_make_condition()], retries=1, timeout=5
        )

    def test_inspect_returns_model_ids(self):
        body = {
            "object": "list",
            "data": [{"id": "glm-5.3"}, {"id": "glm-5.3-flash"}],
        }
        with _patch_request(FakeResponse(200, json.dumps(body))):
            models = self.provider.inspect(token=_TOKEN)
        self.assertEqual(models, ["glm-5.3", "glm-5.3-flash"])

    def test_inspect_non_200_returns_empty(self):
        with _patch_request(FakeResponse(401, _err_body("AuthError", "invalid"))):
            models = self.provider.inspect(token=_TOKEN)
        self.assertEqual(models, [])

    def test_inspect_200_non_json_returns_empty(self):
        with _patch_request(FakeResponse(200, "<html>ok</html>")):
            models = self.provider.inspect(token=_TOKEN)
        self.assertEqual(models, [])

    def test_inspect_empty_token_returns_empty(self):
        models = self.provider.inspect(token="")
        self.assertEqual(models, [])


if __name__ == "__main__":
    unittest.main()
