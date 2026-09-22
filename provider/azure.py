#!/usr/bin/env python3

"""
Azure OpenAI provider implementation.
"""

import re

from core.models import Condition
from tools.logger import get_logger

logger = get_logger("provider")
from typing import Dict, List, Optional

from core.enums import ErrorReason
from core.models import CheckResult
from tools.coordinator import get_user_agent
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider


class AzureOpenAIProvider(OpenAILikeProvider):
    """Azure OpenAI provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Set Azure specific defaults
        self.defaults(
            kwargs,
            {
                "name": "azure",
                "model_path": "/models",
                "default_model": "gpt-4o",
            },
        )

        kwargs["completion_path"] = "/chat/completions"
        if not trim(kwargs.get("base_url", "")):
            kwargs["base_url"] = "https://fake.openai.azure.com"

        for condition in conditions:
            if not condition or not condition.patterns:
                continue

            if not condition.patterns.address_pattern:
                condition.patterns.address_pattern = r"https://[a-zA-Z0-9_\-\.]+.openai.azure.com/openai/"

            if not condition.patterns.endpoint_pattern:
                condition.patterns.endpoint_pattern = r"/deployments/([a-zA-Z0-9_\-]+)/chat"

        api_version = trim(kwargs.get("api_version", ""))

        super().__init__(conditions=conditions, **kwargs)

        self.api_version = api_version or "2024-10-21"

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for Azure OpenAI API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "accept": "application/json",
            "api-key": token,
            "content-type": "application/json",
            "user-agent": get_user_agent(),
        }
        return self._merge_headers(headers, additional)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Azure OpenAI API response."""
        if code == 404:
            # Any 404 means the deployment name in the probe URL is wrong or
            # absent (the api-key header is not even evaluated for routing
            # failures) -> NO_MODEL (wait-check), never a dead-key verdict. The
            # old body-text match on one exact English sentence judged every
            # other 404 as INVALID_KEY, permanently burning live keys.
            return CheckResult.fail(ErrorReason.NO_MODEL)

        return super()._judge(code, message)

    def __generate_address(self, address: str = "", endpoint: str = "", model: str = "") -> str:
        """Generate Azure OpenAI API address."""
        address = trim(address).removesuffix("/")
        if not re.match(r"^https?://([\w\-_]+\.[\w\-_]+)+", address, flags=re.I):
            return ""

        if re.findall(
            r"(xxx|YOUR_RESOURCE_NAME|your_service|YOUR_AZURE_OPENAI_NAME|YOUR-INSTANCE|YOUR_ENDPOINT_NAME|RESOURCE_NAME|YOURAOAIINSTANCE|yourname|YOUR_NAME|YOUR_AOAI_SERVICE|COMPANY|your-deployment-name|YOUR_AOI_SERVICE_NAME|YOUR_AI_ENDPOINT_NAME|YOUR-APP|YOUR-RESOURCE-NAME).openai.azure.com",
            address,
            flags=re.I,
        ):
            return ""

        model = trim(endpoint) or trim(model) or self._default_model
        return f"{address}/deployments/{model}/{self.completion_path}?api-version={self.api_version}"

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Azure OpenAI token validity."""
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        url = self.__generate_address(address=address, endpoint=endpoint, model=model)
        if not url:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if not model:
            model = trim(endpoint)

        return super().check(token=token, address=url, endpoint=endpoint, model=model)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available Azure OpenAI models."""
        domain = trim(address).removesuffix("/")
        if not re.match(r"^https?://([\w\-_]+\.[\w\-_]+)+", domain, flags=re.I):
            logger.error(f"Invalid domain: {domain}, skipping model listing")
            return []

        headers = self._get_headers(token=token)
        if not headers or not self.model_path:
            return []

        url = f"{domain}/{self.model_path}?api-version={self.api_version}"
        return self._fetch_models(url=url, headers=headers)


register_provider("azure", AzureOpenAIProvider)
