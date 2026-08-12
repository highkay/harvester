#!/usr/bin/env python3

"""Push service — reads valid-keys.txt after a scan completes and pushes results
to a gpt-load instance via HTTP API.

Called synchronously from a background thread (``PipelineRunner._on_completed``),
so all DB and HTTP access is synchronous (``sqlite3`` + ``requests``).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

from tools.logger import get_logger

from .crypto import decrypt_str

logger = get_logger("web.push")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default per-batch push cap when a mapping has no explicit max_size.
# Each mapping can override this (provider_group_mapping.max_size, default
# 10000) via the push-config UI / PUT /api/gpt-load/mappings/{provider}.
_DEFAULT_MAX_KEYS_PER_PUSH: int = 10000
_MAX_RETRIES: int = 3
_RETRY_BACKOFF_BASE: float = 1.0  # seconds
_RETRY_BACKOFF_MULTIPLIER: float = 3.0

# ---------------------------------------------------------------------------
# PushService
# ---------------------------------------------------------------------------


class PushService:
    """Service that pushes validated API keys to a gpt-load instance.

    Lifecycle:
        1. ``push_valid_keys(provider, run_id)`` — called from scan thread
        2. Looks up provider → gpt-load group mapping in DB
        3. Reads valid-keys.txt from workspace providers dir
        4. POSTs to gpt-load ``/api/keys/add-multiple`` (or add-async for >500 keys)
        5. Writes result to ``push_logs`` table
    """

    def __init__(self, db_path: str | None = None, workspace: str | None = None) -> None:
        if db_path is None:
            from .db import resolve_db_path

            db_path = resolve_db_path()
        if workspace is None:
            workspace = os.environ.get("HARVESTER_WORKSPACE", "./data")

        self._db_path: str = db_path
        self._workspace: Path = Path(workspace).resolve()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push_valid_keys(self, provider_name: str, run_id: str) -> None:
        """Read valid-keys.txt and push to gpt-load.

        Called by ``PipelineRunner._on_completed`` in a background thread.
        All errors are caught and logged; the method never raises.
        """
        try:
            logger.info(
                f"Push started: provider={provider_name} run_id={run_id}"
            )

            # 1. Resolve mapping
            mapping = self._get_mapping(provider_name)
            if mapping is None:
                logger.info(
                    f"No gpt-load mapping for provider '{provider_name}' — skipping push"
                )
                return

            # 2. Resolve gpt-load config
            config = self._get_gpt_load_config(mapping["gpt_load_config_id"])
            if config is None:
                err = (
                    f"gpt_load_config id={mapping['gpt_load_config_id']} "
                    f"not found for provider '{provider_name}'"
                )
                logger.error(err)
                self._write_push_log(
                    run_id=run_id,
                    provider_name=provider_name,
                    gpt_load_config_id=mapping["gpt_load_config_id"],
                    group_id=mapping["group_id"],
                    keys_count=0,
                    added_count=0,
                    ignored_count=0,
                    status="failed",
                    error_message=err,
                )
                return

            # 3. Decrypt auth_key
            try:
                auth_key = decrypt_str(config["auth_key_encrypted"])
            except ValueError as exc:
                err = (
                    f"Failed to decrypt auth_key for gpt_load_config "
                    f"id={mapping['gpt_load_config_id']}: {exc}"
                )
                logger.error(err)
                self._write_push_log(
                    run_id=run_id,
                    provider_name=provider_name,
                    gpt_load_config_id=mapping["gpt_load_config_id"],
                    group_id=mapping["group_id"],
                    keys_count=0,
                    added_count=0,
                    ignored_count=0,
                    status="failed",
                    error_message=err,
                )
                return

            base_url = config["base_url"].rstrip("/")

            # 4. Read valid keys
            keys = self._read_valid_keys(provider_name)
            if not keys:
                logger.info(
                    f"No valid keys found for provider '{provider_name}' — skipping push"
                )
                return

            keys_count = len(keys)

            # 5. Push to gpt-load (with retry)
            push_result = self._push_keys(
                base_url,
                auth_key,
                mapping["group_id"],
                keys,
                max_size=int(mapping.get("max_size") or 10000),
            )

            # 6. Write push log
            self._write_push_log(
                run_id=run_id,
                provider_name=provider_name,
                gpt_load_config_id=mapping["gpt_load_config_id"],
                group_id=mapping["group_id"],
                keys_count=keys_count,
                added_count=push_result["added_count"],
                ignored_count=push_result["ignored_count"],
                status=push_result["status"],
                error_message=push_result.get("error_message"),
            )

            logger.info(
                f"Push complete: provider={provider_name} run_id={run_id} "
                f"status={push_result['status']} "
                f"added={push_result['added_count']} "
                f"ignored={push_result['ignored_count']}"
            )

        except Exception as exc:
            logger.error(
                f"Push failed unexpectedly: provider={provider_name} "
                f"run_id={run_id} error={exc}"
            )
            # Best-effort log on unexpected failure
            try:
                self._write_push_log(
                    run_id=run_id,
                    provider_name=provider_name,
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

    def _get_mapping(self, provider_name: str) -> dict[str, Any] | None:
        """Look up provider_group_mapping by *provider_name*.

        Returns a dict with keys: gpt_load_config_id, group_id, group_name,
        max_size (per-batch key cap, default 10000).
        """
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT gpt_load_config_id, group_id, group_name, max_size "
                "FROM provider_group_mapping "
                "WHERE provider_name = ? AND enabled = 1",
                (provider_name,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def _get_gpt_load_config(self, config_id: int) -> dict[str, Any] | None:
        """Look up gpt_load_config by id.

        Returns a dict with keys: id, name, base_url, auth_key_encrypted.
        """
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT id, name, base_url, auth_key_encrypted "
                "FROM gpt_load_config WHERE id = ?",
                (config_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    def _read_valid_keys(self, provider_name: str) -> list[str]:
        """Read valid-keys.txt from the workspace providers directory.

        Path: ``{workspace}/providers/{provider_name}/valid-keys.txt``
        Returns a list of non-empty, deduplicated keys.
        """
        keys_path = self._workspace / "providers" / provider_name / "valid-keys.txt"
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

    def _push_keys(
        self,
        base_url: str,
        auth_key: str,
        group_id: int,
        keys: list[str],
        max_size: int = 10000,
    ) -> dict[str, Any]:
        """POST keys to gpt-load with retry on 5xx / network errors.

        Batches larger than *max_size* (per-mapping configuration, default
        10000) use the async import endpoint.

        Returns:
            dict with keys: ``status`` ("success"/"failed"), ``added_count``,
            ``ignored_count``, ``error_message`` (optional).
        """
        keys_count = len(keys)
        # Large batches use async endpoint
        if keys_count > max_size:
            endpoint = "/api/keys/add-async"
        else:
            endpoint = "/api/keys/add-multiple"

        url = f"{base_url}{endpoint}"
        body: dict[str, Any] = {
            "group_id": group_id,
            "keys_text": "\n".join(keys),
        }
        headers = {
            "Authorization": f"Bearer {auth_key}",
            "Content-Type": "application/json",
        }

        last_error: str | None = None

        for attempt in range(1 + _MAX_RETRIES):  # initial + retries
            try:
                resp = requests.post(url, json=body, headers=headers, timeout=30)

                if resp.status_code == 200:
                    data = resp.json()
                    inner = data.get("data", {})
                    return {
                        "status": "success",
                        "added_count": int(inner.get("added_count", 0)),
                        "ignored_count": int(inner.get("ignored_count", 0)),
                    }

                # 5xx or 4xx (except 429)
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code < 500 and resp.status_code != 429:
                    # Non-retryable 4xx
                    break

            except requests.ConnectionError as exc:
                last_error = f"ConnectionError: {exc}"
            except requests.Timeout as exc:
                last_error = f"Timeout: {exc}"
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                # Generic request exception — retry
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break  # Unexpected errors are not retried

            # Retry with backoff
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BACKOFF_BASE * (_RETRY_BACKOFF_MULTIPLIER ** attempt)
                logger.warning(
                    f"Push attempt {attempt + 1} failed ({last_error}), "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)

        return {
            "status": "failed",
            "added_count": 0,
            "ignored_count": 0,
            "error_message": last_error or "Unknown error",
        }

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

_push_service: PushService | None = None


def get_push_service() -> PushService:
    """Return the module-level PushService singleton."""
    global _push_service
    if _push_service is None:
        _push_service = PushService()
    return _push_service
