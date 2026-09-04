#!/usr/bin/env python3

"""
Agnes AI provider implementation.

Agnes AI is an OpenAI-compatible gateway at ``https://apihub.agnes-ai.com/v1``
authenticated with ``sk-`` Bearer keys. Validation uses the minimal chat
completion probe (``max_tokens=1``) — NOT ``GET /models``, which is a
presence-only check that answers 200 for any Bearer. ``inspect()`` only
enumerates model IDs for keys that already passed ``check()``.
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

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")


class AgnesAIProvider(AIBaseProvider):
    """Agnes AI OpenAI-compatible gateway provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "agnes-ai",
                "base_url": "https://apihub.agnes-ai.com/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "agnes-2.5-flash",
            },
        )
        super().__init__(conditions=conditions, **kwargs)

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Agnes AI authenticates via a Bearer token on every endpoint."""
        headers = {"Authorization": f"Bearer {token}"}
        return self._merge_headers(headers, additional)

    def _completion_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.completion_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def _models_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.model_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Agnes AI key validity with a minimal chat-completion probe.

        A ``GET /models`` response is a presence-only signal (200 for any
        Bearer), so the completion probe is the sole validation gate.
        """
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        url = self._completion_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)
        headers = self._get_headers(token=token) or {}
        # The Agnes gateway 400s requests without an explicit JSON content type
        # (measured 2026-09-04) — unlike OpenAI-compatible endpoints that
        # tolerate a missing header for raw data= posts.
        headers["Content-Type"] = "application/json"

        payload = json.dumps(
            {
                "model": trim(model) or self._default_model,
                "stream": False,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
        ).encode("utf8")

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "POST",
                    url,
                    data=payload,
                    headers=headers,
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    return self._judge_chat(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_chat(code, message)
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
            logger.debug(f"Check Agnes AI completion failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_chat(code, message)

    def _judge_chat(self, code: int, message: str) -> CheckResult:
        """Judge the Agnes AI chat-completion response."""
        message = trim(message)

        if code == 200:
            try:
                json.loads(message)
            except Exception:
                # Proxies/gateways answer 200 with junk — not proof of validity
                return CheckResult.fail(ErrorReason.UNKNOWN)

            return CheckResult.success(message="Agnes AI API accepted key")

        text = self._message_text(message)

        if code == 401 or re.findall(r"无效的令牌|invalid\s+(api\s+key|token)", text, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402:
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

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
        """List model IDs available to a key.

        Never used for validation (``GET /models`` answers 200 for any Bearer);
        only enumerated for keys that already passed ``check()``.
        """
        token = trim(token)
        if not token:
            return []

        url = self._models_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=1)
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
                    code, message = response.status_code, response.text
                    break
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code != 200:
            logger.debug(f"Inspect Agnes AI models failed: {trim(message) or code}")
            return []

        try:
            data = json.loads(message)
        except Exception:
            return []

        if not isinstance(data, dict):
            return []

        models = data.get("data")
        if not isinstance(models, list):
            return []

        model_ids: List[str] = []
        for entry in models[:100]:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if model_id:
                model_ids.append(str(model_id))

        return model_ids


register_provider("agnes-ai", AgnesAIProvider)