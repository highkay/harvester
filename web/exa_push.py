#!/usr/bin/env python3

"""Exa push service — reads exa valid-keys.txt after a scan completes and
pushes each validated key into a tavily-hikari style key pool via
``POST {base}/api/keys`` with ``provider: "exa"``.

Called synchronously from a background thread (``PipelineRunner._on_completed``),
so all DB and HTTP access is synchronous (``sqlite3`` + ``requests``).

Deliberately mirrors ``web.brave_push.py`` but stays a separate module: the
target pool is configured via its own env pair (``EXA_PROXY_BASE_URL`` /
``EXA_PROXY_AUTH_KEY``) and the provider gate is ``exa``. Env-driven, so
production keeps working (silent no-op) until the pool endpoint is configured.
As with the brave service, an empty ``EXA_PROXY_AUTH_KEY`` does NOT disable the
push — the production hikari instance runs ``DEV_OPEN_ADMIN=true``, where the
admin API needs no credential. The Bearer header is sent only when the env var
is non-empty, so a future switch to builtin/forward-auth needs no code change.
Keys are pushed as-is: the public Exa API-key format is unmeasured, so no shape
pre-filter is applied (any pre-filter would silently drop real keys — the
serpapi fixed-width lesson; validation already judged the key).

The hikari single-key endpoint is add-or-undelete and answers 201 for BOTH new
and existing keys, so created-vs-existing cannot be observed per item. Net-new
keys are therefore measured via the provider-scoped total delta
(``GET {base}/api/keys?provider=exa``) taken around the push loop; when the
delta cannot be read, accepted responses are counted as added and the
uncertainty is recorded in ``push_logs.error_message``. A 409
``provider_conflict`` (the same key already registered under another provider)
counts as ignored, not failed — the key is not lost.

The hikari batch endpoint (``POST /api/keys/batch``) is Tavily-only, so exa
must use the per-key endpoint.
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

logger = get_logger("web.exa_push")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROVIDER_NAME: str = "exa"
_TIMEOUT_SECONDS: int = 30
_MAX_RETRIES: int = 3
_RETRY_BACKOFF_BASE: float = 1.0  # seconds
_RETRY_BACKOFF_MULTIPLIER: float = 3.0
_UNAUTHORIZED_MESSAGE: str = "unauthorized (admin auth rejected)"
_TOTAL_UNAVAILABLE_MESSAGE: str = (
    "pool count unavailable; added counted from accepted responses"
)


# ---------------------------------------------------------------------------
# ExaPushService
# ---------------------------------------------------------------------------


class ExaPushService:
    """Service that pushes validated exa keys to a tavily-hikari key pool.

    Lifecycle:
        1. ``push_valid_keys(provider, run_id)`` — called from scan thread
        2. Skips unless provider == "exa" and ``EXA_PROXY_BASE_URL`` is set
        3. Reads valid-keys.txt from the workspace providers dir (deduped)
        4. Reads the provider-scoped total (net-new measurement anchor)
        5. POSTs each key to ``{base}/api/keys`` (``provider: exa``,
           retry on 429/5xx, fail-fast on 401/403)
        6. Measures net-new via the provider-scoped total delta; 409 counts
           as ignored
        7. Writes one result row to ``push_logs``
    """

    def __init__(
        self,
        base_url: str | None = None,
        auth_key: str | None = None,
        workspace: str | None = None,
        db_path: str | None = None,
    ) -> None:
        if base_url is None:
            base_url = os.environ.get("EXA_PROXY_BASE_URL", "")
        if auth_key is None:
            auth_key = os.environ.get("EXA_PROXY_AUTH_KEY", "")
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
        """Read valid-keys.txt and push each key to the hikari key pool.

        Called by ``PipelineRunner._on_completed`` in a background thread.
        All errors are caught and logged; the method never raises.
        """
        try:
            logger.info(
                f"Exa push started: provider={provider_name} run_id={run_id}"
            )

            # 1. Gate: only for exa runs with a configured pool base URL —
            # silent no-op otherwise. The auth key is OPTIONAL (DEV_OPEN_ADMIN
            # deployments ignore it); only an empty base URL disables the push.
            if provider_name != _PROVIDER_NAME or not self._base_url:
                logger.info(
                    f"Exa push skipped: provider={provider_name} "
                    f"(env not configured or not an exa run)"
                )
                return

            # 2. Idempotency guard — prevents _on_completed double-fire
            with self._lock:
                if run_id in self._seen_run_ids:
                    logger.info(f"Exa push skipped: run_id={run_id} already pushed")
                    return
                self._seen_run_ids.add(run_id)

            # 3. Read valid keys (deduped, no shape filter — see module docstring)
            keys = self._read_valid_keys(provider_name)
            if not keys:
                logger.info(
                    f"No valid keys found for provider '{provider_name}' "
                    f"— skipping push"
                )
                return

            keys_count = len(keys)

            url = f"{self._base_url.rstrip('/')}/api/keys"

            accepted = 0
            ignored_count = 0
            failures = 0
            errors: list[str] = []
            unauthorized = False
            added_count = 0

            total_before = self._fetch_provider_total(url)

            # 4. Push each key serially (the single-key endpoint; the pool's
            # batch endpoint is Tavily-only)
            for key in keys:
                outcome, error = self._push_key(url, key)
                if outcome == "accepted":
                    accepted += 1
                elif outcome == "ignored":
                    # 409 provider_conflict — already registered elsewhere
                    ignored_count += 1
                elif outcome == "unauthorized":
                    unauthorized = True
                    errors.append(error or _UNAUTHORIZED_MESSAGE)
                    break  # fail-fast: stop pushing remaining keys
                else:
                    failures += 1
                    if error:
                        errors.append(error)

            # 5. The single endpoint answers 201 for both new and existing
            # keys — measure net-new via the provider-scoped total delta.
            total_after = self._fetch_provider_total(url)
            if total_before is not None and total_after is not None:
                added_count = max(0, min(total_after - total_before, accepted))
                ignored_count += accepted - added_count
            else:
                added_count = accepted
                errors.append(_TOTAL_UNAVAILABLE_MESSAGE)

            # 6. Derive status
            if unauthorized:
                status = "failed"
            elif failures == 0:
                status = "success"
            elif failures == keys_count:
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
                f"Exa push complete: provider={provider_name} run_id={run_id} "
                f"status={status} added={added_count} ignored={ignored_count}"
            )

        except Exception as exc:
            logger.error(
                f"Exa push failed unexpectedly: provider={provider_name} "
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

    def _auth_headers(self) -> dict[str, str]:
        """Return request headers, adding Bearer auth only when configured."""
        headers = {"Content-Type": "application/json"}
        if self._auth_key:
            headers["Authorization"] = f"Bearer {self._auth_key}"
        return headers

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

    def _fetch_provider_total(self, url: str) -> int | None:
        """Read the provider-scoped key total (net-new measurement anchor)."""
        try:
            resp = requests.get(
                url,
                params={"provider": _PROVIDER_NAME, "per_page": 1},
                headers=self._auth_headers(),
                timeout=_TIMEOUT_SECONDS,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            total = data.get("total") if isinstance(data, dict) else None
            if isinstance(total, bool) or not isinstance(total, int):
                return None
            return int(total)
        except Exception:
            return None

    def _push_key(self, url: str, key: str) -> tuple[str, str | None]:
        """POST a single key to ``{base}/api/keys`` with retry on 429/5xx.

        Returns ``(outcome, error_message)`` where outcome is one of:
        ``"accepted"`` (201 — new or existing, the endpoint is
        add-or-undelete), ``"ignored"`` (409 provider_conflict — the key is
        already registered under another provider, so it is not lost),
        ``"unauthorized"`` (401/403, fail-fast), ``"failed"`` (other statuses,
        retry exhaustion, unexpected errors).
        """
        headers = self._auth_headers()
        body: dict[str, str] = {"api_key": key, "provider": _PROVIDER_NAME}

        last_error: str | None = None

        for attempt in range(1 + _MAX_RETRIES):  # initial + retries
            try:
                resp = requests.post(
                    url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS
                )

                if resp.status_code == 201:
                    return "accepted", None

                if resp.status_code == 409:
                    # provider_conflict: same key registered under another
                    # provider — counted as ignored, not as a failure.
                    return "ignored", None

                if resp.status_code in (401, 403):
                    return "unauthorized", _UNAUTHORIZED_MESSAGE

                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    # fall through to retry
                else:
                    # Other unexpected 4xx (400 empty body, 422 bad provider…) —
                    # non-retryable failure
                    error = self._response_error(resp)
                    last_error = f"HTTP {resp.status_code}: {error or resp.text[:200]}"
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
                delay = _RETRY_BACKOFF_BASE * (_RETRY_BACKOFF_MULTIPLIER**attempt)
                logger.warning(
                    f"Exa push attempt {attempt + 1} failed ({last_error}), "
                    f"retrying in {delay:.1f}s..."
                )
                time.sleep(delay)

        return "failed", last_error or "Unknown error"

    @staticmethod
    def _response_error(resp: Any) -> str | None:
        """Extract the ``error`` (+ ``detail``) fields from a JSON body."""
        try:
            data = resp.json()
        except Exception:
            return None
        if isinstance(data, dict):
            error = data.get("error")
            detail = data.get("detail")
            if error and detail:
                return f"{error}: {detail}"
            if error:
                return str(error)
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

_exa_push_service: ExaPushService | None = None


def get_exa_push_service() -> ExaPushService:
    """Return the module-level ExaPushService singleton."""
    global _exa_push_service
    if _exa_push_service is None:
        _exa_push_service = ExaPushService()
    return _exa_push_service
