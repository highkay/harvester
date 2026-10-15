#!/usr/bin/env python3

"""TDD unit tests for web/tavily_push.py — tavily-hikari batch push service.

Given a temporary SQLite database and workspace with providers/tavily/valid-keys.txt,
When TavilyPushService.push_valid_keys is called,
Then tvly- keys are POSTed in chunks to {base}/api/keys/batch and ONE push_logs
row is written, with added/ignored/failures folded from the per-item batch
``status`` values (created/undeleted -> added; existed/duplicate_in_input ->
ignored; failed/unknown -> failure).

Test style mirrors tests/test_web_push.py: unittest + unittest.mock, temp SQLite
via web.db.init_db, temp workspace, and patch("web.tavily_push.requests.post").
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from web.db import init_db as _init_db_async
from web.tavily_push import TavilyPushService


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
    return os.path.join(tmpdir, "test_tavily_push.db")


def _init_schema(db_path: str) -> None:
    """Initialize the full schema (incl. push_logs) in a temp database."""
    _run_async(_init_db_async(db_path))


def _write_valid_keys(workspace: str, keys: list[str]) -> None:
    """Write providers/tavily/valid-keys.txt in the workspace."""
    provider_dir = Path(workspace) / "providers" / "tavily"
    provider_dir.mkdir(parents=True, exist_ok=True)
    vk_path = provider_dir / "valid-keys.txt"
    vk_path.write_text("\n".join(keys), encoding="utf-8")


def _resp(status_code: int, text: str = "") -> MagicMock:
    """Build a MagicMock requests.Response for a non-200 status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {}
    resp.text = text
    return resp


def _batch_resp(items: list[tuple[str, str | None]]) -> MagicMock:
    """Build a MagicMock HTTP 200 response with per-item batch results.

    ``items`` is a list of ``(status, error)`` pairs; the response mirrors the
    hikari ``BatchCreateKeysResponse`` shape (``{"summary", "results"}``).
    """
    resp = MagicMock()
    resp.status_code = 200
    results = []
    for i, (status, err) in enumerate(items):
        entry: dict[str, str] = {"api_key": f"k{i}", "status": status}
        if err:
            entry["error"] = err
        results.append(entry)
    resp.json.return_value = {"summary": {}, "results": results}
    resp.text = ""
    return resp


def _echo_created(*args, **kwargs) -> MagicMock:
    """Side effect that answers a chunk with one ``created`` item per key."""
    return _batch_resp([("created", None)] * len(kwargs["json"]["api_keys"]))


def _make_service(
    db_path: str,
    workspace: str,
    *,
    base_url: str = "http://127.0.0.1:19999",
    auth_key: str = "test-master-key",
) -> TavilyPushService:
    return TavilyPushService(
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
# TavilyPushService tests
# ---------------------------------------------------------------------------


class TestTavilyPushServiceBasic(unittest.TestCase):
    """Given a temp DB + workspace,
    When push_valid_keys is called,
    Then keys are batch-POSTed per the response taxonomy and one push_logs row is written.
    """

    def test_push_batch_created_and_existed_accounting(self) -> None:
        """created/undeleted count as added; existed counts as ignored."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa", "tvly-bbbb", "tvly-cccc"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post",
                return_value=_batch_resp(
                    [("created", None), ("undeleted", None), ("existed", None)]
                ),
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-001")

            self.assertEqual(mock_post.call_count, 1, "one chunk = one POST")

            row = _fetch_log(db_path, "run-test-001")
            self.assertIsNotNone(row, "push_logs entry should exist")
            self.assertEqual(row["provider_name"], "tavily")
            self.assertEqual(row["gpt_load_config_id"], 0)
            self.assertEqual(row["group_id"], 0)
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 1)
            self.assertEqual(row["status"], "success")

    def test_push_body_and_headers_shape(self) -> None:
        """POST body is {"api_keys": [...]} and headers carry Bearer auth."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post",
                return_value=_batch_resp([("created", None)]),
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-002")

            call_args = mock_post.call_args
            body = call_args[1]["json"]
            self.assertEqual(
                body, {"api_keys": ["tvly-aaaa"]},
                "body must be exactly {'api_keys': [...]} (snake_case)",
            )
            headers = call_args[1]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer test-master-key")
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(call_args[1]["timeout"], 30)

    def test_push_url_exact_after_rstrip(self) -> None:
        """Trailing slash is stripped → URL is exactly {base}/api/keys/batch."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(
                db_path, workspace, base_url="http://127.0.0.1:19999/"
            )

            with patch(
                "web.tavily_push.requests.post",
                return_value=_batch_resp([("created", None)]),
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-003")

            self.assertEqual(
                mock_post.call_args[0][0],
                "http://127.0.0.1:19999/api/keys/batch",
                "URL must be exactly {base}/api/keys/batch",
            )

    def test_push_chunks_split_at_chunk_size(self) -> None:
        """More keys than _BATCH_CHUNK_SIZE → multiple POSTs with full chunks."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace, [f"tvly-{i}" for i in range(5)]
            )

            svc = _make_service(db_path, workspace)

            with patch("web.tavily_push._BATCH_CHUNK_SIZE", 2), patch(
                "web.tavily_push.requests.post", side_effect=_echo_created
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-004")

            self.assertEqual(mock_post.call_count, 3, "5 keys / chunk 2 = 3 POSTs")
            sizes = [len(c[1]["json"]["api_keys"]) for c in mock_post.call_args_list]
            self.assertEqual(sizes, [2, 2, 1])

            row = _fetch_log(db_path, "run-test-004")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 5)
            self.assertEqual(row["status"], "success")

    def test_push_unauthorized_403_fail_fast_stops_remaining_chunks(self) -> None:
        """403 (not admin) → stop immediately; remaining chunks NOT POSTed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-a", "tvly-b", "tvly-c"])

            svc = _make_service(db_path, workspace)

            with patch("web.tavily_push._BATCH_CHUNK_SIZE", 1), patch(
                "web.tavily_push.requests.post", return_value=_resp(403)
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-005")

            self.assertEqual(
                mock_post.call_count, 1, "403 must fail-fast: only first chunk POSTed"
            )

            row = _fetch_log(db_path, "run-test-005")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(
                row["error_message"], "unauthorized (admin auth rejected)"
            )

    def test_push_unauthorized_401_fail_fast(self) -> None:
        """401 → fail-fast with the same unauthorized message."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post", return_value=_resp(401)
            ):
                svc.push_valid_keys("tavily", "run-test-006")

            row = _fetch_log(db_path, "run-test-006")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(
                row["error_message"], "unauthorized (admin auth rejected)"
            )

    def test_push_429_retries_4_attempts_with_backoff(self) -> None:
        """429 → 4 total attempts with 1s/3s/9s backoff, exhausted → failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post", return_value=_resp(429)
            ) as mock_post, patch("web.tavily_push.time.sleep") as mock_sleep:
                svc.push_valid_keys("tavily", "run-test-007")

            self.assertEqual(mock_post.call_count, 4, "1 initial + 3 retries")
            self.assertEqual(
                [c.args[0] for c in mock_sleep.call_args_list],
                [1.0, 3.0, 9.0],
                "backoff must be 1s, 3s, 9s",
            )

            row = _fetch_log(db_path, "run-test-007")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_500_then_success_recovers(self) -> None:
        """Transient 500 then 200 → the retry succeeds within the same push."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post",
                side_effect=[_resp(500), _batch_resp([("created", None)])],
            ) as mock_post, patch("web.tavily_push.time.sleep"):
                svc.push_valid_keys("tavily", "run-test-008")

            self.assertEqual(mock_post.call_count, 2)

            row = _fetch_log(db_path, "run-test-008")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 1)

    def test_push_partial_when_one_chunk_exhausts_retries(self) -> None:
        """Chunk 1 OK + chunk 2 dead (4 attempts) → partial with an error message."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-a", "tvly-b", "tvly-c", "tvly-d"])

            svc = _make_service(db_path, workspace)

            responses = [
                _batch_resp([("created", None), ("existed", None)]),
                _resp(500),
                _resp(500),
                _resp(500),
                _resp(500),
            ]
            with patch("web.tavily_push._BATCH_CHUNK_SIZE", 2), patch(
                "web.tavily_push.requests.post", side_effect=responses
            ) as mock_post, patch("web.tavily_push.time.sleep"):
                svc.push_valid_keys("tavily", "run-test-009")

            self.assertEqual(mock_post.call_count, 5, "1 + 4 attempts")

            row = _fetch_log(db_path, "run-test-009")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["keys_count"], 4)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 1)
            self.assertIsNotNone(row["error_message"])

    def test_push_item_failed_status_reported(self) -> None:
        """A per-item 'failed' status counts as failure and carries its error."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-ok", "tvly-bad"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post",
                return_value=_batch_resp(
                    [("created", None), ("failed", "database is locked")]
                ),
            ):
                svc.push_valid_keys("tavily", "run-test-010")

            row = _fetch_log(db_path, "run-test-010")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["added_count"], 1)
            self.assertIn("database is locked", row["error_message"])

    def test_push_missing_items_counted_as_failures(self) -> None:
        """A 200 whose results cover fewer keys than submitted → missing = failures."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-a", "tvly-b", "tvly-c"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post",
                return_value=_batch_resp([("created", None)]),
            ):
                svc.push_valid_keys("tavily", "run-test-011")

            row = _fetch_log(db_path, "run-test-011")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["added_count"], 1)
            self.assertIn("missing from response", row["error_message"])

    def test_push_200_unparseable_body_is_failed(self) -> None:
        """HTTP 200 with an unparseable body is not success (no silent loss)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace)

            bad = _resp(200)
            bad.json.side_effect = ValueError("not json")
            with patch(
                "web.tavily_push.requests.post", return_value=bad
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-012")

            self.assertEqual(mock_post.call_count, 1, "unparseable 200 is not retried")

            row = _fetch_log(db_path, "run-test-012")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_non_tvly_prefiltered_ignored(self) -> None:
        """Only tvly- keys are POSTed; other lines (deduped) count into ignored."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace, ["tvly-abc", "tvly-abc", "tavily-xyz", "", "sk-other"]
            )

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post", side_effect=_echo_created
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-013")

            self.assertEqual(
                mock_post.call_count, 1, "only the deduped tvly- key is POSTed"
            )
            body = mock_post.call_args[1]["json"]
            self.assertEqual(body["api_keys"], ["tvly-abc"])

            row = _fetch_log(db_path, "run-test-013")
            self.assertIsNotNone(row)
            self.assertEqual(row["keys_count"], 3, "deduped total lines read")
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 2, "tavily-xyz + sk-other")
            self.assertEqual(row["status"], "success")

    def test_push_skips_non_tavily_provider(self) -> None:
        """provider_name != tavily → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace)

            with patch("web.tavily_push.requests.post") as mock_post:
                svc.push_valid_keys("deepseek", "run-test-014")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-014"), 0)

    def test_push_skips_when_base_url_empty(self) -> None:
        """base_url empty → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace, base_url="")

            with patch("web.tavily_push.requests.post") as mock_post:
                svc.push_valid_keys("tavily", "run-test-015")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-015"), 0)

    def test_push_skips_when_auth_key_empty(self) -> None:
        """auth_key empty → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace, auth_key="")

            with patch("web.tavily_push.requests.post") as mock_post:
                svc.push_valid_keys("tavily", "run-test-016")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-016"), 0)

    def test_push_same_run_id_only_once(self) -> None:
        """Second call with the same run_id does not re-POST keys."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa", "tvly-bbbb"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post", side_effect=_echo_created
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-017")
                svc.push_valid_keys("tavily", "run-test-017")

            self.assertEqual(
                mock_post.call_count, 1, "second call must be a no-op"
            )
            self.assertEqual(_count_logs(db_path, "run-test-017"), 1)

    def test_push_unexpected_exception_best_effort_no_raise(self) -> None:
        """Unexpected exception from requests → no raise, best-effort failed row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.tavily_push.requests.post",
                side_effect=RuntimeError("boom"),
            ):
                svc.push_valid_keys("tavily", "run-test-018")  # must not raise

            row = _fetch_log(db_path, "run-test-018")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_uses_env_defaults_when_not_injected(self) -> None:
        """Constructor falls back to TAVILY_PROXY_* env vars when not injected."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["tvly-aaaa"])

            env = {
                "TAVILY_PROXY_BASE_URL": "http://env-host:48080",
                "TAVILY_PROXY_AUTH_KEY": "env-master-key",
            }
            with patch.dict(os.environ, env, clear=False):
                svc = TavilyPushService(workspace=workspace, db_path=db_path)

            with patch(
                "web.tavily_push.requests.post", return_value=_batch_resp([("created", None)])
            ) as mock_post:
                svc.push_valid_keys("tavily", "run-test-019")

            call_args = mock_post.call_args
            self.assertEqual(call_args[0][0], "http://env-host:48080/api/keys/batch")
            self.assertEqual(
                call_args[1]["headers"]["Authorization"],
                "Bearer env-master-key",
            )


# ---------------------------------------------------------------------------
# get_tavily_push_service singleton tests
# ---------------------------------------------------------------------------


class TestTavilyPushServiceSingleton(unittest.TestCase):
    """Given the module,
    When get_tavily_push_service is called multiple times,
    Then the same TavilyPushService instance is returned.
    """

    def test_get_tavily_push_service_returns_singleton(self) -> None:
        """Module-level singleton factory returns the same object."""
        from web.tavily_push import get_tavily_push_service

        svc1 = get_tavily_push_service()
        svc2 = get_tavily_push_service()
        self.assertIs(svc1, svc2, "get_tavily_push_service must return the same instance")

    def test_tavily_push_service_has_push_valid_keys_method(self) -> None:
        """The singleton must have push_valid_keys(provider_name, run_id) method."""
        from web.tavily_push import get_tavily_push_service

        svc = get_tavily_push_service()
        self.assertTrue(
            callable(getattr(svc, "push_valid_keys", None)),
            "TavilyPushService must have push_valid_keys method",
        )


if __name__ == "__main__":
    unittest.main()