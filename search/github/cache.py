#!/usr/bin/env python3

"""
ETag / TTL response cache for GitHub API responses (stdlib sqlite3).

Inspired by ohmygh/gx local cache: TTL fast path + ETag revalidation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from tools.logger import get_logger
from tools.utils import trim

logger = get_logger("search")


@dataclass
class CacheEntry:
    body: str
    headers: Dict[str, str]
    etag: str
    fetched_at: float
    ttl: int
    status: int = 200

    @property
    def fresh(self) -> bool:
        if self.ttl <= 0:
            return False
        return (time.time() - self.fetched_at) < self.ttl


class ResponseCache:
    """Thread-safe SQLite response cache."""

    def __init__(self, directory: str, max_entries: int = 1000, enabled: bool = True):
        self.enabled = enabled
        self.max_entries = max(1, max_entries)
        self.directory = directory
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

        if not enabled:
            return

        os.makedirs(directory, exist_ok=True)
        db_path = os.path.join(directory, "responses.db")
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                cache_key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                body TEXT NOT NULL,
                headers TEXT NOT NULL,
                etag TEXT,
                status INTEGER NOT NULL,
                fetched_at REAL NOT NULL,
                ttl INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_responses_fetched ON responses(fetched_at)"
        )
        self._conn.commit()

    @staticmethod
    def make_key(method: str, url: str, auth_fingerprint: str = "") -> str:
        raw = f"{method.upper()}\n{url}\n{auth_fingerprint or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint_auth(credential: str = "") -> str:
        credential = trim(credential)
        if not credential:
            return "anon"
        return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]

    def get(self, cache_key: str) -> Optional[CacheEntry]:
        if not self.enabled or not self._conn:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT body, headers, etag, status, fetched_at, ttl FROM responses WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            body, headers_json, etag, status, fetched_at, ttl = row
            try:
                headers = json.loads(headers_json) if headers_json else {}
            except Exception:
                headers = {}
            return CacheEntry(
                body=body or "",
                headers={str(k): str(v) for k, v in headers.items()},
                etag=etag or "",
                status=int(status or 200),
                fetched_at=float(fetched_at or 0),
                ttl=int(ttl or 0),
            )

    def put(
        self,
        cache_key: str,
        url: str,
        body: str,
        headers: Dict[str, str],
        ttl: int,
        status: int = 200,
        etag: str = "",
    ) -> None:
        if not self.enabled or not self._conn:
            return
        headers = headers or {}
        if not etag:
            etag = headers.get("ETag") or headers.get("etag") or ""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO responses(cache_key, url, body, headers, etag, status, fetched_at, ttl)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    url=excluded.url,
                    body=excluded.body,
                    headers=excluded.headers,
                    etag=excluded.etag,
                    status=excluded.status,
                    fetched_at=excluded.fetched_at,
                    ttl=excluded.ttl
                """,
                (
                    cache_key,
                    url,
                    body,
                    json.dumps(headers, ensure_ascii=False),
                    etag,
                    int(status),
                    time.time(),
                    int(ttl),
                ),
            )
            self._evict_if_needed()
            self._conn.commit()

    def touch(self, cache_key: str, headers: Optional[Dict[str, str]] = None) -> None:
        """Update metadata after a 304 Not Modified."""
        if not self.enabled or not self._conn:
            return
        with self._lock:
            if headers:
                etag = headers.get("ETag") or headers.get("etag") or ""
                if etag:
                    self._conn.execute(
                        "UPDATE responses SET fetched_at=?, etag=?, headers=? WHERE cache_key=?",
                        (time.time(), etag, json.dumps(headers, ensure_ascii=False), cache_key),
                    )
                else:
                    self._conn.execute(
                        "UPDATE responses SET fetched_at=? WHERE cache_key=?",
                        (time.time(), cache_key),
                    )
            else:
                self._conn.execute(
                    "UPDATE responses SET fetched_at=? WHERE cache_key=?",
                    (time.time(), cache_key),
                )
            self._conn.commit()

    def _evict_if_needed(self) -> None:
        if not self._conn:
            return
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count <= self.max_entries:
            return
        # Drop oldest rows beyond max_entries
        overflow = count - self.max_entries
        self._conn.execute(
            """
            DELETE FROM responses WHERE cache_key IN (
                SELECT cache_key FROM responses ORDER BY fetched_at ASC LIMIT ?
            )
            """,
            (overflow,),
        )

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


def classify_ttl(url: str, ttl_search: int, ttl_core: int) -> int:
    """Pick TTL based on endpoint class (search vs core)."""
    lowered = (url or "").lower()
    if "/search/" in lowered:
        return ttl_search
    return ttl_core
