#!/usr/bin/env python3

"""Generic gpt-load push idempotency (audit JOB 2).

web/runner.py can invoke the completion hook twice for one run — once via
the TaskManager completion listener and once via the direct
_push_completed_tasks call — and a sibling change is making
HarvesterApp.initialize() idempotent, which may bring the listener path
alive. Every per-provider push service already guards with a seen-run-ids
set (web/tavily_push.py, web/agnes_ai_push.py); web/push.py must do the
same so the double invocation performs ONE HTTP push and writes ONE
push_logs row.

The guard is keyed by (provider_name, run_id) — one config can define
multiple tasks (e.g. mimo-cn + mimo-sg) that share a run_id but push
different valid-keys files; those must BOTH push.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from web.crypto import encrypt_str
from web.db import init_db as _init_db_async


def _run_async(coro):
    return asyncio.run(coro)


def _seed_db(db_path: str, providers: list[tuple[str, int]] | None = None) -> None:
    """Schema + one gpt_load_config + a mapping per (provider, group_id)."""
    _run_async(_init_db_async(db_path))
    conn = sqlite3.connect(db_path)
    try:
        auth_enc = encrypt_str("test-auth-key-for-push")
        conn.execute(
            "INSERT INTO gpt_load_config (name, base_url, auth_key_encrypted) "
            "VALUES (?, ?, ?)",
            ("test-gptload", "http://127.0.0.1:19999", auth_enc),
        )
        for name, group_id in providers or [("deepseek", 2)]:
            conn.execute(
                "INSERT INTO provider_group_mapping "
                "(provider_name, gpt_load_config_id, group_id, group_name) "
                "VALUES (?, 1, ?, ?)",
                (name, group_id, name),
            )
        conn.commit()
    finally:
        conn.close()


def _write_valid_keys(workspace: str, provider: str, keys: list[str]) -> None:
    provider_dir = Path(workspace) / "providers" / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    (provider_dir / "valid-keys.txt").write_text(
        "\n".join(keys), encoding="utf-8"
    )


def _ok_response(added: int = 2) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "code": 0,
        "data": {"added_count": added, "ignored_count": 0, "total_in_group": added},
    }
    return resp


def _push_log_count(db_path: str, run_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM push_logs WHERE run_id = ?", (run_id,)
        )
        return cursor.fetchone()[0]
    finally:
        conn.close()


class TestPushServiceRunIdempotency(unittest.TestCase):
    """Given a configured PushService (mirrors the runner's singleton, which
    is shared by BOTH hook paths),
    When push_valid_keys is invoked twice for the same (provider, run_id),
    Then exactly ONE HTTP push happens and ONE push_logs row is written.
    """

    def setUp(self) -> None:
        self._db_tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self._db_tmp, "push_idem.db")

    def test_same_provider_and_run_id_pushes_once(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(self.db_path)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa", "sk-key-bbbb"])

            from web.push import PushService

            svc = PushService(db_path=self.db_path, workspace=workspace)

            with patch(
                "web.push.requests.post", return_value=_ok_response(2)
            ) as mock_post:
                svc.push_valid_keys("deepseek", "run-idem-1")
                svc.push_valid_keys("deepseek", "run-idem-1")

                self.assertEqual(
                    mock_post.call_count, 1, "double hook fire must push once"
                )

            self.assertEqual(_push_log_count(self.db_path, "run-idem-1"), 1)

    def test_different_run_ids_each_push(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(self.db_path)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            svc = PushService(db_path=self.db_path, workspace=workspace)

            with patch(
                "web.push.requests.post", return_value=_ok_response(1)
            ) as mock_post:
                svc.push_valid_keys("deepseek", "run-idem-2")
                svc.push_valid_keys("deepseek", "run-idem-3")

                self.assertEqual(mock_post.call_count, 2)

            self.assertEqual(_push_log_count(self.db_path, "run-idem-2"), 1)
            self.assertEqual(_push_log_count(self.db_path, "run-idem-3"), 1)

    def test_same_run_id_different_tasks_both_push(self) -> None:
        """Multi-task config (mimo-cn + mimo-sg under ONE run_id): the guard
        must not swallow the second task's push."""
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(self.db_path, providers=[("mimo-cn", 2), ("mimo-sg", 3)])
            _write_valid_keys(workspace, "mimo-cn", ["tp-key-cn"])
            _write_valid_keys(workspace, "mimo-sg", ["tp-key-sg"])

            from web.push import PushService

            svc = PushService(db_path=self.db_path, workspace=workspace)

            with patch(
                "web.push.requests.post", return_value=_ok_response(1)
            ) as mock_post:
                svc.push_valid_keys("mimo-cn", "run-multi")
                svc.push_valid_keys("mimo-sg", "run-multi")

                self.assertEqual(mock_post.call_count, 2)
                groups = {
                    call[1]["json"]["group_id"] for call in mock_post.call_args_list
                }
                self.assertEqual(groups, {2, 3})

            self.assertEqual(_push_log_count(self.db_path, "run-multi"), 2)

    def test_duplicate_invocation_logs_and_writes_nothing(self) -> None:
        """The skipped second call is silent: no push_logs row, no POST."""
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(self.db_path)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            svc = PushService(db_path=self.db_path, workspace=workspace)

            with patch("web.push.requests.post", return_value=_ok_response(1)):
                svc.push_valid_keys("deepseek", "run-idem-4")
            with patch(
                "web.push.requests.post", return_value=_ok_response(1)
            ) as mock_post:
                svc.push_valid_keys("deepseek", "run-idem-4")
                mock_post.assert_not_called()

            self.assertEqual(_push_log_count(self.db_path, "run-idem-4"), 1)

    def test_separate_instances_do_not_share_guard(self) -> None:
        """The guard is per-instance state, not a DB constraint: two distinct
        PushService objects each push (the runner's double-fire goes through
        the ONE get_push_service() singleton, which is what dedupes)."""
        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(self.db_path)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            with patch(
                "web.push.requests.post", return_value=_ok_response(1)
            ) as mock_post:
                PushService(db_path=self.db_path, workspace=workspace).push_valid_keys(
                    "deepseek", "run-idem-5"
                )
                PushService(db_path=self.db_path, workspace=workspace).push_valid_keys(
                    "deepseek", "run-idem-5"
                )
                self.assertEqual(mock_post.call_count, 2)

    def test_guard_is_thread_safe_under_double_fire(self) -> None:
        """Listener path and scan-thread path fire from DIFFERENT threads;
        the lock must make exactly one win."""
        import threading

        with tempfile.TemporaryDirectory() as workspace:
            _seed_db(self.db_path)
            _write_valid_keys(workspace, "deepseek", ["sk-key-aaaa"])

            from web.push import PushService

            svc = PushService(db_path=self.db_path, workspace=workspace)
            barrier = threading.Barrier(2)

            with patch(
                "web.push.requests.post", return_value=_ok_response(1)
            ) as mock_post:
                threads = [
                    threading.Thread(
                        target=lambda: (
                            barrier.wait(),
                            svc.push_valid_keys("deepseek", "run-race"),
                        )
                    )
                    for _ in range(2)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                self.assertEqual(mock_post.call_count, 1)

            self.assertEqual(_push_log_count(self.db_path, "run-race"), 1)


if __name__ == "__main__":
    unittest.main()
