#!/usr/bin/env python3

"""Unit tests for Ollama Cloud provider."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns
from provider.ollama import OllamaProvider
from provider.registry import ProviderRegistry, get_available_providers


def _make_condition() -> Condition:
    return Condition(
        query='"OLLAMA_API_KEY"',
        patterns=Patterns(
            key_pattern=r"(?i)(?:OLLAMA_API_KEY|ollama[_-]?key)\s*[:=]\s*[\"']?([0-9A-Za-z._~+/=-]{20,})[\"']?",
        ),
        description="test",
        enabled=True,
    )


MODELS_RESPONSE = json.dumps(
    {
        "object": "list",
        "data": [
            {"id": "gpt-oss:20b-cloud", "object": "model", "owned_by": "ollama"},
            {"id": "deepseek-v3.1:671b-cloud", "object": "model", "owned_by": "ollama"},
            {"id": "qwen3-coder:480b-cloud", "object": "model", "owned_by": "ollama"},
        ],
    }
)

COMPLETION_OK = json.dumps(
    {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
)

UA_PATCH = "provider.openai_like.get_user_agent"


class TestOllamaProviderRegistration(unittest.TestCase):
    def test_registered_in_registry(self):
        providers = get_available_providers()
        self.assertIn("ollama", providers)

    def test_create_via_registry(self):
        provider = ProviderRegistry.create("ollama", conditions=[_make_condition()])
        self.assertIsInstance(provider, OllamaProvider)
        self.assertEqual(provider.name, "ollama")


class TestOllamaProviderCheck(unittest.TestCase):
    def setUp(self):
        self.provider = OllamaProvider(conditions=[_make_condition()])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_check_success(self, _ua):
        with mock.patch("provider.base.chat", return_value=(200, COMPLETION_OK)):
            result = self.provider.check(token="some-valid-ollama-key-123456")

        self.assertTrue(result.available)

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_check_invalid_key(self, _ua):
        with mock.patch("provider.base.chat", return_value=(401, '{"error": "invalid api key"}')):
            result = self.provider.check(token="bad-key")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.INVALID_KEY)

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_check_rate_limited(self, _ua):
        with mock.patch("provider.base.chat", return_value=(429, '{"error": "rate limit exceeded"}')):
            result = self.provider.check(token="some-key")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.RATE_LIMITED)

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_check_no_quota(self, _ua):
        with mock.patch("provider.base.chat", return_value=(402, '{"error": "insufficient credits"}')):
            result = self.provider.check(token="some-key")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.NO_QUOTA)

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_check_server_error(self, _ua):
        with mock.patch("provider.base.chat", return_value=(500, "internal server error")):
            result = self.provider.check(token="some-key")

        self.assertFalse(result.available)
        self.assertEqual(result.reason, ErrorReason.SERVER_ERROR)


class TestOllamaProviderInspect(unittest.TestCase):
    def setUp(self):
        self.provider = OllamaProvider(conditions=[_make_condition()])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_models(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=MODELS_RESPONSE):
            models = self.provider.inspect(token="some-valid-key")

        self.assertEqual(models, ["gpt-oss:20b-cloud", "deepseek-v3.1:671b-cloud", "qwen3-coder:480b-cloud"])

    @mock.patch(UA_PATCH, return_value="test-agent")
    def test_inspect_empty(self, _ua):
        with mock.patch("provider.openai_like.http_get", return_value=""):
            models = self.provider.inspect(token="some-key")

        self.assertEqual(models, [])


class TestOllamaLiveCheck(unittest.TestCase):
    """Live integration test - only runs if OLLAMA_LIVE_TEST=1 is set."""

    @unittest.skipUnless(os.environ.get("OLLAMA_LIVE_TEST") == "1", "Set OLLAMA_LIVE_TEST=1 to run live tests")
    @mock.patch(UA_PATCH, return_value="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    def test_live_check_and_output(self, _ua):
        provider = OllamaProvider(conditions=[_make_condition()])

        keys = [
            k.strip()
            for k in os.environ.get("OLLAMA_TEST_KEYS", "").split(",")
            if k.strip()
        ]
        if not keys:
            self.skipTest("Set OLLAMA_TEST_KEYS=key1,key2 to run live validation")

        valid_keys = []
        for key in keys:
            result = provider.check(token=key)
            if result.available:
                valid_keys.append(key)
                models = provider.inspect(token=key)
                print(f"[VALID] {key[:8]}... models={models}")
            else:
                print(f"[INVALID] {key[:8]}... reason={result.reason}")

        output_path = os.path.join(os.path.dirname(__file__), "..", "data-ollama", "valid-keys.txt")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for key in valid_keys:
                f.write(key + "\n")

        print(f"\nWrote {len(valid_keys)} valid key(s) to {os.path.abspath(output_path)}")


if __name__ == "__main__":
    unittest.main()
