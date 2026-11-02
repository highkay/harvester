#!/usr/bin/env python3

"""Read-only yield accounting CLI.

Reconciles the three harvest/push sources for a bounded time window without
writing anything to the database:

- ``run_records`` — runs, machine hours, ``valid_keys_found`` and the
  run-level ``total_keys_checked`` / ``links_total`` / ``materials_total``;
- ``run_new_keys`` — distinct ``key_hash`` values NEWLY harvested by the
  windowed runs (the unique-key口径 that ``valid_keys_found`` / pool
  ``added_count`` cannot provide);
- ``push_logs`` — pool placements, split by provenance:
  * ``organic_placements`` — ``added_count`` where ``push_logs.run_id`` joins
    a windowed ``run_records`` row (a natural run pushed its own keys);
  * ``manual_placements`` — ``added_count`` where ``push_logs.run_id`` does
    NOT join any ``run_records`` row (manual salvage / synthetic verification
    runs) and ``pushed_at`` is inside the window;
  * ``ignored`` — the sum of ``ignored_count`` across both placement kinds.

The two placement classes deliberately come apart so a dual-tier key can be
counted once as harvest (one ``run_new_keys`` row) yet twice as placement
(one per tier group in ``push_logs``).

Per-run JSON metrics written by ``web.runner.PipelineRunner`` (gather/verdict
counter snapshots plus the ``cache_hits``/``live_probes`` extension points)
are aggregated best-effort into each provider's ``metrics`` field when
``--workspace`` points at the runner workspace. Missing metrics files simply
yield zeros — the DB-derived fields above never depend on them.

Exit status: 0 on success; 2 for CLI/DB/schema errors (concise message to
stderr, never a traceback).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_DEFAULT_DAYS = (30, 14)
# Every table the audit needs for its core (non-metric) fields.
_REQUIRED_TABLES = ("run_records", "push_logs", "run_new_keys")


class AuditError(Exception):
    """A user-facing audit failure (missing DB / open error / missing schema)."""


# ---------------------------------------------------------------------------
# DB access (read-only)
# ---------------------------------------------------------------------------


def _open_readonly(path: str) -> sqlite3.Connection:
    """Open *path* read-only; raise :class:`AuditError` on any failure.

    ``Path.as_uri()`` keeps the mode=ro URI well-formed on Windows paths that
    otherwise contain backslashes and a drive-letter colon.
    """
    try:
        uri = str(Path(path).resolve().as_uri()) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise AuditError(f"cannot open database {path!r}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _require_schema(conn: sqlite3.Connection) -> None:
    present = _table_names(conn)
    missing = [t for t in _REQUIRED_TABLES if t not in present]
    if missing:
        raise AuditError(
            "database is missing required table(s): " + ", ".join(missing)
        )


def _run_record_extra_columns(conn: sqlite3.Connection) -> list[str]:
    """Column names of run_records, used to skip enrichment columns the audit
    does not strictly need (pre-migration databases may lack them)."""
    names = []
    for row in conn.execute("PRAGMA table_info(run_records)").fetchall():
        name = str(row["name"])
        if name in ("total_keys_checked", "links_total", "materials_total"):
            names.append(name)
    return names


def _provider_where(providers: list[str], column: str = "provider_name") -> str:
    """Exact-match provider filter; params are the provider names in order."""
    if not providers:
        return "1=1"
    return "(" + " OR ".join(f"{column} = ?" for _ in providers) + ")"


def _push_provider_where(providers: list[str]) -> tuple[str, list[Any]]:
    """Provider filter for push_logs that also accepts tier-suffixed rows.

    Capability-tier mappings are keyed ``{provider}:{tier}`` on the mapping
    table, and a lower layer can record the suffixed name in ``push_logs``;
    matching ``provider`` or ``provider:%`` keeps those rows attributable to
    their base provider. Returns ``(clause, params)``.
    """
    if not providers:
        return "1=1", []
    clauses = []
    params: list[Any] = []
    for p in providers:
        clauses.append("pl.provider_name = ? OR pl.provider_name LIKE ?")
        params.extend([p, p + ":%"])
    return "(" + " OR ".join(clauses) + ")", params


def _base_provider(name: object) -> str:
    """Strip a capability-tier suffix so ``doubao:seed`` attributes to ``doubao``."""
    return str(name).split(":", 1)[0]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _query_runs(
    conn: sqlite3.Connection, providers: list[str], days: int
) -> list[sqlite3.Row]:
    extra = _run_record_extra_columns(conn)
    extras_sql = "".join(f", SUM(COALESCE({c}, 0)) AS {c}" for c in extra)
    where = _provider_where(providers)
    sql = (
        "SELECT provider_name, "
        "COUNT(*) AS runs, "
        "SUM(COALESCE(duration_seconds, 0)) AS total_seconds, "
        "SUM(COALESCE(valid_keys_found, 0)) AS valid "
        f"{extras_sql} "
        "FROM run_records "
        f"WHERE {where} AND started_at >= datetime('now', ?) "
        "GROUP BY provider_name"
    )
    return conn.execute(sql, [*providers, f"-{int(days)} days"]).fetchall()


def _query_unique_new(
    conn: sqlite3.Connection, providers: list[str], days: int
) -> list[sqlite3.Row]:
    where = _provider_where(providers, column="rnk.provider_name")
    sql = (
        "SELECT rnk.provider_name, COUNT(DISTINCT rnk.key_hash) AS unique_new "
        "FROM run_new_keys AS rnk "
        "JOIN run_records AS rr ON rr.id = rnk.run_id "
        f"WHERE {where} AND rr.started_at >= datetime('now', ?) "
        "GROUP BY rnk.provider_name"
    )
    return conn.execute(sql, [*providers, f"-{int(days)} days"]).fetchall()


def _query_organic_placements(
    conn: sqlite3.Connection, providers: list[str], days: int
) -> list[sqlite3.Row]:
    clause, params = _push_provider_where(providers)
    sql = (
        "SELECT pl.provider_name, "
        "SUM(COALESCE(pl.added_count, 0)) AS organic, "
        "SUM(COALESCE(pl.ignored_count, 0)) AS organic_ignored "
        "FROM push_logs AS pl "
        "JOIN run_records AS rr ON rr.id = pl.run_id "
        f"WHERE {clause} AND rr.started_at >= datetime('now', ?) "
        "GROUP BY pl.provider_name"
    )
    return conn.execute(sql, [*params, f"-{int(days)} days"]).fetchall()


def _query_manual_placements(
    conn: sqlite3.Connection, providers: list[str], days: int
) -> list[sqlite3.Row]:
    clause, params = _push_provider_where(providers)
    sql = (
        "SELECT pl.provider_name, "
        "SUM(COALESCE(pl.added_count, 0)) AS manual, "
        "SUM(COALESCE(pl.ignored_count, 0)) AS manual_ignored "
        "FROM push_logs AS pl "
        "WHERE NOT EXISTS (SELECT 1 FROM run_records AS rr WHERE rr.id = pl.run_id) "
        f"AND {clause} AND pl.pushed_at >= datetime('now', ?) "
        "GROUP BY pl.provider_name"
    )
    return conn.execute(sql, [*params, f"-{int(days)} days"]).fetchall()


def _query_windowed_run_ids(
    conn: sqlite3.Connection, providers: list[str], days: int
) -> list[tuple[str, str]]:
    where = _provider_where(providers)
    sql = (
        "SELECT id, provider_name FROM run_records "
        f"WHERE {where} AND started_at >= datetime('now', ?)"
    )
    rows = conn.execute(sql, [*providers, f"-{int(days)} days"]).fetchall()
    return [(str(r["id"]), str(r["provider_name"])) for r in rows]


# ---------------------------------------------------------------------------
# Per-run metrics aggregation (best-effort)
# ---------------------------------------------------------------------------


def _resolve_workspace(workspace_arg: str | None) -> str:
    """Resolve the workspace the runner would have written run metrics to."""
    if workspace_arg:
        return str(Path(workspace_arg).resolve())
    env = os.environ.get("HARVESTER_WORKSPACE")
    if env:
        return str(Path(env).resolve())
    return str(Path("./data").resolve())


def _aggregate_metrics(
    workspace: str, run_ids_by_provider: dict[str, list[str]]
) -> dict[str, dict[str, Any]]:
    """Sum per-run JSON metrics per provider; empty dicts when absent."""
    result: dict[str, dict[str, Any]] = {}

    def _empty() -> dict[str, Any]:
        return {
            "cache_hits": 0,
            "live_probes": 0,
            "checked": 0,
            "verdict": {"valid": 0, "invalid": 0, "no_quota": 0,
                        "wait_check": 0, "material": 0, "total": 0},
            "gather": {},
            "runs_with_metrics": 0,
        }

    metrics_dir = Path(workspace) / "run-metrics"
    for provider, run_ids in run_ids_by_provider.items():
        agg = _empty()
        gather: dict[str, int] = {}
        for run_id in run_ids:
            path = metrics_dir / f"{run_id}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            agg["runs_with_metrics"] += 1
            for key in ("cache_hits", "live_probes", "checked"):
                try:
                    agg[key] += int(data.get(key, 0))
                except (TypeError, ValueError):
                    pass
            verdict = data.get("verdict") or {}
            for key in ("valid", "invalid", "no_quota", "wait_check", "material", "total"):
                try:
                    agg["verdict"][key] += int(verdict.get(key, 0))
                except (TypeError, ValueError):
                    pass
            for name, value in (data.get("gather") or {}).items():
                try:
                    gather[name] = gather.get(name, 0) + int(value)
                except (TypeError, ValueError):
                    pass
        agg["gather"] = gather
        result[provider] = agg
    return result


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _provider_record() -> dict[str, Any]:
    return {
        "runs": 0,
        "machine_hours": 0.0,
        "valid": 0,
        "total_keys_checked": 0,
        "links_total": 0,
        "materials_total": 0,
        "unique_new": 0,
        "organic_placements": 0,
        "manual_placements": 0,
        "ignored": 0,
    }


def _sum_field(records: dict[str, dict[str, Any]], field: str) -> Any:
    if field == "machine_hours":
        return round(sum(r["machine_hours"] for r in records.values()), 4)
    return sum(r[field] for r in records.values())


def audit(
    db_path: str,
    days: list[int],
    providers: list[str],
    workspace: str | None = None,
) -> dict[str, Any]:
    """Run the audit and return the report dict (raises :class:`AuditError`)."""
    conn = _open_readonly(db_path)
    try:
        _require_schema(conn)

        effective_providers = list(providers)
        if not effective_providers:
            effective_providers = _discover_providers(conn)

        resolved_workspace = _resolve_workspace(workspace)

        windows: dict[str, Any] = {}
        for d in days:
            records: dict[str, dict[str, Any]] = {
                p: _provider_record() for p in effective_providers
            }

            for row in _query_runs(conn, effective_providers, d):
                rec = records.setdefault(str(row["provider_name"]), _provider_record())
                rec["runs"] = int(row["runs"])
                rec["machine_hours"] = round(float(row["total_seconds"]) / 3600.0, 4)
                rec["valid"] = int(row["valid"])
                # sqlite3.Row is iterated by VALUE, so membership must test keys().
                columns = row.keys()
                if "total_keys_checked" in columns:
                    rec["total_keys_checked"] = int(row["total_keys_checked"])
                if "links_total" in columns:
                    rec["links_total"] = int(row["links_total"])
                if "materials_total" in columns:
                    rec["materials_total"] = int(row["materials_total"])

            for row in _query_unique_new(conn, effective_providers, d):
                rec = records.setdefault(
                    _base_provider(row["provider_name"]), _provider_record()
                )
                rec["unique_new"] += int(row["unique_new"])

            for row in _query_organic_placements(conn, effective_providers, d):
                rec = records.setdefault(
                    _base_provider(row["provider_name"]), _provider_record()
                )
                rec["organic_placements"] += int(row["organic"])
                rec["ignored"] += int(row["organic_ignored"])

            for row in _query_manual_placements(conn, effective_providers, d):
                rec = records.setdefault(
                    _base_provider(row["provider_name"]), _provider_record()
                )
                rec["manual_placements"] += int(row["manual"])
                rec["ignored"] += int(row["manual_ignored"])

            # Best-effort per-run metrics aggregation.
            run_ids_by_provider: dict[str, list[str]] = {
                p: [] for p in effective_providers
            }
            for run_id, provider in _query_windowed_run_ids(conn, effective_providers, d):
                run_ids_by_provider.setdefault(provider, []).append(run_id)
            metrics = _aggregate_metrics(resolved_workspace, run_ids_by_provider)
            for p, rec in records.items():
                rec["metrics"] = metrics.get(
                    p, {"cache_hits": 0, "live_probes": 0, "checked": 0,
                        "verdict": {}, "gather": {}, "runs_with_metrics": 0}
                )

            totals: dict[str, Any] = {field: _sum_field(records, field)
                                      for field in _provider_record()}
            windows[str(d)] = {
                "days": d,
                "providers": records,
                "totals": totals,
            }

        return {
            "schema_version": _SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "db_path": str(Path(db_path).resolve()),
            "workspace": resolved_workspace,
            "providers": effective_providers,
            "days": days,
            "windows": windows,
        }
    finally:
        conn.close()


def _discover_providers(conn: sqlite3.Connection) -> list[str]:
    names: set[str] = set()
    for table in _REQUIRED_TABLES:
        try:
            rows = conn.execute(
                f"SELECT DISTINCT provider_name FROM {table} WHERE provider_name IS NOT NULL"
            ).fetchall()
            names.update(str(r["provider_name"]) for r in rows)
        except sqlite3.Error:
            continue
    return sorted(names)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="audit_yield.py",
        description=(
            "Read-only reconciliation of unique harvest, pool placements and "
            "run metrics across 30/14-day windows."
        ),
    )
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument(
        "--days",
        default=",".join(str(d) for d in _DEFAULT_DAYS),
        help="comma-separated window sizes in days (default: 30,14)",
    )
    parser.add_argument(
        "--providers",
        default="",
        help="comma-separated provider names (default: all providers in the DB)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help=(
            "runner workspace holding run-metrics/*.json "
            "(default: $HARVESTER_WORKSPACE or ./data)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        days = [int(d) for d in str(args.days).split(",") if d.strip()]
    except ValueError as exc:
        parser.error(f"invalid --days value {args.days!r}: {exc}")
    if not days or any(d <= 0 for d in days):
        parser.error("--days must be one or more positive integers")
    args.days = days  # type: ignore[attr-defined]
    args.providers = [  # type: ignore[attr-defined]
        p.strip() for p in str(args.providers).split(",") if p.strip()
    ]
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        report = audit(args.db, args.days, args.providers, args.workspace)
    except AuditError as exc:
        print(f"audit_yield: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())