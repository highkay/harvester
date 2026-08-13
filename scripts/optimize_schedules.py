#!/usr/bin/env python3

"""Idempotent migration — apply the optimized collection schedule to a DB.

The web layer seeds ``schedule_config`` from ``web.scheduler._DEFAULT_SCHEDULES``
only when the table is empty, so an existing production database keeps its old
daily (`0 3 * * *`) rows after an upgrade. This script upserts the optimized
schedule (more providers + higher frequency) into any database, without
touching providers that are not part of the default list.

Usage::

    python scripts/optimize_schedules.py [DB_PATH]

DB path resolution (in order): ``argv[1]`` > ``$HARVESTER_DB_PATH`` >
``$HARVESTER_DB`` > ``<HARVESTER_WORKSPACE>/harvester.db`` (default ``./data``).

Re-running is safe: rows already at the target cron are left unchanged.

Why the optimized schedule: GitHub-leaked AI API keys are revoked within
hours, so once-a-day collection misses keys that appear and get revoked in the
same day (measured: kimi gained 82 valid keys in a ~2.7-hour same-day window).
High-churn providers are moved to every-4-hours (staggered 15 min apart) and
the remaining providers to every-6-hours.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Put the repo root on sys.path so `web.scheduler` imports resolve.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from web.scheduler import _DEFAULT_SCHEDULES  # noqa: E402

# Mirrors web/db.py `_DDL` for schedule_config (CREATE IF NOT EXISTS is safe).
_DDL = """
CREATE TABLE IF NOT EXISTS schedule_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_name TEXT NOT NULL UNIQUE,
    cron_expression TEXT NOT NULL DEFAULT '0 3 * * *',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_file TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def resolve_db_path() -> str:
    """Return the target SQLite DB path (arg > env > default workspace)."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    env_db = os.environ.get("HARVESTER_DB_PATH") or os.environ.get("HARVESTER_DB")
    if env_db:
        return env_db
    workspace = Path(os.environ.get("HARVESTER_WORKSPACE", "./data"))
    return str(workspace / "harvester.db")


def main() -> int:
    db_path = resolve_db_path()
    print(f"Target DB: {db_path}")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_DDL)

        before = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT provider_name, cron_expression FROM schedule_config"
            )
        }

        inserted: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []

        for provider_name, cron, config_file in _DEFAULT_SCHEDULES:
            old = before.get(provider_name)
            conn.execute(
                "INSERT INTO schedule_config "
                "(provider_name, cron_expression, enabled, config_file) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT(provider_name) DO UPDATE SET "
                "cron_expression = excluded.cron_expression, "
                "enabled = excluded.enabled, "
                "config_file = excluded.config_file, "
                "updated_at = datetime('now')",
                (provider_name, cron, config_file),
            )
            if old is None:
                inserted.append(f"{provider_name} -> {cron}")
            elif old != cron:
                updated.append(f"{provider_name}: {old} -> {cron}")
            else:
                unchanged.append(f"{provider_name} (already {cron})")

        conn.commit()

        # Providers in the DB but not in the default list are left untouched.
        untouched = sorted(set(before) - {s[0] for s in _DEFAULT_SCHEDULES})

        after = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT provider_name, cron_expression FROM schedule_config"
            )
        }

        print("\n== Result ==")
        if inserted:
            print(f"INSERTED ({len(inserted)}):")
            for line in inserted:
                print(f"  + {line}")
        if updated:
            print(f"UPDATED ({len(updated)}):")
            for line in updated:
                print(f"  ~ {line}")
        if unchanged:
            print(f"UNCHANGED ({len(unchanged)}):")
            for line in unchanged:
                print(f"  = {line}")
        if untouched:
            print(f"UNTOUCHED non-default providers ({len(untouched)}): "
                  f"{', '.join(untouched)}")
        if not (inserted or updated):
            print("No changes — schedule already up to date.")

        print("\n== Final schedule ==")
        for row in conn.execute(
            "SELECT provider_name, cron_expression, enabled, config_file "
            "FROM schedule_config ORDER BY provider_name"
        ):
            enabled = "on " if row[2] else "off"
            print(f"  [{enabled}] {row[0]:<12} {row[1]:<14} {row[3]}")
        print(f"\nTotal rows: {len(after)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
