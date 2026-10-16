#!/usr/bin/env python3

"""Unit tests for the Exa provider (provider/exa.py).

Pins the live-probed zero-cost probe taxonomy mirrored from the tavily-hikari
pool: ``POST /contents`` with an EMPTY JSON body answers ``422`` for an
AUTHENTICATED key (unbilled — hikari's pinned probe matrix) and ``401`` with a
FLAT ``{"error": ..., "tag": "INVALID_API_KEY"}`` envelope for a dead one, plus
the prepaid-credit 402 / 429 / 403 splits. Also pins the probe request shape
(POST + /contents + body exactly ``{}`` + ``x-api-key``), the flat-tag reader
that must NOT be brave's nested ``error.code`` reader, the HTTPError delivery
path (``request`` calls ``raise_for_status``, so 4xx arrive as exceptions), and
that ``inspect()`` harvests no quota headers (Exa sends none).
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.base import AIBaseProvider
from provider.exa import ExaProvider, _error_tag
from provider.registry import ProviderRegistry, get_available_providers
from search.client import http_error_message


# ---------------------------------------------------------------------------
# Response fixtures (envelope shapes from the hikari live-probed taxonomy)
# ---------------------------------------------------------------------------

_TOKEN = "exa-test-api-key-0123456789"

# The canonical answer for an AUTHENTICATED key, pinned by hikari's
# src/server/tests/provider_admin_api_keys.rs exa_contents mock: 422 and a flat
# "error" STRING (no tag), because the empty body fails request validation
# AFTER auth and Exa bills only 2xx.
_VALIDATION_EMPTY_BODY = {"error": "body must contain either `ids` or `urls`"}

# The real wire envelope for a dead key (hikari src/bin/mock_exa.rs
# unauthorized_response): requestId + error STRING + FLAT top-level tag.
_INVALID_API_KEY = {"requestId": "mock", "error": "Invalid API key", "tag": "INVALID_API_KEY"}

# The harness shape for a dead key: no tag at all, so the STATUS must decide.
_INVALID_NO_TAG = {"error": "invalid api key"}

_NO_MORE_CREDITS = {"requestId": "mock", "error": "You're out of credits", "tag": "NO_MORE_CREDITS"}

_BUDGET_EXCEEDED = {"requestId": "mock", "error": "Budget exceeded", "tag": "API_KEY_BUDGET_EXCEEDED"}

_TEAM_BUDGET_EXCEEDED = {"requestId": "mock", "error": "Team budget exceeded", "tag": "TEAM_BUDGET_EXCEEDED"}

# A 402 tag the taxonomy does not know: still NO_QUOTA (prepaid, SuccessOnly).
_UNKNOWN_402_TAG = {"requestId": "mock", "error": "Payment required", "tag": "SOMETHING_NEW"}

_RATE_LIMITED = {"requestId": "mock", "error": "Rate limit exceeded", "tag": "RATE_LIMIT_EXCEEDED"}

_FORBIDDEN_ENVELOPE = {"requestId": "mock", "error": "Forbidden", "tag": "FORBIDDEN"}

# A Cloudflare / geo-block interstitial: 403 with NO Exa envelope.
_CF_HTML = (
    "<!DOCTYPE html>\n<html><head><title>Attention Required! | CloudFlare</title></head>"
    "<body><h1>Sorry, you have been blocked</h1></body></html>"
)

# Defensive: an empty-body probe cannot produce a billable 2xx.
_CONTENTS_200 = {"results": [{"id": "https://example.com", "status": "success"}]}

# A 401 body whose decisive "tag" tail sits past the 300-char truncation
# http_error_message applies.
_LONG_INVALID = json.dumps(
    {
        "requestId": "mock",
        "error": "Invalid API key: " + ("padding" * 60),
        "tag": "INVALID_API_KEY",
    }
)

# Brave-style headers Exa never sends: inspect() must ignore them entirely.
_BRAVE_STYLE_HEADERS = {
    "X-RateLimit-Policy": "1;w=1, 2000;w=2592000",
    "X-RateLimit-Limit": "1,2000",
    "X-RateLimit-Remaining": "0,1997",
    "X-RateLimit-Reset": "1,1762000000",
}


def _make_condition() -> Condition:
    return Condition(
        query='"EXA_API_KEY"',
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
    """Build the HTTPError search.client.request raises for a non-2xx answer.

    This is the NORMAL delivery path for the expected 4xx answers: request()
    calls response.raise_for_status() before returning.
    """
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.exa.ai/contents"
    response._content = text.encode("utf-8")
    if headers:
        response.headers.update(headers)
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


def _patch_probe(response=None, error=None):
    """Patch provider.exa.request, recording every probe call."""
    calls: list = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        if error is not None:
            raise error
        return response

    return mock.patch("provider.exa.request", side_effect=fake), calls


def _provider(retries: int = 2, timeout: int = 5, **kwargs) -> ExaProvider:
    return ExaProvider(conditions=[_make_condition()], retries=retries, timeout=timeout, **kwargs)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestExaProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        self.assertIn("exa", get_available_providers())

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("exa", conditions=[_make_condition()])
        self.assertIsInstance(provider, ExaProvider)

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(ExaProvider, AIBaseProvider))

    def test_provider_name_and_result_folder(self):
        """The push lane and result files both key off the provider name."""
        provider = _provider()
        self.assertEqual(provider.name, "exa")
        self.assertEqual(provider.result.folder, "exa")

    def test_default_base_url_and_probe_path(self):
        provider = _provider()
        self.assertEqual(provider._probe_url(), "https://api.exa.ai/contents")

    def test_default_model_is_not_empty(self):
        """base.py raises on an empty default_model."""
        self.assertEqual(_provider()._default_model, "exa-search")

    def test_blank_default_model_override_falls_back_to_the_preset(self):
        """base.py raises on an empty default_model, so extract() keeps the preset."""
        self.assertEqual(ExaProvider(conditions=[_make_condition()], default_model="  ")._default_model, "exa-search")

    def test_exported_from_provider_package(self):
        import provider as provider_package

        self.assertIn("ExaProvider", provider_package.__all__)
        self.assertIs(provider_package.ExaProvider, ExaProvider)


# ---------------------------------------------------------------------------
# Probe request shape — zero-cost means an EMPTY body on /contents
# ---------------------------------------------------------------------------


class TestExaProbeShape(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def _one_call(self, **kwargs):
        patcher, calls = _patch_probe(**kwargs)
        with patcher:
            self.provider.check(token=_TOKEN)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_probe_is_post_on_the_contents_path(self):
        call = self._one_call(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.exa.ai/contents")
        self.assertNotIn("?", call["url"])
        self.assertIsNone(call["kwargs"].get("params"))

    def test_probe_body_is_exactly_the_empty_json_object(self):
        """Any ids/urls entry would make this a BILLABLE contents lookup."""
        call = self._one_call(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        body = call["kwargs"]["json"]
        self.assertEqual(body, {})
        self.assertIsInstance(body, dict)
        self.assertEqual(json.dumps(body), "{}")
        self.assertIsNone(call["kwargs"].get("data"))
        self.assertIsNone(call["kwargs"].get("params"))

    def test_probe_carries_key_in_the_x_api_key_header(self):
        call = self._one_call(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        headers = call["kwargs"]["headers"]
        self.assertEqual(headers["x-api-key"], _TOKEN)
        self.assertNotIn("Authorization", headers)

    def test_probe_declares_a_json_content_type(self):
        call = self._one_call(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        headers = call["kwargs"]["headers"]
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["accept"], "application/json")

    def test_probe_is_single_stage(self):
        """One request per validation — no billable /search follow-up."""
        patcher, calls = _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(call["url"].endswith("/contents") for call in calls))

    def test_probe_forwards_timeout_and_proxy_flag(self):
        call = self._one_call(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        self.assertEqual(call["kwargs"]["timeout"], 5)
        self.assertTrue(call["kwargs"]["use_proxy"])

    def test_probe_honours_use_proxy_false(self):
        provider = _provider(use_proxy=False)
        patcher, calls = _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        with patcher:
            provider.check(token=_TOKEN)

        self.assertFalse(calls[0]["kwargs"]["use_proxy"])

    def test_probe_url_follows_address_and_endpoint_override(self):
        patcher, calls = _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        with patcher:
            self.provider.check(token=_TOKEN, address="https://exa.example.com/", endpoint="/contents")

        self.assertEqual(calls[0]["url"], "https://exa.example.com/contents")


# ---------------------------------------------------------------------------
# check() — the live-probed judge taxonomy
# ---------------------------------------------------------------------------


class TestExaProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def _check(self, response=None, error=None, **kwargs):
        provider = _provider(**kwargs) if kwargs else self.provider
        patcher, calls = _patch_probe(response=response, error=error)
        with patcher:
            result = provider.check(token=_TOKEN)
        return result, calls

    # -- 401: the dead-key verdict, whatever the body looks like -------------

    def test_401_flat_tag_envelope_is_invalid(self):
        """The real wire: flat top-level tag, error is a STRING."""
        result, _ = self._check(error=_http_error(401, json.dumps(_INVALID_API_KEY)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 401)

    def test_401_without_a_tag_is_invalid(self):
        """Status-driven: hikari's harness 401 carries no tag at all."""
        result, _ = self._check(error=_http_error(401, json.dumps(_INVALID_NO_TAG)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_401_unparseable_body_is_invalid(self):
        result, _ = self._check(error=_http_error(401, "not json at all <<<garbage>>>"))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_401_empty_body_is_invalid(self):
        result, _ = self._check(error=_http_error(401, ""))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_401_html_interstitial_is_invalid(self):
        result, _ = self._check(error=_http_error(401, _CF_HTML))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_401_delivered_as_a_plain_response_is_invalid(self):
        result, _ = self._check(response=FakeResponse(401, json.dumps(_INVALID_API_KEY)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_invalid_key_lands_in_the_invalid_bucket_not_wait_check(self):
        """The trap this provider exists to avoid: NO_ACCESS routes to wait-check."""
        result, _ = self._check(error=_http_error(401, json.dumps(_INVALID_API_KEY)))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertNotIn(result.reason, {ErrorReason.NO_ACCESS, ErrorReason.UNKNOWN, ErrorReason.BAD_REQUEST})

    # -- 422 / 400: authenticated, unbilled ---------------------------------

    def test_422_empty_body_validation_error_is_valid(self):
        """CANONICAL: hikari's pinned probe matrix answers 422 for a live key."""
        result, calls = self._check(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(_TOKEN, result.message or "")

    def test_422_without_a_raised_error_is_valid(self):
        """A 422 delivered as a plain response object judges identically."""
        result, _ = self._check(response=FakeResponse(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        self.assertTrue(result.ok)

    def test_422_empty_body_is_valid(self):
        result, _ = self._check(error=_http_error(422, ""))
        self.assertTrue(result.ok)

    def test_422_unparseable_body_is_valid(self):
        """Unparseable body on a pinned 4xx keeps the status-driven mapping."""
        result, _ = self._check(error=_http_error(422, "<html>gateway</html>"))
        self.assertTrue(result.ok)

    def test_422_with_an_unknown_tag_is_still_valid(self):
        """The binding table pins 422 -> success regardless of the tag."""
        body = json.dumps({"error": "validation", "tag": "SOMETHING_NEW"})
        result, _ = self._check(error=_http_error(422, body))
        self.assertTrue(result.ok)

    def test_422_with_an_auth_tag_is_still_valid(self):
        """No tag override on a pinned row: 401 is the only invalid verdict."""
        body = json.dumps({"error": "invalid api key", "tag": "INVALID_API_KEY"})
        result, _ = self._check(error=_http_error(422, body))
        self.assertTrue(result.ok)

    def test_400_is_valid(self):
        """Authenticated and unbilled, same as 422."""
        result, _ = self._check(error=_http_error(400, json.dumps({"error": "bad request"})))
        self.assertTrue(result.ok)

    def test_400_empty_body_is_valid(self):
        result, _ = self._check(error=_http_error(400, ""))
        self.assertTrue(result.ok)

    # -- 402: prepaid credit, permanently dead ------------------------------

    def test_402_no_more_credits_is_no_quota(self):
        result, _ = self._check(error=_http_error(402, json.dumps(_NO_MORE_CREDITS)))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)
        self.assertEqual(result.status_code, 402)

    def test_402_api_key_budget_exceeded_is_no_quota(self):
        result, _ = self._check(error=_http_error(402, json.dumps(_BUDGET_EXCEEDED)))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_402_team_budget_exceeded_is_no_quota(self):
        result, _ = self._check(error=_http_error(402, json.dumps(_TEAM_BUDGET_EXCEEDED)))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_402_with_an_unknown_tag_is_no_quota(self):
        """ANY tag: SuccessOnly recovery means no calendar flip ever refills it."""
        result, _ = self._check(error=_http_error(402, json.dumps(_UNKNOWN_402_TAG)))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_402_empty_body_is_no_quota(self):
        result, _ = self._check(error=_http_error(402, ""))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_402_is_terminal_and_not_retryable(self):
        """Never pushed, never re-probed in a loop."""
        result, calls = self._check(error=_http_error(402, json.dumps(_NO_MORE_CREDITS)))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)
        self.assertFalse(result.reason.is_retryable())
        self.assertEqual(len(calls), 1)

    # -- 429 / 403 ----------------------------------------------------------

    def test_429_rate_limit_tag_is_rate_limited(self):
        result, _ = self._check(error=_http_error(429, json.dumps(_RATE_LIMITED)), retries=1)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)
        self.assertTrue(result.reason.is_retryable())

    def test_429_empty_body_is_rate_limited(self):
        result, _ = self._check(error=_http_error(429, ""), retries=1)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_403_json_envelope_is_no_access(self):
        result, _ = self._check(error=_http_error(403, json.dumps(_FORBIDDEN_ENVELOPE)))
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_403_html_interstitial_is_no_access(self):
        """A CF/geo-block page is an exit problem — it must not burn the key."""
        result, _ = self._check(error=_http_error(403, _CF_HTML))
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_403_empty_body_is_no_access(self):
        result, _ = self._check(error=_http_error(403, ""))
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    # -- 2xx / 5xx / transport ---------------------------------------------

    def test_200_is_valid(self):
        """Defensive: an empty-body probe cannot produce a billable 2xx."""
        result, _ = self._check(response=FakeResponse(200, json.dumps(_CONTENTS_200)))
        self.assertTrue(result.ok)

    def test_200_empty_body_is_valid(self):
        result, _ = self._check(response=FakeResponse(200, ""))
        self.assertTrue(result.ok)

    def test_500_is_server_error(self):
        result, _ = self._check(error=_http_error(500, "Internal Server Error"), retries=1)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)
        self.assertTrue(result.reason.is_retryable())

    def test_503_is_server_error(self):
        result, _ = self._check(error=_http_error(503, '{"error":"unavailable"}'), retries=1)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_timeout_is_timeout(self):
        result, _ = self._check(error=requests.exceptions.Timeout(), retries=1)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)
        self.assertTrue(result.reason.is_retryable())

    def test_connection_error_is_network_error(self):
        result, _ = self._check(error=requests.exceptions.ConnectionError("boom"), retries=1)
        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)

    def test_transport_failure_uses_the_retry_budget(self):
        patcher, calls = _patch_probe(error=requests.exceptions.ConnectionError("boom"))
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)
        self.assertEqual(len(calls), 2)

    def test_retryable_429_uses_the_retry_budget(self):
        patcher, calls = _patch_probe(error=_http_error(429, json.dumps(_RATE_LIMITED)))
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)
        self.assertEqual(len(calls), 2)

    # -- unpinned statuses --------------------------------------------------

    def test_404_without_a_tag_is_no_access(self):
        """Undecidable 4xx goes to the recoverable wait pool, not invalid."""
        result, _ = self._check(error=_http_error(404, json.dumps({"error": "not found"})))
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_404_with_an_auth_tag_is_invalid(self):
        body = json.dumps({"error": "Invalid API key", "tag": "INVALID_API_KEY"})
        result, _ = self._check(error=_http_error(404, body))
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_404_with_a_budget_tag_is_no_quota(self):
        body = json.dumps({"error": "out of credits", "tag": "NO_MORE_CREDITS"})
        result, _ = self._check(error=_http_error(404, body))
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    def test_418_with_a_rate_tag_is_rate_limited(self):
        body = json.dumps({"error": "slow down", "tag": "RATE_LIMIT_EXCEEDED"})
        result, _ = self._check(error=_http_error(418, body), retries=1)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    # -- token hygiene ------------------------------------------------------

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

    def test_token_is_trimmed_before_it_reaches_the_wire(self):
        patcher, calls = _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))
        with patcher:
            self.provider.check(token=f"  {_TOKEN}  ")

        self.assertEqual(calls[0]["kwargs"]["headers"]["x-api-key"], _TOKEN)

    def test_verdicts_never_echo_key_material(self):
        for error in (
            _http_error(401, json.dumps(_INVALID_API_KEY)),
            _http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)),
            _http_error(402, json.dumps(_NO_MORE_CREDITS)),
        ):
            result, _ = self._check(error=error)
            self.assertNotIn(_TOKEN, result.message or "")


# ---------------------------------------------------------------------------
# HTTPError delivery path — the expected 4xx arrive as exceptions
# ---------------------------------------------------------------------------


class TestExaHttpErrorPath(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def test_expected_422_arrives_through_the_http_error_branch(self):
        """request() raises for status, so the happy path IS the except branch."""
        error = _http_error(422, json.dumps(_VALIDATION_EMPTY_BODY))
        patcher, calls = _patch_probe(error=error)
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)

    def test_error_body_is_not_truncated_to_300_chars(self):
        """The tag tail sits past 300 chars; a truncated body loses it."""
        error = _http_error(401, _LONG_INVALID)
        self.assertGreater(len(_LONG_INVALID), 300)
        self.assertNotIn("INVALID_API_KEY", http_error_message(error))

        patcher, _ = _patch_probe(error=error)
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_long_body_tag_survives_on_an_unpinned_status(self):
        """Where the tag IS load-bearing, truncation would flip the verdict."""
        error = _http_error(409, _LONG_INVALID)
        patcher, _ = _patch_probe(error=error)
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_error_without_a_response_object_is_a_network_error(self):
        patcher, _ = _patch_probe(error=requests.exceptions.HTTPError("boom"))
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertEqual(result.reason, ErrorReason.NETWORK_ERROR)

    def test_response_text_access_failure_falls_back_to_the_error_message(self):
        class ExplodingResponse:
            status_code = 401
            reason = "Unauthorized"

            @property
            def text(self):
                raise RuntimeError("no body")

        error = requests.exceptions.HTTPError("401 error", response=ExplodingResponse())
        patcher, _ = _patch_probe(error=error)
        with patcher:
            result = self.provider.check(token=_TOKEN)

        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)


# ---------------------------------------------------------------------------
# _judge_probe / _error_tag — unit level
# ---------------------------------------------------------------------------


class TestJudgeProbe(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def test_transport_sentinel_timeout(self):
        self.assertEqual(self.provider._judge_probe(0, "timeout").reason, ErrorReason.TIMEOUT)

    def test_transport_sentinel_network_error(self):
        self.assertEqual(self.provider._judge_probe(0, "connection reset").reason, ErrorReason.NETWORK_ERROR)

    def test_negative_status_is_a_transport_failure(self):
        self.assertEqual(self.provider._judge_probe(-1, "").reason, ErrorReason.NETWORK_ERROR)

    def test_judge_table_rows(self):
        """Every pinned row of the binding judge table, in one place."""
        expectations = [
            (401, json.dumps(_INVALID_API_KEY), ErrorReason.INVALID_KEY),
            (402, json.dumps(_NO_MORE_CREDITS), ErrorReason.NO_QUOTA),
            (402, "", ErrorReason.NO_QUOTA),
            (429, json.dumps(_RATE_LIMITED), ErrorReason.RATE_LIMITED),
            (403, json.dumps(_FORBIDDEN_ENVELOPE), ErrorReason.NO_ACCESS),
            (403, _CF_HTML, ErrorReason.NO_ACCESS),
            (500, "", ErrorReason.SERVER_ERROR),
            (502, "", ErrorReason.SERVER_ERROR),
        ]
        for code, body, reason in expectations:
            with self.subTest(code=code, body=body[:24]):
                self.assertEqual(self.provider._judge_probe(code, body).reason, reason)

    def test_success_rows(self):
        for code, body in [(422, json.dumps(_VALIDATION_EMPTY_BODY)), (400, ""), (200, "{}"), (204, "")]:
            with self.subTest(code=code):
                self.assertTrue(self.provider._judge_probe(code, body).ok)

    def test_status_code_is_recorded_on_failures(self):
        result = self.provider._judge_probe(402, json.dumps(_NO_MORE_CREDITS))
        self.assertEqual(result.status_code, 402)

    def test_non_dict_json_body_does_not_crash(self):
        self.assertTrue(self.provider._judge_probe(422, "[1, 2, 3]").ok)
        self.assertEqual(self.provider._judge_probe(404, "[1, 2, 3]").reason, ErrorReason.NO_ACCESS)

    def test_null_and_scalar_bodies_do_not_crash(self):
        self.assertEqual(self.provider._judge_probe(404, "null").reason, ErrorReason.NO_ACCESS)
        self.assertEqual(self.provider._judge_probe(404, '"tag"').reason, ErrorReason.NO_ACCESS)

    def test_3xx_is_not_a_key_verdict(self):
        self.assertEqual(self.provider._judge_probe(302, "").reason, ErrorReason.NO_ACCESS)


class TestErrorTagReader(unittest.TestCase):
    def test_reads_the_flat_top_level_tag(self):
        self.assertEqual(_error_tag(_INVALID_API_KEY), "INVALID_API_KEY")

    def test_uppercases_and_trims_the_tag(self):
        self.assertEqual(_error_tag({"tag": "  no_more_credits "}), "NO_MORE_CREDITS")

    def test_does_not_read_braves_nested_error_code(self):
        """The deliberate difference from provider/brave.py's reader."""
        self.assertEqual(_error_tag({"error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}}), "")

    def test_ignores_a_non_string_tag(self):
        self.assertEqual(_error_tag({"tag": 42}), "")
        self.assertEqual(_error_tag({"tag": {"name": "INVALID_API_KEY"}}), "")
        self.assertEqual(_error_tag({"tag": None}), "")

    def test_ignores_a_missing_tag(self):
        self.assertEqual(_error_tag(_VALIDATION_EMPTY_BODY), "")

    def test_ignores_non_dict_bodies(self):
        self.assertEqual(_error_tag(None), "")
        self.assertEqual(_error_tag(["INVALID_API_KEY"]), "")
        self.assertEqual(_error_tag("INVALID_API_KEY"), "")


# ---------------------------------------------------------------------------
# inspect() — probe evidence only, no quota harvest
# ---------------------------------------------------------------------------


class TestExaProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = _provider()

    def test_inspect_reports_status_and_flat_tag(self):
        with _patch_probe(error=_http_error(401, json.dumps(_INVALID_API_KEY)))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(items, ["probe.status_code: 401", "probe.tag: INVALID_API_KEY"])

    def test_inspect_canonical_422_reports_the_status_only(self):
        with _patch_probe(error=_http_error(422, json.dumps(_VALIDATION_EMPTY_BODY)))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(items, ["probe.status_code: 422"])

    def test_inspect_costs_no_extra_request(self):
        patcher, calls = _patch_probe(error=_http_error(402, json.dumps(_NO_MORE_CREDITS)))
        with patcher:
            self.provider.inspect(token=_TOKEN)

        self.assertEqual(len(calls), 1)

    def test_inspect_harvests_no_quota_headers(self):
        """Exa sends no X-RateLimit-*; brave's month-window parser must not be here."""
        error = _http_error(401, json.dumps(_INVALID_API_KEY), headers=_BRAVE_STYLE_HEADERS)
        with _patch_probe(error=error)[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertFalse(any(item.startswith("quota.") for item in items))
        self.assertEqual(items, ["probe.status_code: 401", "probe.tag: INVALID_API_KEY"])

    def test_inspect_200_reports_the_status_only(self):
        with _patch_probe(response=FakeResponse(200, json.dumps(_CONTENTS_200)))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(items, ["probe.status_code: 200"])

    def test_inspect_garbage_body_still_reports_the_status(self):
        with _patch_probe(error=_http_error(429, "<html>garbage</html>"))[0]:
            items = self.provider.inspect(token=_TOKEN)

        self.assertEqual(items, ["probe.status_code: 429"])

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

    def test_inspect_never_echoes_key_material_or_the_raw_body(self):
        """Audit items land on disk: no token, no error prose, no requestId."""
        error = _http_error(401, json.dumps(_INVALID_API_KEY), headers=_BRAVE_STYLE_HEADERS)
        with _patch_probe(error=error)[0]:
            items = self.provider.inspect(token=_TOKEN)

        joined = "\n".join(items)
        self.assertNotIn(_TOKEN, joined)
        self.assertNotIn("Invalid API key", joined)
        self.assertNotIn("mock", joined)
        self.assertNotIn("requestId", joined)


if __name__ == "__main__":
    unittest.main()
