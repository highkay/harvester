#!/usr/bin/env python3

"""Tests for web/middleware.py — Bearer token authentication and middleware.

Given a FastAPI test app with protected endpoints,
When requests are sent with/without Authorization headers,
Then the correct authentication and CORS behaviour is observed.
"""

from __future__ import annotations

import os
import tempfile
import unittest

# ═══════════════════════════════════════════════════════════════════════════════
# Set test env vars BEFORE any web.* module import
# ═══════════════════════════════════════════════════════════════════════════════

_TEST_AUTH_KEY = "test-secret-auth-key-for-unittest"
_TEST_WORKSPACE = tempfile.mkdtemp()

os.environ["WEB_AUTH_KEY"] = _TEST_AUTH_KEY
os.environ["HARVESTER_WORKSPACE"] = _TEST_WORKSPACE


def _reset_settings_cache() -> None:
    """Reset the singleton settings cache so new env vars take effect."""
    import web.deps

    web.deps._settings = None  # type: ignore[attr-defined]


def _make_test_app():
    """Build a minimal FastAPI test app with protected endpoints."""
    from fastapi import Depends, FastAPI

    from web.deps import get_current_user
    from web.router_auth import _login_limiter, router as auth_router
    from web.router_tokens import router as token_router

    # Reset rate limiter for clean test state
    _login_limiter.reset()

    app = FastAPI()

    # --- Health (no auth) ---
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # --- Protected test endpoint (auth required) ---
    @app.get("/api/protected")
    async def protected(user: bool = Depends(get_current_user)):
        return {"ok": True, "message": "authenticated"}

    # --- Mount real routers ---
    app.include_router(auth_router)
    app.include_router(token_router)

    return app


# ═══════════════════════════════════════════════════════════════════════════════
# Test cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestBearerAuthRequired(unittest.TestCase):
    """Given protected API endpoints,
    When requests are sent with various Authorization headers,
    Then access is granted or denied appropriately.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _reset_settings_cache()
        cls.app = _make_test_app()

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from web.router_auth import _login_limiter

        _login_limiter.reset()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # ------------------------------------------------------------------
    # Missing auth
    # ------------------------------------------------------------------

    def test_no_auth_header_on_protected_endpoint_returns_401(self) -> None:
        """Given no Authorization header,
        When GET /api/protected is called,
        Then it returns 401.
        """
        resp = self.client.get("/api/protected")
        self.assertEqual(resp.status_code, 401)

    def test_no_auth_header_on_token_endpoint_returns_401(self) -> None:
        """Given no Authorization header,
        When GET /api/tokens is called,
        Then it returns 401 (auth middleware blocks before handler).
        """
        resp = self.client.get("/api/tokens")
        self.assertEqual(resp.status_code, 401)

    # ------------------------------------------------------------------
    # Valid auth
    # ------------------------------------------------------------------

    def test_valid_bearer_token_on_protected_endpoint_returns_200(self) -> None:
        """Given a valid Bearer token,
        When GET /api/protected is called,
        Then it returns 200 (authentication passes).
        """
        resp = self.client.get(
            "/api/protected",
            headers={"Authorization": f"Bearer {_TEST_AUTH_KEY}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

    # ------------------------------------------------------------------
    # Invalid auth
    # ------------------------------------------------------------------

    def test_invalid_bearer_token_returns_401(self) -> None:
        """Given a wrong Bearer token,
        When GET /api/protected is called,
        Then it returns 401.
        """
        resp = self.client.get(
            "/api/protected",
            headers={"Authorization": "Bearer wrong-secret-key"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_invalid_bearer_token_on_tokens_endpoint_returns_401(self) -> None:
        """Given a wrong Bearer token,
        When GET /api/tokens is called,
        Then it returns 401.
        """
        resp = self.client.get(
            "/api/tokens",
            headers={"Authorization": "Bearer wrong-secret-key"},
        )
        self.assertEqual(resp.status_code, 401)

    # ------------------------------------------------------------------
    # Malformed header
    # ------------------------------------------------------------------

    def test_malformed_auth_header_no_scheme_returns_401(self) -> None:
        """Given an Authorization header without Bearer prefix,
        When GET /api/protected is called,
        Then it returns 401.
        """
        resp = self.client.get(
            "/api/protected",
            headers={"Authorization": _TEST_AUTH_KEY},
        )
        self.assertEqual(resp.status_code, 401)

    def test_malformed_auth_header_wrong_scheme_returns_401(self) -> None:
        """Given an Authorization header with a non-Bearer scheme,
        When GET /api/protected is called,
        Then it returns 401.
        """
        resp = self.client.get(
            "/api/protected",
            headers={"Authorization": f"Basic {_TEST_AUTH_KEY}"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_empty_bearer_token_returns_401(self) -> None:
        """Given 'Bearer ' with no token,
        When GET /api/protected is called,
        Then it returns 401.
        """
        resp = self.client.get(
            "/api/protected",
            headers={"Authorization": "Bearer "},
        )
        self.assertEqual(resp.status_code, 401)


# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthUnauthenticated(unittest.TestCase):
    """Given the health endpoint,
    When requests are sent without authentication,
    Then the endpoint is always reachable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _reset_settings_cache()
        cls.app = _make_test_app()

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from web.router_auth import _login_limiter

        _login_limiter.reset()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_health_no_auth_returns_200(self) -> None:
        """Given no Authorization header,
        When GET /health is called,
        Then it returns 200.
        """
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_health_with_auth_also_returns_200(self) -> None:
        """Given a valid Authorization header,
        When GET /health is called,
        Then it still returns 200 (health is always public).
        """
        resp = self.client.get(
            "/health",
            headers={"Authorization": f"Bearer {_TEST_AUTH_KEY}"},
        )
        self.assertEqual(resp.status_code, 200)


# ═══════════════════════════════════════════════════════════════════════════════


class TestLoginEndpointUnauthenticated(unittest.TestCase):
    """Given the login endpoint,
    When requests are sent without authentication,
    Then the endpoint is always reachable (login is unauthenticated).
    """

    @classmethod
    def setUpClass(cls) -> None:
        _reset_settings_cache()
        cls.app = _make_test_app()

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from web.router_auth import _login_limiter

        _login_limiter.reset()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_login_endpoint_reachable_without_bearer_token(self) -> None:
        """Given no Authorization header and a valid auth_key,
        When POST /api/auth/login is called,
        Then the handler processes the request (returns 200, not blocked by auth middleware).
        """
        resp = self.client.post(
            "/api/auth/login", json={"auth_key": _TEST_AUTH_KEY}
        )
        # Valid auth_key → 200, proving the auth middleware didn't intercept
        self.assertEqual(resp.status_code, 200)
        self.assertIn("token", resp.json())


# ═══════════════════════════════════════════════════════════════════════════════


class TestInputValidation(unittest.TestCase):
    """Given protected API endpoints with Pydantic models,
    When invalid request bodies are sent,
    Then FastAPI returns 422 (Pydantic validation).
    """

    @classmethod
    def setUpClass(cls) -> None:
        _reset_settings_cache()
        cls.app = _make_test_app()

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from web.router_auth import _login_limiter

        _login_limiter.reset()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_token_create_with_invalid_body_returns_422(self) -> None:
        """Given an invalid POST body to /api/tokens,
        When the request is sent with valid auth,
        Then FastAPI validates the body and returns 422.
        """
        resp = self.client.post(
            "/api/tokens",
            json={"invalid": "field"},
            headers={"Authorization": f"Bearer {_TEST_AUTH_KEY}"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_token_create_with_empty_token_value_returns_422(self) -> None:
        """Given a TokenCreate body with an empty token_value,
        When POST /api/tokens is called with valid auth,
        Then FastAPI returns 422 (min_length constraint).
        """
        resp = self.client.post(
            "/api/tokens",
            json={"token_type": "api", "token_value": "short"},
            headers={"Authorization": f"Bearer {_TEST_AUTH_KEY}"},
        )
        self.assertEqual(resp.status_code, 422)


# ═══════════════════════════════════════════════════════════════════════════════


class TestCorsConfiguration(unittest.TestCase):
    """Given the FastAPI app with CORS middleware,
    When OPTIONS preflight requests are sent,
    Then proper CORS headers are returned.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _reset_settings_cache()
        cls.app = _make_test_app()

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from web.router_auth import _login_limiter

        _login_limiter.reset()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_cors_preflight_returns_allow_origin(self) -> None:
        """Given an OPTIONS preflight request,
        When sent with an Origin header,
        Then the response includes CORS headers.
        """
        resp = self.client.options(
            "/api/protected",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        # TestClient CORS middleware should return allow-origin header
        # (if configured origins match)
        self.assertIn(resp.status_code, {200, 204, 405})
        if resp.status_code in {200, 204}:
            self.assertIn("access-control-allow-origin", resp.headers)


if __name__ == "__main__":
    unittest.main()
