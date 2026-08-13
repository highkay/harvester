#!/usr/bin/env python3

"""
OpenAI-like provider base class.
"""

import json
import re
import urllib.parse

from tools.logger import get_logger

logger = get_logger("provider")
from typing import Dict, List, Optional

from constant.system import DEFAULT_COMPLETION_PATH, DEFAULT_MODEL_PATH
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_get
from tools.coordinator import get_user_agent
from tools.utils import handle_exceptions, trim

from .base import AIBaseProvider
from .registry import register_provider


class OpenAILikeProvider(AIBaseProvider):
    """Base class for OpenAI-compatible providers."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Extract required parameters without defaults
        name = trim(kwargs.pop("name", ""))
        base_url = trim(kwargs.pop("base_url", ""))
        default_model = trim(kwargs.pop("default_model", ""))

        # Validate required parameters
        if not name:
            raise ValueError("OpenAILike provider requires 'name' parameter to be specified")
        if not base_url:
            raise ValueError(f"OpenAILike provider {name} requires 'base_url' parameter to be specified")
        if not default_model:
            raise ValueError(f"OpenAILike provider {name} requires 'default_model' parameter to be specified")

        # Extract optional parameters with defaults
        config = self.extract(
            kwargs,
            {
                "completion_path": DEFAULT_COMPLETION_PATH,
                "model_path": DEFAULT_MODEL_PATH,
            },
        )

        # Add the validated required parameters back to config
        config.update(
            {
                "name": name,
                "base_url": base_url,
                "default_model": default_model,
            }
        )

        super().__init__(
            config["name"],
            config["base_url"],
            config["completion_path"],
            config["model_path"],
            config["default_model"],
            conditions,
            **kwargs,
        )

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for OpenAI-like API requests."""
        token = trim(token)
        if not token:
            return None

        if not isinstance(additional, dict):
            additional = {}

        auth_key = (trim(self.extras.get("auth_key", None)) if isinstance(self.extras, dict) else "") or "authorization"
        auth_value = f"Bearer {token}" if auth_key.lower() == "authorization" else token

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            auth_key: auth_value,
            "user-agent": get_user_agent(),
        }

        return self._merge_headers(headers, additional)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge OpenAI-like API response."""
        if code == 200:
            try:
                data = json.loads(trim(message))
                if data and isinstance(data, dict):
                    error = data.get("error", None)
                    if error and isinstance(error, dict):
                        error_type = trim(error.get("type", ""))
                        error_reason = trim(error.get("message", "")).lower()

                        if error_type or "authorization" in error_reason:
                            return CheckResult.fail(ErrorReason.INVALID_KEY)
            except:
                logger.error(f"Failed to parse response, domain: {self._base_url}, message: {message}")
                return CheckResult.fail(ErrorReason.UNKNOWN)

            return CheckResult.success()

        message = trim(message)
        if message:
            if code == 403:
                if re.findall(r"model_not_found", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.NO_MODEL)
                elif re.findall(r"unauthorized|已被封禁", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.INVALID_KEY)
                elif re.findall(r"unsupported_country_region_territory|该令牌无权访问模型", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.NO_ACCESS)
                elif re.findall(
                    r"exceeded_current_quota_error|insufficient_user_quota|(额度|余额)(不足|过低)", message, flags=re.I
                ):
                    return CheckResult.fail(ErrorReason.NO_QUOTA)
            elif code == 429:
                if re.findall(r"insufficient_quota|billing_not_active|欠费|请充值|recharge", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.NO_QUOTA)
                elif re.findall(r"rate_limit_exceeded", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.RATE_LIMITED)
            elif code == 503 and re.findall(r"无可用渠道", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_MODEL)

        return super()._judge(code, message)

    @handle_exceptions(default_result=[], log_level="warning")
    def _fetch_models(self, url: str, headers: Dict) -> List[str]:
        """Fetch models from API endpoint."""
        url = trim(url)
        if not url:
            return []

        content = http_get(url=url, headers=headers, interval=1, use_proxy=self._get_use_proxy())
        if not content:
            return []

        result = json.loads(content)
        return [trim(x.get("id", "")) for x in result.get("data", [])]

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available models from OpenAI-like API."""
        headers = self._get_headers(token=token)
        if not headers or not self._base_url or not self.model_path:
            return []

        url = urllib.parse.urljoin(self._base_url, self.model_path)
        return self._fetch_models(url=url, headers=headers)


register_provider("openai_like", OpenAILikeProvider)
