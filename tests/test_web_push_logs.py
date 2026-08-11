#!/usr/bin/env python3

"""TDD unit tests for web/router_push_logs.py — push logs query API.

Given seeded push_logs entries,
When GET /api/push-logs is called with filters,
Then correct paginated results are returned.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from web.db import init_db as _init_db_async


def _run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _temp_db_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "test_push_logs.db")


def _setup_app(db_path: str) -> TestClient:
    """Create a FastAPI app with the push_logs router and return a TestClient."""
    import web.deps

    # Set HARVESTER_DB env var so resolve_db_path() returns our temp db
    os.environ["HARVESTER_DB"] = db_path

    # Force re-read of settings after env change
    web.deps._settings = None  # type: ignore[attr-defined]

    from web.app import create_app
    from web.deps import get_settings

    # Patch settings to use temp db
    settings = get_settings()
    settings.db_path = db_path  # type: ignore[attr-defined]

    # Get the auth key to pass Bearer token on requests
    auth_key = settings.web_auth_key

    app = create_app()
    return TestClient(
        app,
        raise_server_exceptions=False,
        headers={"Authorization": f"Bearer {auth_key}"},
    )


def _seed_push_logs(db_path: str) -> None:
    """Initialize DB schema and insert sample push_logs rows."""
    _run_async(_init_db_async(db_path))

    conn = sqlite3.connect(db_path)
    try:
        # Need to seed dependent tables too (gpt_load_config, provider_group_mapping)
        conn.execute(
            "INSERT INTO gpt_load_config (name, base_url, auth_key_encrypted) "
            "VALUES (?, ?, ?)",
            ("test-gptload", "http://127.0.0.1:19999", "enc-a"),
        )

        # Insert push_logs entries
        logs = [
            ("run-001", "deepseek", 1, 2, 50, 45, 5, "success"),
            ("run-002", "kimi", 1, 3, 30, 28, 2, "success"),
            ("run-003", "deepseek", 1, 2, 100, 0, 100, "failed"),
            ("run-004", "qwen-cn", 1, 7, 10, 8, 2, "partial"),
        ]
        for run_id, provider, cfg_id, gid, kc, ac, ic, st in logs:
            conn.execute(
                "INSERT INTO push_logs "
                "(run_id, provider_name, gpt_load_config_id, group_id, "
                "keys_count, added_count, ignored_count, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, provider, cfg_id, gid, kc, ac, ic, st),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestPushLogsListAll(unittest.TestCase):
    """Given seeded push_logs,
    When GET /api/push-logs is called without filters,
    Then all entries are returned (paginated).
    """

    def test_list_all_push_logs(self) -> None:
        """GET /api/push-logs returns all entries."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs")
        self.assertEqual(resp.status_code, 200)

        data = resp.json()
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 4, "All 4 seeded logs should be returned")

    def test_list_push_logs_default_pagination(self) -> None:
        """GET /api/push-logs with default limit returns correct count."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?limit=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertLessEqual(len(data), 2)

    def test_list_push_logs_with_offset(self) -> None:
        """GET /api/push-logs with offset skips entries."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?offset=2")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertLessEqual(len(data), 2)


class TestPushLogsFilterByProvider(unittest.TestCase):
    """Given seeded push_logs,
    When GET /api/push-logs is called with provider filter,
    Then only matching entries are returned.
    """

    def test_filter_by_provider_deepseek(self) -> None:
        """GET /api/push-logs?provider=deepseek returns only deepseek logs."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?provider=deepseek")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 2)
        for item in data:
            self.assertEqual(item["provider_name"], "deepseek")

    def test_filter_by_provider_unknown(self) -> None:
        """GET /api/push-logs?provider=unknown returns empty list."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?provider=unknown")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 0)


class TestPushLogsFilterByStatus(unittest.TestCase):
    """Given seeded push_logs with mixed statuses,
    When GET /api/push-logs is called with status filter,
    Then only matching status entries are returned.
    """

    def test_filter_by_status_success(self) -> None:
        """GET /api/push-logs?status=success returns only success logs."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?status=success")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for item in data:
            self.assertEqual(item["status"], "success")

    def test_filter_by_status_failed(self) -> None:
        """GET /api/push-logs?status=failed returns only failed logs."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?status=failed")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "failed")


class TestPushLogsFilterByDate(unittest.TestCase):
    """Given seeded push_logs,
    When GET /api/push-logs is called with date filter,
    Then only entries within the range are returned.
    """

    def test_filter_by_date_range(self) -> None:
        """GET with date_from/date_to returns filtered results."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        # All seeded logs should be within a wide date range
        resp = client.get(
            "/api/push-logs?date_from=2000-01-01&date_to=2099-12-31"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 4)


class TestPushLogsCombinedFilters(unittest.TestCase):
    """Given seeded push_logs,
    When multiple filters are applied simultaneously,
    Then the intersection is returned.
    """

    def test_provider_and_status_combined(self) -> None:
        """GET with provider=deepseek&status=success returns intersection."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?provider=deepseek&status=success")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        for item in data:
            self.assertEqual(item["provider_name"], "deepseek")
            self.assertEqual(item["status"], "success")


class TestPushLogsResponseShape(unittest.TestCase):
    """Given push_logs entries,
    When the API returns them,
    Then each entry has the expected fields.
    """

    def test_response_has_expected_fields(self) -> None:
        """Each push_log item must have id, run_id, provider_name, etc."""
        db_path = _temp_db_path()
        _seed_push_logs(db_path)
        client = _setup_app(db_path)

        resp = client.get("/api/push-logs?limit=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)

        item = data[0]
        expected_fields = {
            "id",
            "run_id",
            "provider_name",
            "gpt_load_config_id",
            "group_id",
            "keys_count",
            "added_count",
            "ignored_count",
            "status",
            "pushed_at",
        }
        self.assertTrue(
            expected_fields.issubset(set(item.keys())),
            f"Missing fields: {expected_fields - set(item.keys())}",
        )


if __name__ == "__main__":
    unittest.main()
