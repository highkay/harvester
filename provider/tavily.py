#!/usr/bin/env python3

"""
Tavily provider implementation.
"""

import json
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

from constant.system import NO_RETRY_ERROR_CODES
from core.enums import ErrorReason
from core.models import CheckResult, Condition
from search.client import http_error_message, http_error_status, http_get, request
from tools.logger import get_logger
from tools.utils import trim

from .base import AIBaseProvider
from .registry import register_provider

logger = get_logger("provider")

# Verdicts the FREE /usage pre-filter is allowed to end a check with: the states
# /usage can actually see. Everything else falls through to the /search probe,
# because /usage cannot see a disabled account (measured 2026-09-23).
_USAGE_TERMINAL_REASONS = frozenset(
    {ErrorReason.INVALID_KEY, ErrorReason.NO_ACCESS, ErrorReason.NO_QUOTA}
)


class TavilyProvider(AIBaseProvider):
    """Tavily API provider implementation."""

    def __init__(self, conditions: List[Condition], **kwargs):
        config = self.extract(
            kwargs,
            {
                "name": "tavily",
                "base_url": "https://api.tavily.com",
                "completion_path": "/search",
                "model_path": "/usage",
                "default_model": "tavily-search",
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

    def _get_headers(self, token: str, additional: Optional[Dict] = None) -> Optional[Dict]:
        """Get headers for Tavily API requests."""
        token = trim(token)
        if not token:
            return None

        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        return self._merge_headers(headers, additional)

    def _usage_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.model_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def _search_url(self, address: str = "", endpoint: str = "") -> str:
        base_url = trim(address) or self._base_url
        path = trim(endpoint) or self.completion_path
        return urllib.parse.urljoin(base_url, path.removeprefix("/"))

    def _probe_body(self) -> Dict[str, Any]:
        """Minimal /search payload: one result, basic depth = 1 search credit."""
        return {"query": "harvester key validation probe", "max_results": 1, "search_depth": "basic"}

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "") -> CheckResult:
        """Validate a Tavily key against the REAL endpoint, in two stages.

        Neither endpoint is sufficient alone (both measured 2026-09-23):

        * ``GET /usage`` is FREE and answers 200 for any authentic key, but it
          cannot see a **disabled account** — a key whose account Tavily turned
          off for an unpaid pay-as-you-go balance reported
          ``plan_usage 0/1000 paygo_usage 0/20000`` on /usage while
          ``POST /search`` answered ``402 {"detail":{"error":"Your account is
          currently disabled. This is likely due to unpaid pay-as-you-go
          balance."}}``.
        * ``POST /search`` is authoritative (it is what the proxy pool serves)
          but consumes one search credit.

        So: stage 1 reads /usage (free) and returns a terminal verdict for the
        states it CAN see (spent plan / per-key limit -> NO_QUOTA, auth errors);
        stage 2 only runs when stage 1 saw nothing terminal, and probes /search.
        Keys from disabled accounts were previously classified VALID, pushed to
        the pool, and then returned 402 to every client request.
        """
        headers = self._get_headers(token=token)
        if not headers:
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        usage_result = self._check_usage(headers=headers, address=address, endpoint=endpoint)
        if usage_result is not None:
            return usage_result

        return self._check_search(headers=headers, address=address, endpoint=endpoint)

    def _check_usage(self, headers: Dict[str, str], address: str = "", endpoint: str = "") -> Optional[CheckResult]:
        """Free /usage pre-filter. Returns a TERMINAL failure, else None.

        Only INVALID_KEY / NO_ACCESS / NO_QUOTA end the check here: they are the
        states /usage can see, and they save the /search credit. A healthy (or
        unreadable) /usage answer returns None so the authoritative /search probe
        decides — that is the only way to see a disabled account.
        """
        url = self._usage_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request("GET", url, headers=headers, timeout=timeout, use_proxy=self._get_use_proxy()) as response:
                    result = self._judge_usage(response.status_code, response.text)
                    return result if result.reason in _USAGE_TERMINAL_REASONS else None
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_usage(code, message)
                if result.reason in _USAGE_TERMINAL_REASONS:
                    return result
            except requests.exceptions.Timeout:
                code, message = 0, "timeout"
            except Exception as e:
                code, message = 0, str(e)

            if attempt < retries - 1:
                time.sleep(1)

        logger.debug(f"Tavily /usage pre-filter inconclusive: {message or code}")
        return None

    def _check_search(self, headers: Dict[str, str], address: str = "", endpoint: str = "") -> CheckResult:
        """Authoritative /search probe (1 credit) with the provider's retry budget."""
        url = self._search_url(address=address, endpoint=endpoint)
        timeout = self._get_timeout(default=10)
        retries = self._get_retries(default=2)

        code, message = 0, ""
        for attempt in range(max(1, retries)):
            try:
                with request(
                    "POST",
                    url,
                    headers=headers,
                    json=self._probe_body(),
                    timeout=timeout,
                    use_proxy=self._get_use_proxy(),
                ) as response:
                    return self._judge_search(response.status_code, response.text)
            except requests.exceptions.HTTPError as e:
                code = http_error_status(e)
                message = http_error_message(e)

                result = self._judge_search(code, message)
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
            logger.debug(f"Check Tavily search probe failed: {message}")
            return CheckResult.fail(ErrorReason.TIMEOUT if message == "timeout" else ErrorReason.NETWORK_ERROR)

        return self._judge_search(code, message)

    def _judge_search(self, code: int, message: str) -> CheckResult:
        """Judge the /search probe — the endpoint the proxy pool actually serves."""
        message = trim(message)
        text = self._message_text(message)

        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            # A 200 without a results array is not a search answer (proxy
            # interstitial / captive portal body) — never a valid verdict.
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                return CheckResult.fail(ErrorReason.UNKNOWN)

            return CheckResult.success(message="Tavily /search probe accepted key")

        if code == 401 or re.findall(r"invalid\s+(api\s+)?key|unauthorized|unauthenticated", text, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        # 402 = payment required: spent plan, spent per-key limit, or a disabled
        # account ("account is currently disabled ... unpaid pay-as-you-go
        # balance"), which /usage cannot see. 432/433 are Tavily's own limit
        # codes (the proxy's own sync maps them to MarkExhausted).
        if code in (402, 432, 433) or re.findall(
            r"insufficient|quota|credit|billing|usage\s+limit|pay[- ]?as[- ]?you[- ]?go|account\s+is\s+(currently\s+)?disabled|plan\s+limit",
            text,
            flags=re.I,
        ):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429 or re.findall(r"rate\s*limit|too\s+many\s+requests", text, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 400:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    def _judge_usage(self, code: int, message: str) -> CheckResult:
        """Judge Tavily usage endpoint response.

        The /usage endpoint returns 200 for any authentic key. We parse the
        response body to detect plan-level quota exhaustion: when
        ``account.plan_usage >= account.plan_limit`` the key will return 402
        on actual /search calls, so we classify it as NO_QUOTA instead of
        valid — preventing useless keys from being pushed to the proxy pool.
        """
        message = trim(message)
        text = self._message_text(message)

        if code == 200:
            try:
                data = json.loads(message)
            except Exception:
                return CheckResult.fail(ErrorReason.UNKNOWN)

            if not isinstance(data, dict):
                return CheckResult.fail(ErrorReason.UNKNOWN)

            if self._is_quota_exhausted(data):
                return CheckResult.fail(ErrorReason.NO_QUOTA)

            return CheckResult.success(message="Tavily usage endpoint accepted key with remaining quota")

        if code == 401 or re.findall(r"invalid\s+(api\s+)?key|unauthorized|unauthenticated", text, flags=re.I):
            return CheckResult.fail(ErrorReason.INVALID_KEY)

        if code == 403:
            return CheckResult.fail(ErrorReason.NO_ACCESS)

        if code == 402 or re.findall(r"insufficient|quota|credit|billing|usage\s+limit", text, flags=re.I):
            return CheckResult.fail(ErrorReason.NO_QUOTA)

        if code == 429 or re.findall(r"rate\s*limit|too\s+many\s+requests", text, flags=re.I):
            return CheckResult.fail(ErrorReason.RATE_LIMITED)

        if code == 400:
            return CheckResult.fail(ErrorReason.BAD_REQUEST)

        if code >= 500:
            return CheckResult.fail(ErrorReason.SERVER_ERROR)

        return CheckResult.fail(ErrorReason.UNKNOWN)

    @staticmethod
    def _is_quota_exhausted(data: dict) -> bool:
        """Detect whether the /usage response indicates an exhausted key.

        Checks two levels:
        1. Account plan: ``plan_usage >= plan_limit`` (when plan_limit is a number)
        2. Per-key limit: ``usage >= limit`` (when key.limit is a number)

        Pay-as-you-go credits (``paygo_usage < paygo_limit``) override plan
        exhaustion — a key with paygo balance can still make search calls.
        """
        account = data.get("account")
        if isinstance(account, dict):
            plan_limit = account.get("plan_limit")
            plan_usage = account.get("plan_usage")
            if isinstance(plan_limit, (int, float)) and isinstance(plan_usage, (int, float)):
                if plan_usage >= plan_limit:
                    # Plan exhausted — check if paygo credits are still available
                    paygo_limit = account.get("paygo_limit")
                    paygo_usage = account.get("paygo_usage")
                    if isinstance(paygo_limit, (int, float)) and isinstance(paygo_usage, (int, float)):
                        if paygo_usage < paygo_limit:
                            return False
                    return True

        key_info = data.get("key")
        if isinstance(key_info, dict):
            key_limit = key_info.get("limit")
            key_usage = key_info.get("usage")
            if isinstance(key_limit, (int, float)) and isinstance(key_usage, (int, float)):
                if key_usage >= key_limit:
                    return True

        return False

    @staticmethod
    def _message_text(message: str) -> str:
        try:
            data = json.loads(message)
        except Exception:
            return message

        if not isinstance(data, dict):
            return str(data)

        parts = []
        for field in ("error", "message", "detail"):
            value = data.get(field)
            if value:
                parts.append(str(value))

        return " ".join(parts) or json.dumps(data, ensure_ascii=False, sort_keys=True)

    def inspect(self, token: str, address: str = "", endpoint: str = "") -> List[str]:
        """Fetch Tavily key usage/account audit details."""
        headers = self._get_headers(token=token)
        if not headers:
            return []

        url = self._usage_url(address=address, endpoint=endpoint)
        try:
            content = http_get(
                url=url,
                headers=headers,
                retries=self._get_retries(default=2),
                interval=1,
                timeout=self._get_timeout(default=10),
                use_proxy=self._get_use_proxy(),
            )
        except Exception as e:
            logger.debug(f"Inspect Tavily usage failed: {e}")
            return []

        if not content:
            return []

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse Tavily usage response: {content}")
            return []

        return self._format_audit_items(data)

    def _format_audit_items(self, data: Any) -> List[str]:
        items: List[str] = []
        self._flatten_audit("", data, items)
        return items

    def _flatten_audit(self, prefix: str, value: Any, items: List[str]) -> None:
        if len(items) >= 100:
            return

        if isinstance(value, dict):
            for key in sorted(value.keys()):
                name = f"{prefix}.{key}" if prefix else str(key)
                self._flatten_audit(name, value[key], items)
            return

        if isinstance(value, list):
            items.append(f"{prefix}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
            return

        items.append(f"{prefix}: {value}")


register_provider("tavily", TavilyProvider)
