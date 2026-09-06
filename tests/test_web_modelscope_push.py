#!/usr/bin/env python3

"""TDD unit tests for web/modelscope_push.py — gpt-load key push service.

Given a temporary SQLite database and workspace with
providers/modelscope/valid-keys.txt,
When ModelScopePushService.push_valid_keys is called,
Then keys matching ``[0-9A-Za-z_-]{20,}`` are POSTed in chunks to
{base}/api/keys/add-multiple and ONE push_logs row is written.

Test style mirrors tests/test_web_agnes_ai_push.py: unittest + unittest.mock,
temp SQLite via web.db.init_db, temp workspace, and
patch("web.modelscope_push.requests.post").
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import requests

from web.db import init_db as _init_db_async
from web.modelscope_push import ModelScopePushService


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
    return os.path.join(tmpdir, "test_modelscope_push.db")


def _init_schema(db_path: str) -> None:
    """Initialize the full schema (incl. push_logs) in a temp database."""
    _run_async(_init_db_async(db_path))


def _write_valid_keys(workspace: str, keys: list[str]) -> None:
    """Write providers/modelscope/valid-keys.txt in the workspace."""
    provider_dir = Path(workspace) / "providers" / "modelscope"
    provider_dir.mkdir(parents=True, exist_ok=True)
    vk_path = provider_dir / "valid-keys.txt"
    vk_path.write_text("\n".join(keys), encoding="utf-8")


def _valid_key(i: int) -> str:
    """Build a unique key matching the module pre-filter ([0-9A-Za-z_-]{20,})."""
    return f"MS{i:08d}KEY{i:06d}XY"


def _resp200(payload: dict) -> MagicMock:
    """Build a MagicMock requests.Response with status 200 and a JSON payload."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.text = ""
    return resp


def _resp401() -> MagicMock:
    """Build a MagicMock requests.Response with status 401 (unauthorized)."""
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {}
    resp.text = "Unauthorized"
    return resp


def _resp400_no_valid_keys() -> MagicMock:
    """Build a MagicMock requests.Response with status 400 and a server
    body indicating the chunk contains no valid keys."""
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = {}
    resp.text = '{"code":400,"message":"no valid keys"}'
    return resp


def _make_service(
    db_path: str,
    workspace: str,
    *,
    base_url: str = "http://127.0.0.1:19999",
    auth_key: str = "",
    group_id: int = 13,
) -> ModelScopePushService:
    return ModelScopePushService(
        base_url=base_url,
        auth_key=auth_key,
        group_id=group_id,
        workspace=workspace,
        db_path=db_path,
    )


@contextmanager
def _clear_modelscope_env() -> Iterator[None]:
    """Temporarily remove all MODELSCOPE_LOAD_* env vars so defaults apply."""
    modelscope_vars = (
        "MODELSCOPE_LOAD_BASE_URL",
        "MODELSCOPE_LOAD_AUTH_KEY",
        "MODELSCOPE_LOAD_GROUP_ID",
    )
    saved = {
        var: os.environ.pop(var) for var in modelscope_vars if var in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(saved)


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


# ---------------------------------------------------------------------------
# ModelScopePushService tests
# ---------------------------------------------------------------------------


class TestModelScopePushServiceBasic(unittest.TestCase):
    """Given a temp DB + workspace,
    When push_valid_keys is called,
    Then valid ModelScope keys are chunked-POSTed to {base}/api/keys/add-multiple
    and one push_logs row is written.
    """

    def test_push_happy_path_single_post_success(self) -> None:
        """Env-configured base/group; 2 valid keys + 1 junk line → one POST,
        junk counted ignored, no Authorization header, row success."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            key1 = _valid_key(1)
            key2 = _valid_key(2)
            _write_valid_keys(workspace, [key1, key2, "not-a-key"])

            env = {
                "MODELSCOPE_LOAD_BASE_URL": "http://127.0.0.1:9",
                "MODELSCOPE_LOAD_GROUP_ID": "13",
                "MODELSCOPE_LOAD_AUTH_KEY": "",
            }
            with patch.dict(os.environ, env, clear=False):
                svc = ModelScopePushService(db_path=db_path, workspace=workspace)

            with patch(
                "web.modelscope_push.requests.post",
                return_value=_resp200(
                    {"data": {"added_count": 2, "ignored_count": 0}}
                ),
            ) as mock_post:
                svc.push_valid_keys("modelscope", "run-test-001")

            self.assertEqual(mock_post.call_count, 1, "exactly ONE POST call")
            call_args = mock_post.call_args
            self.assertEqual(
                call_args[0][0], "http://127.0.0.1:9/api/keys/add-multiple"
            )
            self.assertEqual(
                call_args[1]["json"]["group_id"], 13, "group_id from env"
            )
            self.assertEqual(
                call_args[1]["json"]["keys_text"],
                f"{key1}\n{key2}",
                "only the 2 valid keys joined by newline; junk line absent",
            )
            self.assertNotIn(
                "Authorization", call_args[1]["headers"], "no auth header when unset"
            )
            self.assertEqual(
                call_args[1]["headers"]["Content-Type"], "application/json"
            )
            self.assertEqual(call_args[1]["timeout"], 30)

            row = _fetch_log(db_path, "run-test-001")
            self.assertIsNotNone(row, "push_logs entry should exist")
            self.assertEqual(row["provider_name"], "modelscope")
            self.assertEqual(row["gpt_load_config_id"], 0)
            self.assertEqual(row["group_id"], 13)
            self.assertEqual(row["keys_count"], 3, "3 deduped lines read")
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 1, "the 1 non-matching line")
            self.assertEqual(row["status"], "success")

    def test_push_server_ignored_accumulates(self) -> None:
        """Server-side ignored_count accumulates into the row's ignored_count."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(1), _valid_key(2)])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.modelscope_push.requests.post",
                return_value=_resp200(
                    {"data": {"added_count": 1, "ignored_count": 1}}
                ),
            ):
                svc.push_valid_keys("modelscope", "run-test-002")

            row = _fetch_log(db_path, "run-test-002")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 1, "server-ignored accumulates")
            self.assertEqual(row["status"], "success")

    def test_push_auth_header_sent_when_configured(self) -> None:
        """MODELSCOPE_LOAD_AUTH_KEY env → POST carries Authorization: Bearer."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(1)])

            with patch.dict(
                os.environ, {"MODELSCOPE_LOAD_AUTH_KEY": "secret"}, clear=False
            ):
                svc = ModelScopePushService(db_path=db_path, workspace=workspace)

            with patch(
                "web.modelscope_push.requests.post",
                return_value=_resp200(
                    {"data": {"added_count": 1, "ignored_count": 0}}
                ),
            ) as mock_post:
                svc.push_valid_keys("modelscope", "run-test-003")

            headers = mock_post.call_args[1]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_push_connection_error_retries_then_failed_no_raise(self) -> None:
        """ConnectionError → 4 attempts (1+3 backoff retries), no raise, row failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(1), _valid_key(2)])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.modelscope_push.requests.post",
                side_effect=requests.ConnectionError("target down"),
            ) as mock_post, patch("web.modelscope_push.time.sleep") as mock_sleep:
                svc.push_valid_keys("modelscope", "run-test-004")  # must not raise

            self.assertEqual(mock_post.call_count, 4, "1 initial + 3 retries")
            self.assertEqual(
                [c.args[0] for c in mock_sleep.call_args_list],
                [1.0, 3.0, 9.0],
                "backoff must be 1s, 3s, 9s",
            )

            row = _fetch_log(db_path, "run-test-004")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_same_run_id_only_once(self) -> None:
        """Second call with the same run_id does not re-POST keys."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(1), _valid_key(2)])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.modelscope_push.requests.post",
                return_value=_resp200(
                    {"data": {"added_count": 2, "ignored_count": 0}}
                ),
            ) as mock_post:
                svc.push_valid_keys("modelscope", "run-test-005")
                svc.push_valid_keys("modelscope", "run-test-005")

            self.assertEqual(mock_post.call_count, 1, "second call must be a no-op")
            self.assertEqual(_count_logs(db_path, "run-test-005"), 1)

    def test_push_chunks_over_max_keys_per_post(self) -> None:
        """501 valid keys → 2 POST calls (500 + 1)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(i) for i in range(501)])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.modelscope_push.requests.post",
                return_value=_resp200(
                    {"data": {"added_count": 0, "ignored_count": 0}}
                ),
            ) as mock_post:
                svc.push_valid_keys("modelscope", "run-test-006")

            self.assertEqual(mock_post.call_count, 2, "501 keys → 500 + 1 chunks")
            first_call = mock_post.call_args_list[0]
            second_call = mock_post.call_args_list[1]
            self.assertEqual(
                len(first_call[1]["json"]["keys_text"].splitlines()), 500
            )
            self.assertEqual(
                len(second_call[1]["json"]["keys_text"].splitlines()), 1
            )

            row = _fetch_log(db_path, "run-test-006")
            self.assertIsNotNone(row)
            self.assertEqual(row["keys_count"], 501)
            self.assertEqual(row["status"], "success")

    def test_push_unexpected_exception_best_effort_no_raise(self) -> None:
        """Unexpected exception from requests → no raise, best-effort failed row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(1)])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.modelscope_push.requests.post",
                side_effect=RuntimeError("boom"),
            ):
                svc.push_valid_keys("modelscope", "run-test-007")  # must not raise

            row = _fetch_log(db_path, "run-test-007")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")

    def test_push_env_defaults_when_unset(self) -> None:
        """No MODELSCOPE_LOAD_* env → default base url and group id 13."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)

            with _clear_modelscope_env():
                svc = ModelScopePushService(db_path=db_path, workspace=workspace)

            self.assertEqual(svc._base_url, "http://192.168.1.18:43001")
            self.assertEqual(svc._group_id, 13)

    def test_push_skips_non_modelscope_provider(self) -> None:
        """provider_name != modelscope → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(1)])

            svc = _make_service(db_path, workspace)

            with patch("web.modelscope_push.requests.post") as mock_post:
                svc.push_valid_keys("deepseek", "run-test-008")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-008"), 0)

    def test_push_no_valid_keys_skips_without_row(self) -> None:
        """No line passes the pre-filter ([0-9A-Za-z_-]{20,}) → no POST, no row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["with.dot", "spaced out long string"])

            svc = _make_service(db_path, workspace)

            with patch("web.modelscope_push.requests.post") as mock_post:
                svc.push_valid_keys("modelscope", "run-test-009")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-009"), 0)

    def test_push_401_fail_fast_stops_remaining_chunks(self) -> None:
        """HTTP 401 → unauthorized, stop remaining chunks, row failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(i) for i in range(501)])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.modelscope_push.requests.post",
                return_value=_resp401(),
            ) as mock_post:
                svc.push_valid_keys("modelscope", "run-test-010")  # must not raise

            self.assertEqual(
                mock_post.call_count, 1, "401 must fail fast: no second chunk POST"
            )

            row = _fetch_log(db_path, "run-test-010")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_400_no_valid_keys_treated_as_noop_success(self) -> None:
        """HTTP 400 'no valid keys' → success, 0 added / 0 ignored, no raise."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_valid_key(1), _valid_key(2)])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.modelscope_push.requests.post",
                return_value=_resp400_no_valid_keys(),
            ):
                svc.push_valid_keys("modelscope", "run-test-011")  # must not raise

            row = _fetch_log(db_path, "run-test-011")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 0)
            self.assertEqual(row["status"], "success")
            self.assertIsNone(row["error_message"])


# ---------------------------------------------------------------------------
# get_modelscope_push_service singleton tests
# ---------------------------------------------------------------------------


class TestModelScopePushServiceSingleton(unittest.TestCase):
    """Given the module,
    When get_modelscope_push_service is called multiple times,
    Then the same ModelScopePushService instance is returned.
    """

    def test_get_modelscope_push_service_returns_singleton(self) -> None:
        """Module-level singleton factory returns the same object."""
        from web.modelscope_push import get_modelscope_push_service

        svc1 = get_modelscope_push_service()
        svc2 = get_modelscope_push_service()
        self.assertIs(
            svc1, svc2, "get_modelscope_push_service must return the same instance"
        )

    def test_modelscope_push_service_has_push_valid_keys_method(self) -> None:
        """The singleton must have push_valid_keys(provider_name, run_id) method."""
        from web.modelscope_push import get_modelscope_push_service

        svc = get_modelscope_push_service()
        self.assertTrue(
            callable(getattr(svc, "push_valid_keys", None)),
            "ModelScopePushService must have push_valid_keys method",
        )


if __name__ == "__main__":
    unittest.main()