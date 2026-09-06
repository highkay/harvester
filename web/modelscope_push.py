#!/usr/bin/env python3

"""ModelScope push service — reads modelscope valid-keys.txt after a scan completes
and pushes the validated keys to a gpt-load instance via POST
``{base}/api/keys/add-multiple`` (contract mirrors ``web.push.PushService._push_keys``).

Called synchronously from a background thread (``PipelineRunner._on_completed``),
so all DB and HTTP access is synchronous (``sqlite3`` + ``requests``).

Deliberately mirrors ``web.agnes_ai_push.py`` but stays a separate module: the
target instance is configured via its own env vars (``MODELSCOPE_LOAD_BASE_URL`` /
``MODELSCOPE_LOAD_GROUP_ID`` / ``MODELSCOPE_LOAD_AUTH_KEY``), keys are pre-filtered
to ``[0-9A-Za-z_-]{20,}`` tokens (ModelScope keys have no fixed prefix), batches
are chunked at 500 keys per POST, and the provider gate is ``modelscope``.
Env-driven with a default instance, so production keeps working until the target
is reconfigured.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import requests

from tools.logger import get_logger

logger = get_logger("web.modelscope_push")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL: str = "http://192.168.1.18:43001"
_DEFAULT_GROUP_ID: int = 13
_PROVIDER_NAME: str = "modelscope"
_TIMEOUT_SECONDS: int = 30
_MAX_RETRIES: int = 3
_RETRY_BACKOFF_BASE: float = 1.0  # seconds
_RETRY_BACKOFF_MULTIPLIER: float = 3.0
_MAX_KEYS_PER_POST: int = 500

# ModelScope tokens have no fixed prefix (MODELSCOPE_API_KEY / MODELSCOPE_SDK_TOKEN
# are bare alnum-ish strings); a length floor of 20 keeps noise out while staying
# prefix-independent. Other lines count as ignored.
_KEY_RE: re.Pattern[str] = re.compile(r"[0-9A-Za-z_-]{20,}")


# ---------------------------------------------------------------------------
# ModelScopePushService
# ---------------------------------------------------------------------------


class ModelScopePushService:
    """Service that pushes validated modelscope keys to a gpt-load instance.

    Lifecycle:
        1. ``push_valid_keys(provider, run_id)`` — called from scan thread
        2. Skips unless provider == "modelscope" and a base url is configured
        3. Reads valid-keys.txt from the workspace providers dir
        4. Pre-filters to ``[0-9A-Za-z_-]{20,}`` keys; other lines count as ignored
        5. POSTs keys in chunks of ``_MAX_KEYS_PER_POST`` to
           ``{base}/api/keys/add-multiple`` with retry on 429/5xx/network errors
        6. Writes one result row to ``push_logs``
    """

    def __init__(
        self,
        base_url: str | None = None,
        auth_key: str | None = None,
        group_id: int | None = None,
        workspace: str | None = None,
        db_path: str | None = None,
    ) -> None:
        if base_url is None:
            base_url = os.environ.get(
                "MODELSCOPE_LOAD_BASE_URL", _DEFAULT_BASE_URL
            )
        if auth_key is None:
            auth_key = os.environ.get("MODELSCOPE_LOAD_AUTH_KEY", "")
        if group_id is None:
            try:
                group_id = int(
                    os.environ.get(
                        "MODELSCOPE_LOAD_GROUP_ID", str(_DEFAULT_GROUP_ID)
                    )
                )
            except ValueError:
                group_id = _DEFAULT_GROUP_ID
        if workspace is None:
            workspace = os.environ.get("HARVESTER_WORKSPACE", "./data")
        if db_path is None:
            from .db import resolve_db_path

            db_path = resolve_db_path()

        self._base_url: str = base_url.rstrip("/") if base_url else ""
        self._auth_key: str = auth_key
        self._group_id: int = group_id
        self._workspace: Path = Path(workspace).resolve()
        self._db_path: str = db_path
        self._seen_run_ids: set[str] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push_valid_keys(self, provider_name: str, run_id: str) -> None:
        """Read valid-keys.txt and push valid keys to the gpt-load instance.

        Called by ``PipelineRunner._on_completed`` in a background thread.
        All errors are caught and logged; the method never raises.
        """
        try:
            logger.info(
                f"ModelScope push started: provider={provider_name} run_id={run_id}"
            )

            # 1. Gate: only for modelscope runs with a configured base url
            if provider_name != _PROVIDER_NAME or not self._base_url:
                logger.info(
                    f"ModelScope push skipped: provider={provider_name} "
                    f"(base url not configured or not a modelscope run)"
                )
                return

            # 2. Idempotency guard — prevents _on_completed double-fire
            with self._lock:
                if run_id in self._seen_run_ids:
                    logger.info(
                        f"ModelScope push skipped: run_id={run_id} already pushed"
                    )
                    return
                self._seen_run_ids.add(run_id)

            # 3. Read valid keys (strip, drop empty, dedup preserving order)
            lines = self._read_valid_keys(provider_name)
            keys_count = len(lines)

            # 4. Pre-filter: only [0-9A-Za-z_-]{20,} keys are pushed
            valid = [key for key in lines if _KEY_RE.fullmatch(key)]
            if not valid:
                logger.info(
                    f"No valid keys found for provider '{provider_name}' "
                    f"— skipping push"
                )
                return
            ignored_count = len(lines) - len(valid)

            # 5. Push keys in chunks of _MAX_KEYS_PER_POST
            added_count = 0
            chunk_oks = 0
            chunk_fails = 0
            unauthorized = False
            errors: list[str] = []

            for i in range(0, len(valid), _MAX_KEYS_PER_POST):
                chunk = valid[i : i + _MAX_KEYS_PER_POST]
                outcome, added, ignored, error = self._push_chunk(chunk)
                added_count += added
                ignored_count += ignored
                if outcome == "success":
                    chunk_oks += 1
                else:
                    chunk_fails += 1
                    if error:
                        errors.append(error)
                    if outcome == "unauthorized":
                        # Fail-fast: auth is broken, remaining chunks are futile
                        unauthorized = True
                        break

            # 6. Derive status
            if unauthorized or chunk_oks == 0:
                status = "failed"
            elif chunk_fails == 0:
                status = "success"
            else:
                status = "partial"

            error_message = "; ".join(errors[:3]) if errors else None

            # 7. Write ONE push_logs row
            self._write_push_log(
                run_id=run_id,
                provider_name=_PROVIDER_NAME,
                gpt_load_config_id=0,
                group_id=self._group_id,
                keys_count=keys_count,
                added_count=added_count,
                ignored_count=ignored_count,
                status=status,
                error_message=error_message,
            )

            logger.info(
                f"ModelScope push complete: provider={provider_name} "
                f"run_id={run_id} status={status} added={added_count} "
                f"ignored={ignored_count}"
            )

        except Exception as exc:
            logger.error(
                f"ModelScope push failed unexpectedly: provider={provider_name} "
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

    def _push_chunk(self, chunk: list[str]) -> tuple[str, int, int, str | None]:
        """POST one chunk of keys to ``{base}/api/keys/add-multiple``.

        Returns ``(outcome, added_count, ignored_count, error_message)`` where
        outcome is ``"success"`` (HTTP 200, or HTTP 400 whose body indicates the
        chunk contains no valid keys — a noop success), ``"unauthorized"``
        (HTTP 401 — fail-fast), or ``"failed"`` (non-retryable 4xx, retry
        exhaustion, unexpected errors). Retries on 429/5xx and network errors
        with exponential backoff (1s/3s/9s).
        """
        url = f"{self._base_url}/api/keys/add-multiple"
        body = {"group_id": self._group_id, "keys_text": "\n".join(chunk)}
        headers = {"Content-Type": "application/json"}
        if self._auth_key:
            headers["Authorization"] = f"Bearer {self._auth_key}"

        last_error: str | None = None

        for attempt in range(1 + _MAX_RETRIES):  # initial + retries
            try:
                resp = requests.post(
                    url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS
                )

                if resp.status_code == 200:
                    data = (resp.json() or {}).get("data") or {}
                    added = int(data.get("added_count", 0))
                    ignored = int(data.get("ignored_count", 0))
                    return "success", added, ignored, None

                if resp.status_code == 401:
                    last_error = f"HTTP 401: {resp.text[:200]}"
                    return "unauthorized", 0, 0, last_error

                if resp.status_code == 400 and self._is_no_valid_keys_body(resp):
                    # Server contract: a 400 whose body indicates the chunk
                    # contains no valid keys is a noop success, not a failure.
                    return "success", 0, 0, None

                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code < 500 and resp.status_code != 429:
                    # Other 4xx — non-retryable failure
                    return "failed", 0, 0, last_error

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
                delay = _RETRY_BACKOFF_BASE * (_RETRY_BACKOFF_MULTIPLIER**attempt)
                logger.warning(
                    f"ModelScope push attempt {attempt + 1} failed ({last_error}), "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)

        return "failed", 0, 0, last_error or "Unknown error"

    @staticmethod
    def _is_no_valid_keys_body(resp: requests.Response) -> bool:
        """True when a 400 body indicates the chunk contains no valid keys."""
        text = (resp.text or "").lower()
        if "no valid keys" in text or "no valid key" in text:
            return True
        try:
            data = resp.json() or {}
        except Exception:
            return False
        return not data or not (data.get("data") or {})

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

_modelscope_push_service: ModelScopePushService | None = None


def get_modelscope_push_service() -> ModelScopePushService:
    """Return the module-level ModelScopePushService singleton."""
    global _modelscope_push_service
    if _modelscope_push_service is None:
        _modelscope_push_service = ModelScopePushService()
    return _modelscope_push_service