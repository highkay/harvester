#!/usr/bin/env python3

"""Token service — CRUD operations for GitHub tokens with encryption and hot-reload.

``TokenService`` manages the ``github_tokens`` table: encrypting tokens at rest,
masking in responses, and synchronising enabled credentials with the running
pipeline via :func:`tools.coordinator.update_credentials`.

Module-level singleton via :func:`get_token_service` (``web/deps.py`` style).
"""

from __future__ import annotations

import os
import sqlite3
from typing import Final

import aiosqlite
import requests

from tools.coordinator import update_credentials
from tools.logger import get_logger

from .crypto import decrypt_str, encrypt_str
from .crypto import _get_crypto
from .db import get_db
from .models import mask_token

logger = get_logger("web")

# Add-time GitHub /user probe timeout, and the response header that carries a
# fine-grained/expiring PAT's UTC expiry (classic PATs omit it → expires_at NULL).
_GH_VALIDATE_TIMEOUT: Final[float] = 15.0
_GH_EXPIRY_HEADER: Final[str] = "github-authentication-token-expiration"


def _gh_api_proxies() -> dict[str, str] | None:
    """Return a requests ``proxies`` mapping from ``HARVESTER_PROXY``.

    The env var is comma-separated (the runner's rotation list); token
    validation dials the FIRST entry. Empty/unset → ``None`` (direct).
    """
    raw = os.environ.get("HARVESTER_PROXY", "").strip()
    if not raw:
        return None
    first = raw.split(",", 1)[0].strip()
    if not first:
        return None
    return {"http": first, "https": first}


class TokenService:
    """Async CRUD service for GitHub tokens stored in SQLite."""

    def __init__(self, db_path: str) -> None:
        self._db_path: Final[str] = db_path

    # ------------------------------------------------------------------
    # Add-time GitHub validation
    # ------------------------------------------------------------------

    def _validate_token_gh(self, token_value: str) -> tuple[str, str | None]:
        """Probe GitHub ``GET /user`` with *token_value* as a Bearer token.

        Returns ``("valid", expires_at|None)`` on HTTP 200,
        ``("invalid", None)`` on 401/403, and ``("unknown", None)`` on any
        transport or unexpected-status outcome. **Never raises** — token
        management must not break on a network blip.
        """
        try:
            response = requests.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {token_value}",
                    "Accept": "application/vnd.github+json",
                },
                proxies=_gh_api_proxies(),
                timeout=_GH_VALIDATE_TIMEOUT,
            )
        except requests.exceptions.RequestException:
            return ("unknown", None)
        except Exception:
            # Defensive: a mock/adapter problem is still "unknown", never a
            # management-blocking exception.
            return ("unknown", None)

        if response.status_code == 200:
            return ("valid", response.headers.get(_GH_EXPIRY_HEADER))
        if response.status_code in (401, 403):
            return ("invalid", None)
        return ("unknown", None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add_token(
        self, token_type: str, token_value: str, label: str = ""
    ) -> dict:
        """Encrypt *token_value*, store it, and hot-reload credentials.

        API tokens are validated against GitHub first: a 401/403 rejects the
        add with ``ValueError`` (never stored); a 200 stores the captured
        ``expires_at``; an unknown (transport) outcome stores with NULL. Session
        cookies are not API tokens and skip the probe.

        Returns a dict with ``id``, ``token_masked`` and ``expires_at``.
        Raises ``ValueError`` when *token_value* is rejected or duplicates an
        existing hash.
        """
        if token_type == "api":
            status, token_expiry = self._validate_token_gh(token_value)
            if status == "invalid":
                raise ValueError(
                    "Token rejected by GitHub (invalid or forbidden): "
                    f"{mask_token(token_value)}"
                )
            expires_at = token_expiry if status == "valid" else None
        else:
            expires_at = None

        encrypted = encrypt_str(token_value)
        token_hash = _get_crypto().hash_token(token_value)

        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "INSERT INTO github_tokens "
                "(token_type, token_encrypted, token_hash, label, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_type, encrypted, token_hash, label, expires_at),
            )
            await db.commit()
            row_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Duplicate token: a token with the same hash already exists"
            ) from exc
        finally:
            await db.close()

        await self._hot_reload()

        masked = mask_token(token_value)
        logger.info(f"Token added id={row_id} type={token_type} masked={masked}")
        return {"id": row_id, "token_masked": masked, "expires_at": expires_at}

    async def add_tokens_bulk(
        self, token_type: str, tokens_text: str
    ) -> dict:
        """Import tokens from a newline-separated string.

        API-token lines are validated against GitHub one by one: 401/403 lines
        are rejected into ``dead`` (masked, never stored); 200 lines store their
        expiry; unknown outcomes store NULL. Session lines skip the probe.

        Returns ``{"added", "skipped_duplicates", "errors", "dead"}``.
        Hot-reload is called once after all successful inserts.
        """
        added = 0
        skipped = 0
        errors: list[str] = []
        dead: list[str] = []

        already_seen: set[str] = set()

        db = await get_db(self._db_path)
        try:
            lines = [ln.strip() for ln in tokens_text.splitlines()]
            for line in lines:
                if not line:
                    continue
                if line in already_seen:
                    skipped += 1
                    continue
                already_seen.add(line)

                if token_type == "api":
                    status, token_expiry = self._validate_token_gh(line)
                    if status == "invalid":
                        dead.append(mask_token(line))
                        continue
                    expires_at = token_expiry if status == "valid" else None
                else:
                    expires_at = None

                try:
                    encrypted = encrypt_str(line)
                    token_hash = _get_crypto().hash_token(line)
                    await db.execute(
                        "INSERT INTO github_tokens "
                        "(token_type, token_encrypted, token_hash, label, expires_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (token_type, encrypted, token_hash, "", expires_at),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    skipped += 1
                except Exception as exc:
                    errors.append(str(exc))

            await db.commit()
        finally:
            await db.close()

        if added > 0:
            await self._hot_reload()

        logger.info(
            f"Bulk import: added={added} skipped={skipped} "
            f"dead={len(dead)} errors={len(errors)}"
        )
        return {
            "added": added,
            "skipped_duplicates": skipped,
            "errors": errors,
            "dead": dead,
        }

    async def list_tokens(self) -> list[dict]:
        """Return all tokens with masked values — plaintext is never returned."""
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "SELECT id, token_type, token_encrypted, label, enabled, "
                "created_at, expires_at FROM github_tokens ORDER BY id"
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        result: list[dict] = []
        for row in rows:
            decrypted = decrypt_str(row["token_encrypted"])
            result.append({
                "id": row["id"],
                "token_type": row["token_type"],
                "token_masked": mask_token(decrypted),
                "label": row["label"],
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            })
        return result

    async def delete_token(self, token_id: int) -> bool:
        """Delete a token by *token_id* and hot-reload credentials.

        Returns ``True`` if a row was deleted, ``False`` otherwise.
        """
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "DELETE FROM github_tokens WHERE id = ?", (token_id,)
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        finally:
            await db.close()

        if deleted:
            await self._hot_reload()
            logger.info(f"Token deleted id={token_id}")
        return deleted

    async def set_token_enabled(self, token_id: int, enabled: bool) -> bool:
        """Enable or disable a token by *token_id* and hot-reload credentials.

        Returns ``True`` if a row was updated, ``False`` otherwise.
        """
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "UPDATE github_tokens SET enabled = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (int(enabled), token_id),
            )
            await db.commit()
            updated = cursor.rowcount > 0
        finally:
            await db.close()

        if updated:
            await self._hot_reload()
            logger.info(f"Token id={token_id} enabled={enabled}")
        return updated

    async def get_enabled_tokens(self) -> list[str]:
        """Return plaintext values of all enabled API-type tokens.

        This is the **only** method that returns decrypted plaintext tokens;
        it is designed for :class:`PipelineRunner` to inject into temporary
        YAML configurations.
        """
        db = await get_db(self._db_path)
        try:
            cursor = await db.execute(
                "SELECT token_encrypted FROM github_tokens "
                "WHERE token_type = 'api' AND enabled = 1"
            )
            rows = await cursor.fetchall()
        finally:
            await db.close()

        return [decrypt_str(row["token_encrypted"]) for row in rows]

    async def get_stats(self) -> dict:
        """Return token counts from the database.

        The result is ``{"total": N, "enabled": N, "api_count": N,
        "session_count": N, "with_expiry": N}``.  These values come directly
        from the DB and do **not** depend on the runtime ``Credentials`` state.
        ``with_expiry`` counts tokens carrying a stored ``expires_at`` (the
        per-token values live in :meth:`list_tokens`).
        """
        db = await get_db(self._db_path)
        try:
            row = await db.execute_fetchall(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(enabled) AS enabled, "
                "SUM(CASE WHEN token_type = 'api' THEN 1 ELSE 0 END) AS api_count, "
                "SUM(CASE WHEN token_type = 'session' THEN 1 ELSE 0 END) AS session_count, "
                "SUM(CASE WHEN expires_at IS NOT NULL THEN 1 ELSE 0 END) AS with_expiry "
                "FROM github_tokens"
            )
        finally:
            await db.close()

        r = row[0]
        return {
            "total": r["total"] or 0,
            "enabled": r["enabled"] or 0,
            "api_count": r["api_count"] or 0,
            "session_count": r["session_count"] or 0,
            "with_expiry": r["with_expiry"] or 0,
        }

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    async def _hot_reload(self) -> None:
        """Read all enabled tokens/sessions from DB and push to ResourceManager.

        Silently skips when the ResourceManager has not been initialised
        (``try/except RuntimeError``), because hot-reload is only meaningful
        for a running pipeline.  DB persistence is always active.
        """
        db = await get_db(self._db_path)
        try:
            api_cursor = await db.execute(
                "SELECT token_encrypted FROM github_tokens "
                "WHERE token_type = 'api' AND enabled = 1"
            )
            api_rows = await api_cursor.fetchall()

            sess_cursor = await db.execute(
                "SELECT token_encrypted FROM github_tokens "
                "WHERE token_type = 'session' AND enabled = 1"
            )
            sess_rows = await sess_cursor.fetchall()
        finally:
            await db.close()

        tokens = [decrypt_str(r["token_encrypted"]) for r in api_rows]
        sessions = [decrypt_str(r["token_encrypted"]) for r in sess_rows]

        try:
            update_credentials(sessions, tokens)
        except RuntimeError:
            logger.debug(
                "Hot-reload skipped — ResourceManager not initialised "
                f"(tokens={len(tokens)} sessions={len(sessions)} persisted)"
            )


# ---------------------------------------------------------------------------
# Module-level singleton (web/deps.py style)
# ---------------------------------------------------------------------------

_service: TokenService | None = None


def get_token_service(db_path: str | None = None) -> TokenService:
    """Return the module-level ``TokenService`` singleton.

    On first call a database path **must** be provided (or ``HARVESTER_DB``
    env var set).  Subsequent calls return the cached instance.
    """
    global _service
    if _service is None:
        from .db import resolve_db_path
        path = db_path if db_path is not None else resolve_db_path()
        _service = TokenService(path)
    return _service
