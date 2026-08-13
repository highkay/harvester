#!/usr/bin/env python3

"""
QianFan provider implementation.
"""

import urllib.parse
from typing import List

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import chat
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider


class QianFanProvider(OpenAILikeProvider):
    """QianFan provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Set QianFan specific defaults
        self.defaults(
            kwargs,
            {
                "name": "qianfan",
                "base_url": "https://qianfan.baidubce.com",
                "completion_path": "/v2/chat/completions",
                "model_path": "/v2/models",
                "default_model": "ernie-4.0-8k-latest",
                "endpoint_pattern": r"[a-z0-9]{8}(?:-[a-z0-9]{4}){3}-[a-z0-9]{12}",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge QianFan API response."""
        if code == 404:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        return super()._judge(code, message)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check QianFan token validity."""
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        endpoint = trim(endpoint)
        if endpoint:
            headers["appid"] = endpoint

        model = trim(model) or self._default_model
        url = urllib.parse.urljoin(self._base_url, self.completion_path)

        code, message = chat(
            url=url,
            headers=headers,
            model=model,
            retries=self._get_retries(default=2),
            timeout=self._get_timeout(default=10),
            use_proxy=self._get_use_proxy(),
        )
        return self._judge(code=code, message=message)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available QianFan models."""
        headers = self._get_headers(token=token)
        if not headers:
            return []

        endpoint = trim(endpoint)
        if endpoint:
            headers["appid"] = endpoint

        url = urllib.parse.urljoin(self._base_url, self.model_path)
        return self._fetch_models(url=url, headers=headers)


register_provider("qianfan", QianFanProvider)
