#!/usr/bin/env python3

"""Tavily push service — reads tavily valid-keys.txt after a scan completes and
pushes each validated key to a TavilyProxyManager instance via HTTP API.

Called synchronously from a background thread (``PipelineRunner._on_completed``),
so all DB and HTTP access is synchronous (``sqlite3`` + ``requests``).

Deliberately mirrors ``web.push.PushService`` (gpt-load flow) but stays a
separate module: the TavilyProxyManager API is per-key ``POST /api/keys`` (no
batch endpoint), auth comes from ``TAVILY_PROXY_BASE_URL`` /
``TAVILY_PROXY_AUTH_KEY`` env vars, and results land in the shared
``push_logs`` table with ``gpt_load_config_id=0`` / ``group_id=0``.

The ~60-line duplication of the read/write helpers is intentional (plan
mandate: do NOT extract a shared base class, do NOT touch ``web/push.py``),
which keeps the gpt-load flow regression-free and this module independently
reviewable — allow: SIZE_OK per `.omo/plans/tavily-key-push.md`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import requests

from tools.logger import get_logger

logger = get_logger("web.tavily_push")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_NAME: str = "tavily"
_TIMEOUT_SECONDS: int = 30
_MAX_RETRIES: int = 3
_RETRY_BACKOFF_BASE: float = 1.0  # seconds
_RETRY_BACKOFF_MULTIPLIER: float = 3.0

_UNAUTHORIZED_MESSAGE: str = "unauthorized (invalid master key)"

# ---------------------------------------------------------------------------
# TavilyPushService
# ---------------------------------------------------------------------------


class TavilyPushService:
    """Service that pushes validated tavily keys to a TavilyProxyManager.

    Lifecycle:
        1. ``push_valid_keys(provider, run_id)`` — called from scan thread
        2. Skips unless provider == "tavily" and env is configured
        3. Reads valid-keys.txt from the workspace providers dir
        4. Pre-filters to ``tvly-`` prefixed keys; other lines count as ignored
        5. POSTs each key to ``{base}/api/keys`` with retry on 429/5xx
        6. Writes one result row to ``push_logs``
    """

    def __init__(
        self,
        base_url: str | None = None,
        auth_key: str | None = None,
        workspace: str | None = None,
        db_path: str | None = None,
    ) -> None:
        if base_url is None:
            base_url = os.environ.get("TAVILY_PROXY_BASE_URL", "")
        if auth_key is None:
            auth_key = os.environ.get("TAVILY_PROXY_AUTH_KEY", "")
        if workspace is None:
            workspace = os.environ.get("HARVESTER_WORKSPACE", "./data")
        if db_path is None:
            from .db import resolve_db_path

            db_path = resolve_db_path()

        self._base_url: str = base_url
        self._auth_key: str = auth_key
        self._workspace: Path = Path(workspace).resolve()
        self._db_path: str = db_path
        self._seen_run_ids: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push_valid_keys(self, provider_name: str, run_id: str) -> None:
        """Read valid-keys.txt and push each tvly- key to TavilyProxyManager.

        Called by ``PipelineRunner._on_completed`` in a background thread.
        All errors are caught and logged; the method never raises.
        """
        try:
            logger.info(
                f"Tavily push started: provider={provider_name} run_id={run_id}"
            )

            # 1. Gate: only for tavily runs with configured env — silent no-op
            if (
                provider_name != _PROVIDER_NAME
                or not self._base_url
                or not self._auth_key
            ):
                logger.info(
                    f"Tavily push skipped: provider={provider_name} "
                    f"(env not configured or not a tavily run)"
                )
                return

            # 2. Idempotency guard — prevents _on_completed double-fire
            with self._lock:
                if run_id in self._seen_run_ids:
                    logger.info(
                        f"Tavily push skipped: run_id={run_id} already pushed"
                    )
                    return
                self._seen_run_ids.add(run_id)

            # 3. Read valid keys
            keys = self._read_valid_keys(provider_name)
            if not keys:
                logger.info(
                    f"No valid keys found for provider '{provider_name}' "
                    f"— skipping push"
                )
                return

            keys_count = len(keys)

            # 4. Pre-filter: only tvly- prefixed keys are pushed
            tvly_keys: list[str] = []
            ignored_count = 0
            for key in keys:
                if key.startswith("tvly-"):
                    tvly_keys.append(key)
                else:
                    ignored_count += 1

            url = f"{self._base_url.rstrip('/')}/api/keys"

            # 5. Push each key serially
            added_count = 0
            failures = 0
            errors: list[str] = []
            unauthorized = False

            for key in tvly_keys:
                outcome, error = self._push_key(url, key)
                if outcome == "added":
                    added_count += 1
                elif outcome == "ignored":
                    ignored_count += 1
                elif outcome == "unauthorized":
                    unauthorized = True
                    errors.append(error or _UNAUTHORIZED_MESSAGE)
                    break  # fail-fast: stop pushing remaining keys
                else:
                    failures += 1
                    if error:
                        errors.append(error)

            # 6. Derive status
            if unauthorized:
                status = "failed"
            elif failures == 0:
                status = "success"
            elif failures == len(tvly_keys):
                status = "failed"
            else:
                status = "partial"

            error_message = "; ".join(errors[:3]) if errors else None

            # 7. Write ONE push_logs row
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
                f"Tavily push complete: provider={provider_name} run_id={run_id} "
                f"status={status} added={added_count} ignored={ignored_count}"
            )

        except Exception as exc:
            logger.error(
                f"Tavily push failed unexpectedly: provider={provider_name} "
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

    def _push_key(self, url: str, key: str) -> tuple[str, str | None]:
        """POST a single key to ``{base}/api/keys`` with retry on 429/5xx.

        Returns ``(outcome, error_message)`` where outcome is one of:
        ``"added"`` (200), ``"ignored"`` (400 create_failed / invalid_key_format),
        ``"unauthorized"`` (401, fail-fast), ``"failed"`` (other 4xx, retry
        exhaustion, unexpected errors).
        """
        headers = {
            "Authorization": f"Bearer {self._auth_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, str] = {"key": key, "alias": "harvester"}

        last_error: str | None = None

        for attempt in range(1 + _MAX_RETRIES):  # initial + retries
            try:
                resp = requests.post(
                    url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS
                )

                if resp.status_code == 200:
                    return "added", None

                if resp.status_code == 401:
                    return "unauthorized", _UNAUTHORIZED_MESSAGE

                if resp.status_code == 400:
                    error = self._response_error(resp)
                    if error in ("create_failed", "invalid_key_format"):
                        return "ignored", None
                    last_error = f"HTTP 400: {error or resp.text[:200]}"
                    return "failed", last_error

                if resp.status_code in (402, 403, 404):
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    return "failed", last_error

                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    # fall through to retry
                else:
                    # Other unexpected 4xx — non-retryable failure
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    return "failed", last_error

            except requests.ConnectionError as exc:
                last_error = f"ConnectionError: {exc}"
            except requests.Timeout as exc:
                last_error = f"Timeout: {exc}"
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break  # Unexpected errors are not retried

            # Retry with backoff
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BACKOFF_BASE * (_RETRY_BACKOFF_MULTIPLIER ** attempt)
                logger.warning(
                    f"Tavily push attempt {attempt + 1} failed ({last_error}), "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)

        return "failed", last_error or "Unknown error"

    @staticmethod
    def _response_error(resp: Any) -> str | None:
        """Extract the ``error`` field from a JSON response body, if present."""
        try:
            data = resp.json()
        except Exception:
            return None
        if isinstance(data, dict):
            return data.get("error")
        return None

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

_tavily_push_service: TavilyPushService | None = None


def get_tavily_push_service() -> TavilyPushService:
    """Return the module-level TavilyPushService singleton."""
    global _tavily_push_service
    if _tavily_push_service is None:
        _tavily_push_service = TavilyPushService()
    return _tavily_push_service
