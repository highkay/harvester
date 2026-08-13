#!/usr/bin/env python3

"""Unit tests for the use_proxy flag on search.client HTTP helpers."""

from __future__ import annotations

import unittest
from unittest import mock

import search.client as client


class _FakeResponse:
    """Minimal context-manager response fake standing in for requests.Response."""

    def __init__(self, status_code: int = 200, text: str = "", content: bytes = b""):
        self.status_code = status_code
        self.text = text
        self.content = content
        self._closed = False

    def raise_for_status(self) -> None:
        return None

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def close(self) -> None:
        self._closed = True


class TestClientUseProxy(unittest.TestCase):
    def tearDown(self) -> None:
        # Restore global proxy state so other suites in the run are unaffected.
        client.set_proxy("")

    def test_request_direct_uses_direct_session(self):
        http_session = mock.MagicMock()
        direct_session = mock.MagicMock()
        response = mock.MagicMock()
        http_session.request.return_value = response
        direct_session.request.return_value = response

        with mock.patch.object(client, "_HTTP_SESSION", http_session), mock.patch.object(
            client, "_DIRECT_SESSION", direct_session, create=True
        ):
            client.request("GET", "http://x", use_proxy=False)

        direct_session.request.assert_called_once()
        http_session.request.assert_not_called()

    def test_request_default_uses_proxied_session(self):
        http_session = mock.MagicMock()
        direct_session = mock.MagicMock()
        response = mock.MagicMock()
        http_session.request.return_value = response
        direct_session.request.return_value = response

        with mock.patch.object(client, "_HTTP_SESSION", http_session), mock.patch.object(
            client, "_DIRECT_SESSION", direct_session, create=True
        ):
            client.request("GET", "http://x")

        http_session.request.assert_called_once()
        direct_session.request.assert_not_called()

    def test_chat_forwards_use_proxy(self):
        fake_response = _FakeResponse(status_code=200, text="{}")
        request_mock = mock.MagicMock(return_value=fake_response)

        with mock.patch.object(client, "request", request_mock):
            code, message = client.chat(
                "http://x", {"content-type": "application/json"}, model="m", use_proxy=False
            )

        self.assertEqual(code, 200)
        self.assertEqual(message, "{}")
        request_mock.assert_called_once()
        self.assertIs(request_mock.call_args.kwargs["use_proxy"], False)

    def test_http_get_forwards_use_proxy(self):
        fake_response = _FakeResponse(status_code=200, content=b"{}")
        request_mock = mock.MagicMock(return_value=fake_response)

        with mock.patch.object(client, "request", request_mock):
            content = client.http_get("http://x", use_proxy=False)

        self.assertEqual(content, "{}")
        request_mock.assert_called_once()
        self.assertIs(request_mock.call_args.kwargs["use_proxy"], False)

    def test_direct_session_never_proxied(self):
        client.set_proxy("socks5://127.0.0.1:1080")

        self.assertIn("http", client._HTTP_SESSION.proxies)
        self.assertIn("https", client._HTTP_SESSION.proxies)
        self.assertEqual(client._DIRECT_SESSION.proxies, {})
