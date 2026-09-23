#!/usr/bin/env python3

"""Finding C regression: chat() must read the 401 body.

Two bugs in the old ``if code != 401`` skip inside ``chat()``'s HTTPError
branch:

1. Provider judges never saw a 401 response body — a body-carried sub-code
   map (glm/Zhipu classifies 401 codes 1000/1001/1003 from the JSON body)
   could never fire, and no future provider could classify 401 bodies.
2. ``message`` was not reset, so a 401 following a 429/5xx attempt returned
   the PREVIOUS attempt's body — a stale body travelling with a fresh status.

Now the body is read for every status; ``output()`` already routes 401 to
debug, so no extra error-log noise. 401 stays in NO_RETRY_ERROR_CODES (the
retry/break control flow is unchanged).
"""

from __future__ import annotations

import unittest
from unittest import mock

import requests

import search.client as client
from core.enums import ErrorReason
from provider.glm import GLMProvider

_GLM_401_BODY = '{"error":{"code":"1001","message":"Header 中未收到 Authentication 参数"}}'
_SERVER_500_BODY = '{"error":{"code":"1234","message":"server error"}}'
_HEADERS = {"content-type": "application/json"}


def _http_error(status_code: int, text: str, reason: str = "ERR") -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://example.invalid/v1/chat/completions"
    response.reason = reason
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


class TestChatReads401Body(unittest.TestCase):
    def test_401_body_is_returned_to_caller(self):
        # Given a transport that answers 401 with a JSON error body
        with mock.patch.object(client, "request", side_effect=_http_error(401, _GLM_401_BODY)):
            # When chat() is called
            code, message = client.chat("https://example.invalid/v1/chat", dict(_HEADERS), model="m")

        # Then the body reaches the caller verbatim (pre-fix: message was None)
        self.assertEqual(401, code)
        self.assertEqual(_GLM_401_BODY, message)

    def test_401_after_5xx_does_not_carry_stale_body(self):
        # Given attempt 1 fails 500 (retryable) and attempt 2 fails 401
        side_effects = [_http_error(500, _SERVER_500_BODY), _http_error(401, _GLM_401_BODY)]
        with mock.patch.object(client, "request", side_effect=side_effects), mock.patch("time.sleep"):
            # When chat() exhausts its retry onto the 401
            code, message = client.chat("https://example.invalid/v1/chat", dict(_HEADERS), model="m")

        # Then the FRESH 401 body is returned — never the stale 500 body
        self.assertEqual((401, _GLM_401_BODY), (code, message))

    def test_non_json_401_falls_back_to_reason_not_stale_message(self):
        with mock.patch.object(client, "request", side_effect=_http_error(401, "<html>denied</html>")):
            code, message = client.chat("https://example.invalid/v1/chat", dict(_HEADERS), model="m")

        # Non-JSON bodies degrade to the HTTP reason (same rule as every other
        # status), never to None or a previous attempt's body.
        self.assertEqual(401, code)
        self.assertEqual("ERR", message)


class TestGlmJudgeSees401Body(unittest.TestCase):
    """The 401 body must reach a provider judge through the real chat() path."""

    def test_glm_judge_receives_the_401_body_from_transport(self):
        # Given a GLM provider probing a transport that answers 401 + Zhipu body
        provider = GLMProvider(conditions=[])
        seen: dict = {}
        original_judge = provider._judge

        def spy(code: int, message: str):
            seen["code"], seen["message"] = code, message
            return original_judge(code, message)

        with mock.patch("provider.openai_like.get_user_agent", return_value="test-agent"), mock.patch.object(
            client, "request", side_effect=_http_error(401, _GLM_401_BODY)
        ), mock.patch.object(provider, "_judge", side_effect=spy):
            # When the provider checks a token (chat() is NOT mocked — the
            # real HTTPError branch runs against the mocked transport)
            result = provider.check(token="dead.beef")

        # Then the judge saw the actual 401 body and classified from it
        self.assertEqual(401, seen["code"])
        self.assertEqual(_GLM_401_BODY, seen["message"])
        self.assertFalse(result.available)
        self.assertEqual(ErrorReason.INVALID_KEY, result.reason)


if __name__ == "__main__":
    unittest.main()
