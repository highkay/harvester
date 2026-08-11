#!/usr/bin/env python3

"""TDD unit tests for web/token_service.py — GitHub token CRUD + hot-reload.

Given a temporary SQLite database with the github_tokens table,
When TokenService methods are called,
Then tokens are encrypted at rest, duplicates rejected, hot-reload invoked.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from web.db import init_db, get_db
from web.crypto import decrypt_str, encrypt_str
from web.crypto import _get_crypto
from web.models import mask_token


def _run_async(coro):
    """Helper to run an async test from a sync unittest method."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helper: build a temporary db path
# ---------------------------------------------------------------------------

def _temp_db_path() -> str:
    """Create a temporary SQLite database file and return its path."""
    tmpdir = tempfile.mkdtemp()
    return os.path.join(tmpdir, "test.db")


class TestTokenServiceAddAndList(unittest.TestCase):
    """Given an empty token store,
    When tokens are added via TokenService.add_token,
    Then list_tokens returns masked entries with no plaintext leak.
    """

    def test_add_and_list_masked(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            token_value = "ghp_thisIsAVeryLongGitHubToken12345"

            await svc.add_token("api", token_value, label="test-1")

            tokens = await svc.list_tokens()
            self.assertEqual(len(tokens), 1)
            self.assertEqual(tokens[0]["token_type"], "api")
            self.assertEqual(tokens[0]["label"], "test-1")
            self.assertTrue(tokens[0]["enabled"])

            # Masked token must exist and NOT contain the plaintext
            masked = tokens[0]["token_masked"]
            self.assertTrue(len(masked) > 0)
            self.assertNotIn(token_value, masked)
            # The mask rule: len >= 12 → first6...last4
            expected_masked = mask_token(token_value)
            self.assertEqual(masked, expected_masked)
            self.assertEqual(masked, f"{token_value[:6]}...{token_value[-4:]}")

            # Raw DB must contain encrypted data, not plaintext
            raw_db = await get_db(db_path)
            cursor = await raw_db.execute(
                "SELECT token_encrypted FROM github_tokens WHERE id=1"
            )
            row = await cursor.fetchone()
            await raw_db.close()
            encrypted = row[0]
            self.assertNotIn(token_value, encrypted)

        _run_async(_scenario())

    def test_add_short_token_full_mask(self) -> None:
        """Token shorter than 12 chars returns '***'."""
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            await svc.add_token("session", "short", label="short")
            tokens = await svc.list_tokens()
            self.assertEqual(len(tokens), 1)
            self.assertEqual(tokens[0]["token_masked"], "***")

        _run_async(_scenario())


class TestTokenServiceDuplicate(unittest.TestCase):
    """Given an existing token,
    When the same token_value is added again,
    Then ValueError is raised (duplicate hash).
    """

    def test_duplicate_raises_value_error(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            token_value = "ghp_duplicateTestTokenXYZ9999"

            await svc.add_token("api", token_value, label="first")

            with self.assertRaises(ValueError):
                await svc.add_token("api", token_value, label="second")

        _run_async(_scenario())


class TestTokenServiceBulkImport(unittest.TestCase):
    """Given a multi-line token string,
    When add_tokens_bulk is called,
    Then non-duplicate rows are inserted, duplicate lines are skipped.
    """

    def test_bulk_import_counts(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)

            # First add one token so we can test duplicate skipping
            await svc.add_token("api", "ghp_existing_token_AAAAAAA", label="pre-existing")

            tokens_text = (
                "ghp_new_token_one_BBBBBBBB\n"
                "ghp_new_token_two_CCCCCCCC\n"
                "ghp_existing_token_AAAAAAA\n"  # duplicate
            )
            result = await svc.add_tokens_bulk("api", tokens_text)

            self.assertEqual(result["added"], 2)
            self.assertEqual(result["skipped_duplicates"], 1)
            self.assertEqual(len(result["errors"]), 0)

            tokens = await svc.list_tokens()
            self.assertEqual(len(tokens), 3)

        _run_async(_scenario())

    def test_bulk_import_internal_duplicates(self) -> None:
        """When the same token appears twice in the bulk string,
        the second occurrence is skipped as a duplicate."""
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            tokens_text = (
                "ghp_same_token_DDDDDDDDDD\n"
                "ghp_same_token_DDDDDDDDDD\n"
            )
            result = await svc.add_tokens_bulk("api", tokens_text)

            self.assertEqual(result["added"], 1)
            self.assertEqual(result["skipped_duplicates"], 1)

        _run_async(_scenario())


class TestTokenServiceDelete(unittest.TestCase):
    """Given a stored token,
    When delete_token is called,
    Then the token disappears from list_tokens.
    """

    def test_delete_removes_token(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            await svc.add_token("api", "ghp_delete_me_token_EEEE", label="to-delete")
            tokens_before = await svc.list_tokens()
            self.assertEqual(len(tokens_before), 1)

            deleted = await svc.delete_token(tokens_before[0]["id"])
            self.assertTrue(deleted)

            tokens_after = await svc.list_tokens()
            self.assertEqual(len(tokens_after), 0)

        _run_async(_scenario())

    def test_delete_nonexistent_returns_false(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            deleted = await svc.delete_token(99999)
            self.assertFalse(deleted)

        _run_async(_scenario())


class TestTokenServiceSetEnabled(unittest.TestCase):
    """Given a stored token,
    When set_token_enabled is called,
    Then get_enabled_tokens reflects the change.
    """

    def test_disable_then_enable(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            token_value = "ghp_toggle_token_FFFFFFF"
            await svc.add_token("api", token_value, label="toggle")

            tokens = await svc.list_tokens()
            tid = tokens[0]["id"]

            # Disable
            ok = await svc.set_token_enabled(tid, False)
            self.assertTrue(ok)

            # Should not appear in enabled list
            enabled = await svc.get_enabled_tokens()
            self.assertEqual(len(enabled), 0)

            # Re-enable
            ok = await svc.set_token_enabled(tid, True)
            self.assertTrue(ok)

            enabled = await svc.get_enabled_tokens()
            self.assertEqual(len(enabled), 1)
            self.assertEqual(enabled[0], token_value)

        _run_async(_scenario())


class TestTokenServiceGetEnabledTokens(unittest.TestCase):
    """get_enabled_tokens returns only enabled, api-type plaintext tokens."""

    def test_only_enabled_api_tokens_returned(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            api_token = "ghp_api_enabled_GGGGGG"
            session_token = "session_cookie_value_xxxx"

            await svc.add_token("api", api_token, label="api-1")
            await svc.add_token("session", session_token, label="sess-1")
            await svc.add_token("api", "ghp_api_disabled_HHHHHH", label="api-2")

            # Disable the third token
            tokens = await svc.list_tokens()
            for t in tokens:
                if t["label"] == "api-2":
                    await svc.set_token_enabled(t["id"], False)

            enabled = await svc.get_enabled_tokens()
            # Only api-1 (enabled api token) should be returned
            self.assertEqual(len(enabled), 1)
            self.assertIn(api_token, enabled)
            self.assertNotIn(session_token, enabled)

        _run_async(_scenario())


class TestTokenServiceEncryptionAtRest(unittest.TestCase):
    """Verify that token_encrypted field in the DB contains encrypted data."""

    def test_token_encrypted_not_plaintext(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            token_value = "ghp_secret_token_ZZZZZZZZ"
            await svc.add_token("api", token_value, label="secret")

            # Read raw DB
            raw_db = await get_db(db_path)
            cursor = await raw_db.execute(
                "SELECT token_encrypted FROM github_tokens WHERE id=1"
            )
            row = await cursor.fetchone()
            await raw_db.close()

            encrypted = row[0]
            # Encrypted field must not contain the plaintext
            self.assertNotIn(token_value, encrypted)
            # Must decrypt back to original
            self.assertEqual(decrypt_str(encrypted), token_value)

        _run_async(_scenario())


class TestTokenServiceHotReload(unittest.TestCase):
    """When tokens are added/deleted/enabled/disabled,
    _hot_reload calls web.token_service.update_credentials."""

    def test_hot_reload_called_on_add(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            token_value = "ghp_hotreload_token_JJJJ"

            with patch("web.token_service.update_credentials") as mock_update:
                await svc.add_token("api", token_value, label="hr-1")
                mock_update.assert_called_once()
                args, _ = mock_update.call_args
                self.assertIn(token_value, args[1])  # second positional arg = tokens

        _run_async(_scenario())

    def test_hot_reload_called_on_delete(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            token_value = "ghp_hotreload_delete_KKKK"
            await svc.add_token("api", token_value, label="hrd-1")

            tokens = await svc.list_tokens()
            tid = tokens[0]["id"]

            with patch("web.token_service.update_credentials") as mock_update:
                await svc.delete_token(tid)
                mock_update.assert_called_once()

        _run_async(_scenario())

    def test_hot_reload_called_on_toggle(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            token_value = "ghp_hotreload_toggle_LLLL"
            await svc.add_token("api", token_value, label="hrt-1")

            tokens = await svc.list_tokens()
            tid = tokens[0]["id"]

            with patch("web.token_service.update_credentials") as mock_update:
                await svc.set_token_enabled(tid, False)
                mock_update.assert_called_once()

        _run_async(_scenario())

    def test_hot_reload_during_bulk_import(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            tokens_text = "ghp_bulk_hotreload_MMMMM\nghp_bulk_hotreload_NNNNN"

            with patch("web.token_service.update_credentials") as mock_update:
                await svc.add_tokens_bulk("api", tokens_text)
                # Called once after bulk insert
                mock_update.assert_called_once()

        _run_async(_scenario())


class TestTokenServiceStats(unittest.TestCase):
    """Test token statistics endpoint — counts from DB."""

    def test_stats_returns_counts(self) -> None:
        async def _scenario() -> None:
            from web.token_service import TokenService

            db_path = _temp_db_path()
            await init_db(db_path)

            svc = TokenService(db_path)
            await svc.add_token("api", "ghp_stats1_OOOOOOOO", label="s1")
            await svc.add_token("session", "session_stats2_PPP", label="s2")
            await svc.add_token("api", "ghp_stats3_QQQQQQQQ", label="s3")

            # Disable one
            stats = await svc.get_stats()
            self.assertEqual(stats["total"], 3)
            self.assertEqual(stats["api_count"], 2)
            self.assertEqual(stats["session_count"], 1)

        _run_async(_scenario())


if __name__ == "__main__":
    unittest.main()
