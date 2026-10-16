#!/usr/bin/env python3

"""
Exa (exa.ai) provider implementation.

Wire facts mirrored from the live-probed Exa integration in the tavily-hikari
pool (``probe_exa_contents`` in
``src/server/handlers/admin_resources/api_keys_and_logs.rs``,
``classify_exa_failure`` in
``src/analysis/provider_failure_classification.rs`` and the pinned probe matrix
in ``src/server/tests/provider_admin_api_keys.rs``):

- Auth is the ``x-api-key`` header on ``https://api.exa.ai``.
- ``POST {base}/contents`` with an EMPTY JSON object ``{}`` is a ZERO-COST
  probe: authentication runs before request validation and Exa bills only 2xx
  answers, so the canonical ``422`` complaint that the body "must contain
  either ``ids`` or ``urls``" proves the key authenticated and is never
  billed. Validation therefore needs a single stage. ``POST /search`` IS
  billable and Exa credit is prepaid with no reset, so it must never be a
  probe.
- An invalid key answers ``401`` with a FLAT envelope
  (``{"requestId": ..., "error": "Invalid API key", "tag": "INVALID_API_KEY"}``).
  The decisive ``tag`` is a TOP-LEVEL string, NOT brave's nested
  ``error.code`` — reading the nested shape yields "" for every Exa answer,
  which drops a 401 into the "no error envelope" branch and files every dead
  key as wait-check instead of invalid.
- ``402`` = credit spent. Recovery is ``SuccessOnly`` in hikari's provider
  spec: there is NO calendar flip that ever refills it, so 402 under ANY tag
  is a permanently dead key — NO_QUOTA, never pushed.
- ``429`` (tag ``RATE_LIMIT_EXCEEDED``) = the key is valid but throttled.
- Exa publishes no ``X-RateLimit-*`` quota headers, so ``inspect()`` reports
  the probe's own status and tag and nothing else.
"""

import json
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

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

# Exa authenticates with this header — no Bearer scheme, no query parameter.
_API_KEY_HEADER = "x-api-key"

# Verdicts a retry cannot change, so the probe loop ends on them.
_TERMINAL_REASONS = frozenset({ErrorReason.INVALID_KEY, ErrorReason.NO_ACCESS, ErrorReason.NO_QUOTA})

# Exa's flat error tags (measured on the hikari mock of the live wire) mapped
# to verdicts. Used ONLY for statuses the judge table below does not pin, so a
# recognised tag still decides an otherwise undecidable 4xx.
_TAG_REASONS: Dict[str, ErrorReason] = {
    "INVALID_API_KEY": ErrorReason.INVALID_KEY,
    "NO_MORE_CREDITS": ErrorReason.NO_QUOTA,
    "API_KEY_BUDGET_EXCEEDED": ErrorReason.NO_QUOTA,
    "TEAM_BUDGET_EXCEEDED": ErrorReason.NO_QUOTA,
    "RATE_LIMIT_EXCEEDED": ErrorReason.RATE_LIMITED,
}


def _json_body(message: str) -> Any:
    """Parse a response body, returning None when it is not JSON."""
    try:
        return json.loads(message)
    except Exception:
        return None


def _error_tag(data: Any) -> str:
    """Exa's error tag, uppercase, from the FLAT top-level envelope.

    Deliberately NOT brave's nested ``error.code`` reader: Exa's ``error``
    field is a human-readable STRING and the machine-readable classifier sits
    beside it as ``tag``. A nested reader returns "" here and silently misfiles
    every invalid key.
    """
    if not isinstance(data, dict):
        return ""

    tag = data.get("tag")
    if not isinstance(tag, str):
        return ""

    return trim(tag).upper()


def _error_body(error: requests.exceptions.HTTPError) -> str:
    """The FULL error body, not ``http_error_message``'s 300-char truncation.

    The expected answer (422) arrives through this exception path because
    ``search.client.request`` calls ``raise_for_status``, and a truncated body
    can lose the decisive ``tag`` tail — which would file a live key as
    UNKNOWN -> invalid.
    """
    response = getattr(error, "response", None)
    try:
        text = response.text if response is not None else ""
    except Exception:
        text = ""

    return trim(text) or http_error_message(error)


class ExaProvider(AIBaseProvider):
    """Exa API provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        config = self.extract(
            kwargs,
            {
                "name": "exa",
                "base_url": "https://api.exa.ai",
                "completion_path": "/contents",
                "model_path": "/contents",
                "default_model": "exa-search",
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
        """Get headers for Exa API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            _API_KEY_HEADER: token,
        }
        return self._merge_headers(headers, additional)

    def _probe_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.completion_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def _probe_body(self) -> Dict[str, Any]:
        """The EMPTY JSON object — the whole zero-cost trick.

        Any ``ids`` / ``urls`` entry would make this a billable contents
        lookup on the candidate's own prepaid credit.
        """
        return {}

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Validate an Exa key with ONE zero-cost probe.

        The empty-body answer carries the complete taxonomy (module docstring),
        so there is no second stage: validating a candidate never spends that
        key's own credit.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        code, message = self._probe(headers, address=address, endpoint=endpoint)
        if code == 0:
            logger.debug(f"Check Exa probe failed: {message}")

        return self._judge_probe(code, message)

    def _probe(self, headers: Dict[str, str], address: str = "", endpoint: str = "") -> Tuple[int, str]:
        """Run the empty-body probe with the provider's retry budget.

        Returns ``(status_code, body)``. ``status_code == 0`` means no HTTP
        answer at all (timeout / connection failure), with the sentinel
        ``"timeout"`` in the body slot for a timeout.

        The EXPECTED answers are 4xx, and ``search.client.request`` calls
        ``raise_for_status`` — so the canonical 422 arrives as an HTTPError and
        the except branch below is the normal path, not an error path.
        """
        url = self._probe_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "POST",
                    url,
                    headers=headers,
                    json=self._probe_body(),
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    return response.status_code, response.text
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = _error_body(e)

                result = self._judge_probe(code, message)
                if result.ok or code in NO_RETRY_ERROR_CODES or result.reason in _TERMINAL_REASONS:
                    return code, message
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        return code, message

    def _judge_probe(self, code: int, message: str) -> CheckResult:
        """Judge the zero-cost probe against the live-probed Exa taxonomy.

        STATUS decides (that is what hikari's ``classify_exa_failure`` keys
        on); the flat ``tag`` only breaks ties for statuses the table does not
        pin, so an unparseable or tagless body still yields the status-driven
        verdict.
        """
        message = trim(message)
        tag = _error_tag(_json_body(message))

        if code <= 0:
            # No HTTP answer: transport failure, never a key verdict.
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        if code == 401:
            return CheckResult.fail(ErrorReason.INVALID_KEY, status_code=code)

        if code == 402:
            # ANY tag: prepaid credit with SuccessOnly recovery never refills
            # on a calendar, so this key is permanently dead — never push it.
            return CheckResult.fail(ErrorReason.NO_QUOTA, status_code=code)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED, status_code=code)

        if code == 403:
            # Envelope or not: a 403 is an access/exit verdict, never proof of
            # a dead key, and it routes to the recoverable wait pool.
            return CheckResult.fail(ErrorReason.NO_ACCESS, status_code=code)

        if code in (400, 422):
            # The key authenticated; the complaint is about the empty body, so
            # this answer proves validity without ever being billed. 422 is the
            # canonical measured answer.
            return CheckResult.success(message="Exa probe authenticated key (unbilled validation error)")

        if 200 <= code < 300:
            # Defensive: an empty-body probe cannot produce a billable 2xx.
            return CheckResult.success(message="Exa probe accepted key")

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR, status_code=code)

        reason = _TAG_REASONS.get(tag)
        if reason is not None:
            return CheckResult.fail(reason, status_code=code)

        # Unpinned 4xx/3xx (404, 405, 415, a redirect to a login page, ...):
        # undecidable, and NO_ACCESS routes to the recoverable wait pool while
        # UNKNOWN would permanently discard the key on a systematic path or
        # egress error.
        return CheckResult.fail(ErrorReason.NO_ACCESS, status_code=code)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """Report the probe's own evidence: status code and flat error tag.

        Exa publishes no ``X-RateLimit-*`` quota headers, so there is nothing
        to harvest and the audit costs no extra request. The response BODY is
        deliberately not flattened into the audit: audit items are written to
        disk and the body is the only place a rejected key's own material could
        be echoed back.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return []

        code, message = self._probe(headers, address=address, endpoint=endpoint)
        if code <= 0:
            return []

        items: List[str] = [f"probe.status_code: {code}"]

        tag = _error_tag(_json_body(trim(message)))
        if tag:
            items.append(f"probe.tag: {tag}")

        return items


register_provider("exa", ExaProvider)
