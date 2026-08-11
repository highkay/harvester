#!/usr/bin/env python3

"""Tests for web/router_auth.py — login endpoint and rate limiting.

Given a FastAPI test app with the auth router mounted,
When requests are sent to POST /api/auth/login,
Then valid credentials return a token, invalid return 401, and rate limiting applies.
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
    """Build a minimal FastAPI test app with auth router + protected endpoint."""
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


class TestLoginEndpoint(unittest.TestCase):
    """Given a running test app,
    When POST /api/auth/login is called,
    Then behaviour matches expected auth + rate-limit contract.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Build app once per class."""
        _reset_settings_cache()
        cls.app = _make_test_app()

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from web.router_auth import _login_limiter

        # Reset rate limiter per test for isolation
        _login_limiter.reset()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_login_with_valid_auth_key_returns_token(self) -> None:
        """Given a valid auth_key, When POST /api/auth/login is called,
        Then it returns 200 with a token field.
        """
        resp = self.client.post("/api/auth/login", json={"auth_key": _TEST_AUTH_KEY})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("token", data)
        self.assertEqual(data["token"], _TEST_AUTH_KEY)

    # ------------------------------------------------------------------
    # Invalid credentials
    # ------------------------------------------------------------------

    def test_login_with_invalid_auth_key_returns_401(self) -> None:
        """Given a wrong auth_key, When POST /api/auth/login is called,
        Then it returns 401.
        """
        resp = self.client.post("/api/auth/login", json={"auth_key": "wrong-key"})
        self.assertEqual(resp.status_code, 401)

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def test_login_without_body_returns_422(self) -> None:
        """Given no request body, When POST /api/auth/login is called,
        Then FastAPI returns 422 (Pydantic validation).
        """
        resp = self.client.post("/api/auth/login", content=b"")
        self.assertEqual(resp.status_code, 422)

    def test_login_with_empty_auth_key_returns_422(self) -> None:
        """Given an empty auth_key string, When POST /api/auth/login is called,
        Then FastAPI returns 422 (min_length constraint).
        """
        resp = self.client.post("/api/auth/login", json={"auth_key": ""})
        self.assertEqual(resp.status_code, 422)

    def test_login_with_missing_auth_key_field_returns_422(self) -> None:
        """Given a body without the auth_key field, When POST /api/auth/login is called,
        Then FastAPI returns 422.
        """
        resp = self.client.post("/api/auth/login", json={"other": "value"})
        self.assertEqual(resp.status_code, 422)

    # ------------------------------------------------------------------
    # Login endpoint is unauthenticated
    # ------------------------------------------------------------------

    def test_login_endpoint_reachable_without_bearer_token(self) -> None:
        """Given no Authorization header and a valid auth_key,
        When POST /api/auth/login is called,
        Then the handler processes the request (returns 200, not blocked by auth middleware).
        """
        resp = self.client.post(
            "/api/auth/login",
            json={"auth_key": _TEST_AUTH_KEY},
        )
        # Valid auth_key → 200, proving the auth middleware didn't intercept
        self.assertEqual(resp.status_code, 200)
        self.assertIn("token", resp.json())

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def test_login_rate_limiting_blocks_after_6_sequential_attempts(self) -> None:
        """Given a rapid sequence of 6 login attempts from the same IP,
        When the 6th attempt is made within the 60-second window,
        Then the 6th response is 429 (Too Many Requests).
        """
        for i in range(1, 7):
            resp = self.client.post(
                "/api/auth/login", json={"auth_key": "wrong-key"}
            )
            if i <= 5:
                # First 5 should be 401 (wrong password, but not rate-limited)
                self.assertEqual(
                    resp.status_code,
                    401,
                    f"Attempt {i}: expected 401, got {resp.status_code}",
                )
            else:
                # 6th should be 429
                self.assertEqual(
                    resp.status_code,
                    429,
                    f"Attempt {i}: expected 429, got {resp.status_code}",
                )

    def test_login_rate_limiting_counts_per_ip(self) -> None:
        """Given login attempts from a client IP,
        When 5 failed attempts are made,
        Then the 6th attempt is rate-limited (429).

        Uses X-Forwarded-For to simulate different IPs.
        """
        # Simulate IP-A: 1 success + 4 failures = 5 OK attempts total
        for _ in range(5):
            resp = self.client.post(
                "/api/auth/login",
                json={"auth_key": "wrong-key"},
                headers={"X-Forwarded-For": "192.0.2.100"},
            )
            self.assertEqual(resp.status_code, 401)
        # 6th from IP-A → 429
        resp = self.client.post(
            "/api/auth/login",
            json={"auth_key": "wrong-key"},
            headers={"X-Forwarded-For": "192.0.2.100"},
        )
        self.assertEqual(resp.status_code, 429)

        # IP-B (different IP): still allowed
        resp = self.client.post(
            "/api/auth/login",
            json={"auth_key": "wrong-key"},
            headers={"X-Forwarded-For": "192.0.2.200"},
        )
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
