#!/usr/bin/env python3

"""Unit tests for the Brave provider (provider/brave.py).

Pins the live-probed zero-cost probe taxonomy mirrored from the tavily-hikari
pool: a q-less ``GET /res/v1/web/search`` answers ``422 VALIDATION`` for an
AUTHENTICATED key (unbilled), ``422 SUBSCRIPTION_TOKEN_INVALID`` for a dead one
(NOT 401), plus the quota / rate-limit / egress-interstitial splits. Also pins
the probe request shape — no ``q`` parameter, key carried in
``X-Subscription-Token`` — and the free ``X-RateLimit-*`` month-window harvest.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.base import AIBaseProvider
from provider.brave import BraveProvider
from provider.registry import ProviderRegistry, get_available_providers
from search.client import http_error_message


# ---------------------------------------------------------------------------
# Response fixtures (envelope shapes from the hikari live-probed taxonomy)
# ---------------------------------------------------------------------------

_TOKEN = "brave-test-subscription-token-0123456789"

_VALIDATION_MISSING_Q = {
    "type": "https://api-dashboard.search.brave.com/app/subscriptions/subscribe",
    "title": "Bad Request",
    "status": 422,
    "code": "VALIDATION",
    "detail": "Request validation failed",
    "meta": {"errors": [{"loc": ["query", "q"], "msg": "field required", "type": "value_error.missing"}]},
    "instance": "/res/v1/web/search",
}

_VALIDATION_TOKEN_HEADER = {
    "type": "https://api-dashboard.search.brave.com/app/subscriptions/subscribe",
    "title": "Bad Request",
    "status": 422,
    "code": "VALIDATION",
    "detail": "Request validation failed",
    "meta": {
        "errors": [
            {
                "loc": ["header", "x-subscription-token"],
                "msg": "value is not a valid subscription token",
                "type": "value_error",
            }
        ]
    },
    "instance": "/res/v1/web/search",
}

# The same VALIDATION answer in the nested envelope the hikari probe reads.
_VALIDATION_NESTED = {
    "error": {
        "code": "VALIDATION",
        "meta": {"errors": [{"loc": ["query", "q"], "msg": "field required"}]},
    }
}

_VALIDATION_NO_META = {"title": "Bad Request", "status": 422, "code": "VALIDATION", "detail": "Request validation failed"}

_SUBSCRIPTION_TOKEN_INVALID = {
    "type": "https://api-dashboard.search.brave.com/app/subscriptions/subscribe",
    "title": "Subscription Token Invalid",
    "status": 422,
    "code": "SUBSCRIPTION_TOKEN_INVALID",
    "detail": "Subscription token is invalid or expired",
    "instance": "/res/v1/web/search",
}

# Measured live from api.search.brave.com (2026-10-15, q-less probe with a
# bogus 32-char token): HTTP 422 and the envelope NESTED under "error", with no
# X-RateLimit-* headers on the error answer.
_MEASURED_SUBSCRIPTION_TOKEN_INVALID = (
    '{"error":{"code":"SUBSCRIPTION_TOKEN_INVALID","detail":"The provided subscription token is '
    'invalid.","meta":{"component":"authentication"},"status":422},"type":"ErrorResponse"}'
)

_QUOTA_LIMITED = {"title": "Payment Required", "status": 402, "code": "QUOTA_LIMITED", "detail": "Quota exceeded"}

_RATE_LIMITED = {"title": "Too Many Requests", "status": 429, "code": "RATE_LIMITED", "detail": "Rate limited"}

_UNAUTHORIZED_ENVELOPE = {"title": "Unauthorized", "status": 401, "code": "UNAUTHORIZED", "detail": "Unauthorized"}

_FORBIDDEN_ENVELOPE = {"error": {"code": "FORBIDDEN", "detail": "Forbidden"}}

# A Cloudflare / geo-block interstitial: 403 with NO Brave envelope.
_CF_HTML = (
    "<!DOCTYPE html>\n<html><head><title>Attention Required! | CloudFlare</title></head>"
    "<body><h1>Sorry, you have been blocked</h1></body></html>"
)

_SEARCH_200 = {"web": {"results": [{"title": "probe", "url": "https://example.com"}]}, "meta": {"searchTime": 0.01}}

# A VALIDATION body whose decisive meta.errors tail sits past the 300-char
# truncation http_error_message applies.
_LONG_VALIDATION = json.dumps(
    {
        "type": "https://api-dashboard.search.brave.com/app/subscriptions/subscribe",
        "title": "Bad Request",
        "status": 422,
        "code": "VALIDATION",
        "detail": "Request validation failed: " + ("padding" * 60),
        "meta": {"errors": [{"loc": ["query", "q"], "msg": "field required", "type": "value_error.missing"}]},
        "instance": "/res/v1/web/search",
    }
)

# Brave advertises the month window positionally: the policy fixes the order
# and Limit/Remaining/Reset are comma lists paired with it.
_MONTH_HEADERS = {
    "X-RateLimit-Policy": "1;w=1, 2000;w=2592000",
    "X-RateLimit-Limit": "1,2000",
    "X-RateLimit-Remaining": "0,1997",
    "X-RateLimit-Reset": "1,1762000000",
}


def _make_condition() -> Condition:
    return Condition(
        query='"BRAVE_API_KEY"',
        patterns=Patterns(key_pattern=r"[A-Za-z0-9_\-]{20,}"),
        description="test",
        enabled=True,
    )


class FakeResponse:
    """Minimal context-manager response for mocking request()."""

    def __init__(self, status_code: int, text: str, headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status_code: int, text: str, headers: dict | None = None) -> requests.exceptions.HTTPError:
    """Build the HTTPError search.client.request raises for a non-2xx answer."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.search.brave.com/res/v1/web/search"
    response._content = text.encode("utf-8")
    if headers:
        response.headers.update(headers)
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


def _patch_probe(response=None, error=None):
    """Patch provider.brave.request, recording every probe call."""
    calls: list = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        if error is not None:
            raise error
        return response

    return mock.patch("provider.brave.request", side_effect=fake), calls


def _provider(retries: int = 2, timeout: int = 5, **kwargs) -> BraveProvider:
    return BraveProvider(conditions=[_make_condition()], retries=retries, timeout=timeout, **kwargs)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestBraveProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("brave", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("brave", conditions=[_make_condition()])
        self.assertIsInstance(provider, BraveProvider)

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(BraveProvider, AIBaseProvider))

    def test_provider_name_and_result_folder(self):
        """The push lane and result files both key off the provider name."""
        provider = _provider()
        self.assertEqual(provider.name, "brave")
        self.assertEqual(provider.result.folder, "brave")

    def test_exported_from_provider_package(self):
        import provider as provider_package

        self.assertIn("BraveProvider", provider_package.__all__)
        self.assertIs(provider_package.BraveProvider, BraveProvider)


# ---------------------------------------------------------------------------
# Probe request shape — zero-cost means NO q parameter
# ---------------------------------------------------------------------------


class TestBraveProbeShape(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def test_probe_is_get_on_the_search_path_without_q(self):
        patcher, calls = _patch_probe(response=FakeResponse(200, json.dumps(_SEARCH_200)))
        with patcher:
            self.provider.check(token=_TOKEN)

        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://api.search.brave.com/res/v1/web/search")
        self.assertNotIn("q=", call["url"])
        self.assertNotIn("?", call["url"])
        self.assertIsNone(call["kwargs"].get("params"))
        self.assertIsNone(call["kwargs"].get("json"))

    def test_probe_carries_key_in_subscription_token_header(self):
        patcher, calls = _patch_probe(response=FakeResponse(200, json.dumps(_SEARCH_200)))
        with patcher:
            self.provider.check(token=_TOKEN)

        headers = calls[0]["kwargs"]["headers"]
        self.assertEqual(headers["X-Subscription-Token"], _TOKEN)
        self.assertNotIn("Authorization", headers)

    def test_probe_is_single_stage(self):
        """One request per validation — the deviation from Tavily's two stages."""
        patcher, calls = _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_MISSING_Q)))
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)

    def test_probe_forwards_timeout_and_proxy_flag(self):
        patcher, calls = _patch_probe(response=FakeResponse(200, json.dumps(_SEARCH_200)))
        with patcher:
            self.provider.check(token=_TOKEN)

        self.assertEqual(calls[0]["kwargs"]["timeout"], 5)
        self.assertTrue(calls[0]["kwargs"]["use_proxy"])

    def test_probe_honours_use_proxy_false(self):
        provider = _provider(use_proxy=False)
        patcher, calls = _patch_probe(response=FakeResponse(200, json.dumps(_SEARCH_200)))
        with patcher:
            provider.check(token=_TOKEN)

        self.assertFalse(calls[0]["kwargs"]["use_proxy"])

    def test_probe_url_follows_address_and_endpoint_override(self):
        patcher, calls = _patch_probe(response=FakeResponse(200, json.dumps(_SEARCH_200)))
        with patcher:
            self.provider.check(token=_TOKEN, address="https://brave.example.com/", endpoint="/res/v1/web/search")

        self.assertEqual(calls[0]["url"], "https://brave.example.com/res/v1/web/search")


# ---------------------------------------------------------------------------
# check() — the live-probed judge taxonomy
# ---------------------------------------------------------------------------


class TestBraveProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def _check(self, response=None, error=None):
        patcher, calls = _patch_probe(response=response, error=error)
        with patcher:
            result = self.provider.check(token=_TOKEN)
        return result, calls

    def test_422_validation_missing_q_is_valid(self):
        """An authenticated key answers 422 about the missing q — unbilled."""
        result, _ = self._check(error=_http_error(422, json.dumps(_VALIDATION_MISSING_Q)))
        self.assertTrue(result.ok)
        self.assertNotIn(_TOKEN, result.message or "")

    def test_422_validation_nested_envelope_is_valid(self):
        """The nested {"error": {...}} envelope classifies the same way."""
        result, _ = self._check(error=_http_error(422, json.dumps(_VALIDATION_NESTED)))
        self.assertTrue(result.ok)

    def test_422_validation_without_meta_is_valid(self):
        result, _ = self._check(error=_http_error(422, json.dumps(_VALIDATION_NO_META)))
        self.assertTrue(result.ok)

    def test_422_validation_without_raised_error_is_valid(self):
        """A 422 delivered as a plain response object judges identically."""
        result, _ = self._check(response=FakeResponse(422, json.dumps(_VALIDATION_MISSING_Q)))
        self.assertTrue(result.ok)

    def test_422_validation_on_token_header_is_invalid(self):
        """The loc split: a complaint about the token header is a dead key."""
        result, _ = self._check(error=_http_error(422, json.dumps(_VALIDATION_TOKEN_HEADER)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertFalse(result.ok)

    def test_422_validation_token_header_loc_is_case_insensitive(self):
        body = json.dumps({"code": "VALIDATION", "meta": {"errors": [{"loc": ["Header", "X-Subscription-Token"]}]}})
        result, _ = self._check(error=_http_error(422, body))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_422_subscription_token_invalid_is_invalid(self):
        """Live-probed: an invalid token is 422 SUBSCRIPTION_TOKEN_INVALID, not 401."""
        result, _ = self._check(error=_http_error(422, json.dumps(_SUBSCRIPTION_TOKEN_INVALID)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_measured_live_envelope_is_invalid(self):
        """The body api.search.brave.com actually answered with (2026-10-15)."""
        result, _ = self._check(error=_http_error(422, _MEASURED_SUBSCRIPTION_TOKEN_INVALID))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_422_garbage_body_is_unknown(self):
        result, _ = self._check(error=_http_error(422, "not json at all <<<garbage>>>"))
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_422_empty_body_is_unknown(self):
        result, _ = self._check(error=_http_error(422, ""))
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_422_unrecognized_code_is_unknown(self):
        body = json.dumps({"status": 422, "code": "SOMETHING_NEW", "detail": "future taxonomy"})
        result, _ = self._check(error=_http_error(422, body))
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_422_long_body_survives_the_300_char_truncation(self):
        """meta.errors sits past 300 chars, so the FULL body must be judged."""
        error = _http_error(422, _LONG_VALIDATION)
        self.assertGreater(len(_LONG_VALIDATION), 300)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(http_error_message(error))

        result, _ = self._check(error=error)
        self.assertTrue(result.ok)

    def test_402_quota_limited_is_no_quota(self):
        result, _ = self._check(error=_http_error(402, json.dumps(_QUOTA_LIMITED)))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_402_without_code_is_no_quota(self):
        result, _ = self._check(error=_http_error(402, ""))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_429_rate_limited_is_rate_limited(self):
        provider = _provider(retries=1)
        patcher, _ = _patch_probe(error=_http_error(429, json.dumps(_RATE_LIMITED)))
        with patcher:
            result = provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_429_without_code_is_rate_limited(self):
        provider = _provider(retries=1)
        patcher, _ = _patch_probe(error=_http_error(429, ""))
        with patcher:
            result = provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_401_with_json_envelope_is_invalid(self):
        result, _ = self._check(error=_http_error(401, json.dumps(_UNAUTHORIZED_ENVELOPE)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_403_with_json_envelope_is_invalid(self):
        result, _ = self._check(error=_http_error(403, json.dumps(_FORBIDDEN_ENVELOPE)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_403_html_interstitial_is_no_access(self):
        """A CF/geo-block page is an exit problem — it must not burn the key."""
        result, _ = self._check(error=_http_error(403, _CF_HTML))
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_401_html_interstitial_is_no_access(self):
        result, _ = self._check(error=_http_error(401, _CF_HTML))
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_401_empty_body_is_no_access(self):
        result, _ = self._check(error=_http_error(401, ""))
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_200_with_json_is_valid(self):
        """Defensive: a q-less probe normally never 200s."""
        result, _ = self._check(response=FakeResponse(200, json.dumps(_SEARCH_200)))
        self.assertTrue(result.ok)

    def test_200_non_json_is_unknown(self):
        result, _ = self._check(response=FakeResponse(200, _CF_HTML))
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_200_empty_body_is_unknown(self):
        result, _ = self._check(response=FakeResponse(200, ""))
        self.assertEqual(result.reason, ErrorReason.UNKNOWN)

    def test_500_is_server_error(self):
        provider = _provider(retries=1)
        patcher, _ = _patch_probe(error=_http_error(500, "Internal Server Error"))
        with patcher:
            result = provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_503_is_server_error(self):
        provider = _provider(retries=1)
        patcher, _ = _patch_probe(error=_http_error(503, '{"title":"Service Unavailable"}'))
        with patcher:
            result = provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_400_is_bad_request(self):
        result, _ = self._check(error=_http_error(400, '{"title":"Bad Request"}'))
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)

    def test_timeout_is_timeout(self):
        provider = _provider(retries=1)
        patcher, _ = _patch_probe(error=requests.exceptions.Timeout())
        with patcher:
            result = provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)
        self.assertTrue(result.reason.is_retryable())

    def test_connection_error_is_network_error(self):
        provider = _provider(retries=1)
        patcher, _ = _patch_probe(error=requests.exceptions.ConnectionError("boom"))
        with patcher:
            result = provider.check(token=_TOKEN)
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)

    def test_transport_failure_uses_the_retry_budget(self):
        patcher, calls = _patch_probe(error=requests.exceptions.ConnectionError("boom"))
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(len(calls), 2)

    def test_empty_token_is_invalid_without_a_request(self):
        patcher, calls = _patch_probe(response=FakeResponse(200, "{}"))
        with patcher:
            result = self.provider.check(token="")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertEqual(calls, [])

    def test_blank_token_is_invalid_without_a_request(self):
        patcher, calls = _patch_probe(response=FakeResponse(200, "{}"))
        with patcher:
            result = self.provider.check(token="   ")
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# _judge_probe — unit level
# ---------------------------------------------------------------------------


class TestJudgeProbe(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def test_transport_sentinel_timeout(self):
        self.assertEqual(self.provider._judge_probe(0, "timeout").reason, ErrorReason.TIMEOUT)

    def test_transport_sentinel_network_error(self):
        self.assertEqual(self.provider._judge_probe(0, "connection reset").reason, ErrorReason.NETWORK_ERROR)

    def test_422_validation_rows(self):
        valid = self.provider._judge_probe(422, json.dumps(_VALIDATION_MISSING_Q))
        invalid = self.provider._judge_probe(422, json.dumps(_VALIDATION_TOKEN_HEADER))
        self.assertTrue(valid.ok)
        self.assertEqual(invalid.reason, ErrorReason.INVALID_KEY)

    def test_error_code_beats_status(self):
        """Brave's own classification keys on the envelope code, not the status."""
        body = json.dumps({"status": 400, "code": "SUBSCRIPTION_TOKEN_INVALID"})
        self.assertEqual(self.provider._judge_probe(400, body).reason, ErrorReason.INVALID_KEY)

    def test_validation_code_off_422_is_unknown(self):
        body = json.dumps(_VALIDATION_MISSING_Q)
        self.assertEqual(self.provider._judge_probe(400, body).reason, ErrorReason.UNKNOWN)

    def test_401_envelope_split(self):
        self.assertEqual(
            self.provider._judge_probe(401, json.dumps(_UNAUTHORIZED_ENVELOPE)).reason, ErrorReason.INVALID_KEY
        )
        self.assertEqual(self.provider._judge_probe(401, _CF_HTML).reason, ErrorReason.NO_ACCESS)

    def test_non_dict_json_200_is_valid(self):
        self.assertTrue(self.provider._judge_probe(200, "[1, 2, 3]").ok)

    def test_status_code_is_recorded_on_failures(self):
        result = self.provider._judge_probe(402, json.dumps(_QUOTA_LIMITED))
        self.assertEqual(result.status_code, 402)


# ---------------------------------------------------------------------------
# inspect() — free month-window quota harvest
# ---------------------------------------------------------------------------


class TestBraveProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def test_inspect_parses_the_month_window(self):
        error = _http_error(422, json.dumps(_VALIDATION_MISSING_Q), headers=_MONTH_HEADERS)
        patcher, calls = _patch_probe(error=error)
        with patcher:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(
            items,
            [
                "quota.limit: 2000",
                "quota.remaining: 1997",
                "quota.reset: 1762000000",
                "quota.window_seconds: 2592000",
                "probe.status_code: 422",
                "probe.error_code: VALIDATION",
            ],
        )
        self.assertEqual(len(calls), 1, "the audit must not cost an extra request")

    def test_inspect_picks_the_month_position_not_the_second_window(self):
        error = _http_error(422, json.dumps(_VALIDATION_MISSING_Q), headers=_MONTH_HEADERS)
        with _patch_probe(error=error)[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertIn("quota.remaining: 1997", items)
        self.assertNotIn("quota.remaining: 0", items)

    def test_inspect_reads_month_headers_from_a_200_response(self):
        response = FakeResponse(200, json.dumps(_SEARCH_200), headers=_MONTH_HEADERS)
        with _patch_probe(response=response)[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertIn("quota.remaining: 1997", items)
        self.assertIn("probe.status_code: 200", items)

    def test_inspect_unlimited_month_plan(self):
        headers = dict(_MONTH_HEADERS, **{"X-RateLimit-Limit": "1,0"})
        with _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_MISSING_Q), headers=headers))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertIn("quota.limit: unlimited", items)

    def test_inspect_without_rate_limit_headers_tolerates_the_absence(self):
        error = _http_error(422, json.dumps(_VALIDATION_MISSING_Q))
        with _patch_probe(error=error)[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(items, ["probe.status_code: 422", "probe.error_code: VALIDATION"])

    def test_inspect_measured_live_error_answer(self):
        """Real wire (2026-10-15): nested 422 envelope, no X-RateLimit-* headers."""
        with _patch_probe(error=_http_error(422, _MEASURED_SUBSCRIPTION_TOKEN_INVALID))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(items, ["probe.status_code: 422", "probe.error_code: SUBSCRIPTION_TOKEN_INVALID"])

    def test_inspect_without_a_month_window_in_the_policy(self):
        headers = {"X-RateLimit-Policy": "1;w=1", "X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "0"}
        with _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_MISSING_Q), headers=headers))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(items, ["probe.status_code: 422", "probe.error_code: VALIDATION"])

    def test_inspect_malformed_header_values_are_skipped(self):
        headers = dict(_MONTH_HEADERS, **{"X-RateLimit-Remaining": "0,not-a-number"})
        with _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_MISSING_Q), headers=headers))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertFalse(any(item.startswith("quota.") for item in items))

    def test_inspect_short_header_lists_are_skipped(self):
        headers = dict(_MONTH_HEADERS, **{"X-RateLimit-Remaining": "0"})
        with _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_MISSING_Q), headers=headers))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertFalse(any(item.startswith("quota.") for item in items))

    def test_inspect_garbage_body_still_reports_the_status(self):
        with _patch_probe(error=_http_error(422, "<html>garbage</html>", headers=_MONTH_HEADERS))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertIn("quota.remaining: 1997", items)
        self.assertEqual(items[-1], "probe.status_code: 422")

    def test_inspect_transport_failure_returns_empty(self):
        provider = _provider(retries=1)
        with _patch_probe(error=requests.exceptions.ConnectionError("boom"))[0]:
            items = provider.inspect(token=_TOKEN)

        self.assertEqual(items, [])

    def test_inspect_empty_token_returns_empty_without_a_request(self):
        patcher, calls = _patch_probe(response=FakeResponse(200, "{}"))
        with patcher:
            items = self.provider.inspect(token="")

        self.assertEqual(items, [])
        self.assertEqual(calls, [])

    def test_inspect_never_echoes_key_material(self):
        """Audit items land on disk: no token, no raw body."""
        error = _http_error(422, json.dumps(_VALIDATION_TOKEN_HEADER), headers=_MONTH_HEADERS)
        with _patch_probe(error=error)[0]:
            items = self.provider.inspect(token=_TOKEN)

        joined = "\n".join(items)
        self.assertNotIn(_TOKEN, joined)
        self.assertNotIn("value_error", joined)
        self.assertNotIn("subscription token", joined.lower())


if __name__ == "__main__":
    unittest.main()
