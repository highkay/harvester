#!/usr/bin/env python3

"""
OpenCode provider implementation.

OpenCode (https://opencode.ai) is an open-source AI coding agent CLI with a
paid subscription service ("OpenCode Go") that gives users an API key to access
AI models through opencode's hosted endpoint at
``https://opencode.ai/zen/go/v1``. The subscription endpoint is OpenAI-
compatible and authenticated with ``sk-`` Bearer keys (format: ``sk-`` + 64
alphanumeric chars, 67 total).

Validation uses the minimal chat-completion probe (``max_tokens=1``) with model
``glm-5.3`` — NOT ``GET /models``, which is NOT auth-gated (returns 200 with
the full model list for any key, including invalid ones). ``inspect()`` only
enumerates model IDs for keys that already passed ``check()``.

Error response shape (live-verified 2026-09-22 against the production API):
    {"type":"error","error":{"type":"<ClassName>","message":"<string>"}}

Additionally measured on prod 2026-09-28 (real corpus probes):
    - No ``x-opencode-session`` header -> 400 MissingSessionID "Request is
      missing x-opencode-session and cannot be routed efficiently" — a
      PRE-AUTH routing gate: garbage keys 401 without it, but every
      well-formed key gets 400 and can never be judged. The provider always
      sends a session header, so this must never fire.
    - Routed dead key -> 401 {"error":{"type":"server_error","message":
      "Upstream request failed: Invalid credential"}} (no top-level
      "type":"error" wrapper) -> INVALID_KEY via the generic 401 fallthrough.
    - Routed authentic key with lapsed/absent subscription -> 403
      {"error":{"type":"server_error","message":"An active OpenCode Go
      subscription ..."}} -> NO_ACCESS -> wait-check (never pushed).

Status mapping traps (from the opencode gateway source, handler.ts:480-543):
    - 401 AuthError           -> INVALID_KEY (bad/expired key)
    - 401 CreditsError        -> NO_QUOTA (insufficient balance — NOT 402!)
    - 401 MonthlyLimitError   -> NO_QUOTA (monthly cap reached)
    - 401 UserLimitError      -> NO_QUOTA (per-user limit reached)
    - 401 ModelError          -> BAD_REQUEST (model validated BEFORE auth;
                                    pinning a real model ID avoids this)
    - 403 RegionError         -> NO_ACCESS (geo-blocked)
    - 403 DataPolicyError     -> NO_ACCESS (data policy block)
    - 429 RateLimitError     -> RATE_LIMITED (per-key 1000 req/min)
    - 429 GoUsageLimitError  -> RATE_LIMITED (subscription quota: 5h/weekly/monthly)
    - 500                     -> SERVER_ERROR

Note: error bodies arrive with ``content-type: text/plain;charset=UTF-8``
(the gateway constructs ``new Response(body)`` without a JSON content-type),
so the body is parsed as JSON regardless of the content-type header.
"""

import json
import time
import urllib.parse
import uuid
from typing import Dict, List, Optional

import requests

from constant.system import NO_RETRY_ERROR_CODES
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_error_message, http_error_status, request
from tools.logger import get_logger
from tools.utils import trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")


class OpenCodeProvider(AIBaseProvider):
    """OpenCode Go subscription provider implementation.

    Validates ``sk-`` Bearer keys against the OpenCode Go subscription endpoint
    via a minimal chat-completion probe with model ``glm-5.3``.
    """

    # Error types from the opencode gateway that map to NO_QUOTA (all return
    # HTTP 401, not 402 — the gateway groups auth/billing/limit errors under
    # the same status code; only the error.type field distinguishes them).
    _NO_QUOTA_ERROR_TYPES = frozenset(
        {"CreditsError", "MonthlyLimitError", "UserLimitError"}
    )

    def __init__(self, conditions: List[Condition], **kwargs):
        self.defaults(
            kwargs,
            {
                "name": "opencode",
                "base_url": "https://opencode.ai/zen/go/v1",
                "completion_path": "/chat/completions",
                "model_path": "/models",
                "default_model": "glm-5.3",
            },
        )
        super().__init__(conditions=conditions, **kwargs)

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """OpenCode authenticates via a Bearer token on every endpoint."""
        headers = {"Authorization": f"Bearer {token}"}
        return self._merge_headers(headers, additional)

    def _completion_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.completion_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def _models_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.model_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Check OpenCode key validity with a minimal chat-completion probe.

        A ``GET /models`` response is NOT auth-gated (returns 200 with the full
        model list for any key, including invalid ones), so the completion probe
        is the sole validation gate.
        """
        token = trim(token)
        if not token:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        url = self._completion_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)
        headers = self._get_headers(token=token) or {}
        # The opencode gateway 400s requests without an explicit JSON content
        # type (same trap as agnes-ai — unlike OpenAI-compatible endpoints
        # that tolerate a missing header for raw data= posts).
        headers["Content-Type"] = "application/json"
        # The gateway also 400s chat requests without a session id BEFORE
        # judging the key (400 MissingSessionID "Request is missing
        # x-opencode-session and cannot be routed efficiently", measured on
        # prod 2026-09-28: every well-formed key landed in wait-check while
        # garbage keys 401'd without it). Any value satisfies the routing
        # gate; a fresh id per probe avoids sticky-affinity pinning.
        headers["x-opencode-session"] = f"harvester-check-{uuid.uuid4().hex[:12]}"

        payload = json.dumps(
            {
                "model": trim(model) or self._default_model,
                "stream": False,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
        ).encode("utf8")

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "POST",
                    url,
                    data=payload,
                    headers=headers,
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    return self._judge_chat(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_chat(code, message)
                if code in NO_RETRY_ERROR_CODES or result.reason in {
                    ErrorReason.INVALID_KEY,
                    ErrorReason.NO_ACCESS,
                    ErrorReason.NO_QUOTA,
                }:
                    return result
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code == 0:
            logger.debug(f"Check OpenCode completion failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_chat(code, message)

    def _judge_chat(self, code: int, message: str) -> CheckResult:
        """Judge the OpenCode chat-completion response.

        The opencode gateway returns error bodies as JSON with content-type
        ``text/plain`` — parse the body as JSON regardless of content-type.
        Error shape: ``{"type":"error","error":{"type":"<Class>","message":...}}``.
        """
        message = trim(message)

        if code == 200:
            try:
                json.loads(message)
            except Exception:
                # Proxies/gateways answer 200 with junk — not proof of validity
                return CheckResult.fail(ErrorReason.UNKNOWN)

            return CheckResult.success(message="OpenCode API accepted key")

        error_type = self._error_type(message)

        # 401 covers both invalid keys AND billing/quota exhaustion — only the
        # error.type field distinguishes them (source: handler.ts:490-503).
        if code == 401:
            if error_type in self._NO_QUOTA_ERROR_TYPES:
                return CheckResult.fail(ErrorReason.NO_QUOTA)
            if error_type == "ModelError":
                # Model validated BEFORE auth — a real key with a bad model
                # also returns this. Classify as BAD_REQUEST, not INVALID_KEY,
                # so the key is not silently discarded.
                return CheckResult.fail(ErrorReason.BAD_REQUEST)
            # AuthError or any other 401 → invalid/expired key
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 429:
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 400:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    @staticmethod
    def _error_type(message: str) -> str:
        """Extract the ``error.type`` field from an opencode error body.

        Body shape: ``{"type":"error","error":{"type":"AuthError","message":...}}``
        Returns ``""`` if the body is not parseable or the field is missing.
        """
        try:
            data = json.loads(message)
        except Exception:
            return ""

        if not isinstance(data, dict):
            return ""

        error = data.get("error")
        if not isinstance(error, dict):
            return ""

        return trim(str(error.get("type", "")))

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """List model IDs available to a key.

        Never used for validation (``GET /models`` is NOT auth-gated and
        returns 200 with the full list for any key); only enumerated for keys
        that already passed ``check()``.
        """
        token = trim(token)
        if not token:
            return []

        url = self._models_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=1)
        headers = self._get_headers(token=token) or {}

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "GET",
                    url,
                    headers=headers,
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    code, message = response.status_code, response.text
                    break
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        if code != 200:
            logger.debug(f"Inspect OpenCode models failed: {trim(message) or code}")
            return []

        try:
            data = json.loads(message)
        except Exception:
            return []

        if not isinstance(data, dict):
            return []

        models = data.get("data")
        if not isinstance(models, list):
            return []

        model_ids: List[str] = []
        for entry in models[:100]:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if model_id:
                model_ids.append(str(model_id))

        return model_ids


register_provider("opencode", OpenCodeProvider)
