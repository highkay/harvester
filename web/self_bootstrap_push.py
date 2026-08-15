#!/usr/bin/env python3

"""Self-bootstrap push service — reads the github provider's valid-keys.txt
after a scan completes and inserts each validated GitHub token into the
CURRENT instance's own ``github_tokens`` table (encrypted, dedup by hash) —
the exact table ``TokenService`` and ``PipelineRunner`` read to inject
credentials into scans, so this instance's own GitHub token pool grows with
every successful github scan (self-bootstrap).

Called synchronously from a background thread (``PipelineRunner._on_completed``),
so all DB access is synchronous (``sqlite3``) and credentials are hot-reloaded
with direct function calls.

Deliberately mirrors ``web.tavily_push.py`` but stays a separate module: the
target is the local ``github_tokens`` table instead of an external
TavilyProxyManager, keys are pre-filtered to GitHub token prefixes
(``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``/``github_pat_``/``gh_``), and
results land in the shared ``push_logs`` table with ``gpt_load_config_id=0`` /
``group_id=0``.

The ~60-line duplication of the read/write helpers is intentional (plan
mandate: do NOT extract a shared base class, do NOT touch ``web/push.py``),
which keeps the gpt-load flow regression-free and this module independently
reviewable — allow: SIZE_OK per `.omo/plans/github-self-bootstrap.md`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from tools.coordinator import update_credentials
from tools.logger import get_logger

from .crypto import decrypt_str, encrypt_str
from .crypto import _get_crypto

logger = get_logger("web.self_bootstrap_push")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_NAME: str = "github"
_KILL_SWITCH_ENV: str = "HARVESTER_SELF_BOOTSTRAP"
_KILL_SWITCH_ENABLED: str = "1"  # default: self-bootstrap ON unless env == "0"
_TOKEN_TYPE: str = "api"
_LABEL: str = "harvester-bootstrap"
_GH_PREFIXES: tuple[str, ...] = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "gh_",
)
_MAX_ERRORS_IN_LOG: int = 3

# ---------------------------------------------------------------------------
# SelfBootstrapPushService
# ---------------------------------------------------------------------------


class SelfBootstrapPushService:
    """Service that imports validated github keys into the local token store.

    Lifecycle:
        1. ``push_valid_keys(provider, run_id)`` — called from scan thread
        2. Skips unless provider == "github" and the kill-switch is enabled
        3. Reads valid-keys.txt from the workspace providers dir
        4. Pre-filters to GH-prefixed keys; other lines count as ignored
        5. INSERTs each key into ``github_tokens`` (encrypted, dedup by hash)
        6. Hot-reloads running credentials when at least one key was added
        7. Writes one result row to ``push_logs``
    """

    def __init__(
        self,
        db_path: str | None = None,
        workspace: str | None = None,
    ) -> None:
        if workspace is None:
            workspace = os.environ.get("HARVESTER_WORKSPACE", "./data")
        if db_path is None:
            from .db import resolve_db_path

            db_path = resolve_db_path()

        self._workspace: Path = Path(workspace).resolve()
        self._db_path: str = db_path
        self._seen_run_ids: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push_valid_keys(self, provider_name: str, run_id: str) -> None:
        """Read valid-keys.txt and import each GH-prefixed key into github_tokens.

        Called by ``PipelineRunner._on_completed`` in a background thread.
        All errors are caught and logged; the method never raises.
        """
        try:
            logger.info(
                f"Self-bootstrap started: provider={provider_name} run_id={run_id}"
            )

            # 1. Gate: only github runs with the kill-switch enabled — silent no-op
            if (
                provider_name != _PROVIDER_NAME
                or os.environ.get(_KILL_SWITCH_ENV, _KILL_SWITCH_ENABLED) == "0"
            ):
                logger.info(
                    f"Self-bootstrap skipped: provider={provider_name} "
                    f"(kill-switch disabled or not a github run)"
                )
                return

            # 2. Idempotency guard — prevents _on_completed double-fire
            with self._lock:
                if run_id in self._seen_run_ids:
                    logger.info(
                        f"Self-bootstrap skipped: run_id={run_id} already pushed"
                    )
                    return
                self._seen_run_ids.add(run_id)

            # 3. Read valid keys
            keys = self._read_valid_keys(provider_name)
            if not keys:
                logger.info(
                    f"No valid keys found for provider '{provider_name}' "
                    f"— skipping self-bootstrap"
                )
                return

            keys_count = len(keys)

            # 4. Pre-filter: only GH-prefixed keys are imported
            gh_keys: list[str] = []
            ignored_count = 0
            for key in keys:
                if key.startswith(_GH_PREFIXES):
                    gh_keys.append(key)
                else:
                    ignored_count += 1

            # 5. Insert each key into github_tokens (encrypted, dedup by hash)
            added_count = 0
            failures = 0
            errors: list[str] = []

            conn = sqlite3.connect(self._db_path)
            try:
                for key in gh_keys:
                    try:
                        conn.execute(
                            "INSERT INTO github_tokens "
                            "(token_type, token_encrypted, token_hash, label) "
                            "VALUES (?, ?, ?, ?)",
                            (
                                _TOKEN_TYPE,
                                encrypt_str(key),
                                _get_crypto().hash_token(key),
                                _LABEL,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        ignored_count += 1  # duplicate via UNIQUE token_hash
                    except Exception as exc:
                        failures += 1
                        errors.append(f"{type(exc).__name__}: {exc}")
                    else:
                        added_count += 1
                conn.commit()
            finally:
                conn.close()

            # 6. Hot-reload running credentials when at least one key was added
            if added_count > 0:
                self._hot_reload()

            # 7. Derive status
            if failures == 0:
                status = "success"
            elif added_count == 0:
                status = "failed"
            else:
                status = "partial"

            error_message = (
                "; ".join(errors[:_MAX_ERRORS_IN_LOG]) if errors else None
            )

            # 8. Write ONE push_logs row
            self._write_push_log(
                run_id=run_id,
                provider_name=_PROVIDER_NAME,
                gpt_load_config_id=0,
                group_id=0,
                keys_count=keys_count,
                added_count=added_count,
                ignored_count=ignored_count,
                status=status,
                error_message=error_message,
            )

            logger.info(
                f"Self-bootstrap complete: provider={provider_name} "
                f"run_id={run_id} status={status} added={added_count} "
                f"ignored={ignored_count}"
            )

        except Exception as exc:
            logger.error(
                f"Self-bootstrap failed unexpectedly: provider={provider_name} "
                f"run_id={run_id} error={exc}"
            )
            # Best-effort log on unexpected failure
            try:
                self._write_push_log(
                    run_id=run_id,
                    provider_name=_PROVIDER_NAME,
                    gpt_load_config_id=0,
                    group_id=0,
                    keys_count=0,
                    added_count=0,
                    ignored_count=0,
                    status="failed",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_valid_keys(self, provider_name: str) -> list[str]:
        """Read valid-keys.txt from the workspace providers directory.

        Path: ``{workspace}/providers/{provider_name}/valid-keys.txt``
        Returns a list of non-empty, deduplicated keys.
        """
        keys_path = (
            self._workspace / "providers" / provider_name / "valid-keys.txt"
        )
        if not keys_path.exists():
            logger.info(f"valid-keys.txt not found: {keys_path}")
            return []

        try:
            raw = keys_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Failed to read {keys_path}: {exc}")
            return []

        # Split, strip whitespace, drop empty lines, dedup
        seen: set[str] = set()
        keys: list[str] = []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                keys.append(stripped)
        return keys

    def _hot_reload(self) -> None:
        """Read all enabled tokens/sessions from DB and push to ResourceManager.

        Mirrors ``TokenService._hot_reload`` (synchronous).  Silently skips
        when the ResourceManager has not been initialised
        (``try/except RuntimeError``), because hot-reload is only meaningful
        for a running pipeline.  DB persistence is always active.
        """
        conn = sqlite3.connect(self._db_path)
        try:
            api_rows = conn.execute(
                "SELECT token_encrypted FROM github_tokens "
                "WHERE token_type = 'api' AND enabled = 1"
            ).fetchall()
            sess_rows = conn.execute(
                "SELECT token_encrypted FROM github_tokens "
                "WHERE token_type = 'session' AND enabled = 1"
            ).fetchall()
        finally:
            conn.close()

        tokens = [decrypt_str(row[0]) for row in api_rows]
        sessions = [decrypt_str(row[0]) for row in sess_rows]

        try:
            update_credentials(sessions, tokens)
        except RuntimeError:
            logger.debug(
                "Hot-reload skipped — ResourceManager not initialised "
                f"(tokens={len(tokens)} sessions={len(sessions)} persisted)"
            )

    def _write_push_log(
        self,
        run_id: str,
        provider_name: str,
        gpt_load_config_id: int,
        group_id: int,
        keys_count: int,
        added_count: int,
        ignored_count: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Insert a row into the push_logs table (synchronous)."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO push_logs
                   (run_id, provider_name, gpt_load_config_id, group_id,
                    keys_count, added_count, ignored_count, status, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    provider_name,
                    gpt_load_config_id,
                    group_id,
                    keys_count,
                    added_count,
                    ignored_count,
                    status,
                    error_message,
                ),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_self_bootstrap_push_service: SelfBootstrapPushService | None = None


def get_self_bootstrap_push_service() -> SelfBootstrapPushService:
    """Return the module-level SelfBootstrapPushService singleton."""
    global _self_bootstrap_push_service
    if _self_bootstrap_push_service is None:
        _self_bootstrap_push_service = SelfBootstrapPushService()
    return _self_bootstrap_push_service
