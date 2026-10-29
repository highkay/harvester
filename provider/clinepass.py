#!/usr/bin/env python3

"""
ClinePass (api.cline.bot) provider implementation.
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


class ClinepassProvider(OpenAILikeProvider):
    """ClinePass OpenAI-compatible provider implementation.

    ``GET /api/v1/models`` is a PUBLIC trap (measured 2026-10-29 in the
    wave-0 CN probe pack): no-key and bad-key both receive the same
    41,064-byte 200 model list across both socks exits, so it cannot judge a
    key. Validation goes straight to a minimal chat completion
    (``max_tokens=1``): a bad key answers 401 ``{"error":"Unauthorized: ..."}``.
    """

    def __init__(self, conditions: list[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "clinepass",
                "base_url": "https://api.cline.bot/api/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                # Probe model id from the PUBLIC catalog
                # GET /api/v1/ai/cline/recommended-models (fetched
                # 2026-10-29): prefer a cline-free/* id — solar-mini4 is a
                # text model (the other cline-free id, nemotron-3.5-super-vl,
                # is vision-language). The catalog is public, so the id
                # proves nothing about any key.
                "default_model": "cline-free/solar-mini4",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check a ClinePass key with a minimal chat completion.

        The models list is never consulted: it answers the same 200 for
        missing and bad keys, so a presence check would pool dead keys.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        base_url = trim(address) or self._base_url
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)
        model = trim(model) or self._default_model

        return self._check_completion(base_url=base_url, headers=headers, model=model, timeout=timeout, retries=retries)

    def _check_completion(
        self, base_url: str, headers: dict, model: str, timeout: int, retries: int
    ) -> CheckResult:
        """Probe a minimal chat completion (``max_tokens=1``)."""
        url = urllib.parse.urljoin(base_url.removesuffix("/") + "/", self.completion_path.lstrip("/"))
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
            except requests.exceptions.RequestException as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code == 0:
            logger.debug(f"Check ClinePass completion failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge(code=code, message=message)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge the ClinePass chat-completion response.

        Retryable faults (5xx / timeout / garbage) always land in the
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
                # Unparseable 200 body (captive portal, truncated proxy):
                # no verdict possible -> retryable wait, never UNKNOWN.
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

        if code == 401:
            # Measured 2026-10-29: bad key AND no key both answer
            # 401 {"error":"Unauthorized: Please make sure you're using the
            # latest version of Cline and re-authenticate ..."}.
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402 or re.findall(r"insufficient|billing", message, flags=re.IGNORECASE):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            if re.findall(r"quota|insufficient|billing|recharge", message, flags=re.IGNORECASE):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 404:
            # The probe model id disappeared upstream -> wait-check, not invalid.
            return CheckResult.fail(ErrorReason.NO_MODEL)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        # Any other unexpected status: no key verdict possible -> wait-check.
        return CheckResult.fail(ErrorReason.SERVER_ERROR)


register_provider("clinepass", ClinepassProvider)
