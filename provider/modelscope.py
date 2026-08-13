#!/usr/bin/env python3

"""
Alibaba ModelScope (魔搭社区) provider implementation.

Single endpoint (CN): ``https://api-inference.modelscope.cn/v1``

Note: ModelScope's ``GET /v1/models`` is PUBLIC — it returns 200 even with no
or invalid keys (the model list is not gated). Authentication is therefore
verified directly with a minimal chat completion probe (``max_tokens=1``):
a 401 means invalid key (status-code-first, body may be non-JSON); a 402 /
429-quota / 400-Arrearage classifies the key as NO_QUOTA.
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
    """Alibaba ModelScope OpenAI-compatible provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "modelscope",
                "base_url": "https://api-inference.modelscope.cn/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "Qwen/Qwen3-8B",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check ModelScope token validity with a chat completion probe.

        ModelScope's ``GET /v1/models`` is public (returns 200 for invalid keys
        too), so authentication is verified directly via a minimal chat
        completion: 401 -> invalid key; 402 / 429-quota / 400-Arrearage ->
        NO_QUOTA.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        base_url = trim(address) or self._base_url
        url = urllib.parse.urljoin(base_url.removesuffix("/") + "/", self.completion_path)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)
        model = trim(model) or self._default_model

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
            logger.debug(f"Check ModelScope completion failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge(code=code, message=message)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge ModelScope chat probe response."""
        message = trim(message)

        if code == 200:
            return CheckResult.success()

        if code == 401 or re.findall(r"authentication failed|invalid.*token|unauthorized", message, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 402 or re.findall(
            r"arrearage|out_of_service|insufficient.*(?:quota|balance)|good standing|欠费|余额不足",
            message,
            flags=re.I,
        ):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 400:
            if re.findall(r"arrearage|out_of_service|good standing|欠费|余额", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        if code == 429:
            if re.findall(r"quota|throttling|insufficient|billing", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 403:
            if re.findall(r"model_not_found|不存在", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_MODEL)
            if re.findall(r"unauthorized|无权|已被封禁", message, flags=re.I):
                return CheckResult.fail(ErrorReason.INVALID_KEY)
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        return super()._judge(code, message)


register_provider("modelscope", ModelScopeProvider)
