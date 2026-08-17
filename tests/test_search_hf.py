#!/usr/bin/env python3

"""Unit tests for the HuggingFace Hub search backend (search/hf.py)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from config.schemas import StageConfig, TaskConfig
from constant.search import API_RESULTS_PER_PAGE
from core.enums import PipelineStage
from core.models import AcquisitionTask, SearchTask
from search import client, hf
from stage.base import StageResources
from stage.definition import SearchStage

_DATASET = "leaker/env-dump"
_TREE_URL = hf._TREE_URL.format(dataset=_DATASET)
_RESOLVE_BASE = f"https://huggingface.co/datasets/{_DATASET}/resolve/main/"


def _file(path: str, size: int = 100) -> dict:
    return {"type": "file", "path": path, "size": size, "oid": "x"}


def _dir(path: str) -> dict:
    return {"type": "directory", "path": path, "size": 0, "oid": "x"}


def _search_payload(dataset_ids) -> str:
    return json.dumps([{"id": dataset_id, "downloads": 10} for dataset_id in dataset_ids])


class HfHttpTestCase(unittest.TestCase):
    """Zero throttle, empty discovery cache, HTTP faked at the http_get seam."""

    def setUp(self):
        hf._discovery_cache.clear()
        interval = mock.patch.object(hf, "_MIN_REQUEST_INTERVAL", 0.0)
        interval.start()
        self.addCleanup(interval.stop)
        self.http_calls: list[str] = []

    def patch_http(self, tree_entries, dataset_ids=None):
        ids = [_DATASET] if dataset_ids is None else dataset_ids

        def fake_http_get(url, headers=None, params=None, retries=3, interval=1.0, timeout=10, use_proxy=True):
            self.http_calls.append(url)
            if url.startswith(f"{hf._DATASETS_URL}?"):
                return _search_payload(ids)
            if "/tree/main" in url:
                return json.dumps(tree_entries)
            raise AssertionError(f"unexpected URL: {url}")

        return mock.patch.object(hf, "http_get", side_effect=fake_http_get)


class TestTreeFiltering(HfHttpTestCase):
    def test_skips_binary_large_and_deep_entries(self):
        """Given a mixed tree, when links are collected, only small text files remain."""
        entries = [
            _file(".env", 120),
            _file("README.md", 50),
            _file("configs/app.json", 300),
            _file("notes/demo.ipynb", 400),
            _file("data/train.parquet", 10),  # binary suffix
            _file("media/LOGO.PNG", 10),  # case-insensitive suffix
            _file("archive.tar", 10),  # archive suffix
            _file("big.txt", 3 * 1024 * 1024),  # exceeds 2MB
            _file("a/b/c/d/e/f/g/h/i/deep.txt", 10),  # 9 segments deep
            _dir("data"),  # directory
        ]
        with self.patch_http(entries):
            urls, content, total = hf.search_hf("apikey", page=1, peer_page=10)

        expected = [
            _RESOLVE_BASE + "README.md",
            _RESOLVE_BASE + ".env",
            _RESOLVE_BASE + "configs/app.json",
            _RESOLVE_BASE + "notes/demo.ipynb",
        ]
        self.assertEqual(urls, expected)  # smallest files first
        self.assertEqual(content, "")
        self.assertEqual(total, 4)

    def test_caps_files_per_dataset(self):
        """Given 60 candidate files, at most 50 links are emitted per dataset."""
        entries = [_file(f"notes/f{index:02d}.txt", index + 1) for index in range(60)]
        with self.patch_http(entries):
            urls, _, total = hf.search_hf("apikey", page=1, peer_page=100)

        self.assertEqual(total, 50)
        self.assertEqual(len(urls), 50)
        self.assertEqual(urls[0], _RESOLVE_BASE + "notes/f00.txt")  # smallest kept

    def test_scans_at_most_max_datasets(self):
        """Given 20 search hits, trees are fetched for the 16 most relevant only."""
        dataset_ids = [f"user/ds{index:02d}" for index in range(20)]
        with self.patch_http([], dataset_ids=dataset_ids):
            hf.search_hf("apikey", page=1, peer_page=10)

        tree_calls = [url for url in self.http_calls if "/tree/main" in url]
        self.assertEqual(len(tree_calls), hf._MAX_DATASETS_PER_QUERY)

    def test_unknown_or_binaryish_suffixes_are_rejected(self):
        """Given non-text files, unknown/dotted suffixes are skipped."""
        entries = [
            _file(".DS_Store", 100),
            _file("saves/game.state", 100),
            _file("roms/boot.ram", 100),
            _file(".gitattributes", 100),
        ]
        with self.patch_http(entries):
            urls, _, total = hf.search_hf("apikey", page=1, peer_page=10)

        self.assertEqual((urls, total), ([], 0))

    def test_extensionless_files_need_keyish_names(self):
        """Given extension-less files, only key-material names pass the filter."""
        entries = [
            _file("credentials", 100),
            _file("dump/keys", 100),
            _file("LICENSE", 100),
            _file("notes/readme_plain", 100),  # 'readme' is not a key hint
            _file(".env", 100),  # dotted suffix, whitelisted
        ]
        with self.patch_http(entries):
            urls, _, total = hf.search_hf("apikey", page=1, peer_page=10)

        expected = [
            _RESOLVE_BASE + "credentials",
            _RESOLVE_BASE + "dump/keys",
            _RESOLVE_BASE + ".env",
        ]
        self.assertEqual(urls, expected)  # stable order: equal sizes keep input order
        self.assertEqual(total, 3)


class TestPagination(HfHttpTestCase):
    def test_pages_slice_one_discovery(self):
        """Given 25 links, pages 1..3 chunk them and page 4 is empty."""
        entries = [_file(f"dump/d{index:02d}.txt", index + 1) for index in range(25)]
        with self.patch_http(entries):
            page1, _, total1 = hf.search_hf("apikey", page=1, peer_page=10)
            page2, _, total2 = hf.search_hf("apikey", page=2, peer_page=10)
            page3, _, total3 = hf.search_hf("apikey", page=3, peer_page=10)
            page4, _, total4 = hf.search_hf("apikey", page=4, peer_page=10)

        self.assertEqual((len(page1), len(page2), len(page3), len(page4)), (10, 10, 5, 0))
        self.assertEqual(total1, 25)
        self.assertEqual((total2, total3, total4), (25, 25, 25))
        self.assertTrue(set(page1).isdisjoint(page2))
        self.assertEqual(page3, [_RESOLVE_BASE + f"dump/d{index:02d}.txt" for index in range(20, 25)])
        # one search + one tree call: page tasks reuse the cached discovery
        self.assertEqual(len(self.http_calls), 2)

    def test_invalid_inputs_return_empty(self):
        self.assertEqual(hf.search_hf("", page=1, peer_page=10), ([], "", 0))
        self.assertEqual(hf.search_hf("   ", page=1, peer_page=10), ([], "", 0))
        self.assertEqual(hf.search_hf("apikey", page=0, peer_page=10), ([], "", 0))


class TestDispatchRouting(unittest.TestCase):
    def test_hf_routes_to_hf_backend(self):
        """Given search_type=hf, search_with_count calls search_hf, not GitHub."""
        with (
            mock.patch.object(hf, "search_hf", return_value=(["u1"], "", 7)) as hf_search,
            mock.patch.object(client, "search_api_with_count") as api_search,
            mock.patch.object(client, "search_web_with_count") as web_search,
        ):
            results, total, content = client.search_with_count(
                query="apikey", session="", page=2, with_api=True, peer_page=100, search_type="hf"
            )

        self.assertEqual((results, total, content), (["u1"], 7, ""))
        hf_search.assert_called_once_with(query="apikey", page=2, peer_page=100)
        api_search.assert_not_called()
        web_search.assert_not_called()

    def test_hf_route_is_case_insensitive(self):
        with mock.patch.object(hf, "search_hf", return_value=([], "", 0)) as hf_search:
            client.search_with_count(
                query="apikey", session="", page=1, with_api=False, peer_page=20, search_type="HF"
            )
        hf_search.assert_called_once()

    def test_github_types_keep_github_path(self):
        """Given a GitHub search type, routing and encoding are unchanged."""
        with (
            mock.patch.object(client, "search_api_with_count", return_value=(["g"], 1, "c")) as api_search,
            mock.patch.object(hf, "search_hf") as hf_search,
        ):
            results, total, content = client.search_with_count(
                query="a b", session="tok", page=1, with_api=True, peer_page=100, search_type="code"
            )

        self.assertEqual((results, total, content), (["g"], 1, "c"))
        api_search.assert_called_once_with("a+b", "tok", 1, 100, search_type="code")
        hf_search.assert_not_called()


class _ForbiddenAuth:
    """Auth provider that fails loudly if the hf branch touches credentials."""

    def get_token(self) -> str:
        raise AssertionError("hf branch must not request a GitHub token")

    def get_session(self) -> str:
        raise AssertionError("hf branch must not request a GitHub session")


class _EmptyAuth:
    def get_token(self) -> str:
        return ""

    def get_session(self) -> str:
        return ""


def _make_stage(auth) -> SearchStage:
    resources = StageResources(
        limiter=mock.MagicMock(),
        providers={},
        config=mock.MagicMock(),
        task_configs={"openai": TaskConfig(name="openai", provider_type="openai", stages=StageConfig())},
        auth=auth,
    )
    return SearchStage(resources, handler=lambda _output: None)


def _hf_task(page: int = 1) -> SearchTask:
    return SearchTask(
        provider="openai",
        query="apikey",
        regex="sk-[0-9A-Za-z_-]{20,}",
        page=page,
        use_api=True,
        search_type="hf",
    )


class TestSearchStageHfBranch(unittest.TestCase):
    def test_first_page_bypasses_credential_gate(self):
        stage = _make_stage(_ForbiddenAuth())
        with mock.patch.object(client, "search_with_count", return_value=(["u"], 5, "")) as search:
            results, content, total = stage._execute_first_page_search(_hf_task())

        self.assertEqual((results, content, total), (["u"], "", 5))
        search.assert_called_once_with(
            query="apikey",
            session="",
            page=1,
            with_api=True,
            peer_page=API_RESULTS_PER_PAGE,
            search_type="hf",
        )

    def test_page_search_bypasses_credential_gate(self):
        stage = _make_stage(_ForbiddenAuth())
        with mock.patch.object(client, "search_with_count", return_value=(["u"], 5, "")) as search:
            results, content = stage._execute_page_search(_hf_task(page=2))

        self.assertEqual((results, content), (["u"], ""))
        self.assertEqual(search.call_args.kwargs["page"], 2)

    def test_worker_emits_gather_tasks_with_patterns(self):
        stage = _make_stage(_ForbiddenAuth())
        link = f"https://huggingface.co/datasets/{_DATASET}/resolve/main/.env"
        with (
            mock.patch.object(client, "search_with_count", return_value=([link], 1, "")),
            mock.patch.object(client, "get_link_index", return_value=None),
        ):
            output = stage._search_worker(_hf_task())

        assert output is not None
        gather_tasks = [
            task
            for task, name in output.new_tasks
            if name == PipelineStage.GATHER.value and isinstance(task, AcquisitionTask)
        ]
        self.assertEqual(len(gather_tasks), 1)
        self.assertEqual(gather_tasks[0].url, link)
        self.assertEqual(gather_tasks[0].key_pattern, "sk-[0-9A-Za-z_-]{20,}")

    def test_code_search_still_gated_by_credentials(self):
        """Regression: the GitHub path still returns early without credentials."""
        stage = _make_stage(_EmptyAuth())
        task = _hf_task()
        task.search_type = "code"
        with mock.patch.object(client, "search_with_count") as search:
            results, content, total = stage._execute_first_page_search(task)

        self.assertEqual((results, content, total), ([], "", 0))
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
