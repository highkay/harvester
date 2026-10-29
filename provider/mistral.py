#!/usr/bin/env python3

"""
Mistral provider implementation.

Wave-0 (2026-10-29, prod socks exits 1090/1091/1080): GET /v1/models answers
401 {"detail":"Invalid API Key"} for no-key AND bad-key in 1.4-1.6 s - a real
application-level gate, not a WAF page or a public list. Keys are prefix-less
32-char alnum (4/4 samples), so extraction is env+domain anchored. Inherited
judge handles 401 -> INVALID_KEY, 402 -> NO_QUOTA, 403 -> NO_ACCESS.
"""

import json
import re

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider

_AUTH = re.compile(r"invalid api key|incorrect api key|unauthorized|authentication|invalid[_ ]?token", re.IGNORECASE)
_QUOTA = re.compile(r"insufficient|quota|billing|credits|balance|arrears|payment", re.IGNORECASE)
_MODEL_MISSING = re.compile(
    r"(?:model|deployment).{0,40}(?:not found|does not exist|unknown|invalid)|invalid model|model_not_found",
    re.IGNORECASE,
)


class MistralProvider(OpenAILikeProvider):
    """Mistral La Plateforme (api.mistral.ai/v1) OpenAI-compatible provider."""

    def __init__(self, conditions: list[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "mistral",
                "base_url": "https://api.mistral.ai/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                # Liveness UNVERIFIED: wave-0 probed the auth layer only. A
                # retired id answers 404/422 -> NO_MODEL / BAD_REQUEST
                # (wait-check), never INVALID_KEY.
                "default_model": "mistral-small-latest",
            },
        )
        super().__init__(conditions=conditions, **kwargs)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge a Mistral API response."""
        body = trim(message or "")

        if code == 200:
            try:
                data = json.loads(body) if body else None
            except ValueError:
                data = None
            if not isinstance(data, dict):
                # Empty or unparseable 200 body: transport artefact, no verdict.
                return CheckResult.fail(ErrorReason.SERVER_ERROR)
            if data.get("choices"):
                return CheckResult.success()
            # Mistral error bodies use detail/message, not only `error`.
            text = " ".join(str(data[k]) for k in ("error", "detail", "message") if data.get(k)).lower()
            if not text:
                return CheckResult.success()
            if _AUTH.search(text):
                return CheckResult.fail(ErrorReason.INVALID_KEY)
            if _QUOTA.search(text):
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        if code == 404 or (code in (400, 422) and _MODEL_MISSING.search(body)):
            return CheckResult.fail(ErrorReason.NO_MODEL)
        if code == 429:
            return CheckResult.fail(ErrorReason.NO_QUOTA if _QUOTA.search(body) else ErrorReason.RATE_LIMITED)
        if code in (400, 422):
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        result = super()._judge(code=code, message=body)
        if not result.available and result.reason == ErrorReason.UNKNOWN:
            # Undecidable body (captive HTML, truncated proxy): retryable.
            return CheckResult.fail(ErrorReason.SERVER_ERROR)
        return result


register_provider("mistral", MistralProvider)