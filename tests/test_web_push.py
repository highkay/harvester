#!/usr/bin/env python3

"""TDD unit tests for web/push.py — gpt-load push service.

Given a temporary SQLite database with gpt_load_config + provider_group_mapping tables,
When push_valid_keys is called,
Then keys are read from valid-keys.txt, pushed to gpt-load, and push_logs is written.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from web.crypto import decrypt_str, encrypt_str
from web.db import init_db as _init_db_async


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
    return os.path.join(tmpdir, "test_push.db")


def _seed_db(db_path: str, workspace: str) -> None:
    """Initialize schema and seed gpt_load_config + provider_group_mapping."""
    _run_async(_init_db_async(db_path))

    conn = sqlite3.connect(db_path)
    try:
        # Seed gpt_load_config
        auth_enc = encrypt_str("test-auth-key-for-push")
        conn.execute(
            "INSERT INTO gpt_load_config (name, base_url, auth_key_encrypted) "
            "VALUES (?, ?, ?)",
            ("test-gptload", "http://127.0.0.1:19999", auth_enc),
        )
        # Seed provider_group_mapping — deepseek → group 2
        conn.execute(
            "INSERT INTO provider_group_mapping "
            "(provider_name, gpt_load_config_id, group_id, group_name) "
            "VALUES (?, ?, ?, ?)",
            ("deepseek", 1, 2, "DeepSeek"),
        )
        conn.commit()
    finally:
        conn.close()


def _write_valid_keys(workspace: str, provider: str, keys: list[str]) -> None:
    """Write a valid-keys.txt file in the workspace providers dir."""
    provider_dir = Path(workspace) / "providers" / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    vk_path = provider_dir / "valid-keys.txt"
    vk_path.write_text("\n".join(keys), encoding="utf-8")


# ---------------------------------------------------------------------------
# PushService tests
# ---------------------------------------------------------------------------


class TestPushServiceBasic(unittest.TestCase):
    """Given valid mapping and keys,
    When push_valid_keys is called,
    Then the gpt-load API is called with correct body and push_logs is written.
    """

    def test_push_valid_keys_writes_push_log_success(self) -> None:
        """Happy path: mapping exists, keys exist, API returns success."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            _write_valid_keys(
                workspace, "deepseek", ["sk-key-aaaa", "sk-key-bbbb", "sk-key-cccc"]
            )

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "code": 0,
                "data": {
                    "group_id": 2,
                    "credentials_added": 3,
                    "credentials_duplicated": 0,
                },
            }

            with patch("web.push.requests.post", return_value=mock_response) as mock_post:
                svc.push_valid_keys("deepseek", "run-test-001")

                # Verify POST was called with correct args
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                url = call_args[0][0]
                self.assertIn("/api/groups/2/credentials/import", url)

                body = call_args[1]["json"]
                self.assertIn("sk-key-aaaa", body["credentials"])
                self.assertIn("sk-key-bbbb", body["credentials"])
                self.assertIn("sk-key-cccc", body["credentials"])

                headers = call_args[1]["headers"]
                self.assertIn("Authorization", headers)
                self.assertIn("Bearer", headers["Authorization"])
                # gpt-load v2 requires a UUIDv4 Idempotency-Key header
                import uuid as _uuid
                _uuid.UUID(headers["Idempotency-Key"], version=4)

            # Verify push_logs entry
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM push_logs WHERE run_id = ?", ("run-test-001",))
            row = cursor.fetchone()
            conn.close()

            self.assertIsNotNone(row, "push_logs entry should exist")
            self.assertEqual(row["provider_name"], "deepseek")
            self.assertEqual(row["group_id"], 2)
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 3)
            self.assertEqual(row["ignored_count"], 0)
            self.assertEqual(row["status"], "success")

    def test_push_valid_keys_idempotent_no_error(self) -> None:
        """When gpt-load returns added_count=0 (idempotent), record success."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "code": 0,
                "data": {"credentials_added": 0, "credentials_duplicated": 1},
            }

            with patch("web.push.requests.post", return_value=mock_response):
                svc.push_valid_keys("deepseek", "run-test-002")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM push_logs WHERE run_id = ?", ("run-test-002",))
            row = cursor.fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 1)

    def test_push_valid_keys_no_mapping_skips(self) -> None:
        """When no provider_group_mapping exists, skip without error."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            # No mapping for 'unknown-provider', should not raise
            svc.push_valid_keys("unknown-provider", "run-test-003")

            # No push_logs should be written
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT COUNT(*) FROM push_logs WHERE run_id = ?", ("run-test-003",)
            )
            count = cursor.fetchone()[0]
            conn.close()
            self.assertEqual(count, 0, "No push_logs for unmapped provider")

    def test_push_valid_keys_no_keys_file_skips(self) -> None:
        """When valid-keys.txt does not exist, skip without error."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            # No valid-keys.txt written for deepseek

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)
            svc.push_valid_keys("deepseek", "run-test-004")

            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT COUNT(*) FROM push_logs WHERE run_id = ?", ("run-test-004",)
            )
            count = cursor.fetchone()[0]
            conn.close()
            self.assertEqual(count, 0, "No push_logs when no keys file")

    def test_push_valid_keys_retry_on_503_then_success(self) -> None:
        """When API returns 503 twice then 200, retry 3 times total and succeed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            # Mock: 2 failures, then success
            call_count = [0]

            def _side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] < 3:
                    resp = MagicMock()
                    resp.status_code = 503
                    return resp
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "code": 0,
                    "data": {"credentials_added": 1, "credentials_duplicated": 0},
                }
                return resp

            with patch("web.push.requests.post", side_effect=_side_effect):
                svc.push_valid_keys("deepseek", "run-test-005")

            # Verify 3 attempts
            self.assertEqual(call_count[0], 3, "Should retry 2 failures + 1 success = 3 calls")

            # Verify push_logs shows success
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM push_logs WHERE run_id = ?", ("run-test-005",))
            row = cursor.fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")

    def test_push_valid_keys_retry_exhausted_marks_failed(self) -> None:
        """When all retries fail, record push_logs as 'failed'."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            mock_response = MagicMock()
            mock_response.status_code = 503

            with patch("web.push.requests.post", return_value=mock_response) as mock_post:
                svc.push_valid_keys("deepseek", "run-test-006")

                # 1 initial + 3 retries = 4 total attempts
                self.assertGreaterEqual(mock_post.call_count, 3)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM push_logs WHERE run_id = ?", ("run-test-006",))
            row = cursor.fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_valid_keys_large_batch_is_chunked(self) -> None:
        """Batches over the v2 import cap (5000 lines) are chunked.

        Each chunk is one ``credentials/import`` call with its own
        Idempotency-Key; counts aggregate across chunks.
        """
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            keys = [f"sk-key-{i:05d}" for i in range(5001)]
            _write_valid_keys(workspace, "deepseek", keys)

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            def _resp(n: int) -> MagicMock:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "code": 0,
                    "data": {"credentials_added": n, "credentials_duplicated": 0},
                }
                return resp

            with patch(
                "web.push.requests.post",
                side_effect=[_resp(5000), _resp(1)],
            ) as mock_post:
                svc.push_valid_keys("deepseek", "run-test-007")

                self.assertEqual(mock_post.call_count, 2)
                for call in mock_post.call_args_list:
                    self.assertIn(
                        "/api/groups/2/credentials/import", call[0][0]
                    )
                # Distinct Idempotency-Key per chunk
                keys_seen = [
                    call[1]["headers"]["Idempotency-Key"]
                    for call in mock_post.call_args_list
                ]
                self.assertNotEqual(keys_seen[0], keys_seen[1])

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM push_logs WHERE run_id = ?", ("run-test-007",)
            )
            row = cursor.fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 5001)

    def test_push_valid_keys_1000_keys_single_import(self) -> None:
        """A 1000-key batch fits one v2 import call (cap is 5000 lines)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            keys = [f"sk-key-{i:04d}" for i in range(1000)]
            _write_valid_keys(workspace, "deepseek", keys)

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "code": 0,
                "data": {"credentials_added": 1000, "credentials_duplicated": 0},
            }

            with patch("web.push.requests.post", return_value=mock_response) as mock_post:
                svc.push_valid_keys("deepseek", "run-test-008")

                call_url = mock_post.call_args[0][0]
                self.assertIn("/api/groups/2/credentials/import", call_url)

    def test_push_valid_keys_network_error_retried(self) -> None:
        """When network error occurs (ConnectionError), retry and succeed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)

            call_count = [0]

            def _side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] < 3:
                    import requests as req_mod

                    raise req_mod.ConnectionError("mock network error")
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "code": 0,
                    "data": {"credentials_added": 1, "credentials_duplicated": 0},
                }
                return resp

            with patch("web.push.requests.post", side_effect=_side_effect):
                svc.push_valid_keys("deepseek", "run-test-008")

            self.assertEqual(call_count[0], 3)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM push_logs WHERE run_id = ?", ("run-test-008",))
            row = cursor.fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")

    def test_push_valid_keys_empty_keys_file_skips(self) -> None:
        """When valid-keys.txt is empty or only whitespace, skip."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            _write_valid_keys(workspace, "deepseek", ["", "  ", "\t"])

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)
            svc.push_valid_keys("deepseek", "run-test-009")

            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT COUNT(*) FROM push_logs WHERE run_id = ?", ("run-test-009",)
            )
            count = cursor.fetchone()[0]
            conn.close()
            self.assertEqual(count, 0, "No push when all keys are empty")

    def test_push_valid_keys_gpt_load_config_not_found(self) -> None:
        """When provider_group_mapping points to nonexistent config, log and mark failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(db_path, workspace)
            # Add mapping pointing to nonexistent config (id=999)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO provider_group_mapping "
                "(provider_name, gpt_load_config_id, group_id, group_name) "
                "VALUES (?, ?, ?, ?)",
                ("kimi", 999, 3, "Kimi"),
            )
            conn.commit()
            conn.close()

            _write_valid_keys(workspace, "kimi", ["sk-kimi-key"])

            from web.push import PushService

            svc = PushService(db_path=db_path, workspace=workspace)
            svc.push_valid_keys("kimi", "run-test-010")

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM push_logs WHERE run_id = ?", ("run-test-010",))
            row = cursor.fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])


# ---------------------------------------------------------------------------
# get_push_service singleton tests
# ---------------------------------------------------------------------------


class TestPushServiceSingleton(unittest.TestCase):
    """Given the module,
    When get_push_service is called multiple times,
    Then the same PushService instance is returned.
    """

    def test_get_push_service_returns_singleton(self) -> None:
        """Module-level singleton factory returns the same object."""
        from web.push import get_push_service

        svc1 = get_push_service()
        svc2 = get_push_service()
        self.assertIs(svc1, svc2, "get_push_service must return the same instance")

    def test_push_service_has_push_valid_keys_method(self) -> None:
        """The singleton must have push_valid_keys(provider_name, run_id) method."""
        from web.push import get_push_service

        svc = get_push_service()
        self.assertTrue(
            callable(getattr(svc, "push_valid_keys", None)),
            "PushService must have push_valid_keys method",
        )


# ---------------------------------------------------------------------------
# Optional real gpt-load test (skip if unreachable)
# ---------------------------------------------------------------------------


class TestPushServiceRealGptLoad(unittest.TestCase):
    """Optional integration test against real gpt-load instance.

    Pushes a test key, verifies response, then cleans up via delete-multiple.
    Skipped if the instance is unreachable.
    """

    _GPT_LOAD_URL = "http://192.168.1.18:43001"
    _GPT_LOAD_AUTH = "sk-your-gpt-load-auth-key"
    _TEST_GROUP_ID = 2

    def setUp(self) -> None:
        """Check reachability; skip if down."""
        try:
            import requests

            resp = requests.get(f"{self._GPT_LOAD_URL}/api/groups", headers={"Authorization": f"Bearer {self._GPT_LOAD_AUTH}"}, timeout=3)
            if resp.status_code != 200:
                self.skipTest(f"gpt-load not available (status {resp.status_code})")
        except Exception as e:
            self.skipTest(f"gpt-load unreachable: {e}")

    def test_real_push_and_cleanup(self) -> None:
        """Push a test key to gpt-load v2, verify, then clean up."""
        import uuid

        import requests

        test_key = f"sk-test-{uuid.uuid4().hex[:12]}"

        # Push (v2 import contract)
        push_resp = requests.post(
            f"{self._GPT_LOAD_URL}/api/groups/{self._TEST_GROUP_ID}/credentials/import",
            json={"credentials": test_key},
            headers={
                "Authorization": f"Bearer {self._GPT_LOAD_AUTH}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            timeout=10,
        )
        self.assertEqual(push_resp.status_code, 200)
        data = push_resp.json()
        self.assertEqual(data.get("code"), 0)
        self.assertIsNotNone(data.get("data"))

        added = data["data"].get("credentials_added", 0)
        self.assertGreater(added, 0, "Test key should be newly added")

        # Cleanup: find and delete the test credential
        list_resp = requests.get(
            f"{self._GPT_LOAD_URL}/api/groups/{self._TEST_GROUP_ID}/credentials",
            headers={"Authorization": f"Bearer {self._GPT_LOAD_AUTH}"},
            timeout=10,
        )
        if list_resp.status_code == 200:
            items = (list_resp.json().get("data") or {}).get("items") or []
            for item in items:
                if "sk-t" in str(item.get("mask", "")):
                    requests.delete(
                        f"{self._GPT_LOAD_URL}/api/groups/{self._TEST_GROUP_ID}"
                        f"/credentials/{item['credential_id']}",
                        headers={"Authorization": f"Bearer {self._GPT_LOAD_AUTH}"},
                        timeout=10,
                    )


if __name__ == "__main__":
    unittest.main()
