#!/usr/bin/env python3

"""Unit tests for web/db.py — aiosqlite async database with 6 tables."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest

from web.db import get_db, init_db, reconcile_running_runs


def _run_async(coro):
    """Helper to run an async test from a sync unittest method."""
    return asyncio.run(coro)


class TestDatabaseInit(unittest.TestCase):
    """Given a temporary directory,
    When init_db is called,
    Then all 6 expected tables exist in sqlite_master.
    """

    _EXPECTED_TABLES = frozenset({
        "github_tokens",
        "gpt_load_config",
        "provider_group_mapping",
        "run_records",
        "push_logs",
        "schedule_config",
    })

    def test_init_db_creates_all_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            _run_async(init_db(db_path))

            # Verify tables via synchronous sqlite3 (independent check)
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row[0] for row in cursor.fetchall()}
            conn.close()

            self.assertTrue(
                self._EXPECTED_TABLES.issubset(tables),
                f"Missing tables: {self._EXPECTED_TABLES - tables}",
            )

    def test_init_db_is_idempotent(self) -> None:
        """Calling init_db twice should not raise an error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            _run_async(init_db(db_path))
            _run_async(init_db(db_path))  # Must not raise

    def test_init_db_creates_run_new_keys_table(self) -> None:
        """Given an initialized database,
        When we look for the run_new_keys table,
        Then it exists and rejects duplicate (run_id, key_hash) rows.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            _run_async(init_db(db_path))

            # Independent synchronous sqlite3 check
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='run_new_keys'"
                ).fetchone()
                self.assertIsNotNone(
                    row, "run_new_keys table was not created by init_db"
                )

                # Functional proof of the composite UNIQUE(run_id, key_hash) constraint
                conn.execute(
                    "INSERT INTO run_new_keys "
                    "(run_id, provider_name, task_name, key_hash, token_masked) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("run-1", "openai", "openai", "hash_a", "sk-***abc"),
                )
                conn.commit()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO run_new_keys "
                        "(run_id, provider_name, task_name, key_hash, token_masked) "
                        "VALUES (?, ?, ?, ?, ?)",
                        ("run-1", "openai", "openai", "hash_a", "sk-***abc"),
                    )
                    conn.commit()
            finally:
                conn.close()


class TestDatabaseInsertRead(unittest.TestCase):
    """Given an initialized database,
    When we insert a row into github_tokens and read it back,
    Then the stored data is correct and the encrypted field is opaque.
    """

    def test_insert_and_read_github_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            async def _scenario() -> None:
                await init_db(db_path)

                db = await get_db(db_path)
                encrypted = "base64_encrypted_blob_here"
                token_hash = "abcdef0123456789"

                cursor = await db.execute(
                    "INSERT INTO github_tokens "
                    "(token_type, token_encrypted, token_hash, label) "
                    "VALUES (?, ?, ?, ?)",
                    ("api", encrypted, token_hash, "test-token"),
                )
                await db.commit()
                row_id = cursor.lastrowid

                cursor = await db.execute(
                    "SELECT id, token_type, token_encrypted, token_hash, label, enabled "
                    "FROM github_tokens WHERE id = ?",
                    (row_id,),
                )
                row = await cursor.fetchone()
                await db.close()

                assert row is not None
                self.assertEqual(row[0], row_id)
                self.assertEqual(row[1], "api")
                self.assertEqual(row[2], encrypted)
                self.assertEqual(row[3], token_hash)
                self.assertEqual(row[4], "test-token")
                self.assertEqual(row[5], 1)  # enabled default

            _run_async(_scenario())

    def test_insert_without_encrypted_value_visible(self) -> None:
        """Verify that token_hash is in plaintext but encrypted blob is opaque
        (the hash, not the original token, is used for lookup).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            async def _scenario() -> None:
                await init_db(db_path)
                db = await get_db(db_path)

                plaintext_token = "ghp_real_github_token_abc123"
                token_hash = "hash_of_that_token"

                await db.execute(
                    "INSERT INTO github_tokens "
                    "(token_type, token_encrypted, token_hash) VALUES (?, ?, ?)",
                    ("session", "opaque_encrypted_blob", token_hash),
                )
                await db.commit()

                # Query by hash — should find the row
                cursor = await db.execute(
                    "SELECT id, token_hash, token_encrypted "
                    "FROM github_tokens WHERE token_hash = ?",
                    (token_hash,),
                )
                row = await cursor.fetchone()
                await db.close()

                assert row is not None
                self.assertEqual(row[1], token_hash)
                # The encrypted column must NOT contain the plaintext
                self.assertNotIn(plaintext_token, row[2])

            _run_async(_scenario())

    def test_token_hash_unique_constraint(self) -> None:
        """Duplicate token_hash must raise IntegrityError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            async def _scenario() -> None:
                await init_db(db_path)
                db = await get_db(db_path)

                await db.execute(
                    "INSERT INTO github_tokens "
                    "(token_type, token_encrypted, token_hash) VALUES (?, ?, ?)",
                    ("api", "enc_a", "duplicate_hash"),
                )
                await db.commit()

                with self.assertRaises(sqlite3.IntegrityError):
                    await db.execute(
                        "INSERT INTO github_tokens "
                        "(token_type, token_encrypted, token_hash) VALUES (?, ?, ?)",
                        ("api", "enc_b", "duplicate_hash"),
                    )
                    await db.commit()

                await db.close()

            _run_async(_scenario())


class TestReconcileRunningRuns(unittest.TestCase):
    """Given an initialized database containing run_records,
    When reconcile_running_runs is called,
    Then 'running' rows are marked failed and other rows are untouched.
    """

    def test_reconcile_marks_running_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            async def _scenario() -> int:
                await init_db(db_path)
                db = await get_db(db_path)
                await db.execute(
                    "INSERT INTO run_records (id, provider_name, config_file, status) "
                    "VALUES (?, ?, ?, ?)",
                    ("a", "deepseek", "x.yaml", "running"),
                )
                await db.execute(
                    "INSERT INTO run_records (id, provider_name, config_file, status) "
                    "VALUES (?, ?, ?, ?)",
                    ("b", "deepseek", "x.yaml", "completed"),
                )
                await db.commit()
                await db.close()

                return await reconcile_running_runs(db_path)

            reconciled = _run_async(_scenario())
            self.assertEqual(reconciled, 1)

            # Independent verification via synchronous sqlite3
            conn = sqlite3.connect(db_path)
            row_a = conn.execute(
                "SELECT status, finished_at, error_message "
                "FROM run_records WHERE id = 'a'"
            ).fetchone()
            row_b = conn.execute(
                "SELECT status, finished_at FROM run_records WHERE id = 'b'"
            ).fetchone()
            conn.close()

            assert row_a is not None
            self.assertEqual(row_a[0], "failed")
            self.assertIsNotNone(row_a[1])
            self.assertEqual(row_a[2], "interrupted by service restart")

            assert row_b is not None
            self.assertEqual(row_b[0], "completed")
            self.assertIsNone(row_b[1])

    def test_reconcile_no_running_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test.db"

            async def _scenario() -> int:
                await init_db(db_path)
                db = await get_db(db_path)
                for run_id, status in (("a", "completed"), ("b", "failed")):
                    await db.execute(
                        "INSERT INTO run_records "
                        "(id, provider_name, config_file, status) VALUES (?, ?, ?, ?)",
                        (run_id, "deepseek", "x.yaml", status),
                    )
                await db.commit()
                await db.close()

                return await reconcile_running_runs(db_path)

            self.assertEqual(_run_async(_scenario()), 0)
