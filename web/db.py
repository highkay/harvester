#!/usr/bin/env python3

"""Async SQLite database layer — connection factory and schema initialization.

Connection factory enables WAL journal mode and foreign keys.
Schema initialisation creates six tables with ``CREATE TABLE IF NOT EXISTS``.

``db_path`` resolution: ``HARVESTER_DB`` env var > ``<HARVESTER_WORKSPACE>/harvester.db``
(default workspace is ``./data``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import aiosqlite

_DEFAULT_WORKSPACE: Final[str] = "./data"
_DEFAULT_DB_NAME: Final[str] = "harvester.db"

_DDL: Final[str] = """
-- GitHub token (encrypted storage, dedup by hash)
CREATE TABLE IF NOT EXISTS github_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_type TEXT NOT NULL CHECK(token_type IN ('api','session')),
    token_encrypted TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- gpt-load instance config (AUTH_KEY encrypted)
CREATE TABLE IF NOT EXISTS gpt_load_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    auth_key_encrypted TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- provider → gpt-load group mapping (per-task push target + batch size)
CREATE TABLE IF NOT EXISTS provider_group_mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_name TEXT NOT NULL UNIQUE,
    gpt_load_config_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    max_size INTEGER NOT NULL DEFAULT 10000,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- scan run records
CREATE TABLE IF NOT EXISTS run_records (
    id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL,
    config_file TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_seconds REAL,
    valid_keys_found INTEGER DEFAULT 0,
    total_keys_checked INTEGER DEFAULT 0,
    links_total INTEGER DEFAULT 0,
    materials_total INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- push logs (per-run, per-group push audit)
CREATE TABLE IF NOT EXISTS push_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    gpt_load_config_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    keys_count INTEGER NOT NULL,
    added_count INTEGER NOT NULL,
    ignored_count INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','failed','partial')),
    error_message TEXT,
    pushed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- schedule config (APScheduler rebuilds jobs from this table on startup)
CREATE TABLE IF NOT EXISTS schedule_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_name TEXT NOT NULL UNIQUE,
    cron_expression TEXT NOT NULL DEFAULT '0 3 * * *',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_file TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- per-run newly-added valid keys (masked + hashed only, never plaintext)
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


def resolve_db_path() -> str:
    """Return the SQLite database file path.

    Priority:
    1. ``HARVESTER_DB_PATH`` environment variable (canonical, matches
       ``WebSettings.db_path`` in web/config.py)
    2. ``HARVESTER_DB`` environment variable (legacy alias)
    3. ``<HARVESTER_WORKSPACE>/harvester.db`` (default workspace ``./data``)
    """
    env_db = os.environ.get("HARVESTER_DB_PATH") or os.environ.get("HARVESTER_DB")
    if env_db:
        return env_db
    workspace = Path(os.environ.get("HARVESTER_WORKSPACE", _DEFAULT_WORKSPACE))
    return str(workspace / _DEFAULT_DB_NAME)


async def get_db(db_path: str | None = None) -> aiosqlite.Connection:
    """Open an async SQLite connection with WAL journal mode and foreign keys enabled.

    When *db_path* is ``None``, it is resolved via :func:`resolve_db_path`.
    """
    path = db_path if db_path is not None else resolve_db_path()
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def init_db(db_path: str | None = None) -> None:
    """Create the parent directory (if missing) and run ``CREATE TABLE IF NOT EXISTS`` DDL.

    When *db_path* is ``None``, it is resolved via :func:`resolve_db_path`.
    """
    path = db_path if db_path is not None else resolve_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    db = await get_db(path)
    try:
        await db.executescript(_DDL)
        await _run_migrations(db)
        await db.commit()
    finally:
        await db.close()


def _count_valid_keys_for_provider(provider_name: str) -> int:
    """Count non-empty lines in ``providers/{provider_name}/valid-keys.txt``.

    Scoped strictly to this run's own provider directory — reconciliation
    must never report another provider's count. Missing or unreadable file
    → 0. Workspace resolution mirrors :func:`resolve_db_path`
    (``HARVESTER_WORKSPACE``, default ``./data``).

    Caveat: if the process died before the pipeline backed the old files
    away, the count may include a previous run's leftovers — best effort
    without touching provider directories, which are out of scope here.
    """
    workspace = Path(os.environ.get("HARVESTER_WORKSPACE", _DEFAULT_WORKSPACE))
    keys_path = workspace / "providers" / provider_name / "valid-keys.txt"
    try:
        return sum(
            1
            for line in keys_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except (OSError, UnicodeDecodeError):
        return 0


async def reconcile_running_runs(db_path: str | None = None) -> int:
    """Mark rows left in 'running' by a dead process as failed. Returns rowcount.

    Each reconciled row also records:

    - ``duration_seconds`` — wall clock from the row's own ``started_at``
      to now, computed in SQL via ``julianday`` (the dead process's
      Python-side start time is lost with it);
    - ``valid_keys_found`` — counted from THIS row's provider directory
      only (see :func:`_count_valid_keys_for_provider`).
    """
    path = db_path if db_path is not None else resolve_db_path()
    db = await get_db(path)
    try:
        cursor = await db.execute(
            "SELECT id, provider_name FROM run_records WHERE status='running'"
        )
        rows = list(await cursor.fetchall())
        for row in rows:
            await db.execute(
                "UPDATE run_records SET status='failed', "
                "finished_at=datetime('now'), "
                "duration_seconds=CAST((julianday('now') "
                "- julianday(started_at)) * 86400 AS REAL), "
                "valid_keys_found=?, "
                "error_message='interrupted by service restart' "
                "WHERE id=? AND status='running'",
                (
                    _count_valid_keys_for_provider(row["provider_name"]),
                    row["id"],
                ),
            )
        await db.commit()
        return len(rows)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Lightweight migrations for pre-existing databases
# ---------------------------------------------------------------------------


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Apply additive column migrations to databases created by older builds.

    New columns are added via ``ALTER TABLE ... ADD COLUMN`` only when
    missing, so this is safe to re-run on every startup.
    """
    migrations = (
        (
            "provider_group_mapping",
            "max_size",
            "ALTER TABLE provider_group_mapping "
            "ADD COLUMN max_size INTEGER NOT NULL DEFAULT 10000",
        ),
        (
            "run_records",
            "links_total",
            "ALTER TABLE run_records ADD COLUMN links_total INTEGER DEFAULT 0",
        ),
        (
            "run_records",
            "materials_total",
            "ALTER TABLE run_records ADD COLUMN materials_total INTEGER DEFAULT 0",
        ),
    )
    for table, column, ddl in migrations:
        try:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            columns = [row["name"] for row in await cursor.fetchall()]
        except Exception:
            continue  # table does not exist yet — CREATE TABLE already handles it
        if column not in columns:
            await db.execute(ddl)
