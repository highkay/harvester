#!/usr/bin/env python3

"""Unit tests for web/runner.py — PipelineRunner (TDD: RED phase)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from web.crypto import CryptoManager, decrypt_str, encrypt_str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Helper to run an async test from a sync unittest method."""
    return asyncio.run(coro)


_SAMPLE_YAML = """\
# Minimal config for testing
global:
  workspace: "./data-test"
  github_credentials:
    sessions:
      - "your_session_placeholder"
    tokens:
      - "your_token_placeholder"
    strategy: "round_robin"

pipeline:
  threads:
    search: 1
    gather: 2
    check: 1
    inspect: 1

monitoring:
  update_interval: 2.0
  error_threshold: 0.1

persistence:
  auto_restore: false
  shutdown_timeout: 30

ratelimits:
  github_api:
    base_rate: 0.15
    adaptive: true

tasks:
  - name: test-provider
    enabled: true
    provider_type: deepseek
    use_api: true
    max_pages: 10
    stages:
      search: true
      gather: true
      check: true
      inspect: true
    api:
      base_url: https://api.test.com
      timeout: 30
      retries: 3
    patterns:
      key_pattern: "sk-[0-9A-Za-z_-]{20,}"
    conditions:
      - query: '"test"'
"""


class TestTempYamlGeneration(unittest.TestCase):
    """Given a minimal source YAML,
    When _generate_temp_yaml is called,
    Then tokens are injected, sessions is empty list, path contains run_id.
    """

    def test_tokens_injected_and_sessions_empty(self) -> None:
        from web.runner import PipelineRunner

        real_tokens = ["ghp_token_one", "ghp_token_two"]

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_yaml = workdir / "config-test-provider.yaml"
            source_yaml.write_text(_SAMPLE_YAML, encoding="utf-8")
            runtime_dir = workdir / "runtime"
            runtime_dir.mkdir()

            runner = PipelineRunner.__new__(PipelineRunner)
            runner._workspace = str(workdir)
            runner._init_yaml_source_dir = str(workdir)

            run_id = "test-run-id-1234"
            result_path = runner._generate_temp_yaml(
                "test-provider", run_id, real_tokens
            )

            # Verify file path contains run_id
            self.assertIn(run_id, str(result_path))

            # Read generated YAML and verify content
            generated = yaml.safe_load(result_path.read_text(encoding="utf-8"))

            gh_creds = generated["global"]["github_credentials"]
            self.assertEqual(gh_creds["tokens"], real_tokens)
            self.assertEqual(gh_creds["sessions"], [])

    def test_proxy_injected_from_env(self) -> None:
        """When HARVESTER_PROXY is set, global.proxy is injected into the YAML."""
        from web.runner import PipelineRunner

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_yaml = workdir / "config-test-provider.yaml"
            source_yaml.write_text(_SAMPLE_YAML, encoding="utf-8")
            runtime_dir = workdir / "runtime"
            runtime_dir.mkdir()

            runner = PipelineRunner.__new__(PipelineRunner)
            runner._workspace = str(workdir)
            runner._init_yaml_source_dir = str(workdir)

            with patch.dict(
                os.environ,
                {"HARVESTER_PROXY": "socks5://172.23.0.1:1080"},
                clear=False,
            ):
                result_path = runner._generate_temp_yaml(
                    "test-provider", "run-proxy-1", ["ghp_token_one"]
                )
            generated = yaml.safe_load(result_path.read_text(encoding="utf-8"))
            self.assertEqual(
                generated["global"].get("proxy"), "socks5://172.23.0.1:1080"
            )

    def test_proxy_absent_when_env_unset(self) -> None:
        """When HARVESTER_PROXY is unset, no proxy key is forced into the YAML."""
        from web.runner import PipelineRunner

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_yaml = workdir / "config-test-provider.yaml"
            source_yaml.write_text(_SAMPLE_YAML, encoding="utf-8")
            runtime_dir = workdir / "runtime"
            runtime_dir.mkdir()

            runner = PipelineRunner.__new__(PipelineRunner)
            runner._workspace = str(workdir)
            runner._init_yaml_source_dir = str(workdir)

            with patch.dict(os.environ, {}, clear=False):
                # Remove HARVESTER_PROXY if a parent env has it
                os.environ.pop("HARVESTER_PROXY", None)
                result_path = runner._generate_temp_yaml(
                    "test-provider", "run-noproxy-1", ["ghp_token_one"]
                )
            generated = yaml.safe_load(result_path.read_text(encoding="utf-8"))
            self.assertNotIn("proxy", generated["global"])

    def test_proxy_round_robin_cycles(self) -> None:
        """Comma-separated HARVESTER_PROXY rotates per _pick_proxy call."""
        from web.runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        runner._proxy_index = 0
        runner._proxy_lock = __import__("threading").Lock()

        proxies = [
            "socks5://192.168.1.18:1080",
            "socks5://192.168.1.18:1090",
            "socks5://192.168.1.18:1091",
        ]
        with patch.dict(os.environ, {"HARVESTER_PROXY": ",".join(proxies)}, clear=False):
            picks = [runner._pick_proxy() for _ in range(4)]

        # First 3 calls cycle through all proxies, 4th wraps to the first
        self.assertEqual(picks[:3], proxies)
        self.assertEqual(picks[3], proxies[0])

    def test_proxy_single_returns_itself(self) -> None:
        """Single-proxy env always returns that proxy (no rotation state)."""
        from web.runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        with patch.dict(
            os.environ,
            {"HARVESTER_PROXY": "socks5://192.168.1.18:1080"},
            clear=False,
        ):
            self.assertEqual(runner._pick_proxy(), "socks5://192.168.1.18:1080")
            self.assertEqual(runner._pick_proxy(), "socks5://192.168.1.18:1080")

    def test_proxy_empty_env_returns_empty(self) -> None:
        """Unset/empty HARVESTER_PROXY yields '' (no proxy)."""
        from web.runner import PipelineRunner

        runner = PipelineRunner.__new__(PipelineRunner)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HARVESTER_PROXY", None)
            self.assertEqual(runner._pick_proxy(), "")


class TestReEntrancyPrevention(unittest.TestCase):
    """Given a PipelineRunner,
    When two run_scan calls target the same provider,
    Then the second call raises HTTPException(409).
    """

    def test_second_concurrent_scan_raises_409(self) -> None:
        from web.runner import PipelineRunner

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_yaml = workdir / "config-test-provider.yaml"
            source_yaml.write_text(_SAMPLE_YAML, encoding="utf-8")

            db_path = str(workdir / "harvester.db")
            # Create run_records table (needed by _insert_run_record)
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS run_records (
                    id TEXT PRIMARY KEY,
                    provider_name TEXT NOT NULL,
                    config_file TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT,
                    duration_seconds REAL,
                    valid_keys_found INTEGER DEFAULT 0,
                    total_keys_checked INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.commit()
            conn.close()

            # Create runner with overridden _execute that sleeps
            runner = PipelineRunner.__new__(PipelineRunner)
            runner._workspace = str(workdir)
            runner._init_yaml_source_dir = str(workdir)
            runner._db_path = db_path
            runner._running = {}
            runner._locks = {}
            runner._cancel_events = {}

            # Mock _execute to sleep (simulate long-running scan)
            def slow_execute(provider_name: str, run_id: str) -> None:
                time.sleep(0.5)

            runner._execute = slow_execute  # type: ignore[method-assign]
            runner._init_executor = False

            from web.runner import _get_db_path

            async def run_test() -> None:
                # First call: should succeed immediately
                await runner.run_scan("test-provider")
                # Second call while first is sleeping: should raise 409
                with self.assertRaises(Exception) as ctx:
                    await runner.run_scan("test-provider")
                self.assertIn("409", str(ctx.exception))

            _run_async(run_test())


class TestRunRecordsLifecycle(unittest.TestCase):
    """Given a PipelineRunner with a temp DB,
    When run_scan is called and _execute completes,
    Then list_runs includes a completed record.
    """

    def test_run_scan_creates_completed_record(self) -> None:
        from web.runner import PipelineRunner

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_yaml = workdir / "config-test-provider.yaml"
            source_yaml.write_text(_SAMPLE_YAML, encoding="utf-8")

            db_path = str(workdir / "harvester.db")

            # Initialize DB with run_records table
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS run_records (
                    id TEXT PRIMARY KEY,
                    provider_name TEXT NOT NULL,
                    config_file TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT,
                    duration_seconds REAL,
                    valid_keys_found INTEGER DEFAULT 0,
                    total_keys_checked INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.commit()
            conn.close()

            # Create runner with overridden _execute that updates DB directly
            runner = PipelineRunner.__new__(PipelineRunner)
            runner._workspace = str(workdir)
            runner._init_yaml_source_dir = str(workdir)
            runner._db_path = db_path
            runner._running = {}
            runner._locks = {}
            runner._cancel_events = {}

            # Override _execute to simulate successful completion
            def success_execute(provider_name: str, run_id: str) -> None:
                # Simulate successful scan
                conn_local = sqlite3.connect(db_path)
                try:
                    conn_local.execute(
                        """UPDATE run_records
                           SET status='completed',
                               finished_at=datetime('now'),
                               duration_seconds=1.5,
                               valid_keys_found=42
                           WHERE id=?""",
                        (run_id,),
                    )
                    conn_local.commit()
                finally:
                    conn_local.close()
                # Clean up _running
                runner._running.pop(provider_name, None)

            runner._execute = success_execute  # type: ignore[method-assign]

            async def run_test() -> None:
                run_id = await runner.run_scan("test-provider")
                # Wait for thread to finish
                await asyncio.sleep(0.3)

                # Verify list_runs shows the completed record
                runs = await runner.list_runs()
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["id"], run_id)
                self.assertEqual(runs[0]["status"], "completed")
                self.assertEqual(runs[0]["valid_keys_found"], 42)

            _run_async(run_test())

    def test_execute_fires_push_hook_on_completion(self) -> None:
        """_execute must call _on_completed after a successful scan."""
        from web.runner import PipelineRunner

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            source_yaml = workdir / "config-test-provider.yaml"
            source_yaml.write_text(_SAMPLE_YAML, encoding="utf-8")

            db_path = str(workdir / "harvester.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS run_records ("
                "id TEXT PRIMARY KEY, provider_name TEXT NOT NULL, "
                "config_file TEXT NOT NULL, status TEXT NOT NULL, "
                "started_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "finished_at TEXT, duration_seconds REAL, "
                "valid_keys_found INTEGER DEFAULT 0, "
                "total_keys_checked INTEGER DEFAULT 0, "
                "error_message TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            conn.commit()
            conn.close()

            runner = PipelineRunner.__new__(PipelineRunner)
            runner._workspace = str(workdir)
            runner._init_yaml_source_dir = str(workdir)
            runner._db_path = db_path
            runner._running = {}
            runner._locks = {}
            runner._cancel_events = {}

            import yaml as _yaml

            def fake_execute(provider_name: str, run_id: str) -> None:
                # emulate _generate_temp_yaml + app.run + _update_run_sync
                # (run_scan already inserted the record — just mark completed)
                tmp = workdir / "runtime"
                tmp.mkdir(exist_ok=True)
                (tmp / f"config-{provider_name}-{run_id}.yaml").write_text(
                    _yaml.dump({"global": {"github_credentials": {"tokens": []}}}),
                    encoding="utf-8",
                )
                conn2 = sqlite3.connect(db_path)
                conn2.execute(
                    "UPDATE run_records SET status='completed', valid_keys_found=3 "
                    "WHERE id=?",
                    (run_id,),
                )
                conn2.commit()
                conn2.close()
                runner._running.pop(provider_name, None)
                # NEW: the production _execute calls _on_completed here
                runner._on_completed(provider_name, run_id)

            runner._execute = fake_execute  # type: ignore[method-assign]

            # Mock the push service so no real network call happens
            # (_on_completed imports get_push_service from web.push internally)
            with patch("web.push.get_push_service") as mock_get:
                fake_svc = MagicMock()
                mock_get.return_value = fake_svc

                async def run_test() -> None:
                    run_id = await runner.run_scan("test-provider")
                    # _on_completed spawns a daemon thread for the push; poll
                    # until it has been invoked (bounded wait)
                    for _ in range(50):
                        if fake_svc.push_valid_keys.called:
                            break
                        await asyncio.sleep(0.05)
                    self.assertTrue(
                        fake_svc.push_valid_keys.called,
                        "push_valid_keys should be called after completion",
                    )
                    args = fake_svc.push_valid_keys.call_args[0]
                    self.assertEqual(args[0], "test-provider")
                    self.assertEqual(args[1], run_id)

                _run_async(run_test())


class TestTokenReadingFromDb(unittest.TestCase):
    """Given a DB with an encrypted API token,
    When _get_enabled_api_tokens is called,
    Then it returns the decrypted plaintext token list.
    """

    def test_get_enabled_api_tokens_returns_decrypted(self) -> None:
        from web.runner import PipelineRunner

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")

            # Initialize DB with github_tokens table
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS github_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_type TEXT NOT NULL CHECK(token_type IN ('api','session')),
                    token_encrypted TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    label TEXT DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.commit()

            plain_token = "ghp_my_real_test_token_12345"
            # Use module-level encrypt_str so decryption uses the same key
            encrypted = encrypt_str(plain_token)
            token_hash = CryptoManager.hash_token(plain_token)

            conn.execute(
                "INSERT INTO github_tokens (token_type, token_encrypted, token_hash, label, enabled) VALUES (?, ?, ?, ?, ?)",
                ("api", encrypted, token_hash, "test-label", 1),
            )
            conn.commit()

            # Insert a disabled token (should not be returned)
            disabled_encrypted = encrypt_str("ghp_disabled_token")
            conn.execute(
                "INSERT INTO github_tokens (token_type, token_encrypted, token_hash, label, enabled) VALUES (?, ?, ?, ?, ?)",
                ("api", disabled_encrypted, CryptoManager.hash_token("ghp_disabled_token"), "disabled", 0),
            )
            conn.commit()

            # Insert a session token (should not be returned since type != 'api')
            session_encrypted = encrypt_str("session_cookie_value")
            conn.execute(
                "INSERT INTO github_tokens (token_type, token_encrypted, token_hash, label, enabled) VALUES (?, ?, ?, ?, ?)",
                ("session", session_encrypted, CryptoManager.hash_token("session_cookie_value"), "session", 1),
            )
            conn.commit()
            conn.close()

            runner = PipelineRunner.__new__(PipelineRunner)
            runner._db_path = db_path
            tokens = runner._get_enabled_api_tokens()

            self.assertEqual(tokens, [plain_token])


class TestCancelRun(unittest.TestCase):
    """Given a running scan,
    When cancel_run is called,
    Then the cancel event is set and the record is updated.
    """

    def test_cancel_run_sets_event_and_updates_db(self) -> None:
        from web.runner import PipelineRunner

        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            db_path = str(workdir / "harvester.db")

            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS run_records (
                    id TEXT PRIMARY KEY,
                    provider_name TEXT NOT NULL,
                    config_file TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
                    started_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT,
                    duration_seconds REAL,
                    valid_keys_found INTEGER DEFAULT 0,
                    total_keys_checked INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""
            )
            conn.execute(
                "INSERT INTO run_records (id, provider_name, config_file, status) VALUES (?, ?, ?, ?)",
                ("cancel-test-id", "test-provider", "fake.yaml", "running"),
            )
            conn.commit()
            conn.close()

            runner = PipelineRunner.__new__(PipelineRunner)
            runner._db_path = db_path
            runner._running = {"test-provider": "cancel-test-id"}
            runner._locks = {}
            runner._cancel_events = {}

            async def run_test() -> None:
                result = await runner.cancel_run("cancel-test-id")
                self.assertTrue(result)

                # Verify DB record updated to cancelled
                run = await runner.get_run("cancel-test-id")
                self.assertIsNotNone(run)
                self.assertEqual(run["status"], "cancelled")
                self.assertIsNotNone(run["finished_at"])

            _run_async(run_test())
