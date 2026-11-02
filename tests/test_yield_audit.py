#!/usr/bin/env python3

"""Yield-audit accounting (GAP-5 / plan todo 1).

Given a temp SQLite DB holding run_records + run_new_keys + push_logs,
When ``scripts/audit_yield.py`` runs for a bounded day window,
Then each provider's record reconciles these independent口径 with NO conflation:

- ``unique_new`` — DISTINCT ``run_new_keys.key_hash`` NEWLY harvested by the
  windowed runs (a dual-tier key appears ONCE);
- ``organic_placements`` — ``push_logs.added_count`` where ``run_id`` joins a
  windowed run (natural run pushes);
- ``manual_placements`` — ``push_logs.added_count`` where ``run_id`` does NOT
  join any run (manual/verification pushes), windowed by ``pushed_at``;
- ``ignored`` — ignored_count across both placement kinds.

The fixture below is explicit about which key sits in which tier so the
accounting can be proven, not just asserted: ``sk-doubao-dual-1`` is harvested
once (one run_new_keys row) yet placed twice (tier A + tier B organic groups).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_AUDIT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "audit_yield.py"

_spec = importlib.util.spec_from_file_location("audit_yield", _AUDIT_PATH)
assert _spec is not None and _spec.loader is not None
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)

_PROVIDERS = ["doubao", "mimo-sg", "qwen-cn", "qwen-intl", "mimo-cn"]


def _run_async(coro):
    return asyncio.run(coro)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(db_path: str) -> None:
    from web.db import init_db

    _run_async(init_db(db_path))


def _build_fixture(tmpdir: str) -> dict:
    """Build the dual-tier fixture and return the metadata needed to prove it."""
    db_path = str(Path(tmpdir) / "harvester.db")
    _init_db(db_path)
    conn = _connect(db_path)
    try:
        raw_keys = [
            "sk-doubao-dual-1",  # dual-tier: tier A AND tier B
            "sk-doubao-2",
            "sk-doubao-3",
            "sk-doubao-4",
            "sk-doubao-5",
        ]
        # tier A carries dual + 2,3; tier B carries dual + 4; key 5 is manual only.
        tier_a = {raw_keys[0], raw_keys[1], raw_keys[2]}
        tier_b = {raw_keys[0], raw_keys[3]}

        # Windowed run: 1.0 machine-hours, 5 valid, 12 checked.
        conn.execute(
            "INSERT INTO run_records (id, provider_name, config_file, status, "
            "started_at, finished_at, duration_seconds, valid_keys_found, "
            "total_keys_checked, links_total, materials_total) "
            "VALUES (?, ?, ?, ?, datetime('now','-60 minutes'), "
            "datetime('now','-50 minutes'), 3600.0, 5, 12, 1000, 7)",
            ("run-doubao-1", "doubao", "config-doubao.yaml", "completed"),
        )
        # Old runs that the 30/14-day windows must exclude.
        conn.execute(
            "INSERT INTO run_records (id, provider_name, config_file, status, "
            "started_at, valid_keys_found) "
            "VALUES ('run-doubao-old', 'doubao', 'c.yaml', 'completed', "
            "datetime('now','-40 days'), 999)"
        )
        conn.execute(
            "INSERT INTO run_records (id, provider_name, config_file, status, "
            "started_at, valid_keys_found) "
            "VALUES ('run-qwen-cn-old', 'qwen-cn', 'c.yaml', 'completed', "
            "datetime('now','-40 days'), 888)"
        )

        # 5 distinct harvested hashes; the dual-tier key is inserted once and
        # the duplicate is ignored by UNIQUE(run_id, key_hash) -> 5 rows stay.
        for raw in raw_keys:
            conn.execute(
                "INSERT INTO run_new_keys (run_id, provider_name, task_name, "
                "key_hash, token_masked) VALUES (?, ?, ?, ?, ?)",
                ("run-doubao-1", "doubao", "doubao", _hash_token(raw), "sk-***"),
            )
        conn.execute(
            "INSERT OR IGNORE INTO run_new_keys (run_id, provider_name, "
            "task_name, key_hash, token_masked) VALUES (?, ?, ?, ?, ?)",
            ("run-doubao-1", "doubao", "doubao", _hash_token(raw_keys[0]), "sk-***"),
        )
        conn.execute(
            "INSERT INTO run_new_keys (run_id, provider_name, task_name, "
            "key_hash, token_masked) VALUES (?, ?, ?, ?, ?)",
            ("run-qwen-cn-old", "qwen-cn", "qwen-cn", _hash_token("old-qwen"), "***"),
        )

        def _push(run_id, provider, group_id, keys_count, added, ignored, when):
            conn.execute(
                "INSERT INTO push_logs (run_id, provider_name, "
                "gpt_load_config_id, group_id, keys_count, added_count, "
                "ignored_count, status, pushed_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, 'success', datetime('now', ?))",
                (run_id, provider, group_id, keys_count, added, ignored, when),
            )

        # Organic tier A (dual + 2 + 3) and tier B (dual + 4): the dual key is
        # placed twice -> organic added = 3 + 2 = 5 for 5 distinct hashes.
        _push("run-doubao-1", "doubao", 10, 3, 3, 1, "-45 minutes")
        _push("run-doubao-1", "doubao", 11, 2, 2, 0, "-45 minutes")
        # Manual/verification push (run_id has NO run_records row): all 5 keys.
        _push("verification-doubao-1", "doubao", 99, 5, 5, 1, "-40 minutes")
        # Old manual placement excluded by the window.
        _push("verification-doubao-old", "doubao", 99, 50, 50, 0, "-40 days")
        # Organic placement for the OLD run: excluded because the RUN is out of
        # window (organic windows by the run's started_at, not pushed_at).
        _push("run-doubao-old", "doubao", 1, 10, 10, 0, "-30 minutes")

        conn.commit()
        return {
            "db_path": db_path,
            "dual_raw": raw_keys[0],
            "dual_hash": _hash_token(raw_keys[0]),
            "tier_a": tier_a,
            "tier_b": tier_b,
        }
    finally:
        conn.close()


class TestYieldAuditAccounting(unittest.TestCase):
    """The fixture reconciles unique harvest vs organic/manual placements."""

    def test_doubao_accounting_is_not_conflated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = _build_fixture(tmpdir)
            report = _audit.audit(meta["db_path"], [30], _PROVIDERS)

            doubao = report["windows"]["30"]["providers"]["doubao"]
            self.assertEqual(doubao["runs"], 1)
            self.assertEqual(doubao["machine_hours"], 1.0)
            self.assertEqual(doubao["valid"], 5)
            self.assertEqual(doubao["total_keys_checked"], 12)
            self.assertEqual(doubao["links_total"], 1000)
            self.assertEqual(doubao["materials_total"], 7)
            self.assertEqual(doubao["unique_new"], 5)
            self.assertEqual(doubao["organic_placements"], 5)
            self.assertEqual(doubao["manual_placements"], 5)
            self.assertEqual(doubao["ignored"], 2)

    def test_dual_tier_key_once_harvest_twice_placement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = _build_fixture(tmpdir)
            dual_hash = meta["dual_hash"]

            # Once as harvest: a single run_new_keys row for the dual key,
            # despite the duplicate insert attempt.
            conn = _connect(meta["db_path"])
            try:
                harvest_rows = conn.execute(
                    "SELECT COUNT(*) AS n FROM run_new_keys "
                    "WHERE run_id='run-doubao-1' AND key_hash = ?",
                    (dual_hash,),
                ).fetchone()["n"]
                organic_rows = conn.execute(
                    "SELECT COUNT(*) AS n FROM push_logs WHERE run_id='run-doubao-1'"
                ).fetchone()["n"]
                total_harvest = conn.execute(
                    "SELECT COUNT(DISTINCT key_hash) AS n FROM run_new_keys "
                    "WHERE run_id='run-doubao-1'"
                ).fetchone()["n"]
            finally:
                conn.close()

            self.assertEqual(total_harvest, 5)
            self.assertEqual(harvest_rows, 1, "dual-tier key harvested once")
            self.assertEqual(
                organic_rows, 2, "dual-tier key placed into two tier groups"
            )
            # By fixture construction the dual key is the only one spanning both
            # tiers, so its harvest count (1) understates its placement count (2).
            self.assertEqual(meta["tier_a"] & meta["tier_b"], {meta["dual_raw"]})

    def test_old_runs_and_placements_excluded_by_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = _build_fixture(tmpdir)
            report = _audit.audit(meta["db_path"], [30], _PROVIDERS)

            doubao = report["windows"]["30"]["providers"]["doubao"]
            # Old doubao run (999 valid) and its placement are both excluded.
            self.assertEqual(doubao["runs"], 1)
            self.assertEqual(doubao["valid"], 5)
            self.assertEqual(doubao["organic_placements"], 5)
            self.assertEqual(doubao["manual_placements"], 5)

            qwen = report["windows"]["30"]["providers"]["qwen-cn"]
            self.assertEqual(qwen["runs"], 0)
            self.assertEqual(qwen["unique_new"], 0)

    def test_14_day_window_matches_when_all_fixture_data_is_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = _build_fixture(tmpdir)
            report = _audit.audit(meta["db_path"], [14], _PROVIDERS)
            doubao = report["windows"]["14"]["providers"]["doubao"]
            self.assertEqual(doubao["unique_new"], 5)
            self.assertEqual(doubao["organic_placements"], 5)
            self.assertEqual(doubao["manual_placements"], 5)

    def test_tier_suffixed_push_row_attributed_to_base_provider(self) -> None:
        """A ``doubao:seed`` push_logs row must count for provider ``doubao``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "harvester.db")
            _init_db(db_path)
            conn = _connect(db_path)
            try:
                conn.execute(
                    "INSERT INTO push_logs (run_id, provider_name, "
                    "gpt_load_config_id, group_id, keys_count, added_count, "
                    "ignored_count, status, pushed_at) "
                    "VALUES ('verify-1', 'doubao:seed', 1, 5, 2, 2, 0, "
                    "'success', datetime('now','-10 minutes'))"
                )
                conn.commit()
            finally:
                conn.close()

            report = _audit.audit(db_path, [30], ["doubao"])
            doubao = report["windows"]["30"]["providers"]["doubao"]
            self.assertEqual(doubao["manual_placements"], 2)

    def test_totals_sum_across_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = _build_fixture(tmpdir)
            report = _audit.audit(meta["db_path"], [30], _PROVIDERS)
            totals = report["windows"]["30"]["totals"]
            self.assertEqual(totals["unique_new"], 5)
            self.assertEqual(totals["organic_placements"], 5)
            self.assertEqual(totals["manual_placements"], 5)
            self.assertEqual(totals["machine_hours"], 1.0)


class TestYieldAuditCLI(unittest.TestCase):
    """The CLI prints schema-valid JSON; a missing-schema DB is a clean error."""

    def test_cli_emits_schema_valid_json_with_fixture_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = _build_fixture(tmpdir)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(_AUDIT_PATH),
                    "--db",
                    meta["db_path"],
                    "--days",
                    "30",
                    "--providers",
                    "doubao,mimo-sg,qwen-cn,qwen-intl,mimo-cn",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)  # schema-valid JSON
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["days"], [30])
            self.assertEqual(sorted(report["providers"]), sorted(_PROVIDERS))
            doubao = report["windows"]["30"]["providers"]["doubao"]
            self.assertEqual(doubao["unique_new"], 5)
            self.assertEqual(doubao["organic_placements"], 5)
            self.assertEqual(doubao["manual_placements"], 5)

    def test_cli_missing_schema_returns_nonzero_and_concise_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # An empty file is a valid SQLite DB with NO required tables.
            missing = str(Path(tmpdir) / "empty.db")
            sqlite3.connect(missing).close()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(_AUDIT_PATH),
                    "--db",
                    missing,
                    "--days",
                    "30",
                    "--providers",
                    "doubao",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertIn("missing required table", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()