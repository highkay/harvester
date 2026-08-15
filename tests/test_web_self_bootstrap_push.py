#!/usr/bin/env python3

"""TDD unit tests for web/self_bootstrap_push.py — GitHub self-bootstrap service.

Given a temporary SQLite database and workspace with providers/github/valid-keys.txt,
When SelfBootstrapPushService.push_valid_keys is called,
Then each GH-prefixed key is inserted (encrypted) into github_tokens with
label='harvester-bootstrap' and ONE push_logs row is written.

Test style mirrors tests/test_web_tavily_push.py: unittest + unittest.mock,
temp SQLite via web.db.init_db, temp workspace, and patched helpers.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web.crypto import _get_crypto
from web.crypto import decrypt_str, encrypt_str
from web.db import init_db as _init_db_async
from web.self_bootstrap_push import SelfBootstrapPushService


def _run_async(coro):
    """Helper to run async from sync unittest."""
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _temp_db_path() -> str:
    """Build a temporary SQLite database path."""
    tmpdir = tempfile.mkdtemp()
    return os.path.join(tmpdir, "test_self_bootstrap_push.db")


def _init_schema(db_path: str) -> None:
    """Initialize the full schema (incl. github_tokens + push_logs) in a temp database."""
    _run_async(_init_db_async(db_path))


def _write_valid_keys(workspace: str, keys: list[str]) -> None:
    """Write providers/github/valid-keys.txt in the workspace."""
    provider_dir = Path(workspace) / "providers" / "github"
    provider_dir.mkdir(parents=True, exist_ok=True)
    vk_path = provider_dir / "valid-keys.txt"
    vk_path.write_text("\n".join(keys), encoding="utf-8")


def _make_service(db_path: str, workspace: str) -> SelfBootstrapPushService:
    """Build a SelfBootstrapPushService bound to the temp DB + workspace."""
    return SelfBootstrapPushService(db_path=db_path, workspace=workspace)


def _fetch_log(db_path: str, run_id: str):
    """Fetch the push_logs row for a run_id (or None)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM push_logs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()


def _count_logs(db_path: str, run_id: str) -> int:
    """Count push_logs rows for a run_id."""
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM push_logs WHERE run_id = ?", (run_id,)
        )
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def _fetch_tokens(db_path: str) -> list:
    """Fetch all github_tokens rows ordered by id."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM github_tokens ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _count_tokens(db_path: str) -> int:
    """Count github_tokens rows."""
    conn = sqlite3.connect(db_path)
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM github_tokens").fetchone()[0]
        )
    finally:
        conn.close()


def _insert_token(
    db_path: str, token_type: str, token_value: str, label: str = ""
) -> None:
    """Insert a token row the same way TokenService does (encrypted + hashed)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO github_tokens (token_type, token_encrypted, token_hash, label) "
            "VALUES (?, ?, ?, ?)",
            (
                token_type,
                encrypt_str(token_value),
                _get_crypto().hash_token(token_value),
                label,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SelfBootstrapPushService tests
# ---------------------------------------------------------------------------


class TestSelfBootstrapPushServiceBasic(unittest.TestCase):
    """Given a temp DB + workspace,
    When push_valid_keys is called,
    Then GH keys are inserted encrypted into github_tokens and one
    push_logs row is written; gates and failures never raise.
    """

    def test_ghp_key_inserted_added_count_success(self) -> None:
        """ghp_ key → row inserted, added_count=1, status success, label set."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["ghp_fake0001abc"])

            svc = _make_service(db_path, workspace)
            svc.push_valid_keys("github", "run-sb-001")

            tokens = _fetch_tokens(db_path)
            self.assertEqual(len(tokens), 1)
            self.assertEqual(tokens[0]["token_type"], "api")
            self.assertEqual(tokens[0]["label"], "harvester-bootstrap")
            self.assertEqual(tokens[0]["enabled"], 1)
            self.assertEqual(
                tokens[0]["token_hash"],
                _get_crypto().hash_token("ghp_fake0001abc"),
            )

            row = _fetch_log(db_path, "run-sb-001")
            self.assertIsNotNone(row, "push_logs entry should exist")
            self.assertEqual(row["provider_name"], "github")
            self.assertEqual(row["gpt_load_config_id"], 0)
            self.assertEqual(row["group_id"], 0)
            self.assertEqual(row["keys_count"], 1)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 0)
            self.assertEqual(row["status"], "success")
            self.assertIsNone(row["error_message"])

    def test_non_gh_lines_ignored_no_insert(self) -> None:
        """Non-GH lines (e.g. sk-...) → ignored, NO github_tokens insert."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["sk-abc123xyz", "tvly-zzz", ""])

            svc = _make_service(db_path, workspace)
            svc.push_valid_keys("github", "run-sb-002")

            self.assertEqual(_count_tokens(db_path), 0)

            row = _fetch_log(db_path, "run-sb-002")
            self.assertIsNotNone(row)
            self.assertEqual(row["keys_count"], 2, "deduped non-empty lines read")
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 2)
            self.assertEqual(row["status"], "success")

    def test_duplicate_integrity_error_ignored_no_exception(self) -> None:
        """Pre-existing token → IntegrityError → ignored; other keys still added."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _insert_token(db_path, "api", "ghp_dupkey0001")

            _write_valid_keys(workspace, ["ghp_dupkey0001", "ghp_newkey0001"])

            svc = _make_service(db_path, workspace)
            svc.push_valid_keys("github", "run-sb-003")  # must not raise

            self.assertEqual(_count_tokens(db_path), 2)

            row = _fetch_log(db_path, "run-sb-003")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 1, "duplicate counts as ignored")
            self.assertEqual(row["status"], "success")

    def test_kill_switch_env_disables_noop(self) -> None:
        """HARVESTER_SELF_BOOTSTRAP=0 → no inserts, no push_logs rows."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["ghp_fake0002abc"])

            svc = _make_service(db_path, workspace)

            with patch.dict(
                os.environ, {"HARVESTER_SELF_BOOTSTRAP": "0"}, clear=False
            ):
                svc.push_valid_keys("github", "run-sb-004")

            self.assertEqual(_count_tokens(db_path), 0)
            self.assertEqual(_count_logs(db_path, "run-sb-004"), 0)

    def test_non_github_provider_noop(self) -> None:
        """provider_name != github → no inserts, no push_logs rows."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["ghp_fake0003abc"])

            svc = _make_service(db_path, workspace)
            svc.push_valid_keys("tavily", "run-sb-005")

            self.assertEqual(_count_tokens(db_path), 0)
            self.assertEqual(_count_logs(db_path, "run-sb-005"), 0)

    def test_unexpected_exception_best_effort_failed_log_no_raise(self) -> None:
        """sqlite3.connect raising (insert phase) → no raise + failed push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["ghp_fake0004abc"])

            svc = _make_service(db_path, workspace)

            real_connect = sqlite3.connect
            calls = {"n": 0}

            def _flaky_connect(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise sqlite3.OperationalError("boom")
                return real_connect(*args, **kwargs)

            with patch(
                "web.self_bootstrap_push.sqlite3.connect",
                side_effect=_flaky_connect,
            ):
                svc.push_valid_keys("github", "run-sb-006")  # must not raise

            row = _fetch_log(db_path, "run-sb-006")
            self.assertIsNotNone(row, "best-effort failed row must be written")
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_same_run_id_twice_inserts_once(self) -> None:
        """Second call with the same run_id is a no-op: one insert, one log row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["ghp_fake0005abc"])

            svc = _make_service(db_path, workspace)
            svc.push_valid_keys("github", "run-sb-007")
            svc.push_valid_keys("github", "run-sb-007")

            self.assertEqual(_count_tokens(db_path), 1)
            self.assertEqual(_count_logs(db_path, "run-sb-007"), 1)

    def test_hot_reload_update_credentials_order_sessions_first(self) -> None:
        """added>0 → update_credentials called with sessions list FIRST, tokens second."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _insert_token(db_path, "session", "session-cookie-value")

            _write_valid_keys(workspace, ["ghp_fake0006abc"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.self_bootstrap_push.update_credentials"
            ) as mock_uc:
                svc.push_valid_keys("github", "run-sb-008")

            mock_uc.assert_called_once()
            sessions, tokens = mock_uc.call_args[0]
            self.assertEqual(
                sessions, ["session-cookie-value"], "sessions must be first arg"
            )
            self.assertIn("ghp_fake0006abc", tokens)
            self.assertNotIn("session-cookie-value", tokens)

    def test_hot_reload_runtime_error_swallowed(self) -> None:
        """update_credentials raising RuntimeError → swallowed, push still succeeds."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["ghp_fake0007abc"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.self_bootstrap_push.update_credentials",
                side_effect=RuntimeError("ResourceManager not initialised"),
            ):
                svc.push_valid_keys("github", "run-sb-009")  # must not raise

            self.assertEqual(_count_tokens(db_path), 1)
            row = _fetch_log(db_path, "run-sb-009")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")

    def test_encrypted_at_rest(self) -> None:
        """token_encrypted != plaintext and decrypt_str roundtrips to plaintext."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["ghp_fake0008abc"])

            svc = _make_service(db_path, workspace)
            svc.push_valid_keys("github", "run-sb-010")

            tokens = _fetch_tokens(db_path)
            self.assertEqual(len(tokens), 1)
            self.assertNotEqual(
                tokens[0]["token_encrypted"], "ghp_fake0008abc",
                "token must be encrypted at rest",
            )
            self.assertEqual(
                decrypt_str(tokens[0]["token_encrypted"]), "ghp_fake0008abc"
            )

    def test_default_env_resolution_workspace_and_db(self) -> None:
        """No explicit args → HARVESTER_WORKSPACE + resolve_db_path via env."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)

            env = {
                "HARVESTER_WORKSPACE": workspace,
                "HARVESTER_DB_PATH": db_path,
            }
            with patch.dict(os.environ, env, clear=False):
                svc = SelfBootstrapPushService()

            self.assertEqual(svc._workspace, Path(workspace).resolve())
            self.assertEqual(svc._db_path, db_path)


# ---------------------------------------------------------------------------
# get_self_bootstrap_push_service singleton tests
# ---------------------------------------------------------------------------


class TestSelfBootstrapPushServiceSingleton(unittest.TestCase):
    """Given the module,
    When get_self_bootstrap_push_service is called multiple times,
    Then the same SelfBootstrapPushService instance is returned.
    """

    def test_get_self_bootstrap_push_service_returns_singleton(self) -> None:
        """Module-level singleton factory returns the same object."""
        from web.self_bootstrap_push import get_self_bootstrap_push_service

        svc1 = get_self_bootstrap_push_service()
        svc2 = get_self_bootstrap_push_service()
        self.assertIs(
            svc1, svc2,
            "get_self_bootstrap_push_service must return the same instance",
        )

    def test_self_bootstrap_push_service_has_push_valid_keys_method(self) -> None:
        """The singleton must have push_valid_keys(provider_name, run_id) method."""
        from web.self_bootstrap_push import get_self_bootstrap_push_service

        svc = get_self_bootstrap_push_service()
        self.assertTrue(
            callable(getattr(svc, "push_valid_keys", None)),
            "SelfBootstrapPushService must have push_valid_keys method",
        )


if __name__ == "__main__":
    unittest.main()
