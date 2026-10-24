#!/usr/bin/env python3

"""Per-run link cap: global.max_links_per_run bounds the corpus per run.

Since the 2026-09-24 egress fix search works properly, so a wide dork set walks
its full page + refinement tail and per-run corpora ballooned (openrouter 18.8k,
modelscope 35.4k, glm 119k, tavily 146.7k, deepseek 336k links) with 14-28 h
runtimes that collided with the next day's chain. The cap is enforced in
SearchStage (per-Pipeline instance = per run), 0 = unlimited, and the drop is
logged exactly once per run so the behaviour is never silent.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
import types
import unittest
from typing import Any, cast
from unittest import mock

from config.schemas import StageConfig, TaskConfig
from core.models import SearchTask, Service
from search import client
from search.github.index import LinkIndex
from stage.base import StageResources
from stage.definition import SearchStage


# Sentinel: the config key is absent in the mocked global config.
_UNSET = object()


class _Auth:
    def get_session(self):
        return ""

    def get_token(self):
        return "token"

    def get_credential(self, prefer_token: bool = True):
        return "token", "api"

    def get_user_agent(self) -> str:
        return "test-agent"


def _resources(
    max_links: int, dork_tail_empty_page_limit: Any = _UNSET
) -> StageResources:
    global_config = types.SimpleNamespace(max_links_per_run=max_links)
    if dork_tail_empty_page_limit is not _UNSET:
        global_config.dork_tail_empty_page_limit = dork_tail_empty_page_limit
    return StageResources(
        limiter=mock.MagicMock(),
        providers={},
        config=cast(
            Any,
            types.SimpleNamespace(global_config=global_config),
        ),
        task_configs={
            "test": TaskConfig(name="test", provider_type="openai_like", stages=StageConfig())
        },
        auth=_Auth(),
    )


def _task(page: int = 2) -> SearchTask:
    # page != 1 skips the pagination/refinement branch, isolating the cap.
    return SearchTask(provider="test", query='"DORK"', regex="", page=page, use_api=True, max_pages=10)


class TestSearchLinkCap(unittest.TestCase):
    def _sheet(self, links_per_call: int = 3):
        # page != 1 -> the worker calls client.search_code(query, session, page, ...)
        # which returns (links, content). Distinct URLs per call: the 2026-10-19
        # dedup fix counts each URL once per run, so a same-URL-every-call mock
        # no longer advances the cap.
        counter = {"n": 0}

        def _next(*_a: Any, **_kw: Any):
            base = counter["n"] * links_per_call
            counter["n"] += 1
            return (
                [f"https://github.com/o/r/blob/main/f{base + i}.env" for i in range(links_per_call)],
                "",
            )

        return mock.MagicMock(side_effect=_next)

    def test_cap_stops_further_search_and_logs_once(self):
        stage = SearchStage(_resources(max_links=5), handler=lambda _o: None)
        search_mock = self._sheet(links_per_call=3)

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ), self.assertLogs("stage", level="WARNING") as logs:
            first = stage.process_task(_task())
            second = stage.process_task(_task())
            third = stage.process_task(_task())
            fourth = stage.process_task(_task())

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third, "task over the cap must be dropped")
        self.assertIsNone(fourth)
        self.assertEqual(6, stage._gather_links_queued)
        self.assertEqual(2, search_mock.call_count, "no network work past the cap")
        cap_lines = [r for r in logs.records if "per-run link cap reached" in r.getMessage()]
        self.assertEqual(1, len(cap_lines), "the cap logs exactly once per run")

    def test_duplicate_links_count_once_per_run(self):
        # 2026-10-19 audit measurement: the same URL is rediscovered 2.5-4.2x
        # within a run (overlapping dorks + refine partitions), and charging
        # the cap per rediscovery bound the 120k cap at ~40k unique links —
        # nvidia/glm/tavily silently dropped 7-19% of pagination. The cap must
        # count UNIQUE links.
        stage = SearchStage(_resources(max_links=0), handler=lambda _o: None)
        dupes = ["https://github.com/o/r/blob/main/dup.env"] * 3
        search_mock = mock.MagicMock(return_value=(list(dupes), ""))

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            for _ in range(4):
                self.assertIsNotNone(stage.process_task(_task()))

        self.assertEqual(
            3,
            stage._gather_links_queued,
            "4 tasks x the same 3 URLs must charge the cap for 3 unique links, not 12",
        )

    def test_zero_cap_means_unlimited(self):
        stage = SearchStage(_resources(max_links=0), handler=lambda _o: None)
        search_mock = self._sheet(links_per_call=4)

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            for _ in range(5):
                self.assertIsNotNone(stage.process_task(_task()))

        self.assertEqual(20, stage._gather_links_queued)
        self.assertEqual(5, search_mock.call_count)

    def test_unreadable_cap_config_degrades_to_unlimited(self):
        # Production StageResources pass a real Config, but MagicMock configs
        # (many stage tests) must not explode — and must not silently cap.
        resources = _resources(0)
        resources.config = mock.MagicMock()
        stage = SearchStage(resources, handler=lambda _o: None)

        self.assertEqual(0, stage._link_cap())


class TestCapStopsFanOut(unittest.TestCase):
    """A first-page task that crosses the cap must not queue its tail.

    Pages 2..N (up to max_pages) and the ~27 refinement tasks are generated by
    `_handle_first_page_results`; if the cap is only enforced at worker entry,
    the crossing task still queues thousands of doomed tasks that churn the
    queue before being dropped one by one.
    """

    def test_crossing_task_queues_no_page_or_refine_tasks(self) -> None:
        from core.enums import PipelineStage
        from core.models import SearchTask as _SearchTask

        stage = SearchStage(_resources(max_links=2), handler=lambda _o: None)
        search_mock = mock.MagicMock(
            return_value=(
                [
                    "https://github.com/o/r/blob/main/a.env",
                    "https://github.com/o/r/blob/main/b.env",
                    "https://github.com/o/r/blob/main/c.env",
                ],
                "",
                500,
            )
        )

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ), mock.patch.object(
            stage, "_execute_first_page_search", search_mock
        ), self.assertLogs("stage", level="WARNING") as logs:
            output = stage.process_task(_task(page=1))

        self.assertIsNotNone(output)
        assert output is not None
        queued_search = [
            t for t, name in output.new_tasks
            if name == PipelineStage.SEARCH.value and isinstance(t, _SearchTask)
        ]
        self.assertEqual([], queued_search, "no page/refine fan-out past the cap")
        self.assertEqual(3, stage._gather_links_queued)
        self.assertTrue(
            any("per-run link cap reached" in r.getMessage() for r in logs.records)
        )


class TestCapQuiescence(unittest.TestCase):
    """Dropped search tasks must not block the worker/pipeline from settling.

    The pipeline finishes when every stage's queue is empty and no worker is
    active; the cap drops tasks at worker entry, so the contract is: the queue
    drains, the worker goes idle, and no task waits on a dropped one.
    """

    def test_queue_drains_and_worker_settles_after_the_cap(self) -> None:
        import threading
        import time

        stage = SearchStage(_resources(max_links=1), handler=lambda _o: None, thread_count=1)
        search_mock = mock.MagicMock(
            return_value=(
                ["https://github.com/o/r/blob/main/a.env", "https://github.com/o/r/blob/main/b.env"],
                "",
            )
        )
        for _ in range(3):
            stage.queue.put_nowait(_task())

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            stage.running = True
            worker = threading.Thread(target=stage._worker_loop, daemon=True)
            worker.start()
            deadline = time.time() + 5
            while time.time() < deadline and not (
                stage.queue.empty() and not stage._has_active_workers()
            ):
                time.sleep(0.01)
            settled = stage.queue.empty() and not stage._has_active_workers()
            stage.running = False
            worker.join(timeout=5)

        self.assertTrue(settled, "worker/pipeline never quiesced after the cap drop")
        self.assertEqual(1, search_mock.call_count, "only the first task may hit the network")
        self.assertEqual(2, stage._gather_links_queued)


class TestRepoLinkCap(unittest.TestCase):
    """Per-repo fetch cap (2026-10-19 audit, E/P7).

    Benchmark/dataset repos flood every provider's corpus (one benchmark repo
    contributed 1,587 links across 4 providers; agnes-ai's top-100 repos were
    35% of its corpus). The cap is 25 links per repo per run; over-cap links
    stay in links.txt (discovery record) but are not queued for fetch.
    """

    def test_repo_cap_queues_at_most_25_per_repo(self) -> None:
        links = [f"https://github.com/o/big-repo/blob/main/f{i}.env" for i in range(30)]
        links.append("https://github.com/other/small-repo/blob/main/app.env")
        stage = SearchStage(_resources(max_links=0), handler=lambda _o: None)
        search_mock = mock.MagicMock(return_value=(links, ""))

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            output = stage.process_task(_task())

        self.assertIsNotNone(output)
        assert output is not None
        queued = [t for t, _ in output.new_tasks]
        self.assertEqual(26, len(queued), "25 from the big repo + 1 from the small repo")
        self.assertEqual(31, stage._gather_links_queued, "the cap counter still counts every unique link")

    def test_non_github_links_are_uncapped(self) -> None:
        links = [f"https://huggingface.co/datasets/o/r/resolve/main/f{i}.txt" for i in range(30)]
        stage = SearchStage(_resources(max_links=0), handler=lambda _o: None)
        search_mock = mock.MagicMock(return_value=(links, ""))

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            output = stage.process_task(_task())

        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual(30, len(output.new_tasks))


class TestDorkTailConvergence(unittest.TestCase):
    """Dork-tail convergence: unproductive pages latch ``_dork_exhausted``.

    A page is PRODUCTIVE iff it queued at least one link for THIS run's fetch
    (``queued_new``, measured AFTER the 25/repo cap) or yielded keys extracted
    from the search content. A page returning zero links is unproductive by
    definition, and so is a page whose every link lands in an already-capped
    repo. ``_DORK_EMPTY_PAGE_LIMIT`` consecutive unproductive pages must latch
    the dork so its remaining pages cost no search request.
    """

    @staticmethod
    def _stage() -> SearchStage:
        return SearchStage(_resources(max_links=0), handler=lambda _o: None)

    def _run(self, stage: SearchStage, task: SearchTask, search_mock: Any):
        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            return stage.process_task(task)

    def test_three_empty_pages_exhaust_the_dork(self) -> None:
        # The defect: the streak bookkeeping sat inside `if results:`, so a
        # page returning zero links never counted — the deep tail kept running
        # forever. Three EXECUTED empty pages must latch the dork.
        stage = self._stage()
        dork = stage._dork_key(_task(page=2))
        search_mock = mock.MagicMock(return_value=([], ""))

        outputs = [self._run(stage, _task(page=page), search_mock) for page in (2, 3, 4)]

        self.assertTrue(all(o is not None for o in outputs), "empty pages still produce an output")
        self.assertEqual(3, search_mock.call_count, "all three pages were executed")
        self.assertEqual(3, stage._dork_tail_streak.get(dork, 0))
        self.assertIn(dork, stage._dork_exhausted, "three consecutive empty pages must latch the dork")

        # The dequeue gate: the exhausted dork's next page costs no HTTP call.
        self.assertIsNone(self._run(stage, _task(page=5), search_mock))
        self.assertEqual(3, search_mock.call_count, "an exhausted dork must not hit the search API")

    def test_all_links_over_repo_cap_count_as_unproductive(self) -> None:
        # Productive is measured AFTER the 25/repo cap: a page whose every link
        # lands in an already-capped repo fetches nothing this run, so it must
        # advance the streak exactly like an empty page.
        stage = self._stage()
        dork = stage._dork_key(_task(page=2))
        base = "https://github.com/o/big-repo/blob/main/"
        by_page = {
            2: [f"{base}f{i}.env" for i in range(30)],  # 25 queued, 5 capped
            3: [f"{base}g{i}.env" for i in range(4)],  # all capped (fresh URLs)
            4: [f"{base}h{i}.env" for i in range(4)],
            5: [f"{base}i{i}.env" for i in range(4)],
        }

        def _sheet(*_a: Any, **kwargs: Any):
            return by_page[kwargs["page"]], ""

        with mock.patch.object(client, "search_code", side_effect=_sheet), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            self.assertIsNotNone(stage.process_task(_task(page=2)))
            self.assertEqual(25, stage._repo_link_counts["o/big-repo"])
            self.assertNotIn(dork, stage._dork_tail_streak, "the page that filled the repo is productive")

            for page in (3, 4, 5):
                output = stage.process_task(_task(page=page))
                self.assertIsNotNone(output)
                assert output is not None
                self.assertEqual([], output.new_tasks, f"page {page}: every link is over the repo cap")

        self.assertEqual(3, stage._dork_tail_streak[dork])
        self.assertIn(dork, stage._dork_exhausted, "all-capped pages must not look productive")

    def test_extracted_keys_make_an_empty_page_productive(self) -> None:
        # A dork can pay for itself through in-result key extraction: a page
        # that returns no links but yields keys is productive and resets the
        # streak instead of counting toward exhaustion.
        stage = self._stage()
        task = _task(page=2)
        task.regex = '"api_key"'
        dork = stage._dork_key(task)
        stage._dork_tail_streak[dork] = 2
        service = Service(key="sk-test", address="https://example.test")
        search_mock = mock.MagicMock(return_value=([], '{"api_key": "sk-test"}'))

        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ), mock.patch.object(stage, "_extract_keys_from_content", return_value=[service]):
            output = stage.process_task(task)

        self.assertIsNotNone(output)
        assert output is not None
        self.assertEqual(1, len(output.new_tasks), "the extracted key still queues a check task")
        self.assertNotIn(dork, stage._dork_tail_streak, "an extracted key resets the unproductive streak")
        self.assertNotIn(dork, stage._dork_exhausted)


class TestConfigurableDorkTailLimit(unittest.TestCase):
    """``global.dork_tail_empty_page_limit`` drives the guard (todo 2).

    YAML value 1 exhausts a dork after ONE unproductive page; an absent (or
    unusable) key falls back to the historical constant 3; 0 disables the
    guard (a dork is never exhausted, every page keeps executing). One INFO
    summary per run reports search_pages_executed / pages_skipped_by_tail /
    dorks_exhausted.
    """

    @staticmethod
    def _stage(*, limit: Any = _UNSET) -> SearchStage:
        return SearchStage(
            _resources(max_links=0, dork_tail_empty_page_limit=limit),
            handler=lambda _o: None,
        )

    @staticmethod
    def _run_page(stage: SearchStage, page: int, search_mock: Any):
        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            return stage.process_task(_task(page=page))

    def test_config_value_one_exhausts_after_one_unproductive_page(self) -> None:
        stage = self._stage(limit=1)
        dork = stage._dork_key(_task(page=2))
        search_mock = mock.MagicMock(return_value=([], ""))

        self.assertIsNotNone(self._run_page(stage, 2, search_mock))
        self.assertEqual(1, search_mock.call_count, "the first unproductive page executes")
        self.assertEqual(1, stage._dork_tail_streak[dork])
        self.assertIn(dork, stage._dork_exhausted, "limit=1 must exhaust after ONE page")

        self.assertIsNone(self._run_page(stage, 3, search_mock))
        self.assertEqual(1, search_mock.call_count, "the exhausted dork costs no request")
        self.assertEqual(1, stage._pages_skipped_by_tail)
        self.assertEqual(1, stage._search_pages_executed)

    def test_absent_or_unusable_key_falls_back_to_three(self) -> None:
        stage = self._stage()  # key absent from the mocked global config
        self.assertEqual(3, stage._dork_tail_limit())
        self.assertEqual(3, self._stage(limit="abc")._dork_tail_limit())

        dork = stage._dork_key(_task(page=2))
        search_mock = mock.MagicMock(return_value=([], ""))
        for page in (2, 3):
            self.assertIsNotNone(self._run_page(stage, page, search_mock))
            self.assertNotIn(dork, stage._dork_exhausted)
        self.assertIsNotNone(self._run_page(stage, 4, search_mock))
        self.assertIn(dork, stage._dork_exhausted, "the third empty page exhausts")
        self.assertEqual(3, search_mock.call_count)

    def test_zero_disables_exhaustion(self) -> None:
        stage = self._stage(limit=0)
        self.assertEqual(0, stage._dork_tail_limit())
        dork = stage._dork_key(_task(page=2))
        search_mock = mock.MagicMock(return_value=([], ""))

        for page in range(2, 7):
            self.assertIsNotNone(self._run_page(stage, page, search_mock))

        self.assertEqual(5, search_mock.call_count, "0 = never skip a tail page")
        self.assertNotIn(dork, stage._dork_exhausted)
        self.assertEqual(0, stage._pages_skipped_by_tail)

    def test_one_info_summary_per_run(self) -> None:
        stage = self._stage(limit=1)
        search_mock = mock.MagicMock(return_value=([], ""))
        self._run_page(stage, 2, search_mock)  # executes, exhausts
        self._run_page(stage, 3, search_mock)  # skipped by the tail guard

        with self.assertLogs("stage", level="INFO") as logs:
            stage.stop()
            stage.stop()  # second stop must not duplicate the line

        lines = [
            r.getMessage()
            for r in logs.records
            if "dork-tail guard:" in r.getMessage()
        ]
        self.assertEqual(1, len(lines), "exactly ONE INFO summary per run")
        self.assertIn("search_pages_executed=1", lines[0])
        self.assertIn("pages_skipped_by_tail=1", lines[0])
        self.assertIn("dorks_exhausted=1", lines[0])
        self.assertIsNotNone(stage.tail_summary_text())


class TestDorkTailLimitConfig(unittest.TestCase):
    """The knob round-trips through the real YAML loader (defaults<->examples)."""

    EXAMPLE = "examples/config-tavily.yaml"

    @unittest.skipUnless(os.path.exists(EXAMPLE), "example config not present")
    def test_loader_round_trips_the_key(self) -> None:
        from config.loader import ConfigLoader

        with mock.patch.dict(
            os.environ, {"GITHUB_TOKENS": "ghp_dummy_token_for_test_123456"}, clear=False
        ):
            default = ConfigLoader(self.EXAMPLE).load()
            self.assertEqual(3, default.global_config.dork_tail_empty_page_limit)

            source = pathlib.Path(self.EXAMPLE).read_text(encoding="utf-8")
            key_line = re.compile(r"^  dork_tail_empty_page_limit:.*$", re.MULTILINE)
            self.assertRegex(
                source,
                key_line,
                "examples/config-tavily.yaml must carry the knob",
            )
            with tempfile.TemporaryDirectory() as td:
                path = pathlib.Path(td) / "tail_absent.yaml"
                path.write_text(key_line.sub("", source, count=1), encoding="utf-8")
                loaded = ConfigLoader(str(path)).load()
                self.assertEqual(
                    3, loaded.global_config.dork_tail_empty_page_limit,
                    "an absent key must fall back to the default 3",
                )

                for value, expected in (("1", 1), ("0", 0), ("abc", 3)):
                    path = pathlib.Path(td) / f"tail{value}.yaml"
                    path.write_text(
                        key_line.sub(
                            f"  dork_tail_empty_page_limit: {value}", source, count=1
                        ),
                        encoding="utf-8",
                    )
                    loaded = ConfigLoader(str(path)).load()
                    self.assertEqual(
                        expected, loaded.global_config.dork_tail_empty_page_limit
                    )


class TestCapConfigContract(unittest.TestCase):
    """The cap is a real config key: schema default 120000, 0 means unlimited.

    2026-10-24: the schema default stays 120000 (productive providers keep their
    depth), while the eight low-yield configs now SET 12000 explicitly — so the
    contract is pinned on the schema default plus the explicit-override path,
    not on one example happening to omit the key.
    """

    EXAMPLE = "examples/config-kimi-coding.yaml"

    @unittest.skipUnless(os.path.exists(EXAMPLE), "example config not present")
    def test_schema_default_and_explicit_overrides(self) -> None:
        from config.loader import ConfigLoader
        from config.schemas import GlobalConfig

        self.assertEqual(120000, GlobalConfig().max_links_per_run)

        with mock.patch.dict(
            os.environ, {"GITHUB_TOKENS": "ghp_dummy_token_for_test_123456"}, clear=False
        ):
            shipped = ConfigLoader(self.EXAMPLE).load()
            self.assertEqual(
                9000, shipped.global_config.max_links_per_run,
                "the shipped low-yield config carries its measured bound",
            )

            source = pathlib.Path(self.EXAMPLE).read_text(encoding="utf-8")
            with tempfile.TemporaryDirectory() as td:
                for value, expected in (("0", 0), ("7", 7)):
                    path = pathlib.Path(td) / f"cap{value}.yaml"
                    # The shipped example now SETS max_links_per_run explicitly
                    # (2026-10-24 depth bound), so an injected key placed above it
                    # would be shadowed by YAML's last-wins rule. Replace the
                    # existing line instead; the "7"/"0" cases still prove the
                    # override path end to end.
                    patched = re.sub(
                        r"^  max_links_per_run:.*$",
                        f"  max_links_per_run: {value}",
                        source,
                        count=1,
                        flags=re.M,
                    )
                    self.assertNotEqual(source, patched, "example must carry the key")
                    path.write_text(patched, encoding="utf-8")
                    loaded = ConfigLoader(str(path)).load()
                    self.assertEqual(expected, loaded.global_config.max_links_per_run)


class TestLinkIndexWriteGating(unittest.TestCase):
    """The link-index WRITE is gated on should_skip_known_links().

    Every shipped config sets skip_known_links=false, and search/github/index.py
    has no eviction — so an ungated write made links.db a write-only dead weight
    (1.1GB on prod). Read/dedup semantics are unchanged; only the write is bound
    to the same predicate as the read.
    """

    def _sheet(self, links_per_call: int = 3) -> Any:
        # page != 1 -> the worker calls client.search_code(query, ...) -> (links, content).
        return mock.MagicMock(
            return_value=(
                [f"https://github.com/o/r/blob/main/g{i}.env" for i in range(links_per_call)],
                "",
            )
        )

    def _index_after_run(self, *, skip_known: bool, tmp: str) -> LinkIndex:
        stage = SearchStage(_resources(max_links=0), handler=lambda _o: None)
        idx = LinkIndex(tmp, enabled=True)
        search_mock = self._sheet(links_per_call=3)
        with mock.patch.object(client, "search_code", search_mock), mock.patch.object(
            client, "get_link_index", return_value=idx
        ), mock.patch.object(client, "should_skip_known_links", return_value=skip_known):
            # page=2 isolates the index block from pagination/refinement.
            self.assertIsNotNone(stage.process_task(_task(page=2)))
        return idx

    def test_no_write_when_skip_known_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            idx = self._index_after_run(skip_known=False, tmp=tmp)
            try:
                self.assertEqual(
                    0, idx.stats("test")["count"], "skip_known=false must not write the index"
                )
            finally:
                idx.close()

    def test_write_when_skip_known_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            idx = self._index_after_run(skip_known=True, tmp=tmp)
            try:
                self.assertEqual(
                    3,
                    idx.stats("test")["count"],
                    "skip_known=true must index the discovered links (read/write parity kept)",
                )
            finally:
                idx.close()


if __name__ == "__main__":
    unittest.main()
