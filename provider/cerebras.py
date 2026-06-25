#!/usr/bin/env python3

"""
Cerebras provider implementation.
"""

import json
import re
import time
import urllib.parse
from typing import List

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


class CerebrasProvider(OpenAILikeProvider):
    """Cerebras OpenAI-compatible provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "cerebras",
                "base_url": "https://api.cerebras.ai/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "gpt-oss-120b",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Cerebras token validity with the lightweight models endpoint."""
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        base_url = trim(address) or self._base_url
        url = urllib.parse.urljoin(base_url.removesuffix("/") + "/", self.model_path)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request("GET", url, headers=headers, timeout=timeout) as response:
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
            logger.debug(f"Check Cerebras models failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_models(code, message)

    def _judge_models(self, code: int, message: str) -> CheckResult:
        """Judge Cerebras models.list response."""
        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            models = data.get("data", []) if isinstance(data, dict) else []
            if isinstance(models, list):
                return CheckResult.success()

            return CheckResult.fail(ErrorReason.UNKNOWN)

        return self._judge(code=code, message=message)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Cerebras API response."""
        message = trim(message)

        if code in (401, 403) and re.findall(r"invalid|incorrect|unauthorized|forbidden", message, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402 or re.findall(r"insufficient|quota|credits?|billing", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 404 and re.findall(r"model|not\s+found", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_MODEL)

        return super()._judge(code, message)


register_provider("cerebras", CerebrasProvider)
