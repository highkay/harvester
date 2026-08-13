#!/usr/bin/env python3

"""
Base provider class for AI service providers.
"""

import os
import re
import urllib.parse
from typing import Dict, List, Optional, Union

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns, ResultStorage
from core.types import IProvider
from search.client import chat
from tools.logger import get_logger
from tools.utils import trim

logger = get_logger("provider")


def _normalize_path(value: str, strict: bool = True) -> str:
    value = trim(value)
    if not value:
        return ""

    pattern = r"[^a-zA-Z0-9_\-]" if strict else r"[^a-zA-Z0-9_\-/\\]"
    return re.sub(pattern, "-", value, flags=re.I).strip("-").lower()


class AIBaseProvider(IProvider):
    """Base implementation for AI service providers.

    Implements the Provider interface to ensure type safety and consistency
    across the application while providing concrete functionality.
    """

    @staticmethod
    def extract(kwargs: Dict, defaults: Dict) -> Dict:
        """Extract configuration parameters from kwargs with defaults.

        Args:
            kwargs: Input parameters dictionary (will be modified)
            defaults: Default values dictionary

        Returns:
            Dict: Processed parameters with defaults applied
        """
        result = {}
        for key, default in defaults.items():
            # Extract value and remove from kwargs
            value = kwargs.pop(key, "")
            result[key] = trim(value) or default

        return result

    @staticmethod
    def filter(kwargs: Dict, exclude: List[str]) -> Dict:
        """Filter out specified keys from kwargs.

        Args:
            kwargs: Input parameters dictionary
            exclude: Keys to exclude

        Returns:
            Dict: Filtered parameters dictionary
        """
        return {k: v for k, v in kwargs.items() if k not in exclude}

    @staticmethod
    def defaults(kwargs: Dict, values: Dict) -> None:
        """Set default values for kwargs if not present.

        Args:
            kwargs: Parameters dictionary to modify
            values: Default values to set
        """
        for key, value in values.items():
            kwargs.setdefault(key, value)

    @staticmethod
    def filenames() -> Dict[str, str]:
        """Default result filenames used by AI providers."""
        return {
            "valid": "valid-keys.txt",
            "no_quota": "no-quota-keys.txt",
            "wait_check": "wait-check-keys.txt",
            "invalid": "invalid-keys.txt",
            "material": "material.txt",
            "summary": "summary.json",
            "links": "links.txt",
        }

    def __init__(
        self,
        name: str,
        base_url: str,
        completion_path: str,
        model_path: str,
        default_model: str,
        conditions: Union[Condition, List[Condition]],
        **kwargs,
    ):
        name = str(name)
        if not name:
            raise ValueError("provider name cannot be empty")

        default_model = trim(default_model)
        if not default_model:
            raise ValueError("default_model cannot be empty")

        base_url = trim(base_url)

        # see: https://stackoverflow.com/questions/10893374/python-confusions-with-urljoin
        if base_url and not base_url.endswith("/"):
            base_url += "/"

        # provider name
        self._name = name

        storage = kwargs.pop("storage", {}) if kwargs else {}
        directory, plan = "", ""
        if isinstance(storage, dict):
            directory = trim(storage.get("directory", ""))
            plan = trim(storage.get("plan", ""))

        if directory and plan:
            folder = os.path.join(_normalize_path(directory, strict=False), _normalize_path(plan))
        elif directory:
            folder = _normalize_path(directory, strict=False)
        else:
            folder = _normalize_path(name)

        self._result = ResultStorage(folder=folder, filenames=self.filenames())

        # base url for llm service api
        self._base_url = base_url

        # path for completion api
        self.completion_path = trim(completion_path).removeprefix("/")

        # path for model list api
        self.model_path = trim(model_path).removeprefix("/")

        # default model for completion api used to verify token
        self._default_model = default_model

        conditions = (
            [conditions]
            if isinstance(conditions, Condition)
            else ([] if not isinstance(conditions, list) else conditions)
        )

        items = set()
        for condition in conditions:
            if not isinstance(condition, Condition) or not condition.patterns.key_pattern or not condition.enabled:
                logger.warning(f"Invalid condition: {condition}, skipping it")
                continue

            items.add(condition)

        # search and extract keys conditions
        self._conditions = list(items)

        # additional parameters for provider
        self.extras = kwargs

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for API requests. Must be implemented by subclasses."""
        raise NotImplementedError

    def _merge_headers(self, headers: Optional[Dict], additional: Optional[Dict] = None) -> Optional[Dict]:
        """Merge provider configured headers with request-specific headers."""
        if not isinstance(headers, dict):
            return headers

        extra_headers = self.extras.get("extra_headers", {}) if isinstance(self.extras, dict) else {}
        if isinstance(extra_headers, dict):
            headers.update(extra_headers)

        if isinstance(additional, dict):
            headers.update(additional)

        return headers

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge API response and return check result."""
        message = trim(message)

        if code == 200 and message:
            return CheckResult.success()
        elif code == 400:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)
        elif code == 401 or re.findall(r"invalid_api_key", message, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)
        elif code == 402 or re.findall(r"insufficient", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)
        elif code == 403 or code == 404:
            return CheckResult.fail(ErrorReason.NO_ACCESS)
        elif code == 418 or code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED)
        elif code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    def _get_retries(self, default: int) -> int:
        return self.extras.get("retries", max(default, 0))

    def _get_timeout(self, default: int) -> int:
        return self.extras.get("timeout", max(default, 0))

    def _get_use_proxy(self, default: bool = True) -> bool:
        """Whether provider validation requests should route through the outbound proxy.

        Read from the task api config (extras).  Domestic endpoints set
        use_proxy: false to connect directly; international endpoints keep the
        default (proxy).
        """
        return bool(self.extras.get("use_proxy", default))

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check if token is valid."""
        url, regex = trim(address), r"^https?://([\w\-_]+\.[\w\-_]+)+"
        if not url and re.match(regex, self._base_url, flags=re.I):
            url = urllib.parse.urljoin(self._base_url, self.completion_path)

        if not re.match(regex, url, flags=re.I):
            logger.error(f"Invalid URL: {url}, skipping check")
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        model = trim(model) or self._default_model

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
        """List available models. Must be implemented by subclasses."""
        raise NotImplementedError

    # Implement abstract properties from IProvider interface
    @property
    def name(self) -> str:
        """Provider name identifier"""
        return self._name

    @property
    def conditions(self) -> List:
        """Search conditions for this provider"""
        return self._conditions

    @property
    def result(self) -> ResultStorage:
        """Result storage metadata for this provider"""
        return self._result

    def get_patterns(self) -> Patterns:
        """Get patterns configuration for this provider"""
        # Extract patterns from the first condition if available
        if self._conditions:
            return self._conditions[0].patterns

        # Return empty patterns if no conditions
        return Patterns()
