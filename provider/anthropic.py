#!/usr/bin/env python3

"""
Anthropic provider implementation.
"""

import json
import re
import time
import traceback
import urllib.parse
from typing import Dict, List, Optional

import requests

from constant.system import NO_RETRY_ERROR_CODES
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_error_message, http_error_status, http_get, request
from tools.coordinator import get_user_agent
from tools.logger import get_logger
from tools.utils import handle_exceptions, trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")


class AnthropicProvider(AIBaseProvider):
    """Anthropic provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Extract parameters with defaults
        config = self.extract(
            kwargs,
            {
                "name": "anthropic",
                "base_url": "https://api.anthropic.com",
                "completion_path": "/v1/messages",
                "model_path": "/v1/models",
                "default_model": "claude-sonnet-4-20250514",
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
        """Get headers for Anthropic API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
        }
        return self._merge_headers(headers, additional)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Anthropic token validity."""
        token = trim(token)
        if token.startswith("sk-ant-sid01-"):
            # Handle Claude session tokens
            url = "https://api.claude.ai/api/organizations"
            headers = {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
                "cache-control": "max-age=0",
                "cookie": f"sessionKey={token}",
                "user-agent": get_user_agent(),
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
            }

            content, success = "", False
            attempt, retries, timeout = 0, self._get_retries(default=3), self._get_timeout(default=10)

            while attempt < retries:
                try:
                    with request("GET", url, headers=headers, timeout=timeout, use_proxy=self._get_use_proxy()) as response:
                        content = response.text
                        success = True
                        break
                except requests.exceptions.HTTPError as e:
                    code = http_error_status(e)
                    if code == 401:
                        return CheckResult.fail(ErrorReason.INVALID_KEY)
                    else:
                        content = http_error_message(e)

                        if code == 403:
                            message = ""
                            try:
                                data = json.loads(content)
                                message = data.get("error", {}).get("message", "")
                            except:
                                message = content

                            if re.findall(r"Invalid authorization", message, flags=re.I):
                                return CheckResult.fail(ErrorReason.INVALID_KEY)

                        if code in NO_RETRY_ERROR_CODES:
                            break
                except Exception as e:
                    if not isinstance(e, requests.exceptions.Timeout):
                        logger.error(f"Check Claude session error, key: {token}, message: {traceback.format_exc()}")

                attempt += 1
                time.sleep(1)

            if not content or re.findall(r"Invalid authorization", content, flags=re.I):
                return CheckResult.fail(ErrorReason.INVALID_KEY)
            elif not success:
                logger.error(f"Check Claude session error, key: {token}, message: {content}")
                return CheckResult.fail(ErrorReason.UNKNOWN)

            try:
                data = json.loads(content)
                valid = False
                if data and isinstance(data, list):
                    valid = trim(data[0].get("name", None)) != ""

                    capabilities = data[0].get("capabilities", [])
                    if capabilities and isinstance(capabilities, list) and "claude_pro" in capabilities:
                        logger.info(f"Found Claude Pro key: {token}")

                if not valid:
                    logger.warning(f"Check error, Anthropic session key: {token}, message: {content}")

                return CheckResult.success() if valid else CheckResult.fail(ErrorReason.INVALID_KEY)
            except:
                return CheckResult.fail(ErrorReason.INVALID_KEY)

        return super().check(token=token, address=address, endpoint=endpoint, model=model)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Anthropic API response."""
        message = trim(message)
        if re.findall(r"credit balance is too low|Billing|purchase", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)
        elif code == 404 and re.findall(r"not_found_error", trim(message), flags=re.I):
            return CheckResult.fail(ErrorReason.NO_MODEL)

        return super()._judge(code, message)

    @handle_exceptions(default_result=[], log_level="warning")
    def _fetch_models(self, url: str, headers: Dict) -> List[str]:
        """Fetch Anthropic models from the Models API."""
        url = trim(url)
        if not url:
            return []

        models: List[str] = []
        after_id = ""
        seen_pages = set()

        while True:
            params = {"limit": 100}
            if after_id:
                params["after_id"] = after_id

            content = http_get(
                url=url,
                headers=headers,
                params=params,
                interval=1,
                timeout=self._get_timeout(default=10),
                use_proxy=self._get_use_proxy(),
            )
            if not content:
                break

            result = json.loads(content)
            for item in result.get("data", []):
                model = trim(item.get("id", ""))
                if model:
                    models.append(model)

            if not result.get("has_more", False):
                break

            after_id = trim(result.get("last_id", ""))
            if not after_id or after_id in seen_pages:
                break

            seen_pages.add(after_id)

        return list(dict.fromkeys(models))

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available Anthropic models from the Models API."""
        headers = self._get_headers(token=token)
        if not headers or not self.model_path:
            return []

        base_url = trim(address) or self._base_url
        if not re.match(r"^https?://([\w\-_]+\.[\w\-_]+)+", base_url, flags=re.I):
            logger.error(f"Invalid domain: {base_url}, skipping model listing")
            return []

        url = urllib.parse.urljoin(base_url.removesuffix("/") + "/", self.model_path)
        return self._fetch_models(url=url, headers=headers)


register_provider("anthropic", AnthropicProvider)
