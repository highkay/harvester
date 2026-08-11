#!/usr/bin/env python3

"""
GLM (Zhipu AI / BigModel) provider implementation.
"""

import json
import re
from typing import List

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from tools.logger import get_logger
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider

logger = get_logger("provider")


class GLMProvider(OpenAILikeProvider):
    """GLM (Zhipu AI) provider implementation.

    Zhipu's API (open.bigmodel.cn/api/paas/v4) exposes NO ``GET /models``
    endpoint, so ``check`` uses the inherited OpenAI-compatible chat-completions
    probe against ``glm-4.7-flash`` and auth is classified from Zhipu's
    string error codes. ``inspect`` returns an empty list because there is no
    model-list API for this provider.
    """

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "glm",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "completion_path": "/chat/completions",
                "model_path": "",
                "default_model": "glm-4.7-flash",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Zhipu (BigModel) API response.

        Zhipu error bodies are always ``{"error": {"code": "<string>", "message": "..."}}``:
          - 401 codes 1000 / 1001 / 1003  -> invalid key
          - 400 code 1211                 -> auth passed, unknown model
          - 429 code 1113                 -> valid key but no balance
        """
        message = trim(message)
        error_code = ""
        text = message.lower()

        if message:
            try:
                data = json.loads(message)
                error = data.get("error", {}) if isinstance(data, dict) else {}
                if isinstance(error, dict):
                    code_value = error.get("code", "")
                    error_code = str(code_value) if code_value is not None else ""
                    text = trim(error.get("message", "")).lower()
            except Exception:
                # Not JSON; fall through to status-code + keyword heuristics.
                error_code = ""

        if code == 200:
            return CheckResult.success()

        if code == 401 or error_code in ("1000", "1001", "1003"):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if error_code == "1211" or (code in (400, 404) and re.findall(r"model|不存在", text)):
            return CheckResult.fail(ErrorReason.NO_MODEL)

        if code == 402 or error_code == "1113":
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        return super()._judge(code, message)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """Zhipu has no models endpoint, so there is nothing to enumerate."""
        return []


register_provider("glm", GLMProvider)