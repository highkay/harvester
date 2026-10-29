#!/usr/bin/env python3

"""
Doubao provider implementation.
"""

from typing import List

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider


class DoubaoProvider(OpenAILikeProvider):
    """Doubao provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Set Doubao specific defaults
        self.defaults(
            kwargs,
            {
                "name": "doubao",
                "base_url": "https://ark.cn-beijing.volces.com",
                "completion_path": "/api/v3/chat/completions",
                "model_path": "/api/v3/models",
                # Ark ids are versioned: real probes target per-deployment
                # endpoint ids (ep-XXXXXXXXXXXXXX-XXXXX, see model_pattern) or
                # version-suffixed model names. Measured 2026-10-29
                # (.omo/evidence/magpie-coverage/wave0-cn.md, probe b): the
                # pay-as-you-go /api/v3 catalog's latest seed-2.1-pro id is
                # doubao-seed-2-1-pro-260915; the previous default
                # (doubao-pro-32k) is a legacy alias with no live probe. A 404
                # on the probe id means "endpoint/model unknown", which
                # _judge routes to NO_MODEL (wait-check), not INVALID_KEY.
                "default_model": "doubao-seed-2-1-pro-260915",
                "model_pattern": r"ep-[0-9]{14}-[a-z0-9]{5}",
            },
        )

        super().__init__(conditions=conditions, **kwargs)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge Doubao API response."""
        if code == 404:
            # Ark answers 404 for an unknown/absent deployment or model id —
            # the key itself is not condemned by a routing failure. NO_MODEL
            # goes to wait-check (recoverable); INVALID_KEY would be a
            # permanent discard.
            return CheckResult.fail(ErrorReason.NO_MODEL)

        return super()._judge(code, message)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Doubao token validity."""
        model = trim(model)
        if not model:
            model = self._default_model

        return super().check(token=token, address=address, endpoint=endpoint, model=model)


register_provider("doubao", DoubaoProvider)
