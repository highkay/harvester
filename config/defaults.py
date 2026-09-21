#!/usr/bin/env python3

"""
Default Configuration Values

This module provides default configuration values for the entire application.
It ensures consistent defaults across all configuration sections.

Key Features:
- Centralized default values
- Complete configuration template
- Easy customization
- Type-safe defaults
- Auto-sync with Config schema
"""

from typing import Any, Dict

from config.schemas import Config


def get_default_config() -> Dict[str, Any]:
    """Get complete default configuration

    This function creates a Config instance with default values and converts it to a dictionary.
    Then it adds example rate_limits and tasks for demonstration purposes.
    This approach ensures automatic synchronization with the Config schema.

    Returns:
        Dict[str, Any]: Default configuration dictionary
    """

    # Convert to dictionary to get the base structure
    config = Config().to_dict()

    grok_web_sso_pattern = (
        r"(?i:\b(?:grok|xai|x_ai)?[_-]?"
        r"(?:sso|session|auth|id[_-]?token|access[_-]?token|refresh[_-]?token)"
        r"\b[\"']?\s*[:=]\s*[\"']?[0-9A-Za-z._~+/=-]{20,}[\"']?"
        r"|\b(?:__Secure-[A-Za-z0-9_.-]+|next-auth\.session-token)"
        r"\s*=\s*[0-9A-Za-z._~+/=-]{20,})"
    )

    # Add example rate limits for demonstration
    config["ratelimits"].update(
        {
            "github_api": {"base_rate": 1.0, "burst_limit": 5, "adaptive": True},
            "github_web": {"base_rate": 2.0, "burst_limit": 3, "adaptive": False},
        }
    )

    # Add example tasks for demonstration
    config["tasks"].extend(
        [
            {
                "name": "openai",
                "enabled": True,
                "provider_type": "openai_like",
                "use_api": False,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://api.openai.com",
                    "completion_path": "/v1/chat/completions",
                    "model_path": "/v1/models",
                    "default_model": "gpt-4o-mini",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "sk(?:-proj)?-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"T3BlbkFJ"'}],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "cerebras",
                "enabled": False,
                "provider_type": "cerebras",
                "use_api": False,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://api.cerebras.ai/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "gpt-oss-120b",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "csk-[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"csk-"'}],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "openrouter",
                "enabled": False,
                "provider_type": "openrouter",
                "use_api": False,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "openrouter/free",
                    "auth_key": "Authorization",
                    "extra_headers": {"X-Title": "harvester"},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "sk-or-v1-[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"sk-or-v1"'}],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "groq",
                "enabled": False,
                "provider_type": "groq",
                "use_api": False,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://api.groq.com/openai/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "llama-3.3-70b-versatile",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    # Honeypot decoys embed base64("XgroqX") == "WGdyb3FY"; the
                    # "WGdy" lookahead also catches truncated fragments. Length
                    # stays 20+ (length-independent; no measured real-key corpus).
                    # Marker words / sequential placeholders / 4-char runs are
                    # excluded too — 2026-09-06 prod audit: the pool was 100%
                    # doc placeholders (gsk_abc123…, gsk_xxxx…, gsk_test…).
                    "key_pattern": "gsk_(?!.*WGdy)(?!.*(?i:test|secret|example|demo|fake|dummy|sample|placeholder|keyhere|yourkey|localhost))(?!.*(?:abc123|xyz456|def789|ghi012|jkl345|mno678|pqr901|stu234|vwx567|1234567890|abcdefghijklmnopqrstuvwxyz|abcdefgh))(?!.*(?:X{4,}|x{4,}|A{4,}|a{4,}|B{4,}|0{4,}|9{4,}))[A-Za-z0-9]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [
                    {"query": '"gsk_"'},
                    {"query": '"GROQ_API_KEY"'},
                    {"query": '"GROQ_API_KEY="'},
                    {"query": '"GROQ_API_KEY:"'},
                    {"query": '"api.groq.com"'},
                    {"query": '"api.groq.com" "Authorization"'},
                    {"query": '"gsk_" created:>=2026-08-01'},
                ],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "grok",
                "enabled": False,
                "provider_type": "grok",
                "use_api": False,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://api.x.ai/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "grok-4",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": grok_web_sso_pattern,
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [
                    {
                        "query": '"grok.com" "access_token"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                    {
                        "query": '"grok.com" "refresh_token"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                    {
                        "query": '"grok.com" "id_token"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                    {
                        "query": '"grok.com" "session"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                    {
                        "query": '"grok.com" "sso"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                    {
                        "query": '"x.ai" "access_token"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                    {
                        "query": '"x.ai" "refresh_token"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                    {
                        "query": '"next-auth.session-token" "grok.com"',
                        "patterns": {"key_pattern": grok_web_sso_pattern},
                    },
                ],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "gemini",
                "enabled": False,
                "provider_type": "gemini",
                "use_api": False,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://generativelanguage.googleapis.com",
                    "completion_path": "/v1beta/models",
                    "model_path": "/v1beta/models",
                    "default_model": "gemini-3.5-flash",
                    "auth_key": "",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "AIza[0-9A-Za-z_-]{35}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"AIza"'}],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "tavily",
                "enabled": False,
                "provider_type": "tavily",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://api.tavily.com",
                    "completion_path": "/search",
                    "model_path": "/usage",
                    "default_model": "tavily-search",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "(?:tvly|tavily)-[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [
                    {"query": '"tvly-"'},
                    {"query": '"tavily-"'},
                    {"query": '"tvly-dev-"'},
                    {"query": '"tvly-prod-"'},
                    {"query": '"TAVILY_API_KEY"'},
                    {"query": '"TAVILY_API_KEY="'},
                    {"query": '"TAVILY_API_KEY:"'},
                    {"query": '"TAVILY_API_KEY" "tvly-"'},
                    {"query": '"api.tavily.com"'},
                    {"query": '"api.tavily.com" "Authorization"'},
                    {"query": '"api.tavily.com" "api_key"'},
                    {"query": '"tvly-" "api.tavily.com"'},
                    {"query": '"tvly-" "Authorization"'},
                    {"query": '"tvly-" "api_key"'},
                    {"query": '"tvly-" "TAVILY"'},
                    {"query": '"tvly-" language:Python'},
                    {"query": '"tvly-" language:JavaScript'},
                    {"query": '"tvly-" language:TypeScript'},
                    {"query": '"tvly-" language:Go'},
                    {"query": '"tvly-" language:Shell'},
                    {"query": '"tvly-" extension:env'},
                    {"query": '"tvly-" extension:json'},
                    {"query": '"tvly-" extension:yaml'},
                    {"query": '"tvly-" extension:yml'},
                    {"query": '"tvly-" extension:toml'},
                    {"query": '"tvly-" extension:md'},
                    {"query": '"tavily-" language:Python'},
                    {"query": '"tavily-" extension:env'},
                ],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "serpapi",
                "enabled": False,
                "provider_type": "serpapi",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://serpapi.com",
                    "completion_path": "/search.json",
                    "model_path": "/account.json",
                    "default_model": "serpapi-account",
                    "auth_key": "",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": (
                        r'(?i)(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                        r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                        r'["\']?(?<![0-9a-fA-F])([0-9a-fA-F]{20,64})(?![0-9a-fA-F])["\']?'
                    ),
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [
                    {"query": '"SERPAPI_API_KEY"'},
                    {"query": '"SERPAPI_KEY"'},
                    {"query": '"SERP_API_KEY"'},
                    {"query": '"serpapi_key"'},
                    {"query": '"SERPAPI_API_KEY="'},
                    {"query": '"SERPAPI_API_KEY:"'},
                    {"query": '"SERPAPI_KEY="'},
                    {"query": '"SERPAPI_KEY:"'},
                    {"query": '"SERPAPI_API_KEY" language:Python'},
                    {"query": '"SERPAPI_API_KEY" language:JavaScript'},
                    {"query": '"SERPAPI_API_KEY" language:TypeScript'},
                    {"query": '"SERPAPI_API_KEY" language:Go'},
                    {"query": '"SERPAPI_API_KEY" extension:env'},
                    {"query": '"SERPAPI_API_KEY" extension:json'},
                    {"query": '"SERPAPI_API_KEY" extension:yaml'},
                    {"query": '"SERPAPI_API_KEY" extension:yml'},
                    {"query": '"SERPAPI_API_KEY" extension:toml'},
                    {
                        "query": '"serpapi.com" "api_key"',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                    {
                        "query": '"serpapi.com" "api_key="',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                    {
                        "query": '"serpapi.com/search.json"',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                    {
                        "query": '"search.json?api_key="',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                    {
                        "query": '"serpapi.com" "SERPAPI"',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                    {
                        "query": '"serpapi.com" language:Python',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                    {
                        "query": '"serpapi.com" extension:env',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                    {
                        "query": '"serpapi.com" extension:yaml',
                        "patterns": {
                            "key_pattern": (
                                r'(?i)(?:(?:SERPAPI_API_KEY|SERPAPI_KEY|SERP_API_KEY|'
                                r'serpapi[_-]?(?:api[_-]?key|key))["\'\]]{0,2}\s*[:=]\s*'
                                r'["\']?|(?:[?&]|&amp;)api_key=)(?<![0-9a-fA-F])([0-9a-fA-F]{8,128})(?=["\'\s&#]|$)'
                            ),
                        },
                    },
                ],
                "rate_limit": {"base_rate": 2.0, "burst_limit": 10, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
        {
                "name": "agnes-ai",
                "enabled": False,
                "provider_type": "agnes-ai",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://apihub.agnes-ai.com/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "agnes-2.5-flash",
                    "auth_key": "",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    # Env-anchored extraction only; the lookahead excludes
                    # Anthropic/OpenAI project & service-account sk- keys.
                    # Domain conditions below widen to Bearer/quoted sk- keys.
                    "key_pattern": (
                        r"(?i)(?:AGNES_API_KEY|AGNES_AI_API_KEY|AGNES_KEY|"
                        r"agnes[_-]?(?:ai[_-]?)?api[_-]?key)"
                        r"[\"'\]]{0,2}\s*[:=]\s*[\"']?"
                        r"(sk-(?!(?:ant|proj|svcacct)-)[A-Za-z0-9]{16,64})"
                        r"[\"']?"
                    ),
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [
                    {"query": '"AGNES_API_KEY"'},
                    {"query": '"AGNES_API_KEY="'},
                    {"query": '"AGNES_API_KEY:"'},
                    {"query": '"AGNES_API_KEY" language:Python'},
                    {"query": '"AGNES_API_KEY" extension:env'},
                    {"query": '"AGNES_API_KEY" extension:yaml'},
                    {
                        "query": '"apihub.agnes-ai.com"',
                        "patterns": {
                            "key_pattern": (
                                r"(?i)(?:Bearer\s+|[\"']\s*)?"
                                r"(sk-(?!(?:ant|proj|svcacct)-)[A-Za-z0-9]{16,64})"
                            ),
                        },
                    },
                    {
                        "query": '"apihub.agnes-ai.com" "Authorization"',
                        "patterns": {
                            "key_pattern": (
                                r"(?i)(?:Bearer\s+|[\"']\s*)?"
                                r"(sk-(?!(?:ant|proj|svcacct)-)[A-Za-z0-9]{16,64})"
                            ),
                        },
                    },
                    {
                        "query": '"agnes-ai.com" "api_key"',
                        "patterns": {
                            "key_pattern": (
                                r"(?i)(?:Bearer\s+|[\"']\s*)?"
                                r"(sk-(?!(?:ant|proj|svcacct)-)[A-Za-z0-9]{16,64})"
                            ),
                        },
                    },
                ],
                "rate_limit": {"base_rate": 1.0, "burst_limit": 5, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
        {
                "name": "deepseek",
                "enabled": False,
                "provider_type": "deepseek",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://api.deepseek.com",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "deepseek-v4-flash",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "sk-[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"DEEPSEEK_API_KEY"'}],
                "rate_limit": {"base_rate": 0.2, "burst_limit": 2, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "kimi",
                "enabled": False,
                "provider_type": "kimi",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://api.moonshot.cn/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "kimi-k3",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "sk-[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"MOONSHOT_API_KEY"'}],
                "rate_limit": {"base_rate": 0.2, "burst_limit": 2, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "glm",
                "enabled": False,
                "provider_type": "glm",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "completion_path": "/chat/completions",
                    "model_path": "",
                    "default_model": "glm-4.5-flash",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "[0-9a-f]{32}\\.[A-Za-z0-9]{16,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"ZHIPUAI_API_KEY"'}],
                "rate_limit": {"base_rate": 0.2, "burst_limit": 2, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "mimo",
                "enabled": False,
                "provider_type": "mimo",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "mimo-v2.5-pro",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "tp-[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"MIMO_API_KEY"'}],
                "rate_limit": {"base_rate": 0.2, "burst_limit": 2, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "qwen",
                "enabled": False,
                "provider_type": "qwen",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "qwen-turbo",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "sk-[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"DASHSCOPE_API_KEY"'}],
                "rate_limit": {"base_rate": 0.2, "burst_limit": 2, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
            {
                "name": "modelscope",
                "enabled": False,
                "provider_type": "modelscope",
                "use_api": True,
                "max_pages": 1000,
                "stages": {
                    "search": True,
                    "gather": True,
                    "check": True,
                    "inspect": True,
                },
                "extras": {},
                "api": {
                    "base_url": "https://modelscope.cn/openapi/v1",
                    "completion_path": "/users/me",
                    "model_path": "/users/me",
                    "default_model": "modelscope-account",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                    "use_proxy": False,
                },
                "patterns": {
                    # ms- SDK/user tokens extracted only from MODELSCOPE_* env
                    # assignments (no bare branch). Domain conditions below
                    # widen to Bearer/quoted or oauth2 URL forms.
                    "key_pattern": (
                        r"(?i)(?:MODELSCOPE_API_TOKEN|MODELSCOPE_SDK_TOKEN|"
                        r"MODELSCOPE_ACCESS_TOKEN|MODELSCOPE_API_KEY)"
                        r"[\"'\]]{0,2}\s*[:=]\s*[\"']?"
                        r"(ms-[A-Za-z0-9_-]{8,})"
                        r"[\"']?"
                    ),
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [
                    {"query": '"MODELSCOPE_API_KEY"'},
                    {"query": '"MODELSCOPE_API_KEY="'},
                    {"query": '"MODELSCOPE_API_KEY:"'},
                    {"query": '"MODELSCOPE_SDK_TOKEN"'},
                    {"query": '"MODELSCOPE_SDK_TOKEN="'},
                    {"query": '"MODELSCOPE_SDK_TOKEN:"'},
                    {"query": '"MODELSCOPE_ACCESS_TOKEN"'},
                    {"query": '"MODELSCOPE_API_TOKEN"'},
                    {"query": '"MODELSCOPE_API_KEY" language:Python'},
                    {"query": '"MODELSCOPE_API_KEY" language:JavaScript'},
                    {"query": '"MODELSCOPE_API_KEY" language:TypeScript'},
                    {"query": '"MODELSCOPE_SDK_TOKEN" language:Python'},
                    {"query": '"modelscope" extension:env'},
                    {"query": '"modelscope" extension:yaml'},
                    {"query": '"modelscope" extension:json'},
                    {"query": '"modelscope" extension:toml'},
                    {
                        "query": '"modelscope.cn" "Authorization"',
                        "patterns": {
                            "key_pattern": (
                                r"(?i)(?:Bearer\s+|[\"']\s*)?"
                                r"(ms-[A-Za-z0-9_-]{8,})"
                            ),
                        },
                    },
                    {
                        "query": '"oauth2:" "modelscope.cn"',
                        "patterns": {
                            "key_pattern": r"oauth2:(ms-[A-Za-z0-9_-]{8,})@",
                        },
                    },
                    {
                        "query": '"api-inference.modelscope.cn"',
                        "patterns": {
                            "key_pattern": (
                                r"(?i)(?:Bearer\s+|[\"']\s*)?"
                                r"(ms-[A-Za-z0-9_-]{8,})"
                            ),
                        },
                    },
                    {
                        "query": '"api-inference.modelscope.cn" "Authorization"',
                        "patterns": {
                            "key_pattern": (
                                r"(?i)(?:Bearer\s+|[\"']\s*)?"
                                r"(ms-[A-Za-z0-9_-]{8,})"
                            ),
                        },
                    },
                ],
                "rate_limit": {"base_rate": 0.2, "burst_limit": 2, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
        ]
    )

    return config
