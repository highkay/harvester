#!/usr/bin/env python3

"""
Ollama Cloud provider implementation.
"""

import re
from typing import List

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from tools.logger import get_logger
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider

logger = get_logger("provider")


class OllamaProvider(OpenAILikeProvider):
    """Ollama Cloud OpenAI-compatible provider implementation.

    Validates keys via /v1/chat/completions (inherited from base).
    """

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "ollama",
                "base_url": "https://ollama.com/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "gpt-oss:20b",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Ollama Cloud API response."""
        message = trim(message)

        if code in (401, 403) and re.findall(
            r"invalid[_-]?api[_-]?key|invalid\s+api\s*key|api\s*key.*invalid|incorrect\s+api\s*key|unauthorized|forbidden",
            message,
            flags=re.I,
        ):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code in (401, 403) and re.findall(r"permission|access|not\s+allowed", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 402 or re.findall(r"insufficient|quota|credits?|billing|balance", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code in (400, 404) and re.findall(r"model|not\s+found", message, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_MODEL)

        if code == 429 or re.findall(r"rate\s*limit|too\s+many\s+requests", message, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        return super()._judge(code, message)


register_provider("ollama", OllamaProvider)
