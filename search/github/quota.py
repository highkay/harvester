#!/usr/bin/env python3

"""
GitHub API quota tracker using X-RateLimit-* headers.

Tracks search vs core resources per credential (inspired by ohmygh/gx resource split).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from tools.logger import get_logger
from tools.utils import trim

logger = get_logger("search")


@dataclass
class ResourceQuota:
    limit: int = 0
    remaining: int = 0
    reset_at: float = 0.0
    resource: str = "core"

    def exhausted(self) -> bool:
        if self.remaining > 0:
            return False
        if self.reset_at <= 0:
            return self.remaining <= 0 and self.limit > 0
        return time.time() < self.reset_at

    def wait_seconds(self) -> float:
        if not self.exhausted():
            return 0.0
        if self.reset_at <= 0:
            return 0.0
        return max(0.0, self.reset_at - time.time())


class QuotaTracker:
    """Per-credential GitHub rate-limit ledger."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._lock = threading.RLock()
        # key: (credential_fp, resource) -> ResourceQuota
        self._quotas: Dict[Tuple[str, str], ResourceQuota] = {}

    @staticmethod
    def fingerprint(credential: str = "") -> str:
        credential = trim(credential)
        if not credential:
            return "anon"
        # Keep short stable id without storing the secret
        import hashlib

        return hashlib.sha256(credential.encode("utf-8")).hexdigest()[:12]

    def update_from_headers(self, credential: str, headers: Dict[str, str]) -> Optional[ResourceQuota]:
        if not self.enabled or not headers:
            return None

        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
        if "x-ratelimit-remaining" not in normalized and "x-ratelimit-limit" not in normalized:
            return None

        resource = (normalized.get("x-ratelimit-resource") or "core").strip().lower()
        try:
            limit = int(float(normalized.get("x-ratelimit-limit") or 0))
        except ValueError:
            limit = 0
        try:
            remaining = int(float(normalized.get("x-ratelimit-remaining") or 0))
        except ValueError:
            remaining = 0
        try:
            reset_at = float(normalized.get("x-ratelimit-reset") or 0)
        except ValueError:
            reset_at = 0.0

        quota = ResourceQuota(limit=limit, remaining=remaining, reset_at=reset_at, resource=resource)
        key = (self.fingerprint(credential), resource)
        with self._lock:
            self._quotas[key] = quota

        if remaining <= 0 and reset_at > time.time():
            wait = reset_at - time.time()
            logger.info(
                f"[quota] {resource} exhausted for credential {self.fingerprint(credential)}, "
                f"resets in {wait:.0f}s"
            )
        return quota

    def wait_if_needed(self, credential: str, resource: str = "search") -> float:
        """
        If the credential has no remaining quota for resource, sleep until reset.
        Returns seconds slept.
        """
        if not self.enabled:
            return 0.0

        key = (self.fingerprint(credential), resource)
        with self._lock:
            quota = self._quotas.get(key)
            if not quota or not quota.exhausted():
                return 0.0
            wait = quota.wait_seconds()

        if wait <= 0:
            return 0.0

        # Cap single sleep to avoid multi-hour blocks on bad clocks
        sleep_for = min(wait + 0.5, 3600.0)
        logger.warning(
            f"[quota] waiting {sleep_for:.1f}s for {resource} quota reset "
            f"(credential {self.fingerprint(credential)})"
        )
        time.sleep(sleep_for)
        return sleep_for

    def remaining(self, credential: str, resource: str = "search") -> Optional[int]:
        key = (self.fingerprint(credential), resource)
        with self._lock:
            quota = self._quotas.get(key)
            return None if not quota else quota.remaining


def resource_from_url(url: str) -> str:
    lowered = (url or "").lower()
    if "api.github.com/search/" in lowered or "/search/" in lowered:
        return "search"
    return "core"
