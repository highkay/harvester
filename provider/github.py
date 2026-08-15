#!/usr/bin/env python3

"""
GitHub token provider implementation.
"""

import re
import time
import urllib.parse
from typing import Dict, List, Optional

import requests

from constant.system import NO_RETRY_ERROR_CODES
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_error_message, http_error_status, request
from tools.coordinator import get_user_agent
from tools.logger import get_logger
from tools.utils import trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")


class GitHubTokenProvider(AIBaseProvider):
    """GitHub API token provider implementation.

    Validates GitHub API tokens (ghp_/gho_/ghu_/ghs_/ghr_/github_pat_/gh_
    prefixes) against the official `GET /user` endpoint with Bearer auth.
    GitHub exposes no model enumeration API, so inspect() returns nothing.
    """

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "github",
                "base_url": "https://api.github.com",
                "completion_path": "",
                "model_path": "",
                "default_model": "github",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for GitHub API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": get_user_agent(),
        }

        return self._merge_headers(headers, additional)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check GitHub token validity with the `GET /user` endpoint."""
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        url = urllib.parse.urljoin(trim(address) or self._base_url, "user")
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request("GET", url, headers=headers, timeout=timeout, use_proxy=self._get_use_proxy()) as response:
                    return self._judge_token(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_token(code, message)
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
            logger.debug(f"Check GitHub token failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_token(code, message)

    def _judge_token(self, code: int, message: str) -> CheckResult:
        """Judge GitHub `GET /user` response."""
        message = trim(message)

        if code == 200:
            return CheckResult.success()

        if code == 401:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403:
            if re.search(r"rate\s*limit", message, re.I):
                return CheckResult.fail(ErrorReason.RATE_LIMITED)
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available models. GitHub API has no model enumeration."""
        return []


register_provider("github", GitHubTokenProvider)
