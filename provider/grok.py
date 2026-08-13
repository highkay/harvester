#!/usr/bin/env python3

"""
xAI Grok provider implementation.
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


class GrokProvider(OpenAILikeProvider):
    """xAI Grok OpenAI-compatible provider implementation."""

    web_sso_pattern = re.compile(
        r"(?i)(?:\b(?:grok|xai|x_ai)?[_-]?"
        r"(?:sso|session|auth|id[_-]?token|access[_-]?token|refresh[_-]?token)\b\s*[:=]"
        r"|\b(?:__Secure-[A-Za-z0-9_.-]+|next-auth\.session-token)\s*=)"
    )

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "grok",
                "base_url": "https://api.x.ai/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "grok-4",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check xAI token validity with the lightweight models endpoint."""
        token = trim(token)
        if self._looks_like_web_sso_token(token):
            return CheckResult.fail(
                ErrorReason.NO_ACCESS,
                "Grok web/SSO token requires browser-context manual verification",
            )

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
                with request("GET", url, headers=headers, timeout=timeout, use_proxy=False) as response:
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
            logger.debug(f"Check xAI Grok models failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_models(code, message)

    @classmethod
    def _looks_like_web_sso_token(cls, token: str) -> bool:
        return bool(token and cls.web_sso_pattern.search(token))

    def _judge_models(self, code: int, message: str) -> CheckResult:
        """Judge xAI models.list response."""
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
        """Judge xAI Grok API response."""
        message = trim(message)

        if code in (400, 401, 403) and re.findall(
            r"invalid[_-]?api[_-]?key|invalid\s+api\s*key|api\s*key.*invalid|incorrect\s+api\s*key|unauthorized|forbidden",
            message,
            flags=re.I,
        ):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code in (401, 403) and re.findall(r"permission|access|not\s+allowed", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 402 or re.findall(r"insufficient|quota|credits?|billing|balance", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code in (400, 404) and re.findall(r"model|not\s+found", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_MODEL)

        if code == 429 or re.findall(r"rate\s*limit|too\s+many\s+requests", message, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        return super()._judge(code, message)


register_provider("grok", GrokProvider)
