#!/usr/bin/env python3

"""Axis-routing tests for RefineEngine._divide_with_language (2026-10-19).

Measured on prod (six-agent audit, agent-e-search.md section P3): a dork
qualified by a file-class qualifier (``extension:`` / ``filename:`` /
``path:``) has no satisfiable ``language:`` partition — 27/27 refined
language: children of extension:-qualified dorks returned 0 results (270
wasted search requests on a day all five affected providers run). File-class
dorks must route straight to the ``size:`` axis (0% zero-result measured).

NOTE: the engine is a singleton; tests only call the pure routing method, so
no shared state leaks between cases.
"""

import unittest

from constant.search import POPULAR_LANGUAGES, SIZE_RANGES
from search.github.refine.engine import RefineEngine


class TestFileClassAxisRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RefineEngine.get_instance()

    def test_extension_qualified_query_goes_straight_to_size(self) -> None:
        queries = self.engine._divide_with_language('"tvly-" extension:env')
        self.assertEqual(len(SIZE_RANGES), len(queries))
        for query in queries:
            self.assertIn("size:", query)
            self.assertNotIn("language:", query)

    def test_filename_qualified_query_goes_straight_to_size(self) -> None:
        queries = self.engine._divide_with_language('"BRAVE_API_KEY" filename:.env')
        self.assertEqual(len(SIZE_RANGES), len(queries))
        for query in queries:
            self.assertIn("size:", query)

    def test_path_qualified_query_goes_straight_to_size(self) -> None:
        queries = self.engine._divide_with_language('"sk-" path:config')
        self.assertEqual(len(SIZE_RANGES), len(queries))

    def test_file_class_with_size_axis_is_terminal(self) -> None:
        queries = self.engine._divide_with_language('"tvly-" extension:env size:1..1000')
        self.assertEqual(['"tvly-" extension:env size:1..1000'], queries)

    def test_plain_query_still_uses_language_axis(self) -> None:
        queries = self.engine._divide_with_language('"TAVILY_API_KEY"')
        self.assertEqual(len(POPULAR_LANGUAGES), len(queries))
        for query in queries:
            self.assertIn("language:", query)
            self.assertNotIn("size:", query)

    def test_language_qualified_query_uses_size_axis(self) -> None:
        queries = self.engine._divide_with_language('"TAVILY_API_KEY" language:Python')
        self.assertEqual(len(SIZE_RANGES), len(queries))
        for query in queries:
            self.assertIn("size:", query)

    def test_language_and_size_is_terminal(self) -> None:
        base = '"TAVILY_API_KEY" language:Python size:1..1000'
        self.assertEqual([base], self.engine._divide_with_language(base))

    def test_empty_query_returns_empty(self) -> None:
        self.assertEqual([], self.engine._divide_with_language(""))


if __name__ == "__main__":
    unittest.main()
