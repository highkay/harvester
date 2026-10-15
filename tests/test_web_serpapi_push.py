#!/usr/bin/env python3

"""TDD unit tests for web/serpapi_push.py — tavily-hikari single-key push service.

Given a temporary SQLite database and workspace with providers/serpapi/valid-keys.txt,
When SerpapiPushService.push_valid_keys is called,
Then each 20-64 hex key is POSTed to {base}/api/keys with ``provider: serpapi``
and ONE push_logs row is written. Because the hikari single-key endpoint is
add-or-undelete and answers 201 for BOTH new and existing keys, net-new keys
are measured via the provider-scoped total delta (GET .../api/keys?provider=serpapi).

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


def _resp(status_code: int, error: str | None = None, detail: str | None = None) -> MagicMock:
    """Build a MagicMock requests.Response for a non-201 status."""
    resp = MagicMock()
    resp.status_code = status_code
    body: dict[str, str] = {}
    if error is not None:
        body["error"] = error
    if detail is not None:
        body["detail"] = detail
    resp.json.return_value = body
    resp.text = ""
    return resp


def _post_201() -> MagicMock:
    """201 CreateKeyResponse for an accepted single-key add (new OR existing)."""
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"id": "k_01"}
    resp.text = ""
    return resp


def _get_resp(total: int, status_code: int = 200) -> MagicMock:
    """Mock for the provider-scoped total probe (GET .../api/keys)."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"total": total}
    resp.text = ""
    return resp


def _totals(before: int, after: int):
    """Patch the two provider-total probes performed around the push loop."""
    return patch(
        "web.serpapi_push.requests.get",
        side_effect=[_get_resp(before), _get_resp(after)],
    )


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

    def test_push_existing_keys_delta_zero_all_ignored(self) -> None:
        """All keys accepted (201) with an unchanged total → all duplicates."""
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

            with _totals(413, 413), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-001")

            self.assertEqual(mock_post.call_count, 3)

            row = _fetch_log(db_path, "run-test-001")
            self.assertIsNotNone(row, "push_logs entry should exist")
            self.assertEqual(row["provider_name"], "serpapi")
            self.assertEqual(row["gpt_load_config_id"], 0)
            self.assertEqual(row["group_id"], 0)
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 3)
            self.assertEqual(row["status"], "success")

    def test_push_delta_counts_net_new_added(self) -> None:
        """A total increase of two across three accepted keys → added=2, ignored=1."""
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

            with _totals(413, 415), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ):
                svc.push_valid_keys("serpapi", "run-test-002")

            row = _fetch_log(db_path, "run-test-002")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 1)
            self.assertEqual(row["status"], "success")

    def test_push_body_and_headers_shape(self) -> None:
        """POST body is {"api_key", "provider": "serpapi"} with Bearer auth."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 414), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-003")

            call_args = mock_post.call_args
            body = call_args[1]["json"]
            self.assertEqual(
                body,
                {"api_key": "0123456789abcdef0123456789abcdef", "provider": "serpapi"},
                "body must be exactly {'api_key', 'provider': 'serpapi'}",
            )
            headers = call_args[1]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer test-master-key")
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(call_args[1]["timeout"], 30)

    def test_push_url_exact_after_rstrip(self) -> None:
        """Trailing slash on base_url is stripped → URL is exactly {base}/api/keys."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(
                db_path, workspace, base_url="http://127.0.0.1:19999/"
            )

            with _totals(413, 414), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-004")

            self.assertEqual(
                mock_post.call_args[0][0],
                "http://127.0.0.1:19999/api/keys",
                "URL must be exactly {base}/api/keys with no trailing slash",
            )

    def test_push_403_unauthorized_fail_fast_stops_remaining_keys(self) -> None:
        """403 (not admin) → stop iterating immediately; status failed."""
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

            with _totals(413, 413), patch(
                "web.serpapi_push.requests.post", return_value=_resp(403)
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-005")

            self.assertEqual(
                mock_post.call_count, 1, "403 must fail-fast: only first key POSTed"
            )

            row = _fetch_log(db_path, "run-test-005")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(
                row["error_message"], "unauthorized (admin auth rejected)"
            )

    def test_push_401_unauthorized_fail_fast(self) -> None:
        """401 → fail-fast with the same unauthorized message."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.serpapi_push.requests.post", return_value=_resp(401)
            ):
                svc.push_valid_keys("serpapi", "run-test-006")

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
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.serpapi_push.requests.post", return_value=_resp(429)
            ) as mock_post, patch("web.serpapi_push.time.sleep") as mock_sleep:
                svc.push_valid_keys("serpapi", "run-test-007")

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

    def test_push_500_then_201_recovers(self) -> None:
        """Transient 500 then 201 → the retry succeeds within the same push."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 414), patch(
                "web.serpapi_push.requests.post",
                side_effect=[_resp(500), _post_201()],
            ) as mock_post, patch("web.serpapi_push.time.sleep"):
                svc.push_valid_keys("serpapi", "run-test-008")

            self.assertEqual(mock_post.call_count, 2)

            row = _fetch_log(db_path, "run-test-008")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 1)

    def test_push_409_provider_conflict_failed(self) -> None:
        """409 provider_conflict → failure carrying code + detail."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.serpapi_push.requests.post",
                return_value=_resp(
                    409,
                    "provider_conflict",
                    "api_key is already registered for provider 'exa'",
                ),
            ):
                svc.push_valid_keys("serpapi", "run-test-009")

            row = _fetch_log(db_path, "run-test-009")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIn("provider_conflict", row["error_message"])
            self.assertIn("exa", row["error_message"])

    def test_push_partial_when_one_key_exhausts_retries(self) -> None:
        """2 accepted + 1 dead (4 attempts) → partial; delta measures the net-new."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(
                workspace,
                [
                    "0123456789abcdef0123456789abcdef",
                    "fedcba9876543210fedcba9876543210",
                    "99999999999999999999999999999999",
                ],
            )

            svc = _make_service(db_path, workspace)

            responses = [_post_201(), _post_201(), _resp(500), _resp(500), _resp(500), _resp(500)]
            with _totals(413, 414), patch(
                "web.serpapi_push.requests.post", side_effect=responses
            ) as mock_post, patch("web.serpapi_push.time.sleep"):
                svc.push_valid_keys("serpapi", "run-test-010")

            self.assertEqual(mock_post.call_count, 6, "2 accepted + 4 attempts")

            row = _fetch_log(db_path, "run-test-010")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 1)
            self.assertIsNotNone(row["error_message"])

    def test_push_totals_unavailable_falls_back_to_accepted(self) -> None:
        """Unreadable totals → added counts accepted responses, with a note."""
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
                "web.serpapi_push.requests.get",
                side_effect=[_get_resp(0, 500), _get_resp(0, 500)],
            ), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ):
                svc.push_valid_keys("serpapi", "run-test-011")

            row = _fetch_log(db_path, "run-test-011")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 0)
            self.assertIn("pool count unavailable", row["error_message"])

    def test_push_skips_non_serpapi_provider(self) -> None:
        """provider_name != serpapi → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with patch("web.serpapi_push.requests.post") as mock_post:
                svc.push_valid_keys("deepseek", "run-test-012")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-012"), 0)

    def test_push_skips_when_base_url_empty(self) -> None:
        """base_url empty → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace, base_url="")

            with patch("web.serpapi_push.requests.post") as mock_post:
                svc.push_valid_keys("serpapi", "run-test-013")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-013"), 0)

    def test_push_skips_when_auth_key_empty(self) -> None:
        """auth_key empty → no POST and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace, auth_key="")

            with patch("web.serpapi_push.requests.post") as mock_post:
                svc.push_valid_keys("serpapi", "run-test-014")

            mock_post.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-014"), 0)

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

            with _totals(413, 413), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-015")
                svc.push_valid_keys("serpapi", "run-test-015")

            self.assertEqual(
                mock_post.call_count, 2, "second call must be a no-op"
            )
            self.assertEqual(_count_logs(db_path, "run-test-015"), 1)

    def test_push_unexpected_exception_best_effort_no_raise(self) -> None:
        """Unexpected exception from requests → no raise, best-effort failed row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789abcdef0123456789abcdef"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.serpapi_push.requests.post",
                side_effect=RuntimeError("boom"),
            ):
                svc.push_valid_keys("serpapi", "run-test-016")  # must not raise

            row = _fetch_log(db_path, "run-test-016")
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

            with _totals(413, 414), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-017")

            call_args = mock_post.call_args
            self.assertEqual(call_args[0][0], "http://env-host:48080/api/keys")
            self.assertEqual(
                call_args[1]["headers"]["Authorization"],
                "Bearer env-master-key",
            )

    def test_prefilter_accepts_uppercase_hex(self) -> None:
        """Uppercase hex keys pass the pre-filter and keep their original case."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["0123456789ABCDEF0123456789ABCDEF"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 414), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-018")

            self.assertEqual(mock_post.call_count, 1)
            body = mock_post.call_args[1]["json"]
            self.assertEqual(body["api_key"], "0123456789ABCDEF0123456789ABCDEF")

    def test_prefilter_accepts_length_range(self) -> None:
        """20-64 char hex keys pass the pre-filter (validation is length-independent)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, ["a" * 20, "b" * 32, "c" * 64])

            svc = _make_service(db_path, workspace)

            with _totals(413, 416), patch(
                "web.serpapi_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("serpapi", "run-test-019")

            self.assertEqual(mock_post.call_count, 3)
            row = _fetch_log(db_path, "run-test-019")
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
                svc.push_valid_keys("serpapi", "run-test-020")

            mock_post.assert_not_called()
            row = _fetch_log(db_path, "run-test-020")
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