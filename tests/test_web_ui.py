#!/usr/bin/env python3

"""TDD unit tests for web/router_ui.py — Jinja2 管理界面页面。

Given a seeded temp database and WEB_AUTH_KEY auth enabled,
When the UI pages are requested (with/without Bearer token),
Then the correct status code and Chinese page content are returned.
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

_TEST_AUTH_KEY = "test-ui-key"


def _temp_db_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "test_ui.db")


def _seed_db(db_path: str) -> None:
    """Initialize schema and insert sample rows for all UI tables."""
    _run_async(_init_db_async(db_path))

    conn = sqlite3.connect(db_path)
    try:
        # github_tokens: 2 rows (encrypted placeholder, only hash shown in UI)
        conn.execute(
            "INSERT INTO github_tokens "
            "(token_type, token_encrypted, token_hash, label, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("api", "enc-secret-A", "a1b2c3d4e5f60718", "主账号 Token", 1),
        )
        conn.execute(
            "INSERT INTO github_tokens "
            "(token_type, token_encrypted, token_hash, label, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("session", "enc-secret-B", "f8e7d6c5b4a39201", "备用会话", 0),
        )

        # run_records: 2 rows
        conn.execute(
            "INSERT INTO run_records "
            "(id, provider_name, config_file, status, started_at, finished_at, "
            "duration_seconds, valid_keys_found, total_keys_checked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-aaaa-0001", "deepseek", "config-deepseek.yaml", "completed",
             "2026-08-01 10:00:00", "2026-08-01 10:05:00", 300.0, 12, 50),
        )
        conn.execute(
            "INSERT INTO run_records "
            "(id, provider_name, config_file, status, started_at, finished_at, "
            "duration_seconds, valid_keys_found, total_keys_checked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-bbbb-0002", "kimi", "config-kimi.yaml", "failed",
             "2026-08-02 11:00:00", "2026-08-02 11:01:00", 60.0, 0, 10),
        )

        # push_logs: 2 rows
        conn.execute(
            "INSERT INTO push_logs "
            "(run_id, provider_name, gpt_load_config_id, group_id, keys_count, "
            "added_count, ignored_count, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-aaaa-0001", "deepseek", 1, 2, 12, 10, 2, "success"),
        )
        conn.execute(
            "INSERT INTO push_logs "
            "(run_id, provider_name, gpt_load_config_id, group_id, keys_count, "
            "added_count, ignored_count, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-bbbb-0002", "kimi", 1, 3, 10, 0, 10, "failed"),
        )

        # schedule_config: 2 rows
        conn.execute(
            "INSERT INTO schedule_config "
            "(provider_name, cron_expression, enabled, config_file) "
            "VALUES (?, ?, ?, ?)",
            ("deepseek", "0 3 * * *", 1, "config-deepseek.yaml"),
        )
        conn.execute(
            "INSERT INTO schedule_config "
            "(provider_name, cron_expression, enabled, config_file) "
            "VALUES (?, ?, ?, ?)",
            ("kimi", "30 4 * * *", 0, "config-kimi.yaml"),
        )
        conn.commit()
    finally:
        conn.close()


def _make_client(db_path: str) -> TestClient:
    """Create a TestClient against a fresh app wired to *db_path*.

    No default auth headers are attached — each request passes its own
    Bearer token explicitly (or none, to exercise the 401 path).
    """
    os.environ["HARVESTER_DB"] = db_path
    os.environ["WEB_AUTH_KEY"] = _TEST_AUTH_KEY

    import web.deps

    # Force settings singleton to re-read env vars
    web.deps._settings = None  # type: ignore[attr-defined]

    from web.app import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_AUTH_KEY}"}


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class WebUiPageTestBase(unittest.TestCase):
    """Common setup: seeded temp DB + fresh app per test."""

    def setUp(self) -> None:
        self._db_path = _temp_db_path()
        _seed_db(self._db_path)
        self.client = _make_client(self._db_path)


class TestLoginPage(WebUiPageTestBase):
    """GET /login must be public (no auth required)."""

    def test_login_page_requires_no_auth(self) -> None:
        """GET /login without Bearer returns 200 with login form."""
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("登录", resp.text)
        self.assertIn("auth_key", resp.text)


class TestDashboardPage(WebUiPageTestBase):
    """GET / — dashboard with stats and recent tables."""

    def test_dashboard_with_bearer(self) -> None:
        """GET / with Bearer returns 200 and dashboard content."""
        resp = self.client.get("/", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("仪表盘", resp.text)
        self.assertIn("Token 总数", resp.text)
        self.assertIn("deepseek", resp.text)  # seeded schedule row

    def test_dashboard_requires_auth(self) -> None:
        """GET / without Bearer returns 401."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 401)


class TestTokensPage(WebUiPageTestBase):
    """GET /tokens — tokens listed with masked values only."""

    def test_tokens_page_with_bearer(self) -> None:
        """GET /tokens returns 200 with masked token table."""
        resp = self.client.get("/tokens", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Token 管理", resp.text)
        self.assertIn("脱敏", resp.text)
        self.assertIn("启用状态", resp.text)
        self.assertIn("主账号 Token", resp.text)

    def test_tokens_never_leak_encrypted_value(self) -> None:
        """Encrypted token placeholder must never appear in the page."""
        resp = self.client.get("/tokens", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("enc-secret-A", resp.text)
        self.assertNotIn("enc-secret-B", resp.text)

    def test_tokens_page_requires_auth(self) -> None:
        """GET /tokens without Bearer returns 401 (auth enforcement)."""
        resp = self.client.get("/tokens")
        self.assertEqual(resp.status_code, 401)


class TestSchedulePage(WebUiPageTestBase):
    """GET /schedule — schedule config list."""

    def test_schedule_page_with_bearer(self) -> None:
        """GET /schedule returns 200 with cron/config columns."""
        resp = self.client.get("/schedule", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("调度管理", resp.text)
        self.assertIn("Cron 表达式", resp.text)
        self.assertIn("config-deepseek.yaml", resp.text)

    def test_schedule_page_requires_auth(self) -> None:
        """GET /schedule without Bearer returns 401."""
        resp = self.client.get("/schedule")
        self.assertEqual(resp.status_code, 401)


class TestRunsPage(WebUiPageTestBase):
    """GET /runs — run records list."""

    def test_runs_page_with_bearer(self) -> None:
        """GET /runs returns 200 with run table headers."""
        resp = self.client.get("/runs", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("运行历史", resp.text)
        self.assertIn("Provider", resp.text)
        self.assertIn("deepseek", resp.text)
        self.assertIn("run-aaaa", resp.text)  # truncated run id

    def test_runs_page_requires_auth(self) -> None:
        """GET /runs without Bearer returns 401."""
        resp = self.client.get("/runs")
        self.assertEqual(resp.status_code, 401)


class TestRunDetailPage(WebUiPageTestBase):
    """GET /runs/{run_id} — single run record detail."""

    def test_run_detail_with_bearer(self) -> None:
        """GET /runs/run-aaaa-0001 returns 200 with detail fields."""
        resp = self.client.get("/runs/run-aaaa-0001", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("运行详情", resp.text)
        self.assertIn("run-aaaa-0001", resp.text)
        self.assertIn("completed", resp.text)

    def test_run_detail_missing_returns_404(self) -> None:
        """GET /runs/not-exist returns 404."""
        resp = self.client.get("/runs/not-exist", headers=_auth_headers())
        self.assertEqual(resp.status_code, 404)

    def test_run_detail_requires_auth(self) -> None:
        """GET /runs/{id} without Bearer returns 401."""
        resp = self.client.get("/runs/run-aaaa-0001")
        self.assertEqual(resp.status_code, 401)


class TestPushLogsPage(WebUiPageTestBase):
    """GET /push-logs — push audit records."""

    def test_push_logs_page_with_bearer(self) -> None:
        """GET /push-logs returns 200 with push table headers."""
        resp = self.client.get("/push-logs", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("推送日志", resp.text)
        self.assertIn("分组 ID", resp.text)
        self.assertIn("success", resp.text)

    def test_push_logs_page_requires_auth(self) -> None:
        """GET /push-logs without Bearer returns 401."""
        resp = self.client.get("/push-logs")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()