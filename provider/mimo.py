#!/usr/bin/env python3

"""
Xiaomi MiMo provider implementation.

Two regional Token Plan endpoints (keys prefixed ``tp-``) plus a pay-as-you-go
endpoint (keys prefixed ``sk-``):

- mainland China:  https://token-plan-cn.xiaomimimo.com/v1
- Singapore:       https://token-plan-sgp.xiaomimimo.com/v1
- pay-as-you-go:   https://api.xiaomimimo.com/v1

``GET /models`` is available on the OpenAI-compatible endpoints, so keys are
validated with the lightweight models endpoint. A 401 is an invalid key
regardless of the body (status-code-first); a 429 with a quota error means
the key is valid but the account has no quota/balance.
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


class MiMoProvider(OpenAILikeProvider):
    """Xiaomi MiMo OpenAI-compatible provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "mimo",
                "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "mimo-v2.5-pro",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check MiMo token validity with a models gate + completion probe.

        The ``GET /models`` endpoint returns 200 even for accounts with no
        balance, so auth-valid keys are probed with a minimal chat completion
        (``max_tokens=1``): a 402 Payment Required (or a 429 with a quota
        error body) classifies the key as NO_QUOTA instead of valid.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        base_url = trim(address) or self._base_url
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        result = self._check_models(base_url=base_url, headers=headers, timeout=timeout, retries=retries)
        if not result.available:
            return result

        # Models endpoint passed, but MiMo returns 200 there for zero-balance
        # accounts too. Probe a real chat completion to surface 402.
        model = trim(model) or self._default_model
        return self._check_completion(base_url=base_url, headers=headers, model=model, timeout=timeout, retries=retries)

    def _check_completion(
        self, base_url: str, headers: dict, model: str, timeout: int, retries: int
    ) -> CheckResult:
        """Probe a minimal chat completion (``max_tokens=1``) to surface 402."""
        url = urllib.parse.urljoin(base_url.removesuffix("/") + "/", self.completion_path)
        payload = json.dumps(
            {
                "model": model,
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }
        ).encode("utf8")

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request("POST", url, data=payload, headers=headers, timeout=timeout, use_proxy=False) as response:
                    code = response.status_code
                    message = response.text
                    break
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)
                if code in NO_RETRY_ERROR_CODES:
                    break
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code == 0:
            logger.debug(f"Check MiMo completion failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        if code == 200:
            return CheckResult.success()

        if code == 402:
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            lowered = trim(message).lower()
            if re.findall(r"quota|exceeded_current_quota|insufficient", lowered):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        return self._judge(code=code, message=message)

    def _check_models(
        self, base_url: str, headers: dict, timeout: int, retries: int
    ) -> CheckResult:
        """Check token authentication with the lightweight models endpoint."""
        url = urllib.parse.urljoin(base_url.removesuffix("/") + "/", self.model_path)

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
            logger.debug(f"Check MiMo models failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_models(code, message)

    def _judge_models(self, code: int, message: str) -> CheckResult:
        """Judge MiMo models.list response."""
        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            models = data.get("data", []) if isinstance(data, dict) else []
            if isinstance(models, list):
                return CheckResult.success()

            return CheckResult.fail(ErrorReason.UNKNOWN)

        if code == 401:
            # MiMo: {"error": {"message": "Invalid API Key", "type": "invalid_key"}}
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402:
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            lowered = trim(message).lower()
            if re.findall(r"quota|exceeded_current_quota|insufficient", lowered):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        return self._judge(code=code, message=message)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge MiMo API response."""
        message = trim(message)

        if code == 200:
            return CheckResult.success()

        if code == 401 or re.findall(r"invalid_api_key|invalid key", message, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402 or re.findall(r"exceeded_current_quota|insufficient_quota", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        return super()._judge(code, message)


register_provider("mimo", MiMoProvider)
