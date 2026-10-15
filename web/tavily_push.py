#!/usr/bin/env python3

"""Tavily push service — reads tavily valid-keys.txt after a scan completes and
pushes each validated key into a tavily-hikari style key pool via
``POST {base}/api/keys/batch``.

Called synchronously from a background thread (``PipelineRunner._on_completed``),
so all DB and HTTP access is synchronous (``sqlite3`` + ``requests``).

Deliberately mirrors ``web.push.PushService`` (gpt-load flow) but stays a
separate module: the pool exposes the hikari batch endpoint (Tavily-only per
its API reference), auth comes from ``TAVILY_PROXY_BASE_URL`` /
``TAVILY_PROXY_AUTH_KEY`` env vars, and results land in the shared ``push_logs``
table with ``gpt_load_config_id=0`` / ``group_id=0``.

Batch accounting folds the per-item ``status`` values — ``created`` /
``undeleted`` count as added, ``existed`` / ``duplicate_in_input`` as ignored,
``failed`` (or an unknown status) as a failure carrying the item's ``error``
text. Chunks of ``_BATCH_CHUNK_SIZE`` stay under the server's 1000-item limit;
resubmitting a chunk is idempotent because the endpoint is add-or-undelete.

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
_BATCH_CHUNK_SIZE: int = 500  # server-side API_KEYS_BATCH_LIMIT is 1000

_UNAUTHORIZED_MESSAGE: str = "unauthorized (admin auth rejected)"

# ---------------------------------------------------------------------------
# TavilyPushService
# ---------------------------------------------------------------------------


class TavilyPushService:
    """Service that pushes validated tavily keys to a tavily-hikari key pool.

    Lifecycle:
        1. ``push_valid_keys(provider, run_id)`` — called from scan thread
        2. Skips unless provider == "tavily" and env is configured
        3. Reads valid-keys.txt from the workspace providers dir
        4. Pre-filters to ``tvly-`` prefixed keys; other lines count as ignored
        5. POSTs ``api_keys`` chunks to ``{base}/api/keys/batch`` (retry 429/5xx)
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
        """Read valid-keys.txt and push tvly- keys to the configured key pool.

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

            url = f"{self._base_url.rstrip('/')}/api/keys/batch"

            added_count = 0
            failures = 0
            errors: list[str] = []
            unauthorized = False

            # 5. Push keys in batch chunks (server-side limit: 1000 items)
            for start in range(0, len(tvly_keys), _BATCH_CHUNK_SIZE):
                chunk = tvly_keys[start : start + _BATCH_CHUNK_SIZE]
                outcome, data, error = self._push_chunk(url, chunk)

                if outcome == "unauthorized":
                    unauthorized = True
                    errors.append(error or _UNAUTHORIZED_MESSAGE)
                    break  # fail-fast: stop pushing remaining chunks

                if outcome != "ok" or data is None:
                    failures += len(chunk)
                    if error:
                        errors.append(error)
                    continue

                chunk_added, chunk_ignored, chunk_failed, chunk_errors = (
                    self._tally_results(data)
                )
                added_count += chunk_added
                ignored_count += chunk_ignored
                failures += chunk_failed
                errors.extend(chunk_errors)

                accounted = chunk_added + chunk_ignored + chunk_failed
                if accounted < len(chunk):
                    missing = len(chunk) - accounted
                    failures += missing
                    errors.append(f"{missing} batch item(s) missing from response")

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

    def _push_chunk(
        self, url: str, keys: list[str]
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        """POST one ``api_keys`` chunk to ``{base}/api/keys/batch``.

        Retries on 429/5xx and transport errors. Returns
        ``(outcome, data, error_message)`` with outcome ``"ok"`` (HTTP 200 with
        a batch envelope), ``"unauthorized"`` (401/403, fail-fast) or
        ``"failed"`` (everything else, including retry exhaustion).
        """
        headers = {
            "Authorization": f"Bearer {self._auth_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {"api_keys": keys}

        last_error: str | None = None

        for attempt in range(1 + _MAX_RETRIES):  # initial + retries
            try:
                resp = requests.post(
                    url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS
                )

                if resp.status_code == 200:
                    data = self._batch_payload(resp)
                    if data is None:
                        return (
                            "failed",
                            None,
                            f"HTTP 200: unparseable batch response: "
                            f"{resp.text[:200]}",
                        )
                    return "ok", data, None

                if resp.status_code in (401, 403):
                    return "unauthorized", None, _UNAUTHORIZED_MESSAGE

                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    # fall through to retry
                else:
                    # Other unexpected 4xx (400 too_many_items, 409 conflict…) —
                    # non-retryable failure
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    return "failed", None, last_error

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

        return "failed", None, last_error or "Unknown error"

    @staticmethod
    def _batch_payload(resp: Any) -> dict[str, Any] | None:
        """Return the response body when it looks like a batch envelope."""
        try:
            data = resp.json()
        except Exception:
            return None
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data
        return None

    @staticmethod
    def _tally_results(data: dict[str, Any]) -> tuple[int, int, int, list[str]]:
        """Fold per-item batch statuses into (added, ignored, failed, errors)."""
        added = 0
        ignored = 0
        failures = 0
        errors: list[str] = []
        for item in data["results"]:
            status = item.get("status") if isinstance(item, dict) else None
            if status in ("created", "undeleted"):
                added += 1
            elif status in ("existed", "duplicate_in_input"):
                ignored += 1
            else:
                failures += 1
                message = item.get("error") if isinstance(item, dict) else None
                errors.append(
                    str(message) if message else f"batch item status={status!r}"
                )
        return added, ignored, failures, errors

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