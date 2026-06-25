#!/usr/bin/env python3

"""
Shared Regex Patterns - Centralized regex compilation for performance
"""

import re
from typing import List, Pattern

# API key patterns for redaction
API_KEY_PATTERNS = [
    r"\bAIza[0-9A-Za-z_-]{35}",  # Google API keys (Gemini)
    r"\bsk-proj-[0-9A-Za-z_-]{20,}",  # OpenAI project keys
    r"\bsk-or-v1-[0-9A-Za-z_-]{20,}",  # OpenRouter keys
    r"\bcsk-[0-9A-Za-z_-]{20,}",  # Cerebras keys
    r"\bxai-[0-9A-Za-z_-]{20,}",  # xAI/Grok keys
    (
        r"(?i)\b(?:grok|xai|x_ai)?[_-]?"
        r"(?:sso|session|auth|id[_-]?token|access[_-]?token|refresh[_-]?token)"
        r"\b[\"']?\s*[:=]\s*[\"']?[0-9A-Za-z._~+/=-]{20,}[\"']?"
    ),  # Grok web/SSO token assignments
    (
        r"(?i)\b(?:__Secure-[A-Za-z0-9_.-]+|next-auth\.session-token)"
        r"\s*=\s*[0-9A-Za-z._~+/=-]{20,}"
    ),  # Grok web session cookies
    r"\bsk-[0-9A-Za-z_-]{20,}",  # OpenAI and sk- prefixed keys
    r"\banthrop[0-9A-Za-z_-]{20,}",  # Anthropic keys
    r"\bgsk_[0-9A-Za-z_-]{20,}",  # Groq/GooeyAI keys
    r"\bstab_[0-9A-Za-z_-]{20,}",  # StabilityAI keys
]

# Query parsing patterns
GITHUB_QUERY_PATTERN = r"/([^/]+)/"

# Pre-compiled patterns for performance
COMPILED_API_KEY_PATTERNS: List[Pattern[str]] = [re.compile(pattern) for pattern in API_KEY_PATTERNS]

COMPILED_GITHUB_QUERY_PATTERN: Pattern[str] = re.compile(GITHUB_QUERY_PATTERN)


def redact_api_key(key: str) -> str:
    """Redact API key for safe logging (show first 6 and last 6 characters)"""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:6]}...{key[-6:]}"


def redact_api_keys_in_text(text: str) -> str:
    """Redact all API keys found in text"""
    result = text
    for pattern in COMPILED_API_KEY_PATTERNS:
        result = pattern.sub(lambda m: redact_api_key(m.group(0)), result)
    return result


def extract_github_query_pattern(query: str) -> str:
    """Extract regex pattern from GitHub search query format"""
    match = COMPILED_GITHUB_QUERY_PATTERN.search(query)
    return match.group(1) if match else ""
