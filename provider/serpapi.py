#!/usr/bin/env python3

"""
SerpApi provider implementation.

Facts (verified live via serpapi.com, 2026-08-28):
- ``GET https://serpapi.com/account.json?api_key=<key>`` is the official
  Account API: free of charge and not counted toward the monthly quota.
- Invalid or missing key -> HTTP 401 ``{"error": "Invalid API key. ..."}``.
- Valid key -> HTTP 200 JSON with ``account_id`` / ``plan_name`` /
  ``account_status`` etc. Note the response ECHOES the ``api_key`` field,
  so ``inspect()`` drops it before flattening and success messages never
  include the response body.
- Trap: ``search.json`` returns 200 with real results even WITHOUT a key —
  the account endpoint is the only strict validation gate.
"""

import json
import re
import time
import urllib.parse
from typing import Dict, List, Optional

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

# Inspect must never surface the echoed key (or any sensitive field).
_SKIP_AUDIT_FIELDS = frozenset({"api_key"})


class SerpapiProvider(AIBaseProvider):
    """SerpApi search API provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "serpapi",
                "base_url": "https://serpapi.com",
                "completion_path": "/search.json",
                "model_path": "/account.json",
                "default_model": "serpapi-account",
            },
        )
        super().__init__(conditions=conditions, **kwargs)

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """SerpApi authenticates via the ``api_key`` query parameter, not headers."""
        headers = {"accept": "application/json"}
        return self._merge_headers(headers, additional)

    def _account_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.model_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check SerpApi key validity with the lightweight Account API endpoint."""
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        url = self._account_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        params = {"api_key": token}
        headers = self._get_headers(token=token) or {}

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    return self._judge_account(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_account(code, message)
                if code in NO_RETRY_ERROR_CODES or result.reason in {
                    ErrorReason.INVALID_KEY,
                    ErrorReason.NO_ACCESS,
                    ErrorReason.NO_QUOTA,
                }:
                    return result
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code == 0:
            logger.debug(f"Check SerpApi account failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_account(code, message)

    def _judge_account(self, code: int, message: str) -> CheckResult:
        """Judge the SerpApi account endpoint response."""
        message = trim(message)

        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            # Require account-specific fields: search.json also answers 200
            # without a key, so a bare 200 is NOT proof of key validity.
            if isinstance(data, dict) and any(
                data.get(field) for field in ("plan_name", "account_id", "account_status")
            ):
                return CheckResult.success(message="SerpApi account endpoint accepted key")

            return CheckResult.fail(ErrorReason.UNKNOWN)

        text = self._message_text(message)

        if code == 401 or re.findall(r"invalid\s+(api\s+)?key", text, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403 or re.findall(r"forbidden", text, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 402 or re.findall(r"insufficient|quota|credit|billing", text, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429 or re.findall(r"rate\s*limit|too\s+many\s+requests", text, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 400:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    @staticmethod
    def _message_text(message: str) -> str:
        try:
            data = json.loads(message)
        except Exception:
            return message

        if not isinstance(data, dict):
            return str(data)

        parts = []
        for field in ("error", "message", "detail"):
            value = data.get(field)
            if value:
                parts.append(str(value))

        return " ".join(parts) or json.dumps(data, ensure_ascii=False, sort_keys=True)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """Fetch SerpApi account/plan audit details (never the echoed key)."""
        token = trim(token)
        if not token:
            return []

        url = self._account_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=1)
        headers = self._get_headers(token=token) or {}

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "GET",
                    url,
                    params={"api_key": token},
                    headers=headers,
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    code, message = response.status_code, response.text
                    break
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code != 200:
            logger.debug(f"Inspect SerpApi account failed: {trim(message) or code}")
            return []

        try:
            data = json.loads(message)
        except Exception:
            return []

        if not isinstance(data, dict):
            return []

        return self._format_audit_items(data)

    def _format_audit_items(self, data: Dict) -> List[str]:
        items: List[str] = []
        self._flatten_audit("", data, items)
        return items

    def _flatten_audit(self, prefix: str, value: object, items: List[str]) -> None:
        if len(items) >= 100:
            return

        if isinstance(value, dict):
            for key, sub in value.items():
                if key in _SKIP_AUDIT_FIELDS:
                    continue
                label = f"{prefix}.{key}" if prefix else str(key)
                self._flatten_audit(label, sub, items)
            return

        if isinstance(value, list):
            joined = ", ".join(str(v) for v in value[:10])
            label = f"{prefix}: {joined}" if prefix else joined
            if label:
                items.append(label)
            return

        if value is not None and str(value) != "":
            label = f"{prefix}: {value}" if prefix else str(value)
            items.append(label)


register_provider("serpapi", SerpapiProvider)