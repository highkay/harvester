#!/usr/bin/env python3

"""TDD unit tests for web/router_ui.py — Jinja2 管理界面页面。

Given a seeded temp database and WEB_AUTH_KEY auth enabled,
When the UI pages are requested (with/without Bearer token),
Then the correct status code and Chinese page content are returned.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from web.db import init_db as _init_db_async
from web.models import mask_token


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

        # gpt_load_config: 1 row (for push_config tests)
        conn.execute(
            "INSERT INTO gpt_load_config "
            "(name, base_url, auth_key_encrypted, enabled) "
            "VALUES (?, ?, ?, ?)",
            ("测试实例", "http://127.0.0.1:8080", "enc-key-aaa", 1),
        )

        # provider_group_mapping: 1 row
        conn.execute(
            "INSERT INTO provider_group_mapping "
            "(provider_name, gpt_load_config_id, group_id, group_name, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("deepseek", 1, 1, "测试分组", 1),
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


# DDL stays inline in tests until the feature lands in web/db.py `_DDL`.
_RUN_NEW_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS run_new_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    task_name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    token_masked TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(run_id, key_hash)
);
"""


def _seed_run_new_keys(
    db_path: str, rows: list[tuple[str, str, str, str, str, str]]
) -> None:
    """Create the (not-yet-schema'd) run_new_keys table and insert *rows*.

    Rows are (run_id, provider_name, task_name, key_hash, token_masked,
    created_at) tuples. Pass an empty list to seed the empty state.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_RUN_NEW_KEYS_DDL)
        conn.executemany(
            "INSERT INTO run_new_keys "
            "(run_id, provider_name, task_name, key_hash, token_masked, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------
# Test session-cookie auth flow (browser-style login)
# ---------------------------------------------------------------------------


class TestSessionCookieFlow(WebUiPageTestBase):
    """Browser login sets a session cookie that unlocks UI pages."""

    def test_login_sets_session_cookie(self) -> None:
        """POST /api/auth/login sets the harvester_session cookie."""
        resp = self.client.post(
            "/api/auth/login", json={"auth_key": _TEST_AUTH_KEY}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("harvester_session", resp.cookies)

    def test_ui_page_accessible_with_session_cookie(self) -> None:
        """GET / with the session cookie returns 200 (no Bearer needed)."""
        login = self.client.post(
            "/api/auth/login", json={"auth_key": _TEST_AUTH_KEY}
        )
        cookie = login.cookies.get("harvester_session")
        resp = self.client.get("/", cookies={"harvester_session": cookie})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("仪表盘", resp.text)

    def test_ui_page_without_auth_redirects_to_login(self) -> None:
        """GET / without any credential redirects to /login (303)."""
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location"), "/login")

    def test_api_still_requires_bearer_without_cookie(self) -> None:
        """API endpoints do NOT accept the UI redirect — 401 without auth."""
        resp = self.client.get("/api/tokens")
        self.assertEqual(resp.status_code, 401)


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
        """GET / without auth redirects to login (303)."""
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)


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
        """GET /tokens without auth redirects to login (303)."""
        resp = self.client.get("/tokens", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)


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
        """GET /schedule without auth redirects to login (303)."""
        resp = self.client.get("/schedule", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)


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
        """GET /runs without auth redirects to login (303)."""
        resp = self.client.get("/runs", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)


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
        """GET /runs/{id} without auth redirects to login (303)."""
        resp = self.client.get("/runs/run-aaaa-0001", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)


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
        """GET /push-logs without auth redirects to login (303)."""
        resp = self.client.get("/push-logs", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)


class TestTokensPageInteractive(WebUiPageTestBase):
    """GET /tokens — verify new interactive elements (add form, action buttons)."""

    def test_tokens_page_has_add_form(self) -> None:
        """Page must contain the add-token form with expected fields."""
        resp = self.client.get("/tokens", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("add-token-form", resp.text)
        self.assertIn("token_type", resp.text)
        self.assertIn("token_value", resp.text)
        self.assertIn("添加", resp.text)

    def test_tokens_page_has_action_buttons(self) -> None:
        """Each token row must have enable/disable and delete buttons."""
        resp = self.client.get("/tokens", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        # Enabled token (id=1) shows "停用" button
        self.assertIn("toggleToken(1, false)", resp.text)
        # Disabled token (id=2) shows "启用" button
        self.assertIn("toggleToken(2, true)", resp.text)
        # Both rows have delete buttons
        self.assertIn("deleteToken(1)", resp.text)
        self.assertIn("deleteToken(2)", resp.text)

    def test_tokens_page_has_operations_column(self) -> None:
        """Table header must include '操作' column."""
        resp = self.client.get("/tokens", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("操作", resp.text)

    def test_tokens_page_contains_fetch_js(self) -> None:
        """Inline JS must call fetch for POST/PATCH/DELETE."""
        resp = self.client.get("/tokens", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("fetch('/api/tokens'", resp.text)
        self.assertIn("method: 'POST'", resp.text)
        self.assertIn("method: 'PATCH'", resp.text)
        self.assertIn("method: 'DELETE'", resp.text)


class TestPushConfigPage(WebUiPageTestBase):
    """GET /push-config — gpt-load instances + provider mappings."""

    def test_push_config_page_with_bearer(self) -> None:
        """GET /push-config returns 200 with instance table + mapping table."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("推送配置", resp.text)
        self.assertIn("gpt-load 实例", resp.text)
        self.assertIn("推送目标映射", resp.text)

    def test_push_config_shows_seeded_instance(self) -> None:
        """Seeded gpt-load instance '测试实例' must appear in the page."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("测试实例", resp.text)
        self.assertIn("http://127.0.0.1:8080", resp.text)

    def test_push_config_shows_providers(self) -> None:
        """Providers from schedule_config must appear in the mapping table."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deepseek", resp.text)
        self.assertIn("kimi", resp.text)

    def test_push_config_shows_mapped_status(self) -> None:
        """deepseek mapping has '已配置' tag."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        # deepseek has a mapping → 已配置
        self.assertIn("已配置", resp.text)

    def test_push_config_shows_unmapped_status(self) -> None:
        """kimi has no mapping → should show '未配置'."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("未配置", resp.text)

    def test_push_config_has_add_instance_form(self) -> None:
        """Page must contain the add-instance form."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("add-instance-form", resp.text)
        self.assertIn("inst_name", resp.text)
        self.assertIn("inst_url", resp.text)
        self.assertIn("inst_key", resp.text)

    def test_push_config_has_mapping_selects(self) -> None:
        """Page must have config-select and group-select dropdowns for mappings."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("mapping-config", resp.text)
        self.assertIn("mapping-group", resp.text)
        self.assertIn("saveMapping(", resp.text)
        self.assertIn("deleteMapping(", resp.text)

    def test_push_config_has_delete_instance_buttons(self) -> None:
        """Each instance row must have a delete button."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("deleteInstance(", resp.text)

    def test_push_config_contains_fetch_js(self) -> None:
        """Inline JS must call fetch for gpt-load operations."""
        resp = self.client.get("/push-config", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("fetch('/api/gpt-load'", resp.text)
        self.assertIn("method: 'POST'", resp.text)
        self.assertIn("method: 'PUT'", resp.text)

    def test_push_config_requires_auth(self) -> None:
        """GET /push-config without auth redirects to login (303)."""
        resp = self.client.get("/push-config", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers.get("location"), "/login")


class TestRunNewKeysSection(WebUiPageTestBase):
    """'最近新增 Key' section on dashboard and run detail (run_new_keys).

    RED tests: the feature (context key `new_keys`, template section) does
    not exist yet, so every content assertion below must fail.
    """

    _TOKEN = "sk-proj-1234567890abcdef"

    @staticmethod
    def _key_hash(token: str) -> str:
        """Stable key_hash for run_new_keys (sha256 of the token)."""
        return hashlib.sha256(token.encode()).hexdigest()

    def _new_key_row(self, run_id: str) -> tuple[str, str, str, str, str, str]:
        return (
            run_id,
            "github",
            "github",
            self._key_hash(self._TOKEN),
            mask_token(self._TOKEN),
            "2026-01-01 00:00:00",
        )

    def test_dashboard_shows_new_keys_section(self) -> None:
        """GET / must render '最近新增 Key' header and the masked token."""
        _seed_run_new_keys(self._db_path, [self._new_key_row("run-aaaa-0001")])
        resp = self.client.get("/", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("最近新增 Key", resp.text)
        self.assertIn(mask_token(self._TOKEN), resp.text)

    def test_dashboard_empty_state_when_no_new_keys(self) -> None:
        """GET / must render '暂无新增' when run_new_keys has no rows."""
        _seed_run_new_keys(self._db_path, [])
        resp = self.client.get("/", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("暂无新增", resp.text)

    def test_run_detail_shows_new_keys(self) -> None:
        """GET /runs/{run_id} must render the run's newly added masked key."""
        _seed_run_new_keys(self._db_path, [self._new_key_row("run-aaaa-0001")])
        resp = self.client.get("/runs/run-aaaa-0001", headers=_auth_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn(mask_token(self._TOKEN), resp.text)


if __name__ == "__main__":
    unittest.main()
