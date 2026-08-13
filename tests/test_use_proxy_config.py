#!/usr/bin/env python3

"""Unit tests for the config-driven use_proxy flag."""

from __future__ import annotations

import unittest

from config.schemas import ApiConfig
from core.models import Condition, Patterns
from provider.deepseek import DeepSeekProvider


def _make_condition() -> Condition:
    return Condition(
        query='"DEEPSEEK_API_KEY"',
        patterns=Patterns(key_pattern=r"sk-[0-9A-Za-z_-]{20,}"),
        description="test",
        enabled=True,
    )


class TestApiConfigUseProxy(unittest.TestCase):
    def test_use_proxy_defaults_to_true(self):
        # Default True = proxy routing, preserving behavior for
        # international endpoints and configs that do not specify the flag.
        self.assertIs(ApiConfig().use_proxy, True)


class TestProviderUseProxyHelper(unittest.TestCase):
    def test_get_use_proxy_false_when_constructed_with_false(self):
        provider = DeepSeekProvider(conditions=[_make_condition()], use_proxy=False)
        self.assertIs(provider._get_use_proxy(), False)

    def test_get_use_proxy_true_when_constructed_with_true(self):
        provider = DeepSeekProvider(conditions=[_make_condition()], use_proxy=True)
        self.assertIs(provider._get_use_proxy(), True)

    def test_get_use_proxy_defaults_to_true(self):
        provider = DeepSeekProvider(conditions=[_make_condition()])
        self.assertIs(provider._get_use_proxy(), True)


if __name__ == "__main__":
    unittest.main()
