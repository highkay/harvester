#!/usr/bin/env python3

"""
OpenAI-like provider base class.
"""

import json
import re
import urllib.parse

from tools.logger import get_logger
from tools.patterns import redact_api_keys_in_text

logger = get_logger("provider")
from typing import Dict, List, Optional

from constant.system import DEFAULT_COMPLETION_PATH, DEFAULT_MODEL_PATH
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_get
from tools.coordinator import get_user_agent
from tools.utils import handle_exceptions, trim

from .base import AIBaseProvider
from .registry import register_provider

# Case-insensitive markers for classifying HTTP-200-with-error bodies (see
# OpenAILikeProvider._judge). Auth-flavoured -> the key itself was rejected
# (permanent INVALID_KEY discard); quota/billing -> authentic key without
# funds (NO_QUOTA); neither -> no key verdict can be made, so the body goes
# to the recoverable wait-check bucket (BAD_REQUEST).
_AUTH_ERROR_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "unauthorized",
    "authentication",
    "api key not valid",
)
_QUOTA_ERROR_MARKERS = ("insufficient", "quota", "billing", "credits", "balance")


class OpenAILikeProvider(AIBaseProvider):
    """Base class for OpenAI-compatible providers."""

    def __init__(self, conditions: List[Condition], **kwargs):
        # Extract required parameters without defaults
        name = trim(kwargs.pop("name", ""))
        base_url = trim(kwargs.pop("base_url", ""))
        default_model = trim(kwargs.pop("default_model", ""))

        # Validate required parameters
        if not name:
            raise ValueError("OpenAILike provider requires 'name' parameter to be specified")
        if not base_url:
            raise ValueError(f"OpenAILike provider {name} requires 'base_url' parameter to be specified")
        if not default_model:
            raise ValueError(f"OpenAILike provider {name} requires 'default_model' parameter to be specified")

        # Extract optional parameters with defaults
        config = self.extract(
            kwargs,
            {
                "completion_path": DEFAULT_COMPLETION_PATH,
                "model_path": DEFAULT_MODEL_PATH,
            },
        )

        # Add the validated required parameters back to config
        config.update(
            {
                "name": name,
                "base_url": base_url,
                "default_model": default_model,
            }
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

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for OpenAI-like API requests."""
        token = trim(token)
        if not token:
            return None

        if not isinstance(additional, dict):
            additional = {}

        auth_key = (trim(self.extras.get("auth_key", None)) if isinstance(self.extras, dict) else "") or "authorization"
        auth_value = f"Bearer {token}" if auth_key.lower() == "authorization" else token

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            auth_key: auth_value,
            "user-agent": get_user_agent(),
        }

        return self._merge_headers(headers, additional)

    def _judge(self, code: int, message: str) -> CheckResult:
        """Judge OpenAI-like API response."""
        if code == 200:
            body = trim(message)
            if not body:
                # An EMPTY 200 body is a transport/proxy artefact (a truncated
                # connection or a transparent proxy exit), NOT a key verdict.
                # The old path raised inside json.loads and returned UNKNOWN,
                # which CheckStage routes to invalid-keys.txt — a PERMANENT
                # discard of an otherwise authenticated key. SERVER_ERROR is
                # retryable, so the key lands in the recoverable
                # wait-check-keys.txt bucket instead.
                return CheckResult.fail(ErrorReason.SERVER_ERROR)
            try:
                data = json.loads(body)
                if data and isinstance(data, dict):
                    error = data.get("error", None)
                    # Normalize both error shapes to lowercase marker text.
                    # The string arm is the native-Ollama body
                    # ({"error": "model not found"}) — the old dict-only gate
                    # let it fall through to success and pool a soft-error
                    # body as a VALID key. None / falsy / other types keep the
                    # historical success fall-through.
                    error_text = ""
                    if isinstance(error, str):
                        error_text = trim(error).lower()
                    elif isinstance(error, dict) and error:
                        error_text = json.dumps(error, ensure_ascii=False).lower()
                    if error_text:
                        # A 200 carrying an error body is a failed request,
                        # but the FAILURE KIND decides the bucket (matching how
                        # stage/definition.py routes verdicts):
                        #   auth-flavoured  -> the key itself was rejected ->
                        #                      INVALID_KEY (permanent discard)
                        #   quota/billing   -> authentic key without funds ->
                        #                      NO_QUOTA
                        #   anything else   -> a transient upstream/model fault,
                        #                      NOT a key verdict -> BAD_REQUEST,
                        #                      which CheckStage files in the
                        #                      recoverable wait-check bucket.
                        # The old blanket INVALID_KEY permanently burned valid
                        # keys whenever a wrapper gateway answered a transient
                        # upstream fault with HTTP 200 + error JSON.
                        if any(marker in error_text for marker in _AUTH_ERROR_MARKERS):
                            return CheckResult.fail(ErrorReason.INVALID_KEY)
                        if any(marker in error_text for marker in _QUOTA_ERROR_MARKERS):
                            return CheckResult.fail(ErrorReason.NO_QUOTA)
                        return CheckResult.fail(ErrorReason.BAD_REQUEST)
            except:
                # Present-but-unparseable body (e.g. an HTML captive/error
                # page served by a transparent proxy exit): no key verdict can
                # be made, so the historical UNKNOWN classification is kept
                # (permanent invalid-keys bucket — unchanged semantics).
                # Redact before logging — this family can carry prefix-less
                # keys that the global RedactionFilter misses — and cap the
                # length so a huge body cannot flood the log.
                logger.error(
                    f"Failed to parse response, domain: {self._base_url}, "
                    f"message: {redact_api_keys_in_text(message)[:200]}"
                )
                return CheckResult.fail(ErrorReason.UNKNOWN)

            # Deliberately NOT tightening the success shape (no `choices`
            # requirement): several OpenAI-compatible gateways return unusual
            # success bodies, and demanding a canonical chat-completion shape
            # would regress their live keys from valid to UNKNOWN. The rule is
            # only "200 without an error object", nothing more.
            return CheckResult.success()

        message = trim(message)
        if message:
            if code == 403:
                if re.findall(r"model_not_found", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.NO_MODEL)
                elif re.findall(r"unauthorized|已被封禁", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.INVALID_KEY)
                elif re.findall(r"unsupported_country_region_territory|该令牌无权访问模型", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.NO_ACCESS)
                elif re.findall(
                    r"exceeded_current_quota_error|insufficient_user_quota|(额度|余额)(不足|过低)", message, flags=re.I
                ):
                    return CheckResult.fail(ErrorReason.NO_QUOTA)
            elif code == 429:
                if re.findall(r"insufficient_quota|billing_not_active|欠费|请充值|recharge", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.NO_QUOTA)
                elif re.findall(r"rate_limit_exceeded", message, flags=re.I):
                    return CheckResult.fail(ErrorReason.RATE_LIMITED)
            elif code == 503 and re.findall(r"无可用渠道", message, flags=re.I):
                return CheckResult.fail(ErrorReason.NO_MODEL)

        return super()._judge(code, message)

    @handle_exceptions(default_result=[], log_level="warning")
    def _fetch_models(self, url: str, headers: Dict) -> List[str]:
        """Fetch models from API endpoint."""
        url = trim(url)
        if not url:
            return []

        content = http_get(url=url, headers=headers, interval=1, use_proxy=self._get_use_proxy())
        if not content:
            return []

        result = json.loads(content)
        return [trim(x.get("id", "")) for x in result.get("data", [])]

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List available models from OpenAI-like API."""
        headers = self._get_headers(token=token)
        if not headers or not self._base_url or not self.model_path:
            return []

        url = urllib.parse.urljoin(self._base_url, self.model_path)
        return self._fetch_models(url=url, headers=headers)


register_provider("openai_like", OpenAILikeProvider)
