#!/usr/bin/env python3

"""
Local index of discovered GitHub links (stdlib sqlite3).

Inspired by ohmygh/gx passive indexing: remember what we already found so
re-runs can skip duplicate gather work and support offline lookup.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set

from tools.logger import get_logger
from tools.utils import trim

logger = get_logger("search")


@dataclass
class IndexedLink:
    url: str
    provider: str
    search_type: str
    query: str
    first_seen: float
    last_seen: float
    hits: int


class LinkIndex:
    """Thread-safe discovered-link index under the workspace."""

    def __init__(self, directory: str, enabled: bool = True):
        self.enabled = enabled
        self.directory = directory
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

        if not enabled:
            return

        os.makedirs(directory, exist_ok=True)
        db_path = os.path.join(directory, "links.db")
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                url TEXT NOT NULL,
                provider TEXT NOT NULL,
                search_type TEXT NOT NULL DEFAULT 'code',
                query TEXT NOT NULL DEFAULT '',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                hits INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (url, provider)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_links_provider ON links(provider)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_links_last_seen ON links(last_seen)")
        # Optional FTS if available; ignore when the build lacks FTS5
        try:
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS links_fts USING fts5(
                    url, provider, query, search_type, content='links', content_rowid='rowid'
                )
                """
            )
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
            logger.debug("[index] FTS5 unavailable; using plain SQL index only")
        self._conn.commit()

    def add_many(
        self,
        urls: Sequence[str],
        provider: str,
        search_type: str = "code",
        query: str = "",
    ) -> int:
        """Insert or bump hit counts. Returns number of newly seen URLs."""
        if not self.enabled or not self._conn or not urls:
            return 0

        provider = trim(provider) or "unknown"
        search_type = trim(search_type) or "code"
        query = query or ""
        now = time.time()
        new_count = 0

        with self._lock:
            for raw in urls:
                url = trim(raw)
                if not url:
                    continue
                row = self._conn.execute(
                    "SELECT hits FROM links WHERE url=? AND provider=?",
                    (url, provider),
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE links SET last_seen=?, hits=hits+1, search_type=?, query=? WHERE url=? AND provider=?",
                        (now, search_type, query, url, provider),
                    )
                else:
                    self._conn.execute(
                        """
                        INSERT INTO links(url, provider, search_type, query, first_seen, last_seen, hits)
                        VALUES(?,?,?,?,?,?,1)
                        """,
                        (url, provider, search_type, query, now, now),
                    )
                    new_count += 1
            self._conn.commit()
        return new_count

    def known(self, urls: Iterable[str], provider: str) -> Set[str]:
        """Return the subset of urls already indexed for provider."""
        if not self.enabled or not self._conn:
            return set()
        provider = trim(provider)
        found: Set[str] = set()
        with self._lock:
            for raw in urls:
                url = trim(raw)
                if not url:
                    continue
                row = self._conn.execute(
                    "SELECT 1 FROM links WHERE url=? AND provider=? LIMIT 1",
                    (url, provider),
                ).fetchone()
                if row:
                    found.add(url)
        return found

    def filter_new(self, urls: Sequence[str], provider: str) -> List[str]:
        """Keep only URLs not yet in the index for this provider."""
        if not self.enabled or not self._conn:
            return list(urls)
        known = self.known(urls, provider)
        return [u for u in urls if u not in known]

    def search(self, keyword: str, provider: str = "", limit: int = 50) -> List[IndexedLink]:
        """Simple substring search over indexed URLs/queries."""
        if not self.enabled or not self._conn:
            return []
        keyword = trim(keyword)
        if not keyword:
            return []
        limit = max(1, min(500, int(limit)))
        like = f"%{keyword}%"
        with self._lock:
            if provider:
                rows = self._conn.execute(
                    """
                    SELECT url, provider, search_type, query, first_seen, last_seen, hits
                    FROM links
                    WHERE provider=? AND (url LIKE ? OR query LIKE ?)
                    ORDER BY last_seen DESC LIMIT ?
                    """,
                    (provider, like, like, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT url, provider, search_type, query, first_seen, last_seen, hits
                    FROM links
                    WHERE url LIKE ? OR query LIKE ?
                    ORDER BY last_seen DESC LIMIT ?
                    """,
                    (like, like, limit),
                ).fetchall()
        return [
            IndexedLink(
                url=r[0],
                provider=r[1],
                search_type=r[2],
                query=r[3],
                first_seen=r[4],
                last_seen=r[5],
                hits=r[6],
            )
            for r in rows
        ]

    def stats(self, provider: str = "") -> dict:
        if not self.enabled or not self._conn:
            return {"enabled": False, "count": 0}
        with self._lock:
            if provider:
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM links WHERE provider=?", (provider,)
                ).fetchone()[0]
            else:
                count = self._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        return {"enabled": True, "count": int(count), "provider": provider or "*"}

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


_link_index: Optional[LinkIndex] = None
_link_index_lock = threading.Lock()


def get_link_index() -> Optional[LinkIndex]:
    return _link_index


def init_link_index(directory: str, enabled: bool = True) -> Optional[LinkIndex]:
    global _link_index
    with _link_index_lock:
        if not enabled:
            _link_index = None
            logger.info("[index] link index disabled")
            return None
        _link_index = LinkIndex(directory=directory, enabled=True)
        logger.info(f"[index] link index ready at {directory}")
        return _link_index
