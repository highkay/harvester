#!/usr/bin/env python3

"""
Google Gemini provider implementation.
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
from search.client import http_error_message, http_error_status, http_get, request
from tools.logger import get_logger
from tools.utils import trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")


class GeminiProvider(AIBaseProvider):
    """Google Gemini provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Extract parameters with defaults
        config = self.extract(
            kwargs,
            {
                "name": "gemini",
                "base_url": "https://generativelanguage.googleapis.com",
                "completion_path": "/v1beta/models",
                "model_path": "/v1beta/models",
                "default_model": "gemini-3.5-flash",
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
        """Get headers for Gemini API requests."""
        headers = {"accept": "application/json", "content-type": "application/json"}
        token = trim(token)
        if token:
            headers["x-goog-api-key"] = token
        return self._merge_headers(headers, additional)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Gemini API response."""
        if code == 200:
            return CheckResult.success()

        message = trim(message)
        if code == 400:
            if re.findall(r"API_KEY_INVALID|API key expired|API key not valid", message, flags=re.I):
                return CheckResult.fail(ErrorReason.INVALID_KEY)
            elif re.findall(r"FAILED_PRECONDITION|SERVICE_DISABLED|BILLING_DISABLED", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_ACCESS)
            elif re.findall(r"NOT_FOUND|model.*not\s+found", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_MODEL)
        elif code == 403:
            if re.findall(
                r"PERMISSION_DENIED|Your API key was reported as leaked|API_KEY_SERVICE_BLOCKED",
                message,
                flags=re.I,
            ):
                return CheckResult.fail(ErrorReason.INVALID_KEY)
            elif re.findall(r"disabled|billing|permission", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_ACCESS)
        elif code == 404:
            if re.findall(r"NOT_FOUND|model.*not\s+found", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_MODEL)
        elif code == 429:
            if re.findall(r"RESOURCE_EXHAUSTED|Quota exceeded|quotaExceeded|quota metric", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_QUOTA)

        return super()._judge(code, message)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Gemini token validity with the lightweight models endpoint."""
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        base_url = trim(address) or self._base_url
        url = urllib.parse.urljoin(base_url, self.model_path)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "GET",
                    url,
                    headers=self._get_headers(token=token),
                    params={"pageSize": 1},
                    timeout=timeout,
                    use_proxy=False,
                ) as response:
                    return self._judge_models(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_models(code, message)
                if code in NO_RETRY_ERROR_CODES or result.reason in {
                    ErrorReason.INVALID_KEY,
                    ErrorReason.NO_ACCESS,
                    ErrorReason.NO_QUOTA,
                    ErrorReason.NO_MODEL,
                }:
                    return result
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code == 0:
            logger.debug(f"Check Gemini models failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_models(code, message)

    def _judge_models(self, code: int, message: str) -> CheckResult:
        """Judge Gemini models.list response."""
        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            models = data.get("models", []) if isinstance(data, dict) else []
            if isinstance(models, list):
                return CheckResult.success()

            return CheckResult.fail(ErrorReason.UNKNOWN)

        return self._judge(code=code, message=message)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available Gemini models."""
        token = trim(token)
        if not token:
            return []

        base_url = trim(address) or self._base_url
        url = urllib.parse.urljoin(base_url, self.model_path)
        content = http_get(url=url, headers=self._get_headers(token=token), interval=1, use_proxy=False)
        if not content:
            return []

        try:
            data = json.loads(content)
            models = data.get("models", [])
            return [x.get("name", "").removeprefix("models/") for x in models]
        except (json.JSONDecodeError, AttributeError):
            logger.error(f"Failed to parse models from response: {content}")
            return []


register_provider("gemini", GeminiProvider)
