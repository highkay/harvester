#!/usr/bin/env python3

"""
OpenRouter provider implementation.
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

from .openai_like import OpenAILikeProvider
from .registry import register_provider

logger = get_logger("provider")


class OpenRouterProvider(OpenAILikeProvider):
    """OpenRouter OpenAI-compatible provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "openrouter/free",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for OpenRouter API requests."""
        headers = super()._get_headers(token=token, additional=additional)
        if isinstance(headers, dict):
            headers.setdefault("X-Title", "harvester")
        return headers

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check OpenRouter token validity with the key metadata endpoint."""
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        url = urllib.parse.urljoin(self._base_url, "key")
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request("GET", url, headers=headers, timeout=timeout) as response:
                    return self._judge_key_info(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_key_info(code, message)
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
            logger.debug(f"Check OpenRouter key metadata failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_key_info(code, message)

    def _judge_key_info(self, code: int, message: str) -> CheckResult:
        """Judge OpenRouter key metadata response."""
        message = trim(message)

        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            key_info = data.get("data", {}) if isinstance(data, dict) else {}
            if not isinstance(key_info, dict):
                return CheckResult.fail(ErrorReason.UNKNOWN)

            remaining = key_info.get("limit_remaining")
            if isinstance(remaining, (int, float)) and remaining <= 0:
                return CheckResult.fail(ErrorReason.NO_QUOTA)

            limit = key_info.get("limit")
            usage = key_info.get("usage")
            if isinstance(limit, (int, float)) and isinstance(usage, (int, float)) and usage >= limit:
                return CheckResult.fail(ErrorReason.NO_QUOTA)

            return CheckResult.success()

        if code in (401, 403):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402 or re.findall(r"insufficient\s+credits?|quota|billing", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429 or re.findall(r"rate\s*limit", message, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge OpenRouter API response."""
        message = trim(message)

        if code in (401, 403) and re.findall(
            r"no\s+auth|invalid\s+authorization|invalid\s+api\s+key|unauthorized|user\s+not\s+found",
            message,
            flags=re.I,
        ):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402 or re.findall(r"insufficient\s+credits?|quota|billing", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code in (404, 400) and re.findall(r"model.*not\s+found|no\s+endpoints?\s+found", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_MODEL)

        if code == 429 or re.findall(r"rate\s*limit", message, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        return super()._judge(code, message)


register_provider("openrouter", OpenRouterProvider)
