#!/usr/bin/env python3

"""
StabilityAI provider implementation.
"""

import urllib.parse

import requests

from tools.logger import get_logger

from .registry import register_provider

logger = get_logger("provider")
import time
from typing import Dict, List, Optional, Tuple

from constant.system import NO_RETRY_ERROR_CODES
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_error_message, http_error_status, request
from tools.coordinator import get_user_agent
from tools.patterns import redact_api_key, redact_api_keys_in_text
from tools.utils import trim

from .base import AIBaseProvider


class StabilityAIProvider(AIBaseProvider):
    """StabilityAI provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Extract parameters with defaults
        config = self.extract(
            kwargs,
            {
                "name": "stabilityai",
                "base_url": "https://api.stability.ai",
                "completion_path": "/v2beta/stable-image/generate",
                "model_path": "",
                "default_model": "core",
            },
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
        """Get headers for StabilityAI API requests."""
        key = trim(token)
        if not key:
            return None

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "multipart/form-data",
            "Accept": "application/json",
        }
        return self._merge_headers(headers, additional)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check StabilityAI token validity."""

        def post_multipart(
            url: str, token: str, fields: Optional[Dict] = None, files: Optional[Dict] = None, retries: int = 3
        ) -> Tuple[int, str]:
            url, token = trim(url), trim(token)
            if not url or not token:
                return 401, ""

            if not isinstance(fields, dict):
                fields = dict()
            if not isinstance(files, dict):
                files = dict()

            multipart_files = {name: (None, value) for name, value in fields.items()}
            for name, value in files.items():
                filename, data = value
                multipart_files[name] = (filename, data, "application/octet-stream")

            # send request with retry
            code, message, attempt, retries = 401, "", 0, max(1, retries)
            while attempt < retries:
                try:
                    response = request(
                        "POST",
                        url,
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {token}",
                            "User-Agent": get_user_agent(),
                        },
                        files=multipart_files,
                        timeout=self._get_timeout(default=15),
                        use_proxy=self._get_use_proxy(),
                    )
                    code = response.status_code
                    message = response.text
                    response.close()
                    break
                except requests.exceptions.HTTPError as e:
                    code = http_error_status(e)
                    if code != 401:
                        message = http_error_message(e)
                        safe_message = redact_api_keys_in_text(message)

                        logger.error(
                            f"[chat] failed to request URL: {url}, token: {redact_api_key(token)}, status code: {code}, message: {safe_message}"
                        )

                    if code in NO_RETRY_ERROR_CODES:
                        break
                except Exception:
                    pass

                attempt += 1
                time.sleep(1)

            return code, message

        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        model = trim(model) or self._default_model
        url = f"{urllib.parse.urljoin(self._base_url, self.completion_path)}/{model}"
        fields = {"prompt": "Lighthouse on a cliff overlooking the ocean", "aspect_ratio": "3:2"}

        code, message = post_multipart(url=url, token=token, fields=fields, retries=self._get_retries(default=3))
        return self._judge(code=code, message=message)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available StabilityAI models."""
        return []


register_provider("stabilityai", StabilityAIProvider)
