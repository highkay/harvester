#!/usr/bin/env python3

"""Capability-tier probing for providers that declare ``capability_tiers``.

A provider task may declare ``capability_tiers``: a list of ``{name,
probe_model}`` entries. Only when it does does the judge measure, after the
cheap gate passes, *which model families a key can serve* — one ``max_tokens=1``
chat completion per tier — and record per-tier outcomes. A key that serves no
tier is not valid (the pool-usability semantic lives in ``CheckStage`` and
``AIBaseProvider._apply_capability_tiers``).

Outcome constants and the ``valid-keys-<tier>.txt`` naming are in
``constant.capability`` (the neutral module the storage and push layers import).
"""

import json
import re
import urllib.parse
from typing import Dict

from constant.capability import (
    TIER_NO_ACCESS,
    TIER_NO_MODEL,
    TIER_NO_QUOTA,
    TIER_OK,
    TIER_RETRYABLE,
    tier_file_name,
    tier_result_type,
)
from search.client import chat
from tools.utils import trim

__all__ = [
    "TIER_OK",
    "TIER_NO_ACCESS",
    "TIER_NO_QUOTA",
    "TIER_NO_MODEL",
    "TIER_RETRYABLE",
    "tier_result_type",
    "tier_file_name",
    "classify_tier",
    "probe_tier",
    "probe_tiers",
]

# Billing/quota markers that split a non-200 tier answer into NO_QUOTA instead
# of the RETRYABLE default. Deliberately wider than the provider-level judges:
# an over-broad match here merely keeps the tier out of the usable set (same
# net effect as RETRYABLE), it can never discard a key.
_QUOTA_MARKERS = re.compile(
    r"insufficient|quota|billing|credits|balance|arrearage|"
    r"余额不足|无可用资源包|请充值",
    re.I,
)

# Upstream string codes that mean "this account cannot pay for this model".
_QUOTA_CODES = frozenset({"1113"})


def classify_tier(code: int, message: str) -> str:
    """Map one tier chat-completion answer to a ``TIER_*`` outcome class.

    ``OK`` requires a real completion body: HTTP 200 whose JSON parses to a
    dict without an ``error`` object. An empty or unparseable 200 body is a
    transport artefact (RETRYABLE), never OK.
    """
    message = trim(message or "")

    if code == 200:
        if not message:
            return TIER_RETRYABLE
        try:
            data = json.loads(message)
        except Exception:
            return TIER_RETRYABLE  # unparseable 200 (proxy/captive page)
        if isinstance(data, dict) and "error" not in data:
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                return TIER_OK
        return TIER_RETRYABLE  # 200 without a real completion is not a clean OK

    if code in (401, 403):
        return TIER_NO_ACCESS

    if code == 402:
        return TIER_NO_QUOTA

    if code == 404:
        return TIER_NO_MODEL

    if message and _quota_code(message):
        return TIER_NO_QUOTA

    # Quota markers may ride a 400/429 (e.g. DashScope Arrearage, Ark arrearage).
    if _QUOTA_MARKERS.search(message):
        return TIER_NO_QUOTA

    # Timeout / connection (code 0), 5xx and every other non-200 stay retryable:
    # a transient fault must not pull a key out of a tier that another attempt
    # would have served.
    return TIER_RETRYABLE


def _quota_code(message: str) -> bool:
    """True when the body carries a known balance/entitlement error code."""
    try:
        data = json.loads(message)
    except (TypeError, ValueError):
        return False
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return False
    code = str(error.get("code", "")).strip()
    return code in _QUOTA_CODES


def probe_tier(provider, token: str, probe_model: str) -> str:
    """One ``max_tokens=1`` chat completion against ``probe_model``."""
    url = urllib.parse.urljoin(provider._base_url, provider.completion_path)
    headers = provider._get_headers(token=token)
    if not headers:
        return TIER_RETRYABLE

    code, message = chat(
        url=url,
        headers=headers,
        model=probe_model,
        params={
            "model": probe_model,
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        },
        retries=provider._get_retries(default=2),
        timeout=provider._get_timeout(default=10),
        use_proxy=provider._get_use_proxy(),
    )
    return classify_tier(code, message)


def probe_tiers(provider, token: str) -> Dict[str, str]:
    """Probe declared capability tiers; returns ``{name: outcome}``.

    ``tier_mode=fan_out`` (default) probes every tier. ``exclusive`` probes in
    declared order and stops at the first OK tier, so a key is admitted into
    exactly one tier group (glm 5.3/4.7 split). A non-OK tier never stops the
    walk; if no tier is OK the full outcome map still drives the verdict.
    """
    tiers: Dict[str, str] = {}
    exclusive = getattr(provider, "tier_mode", "fan_out") == "exclusive"
    for entry in provider.capability_tiers:
        if not isinstance(entry, dict):
            continue
        name = trim(entry.get("name", ""))
        probe_model = trim(entry.get("probe_model", ""))
        if not name or not probe_model:
            continue
        outcome = probe_tier(provider, token, probe_model)
        tiers[name] = outcome
        if exclusive and outcome == TIER_OK:
            break
    return tiers