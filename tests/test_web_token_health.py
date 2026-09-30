#!/usr/bin/env python3

"""TDD tests for the GitHub token health feature (web layer).

Three pieces, each given a failing-first test here:

A. Schema — ``github_tokens.expires_at`` exists on fresh and legacy DBs.
B. Add-time validation — ``TokenService._validate_token_gh`` + single/bulk add.
D. Surface — list/stats/UI carry ``expires_at``.

The GitHub ``GET /user`` probe is always mocked (no network in tests).
Runtime quarantine (tools/state, tools/credential, search/client) lives in
``tests/test_token_health_runtime.py``.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests


def _run_async(coro):
    return asyncio.run(coro)


def _temp_db_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "test_token_health.db")


def _resp(status: int, headers: dict | None = None, content: bytes = b"{}"):
    """Build a minimal fake requests.Response."""
    fake = MagicMock()
    fake.status_code = status
    fake.headers = headers or {}
    fake.content = content
    try:
        fake.text = content.decode("utf-8")
    except Exception:
        fake.text = ""
    fake.reason = ""
    return fake


# ---------------------------------------------------------------------------
# A. Schema
# ---------------------------------------------------------------------------


class TestSchemaExpiresAt(unittest.TestCase):
    """A. github_tokens gains a nullable expires_at column."""

    def test_fresh_db_has_expires_at_column(self) -> None:
        from web.db import get_db, init_db

        async def _scenario() -> None:
            db_path = _temp_db_path()
            await init_db(db_path)
            db = await get_db(db_path)
            try:
                cur = await db.execute("PRAGMA table_info(github_tokens)")
                cols = [row["name"] for row in await cur.fetchall()]
            finally:
                await db.close()
            self.assertIn("expires_at", cols)

        _run_async(_scenario())

    def test_migration_adds_expires_at_to_legacy_db(self) -> None:
        """A pre-existing table without the column gets ALTER-ed at startup."""
        from web.db import get_db, init_db

        db_path = _temp_db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE github_tokens ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "token_type TEXT NOT NULL, token_encrypted TEXT NOT NULL, "
                "token_hash TEXT NOT NULL UNIQUE, label TEXT DEFAULT '', "
                "enabled INTEGER NOT NULL DEFAULT 1, "
                "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            conn.commit()
        finally:
            conn.close()

        async def _scenario() -> None:
            await init_db(db_path)
            db = await get_db(db_path)
            try:
                cur = await db.execute("PRAGMA table_info(github_tokens)")
                cols = [row["name"] for row in await cur.fetchall()]
            finally:
                await db.close()
            self.assertIn("expires_at", cols)

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# B. Add-time validation — _validate_token_gh
# ---------------------------------------------------------------------------


class TestValidateTokenGh(unittest.TestCase):
    """B. _validate_token_gh status matrix (requests.get mocked)."""

    def _svc(self):
        from web.token_service import TokenService

        return TokenService(_temp_db_path())

    def test_200_valid_returns_expiry_header(self) -> None:
        svc = self._svc()
        expiry = "2026-12-31 23:59:59 UTC"
        with patch(
            "web.token_service.requests.get",
            return_value=_resp(200, {"github-authentication-token-expiration": expiry}),
        ) as mock_get:
            status, expires_at = svc._validate_token_gh("ghp_alive_token_ABCDEF")
        self.assertEqual(status, "valid")
        self.assertEqual(expires_at, expiry)
        # Bearer auth + api.github.com/user
        _, kwargs = mock_get.call_args
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer ghp_alive_token_ABCDEF"
        )
        self.assertIn("api.github.com/user", mock_get.call_args.args[0])

    def test_200_without_expiry_header_is_classic_pat(self) -> None:
        svc = self._svc()
        with patch("web.token_service.requests.get", return_value=_resp(200)):
            status, expires_at = svc._validate_token_gh("ghp_classic_pat_ABCDEF")
        self.assertEqual(status, "valid")
        self.assertIsNone(expires_at)

    def test_401_invalid(self) -> None:
        svc = self._svc()
        with patch("web.token_service.requests.get", return_value=_resp(401)):
            status, expires_at = svc._validate_token_gh("ghp_dead_token_ABCDEF")
        self.assertEqual(status, "invalid")
        self.assertIsNone(expires_at)

    def test_403_invalid(self) -> None:
        svc = self._svc()
        with patch("web.token_service.requests.get", return_value=_resp(403)):
            status, expires_at = svc._validate_token_gh("ghp_forbidden_ABCDEF")
        self.assertEqual(status, "invalid")
        self.assertIsNone(expires_at)

    def test_transport_error_unknown_does_not_raise(self) -> None:
        svc = self._svc()
        with patch(
            "web.token_service.requests.get",
            side_effect=requests.exceptions.ConnectionError("boom"),
        ):
            status, expires_at = svc._validate_token_gh("ghp_netblip_ABCDEF")
        self.assertEqual(status, "unknown")
        self.assertIsNone(expires_at)

    def test_500_unknown(self) -> None:
        svc = self._svc()
        with patch("web.token_service.requests.get", return_value=_resp(502)):
            status, expires_at = svc._validate_token_gh("ghp_5xx_ABCDEF")
        self.assertEqual(status, "unknown")
        self.assertIsNone(expires_at)

    def test_proxy_env_honoured_first_entry(self) -> None:
        svc = self._svc()
        with patch.dict(
            os.environ,
            {"HARVESTER_PROXY": "socks5://127.0.0.1:1080,socks5://127.0.0.1:1090"},
        ), patch(
            "web.token_service.requests.get", return_value=_resp(200)
        ) as mock_get:
            svc._validate_token_gh("ghp_proxy_token_ABCDEF")
        _, kwargs = mock_get.call_args
        self.assertEqual(
            kwargs["proxies"], {"http": "socks5://127.0.0.1:1080",
                                "https": "socks5://127.0.0.1:1080"}
        )

    def test_no_proxy_env_uses_none(self) -> None:
        svc = self._svc()
        env = {k: v for k, v in os.environ.items() if k != "HARVESTER_PROXY"}
        with patch.dict(os.environ, env, clear=True), patch(
            "web.token_service.requests.get", return_value=_resp(200)
        ) as mock_get:
            svc._validate_token_gh("ghp_direct_token_ABCDEF")
        _, kwargs = mock_get.call_args
        self.assertIsNone(kwargs.get("proxies"))


# ---------------------------------------------------------------------------
# B. Add-time validation — TokenService.add_token
# ---------------------------------------------------------------------------


class TestAddTokenValidation(unittest.TestCase):
    """B. add_token stores valid/unknown, rejects invalid with ValueError."""

    def test_add_valid_stores_expiry(self) -> None:
        from web.token_service import TokenService

        async def _scenario() -> None:
            from web.db import get_db, init_db

            db_path = _temp_db_path()
            await init_db(db_path)
            svc = TokenService(db_path)
            with patch(
                "web.token_service.requests.get",
                return_value=_resp(200, {"github-authentication-token-expiration": "2030-01-01 00:00:00 UTC"}),
            ):
                result = await svc.add_token("api", "ghp_valid_add_ABCDEF", label="v")
            self.assertEqual(result["expires_at"], "2030-01-01 00:00:00 UTC")
            db = await get_db(db_path)
            try:
                cur = await db.execute(
                    "SELECT expires_at FROM github_tokens WHERE id=?", (result["id"],)
                )
                row = await cur.fetchone()
            finally:
                await db.close()
            self.assertEqual(row["expires_at"], "2030-01-01 00:00:00 UTC")

        _run_async(_scenario())

    def test_add_invalid_401_raises_and_does_not_store(self) -> None:
        from web.token_service import TokenService

        async def _scenario() -> None:
            from web.db import init_db

            db_path = _temp_db_path()
            await init_db(db_path)
            svc = TokenService(db_path)
            with patch("web.token_service.requests.get", return_value=_resp(401)):
                with self.assertRaises(ValueError):
                    await svc.add_token("api", "ghp_revoked_add_ABCDEF", label="bad")
            tokens = await svc.list_tokens()
            self.assertEqual(tokens, [])

        _run_async(_scenario())

    def test_add_unknown_transport_stores_with_null_expiry(self) -> None:
        from web.token_service import TokenService

        async def _scenario() -> None:
            from web.db import init_db

            db_path = _temp_db_path()
            await init_db(db_path)
            svc = TokenService(db_path)
            with patch(
                "web.token_service.requests.get",
                side_effect=requests.exceptions.ConnectionError("blip"),
            ):
                result = await svc.add_token("api", "ghp_blip_add_ABCDEF", label="blip")
            self.assertIsNone(result["expires_at"])
            tokens = await svc.list_tokens()
            self.assertEqual(len(tokens), 1)
            self.assertIsNone(tokens[0]["expires_at"])

        _run_async(_scenario())

    def test_session_token_skips_validation(self) -> None:
        """Session cookies must not be probed against the Bearer API."""
        from web.token_service import TokenService

        async def _scenario() -> None:
            from web.db import init_db

            db_path = _temp_db_path()
            await init_db(db_path)
            svc = TokenService(db_path)
            with patch("web.token_service.requests.get") as mock_get:
                result = await svc.add_token(
                    "session", "user_session_cookie_value_123", label="s"
                )
            mock_get.assert_not_called()
            self.assertIsNone(result["expires_at"])
            tokens = await svc.list_tokens()
            self.assertEqual(len(tokens), 1)

        _run_async(_scenario())


class TestBulkImportValidation(unittest.TestCase):
    """B. add_tokens_bulk rejects dead (401) tokens into ``dead``."""

    def test_bulk_import_dead_list_masked(self) -> None:
        from web.token_service import TokenService

        async def _scenario() -> None:
            from web.db import init_db

            db_path = _temp_db_path()
            await init_db(db_path)
            svc = TokenService(db_path)

            def _fake_get(url, **kwargs):
                auth = kwargs["headers"]["Authorization"]
                if "deadone" in auth:
                    return _resp(401)
                return _resp(200, {"github-authentication-token-expiration": "2030-01-01 00:00:00 UTC"})

            with patch("web.token_service.requests.get", side_effect=_fake_get):
                result = await svc.add_tokens_bulk(
                    "api",
                    "ghp_liveone_ABCDEF\nghp_deadone_ABCDEF\n",
                )
            self.assertEqual(result["added"], 1)
            self.assertEqual(len(result["dead"]), 1)
            # masked, never plaintext
            self.assertNotIn("ghp_deadone_ABCDEF", result["dead"][0])
            tokens = await svc.list_tokens()
            self.assertEqual(len(tokens), 1)
            self.assertIsNotNone(tokens[0]["expires_at"])

        _run_async(_scenario())


# ---------------------------------------------------------------------------
# D. Surface — models / list / stats
# ---------------------------------------------------------------------------


class TestTokenSurface(unittest.TestCase):
    """D. TokenOut, list_tokens and get_stats surface expiry."""

    def test_token_out_has_expires_at(self) -> None:
        from web.models import TokenOut

        out = TokenOut(
            id=1,
            token_type="api",
            token_masked="ghp_ab...cdef",
            label="x",
            enabled=True,
            created_at="",
            expires_at="2030-01-01 00:00:00 UTC",
        )
        self.assertEqual(out.expires_at, "2030-01-01 00:00:00 UTC")

    def test_list_tokens_includes_expires_at(self) -> None:
        from web.token_service import TokenService

        async def _scenario() -> None:
            from web.db import get_db, init_db

            db_path = _temp_db_path()
            await init_db(db_path)
            svc = TokenService(db_path)
            db = await get_db(db_path)
            try:
                from web.crypto import encrypt_str

                await db.execute(
                    "INSERT INTO github_tokens "
                    "(token_type, token_encrypted, token_hash, label, expires_at) "
                    "VALUES ('api', ?, 'hash-x', 'lbl', '2030-01-01 00:00:00 UTC')",
                    (encrypt_str("ghp_listed_token_ABCDEF"),),
                )
                await db.commit()
            finally:
                await db.close()

            tokens = await svc.list_tokens()
            self.assertEqual(len(tokens), 1)
            self.assertEqual(tokens[0]["expires_at"], "2030-01-01 00:00:00 UTC")

        _run_async(_scenario())

    def test_stats_counts_tokens_with_expiry(self) -> None:
        from web.token_service import TokenService

        async def _scenario() -> None:
            from web.db import get_db, init_db

            db_path = _temp_db_path()
            await init_db(db_path)
            svc = TokenService(db_path)
            db = await get_db(db_path)
            try:
                await db.executemany(
                    "INSERT INTO github_tokens "
                    "(token_type, token_encrypted, token_hash, label, expires_at) "
                    "VALUES ('api', 'enc', ?, ?, ?)",
                    [
                        ("h1", "a", "2030-01-01 00:00:00 UTC"),
                        ("h2", "b", None),
                    ],
                )
                await db.commit()
            finally:
                await db.close()

            stats = await svc.get_stats()
            self.assertEqual(stats["with_expiry"], 1)

        _run_async(_scenario())


if __name__ == "__main__":
    unittest.main()