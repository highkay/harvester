#!/usr/bin/env python3

"""TDD unit tests for web/serpapi_push.py — SerpApi key-pool push service.

Given a temporary SQLite database and workspace with providers/serpapi/valid-keys.txt,
When SerpapiPushService.push_valid_keys is called,
Then each 32-hex key is POSTed to {base}/api/keys and ONE push_logs row is written.

Test style mirrors tests/test_web_tavily_push.py: unittest + unittest.mock, temp
SQLite via web.db.init_db, temp workspace, and patch("web.serpapi_push.requests.post").
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from web.db import init_db as _init_db_async
from web.serpapi_push import SerpapiPushService


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
    return os.path.join(tmpdir, "test_serpapi_push.db")


def _init_schema(db_path: str) -> None:
    """Initialize the full schema (incl. push_logs) in a temp database."""
    _run_async(_init_db_async(db_path))


def _write_valid_keys(workspace: str, keys: list[str]) -> None:
    """Write providers/serpapi/valid-keys.txt in the workspace."""
    provider_dir = Path(workspace) / "providers" / "serpapi"
    provider_dir.mkdir(parents=True, exist_ok=True)
    vk_path = provider_dir / "valid-keys.txt"
    vk_path.write_text("\n".join(keys), encoding="utf-8")


def _resp(status_code: int, error: str | None = None) -> MagicMock:
    """Build a MagicMock requests.Response with the given status/error body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"error": error} if error is not None else {}
    resp.text = ""
    return resp


def _make_service(
    db_path: str,
    workspace: str,
    *,
    base_url: str = "http://127.0.0.1:19999",
    auth_key: str = "test-master-key",
) -> SerpapiPushService:
    return SerpapiPushService(
        base_url=base_url, auth_key=auth_key, workspace=workspace, db_path=db_path
    )


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
# SerpapiPushService tests
# ---------------------------------------------------------------------------


class TestSerpapiPushServiceBasic(unittest.TestCase):
    """Given a temp DB + workspace,
    When push_valid_keys is called,
    Then keys are POSTed per the response taxonomy and one push_logs row is written.
    """

    def test_push_200_added_and_success(self) -> None:
        """All keys return 200 → added_count matches, status success."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "0123456789abcdef0123456789abcdef",
                    "fedcba9876543210fedcba9876543210",
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ],
            )

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-001")

            self.assertEqual(mock_post.call_count, 3)

            row = _fetch_log(db_path, "run-test-001")
            self.assertIsNotNone(row, "push_logs entry should exist")
            self.assertEqual(row["provider_name"], "serpapi")
            self.assertEqual(row["gpt_load_config_id"], 0)
            self.assertEqual(row["group_id"], 0)
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 3)
            self.assertEqual(row["ignored_count"], 0)
            self.assertEqual(row["status"], "success")

    def test_push_400_create_failed_counted_ignored(self) -> None:
        """400 create_failed (duplicate) → ignored, status success."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "0123456789abcdef0123456789abcdef",
                    "fedcba9876543210fedcba9876543210",
                ],
            )

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post",
                return_value=_resp(400, "create_failed"),
            ):
                svc.push_valid_keys("serpapi", "run-test-002")

            row = _fetch_log(db_path, "run-test-002")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 2)
            self.assertEqual(row["status"], "success")

    def test_push_400_invalid_key_format_counted_ignored(self) -> None:
        """400 invalid_key_format (defensive) → ignored, no failure."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post",
                return_value=_resp(400, "invalid_key_format"),
            ):
                svc.push_valid_keys("serpapi", "run-test-003")

            row = _fetch_log(db_path, "run-test-003")
            self.assertIsNotNone(row)
            self.assertEqual(row["ignored_count"], 1)
            self.assertEqual(row["status"], "success")

    def test_push_401_fail_fast_stops_remaining_keys(self) -> None:
        """401 → stop iterating immediately; remaining keys NOT POSTed, status failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "0123456789abcdef0123456789abcdef",
                    "11111111111111111111111111111111",
                    "22222222222222222222222222222222",
                ],
            )

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post",
                return_value=_resp(401, "unauthorized"),
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-004")

            self.assertEqual(
                mock_post.call_count, 1, "401 must fail-fast: only first key POSTed"
            )

            row = _fetch_log(db_path, "run-test-004")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error_message"], "unauthorized (invalid master key)")

    def test_push_429_retries_4_attempts_with_backoff(self) -> None:
        """429 → 4 total attempts with 1s/3s/9s backoff, exhausted → failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(429)
            ) as mock_post, patch("web.serpapi_push.time.sleep") as mock_sleep:
                svc.push_valid_keys("serpapi", "run-test-005")

            self.assertEqual(mock_post.call_count, 4, "1 initial + 3 retries")
            self.assertEqual(
                [c.args[0] for c in mock_sleep.call_args_list],
                [1.0, 3.0, 9.0],
                "backoff must be 1s, 3s, 9s",
            )

            row = _fetch_log(db_path, "run-test-005")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_mixed_results_partial(self) -> None:
        """added + duplicate + exhausted-5xx → status partial."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "0123456789abcdef0123456789abcdef",
                    "abcdefabcdefabcdefabcdefabcdef",
                    "99999999999999999999999999999999",
                ],
            )

            svc = _make_service(db_path, workspace)

            def _side_effect(*args, **kwargs):
                key = kwargs["json"]["key"]
                if key == "0123456789abcdef0123456789abcdef":
                    return _resp(200)
                if key == "abcdefabcdefabcdefabcdefabcdef":
                    return _resp(400, "create_failed")
                return _resp(500)

            with patch(
                "web.serpapi_push.requests.post", side_effect=_side_effect
            ), patch("web.serpapi_push.time.sleep"):
                svc.push_valid_keys("serpapi", "run-test-006")

            row = _fetch_log(db_path, "run-test-006")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 1)
            self.assertIsNotNone(row["error_message"])

    def test_push_skips_non_serpapi_provider(self) -> None:
        """provider_name != serpapi → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with patch("web.serpapi_push.requests.post") as mock_post:
                svc.push_valid_keys("deepseek", "run-test-007")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-007"), 0)

    def test_push_skips_when_base_url_empty(self) -> None:
        """base_url empty → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace, base_url="")

            with patch("web.serpapi_push.requests.post") as mock_post:
                svc.push_valid_keys("serpapi", "run-test-008")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-008"), 0)

    def test_push_skips_when_auth_key_empty(self) -> None:
        """auth_key empty → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace, auth_key="")

            with patch("web.serpapi_push.requests.post") as mock_post:
                svc.push_valid_keys("serpapi", "run-test-009")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-009"), 0)

    def test_push_url_exact_after_rstrip(self) -> None:
        """Trailing slash on base_url is stripped → URL is exactly {base}/api/keys."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(
                db_path, workspace, base_url="http://127.0.0.1:19999/"
            )

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-010")

            call_args = mock_post.call_args
            self.assertEqual(
                call_args[0][0],
                "http://127.0.0.1:19999/api/keys",
                "URL must be exactly {base}/api/keys with no trailing slash",
            )

    def test_push_body_and_headers_shape(self) -> None:
        """POST body is {"key", "alias": "harvester"} and headers carry Bearer auth."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-011")

            call_args = mock_post.call_args
            body = call_args[1]["json"]
            self.assertEqual(
                body,
                {"key": "0123456789abcdef0123456789abcdef", "alias": "harvester"},
                "body must be exactly {'key', 'alias': 'harvester'}",
            )
            headers = call_args[1]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer test-master-key")
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(call_args[1]["timeout"], 30)

    def test_push_non_hex_prefiltered_ignored(self) -> None:
        """Only 32-hex keys are POSTed; other lines (deduped) count into ignored."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "0123456789abcdef0123456789abcdef",
                    "0123456789abcdef0123456789abcdef",
                    "sk-deadbeefnotaserpapikey",
                    "",
                    "tvly-other-provider-key",
                    "0123456789ABCDEF0123456789ABCDEF",
                ],
            )

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-012")

            self.assertEqual(
                mock_post.call_count, 2, "only the deduped 32-hex keys are POSTed"
            )

            row = _fetch_log(db_path, "run-test-012")
            self.assertIsNotNone(row)
            self.assertEqual(row["keys_count"], 4, "deduped total lines read")
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 2, "sk- + tvly- lines")
            self.assertEqual(row["status"], "success")

    def test_push_same_run_id_only_once(self) -> None:
        """Second call with the same run_id does not re-POST keys."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "0123456789abcdef0123456789abcdef",
                    "fedcba9876543210fedcba9876543210",
                ],
            )

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-013")
                svc.push_valid_keys("serpapi", "run-test-013")

            self.assertEqual(
                mock_post.call_count, 2, "second call must be a no-op"
            )
            self.assertEqual(_count_logs(db_path, "run-test-013"), 1)

    def test_push_unexpected_exception_best_effort_no_raise(self) -> None:
        """Unexpected exception from requests → no raise, best-effort failed row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post",
                side_effect=RuntimeError("boom"),
            ):
                svc.push_valid_keys("serpapi", "run-test-014")  # must not raise

            row = _fetch_log(db_path, "run-test-014")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
    def test_push_uses_env_defaults_when_not_injected(self) -> None:
        """Constructor falls back to SERPAPI_PROXY_* env vars when not injected."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            env = {
                "SERPAPI_PROXY_BASE_URL": "http://env-host:48080",
                "SERPAPI_PROXY_AUTH_KEY": "env-master-key",
            }
            with patch.dict(os.environ, env, clear=False):
                svc = SerpapiPushService(workspace=workspace, db_path=db_path)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-015")

            call_args = mock_post.call_args
            self.assertEqual(call_args[0][0], "http://env-host:48080/api/keys")
            self.assertEqual(
                call_args[1]["headers"]["Authorization"],
                "Bearer env-master-key",
            )

    def test_prefilter_accepts_uppercase_hex(self) -> None:
        """Uppercase hex keys normalize-accept and are pushed (validation already passed)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789ABCDEF0123456789ABCDEF"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-016")

            self.assertEqual(mock_post.call_count, 1)
            body = mock_post.call_args[1]["json"]
            self.assertEqual(body["key"], "0123456789ABCDEF0123456789ABCDEF")

    def test_prefilter_accepts_length_range(self) -> None:
        """20–64 char hex keys pass the pre-filter (validation is length-independent)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "a" * 20,
                    "b" * 32,
                    "c" * 64,
                ],
            )

            svc = _make_service(db_path, workspace)

            with patch(
                "web.serpapi_push.requests.post", return_value=_resp(200)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-017")

            self.assertEqual(mock_post.call_count, 3)
            row = _fetch_log(db_path, "run-test-017")
            self.assertEqual(row["added_count"], 3)
            self.assertEqual(row["ignored_count"], 0)

    def test_prefilter_rejects_outside_length_range(self) -> None:
        """19-char and 65-char hex runs are ignored by the pre-filter."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["a" * 19, "b" * 65])

            svc = _make_service(db_path, workspace)

            with patch("web.serpapi_push.requests.post") as mock_post:
                svc.push_valid_keys("serpapi", "run-test-018")

            mock_post.assert_not_called()
            row = _fetch_log(db_path, "run-test-018")
            self.assertEqual(row["keys_count"], 2)
            self.assertEqual(row["ignored_count"], 2)
            self.assertEqual(row["status"], "success")


# ---------------------------------------------------------------------------
# get_serpapi_push_service singleton tests
# ---------------------------------------------------------------------------


class TestSerpapiPushServiceSingleton(unittest.TestCase):
    """Given the module,
    When get_serpapi_push_service is called multiple times,
    Then the same SerpapiPushService instance is returned.
    """

    def test_get_serpapi_push_service_returns_singleton(self) -> None:
        """Module-level singleton factory returns the same object."""
        from web.serpapi_push import get_serpapi_push_service

        svc1 = get_serpapi_push_service()
        svc2 = get_serpapi_push_service()
        self.assertIs(
            svc1, svc2, "get_serpapi_push_service must return the same instance"
        )

    def test_serpapi_push_service_has_push_valid_keys_method(self) -> None:
        """The singleton must have push_valid_keys(provider_name, run_id) method."""
        from web.serpapi_push import get_serpapi_push_service

        svc = get_serpapi_push_service()
        self.assertTrue(
            callable(getattr(svc, "push_valid_keys", None)),
            "SerpapiPushService must have push_valid_keys method",
        )


if __name__ == "__main__":
    unittest.main()