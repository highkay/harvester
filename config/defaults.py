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
                    "key_pattern": "gsk_[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"gsk_"'}],
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
                    "default_model": "glm-4.7-flash",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "[0-9a-f]{32}\\.[0-9a-f]{32}",
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
                    "base_url": "https://api-inference.modelscope.cn/v1",
                    "completion_path": "/chat/completions",
                    "model_path": "/models",
                    "default_model": "Qwen/Qwen3-8B",
                    "auth_key": "Authorization",
                    "extra_headers": {},
                    "api_version": "",
                    "timeout": 30,
                    "retries": 3,
                },
                "patterns": {
                    "key_pattern": "[0-9A-Za-z_-]{20,}",
                    "address_pattern": "",
                    "endpoint_pattern": "",
                    "model_pattern": "",
                },
                "conditions": [{"query": '"MODELSCOPE_API_KEY"'}],
                "rate_limit": {"base_rate": 0.2, "burst_limit": 2, "adaptive": True},
                "storage": {
                    "directory": "",
                    "plan": "",
                },
            },
        ]
    )

    return config
