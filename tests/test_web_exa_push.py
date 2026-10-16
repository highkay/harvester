#!/usr/bin/env python3

"""TDD unit tests for web/exa_push.py — tavily-hikari single-key push service.

Given a temporary SQLite database and workspace with providers/exa/valid-keys.txt,
When ExaPushService.push_valid_keys is called,
Then every deduped key line is POSTed to {base}/api/keys with
``provider: exa`` and ONE push_logs row is written. Because the hikari
single-key endpoint is add-or-undelete and answers 201 for BOTH new and
existing keys, net-new keys MUST come from the provider-scoped total delta
(GET .../api/keys?provider=exa) — never from the 201 count — and a 409
``provider_conflict`` counts as ignored, not failed.

Test style mirrors tests/test_web_brave_push.py: unittest + unittest.mock,
temp SQLite via web.db.init_db, temp workspace, and
patch("web.exa_push.requests.post").
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from web.exa_push import ExaPushService
from web.db import init_db as _init_db_async


def _run_async(coro):
    """Helper to run async from sync unittest."""
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_KEY_A = "EXAaaaaaaaaaaaaaaaaaaaaaaaa"
_KEY_B = "EXAbbbbbbbbbbbbbbbbbbbbbbbb"
_KEY_C = "EXAcccccccccccccccccccccccc"


def _temp_db_path() -> str:
    """Build a temporary SQLite database path."""
    tmpdir = tempfile.mkdtemp()
    return os.path.join(tmpdir, "test_exa_push.db")


def _init_schema(db_path: str) -> None:
    """Initialize the full schema (incl. push_logs) in a temp database."""
    _run_async(_init_db_async(db_path))


def _write_valid_keys(workspace: str, keys: list[str]) -> None:
    """Write providers/exa/valid-keys.txt in the workspace."""
    provider_dir = Path(workspace) / "providers" / "exa"
    provider_dir.mkdir(parents=True, exist_ok=True)
    vk_path = provider_dir / "valid-keys.txt"
    vk_path.write_text("\n".join(keys), encoding="utf-8")


def _resp(
    status_code: int,
    error: str | None = None,
    detail: str | None = None,
    *,
    raw_text: str = "",
    json_raises: bool = False,
) -> MagicMock:
    """Build a MagicMock requests.Response for a non-201 status."""
    resp = MagicMock()
    resp.status_code = status_code
    body: dict[str, str] = {}
    if error is not None:
        body["error"] = error
    if detail is not None:
        body["detail"] = detail
    if json_raises:
        resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    else:
        resp.json.return_value = body
    resp.text = raw_text
    return resp


def _post_201() -> MagicMock:
    """201 CreateKeyResponse for an accepted single-key add (new OR existing)."""
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"id": "k_01"}
    resp.text = ""
    return resp


def _get_resp(total: object = 0, status_code: int = 200, *, json_raises: bool = False) -> MagicMock:
    """Mock for the provider-scoped total probe (GET .../api/keys)."""
    resp = MagicMock()
    resp.status_code = status_code
    if json_raises:
        resp.json.side_effect = ValueError("no json here")
    else:
        resp.json.return_value = {"total": total}
    resp.text = ""
    return resp


def _totals(before: int, after: int):
    """Patch the two provider-total probes performed around the push loop."""
    return patch(
        "web.exa_push.requests.get",
        side_effect=[_get_resp(before), _get_resp(after)],
    )


def _make_service(
    db_path: str,
    workspace: str,
    *,
    base_url: str = "http://127.0.0.1:19999",
    auth_key: str = "th-test-master-key",
) -> ExaPushService:
    return ExaPushService(
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
# ExaPushService tests
# ---------------------------------------------------------------------------


class TestExaPushServiceBasic(unittest.TestCase):
    """Given a temp DB + workspace,
    When push_valid_keys is called,
    Then keys are POSTed per the response taxonomy and one push_logs row is written.
    """

    def test_push_existing_keys_delta_zero_all_ignored(self) -> None:
        """All keys accepted (201) with an unchanged total → all duplicates.

        Pins the misleading-success class: three 201 responses must NOT report
        added=3 — net-new comes from the provider-scoped total delta.
        """
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B, _KEY_C])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-001")

            self.assertEqual(mock_post.call_count, 3)

            row = _fetch_log(db_path, "run-test-001")
            self.assertIsNotNone(row, "push_logs entry should exist")
            self.assertEqual(row["provider_name"], "exa")
            self.assertEqual(row["gpt_load_config_id"], 0)
            self.assertEqual(row["group_id"], 0)
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 3)
            self.assertEqual(row["status"], "success")
            self.assertIsNone(row["error_message"])

    def test_push_delta_counts_net_new_added(self) -> None:
        """A total increase of two across three accepted keys → added=2, ignored=1."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B, _KEY_C])

            svc = _make_service(db_path, workspace)

            with _totals(413, 415), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ):
                svc.push_valid_keys("exa", "run-test-002")

            row = _fetch_log(db_path, "run-test-002")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 1)
            self.assertEqual(row["status"], "success")

    def test_push_added_clamped_to_accepted_count(self) -> None:
        """A delta larger than the accepted count is clamped to accepted."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B])

            svc = _make_service(db_path, workspace)

            # +5 total while only 2 keys were POSTed → someone else inserted
            with _totals(413, 418), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ):
                svc.push_valid_keys("exa", "run-test-003")

            row = _fetch_log(db_path, "run-test-003")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 0)
            self.assertEqual(row["status"], "success")

    def test_push_negative_delta_clamped_to_zero(self) -> None:
        """A shrinking total (concurrent delete) → added=0, never negative."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B])

            svc = _make_service(db_path, workspace)

            with _totals(413, 411), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ):
                svc.push_valid_keys("exa", "run-test-004")

            row = _fetch_log(db_path, "run-test-004")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 2)
            self.assertEqual(row["status"], "success")

    def test_push_body_and_headers_shape(self) -> None:
        """POST body is {"api_key", "provider": "exa"} with Bearer auth."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 414), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-005")

            call_args = mock_post.call_args
            body = call_args[1]["json"]
            self.assertEqual(
                body,
                {"api_key": _KEY_A, "provider": "exa"},
                "body must be exactly {'api_key', 'provider': 'exa'}",
            )
            headers = call_args[1]["headers"]
            self.assertEqual(headers["Authorization"], "Bearer th-test-master-key")
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(call_args[1]["timeout"], 30)

    def test_push_url_exact_after_rstrip(self) -> None:
        """Trailing slash on base_url is stripped → URL is exactly {base}/api/keys."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace, base_url="http://127.0.0.1:19999/")

            with _totals(413, 414), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-006")

            self.assertEqual(
                mock_post.call_args[0][0],
                "http://127.0.0.1:19999/api/keys",
                "URL must be exactly {base}/api/keys with no trailing slash",
            )

    def test_push_reads_provider_scoped_total_twice(self) -> None:
        """The total probe is provider-scoped, per_page=1, and runs before+after."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 414), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ), patch(
                "web.exa_push.requests.get", side_effect=[_get_resp(413), _get_resp(414)]
            ) as mock_get:
                svc.push_valid_keys("exa", "run-test-007")

            self.assertEqual(mock_get.call_count, 2, "total probed before and after")
            for call in mock_get.call_args_list:
                self.assertEqual(call[0][0], "http://127.0.0.1:19999/api/keys")
                self.assertEqual(
                    call[1]["params"], {"provider": "exa", "per_page": 1}
                )
                self.assertEqual(call[1]["timeout"], 30)
                self.assertEqual(
                    call[1]["headers"]["Authorization"], "Bearer th-test-master-key"
                )

    def test_push_auth_header_omitted_when_auth_key_empty(self) -> None:
        """Empty auth key still pushes (DEV_OPEN_ADMIN) but sends no Bearer."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace, auth_key="")

            with patch(
                "web.exa_push.requests.get",
                side_effect=[_get_resp(413), _get_resp(414)],
            ) as mock_get, patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-008")

            self.assertEqual(mock_post.call_count, 1, "push must NOT be gated on auth")
            self.assertNotIn("Authorization", mock_post.call_args[1]["headers"])
            for call in mock_get.call_args_list:
                self.assertNotIn("Authorization", call[1]["headers"])

            row = _fetch_log(db_path, "run-test-008")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["status"], "success")

    def test_push_403_unauthorized_fail_fast_stops_remaining_keys(self) -> None:
        """403 (not admin) → stop iterating immediately; status failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B, _KEY_C])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post", return_value=_resp(403)
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-009")

            self.assertEqual(
                mock_post.call_count, 1, "403 must fail-fast: only first key POSTed"
            )

            row = _fetch_log(db_path, "run-test-009")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error_message"], "unauthorized (admin auth rejected)")

    def test_push_401_unauthorized_fail_fast(self) -> None:
        """401 → fail-fast with the same unauthorized message."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post", return_value=_resp(401)
            ):
                svc.push_valid_keys("exa", "run-test-010")

            row = _fetch_log(db_path, "run-test-010")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error_message"], "unauthorized (admin auth rejected)")

    def test_push_429_retries_4_attempts_with_backoff(self) -> None:
        """429 → 4 total attempts with 1s/3s/9s backoff, exhausted → failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post", return_value=_resp(429)
            ) as mock_post, patch("web.exa_push.time.sleep") as mock_sleep:
                svc.push_valid_keys("exa", "run-test-011")

            self.assertEqual(mock_post.call_count, 4, "1 initial + 3 retries")
            self.assertEqual(
                [c.args[0] for c in mock_sleep.call_args_list],
                [1.0, 3.0, 9.0],
                "backoff must be 1s, 3s, 9s",
            )

            row = _fetch_log(db_path, "run-test-011")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["error_message"])

    def test_push_500_then_201_recovers(self) -> None:
        """Transient 500 then 201 → the retry succeeds within the same push."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 414), patch(
                "web.exa_push.requests.post",
                side_effect=[_resp(500), _post_201()],
            ) as mock_post, patch("web.exa_push.time.sleep"):
                svc.push_valid_keys("exa", "run-test-012")

            self.assertEqual(mock_post.call_count, 2)

            row = _fetch_log(db_path, "run-test-012")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 1)

    def test_push_409_provider_conflict_counts_as_ignored(self) -> None:
        """409 provider_conflict → ignored (key not lost), NOT a failure."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post",
                return_value=_resp(
                    409,
                    "provider_conflict",
                    "api_key is already registered for provider 'tavily'",
                ),
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-013")

            self.assertEqual(mock_post.call_count, 1, "409 must not be retried")

            row = _fetch_log(db_path, "run-test-013")
            self.assertIsNotNone(row)
            self.assertEqual(row["keys_count"], 1)
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 1)
            self.assertEqual(row["status"], "success")
            self.assertIsNone(row["error_message"])

    def test_push_409_mixed_with_duplicate_201(self) -> None:
        """201 (existing) + 409 → both land in ignored; added stays 0."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B])

            svc = _make_service(db_path, workspace)

            responses = [_post_201(), _resp(409, "provider_conflict", "registered")]
            with _totals(413, 413), patch(
                "web.exa_push.requests.post", side_effect=responses
            ):
                svc.push_valid_keys("exa", "run-test-014")

            row = _fetch_log(db_path, "run-test-014")
            self.assertIsNotNone(row)
            self.assertEqual(row["added_count"], 0)
            self.assertEqual(row["ignored_count"], 2)
            self.assertEqual(row["status"], "success")

    def test_push_409_plus_hard_failure_is_partial(self) -> None:
        """409 (ignored) + dead key (failed) → partial, not failed."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B])

            svc = _make_service(db_path, workspace)

            responses = [
                _resp(409, "provider_conflict", "registered"),
                _resp(500),
                _resp(500),
                _resp(500),
                _resp(500),
            ]
            with _totals(413, 413), patch(
                "web.exa_push.requests.post", side_effect=responses
            ), patch("web.exa_push.time.sleep"):
                svc.push_valid_keys("exa", "run-test-015")

            row = _fetch_log(db_path, "run-test-015")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["ignored_count"], 1)
            self.assertEqual(row["added_count"], 0)

    def test_push_partial_when_one_key_exhausts_retries(self) -> None:
        """2 accepted + 1 dead (4 attempts) → partial; delta measures the net-new."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B, _KEY_C])

            svc = _make_service(db_path, workspace)

            responses = [_post_201(), _post_201(), _resp(500), _resp(500), _resp(500), _resp(500)]
            with _totals(413, 414), patch(
                "web.exa_push.requests.post", side_effect=responses
            ) as mock_post, patch("web.exa_push.time.sleep"):
                svc.push_valid_keys("exa", "run-test-016")

            self.assertEqual(mock_post.call_count, 6, "2 accepted + 4 attempts")

            row = _fetch_log(db_path, "run-test-016")
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
            _write_valid_keys(workspace, [_KEY_A, _KEY_B])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.exa_push.requests.get",
                side_effect=[_get_resp(0, 500), _get_resp(0, 500)],
            ), patch("web.exa_push.requests.post", return_value=_post_201()):
                svc.push_valid_keys("exa", "run-test-017")

            row = _fetch_log(db_path, "run-test-017")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 2)
            self.assertEqual(row["ignored_count"], 0)
            self.assertIn("pool count unavailable", row["error_message"])

    def test_push_total_non_json_body_falls_back(self) -> None:
        """A non-JSON total body (proxy interstitial) → fallback, never a raise."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.exa_push.requests.get",
                side_effect=[
                    _get_resp(json_raises=True),
                    _get_resp(json_raises=True),
                ],
            ), patch("web.exa_push.requests.post", return_value=_post_201()):
                svc.push_valid_keys("exa", "run-test-018")

            row = _fetch_log(db_path, "run-test-018")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["added_count"], 1)
            self.assertIn("pool count unavailable", row["error_message"])

    def test_push_total_malformed_payload_falls_back(self) -> None:
        """Missing / non-int / bool ``total`` → treated as unreadable."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)

            for idx, payload in enumerate(({}, {"total": "413"}, {"total": True})):
                run_id = f"run-test-019-{idx}"
                _write_valid_keys(workspace, [_KEY_A])
                svc = _make_service(db_path, workspace)

                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = payload
                resp.text = ""
                with patch(
                    "web.exa_push.requests.get", side_effect=[resp, resp]
                ), patch("web.exa_push.requests.post", return_value=_post_201()):
                    svc.push_valid_keys("exa", run_id)

                row = _fetch_log(db_path, run_id)
                self.assertIsNotNone(row, f"payload {payload} must still log")
                self.assertEqual(row["added_count"], 1, f"payload {payload}")
                self.assertIn("pool count unavailable", row["error_message"])

    def test_push_non_json_error_body_uses_text_snippet(self) -> None:
        """A 400 with an HTML body → failed, error carries the text snippet."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post",
                return_value=_resp(
                    400, json_raises=True, raw_text="<html>Bad Request</html>"
                ),
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-020")

            self.assertEqual(mock_post.call_count, 1, "4xx must not be retried")

            row = _fetch_log(db_path, "run-test-020")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIn("HTTP 400", row["error_message"])
            self.assertIn("<html>Bad Request</html>", row["error_message"])

    def test_push_422_bad_provider_is_non_retryable_failure(self) -> None:
        """422 with a JSON error → failed carrying code + detail, single attempt."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post",
                return_value=_resp(422, "invalid_provider", "unknown provider"),
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-021")

            self.assertEqual(mock_post.call_count, 1)

            row = _fetch_log(db_path, "run-test-021")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIn("invalid_provider", row["error_message"])
            self.assertIn("unknown provider", row["error_message"])

    def test_push_connection_error_retries_then_failed(self) -> None:
        """ConnectionError → 4 attempts with backoff, then a failed row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post",
                side_effect=requests.ConnectionError("connection refused"),
            ) as mock_post, patch("web.exa_push.time.sleep") as mock_sleep:
                svc.push_valid_keys("exa", "run-test-022")

            self.assertEqual(mock_post.call_count, 4)
            self.assertEqual(len(mock_sleep.call_args_list), 3)

            row = _fetch_log(db_path, "run-test-022")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIn("ConnectionError", row["error_message"])

    def test_push_skips_non_exa_provider(self) -> None:
        """provider_name != exa → no HTTP at all and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with patch("web.exa_push.requests.post") as mock_post, patch(
                "web.exa_push.requests.get"
            ) as mock_get:
                svc.push_valid_keys("deepseek", "run-test-023")

            mock_post.assert_not_called()
            mock_get.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-023"), 0)

    def test_push_skips_when_base_url_empty(self) -> None:
        """base_url empty → no HTTP at all and no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace, base_url="")

            with patch("web.exa_push.requests.post") as mock_post, patch(
                "web.exa_push.requests.get"
            ) as mock_get:
                svc.push_valid_keys("exa", "run-test-024")

            mock_post.assert_not_called()
            mock_get.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-024"), 0)

    def test_push_missing_valid_keys_file_writes_no_row(self) -> None:
        """No valid-keys.txt → silent skip, no HTTP, no push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)

            svc = _make_service(db_path, workspace)

            with patch("web.exa_push.requests.post") as mock_post, patch(
                "web.exa_push.requests.get"
            ) as mock_get:
                svc.push_valid_keys("exa", "run-test-025")

            mock_post.assert_not_called()
            mock_get.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-025"), 0)

    def test_push_dedups_repeated_lines(self) -> None:
        """Duplicate lines are pushed once; blank lines are dropped."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, "", f"  {_KEY_A}  ", _KEY_B, "\n"])

            svc = _make_service(db_path, workspace)

            with _totals(413, 415), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-026")

            self.assertEqual(mock_post.call_count, 2, "deduped to two keys")
            posted = [c[1]["json"]["api_key"] for c in mock_post.call_args_list]
            self.assertEqual(posted, [_KEY_A, _KEY_B])

            row = _fetch_log(db_path, "run-test-026")
            self.assertEqual(row["keys_count"], 2)
            self.assertEqual(row["added_count"], 2)

    def test_push_has_no_key_shape_prefilter(self) -> None:
        """Any non-empty line is pushed verbatim (Exa format is unmeasured)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            keys = [
                "EXA" + "x" * 5,
                "short",
                "Z" * 64,
                "01234567-89ab-cdef-0123-456789abcdef",
            ]
            _write_valid_keys(workspace, keys)

            svc = _make_service(db_path, workspace)

            with _totals(413, 417), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-027")

            self.assertEqual(mock_post.call_count, len(keys))
            posted = [c[1]["json"]["api_key"] for c in mock_post.call_args_list]
            self.assertEqual(posted, keys, "keys must be pushed verbatim")

            row = _fetch_log(db_path, "run-test-027")
            self.assertEqual(row["keys_count"], len(keys))
            self.assertEqual(row["added_count"], len(keys))
            self.assertEqual(row["ignored_count"], 0)

    def test_push_same_run_id_only_once(self) -> None:
        """Second call with the same run_id does not re-POST keys (resumable flow)."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-028")
                svc.push_valid_keys("exa", "run-test-028")
                svc.push_valid_keys("exa", "run-test-028")

            self.assertEqual(mock_post.call_count, 2, "repeat calls must be a no-op")
            self.assertEqual(_count_logs(db_path, "run-test-028"), 1)

    def test_push_different_run_id_pushes_again(self) -> None:
        """The idempotency guard is per run_id, not per service instance."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with patch(
                "web.exa_push.requests.get",
                side_effect=[_get_resp(413), _get_resp(414), _get_resp(414), _get_resp(414)],
            ), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-029")
                svc.push_valid_keys("exa", "run-test-030")

            self.assertEqual(mock_post.call_count, 2)
            self.assertEqual(_count_logs(db_path, "run-test-029"), 1)
            self.assertEqual(_count_logs(db_path, "run-test-030"), 1)

    def test_push_unexpected_exception_best_effort_no_raise(self) -> None:
        """Unexpected exception from requests → no raise, best-effort failed row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(db_path, workspace)

            with _totals(413, 413), patch(
                "web.exa_push.requests.post",
                side_effect=RuntimeError("boom"),
            ):
                svc.push_valid_keys("exa", "run-test-031")  # must not raise

            row = _fetch_log(db_path, "run-test-031")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIn("boom", row["error_message"])

    def test_push_db_error_does_not_raise(self) -> None:
        """An unusable DB path → the method still returns without raising."""
        with tempfile.TemporaryDirectory() as workspace:
            _write_valid_keys(workspace, [_KEY_A])

            svc = _make_service(
                os.path.join(workspace, "missing-dir", "nope.db"), workspace
            )

            with _totals(413, 414), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ):
                svc.push_valid_keys("exa", "run-test-032")  # must not raise

    def test_push_uses_env_defaults_when_not_injected(self) -> None:
        """Constructor falls back to EXA_PROXY_* env vars when not injected."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            env = {
                "EXA_PROXY_BASE_URL": "http://env-host:8787",
                "EXA_PROXY_AUTH_KEY": "th-env-master-key",
            }
            with patch.dict(os.environ, env, clear=False):
                svc = ExaPushService(workspace=workspace, db_path=db_path)

            with _totals(413, 414), patch(
                "web.exa_push.requests.post", return_value=_post_201()
            ) as mock_post:
                svc.push_valid_keys("exa", "run-test-033")

            call_args = mock_post.call_args
            self.assertEqual(call_args[0][0], "http://env-host:8787/api/keys")
            self.assertEqual(
                call_args[1]["headers"]["Authorization"],
                "Bearer th-env-master-key",
            )

    def test_push_env_unset_is_silent_noop(self) -> None:
        """With EXA_PROXY_BASE_URL unset, the default-constructed service no-ops."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A])

            env = {k: v for k, v in os.environ.items() if not k.startswith("EXA_PROXY")}
            with patch.dict(os.environ, env, clear=True):
                svc = ExaPushService(workspace=workspace, db_path=db_path)

            with patch("web.exa_push.requests.post") as mock_post, patch(
                "web.exa_push.requests.get"
            ) as mock_get:
                svc.push_valid_keys("exa", "run-test-034")

            mock_post.assert_not_called()
            mock_get.assert_not_called()
            self.assertEqual(_count_logs(db_path, "run-test-034"), 0)

    def test_push_writes_exactly_one_row_per_run(self) -> None:
        """A mixed-outcome run still writes exactly ONE push_logs row."""
        db_path = _temp_db_path()
        with tempfile.TemporaryDirectory() as workspace:
            _init_schema(db_path)
            _write_valid_keys(workspace, [_KEY_A, _KEY_B, _KEY_C])

            svc = _make_service(db_path, workspace)

            responses = [
                _post_201(),
                _resp(409, "provider_conflict", "registered"),
                _resp(400, "bad_request", "api_key empty"),
            ]
            with _totals(413, 414), patch(
                "web.exa_push.requests.post", side_effect=responses
            ):
                svc.push_valid_keys("exa", "run-test-035")

            self.assertEqual(_count_logs(db_path, "run-test-035"), 1)
            row = _fetch_log(db_path, "run-test-035")
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["keys_count"], 3)
            self.assertEqual(row["added_count"], 1)
            self.assertEqual(row["ignored_count"], 1)
            self.assertIn("bad_request", row["error_message"])


# ---------------------------------------------------------------------------
# get_exa_push_service singleton tests
# ---------------------------------------------------------------------------


class TestExaPushServiceSingleton(unittest.TestCase):
    """Given the module,
    When get_exa_push_service is called multiple times,
    Then the same ExaPushService instance is returned.
    """

    def test_get_exa_push_service_returns_singleton(self) -> None:
        """Module-level singleton factory returns the same object."""
        from web.exa_push import get_exa_push_service

        svc1 = get_exa_push_service()
        svc2 = get_exa_push_service()
        self.assertIs(
            svc1, svc2, "get_exa_push_service must return the same instance"
        )

    def test_exa_push_service_has_push_valid_keys_method(self) -> None:
        """The singleton must have push_valid_keys(provider_name, run_id) method."""
        from web.exa_push import get_exa_push_service

        svc = get_exa_push_service()
        self.assertTrue(
            callable(getattr(svc, "push_valid_keys", None)),
            "ExaPushService must have push_valid_keys method",
        )


if __name__ == "__main__":
    unittest.main()
