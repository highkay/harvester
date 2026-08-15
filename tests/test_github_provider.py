#!/usr/bin/env python3

"""Unit tests for GitHub token provider."""

from __future__ import annotations

import unittest
from unittest import mock

import requests

from core.enums import ErrorReason
from core.models import Condition, Patterns
from provider.base import AIBaseProvider
from provider.github import GitHubTokenProvider
from provider.registry import ProviderRegistry, get_available_providers

UA_PATCH = "provider.github.get_user_agent"


def _make_condition() -> Condition:
    return Condition(
        query='"GITHUB_TOKEN"',
        patterns=Patterns(
            key_pattern=r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[a-zA-Z0-9_]{20,}",
        ),
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
    return mock.patch("provider.github.request", return_value=response)


def _http_error(status_code: int, text: str) -> requests.exceptions.HTTPError:
    """Build an HTTPError carrying a real response, as raise_for_status does."""
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.github.com/user"
    response._content = text.encode("utf-8")
    return requests.exceptions.HTTPError(f"{status_code} error", response=response)


class TestGitHubProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("github", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("github", conditions=[_make_condition()])
        self.assertIsInstance(provider, GitHubTokenProvider)
        self.assertEqual(provider.name, "github")

    def test_is_base_provider_subclass(self):
        self.assertTrue(issubclass(GitHubTokenProvider, AIBaseProvider))


class TestGitHubProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = GitHubTokenProvider(conditions=[_make_condition()])
        self._ua_patcher = mock.patch(UA_PATCH, return_value="test-agent")
        self._ua_patcher.start()

    def tearDown(self):
        self._ua_patcher.stop()

    def test_check_success(self):
        with _patch_request(FakeResponse(200, "{}")):
            result = self.provider.check(token="ghp_fake-token-for-testing-1234567890")

        self.assertTrue(result.available)

    def test_check_invalid_key(self):
        with _patch_request(FakeResponse(401, '{"message": "Bad credentials"}')):
            result = self.provider.check(token="ghp_invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    def test_check_403_without_rate_limit_is_no_access(self):
        with _patch_request(FakeResponse(403, '{"message": "Resource not accessible by integration"}')):
            result = self.provider.check(token="ghp_forbidden")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_ACCESS)

    def test_check_403_with_rate_limit_body_is_rate_limited(self):
        body = '{"message": "API rate limit exceeded for user ID 123456."}'
        with _patch_request(FakeResponse(403, body)):
            result = self.provider.check(token="ghp_limited")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_429_is_rate_limited(self):
        with _patch_request(FakeResponse(429, '{"message": "API rate limit exceeded"}')):
            result = self.provider.check(token="ghp_429")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    def test_check_server_error(self):
        with _patch_request(FakeResponse(500, "server error")):
            result = self.provider.check(token="ghp_servererror")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_503_is_server_error(self):
        with _patch_request(FakeResponse(503, "service unavailable")):
            result = self.provider.check(token="ghp_503")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)

    def test_check_empty_token_is_bad_request(self):
        with _patch_request(FakeResponse(200, "{}")) as req_mock:
            result = self.provider.check(token="")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.BAD_REQUEST)
        req_mock.assert_not_called()

    def test_check_http_error_401_returns_without_retry(self):
        with mock.patch("provider.github.request", side_effect=_http_error(401, "Bad credentials")) as req_mock:
            result = self.provider.check(token="ghp_invalid")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)
        self.assertEqual(req_mock.call_count, 1)

    def test_check_timeout_after_retries(self):
        with mock.patch("provider.github.request", side_effect=requests.exceptions.Timeout) as req_mock, mock.patch(
            "provider.github.time.sleep"
        ):
            result = self.provider.check(token="ghp_timeout")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.TIMEOUT)
        self.assertEqual(req_mock.call_count, 2)

    def test_check_url_and_headers(self):
        with _patch_request(FakeResponse(200, "{}")) as req_mock:
            result = self.provider.check(token="ghp_token-1234567890")

        self.assertTrue(result.available)

        args, kwargs = req_mock.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "https://api.github.com/user")

        headers = kwargs.get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer ghp_token-1234567890")
        self.assertEqual(headers.get("Accept"), "application/vnd.github+json")
        self.assertEqual(headers.get("X-GitHub-Api-Version"), "2022-11-28")
        self.assertEqual(headers.get("User-Agent"), "test-agent")

    def test_check_custom_address(self):
        with mock.patch("provider.github.request", return_value=FakeResponse(200, "{}")) as req_mock:
            result = self.provider.check(token="ghp_custom", address="https://github.enterprise.local")

        self.assertTrue(result.available)
        self.assertEqual(req_mock.call_args.args[1], "https://github.enterprise.local/user")

    def test_check_custom_base_url(self):
        provider = GitHubTokenProvider(conditions=[_make_condition()], base_url="https://github.example.com")
        with mock.patch("provider.github.request", return_value=FakeResponse(200, "{}")) as req_mock:
            result = provider.check(token="ghp_custom")

        self.assertTrue(result.available)
        self.assertEqual(req_mock.call_args.args[1], "https://github.example.com/user")


class TestGitHubProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = GitHubTokenProvider(conditions=[_make_condition()])

    def test_inspect_empty(self):
        self.assertEqual(self.provider.inspect(token="ghp_valid"), [])


if __name__ == "__main__":
    unittest.main()
