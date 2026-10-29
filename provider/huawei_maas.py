#!/usr/bin/env python3

"""
Huawei MaaS (ModelArts MaaS) provider implementation.

Gate evidence (wave-0 CN probe pack, 2026-10-29, prod container):
  - no key  -> 400 {"error":{"code":"ModelArts.81002","message":"Failed to
    get the authorization header."}}  (the header is MISSING — a gate
    artifact, never a key verdict)
  - bad key -> 401 {"error":{"code":"ModelArts.81003","message":"Invalid
    authorization header."}}  (the key itself was rejected -> INVALID_KEY)

Validation is the inherited minimal chat completion (base_url +
/openai/v1/chat/completions, Bearer token). The 400/81002 split must stay
out of the terminal buckets: it says nothing about the key.
"""

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider


class HuaweiMaasProvider(OpenAILikeProvider):
    """Huawei ModelArts MaaS OpenAI-compatible provider."""

    def __init__(self, conditions: list[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "huawei-maas",
                "base_url": "https://api.modelarts-maas.com/openai/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                # Probe model id UNVERIFIED: wave-0 (2026-10-29) confirmed the
                # gate but found no free-quota terms and no real key to list
                # /models with. The pilot run must confirm this id against a
                # live key's catalog; a wrong id answers 404 -> NO_MODEL
                # (wait-check), so a stale id costs re-checks, never key
                # discards.
                "default_model": "DeepSeek-V3.1",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge a Huawei MaaS API response."""
        body = trim(message or "")

        if code == 400 and "ModelArts.81002" in body:
            # Missing-authorization-header gate artifact (measured 2026-10-29:
            # a request WITHOUT a key answers 400 ModelArts.81002). It carries
            # no information about the key, so it must never reach the
            # permanent buckets: SERVER_ERROR is retryable and CheckStage
            # files it in wait-check.
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        if code == 404:
            # Unknown/absent model id on an authenticated key — a routing
            # failure, not a key verdict. NO_MODEL goes to wait-check.
            return CheckResult.fail(ErrorReason.NO_MODEL)

        result = super()._judge(code=code, message=body)
        if not result.available and result.reason == ErrorReason.UNKNOWN:
            # An undecidable body (captive HTML, truncated proxy exit) is
            # retryable, never a permanent discard.
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return result


register_provider("huawei-maas", HuaweiMaasProvider)
