#!/usr/bin/env python3

"""
Brave Search provider implementation.

Wire facts mirrored from the live-probed Brave integration in the tavily-hikari
pool (``probe_brave_missing_q`` in
``src/server/handlers/admin_resources/api_keys_and_logs.rs`` and
``classify_brave_failure`` in ``src/analysis/provider_failure_classification.rs``):

- Auth is the ``X-Subscription-Token`` header on ``https://api.search.brave.com``.
- ``GET {base}/res/v1/web/search`` WITHOUT a ``q`` parameter is a ZERO-COST
  probe: authentication runs before request validation, so a 422 VALIDATION
  answer complaining about the missing ``q`` proves the key authenticated and
  is never billed. Validation therefore needs a single stage — unlike Tavily,
  whose free /usage endpoint cannot see a disabled account and must be followed
  by a billable /search probe.
- An invalid token answers ``422 SUBSCRIPTION_TOKEN_INVALID`` — NOT 401.
- ``402 QUOTA_LIMITED`` = month quota spent; it recovers at the UTC month
  boundary, so it is a wait-check verdict, never a key verdict.
- ``429 RATE_LIMITED`` = the key itself is valid but throttled.
- Responses may advertise the month quota in the POSITIONAL ``X-RateLimit-*``
  header family, which ``inspect()`` harvests without an extra request.
"""

import json
import time
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests

from constant.system import NO_RETRY_ERROR_CODES
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_error_message, http_error_status, request
from tools.logger import get_logger
from tools.utils import trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")

# Brave authenticates with this header — no Bearer scheme, no query parameter.
_SUBSCRIPTION_TOKEN_HEADER = "X-Subscription-Token"

# Length of the month window Brave advertises in ``X-RateLimit-Policy``
# (``1;w=1, 2000;w=2592000``); the Limit/Remaining/Reset headers are comma
# lists paired with that policy position-for-position.
_MONTH_WINDOW_SECONDS = 2592000

# Verdicts a retry cannot change, so the probe loop ends on them.
_TERMINAL_REASONS = frozenset({ErrorReason.INVALID_KEY, ErrorReason.NO_ACCESS, ErrorReason.NO_QUOTA})

# Fields that identify a Brave JSON error envelope, telling an upstream auth
# rejection apart from a Cloudflare/geo-block HTML interstitial.
_ENVELOPE_MARKERS = ("code", "title", "type", "detail")


def _to_int(raw: Any) -> Optional[int]:
    try:
        return int(trim(str(raw)))
    except Exception:
        return None


def _positional_int(raw: Any, index: int) -> Optional[int]:
    """The index-th integer of a comma-separated header list, or None."""
    if raw is None:
        return None

    parts = [part.strip() for part in str(raw).split(",")]
    if index >= len(parts):
        return None

    return _to_int(parts[index])


def _json_body(message: str) -> Any:
    """Parse a response body, returning None when it is not JSON."""
    try:
        return json.loads(message)
    except Exception:
        return None


def _error_code(data: Any) -> str:
    """Brave's error code, uppercase, from either envelope shape.

    The documented envelope is flat (``{"code": "VALIDATION"}``) while the
    hikari probe reads a nested one (``{"error": {"code": ...}}``); both are
    accepted so the taxonomy holds whichever shape answers.
    """
    if not isinstance(data, dict):
        return ""

    for container in (data.get("error"), data):
        if isinstance(container, dict):
            code = container.get("code")
            if isinstance(code, str) and trim(code):
                return trim(code).upper()

    return ""


def _validation_errors(data: Any) -> List[Any]:
    """The ``meta.errors`` array of a VALIDATION envelope (flat or nested)."""
    if not isinstance(data, dict):
        return []

    containers: List[Any] = [data]
    error = data.get("error")
    if isinstance(error, dict):
        containers.append(error)

    for container in containers:
        meta = container.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("errors"), list):
            return list(meta["errors"])

    return []


def _validation_flags_subscription_token(data: Any) -> bool:
    """True when a VALIDATION error is about the token header, not the query.

    This loc check is the whole trick of the probe: a complaint about the
    missing ``q`` means the key already authenticated (unbilled), while a
    complaint about ``["header", "x-subscription-token"]`` means it did not.
    """
    wanted = _SUBSCRIPTION_TOKEN_HEADER.lower()
    for entry in _validation_errors(data):
        if not isinstance(entry, dict):
            continue

        loc = entry.get("loc")
        if not isinstance(loc, list) or len(loc) < 2:
            continue

        place, name = loc[0], loc[1]
        if not isinstance(place, str) or not isinstance(name, str):
            continue

        if place.strip().lower() == "header" and name.strip().lower() == wanted:
            return True

    return False


def _has_error_envelope(data: Any) -> bool:
    """True for a Brave JSON error envelope, False for an HTML interstitial."""
    if not isinstance(data, dict):
        return False

    if isinstance(data.get("error"), dict):
        return True

    return any(trim(str(data.get(marker))) for marker in _ENVELOPE_MARKERS if data.get(marker) is not None)


def _response_headers(response: Any) -> Dict[str, str]:
    """Lowercase-keyed copy of a response's headers ({} when absent)."""
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return {}

    return {str(key).lower(): str(value) for key, value in headers.items()}


def _error_body(error: requests.exceptions.HTTPError) -> str:
    """The FULL error body, not ``http_error_message``'s 300-char truncation.

    A real 422 VALIDATION envelope runs ~290-400 chars and its decisive
    ``meta.errors[].loc`` tail sits at the END, so a truncated body loses the
    only field that separates a valid key from an invalid one and would file
    live keys as UNKNOWN -> invalid.
    """
    response = getattr(error, "response", None)
    try:
        text = response.text if response is not None else ""
    except Exception:
        text = ""

    return trim(text) or http_error_message(error)


class BraveProvider(AIBaseProvider):
    """Brave Search API provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        config = self.extract(
            kwargs,
            {
                "name": "brave",
                "base_url": "https://api.search.brave.com",
                "completion_path": "/res/v1/web/search",
                "model_path": "/res/v1/web/search",
                "default_model": "brave-web-search",
            },
        )

        super().__init__(
            config["name"],
            config["base_url"],
            config["completion_path"],
            config["model_path"],
            config["default_model"],
            conditions,
            **kwargs,
        )

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for Brave API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "accept": "application/json",
            _SUBSCRIPTION_TOKEN_HEADER: token,
        }
        return self._merge_headers(headers, additional)

    def _probe_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.completion_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Validate a Brave key with ONE zero-cost probe.

        The q-less answer carries the complete taxonomy (module docstring), so
        there is no second stage: validating a candidate never spends that
        key's own month quota.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        code, message, _ = self._probe(headers, address=address, endpoint=endpoint)
        if code == 0:
            logger.debug(f"Check Brave probe failed: {message}")

        return self._judge_probe(code, message)

    def _probe(self, headers: Dict[str, str], address: str = "", endpoint: str = "") -> Tuple[int, str, Dict[str, str]]:
        """Run the q-less probe with the provider's retry budget.

        Returns ``(status_code, body, response_headers)``. ``status_code == 0``
        means no HTTP answer at all (timeout / connection failure), with the
        sentinel ``"timeout"`` in the body slot for a timeout.
        """
        url = self._probe_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message, response_headers = 0, "", {}
        for attempt in range(max(1, retries)):
            try:
                # NO q parameter and no params kwarg: adding a query would turn
                # this free probe into a billable search on the candidate's key.
                with request("GET", url, headers=headers, timeout=timeout, use_proxy=self._get_use_proxy()) as response:
                    return response.status_code, response.text, _response_headers(response)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = _error_body(e)
                response_headers = _response_headers(getattr(e, "response", None))

                result = self._judge_probe(code, message)
                if result.ok or code in NO_RETRY_ERROR_CODES or result.reason in _TERMINAL_REASONS:
                    return code, message, response_headers
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        return code, message, response_headers

    def _judge_probe(self, code: int, message: str) -> CheckResult:
        """Judge the zero-cost probe against the live-probed Brave taxonomy.

        The error CODE decides first (it is what Brave's own classification
        keys on), then the status. Undecidable answers fall to UNKNOWN, which
        the check stage files as invalid rather than pushing an unproven key.
        """
        message = trim(message)
        data = _json_body(message)
        error_code = _error_code(data)

        if code <= 0:
            # No HTTP answer: transport failure, never a key verdict.
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        if error_code == "SUBSCRIPTION_TOKEN_INVALID":
            return CheckResult.fail(ErrorReason.INVALID_KEY, status_code=code)

        if error_code == "QUOTA_LIMITED":
            return CheckResult.fail(ErrorReason.NO_QUOTA, status_code=code)

        if error_code == "RATE_LIMITED":
            return CheckResult.fail(ErrorReason.RATE_LIMITED, status_code=code)

        if error_code == "VALIDATION":
            if _validation_flags_subscription_token(data):
                return CheckResult.fail(ErrorReason.INVALID_KEY, status_code=code)

            # The key authenticated; the complaint is about the missing q, so
            # this answer proves validity without ever being billed.
            if code == 422:
                return CheckResult.success(message="Brave probe authenticated key (unbilled validation error)")

            return CheckResult.fail(ErrorReason.UNKNOWN, status_code=code)

        if code == 200:
            # Defensive: a q-less probe normally never answers 200. A body that
            # is not JSON is an interstitial, never proof of a valid key.
            if data is None:
                return CheckResult.fail(ErrorReason.UNKNOWN, status_code=code)

            return CheckResult.success(message="Brave probe accepted key")

        if code in (401, 403):
            # A JSON envelope is Brave rejecting the key; a non-JSON body is a
            # Cloudflare/geo-block interstitial — an exit problem, not a dead
            # key, so it must not burn the candidate.
            if _has_error_envelope(data):
                return CheckResult.fail(ErrorReason.INVALID_KEY, status_code=code)

            return CheckResult.fail(ErrorReason.NO_ACCESS, status_code=code)

        if code == 402:
            return CheckResult.fail(ErrorReason.NO_QUOTA, status_code=code)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED, status_code=code)

        if code == 400:
            return CheckResult.fail(ErrorReason.BAD_REQUEST, status_code=code)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR, status_code=code)

        return CheckResult.fail(ErrorReason.UNKNOWN, status_code=code)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """Harvest the month-window quota the probe response advertises.

        Brave publishes quota in the positional ``X-RateLimit-*`` headers, so
        the audit costs no extra request. Error answers usually carry no such
        headers — their absence yields no quota items and never an exception.

        The response BODY is deliberately not flattened into the audit the way
        Tavily's /usage payload is: a header-validation entry can echo the
        submitted token value, and audit items are written to disk.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return []

        code, message, response_headers = self._probe(headers, address=address, endpoint=endpoint)

        items: List[str] = [f"{label}: {value}" for label, value in self._quota_items(response_headers)]
        if code > 0:
            items.append(f"probe.status_code: {code}")

            error_code = _error_code(_json_body(trim(message)))
            if error_code:
                items.append(f"probe.error_code: {error_code}")

        return items

    @classmethod
    def _quota_items(cls, headers: Mapping[str, str]) -> List[Tuple[str, Any]]:
        """Flatten the month window into audit pairs ([] when not advertised)."""
        window = cls._month_window(headers)
        return [(f"quota.{key}", window[key]) for key in sorted(window)]

    @classmethod
    def _month_window(cls, headers: Mapping[str, str]) -> Dict[str, Any]:
        """Parse Brave's month quota from the positional X-RateLimit-* headers.

        ``X-RateLimit-Policy`` fixes the window order and Limit/Remaining/Reset
        are comma lists paired with it by position. Anything absent, short or
        unparseable yields {} — the harvest is best-effort.
        """
        index = cls._month_window_index(trim(str(headers.get("x-ratelimit-policy", ""))))
        if index is None:
            return {}

        remaining = _positional_int(headers.get("x-ratelimit-remaining"), index)
        if remaining is None:
            return {}

        window: Dict[str, Any] = {"remaining": remaining, "window_seconds": _MONTH_WINDOW_SECONDS}

        limit = _positional_int(headers.get("x-ratelimit-limit"), index)
        if limit is not None:
            # Brave advertises 0 as an unlimited month plan.
            window["limit"] = "unlimited" if limit == 0 else limit

        reset = _positional_int(headers.get("x-ratelimit-reset"), index)
        if reset is not None:
            window["reset"] = reset

        return window

    @staticmethod
    def _month_window_index(policy: str) -> Optional[int]:
        """Positional index of the ``w=2592000`` month window in the policy."""
        for index, spec in enumerate(policy.split(",")):
            for part in spec.split(";"):
                part = part.strip()
                if part.lower().startswith("w=") and _to_int(part[2:]) == _MONTH_WINDOW_SECONDS:
                    return index

        return None


register_provider("brave", BraveProvider)
