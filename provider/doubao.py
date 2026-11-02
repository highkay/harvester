#!/usr/bin/env python3

"""
Doubao provider implementation.
"""

import re
from typing import List

from core.enums import ErrorReason
from core.models import CheckResult, Condition
from tools.utils import trim

from .openai_like import OpenAILikeProvider
from .registry import register_provider

# Ark's 403 body for an authentic but unfunded account. Cover the measured
# error code plus the natural-language forms seen in the same 403 shape.
_OVERDUE_MARKERS = re.compile(r"AccountOverdueError|account\s+overdue|arrearage", flags=re.IGNORECASE)


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

        if code == 403 and _OVERDUE_MARKERS.search(message or ""):
            # Measured 2026-11-02 (probe-matrix.jsonl): an authentic Ark key
            # with an unpaid bill answers 403 {"code":"AccountOverdueError",
            # "type":"Forbidden"}. The shared OpenAI-like 403 branch only knows
            # quota phrases like insufficient_user_quota, so this body fell
            # through to NO_ACCESS — a recoverable-but-wrong class that keeps
            # an unfunded key in wait-check instead of the no-quota bucket.
            # The key is genuine; only its billing state is dead.
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        return super()._judge(code, message)

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check Doubao token validity.

        Cheap gate first (a single chat completion on the default model, the
        current OpenAILike gate). For a key that passes AND has
        ``capability_tiers`` declared, measure each tier with one
        ``max_tokens=1`` chat completion.
        """
        model = trim(model)
        if not model:
            model = self._default_model

        result = super().check(token=token, address=address, endpoint=endpoint, model=model)
        if result.available and self._capability_tiers:
            result = self._apply_capability_tiers(token, result)
        return result


register_provider("doubao", DoubaoProvider)
