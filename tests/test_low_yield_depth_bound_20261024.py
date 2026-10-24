#!/usr/bin/env python3

"""Pin the 2026-10-24 mimo/qwen depth bound in the shipped example configs.

Measured basis (prod, 2026-10-24):
- mimo-sg discovered 95,742 links, queued only 19,394 (20.3%) and its single
  valid key appeared at links~7.5k; the run was 73% gather drain (138/188 min).
- The corpus bound is `max_links_per_run` (unique queued links). Pagination depth
  stays at the inert 1000 because lowering it to 3 was measured on a live run to
  INCREASE search pages from 869 to 1,179: refine triggers on total > max_pages*100.
- Weekly-era yield for these tasks is 0-2 net-new keys per run (70 d: mimo-sg 3,
  mimo-cn 2, kimi-coding 2, kimi-ai 3, kimi 4, deepseek 17), so the house weekly
  floor stays and the per-run corpus is bounded instead.

A regression that drops either bound (or copies a config without it) silently
restores the 3-13 h runs, so the parsed values are pinned here.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

from config.loader import ConfigLoader

_ROOT = pathlib.Path(__file__).resolve().parent.parent

_BOUNDED = (
    "mimo",
    "mimo-sg",
    "qwen",
    "qwen-intl",
    "deepseek",
    "kimi",
    "kimi-ai",
    "kimi-coding",
)


class TestLowYieldDepthBound(unittest.TestCase):
    def _parse(self, name: str):
        path = _ROOT / "examples" / f"config-{name}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ConfigLoader(str(path))._parse_config(data)

    def test_bounded_configs_cap_pages_and_links(self) -> None:
        for name in _BOUNDED:
            with self.subTest(config=name):
                config = self._parse(name)
                self.assertEqual(1000, config.tasks[0].max_pages, f"{name}: pagination left inert")
                self.assertEqual(9000, config.global_config.max_links_per_run, f"{name}: run corpus bound")

    def test_high_yield_configs_keep_their_depth(self) -> None:
        # The wave is scoped to the low-yield set; a copy-paste that bounds a
        # productive provider would cut real yield, so the control is pinned.
        config = self._parse("tavily")
        self.assertEqual(1000, config.tasks[0].max_pages)
        self.assertEqual(120000, config.global_config.max_links_per_run)


if __name__ == "__main__":
    unittest.main()
