#!/usr/bin/env python3
"""Bounded persistent cache for terminal API-key verdicts.

Only two verdicts are terminal enough to survive a restart: an explicit
``INVALID_KEY`` (30 days) and an explicit ``NO_QUOTA`` (14 days). Everything
else - UNKNOWN, NO_MODEL, NO_ACCESS, BAD_REQUEST, retryable transport signals
and valid/available results - must be re-probed and is never written here.

Layout
------
``<workspace>/verdict-cache/<sha256(task)>.json`` holds one flat JSON object
per task::

    {
      "<cache-key>": {
        "verdict": "invalid_key",
        "observed_at": 1700000000.0,
        "fingerprint": "<64 hex chars>"
      }
    }

The cache key is ``SHA-256(task + key + normalized base_url + endpoint +
probe_model + tier)``; the stored fingerprint is a separate SHA-256 of the
same identity without the key. Raw API keys and response bodies never reach
disk. A lookup recomputes both the key and the fingerprint, so a changed task,
address/region, endpoint, probe model or capability tier is a miss (never a
false hit).

Failure policy
--------------
Every read path fails open: missing, malformed, unreadable or fingerprint-
mismatched state is a miss and never raises into the scan. Writes use
``AtomicFileWriter.write_atomic`` (temp file + fsync + replace); a write
failure returns ``False`` and leaves the previous file intact. Expired entries
are dropped on lookup *and* pruned from the file during the next successful
write, so the TTL - not an arbitrary entry count - is the storage bound.

Concurrency
-----------
A per-task ``threading.Lock`` covers the complete load-prune-merge-write cycle,
so concurrent writers sharing one ``VerdictCache`` instance cannot lose each
other's entries. Callers should share one instance per workspace.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from core.enums import ErrorReason
from storage.atomic import AtomicFileWriter
from tools.logger import get_logger
from tools.utils import handle_exceptions

logger = get_logger("storage")

CACHE_DIRECTORY_NAME = "verdict-cache"
INVALID_KEY_TTL_SECONDS = 30 * 24 * 60 * 60
NO_QUOTA_TTL_SECONDS = 14 * 24 * 60 * 60

_VERDICT_TTLS: dict[str, int] = {
    ErrorReason.INVALID_KEY.value: INVALID_KEY_TTL_SECONDS,
    ErrorReason.NO_QUOTA.value: NO_QUOTA_TTL_SECONDS,
}
VERDICT_TTLS: Mapping[str, int] = MappingProxyType(_VERDICT_TTLS)

_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class VerdictEntry:
    """One persisted terminal verdict (the only three fields ever stored)."""

    verdict: str
    observed_at: float
    fingerprint: str


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_task(task: str) -> str:
    return task.strip()


def normalize_base_url(base_url: str) -> str:
    """Canonicalize the base URL used for identity.

    Scheme and host are case-insensitive and default ports are dropped, so
    ``HTTPS://API.EXAMPLE.COM:443/v1/`` and ``https://api.example.com/v1`` are
    the same probe target. The path stays case-sensitive, query strings are
    preserved, fragments and userinfo are dropped. A malformed URL is returned
    trimmed rather than raising; the identity stays deterministic either way.
    """
    text = base_url.strip()
    if not text:
        return ""

    try:
        parts = urllib.parse.urlsplit(text)
        port = parts.port
    except ValueError:
        return text

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not scheme and not host:
        # Bare host/path without a scheme: only the trailing slash is safe to
        # normalize; lowercasing the path could merge two distinct targets.
        return text.rstrip("/")

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = host
    else:
        netloc = f"{host}:{port}"

    return urllib.parse.urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint.strip().strip("/")


def _normalize_probe_model(probe_model: str) -> str:
    return probe_model.strip()


def _normalize_tier(tier: str) -> str:
    return tier.strip()


def _identity_payload(
    task: str,
    base_url: str,
    endpoint: str,
    probe_model: str,
    tier: str,
) -> str:
    return json.dumps(
        [
            _normalize_task(task),
            normalize_base_url(base_url),
            _normalize_endpoint(endpoint),
            _normalize_probe_model(probe_model),
            _normalize_tier(tier),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def make_fingerprint(
    *,
    task: str,
    base_url: str,
    endpoint: str = "",
    probe_model: str = "",
    tier: str = "",
) -> str:
    """SHA-256 of the full probe identity (task, address, endpoint, model, tier)."""
    payload = _identity_payload(task, base_url, endpoint, probe_model, tier)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_cache_key(
    *,
    task: str,
    key: str,
    base_url: str,
    endpoint: str = "",
    probe_model: str = "",
    tier: str = "",
) -> str:
    """SHA-256(task + key + normalized base_url + endpoint + model/tier)."""
    payload = json.dumps(
        [
            _normalize_task(task),
            key,
            normalize_base_url(base_url),
            _normalize_endpoint(endpoint),
            _normalize_probe_model(probe_model),
            _normalize_tier(tier),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _coerce_verdict(verdict: ErrorReason | str) -> str | None:
    if isinstance(verdict, ErrorReason):
        value = verdict.value
    elif isinstance(verdict, str):
        value = verdict.strip().lower()
    else:
        return None
    return value if value in _VERDICT_TTLS else None


def _is_hex_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX_DIGITS for char in value)


def _parse_entry(raw: Any) -> VerdictEntry | None:
    if not isinstance(raw, dict):
        return None

    verdict = raw.get("verdict")
    observed_at = raw.get("observed_at")
    fingerprint = raw.get("fingerprint")

    if not isinstance(verdict, str) or verdict not in _VERDICT_TTLS:
        return None
    if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
        return None
    try:
        observed = float(observed_at)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(observed) or observed < 0:
        return None
    if not isinstance(fingerprint, str) or not _is_hex_digest(fingerprint):
        return None

    return VerdictEntry(verdict=verdict, observed_at=observed, fingerprint=fingerprint)


def _is_stale(observed_at: float, now: float, ttl: int) -> bool:
    """TTL is inclusive: at exactly ``observed_at + ttl`` the entry is expired.

    A timestamp from the future is also unusable (clock skew makes its age
    unverifiable), so it fails open instead of pinning an entry forever.
    """
    age = now - observed_at
    return age < 0 or age >= ttl


def _read_entries(path: str) -> dict[str, Any]:
    """Load the raw task file.

    Missing and structurally-corrupt files are treated as empty (a later write
    heals them). I/O errors such as permission/sharing failures propagate so a
    write path can refuse to clobber a file it could not read.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError:
        return {}

    try:
        data = json.loads(text)
    except ValueError:
        return {}

    if not isinstance(data, dict):
        return {}
    return data


def _compact_entries(raw: dict[str, Any], now: float) -> dict[str, VerdictEntry]:
    """Drop unparseable, forbidden and expired entries before a rewrite."""
    compacted: dict[str, VerdictEntry] = {}
    for cache_key, raw_entry in raw.items():
        if not isinstance(cache_key, str):
            continue
        entry = _parse_entry(raw_entry)
        if entry is None:
            continue
        ttl = _VERDICT_TTLS.get(entry.verdict)
        if ttl is None or _is_stale(entry.observed_at, now, ttl):
            continue
        compacted[cache_key] = entry
    return compacted


class VerdictCache:
    """Per-workspace persistent cache for explicit INVALID_KEY / NO_QUOTA verdicts."""

    def __init__(self, workspace: str, clock: Callable[[], float] | None = None):
        self.workspace = workspace
        self.directory = os.path.join(workspace, CACHE_DIRECTORY_NAME)
        self._clock = clock or time.time
        self._locks_guard = threading.Lock()
        self._task_locks: dict[str, threading.Lock] = {}

    def task_path(self, task: str) -> str:
        """Absolute path of the single JSON file that holds one task's entries."""
        normalized = _normalize_task(task) if isinstance(task, str) else ""
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return os.path.join(self.directory, digest + ".json")

    def _now(self) -> float | None:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        return result if math.isfinite(result) else None

    def _task_lock(self, task: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._task_locks.get(task)
            if lock is None:
                lock = threading.Lock()
                self._task_locks[task] = lock
            return lock

    @handle_exceptions(default_result=None, log_level="debug")
    def lookup(
        self,
        *,
        task: str,
        key: str,
        base_url: str,
        endpoint: str = "",
        probe_model: str = "",
        tier: str = "",
    ) -> VerdictEntry | None:
        """Return a fresh, fingerprint-matching terminal verdict, else ``None``.

        Never raises: any malformed, stale or unreadable state is a miss.
        """
        if not _is_identifier(task) or not _is_identifier(key):
            return None
        if not all(_is_text(value) for value in (base_url, endpoint, probe_model, tier)):
            return None

        now = self._now()
        if now is None:
            return None

        fingerprint = make_fingerprint(
            task=task,
            base_url=base_url,
            endpoint=endpoint,
            probe_model=probe_model,
            tier=tier,
        )
        cache_key = make_cache_key(
            task=task,
            key=key,
            base_url=base_url,
            endpoint=endpoint,
            probe_model=probe_model,
            tier=tier,
        )
        raw_entry = _read_entries(self.task_path(task)).get(cache_key)
        entry = _parse_entry(raw_entry)
        if entry is None or entry.fingerprint != fingerprint:
            return None

        ttl = _VERDICT_TTLS.get(entry.verdict)
        if ttl is None or _is_stale(entry.observed_at, now, ttl):
            return None
        return entry

    @handle_exceptions(default_result=False, log_level="warning")
    def store(
        self,
        *,
        task: str,
        key: str,
        base_url: str,
        verdict: ErrorReason | str,
        endpoint: str = "",
        probe_model: str = "",
        tier: str = "",
    ) -> bool:
        """Persist one eligible verdict; return ``False`` on any failure.

        Only explicit INVALID_KEY / NO_QUOTA values are accepted. The complete
        read-prune-merge-write cycle runs under this task's lock, and expired
        entries are compacted away before the atomic replace.
        """
        if not _is_identifier(task) or not _is_identifier(key):
            return False
        if not all(_is_text(value) for value in (base_url, endpoint, probe_model, tier)):
            return False

        verdict_name = _coerce_verdict(verdict)
        if verdict_name is None:
            return False

        now = self._now()
        if now is None:
            return False

        normalized_task = _normalize_task(task)
        with self._task_lock(normalized_task):
            path = self.task_path(normalized_task)
            fingerprint = make_fingerprint(
                task=normalized_task,
                base_url=base_url,
                endpoint=endpoint,
                probe_model=probe_model,
                tier=tier,
            )
            cache_key = make_cache_key(
                task=normalized_task,
                key=key,
                base_url=base_url,
                endpoint=endpoint,
                probe_model=probe_model,
                tier=tier,
            )

            entries = _compact_entries(_read_entries(path), now)
            entries[cache_key] = VerdictEntry(
                verdict=verdict_name,
                observed_at=now,
                fingerprint=fingerprint,
            )

            payload = {
                entry_key: {
                    "verdict": entry.verdict,
                    "observed_at": entry.observed_at,
                    "fingerprint": entry.fingerprint,
                }
                for entry_key, entry in sorted(entries.items())
            }
            AtomicFileWriter.write_atomic(
                path,
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            )
        return True