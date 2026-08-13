#!/usr/bin/env python3

"""
Google Vertex AI provider implementation.
"""

import json
import re
from typing import Dict, List, Optional

from constant.system import DEFAULT_QUESTION
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import chat, http_get
from tools.logger import get_logger
from tools.utils import trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")


class VertexProvider(AIBaseProvider):
    """Google Vertex AI provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Extract parameters with defaults
        config = self.extract(
            kwargs,
            {
                "name": "vertex",
                "base_url": "https://aiplatform.googleapis.com",
                "completion_path": "/v1/projects/{project}/locations/{location}/publishers/{publisher}/models/{model}:predict",
                "model_path": "/v1/projects/{project}/locations/{location}/models",
                "default_model": "gemini-2.5-pro",
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

        # Supported publishers in Vertex AI Model Garden
        self.publishers = {
            "google": ["gemini", "text-bison", "chat-bison", "codey", "textembedding", "multimodalembedding"],
            "anthropic": ["claude"],
            "meta": ["llama", "code-llama"],
            "mistralai": ["mistral"],
            "ai21": ["jamba"],
            "cohere": ["command", "embed"],
            "nvidia": ["nemotron"],
            "salesforce": ["xgen"],
        }

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for Vertex AI API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        }
        return self._merge_headers(headers, additional)

    def detect_publisher(self, model: str) -> str:
        """Detect publisher from model name."""
        model = trim(model).lower()

        # More comprehensive matching patterns for 2025 models
        if (
            model.startswith("gemini")
            or model.startswith("text-bison")
            or model.startswith("chat-bison")
            or model.startswith("code-bison")
            or model.startswith("codechat-bison")
            or model.startswith("text-unicorn")
            or model.startswith("codey")
            or model.startswith("textembedding")
            or model.startswith("multimodalembedding")
        ):
            return "google"
        elif model.startswith("claude"):
            return "anthropic"
        elif model.startswith("llama") or model.startswith("code-llama") or "llama" in model:
            return "meta"
        elif model.startswith("mistral") or model.startswith("codestral") or "mistral" in model:
            return "mistralai"
        elif model.startswith("jamba"):
            return "ai21"
        elif model.startswith("command") or model.startswith("embed") or "cohere" in model:
            return "cohere"
        elif model.startswith("nemotron"):
            return "nvidia"
        elif model.startswith("xgen"):
            return "salesforce"

        # Default to google for unknown models
        return "google"

    def build_url(self, project: str, location: str, model: str, action: str = "predict") -> str:
        """Build Vertex AI API URL."""
        project = trim(project)
        location = trim(location)
        model = trim(model)

        if not project or not model:
            return ""

        publisher = self.detect_publisher(model)

        # Different actions for different publishers
        if publisher == "google":
            if action == "predict":
                action = "generateContent"
        elif publisher == "anthropic":
            if action == "predict":
                action = "rawPredict"
        elif publisher in ["meta", "mistralai", "ai21", "cohere"]:
            if action == "predict":
                action = "rawPredict"
        else:
            # For other publishers like nvidia, salesforce
            if action == "predict":
                action = "rawPredict"

        # Use location-specific URL if location is provided, otherwise use global
        if location:
            base_url = f"https://{location}-aiplatform.googleapis.com"
        else:
            base_url = "https://aiplatform.googleapis.com"
            location = "global"

        url = f"{base_url}/v1/projects/{project}/locations/{location}/publishers/{publisher}/models/{model}:{action}"
        return url

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Vertex AI API response."""
        message = trim(message)

        if code == 200:
            return CheckResult.success()
        elif code == 400:
            if re.findall(r"API_KEY_INVALID|invalid.*key", message, flags=re.I):
                return CheckResult.fail(ErrorReason.INVALID_KEY)
            elif re.findall(r"PERMISSION_DENIED|permission", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_ACCESS)
        elif code == 403:
            if re.findall(r"quota|billing", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            else:
                return CheckResult.fail(ErrorReason.NO_ACCESS)
        elif code == 404:
            if re.findall(r"model.*not.*found", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_MODEL)

        return super()._judge(code, message)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Vertex AI token validity."""
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        # Map parameters: address=location, endpoint=project_id
        location = trim(address) or "global"
        project = trim(endpoint)
        model = trim(model) or self._default_model

        if not project:
            logger.error("Project ID is required for Vertex AI")
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        url = self.build_url(project, location, model, "predict")
        if not url:
            logger.error(f"Failed to build URL for project: {project}, model: {model}")
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        # Build request payload based on publisher
        publisher = self.detect_publisher(model)
        if publisher == "google":
            params = {
                "contents": [{"role": "user", "parts": [{"text": DEFAULT_QUESTION}]}],
                "generation_config": {"temperature": 0.1, "max_output_tokens": 100},
            }
        elif publisher == "anthropic":
            # Claude models use rawPredict with direct Anthropic API format
            params = {
                "messages": [{"role": "user", "content": DEFAULT_QUESTION}],
                "anthropic_version": "vertex-2023-10-16",
                "max_tokens": 100,
                "temperature": 0.1,
            }
        elif publisher == "meta":
            # Meta Llama models
            params = {
                "instances": [
                    {
                        "inputs": DEFAULT_QUESTION,
                        "parameters": {"temperature": 0.1, "max_new_tokens": 100},
                    }
                ]
            }
        elif publisher == "mistralai":
            # Mistral models
            params = {
                "instances": [
                    {
                        "inputs": DEFAULT_QUESTION,
                        "parameters": {"temperature": 0.1, "max_tokens": 100},
                    }
                ]
            }
        elif publisher == "ai21":
            # AI21 Jamba models
            params = {
                "instances": [
                    {
                        "messages": [{"role": "user", "content": DEFAULT_QUESTION}],
                        "max_tokens": 100,
                        "temperature": 0.1,
                    }
                ]
            }
        elif publisher == "cohere":
            # Cohere Command models
            params = {
                "instances": [
                    {
                        "message": DEFAULT_QUESTION,
                        "max_tokens": 100,
                        "temperature": 0.1,
                    }
                ]
            }
        else:
            # For other publishers (nvidia, salesforce, etc.)
            params = {
                "instances": [
                    {
                        "inputs": DEFAULT_QUESTION,
                        "parameters": {"temperature": 0.1, "max_new_tokens": 100},
                    }
                ]
            }

        code, message = chat(
            url=url,
            headers=headers,
            params=params,
            retries=self._get_retries(default=2),
            timeout=self._get_timeout(default=10),
            use_proxy=self._get_use_proxy(),
        )
        return self._judge(code=code, message=message)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available Vertex AI models."""
        token = trim(token)
        if not token:
            logger.warning("No token provided, returning default model list")
            return self._get_default_models()

        # Map parameters: address=location, endpoint=project_id
        location = trim(address) or "global"
        project = trim(endpoint)

        if not project:
            logger.error("Project ID is required for Vertex AI")
            return []

        # Try to get models from different publishers
        models = []

        # List of publishers to check
        publishers = ["google", "anthropic", "meta", "mistralai", "ai21", "cohere", "nvidia", "salesforce"]

        for publisher in publishers:
            try:
                # Use location-specific URL if location is provided
                if location and location != "global":
                    base_url = f"https://{location}-aiplatform.googleapis.com"
                else:
                    base_url = "https://aiplatform.googleapis.com"
                    location = "global"

                url = f"{base_url}/v1/projects/{project}/locations/{location}/publishers/{publisher}/models"
                headers = self._get_headers(token=token)
                if not headers:
                    continue

                content = http_get(url=url, headers=headers, interval=1, use_proxy=self._get_use_proxy())
                if not content:
                    continue

                data = json.loads(content)
                models = data.get("models", [])

                for model in models:
                    name = model.get("name", "")
                    display_name = model.get("displayName", "")

                    if name:
                        # Extract model ID from full resource name
                        # Format: projects/{project}/locations/{location}/publishers/{publisher}/models/{model}
                        parts = name.split("/")
                        if len(parts) >= 8 and parts[-2] == "models":
                            model_id = parts[-1]
                            models.append(model_id)
                        elif display_name:
                            models.append(display_name)

            except Exception as e:
                logger.debug(f"Failed to get models from publisher {publisher}: {e}")
                continue

        # If no models found from publishers, try the general models endpoint
        if not models:
            try:
                # Use location-specific URL if location is provided
                if location and location != "global":
                    base_url = f"https://{location}-aiplatform.googleapis.com"
                else:
                    base_url = "https://aiplatform.googleapis.com"
                    location = "global"

                url = f"{base_url}/v1/projects/{project}/locations/{location}/models"
                headers = self._get_headers(token=token)
                if headers:
                    content = http_get(url=url, headers=headers, interval=1, use_proxy=self._get_use_proxy())
                    if content:
                        data = json.loads(content)
                        models = data.get("models", [])

                        for model in models:
                            name = model.get("name", "")
                            if name:
                                parts = name.split("/")
                                if len(parts) >= 6 and parts[-2] == "models":
                                    models.append(parts[-1])
            except Exception as e:
                logger.error(f"Failed to get models from general endpoint: {e}")

        # Remove duplicates and sort
        unique_models = list(set(models))

        # If no models found, return default models
        if not unique_models:
            logger.warning("No models found via API, returning default model list")
            return self._get_default_models()

        return sorted(unique_models)

    def _get_default_models(self) -> List[str]:
        """Get default Vertex AI models list (2025)."""
        return [
            # Google models (latest available including Gemini 2.5)
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash-exp",
            "gemini-1.5-pro-002",
            "gemini-1.5-flash-002",
            "gemini-1.5-flash-8b",
            "gemini-exp-1206",
            "text-bison-32k",
            "text-unicorn",
            "code-bison-32k",
            "codechat-bison-32k",
            # Anthropic models (latest Claude 4 and 3.x series)
            "claude-opus-4",
            "claude-sonnet-4",
            "claude-3-7-sonnet",
            "claude-3-5-sonnet-v2@20241022",
            "claude-3-5-haiku@20241022",
            "claude-3-5-sonnet",
            "claude-3-opus@20240229",
            "claude-3-sonnet@20240229",
            "claude-3-haiku@20240307",
            # Meta models (Llama 3.x series)
            "llama-3.1-405b-instruct-maas",
            "llama-3.1-70b-instruct-maas",
            "llama-3.1-8b-instruct-maas",
            "llama-3.2-90b-vision-instruct-maas",
            "llama-3.2-11b-vision-instruct-maas",
            "llama-3.2-3b-instruct-maas",
            "llama-3.2-1b-instruct-maas",
            "code-llama-34b-instruct-maas",
            # Mistral models (latest versions)
            "mistral-large-2407",
            "mistral-nemo-2407",
            "codestral-2405",
            "mistral-7b-instruct-v0.3",
            # AI21 models
            "jamba-1.5-large",
            "jamba-1.5-mini",
            # Cohere models
            "command-r-plus-08-2024",
            "command-r-08-2024",
            "embed-english-v3.0",
            "embed-multilingual-v3.0",
            # Additional publishers
            "nemotron-4-340b-instruct",
            "xgen-7b-8k-instruct",
        ]


register_provider("vertex", VertexProvider)
