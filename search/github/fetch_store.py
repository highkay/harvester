#!/usr/bin/env python3

"""
Cross-run ETag + extraction-outcome store for RAW file fetches (stdlib sqlite3).

Why this exists (measured 2026-10-22): consecutive scan runs re-discover the
SAME links — overlap is 78.8-92.2% (nvidia 92.2 / glm 84.1 / tavily 83.8 /
exa 81.6 / brave 81.4) — and every one of them was re-downloaded and
re-extracted. raw.githubusercontent.com answers ``If-None-Match`` with a
**304 and a 0-byte body** (probed this session), so the body download can be
skipped, but skipping the EXTRACTION too would drop ``valid_keys_found`` /
``materials_total`` by the overlap fraction and stop the pool's periodic
re-validation of already-known keys. This store therefore keeps two facts
apart:

* ``fetch_state``   — RESOURCE facts (etag, status) keyed by url hash alone,
  provider-independent: the file either changed or it did not.
* ``fetch_outcome`` — EXTRACTION facts (the candidates one provider's patterns
  pulled out of that body), keyed by (provider, url hash, pattern
  fingerprint): a 304 replays them into CHECK without a download and without
  calling the extractor, so both counters and the re-validation cadence stay
  intact. An extraction that produced NOTHING is stored too, as an explicit
  empty row (``candidates='[]'`` + ``has_candidates=0``): ~99% of raw links
  yield no candidate (kimi-coding 0.07%, glm 0.70-0.76% measured), so this is
  the bulk case — a 304 on it skips the fetch entirely instead of paying a
  probe plus a full re-download.

Shape mirrors ``search/github/index.py`` / ``search/github/cache.py`` (WAL,
``check_same_thread=False``, one RLock) with two deliberate differences:

1. It is NEVER a process-global singleton. ``configure_github_transport``
   close()s the singletons it owns on every scan start
   (``search/client.py:668-670``), so a shared store would be closed under a
   concurrent run. ``AcquisitionStage`` instantiates one per run and closes it
   on teardown.
2. It is FAIL-OPEN. Any open/read/write error disables the instance (one
   warning, never raised): conditional fetching is an optimization, and an
   unwritable ``./data`` must not stop a scan — the caller degrades to the
   plain unconditional fetch.

Safety rules encoded here (see the plan's "Must NOT" list):
* an empty etag never authorizes a skip (``FetchState.etag`` is only written
  from a real header value);
* a changed pattern fingerprint is a different row, so it never replays;
* provider is part of the outcome PK, so nothing replays across providers;
* only successful fetches are stored, so a 404 is never cached;
* a missing outcome row still means "never extracted" and forces a full fetch;
  only an explicit empty marker authorizes skipping the extraction.
* rows are pruned by age (14 d) and by a hard row cap (2 M) — ``./data`` is a
  bind mount and ``links.db`` already hit 1.1 GB when a store grew without
  eviction.

Candidate bodies are API keys: nothing in this module logs a key, only counts
and url hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from core.models import Service
from tools.logger import get_logger
from tools.utils import trim

logger = get_logger("search")


@dataclass
class FetchState:
    """Resource facts for one URL (provider-independent)."""

    url_sha: str
    etag: str
    http_status: int
    fetched_at: float


def default_directory(workspace: str) -> str:
    """``<workspace>/cache/fetch_store`` — sibling of ``cache/github_api``."""
    return os.path.join(workspace or ".", "cache", "fetch_store")


class FetchStore:
    """Thread-safe sqlite store of raw-fetch etags and extraction outcomes."""

    #: Delete outcome/state rows older than this many days on prune().
    RETENTION_DAYS = 14
    #: Hard row cap for the larger table on prune() (disk guard).
    MAX_ROWS = 2_000_000

    def __init__(self, directory: str, enabled: bool = True):
        self.enabled = bool(enabled)
        self.directory = directory
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

        if not self.enabled:
            return

        try:
            os.makedirs(directory, exist_ok=True)
            db_path = os.path.join(directory, "fetch_store.db")
            conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            # The store is new in this change set, so there is no data to
            # migrate. A fetch_store.db written by an earlier build of this
            # branch may still exist under ./data, though: it lacks the
            # has_candidates marker. Recreate JUST the outcome table then (a
            # one-time pre-release shape) and keep the state/etag rows.
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(fetch_outcome)")}
            if existing_columns and "has_candidates" not in existing_columns:
                conn.execute("DROP TABLE fetch_outcome")
                logger.warning(
                    "[fetch-store] recreating fetch_outcome for the empty-outcome marker "
                    "(pre-release schema; no data migration needed)"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fetch_state (
                    url_sha TEXT PRIMARY KEY,
                    etag TEXT,
                    http_status INTEGER NOT NULL DEFAULT 200,
                    fetched_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fetch_outcome (
                    provider TEXT NOT NULL,
                    url_sha TEXT NOT NULL,
                    pattern_fp TEXT NOT NULL,
                    candidates TEXT NOT NULL,
                    has_candidates INTEGER NOT NULL DEFAULT 0,
                    fetched_at REAL NOT NULL,
                    PRIMARY KEY (provider, url_sha, pattern_fp)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fetch_state_fetched ON fetch_state(fetched_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fetch_outcome_fetched ON fetch_outcome(fetched_at)"
            )
            conn.commit()
            self._conn = conn
        except Exception as e:
            # Fail-open: no store is always safe, it only costs the bandwidth.
            self._discard_connection()
            self.enabled = False
            logger.warning(
                f"[fetch-store] unavailable at {directory} ({e}); "
                f"raw fetches stay unconditional for this run"
            )

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def url_sha(url: str) -> str:
        """Stable hash of a fetch URL (never store the URL itself)."""
        return hashlib.sha256(trim(url).encode("utf-8")).hexdigest()

    @staticmethod
    def pattern_fingerprint(
        key_pattern: str = "",
        address_pattern: str = "",
        endpoint_pattern: str = "",
        model_pattern: str = "",
    ) -> str:
        """Fingerprint of the four extraction patterns (key|address|endpoint|model).

        A different fingerprint is a different ``fetch_outcome`` row, so a
        pattern change can never replay stale candidates: the URL is
        re-fetched and re-extracted under the new fingerprint.
        """
        raw = "|".join(
            (
                key_pattern or "",
                address_pattern or "",
                endpoint_pattern or "",
                model_pattern or "",
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # fetch_state (resource facts)
    # ------------------------------------------------------------------

    def get_state(self, url: str) -> Optional[FetchState]:
        """Return the stored resource facts for *url*, or None."""
        if not self.enabled or not self._conn:
            return None
        sha = self.url_sha(url)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT url_sha, etag, http_status, fetched_at FROM fetch_state WHERE url_sha=?",
                    (sha,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning(f"[fetch-store] get_state failed ({e}); treating as unseen")
            return None
        if not row:
            return None
        return FetchState(
            url_sha=row[0],
            etag=row[1] or "",
            http_status=int(row[2] or 0),
            fetched_at=float(row[3] or 0.0),
        )

    def put_state(self, url: str, etag: str, http_status: int = 200) -> bool:
        """Store the etag of a SUCCESSFUL fetch. Returns True when written.

        An empty etag is never written: it could not drive a conditional
        request, and a row without one would only invite a caller to skip a
        fetch on the strength of nothing.
        """
        if not self.enabled or not self._conn:
            return False
        etag = trim(etag)
        if not etag:
            return False
        sha = self.url_sha(url)
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO fetch_state(url_sha, etag, http_status, fetched_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(url_sha) DO UPDATE SET
                        etag=excluded.etag,
                        http_status=excluded.http_status,
                        fetched_at=excluded.fetched_at
                    """,
                    (sha, etag, int(http_status), time.time()),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"[fetch-store] put_state failed ({e})")
            return False
        return True

    # ------------------------------------------------------------------
    # fetch_outcome (extraction facts)
    # ------------------------------------------------------------------

    def get_outcome(self, provider: str, url: str, pattern_fp: str) -> Optional[List[Service]]:
        """Replay candidates for (provider, url, pattern_fp), or None.

        Three possible answers:

        * a non-empty list — the stored candidates for this
          (provider, url, pattern fingerprint);
        * ``[]`` — that pair WAS extracted and produced nothing. This is an
          explicit, replayable fact (``has_candidates=0``): on a 304 the caller
          skips the download AND the extractor. Zero-candidate links are the
          bulk of the corpus (~99%), so this is what makes an unchanged link
          cost no body bytes and no extraction;
        * ``None`` — no row at all (never extracted under these patterns), the
          row is unreadable/corrupt, or the store is disabled. The caller must
          then do a normal full fetch.
        """
        if not self.enabled or not self._conn:
            return None
        provider = trim(provider)
        pattern_fp = trim(pattern_fp)
        if not provider or not pattern_fp:
            return None
        sha = self.url_sha(url)
        try:
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT candidates, has_candidates FROM fetch_outcome
                    WHERE provider=? AND url_sha=? AND pattern_fp=?
                    """,
                    (provider, sha, pattern_fp),
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning(f"[fetch-store] get_outcome failed ({e}); treating as unseen")
            return None
        if not row:
            return None
        try:
            raw = json.loads(row[0])
            if not isinstance(raw, list):
                raise ValueError("candidates payload is not a list")
            services = [Service.from_dict(item) for item in raw if isinstance(item, dict)]
        except Exception as e:
            logger.warning(f"[fetch-store] unreadable outcome row ({e}); treating as unseen")
            return None
        if not int(row[1] or 0):
            # Explicit empty marker: the extraction completed and produced
            # nothing. ``[]`` (not None) is what authorizes the 304 skip.
            return []
        if not services:
            # The marker claims candidates but none deserialized: a corrupt
            # row must force a real fetch, never a silent skip.
            logger.warning(
                "[fetch-store] outcome row marked non-empty but deserialized empty; treating as unseen"
            )
            return None
        return services

    def put_outcome(
        self, provider: str, url: str, pattern_fp: str, candidates: Sequence[Service]
    ) -> bool:
        """Store one completed extraction (empties included). True when written.

        A row is written for EVERY completed extraction, including one that
        produced nothing: ``candidates='[]'`` with ``has_candidates=0``. That
        empty marker is what lets a later 304 skip the download and the
        extractor for the ~99% of raw links that yield no candidate; without
        it those links would pay a conditional probe PLUS a full fetch on
        every run (strictly worse than no ETag at all).
        """
        if not self.enabled or not self._conn:
            return False
        provider = trim(provider)
        pattern_fp = trim(pattern_fp)
        if not provider or not pattern_fp:
            return False
        services = list(candidates or [])
        sha = self.url_sha(url)
        try:
            payload = json.dumps([c.to_dict() for c in services], ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[fetch-store] cannot serialize {len(services)} candidate(s): {e}")
            return False
        has_candidates = 1 if services else 0
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO fetch_outcome(
                        provider, url_sha, pattern_fp, candidates, has_candidates, fetched_at
                    )
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(provider, url_sha, pattern_fp) DO UPDATE SET
                        candidates=excluded.candidates,
                        has_candidates=excluded.has_candidates,
                        fetched_at=excluded.fetched_at
                    """,
                    (provider, sha, pattern_fp, payload, has_candidates, time.time()),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"[fetch-store] put_outcome failed ({e})")
            return False
        return True

    # ------------------------------------------------------------------
    # Lifecycle / retention
    # ------------------------------------------------------------------

    def prune(self, retention_days: int = RETENTION_DAYS, max_rows: int = MAX_ROWS) -> int:
        """Drop rows older than *retention_days*, then cap the tables at *max_rows*.

        Returns the number of rows deleted. Never raises: retention is a disk
        guard, and a locked/partial DB must not abort a teardown.
        """
        if not self.enabled or not self._conn:
            return 0
        retention_days = max(0, int(retention_days))
        max_rows = max(1, int(max_rows))
        cutoff = time.time() - retention_days * 86400.0
        removed = 0
        try:
            with self._lock:
                for table in ("fetch_state", "fetch_outcome"):
                    cur = self._conn.execute(
                        f"DELETE FROM {table} WHERE fetched_at < ?", (cutoff,)
                    )
                    removed += int(cur.rowcount or 0)
                    # The row cap is a disk guard, not a routine pass: skip the
                    # ORDER BY scan entirely while the table is under it.
                    rows = int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    if rows <= max_rows:
                        continue
                    # Newest-first keepers, so a cap hit drops the stalest rows.
                    cur = self._conn.execute(
                        f"""
                        DELETE FROM {table} WHERE rowid NOT IN (
                            SELECT rowid FROM {table} ORDER BY fetched_at DESC LIMIT ?
                        )
                        """,
                        (max_rows,),
                    )
                    removed += int(cur.rowcount or 0)
                self._conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"[fetch-store] prune failed ({e})")
            return removed
        if removed:
            logger.info(f"[fetch-store] pruned {removed} row(s) (retention {retention_days}d, cap {max_rows})")
        return removed

    def stats(self) -> dict:
        """Row counts + on-disk size (observability / disk guard)."""
        if not self.enabled or not self._conn:
            return {"enabled": False, "fetch_state": 0, "fetch_outcome": 0}
        try:
            with self._lock:
                state = int(self._conn.execute("SELECT COUNT(*) FROM fetch_state").fetchone()[0])
                outcome = int(self._conn.execute("SELECT COUNT(*) FROM fetch_outcome").fetchone()[0])
        except sqlite3.Error as e:
            logger.warning(f"[fetch-store] stats failed ({e})")
            return {"enabled": True, "fetch_state": 0, "fetch_outcome": 0}
        return {
            "enabled": True,
            "fetch_state": state,
            "fetch_outcome": outcome,
            "bytes": self._db_size(),
        }

    def _db_size(self) -> int:
        try:
            path = os.path.join(self.directory, "fetch_store.db")
            return os.path.getsize(path) if os.path.exists(path) else 0
        except OSError:
            return 0

    def _discard_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        """Idempotent close (mirrors ``LinkIndex.close``)."""
        with self._lock:
            self._discard_connection()
