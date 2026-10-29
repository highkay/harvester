#!/usr/bin/env python3

"""SiliconFlow provider implementation."""

import re

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider

# Documented taxonomy: 401 wrong key, 402 arrears, 403 real-name/permission
# gate, 429 a REFILLABLE rate limit. Only balance/arrears markers reach terminal
# NO_QUOTA; a throttled key stays recoverable (wait-check, qwen's lesson).
_QUOTA_EXHAUSTED = re.compile(
    r"insufficient[_ ]?(?:quota|balance|funds)|quota[_ -]?exceeded|exceeded_current_quota|"
    r"arrears|arrearage|余额不足|额度不足|欠费|请充值|recharge",
    re.IGNORECASE,
)
# No bare "authentication": the 403 body says "requires real-name authentication".
_AUTH_REJECTED = re.compile(r"token is invalid|invalid[_ ]?(?:api[ _-]?)?(?:key|token)|unauthorized", re.IGNORECASE)
_MODEL_MISSING = re.compile(r"model (?:does not exist|not found)|no such model|\b20012\b", re.IGNORECASE)


class SiliconFlowProvider(OpenAILikeProvider):
    """SiliconFlow (api.siliconflow.cn) OpenAI-compatible provider."""
    def __init__(self, conditions: list[Condition], **kwargs):
        self.defaults(kwargs, {
            "name": "siliconflow",
            "base_url": "https://api.siliconflow.cn",
            "completion_path": "/v1/chat/completions",
            "model_path": "/v1/models",
            # UNVERIFIED (wave-0: free list is login-walled): the pilot must
            # confirm this id; 404 / 400 code 20012 -> NO_MODEL (wait).
            "default_model": "Qwen/Qwen2.5-7B-Instruct",
        })
        super().__init__(conditions=conditions, **kwargs)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge SiliconFlow API response."""
        body = trim(message or "")
        if code == 404 or (code == 400 and _MODEL_MISSING.search(body)):
            return CheckResult.fail(ErrorReason.NO_MODEL)
        if code == 401 or (code == 403 and _AUTH_REJECTED.search(body)):
            return CheckResult.fail(ErrorReason.INVALID_KEY)
        if code == 402 or (code >= 400 and _QUOTA_EXHAUSTED.search(body)):
            return CheckResult.fail(ErrorReason.NO_QUOTA)
        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        result = super()._judge(code=code, message=body)
        if not result.available and result.reason == ErrorReason.UNKNOWN:
            # Undecidable body (captive HTML, truncated page): retryable, never
            # a permanent invalid discard.
            return CheckResult.fail(ErrorReason.SERVER_ERROR)
        return result


register_provider("siliconflow", SiliconFlowProvider)
