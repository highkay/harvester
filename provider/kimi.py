#!/usr/bin/env python3

"""
Kimi (Moonshot AI) provider implementation.
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


class KimiProvider(OpenAILikeProvider):
    """Kimi (Moonshot AI) OpenAI-compatible provider implementation.

    Validates keys with the lightweight ``GET /v1/models`` endpoint. A 401 is
    an invalid key regardless of the body (``invalid_authentication_error`` or
    ``incorrect_api_key_error``); a 429 with an ``exceeded_current_quota_error``
    means the key is valid but the account has no quota/balance.
    """

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "kimi",
                "base_url": "https://api.moonshot.cn/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "kimi-k3",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Kimi token validity with a models gate + completion probe.

        The ``GET /v1/models`` endpoint returns 200 even for accounts with no
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

        # Models endpoint passed, but Moonshot returns 200 there for zero-balance
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
                with request("POST", url, data=payload, headers=headers, timeout=timeout, use_proxy=self._get_use_proxy()) as response:
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
            logger.debug(f"Check Kimi completion failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        if code == 200:
            # Do not believe the status line alone: read the body (see _judge).
            return self._judge(code=code, message=message)

        if code == 402:
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            lowered = trim(message).lower()
            if re.findall(r"quota|exceeded_current_quota", lowered):
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
                with request("GET", url, headers=headers, timeout=timeout, use_proxy=self._get_use_proxy()) as response:
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
            logger.debug(f"Check Kimi models failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_models(code, message)

    def _judge_models(self, code: int, message: str) -> CheckResult:
        """Judge Kimi models.list response."""
        if code == 200:
            body = trim(message)
            if not body:
                # An EMPTY 200 body is a transport/proxy artefact (a truncated
                # connection or a transparent proxy exit), NOT a key verdict.
                # The old path returned UNKNOWN, which CheckStage files in
                # invalid-keys.txt — a PERMANENT discard — and this branch
                # returns from inside the retry loop, so a single truncation
                # was final. SERVER_ERROR is retryable, so the key lands in the
                # recoverable wait-check-keys.txt bucket instead. Same fix as
                # OpenAILikeProvider._judge (review-kimi.md D1, 2026-10-29).
                return CheckResult.fail(ErrorReason.SERVER_ERROR)
            try:
                data = json.loads(body)
            except Exception:
                # Present-but-unparseable 200 (an HTML captive/error page served
                # by a proxy exit): no key verdict can be made here either, so
                # it is retryable for the same reason as the empty body above.
                return CheckResult.fail(ErrorReason.SERVER_ERROR)

            models = data.get("data", []) if isinstance(data, dict) else []
            if isinstance(models, list):
                return CheckResult.success()

            # Parsed JSON whose `data` is not a list: the body was read but the
            # verdict is genuinely unknowable — unchanged (UNKNOWN).
            return CheckResult.fail(ErrorReason.UNKNOWN)

        if code == 401:
            # Moonshot: invalid_authentication_error | incorrect_api_key_error
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402:
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            lowered = trim(message).lower()
            if re.findall(r"quota|exceeded_current_quota", lowered):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        return self._judge(code=code, message=message)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Kimi API response."""
        message = trim(message)

        if code == 200:
            # A 200 is not automatically a valid key: wrapper gateways answer
            # HTTP 200 + an error object, and the old blanket success pooled
            # that body as VALID, which web/push.py then ships to gpt-load.
            # Reuse the openai_like guard instead: it splits an error payload
            # by flavour (auth marker -> INVALID_KEY, quota marker -> NO_QUOTA,
            # anything else -> BAD_REQUEST, i.e. the recoverable wait-check
            # bucket), treats an empty body as a retryable transport artefact
            # and keeps a clean completion body as success
            # (review-kimi.md D2, 2026-10-29).
            result = super()._judge(code=code, message=message)
            if not result.available and result.reason == ErrorReason.UNKNOWN:
                # openai_like leaves a present-but-unparseable 200 (proxy
                # interstitial) as UNKNOWN -> invalid-keys.txt. _judge_models
                # classifies that same shape as retryable; keep the two probes
                # aligned so a hijacked chat answer cannot permanently burn a
                # key that authenticated on /models one call earlier.
                return CheckResult.fail(ErrorReason.SERVER_ERROR)
            return result

        # Auth passed, but the model id or plan is wrong. Docs + measurement on
        # api.kimi.com/coding/v1 (2026-09-21): an unknown model id answers 401
        # "Your model id does not exist, recognized as other:<id>", and a
        # plan-gated model answers 401 "... does not have access to <model>".
        # Neither means the key is invalid — route them to NO_MODEL so they land
        # in wait-check-keys.txt instead of being silently discarded as invalid.
        if code == 401 and re.findall(
            r"model id does not exist|recognized as other|does not have access to", message, flags=re.I
        ):
            return CheckResult.fail(ErrorReason.NO_MODEL)

        if code == 401 or re.findall(r"invalid_authentication|incorrect_api_key", message, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402 or re.findall(r"exceeded_current_quota|insufficient_quota", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        return super()._judge(code, message)


register_provider("kimi", KimiProvider)