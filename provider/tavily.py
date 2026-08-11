#!/usr/bin/env python3

"""
Tavily provider implementation.
"""

import json
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

from constant.system import NO_RETRY_ERROR_CODES
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_error_message, http_error_status, http_get, request
from tools.logger import get_logger
from tools.utils import trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")


class TavilyProvider(AIBaseProvider):
    """Tavily API provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        config = self.extract(
            kwargs,
            {
                "name": "tavily",
                "base_url": "https://api.tavily.com",
                "completion_path": "/search",
                "model_path": "/usage",
                "default_model": "tavily-search",
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
        """Get headers for Tavily API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        return self._merge_headers(headers, additional)

    def _usage_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.model_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Tavily token validity with the lightweight usage endpoint."""
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        url = self._usage_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request("GET", url, headers=headers, timeout=timeout) as response:
                    return self._judge_usage(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_usage(code, message)
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
            logger.debug(f"Check Tavily usage failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_usage(code, message)

    def _judge_usage(self, code: int, message: str) -> CheckResult:
        """Judge Tavily usage endpoint response."""
        message = trim(message)
        text = self._message_text(message)

        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            if isinstance(data, dict):
                return CheckResult.success(message="Tavily usage endpoint accepted key")

            return CheckResult.fail(ErrorReason.UNKNOWN)

        if code == 401 or re.findall(r"invalid\s+(api\s+)?key|unauthorized|unauthenticated", text, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 402 or re.findall(r"insufficient|quota|credit|billing|usage\s+limit", text, flags=re.I):
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
        """Fetch Tavily key usage/account audit details."""
        headers = self._get_headers(token=token)
        if not headers:
            return []

        url = self._usage_url(address=address, endpoint=endpoint)
        try:
            content = http_get(
                url=url,
                headers=headers,
                retries=self._get_retries(default=2),
                interval=1,
                timeout=self._get_timeout(default=10),
            )
        except Exception as e:
            logger.debug(f"Inspect Tavily usage failed: {e}")
            return []

        if not content:
            return []

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Tavily usage response: {content}")
            return []

        return self._format_audit_items(data)

    def _format_audit_items(self, data: Any) -> List[str]:
        items: List[str] = []
        self._flatten_audit("", data, items)
        return items

    def _flatten_audit(self, prefix: str, value: Any, items: List[str]) -> None:
        if len(items) >= 100:
            return

        if isinstance(value, dict):
            for key in sorted(value.keys()):
                name = f"{prefix}.{key}" if prefix else str(key)
                self._flatten_audit(name, value[key], items)
            return

        if isinstance(value, list):
            items.append(f"{prefix}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
            return

        items.append(f"{prefix}: {value}")


register_provider("tavily", TavilyProvider)
