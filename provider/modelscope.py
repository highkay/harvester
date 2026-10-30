#!/usr/bin/env python3

"""
Alibaba ModelScope (魔搭社区) provider implementation.

Validation is two-stage. The account gate ``GET
https://modelscope.cn/openapi/v1/users/me`` (``Authorization: Bearer <token>``,
single CN hub endpoint; direct egress, ``use_proxy: false`` in the scan preset)
is followed by ONE minimal chat completion against the official API-Inference
endpoint ``POST https://api-inference.modelscope.cn/v1/chat/completions``
(``max_tokens=1``). A hub token only counts as valid if it can actually serve
inference — measured 2026-10-29/30: 4/4 pooled keys passed the account gate but
0/4 could complete a chat, so the account gate alone over-counts for the pool.

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
    """Alibaba ModelScope provider; validates hub tokens via users/me + chat probe."""

    # Official API-Inference (OpenAI-compatible) endpoint and probe model.
    # The account gate only proves the token exists; the pool needs keys that
    # can serve a chat completion. Model id verified 2026-10-30 from the public
    # API-Inference docs example (https://modelscope.cn/docs/model-service/
    # API-Inference/intro) cross-checked against the live hosted catalog at
    # GET api-inference.modelscope.cn/v1/models (presence-only — see module doc).
    _INFERENCE_BASE_URL = "https://api-inference.modelscope.cn/v1"
    _INFERENCE_COMPLETION_PATH = "chat/completions"
    _PROBE_MODEL = "Qwen/Qwen3.5-35B-A3B"

    def __init__(self, conditions: list[Condition], **kwargs):
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
        """Check ModelScope token validity: hub account gate + inference probe."""
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        account = self._check_account(token=token, address=address, endpoint=endpoint)
        if not account.available:
            return account

        # The hub accepted the token, but that does not prove the key can serve
        # inference (measured 2026-10-29/30: 4/4 pooled keys passed the gate, 0/4
        # completed a chat). Probe one minimal chat completion before counting it.
        return self._check_inference(token=token, model=model)

    def _check_account(self, token: str, address: str = "", endpoint: str = "") -> CheckResult:
        """Check the hub users/me endpoint (account gate, unchanged semantics)."""
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
            except requests.exceptions.RequestException as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code == 0:
            logger.debug(f"Check ModelScope hub account failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge(code, message)

    def _check_inference(self, token: str, model: str = "") -> CheckResult:
        """Probe ONE minimal chat completion to confirm the key serves inference."""
        headers = self._get_headers(token=token) or {}
        model = trim(model) or self._PROBE_MODEL
        url = urllib.parse.urljoin(
            self._INFERENCE_BASE_URL.removesuffix("/") + "/",
            self._INFERENCE_COMPLETION_PATH,
        )
        payload = json.dumps(
            {
                "model": model,
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }
        ).encode("utf8")

        code, message = 0, ""
        try:
            with request(
                "POST",
                url,
                data=payload,
                headers=headers,
                timeout=self._get_timeout(default=10),
                use_proxy=self._get_use_proxy(),
            ) as response:
                code = response.status_code
                message = response.text
        except requests.exceptions.HTTPError as e:
            code = http_error_status(e)
            message = http_error_message(e)
        except requests.exceptions.Timeout:
            code, message = 0, "timeout"
        except requests.exceptions.RequestException as e:
            code, message = 0, str(e)

        if code == 0:
            logger.debug(f"Check ModelScope inference failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_chat(code, message)

    def _judge_chat(self, code: int, message: str) -> CheckResult:
        """Judge the ModelScope API-Inference chat-completion response.

        Only a key that can actually serve inference counts as valid. Retryable
        faults (timeout / transport / empty-or-garbage 200) land in the
        wait-check bucket — never UNKNOWN (a permanent invalid-keys discard).
        """
        message = trim(message)

        if code == 200:
            if not message:
                # Empty 200 body: transport artefact, not a key verdict.
                return CheckResult.fail(ErrorReason.SERVER_ERROR)
            try:
                data = json.loads(message)
            except ValueError:
                # Unparseable 200 body (captive portal, truncated proxy): no
                # verdict possible -> retryable wait, never UNKNOWN.
                return CheckResult.fail(ErrorReason.SERVER_ERROR)
            if isinstance(data, dict):
                error = data.get("error")
                error_text = ""
                if isinstance(error, str):
                    error_text = error.lower()
                elif isinstance(error, dict) and error:
                    error_text = json.dumps(error, ensure_ascii=False).lower()
                if error_text:
                    if re.findall(r"invalid api key|incorrect api key|unauthorized|authentication", error_text):
                        return CheckResult.fail(ErrorReason.INVALID_KEY)
                    if re.findall(r"insufficient|quota|billing|credits|balance", error_text):
                        return CheckResult.fail(ErrorReason.NO_QUOTA)
                    return CheckResult.fail(ErrorReason.BAD_REQUEST)
            return CheckResult.success()

        text = self._message_text(message)

        if code == 401:
            # A token the hub accepted but which has no API-Inference permission
            # is useless to the pool: classify as INVALID_KEY, not NO_ACCESS.
            # Semantic decision user-approved 2026-10-30.
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403:
            # Same as 401: no inference permission = no pool value.
            # Semantic decision user-approved 2026-10-30.
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 429:
            if re.findall(r"quota|insufficient|billing|recharge|欠费|请充值", text, flags=re.IGNORECASE):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 404:
            # The probe model id disappeared upstream -> wait-check, not invalid.
            return CheckResult.fail(ErrorReason.NO_MODEL)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        # Any other unexpected status: no key verdict possible -> wait-check.
        return CheckResult.fail(ErrorReason.SERVER_ERROR)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge the ModelScope hub users/me response."""
        message = trim(message)

        if code == 200:
            try:
                data = json.loads(message)
            except ValueError:
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

        if code == 401 or re.findall(r"InvalidAuthentication|invalid.*authorization", text, flags=re.IGNORECASE):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403 or re.findall(r"forbidden", text, flags=re.IGNORECASE):
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 429 or re.findall(r"rate.?limit|too many", text, flags=re.IGNORECASE):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code >= 500:
            return CheckResult.fail(ErrorReason.NETWORK_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    @staticmethod
    def _message_text(message: str) -> str:
        try:
            data = json.loads(message)
        except ValueError:
            return message

        if not isinstance(data, dict):
            return str(data)

        parts = []
        for field in ("error", "message", "detail"):
            value = data.get(field)
            if value:
                parts.append(str(value))

        return " ".join(parts) or json.dumps(data, ensure_ascii=False, sort_keys=True)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> list[str]:
        """hub token has no model enumeration; users/me returns a user profile."""
        return []


register_provider("modelscope", ModelScopeProvider)