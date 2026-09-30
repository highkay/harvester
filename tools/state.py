#!/usr/bin/env python3

"""
GitHub credential cooldown and masking helpers.
"""

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from constant.system import (
    GITHUB_CREDENTIAL_COOLDOWN_FACTOR,
    GITHUB_CREDENTIAL_COOLDOWN_MAX,
    GITHUB_CREDENTIAL_COOLDOWN_MIN,
)


def mask_credential(credential: str) -> str:
    """Mask a credential while keeping it recognizable"""
    text = credential or ""
    if len(text) <= 4:
        return "*" * len(text)
    if len(text) <= 12:
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"
    return f"{text[:6]}{'*' * max(6, len(text) - 10)}{text[-4:]}"


def credential_bucket_key(service: str, credential: str) -> str:
    """Build a stable non-sensitive rate-limit bucket key"""
    digest = hashlib.sha256((credential or "").encode("utf-8")).hexdigest()[:12]
    return f"{service}:{digest}" if service and credential else service


class GithubCredentialLimited(Exception):
    """Raised when a single GitHub credential is cooling down"""

    def __init__(self, service: str, credential: str, wait: float, reason: str = ""):
        self.service = service
        self.credential = credential
        self.wait = wait
        self.reason = reason
        super().__init__(f"{service} credential is rate limited for {wait:.1f}s")


@dataclass
class Cooldown:
    until: float = 0.0
    failures: int = 0
    last_wait: float = 0.0


class GithubCredentialState:
    """Thread-safe cooldown registry for GitHub credentials.

    Two independent registries live here:

    - the **cooldown** registry (``_items``) — time-based backoff for
      403/429/quota exhaustion, unchanged;
    - the **dead** registry (``_dead`` + ``_strikes``) — process-lifetime
      quarantine for credentials GitHub has revoked. A revoked PAT answers
      401 forever; without quarantine it is round-robined forever and silently
      burns its share of every search. Detection strikes are consecutive-401
      scoped: any success clears both the dead flag and the strike count.

    Nothing here is persisted — a revoked token re-enters at process restart
    and re-dies after the strike threshold (search/client.py).
    """

    def __init__(self) -> None:
        self._items: Dict[Tuple[str, str], Cooldown] = {}
        self._dead: set = set()
        self._strikes: Dict[Tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def mark_limited(self, service: str, credential: str, wait: Optional[float] = None) -> float:
        """Mark a credential as cooling down and return the selected wait"""
        if not service or not credential:
            return 0.0

        now = time.time()
        key = (service, credential)

        with self._lock:
            item = self._items.get(key, Cooldown())
            if wait is None or wait <= 0:
                wait = self._next_backoff(item)
            wait = self._clamp(wait)
            item.failures += 1
            item.last_wait = wait
            item.until = max(item.until, now + wait)
            self._items[key] = item
            return wait

    def mark_success(self, service: str, credential: str) -> None:
        """Clear cooldown state after a successful request"""
        if not service or not credential:
            return

        with self._lock:
            key = (service, credential)
            item = self._items.get(key)
            if item and item.until <= time.time():
                self._items.pop(key, None)

    def is_cooling(self, service: str, credential: str) -> bool:
        """Return whether a credential is still cooling down"""
        return self.wait_time(service, credential) > 0

    # ------------------------------------------------------------------
    # Dead-token quarantine (revoked / unauthorized credentials)
    # ------------------------------------------------------------------

    def mark_strike(self, service: str, credential: str) -> int:
        """Count one consecutive auth failure; return the new strike total."""
        if not service or not credential:
            return 0
        with self._lock:
            key = (service, credential)
            strikes = self._strikes.get(key, 0) + 1
            self._strikes[key] = strikes
            return strikes

    def mark_dead(self, service: str, credential: str) -> None:
        """Quarantine a credential: it is never handed out again."""
        if not service or not credential:
            return
        with self._lock:
            key = (service, credential)
            self._dead.add(key)
            self._strikes.pop(key, None)

    def is_dead(self, service: str, credential: str) -> bool:
        """Return whether a credential is quarantined as dead."""
        if not service or not credential:
            return False
        with self._lock:
            return (service, credential) in self._dead

    def mark_alive(self, service: str, credential: str) -> None:
        """Revive a credential: clears both the dead flag and its strikes."""
        if not service or not credential:
            return
        with self._lock:
            key = (service, credential)
            self._dead.discard(key)
            self._strikes.pop(key, None)

    def all_dead(self, service: str, credentials: List[str]) -> bool:
        """Return whether every non-empty credential is quarantined."""
        items = [x for x in credentials if x]
        return bool(items) and all(self.is_dead(service, item) for item in items)

    def wait_time(self, service: str, credential: str) -> float:
        """Return remaining cooldown seconds for a credential"""
        if not service or not credential:
            return 0.0

        now = time.time()
        key = (service, credential)

        with self._lock:
            item = self._items.get(key)
            if not item:
                return 0.0
            wait = item.until - now
            if wait <= 0:
                return 0.0
            return wait

    def all_cooling(self, service: str, credentials: List[str]) -> bool:
        """Return whether all credentials are cooling down"""
        items = [x for x in credentials if x]
        return bool(items) and all(self.is_cooling(service, item) for item in items)

    def next_wait(self, service: str, credentials: List[str]) -> float:
        """Return the earliest cooldown release among credentials"""
        waits = [self.wait_time(service, item) for item in credentials if item]
        waits = [wait for wait in waits if wait > 0]
        return min(waits) if waits else 0.0

    def _next_backoff(self, item: Cooldown) -> float:
        if item.failures <= 0 or item.last_wait <= 0:
            return float(GITHUB_CREDENTIAL_COOLDOWN_MIN)
        return item.last_wait * GITHUB_CREDENTIAL_COOLDOWN_FACTOR

    def _clamp(self, wait: float) -> float:
        return min(float(GITHUB_CREDENTIAL_COOLDOWN_MAX), max(float(GITHUB_CREDENTIAL_COOLDOWN_MIN), float(wait)))


github_credential_state = GithubCredentialState()
