#!/usr/bin/env python3

"""
Alibaba ModelScope (魔搭社区) provider implementation.

Token validation probes the REAL hub account endpoint:
``GET https://modelscope.cn/openapi/v1/users/me`` with
``Authorization: Bearer <token>`` (single CN hub endpoint; direct egress,
``use_proxy: false`` in the scan preset).

Traps (do not use for validation):
- ``GET https://api-inference.modelscope.cn/v1/models`` is presence-only — it
  answers 200 for ANY key (including invalid ones), so it is never used.
- ``POST /api/v1/login`` returns HTTP 400 + business code ``10010103009`` for
  invalid tokens — read-only-tier subtleties keep it out of the primary gate.
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


class ModelScopeProvider(OpenAILikeProvider):
    """Alibaba ModelScope provider; validates hub tokens via users/me."""

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "modelscope",
                "base_url": "https://modelscope.cn/openapi/v1",
                "completion_path": "/users/me",
                "model_path": "/users/me",
                "default_model": "modelscope-account",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _me_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url.rstrip("/") + "/"
        path = trim(endpoint) or self.model_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check ModelScope token validity against the hub users/me endpoint."""
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        url = self._me_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)
        headers = self._get_headers(token=token) or {}

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "GET",
                    url,
                    headers=headers,
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    return self._judge(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge(code, message)
                if code in NO_RETRY_ERROR_CODES or result.reason in {
                    ErrorReason.INVALID_KEY,
                    ErrorReason.NO_ACCESS,
                    ErrorReason.RATE_LIMITED,
                }:
                    return result
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code == 0:
            logger.debug(f"Check ModelScope hub account failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge(code, message)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge the ModelScope hub users/me response."""
        message = trim(message)

        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            # Require BOTH a true ``success`` flag AND a ``data`` dict:
            # other ModelScope endpoints answer 200 for any key.
            if (
                isinstance(data, dict)
                and data.get("success") is True
                and isinstance(data.get("data"), dict)
            ):
                return CheckResult.success(message="ModelScope hub users/me accepted key")

            return CheckResult.fail(ErrorReason.UNKNOWN)

        text = self._message_text(message)

        if code == 401 or re.findall(r"InvalidAuthentication|invalid.*authorization", text, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403 or re.findall(r"forbidden", text, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 429 or re.findall(r"rate.?limit|too many", text, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code >= 500:
            return CheckResult.fail(ErrorReason.NETWORK_ERROR)

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
        """hub token has no model enumeration; users/me returns a user profile."""
        return []


register_provider("modelscope", ModelScopeProvider)