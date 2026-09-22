#!/usr/bin/env python3

"""Regression tests for SearchStage first-page expansion: refine AND paginate.

Measured 2026-09-22 on production: a dork qualified by ``extension:`` has no
satisfiable ``language:`` partition, so the refine branch used to swallow
pagination entirely and cap the dork at its first page — ``"ollama" "api_key"
extension:env`` returned 3144 results while only 100 links were ever walked
(and the same 7 ollama keys were re-validated every day for 18 days).
"""

from __future__ import annotations

import unittest
from typing import List, Optional, Tuple
from unittest import mock

from config.schemas import StageConfig, TaskConfig
from core.enums import PipelineStage
from core.models import SearchTask
from stage.base import StageOutput, StageResources
from stage.definition import SearchStage


class _Auth:
    """Minimal IAuthProvider stub (the search stage only needs token lookups)."""

    def get_session(self) -> Optional[str]:
        return ""

    def get_token(self) -> Optional[str]:
        return "token"

    def get_credential(self, prefer_token: bool = True) -> Tuple[str, str]:
        return "token", "api"

    def get_user_agent(self) -> str:
        return "test-agent"


def _make_stage() -> SearchStage:
    resources = StageResources(
        limiter=mock.MagicMock(),
        providers={},
        config=mock.MagicMock(),
        task_configs={"ollama": TaskConfig(name="ollama", provider_type="ollama", stages=StageConfig())},
        auth=_Auth(),
    )
    return SearchStage(resources, handler=lambda _output: None)


def _task(query: str, max_pages: int = 1000) -> SearchTask:
    return SearchTask(provider="ollama", query=query, regex="x", page=1, use_api=True, max_pages=max_pages)


class TestFirstPageExpansion(unittest.TestCase):
    def test_extension_dork_keeps_pagination_alongside_refinement(self):
        stage = _make_stage()
        task = _task('"ollama" "api_key" extension:env')
        output = StageOutput(task=task)

        stage._handle_first_page_results(task, total=3144, output=output)

        search_tasks: List[SearchTask] = [
            t for t, name in output.new_tasks if name == PipelineStage.SEARCH.value and isinstance(t, SearchTask)
        ]
        pages = sorted(t.page for t in search_tasks if t.query == task.query)
        refined = [t for t in search_tasks if t.query != task.query]
        self.assertEqual(pages, list(range(2, 11)))  # pages 2..10 = the 1000-result cap
        self.assertTrue(refined)  # language/size refinement still happens
        self.assertTrue(all(t.page == 1 for t in refined))

    def test_single_page_result_set_adds_nothing(self):
        stage = _make_stage()
        task = _task('"OLLAMA_API_KEY"')
        output = StageOutput(task=task)

        stage._handle_first_page_results(task, total=100, output=output)

        self.assertEqual(output.new_tasks, [])


if __name__ == "__main__":
    unittest.main()