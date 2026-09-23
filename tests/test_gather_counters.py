#!/usr/bin/env python3

"""Zero-yield observability (audit JOB 1a): per-provider gather-outcome counters.

Given the persistence ResultManager and the AcquisitionStage gather worker,
When a link fetch succeeds with candidates / succeeds empty / fails,
Then the provider's gather_ok / gather_empty / gather_error_404 /
gather_error_other counters reflect the outcome — thread-safe, additive,
and reachable from the stage worker via the provider-instance registry in
storage.persistence (and from web/runner.py via gather_counters()).
"""

from __future__ import annotations

import gc
import tempfile
import threading
import unittest
from types import SimpleNamespace
from typing import Any, List
from unittest import mock

from config.schemas import StageConfig, TaskConfig
from core.models import AcquisitionTask, CheckResult, Patterns, ResultStorage, Service
from core.types import IProvider
from stage.base import StageResources
from stage.definition import AcquisitionStage
from storage.persistence import (
    GATHER_EMPTY,
    GATHER_ERROR_404,
    GATHER_ERROR_OTHER,
    GATHER_OK,
    ResultManager,
    lookup_result_manager,
    record_gather_outcome,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILENAMES = {
    "valid": "valid-keys.txt",
    "invalid": "invalid-keys.txt",
    "no_quota": "no-quota-keys.txt",
    "wait_check": "wait-check-keys.txt",
    "material": "material.txt",
    "links": "links.txt",
}


class _FakeProvider(IProvider):
    """Weakref-able IProvider double (same shape as _ExplodingProvider in
    tests/test_stage_retry_requeue.py). SimpleNamespace CANNOT serve here:
    it is not weak-referenceable and the registry keys are weak."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def conditions(self) -> List:
        return []

    @property
    def result(self) -> ResultStorage:
        return ResultStorage(folder=self._name, filenames=dict(_FILENAMES))

    def get_patterns(self) -> Patterns:
        return Patterns()

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "", **kwargs) -> CheckResult:
        return CheckResult()

    def inspect(self, token: str, address: str = "", endpoint: str = "", **kwargs) -> List[str]:
        return []


def _fake_provider(name: str = "test") -> _FakeProvider:
    return _FakeProvider(name)


def _make_manager(tmpdir: str, provider: IProvider) -> ResultManager:
    return ResultManager(
        provider,
        tmpdir,
        batch_size=50,
        save_interval=30.0,
        simple=True,
        shutdown_timeout=1.0,
    )


class _Auth:
    """Minimal IAuthProvider stub (same shape as test_stage_retry_requeue)."""

    def get_session(self):
        return ""

    def get_token(self):
        return "token"

    def get_credential(self, prefer_token: bool = True):
        return "token", "api"

    def get_user_agent(self) -> str:
        return "test-agent"


def _resources(providers: dict) -> StageResources:
    return StageResources(
        limiter=mock.MagicMock(),
        providers=providers,
        config=mock.MagicMock(),
        task_configs={
            "test": TaskConfig(
                name="test", provider_type="openai_like", stages=StageConfig()
            )
        },
        auth=_Auth(),
    )


def _acquisition_task() -> AcquisitionTask:
    return AcquisitionTask(
        provider="test", url="https://example.com/files/env.txt", key_pattern="k-x"
    )


# ---------------------------------------------------------------------------
# ResultManager counter units
# ---------------------------------------------------------------------------


class TestResultManagerGatherCounters(unittest.TestCase):
    """Given a live ResultManager,
    When outcomes are recorded,
    Then exactly the matching counter moves and the snapshot stays complete.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = _fake_provider()
        self.manager = _make_manager(self._tmp.name, self.provider)

    def tearDown(self) -> None:
        self.manager.stop()
        self._tmp.cleanup()

    def test_counters_start_at_zero(self) -> None:
        counters = self.manager.gather_counters()
        self.assertEqual(
            counters,
            {
                "gather_ok": 0,
                "gather_empty": 0,
                "gather_error_404": 0,
                "gather_error_other": 0,
                "gather_error": 0,
            },
        )

    def test_each_outcome_bumps_only_its_counter(self) -> None:
        self.manager.record_gather_outcome(GATHER_OK)
        self.manager.record_gather_outcome(GATHER_OK)
        self.manager.record_gather_outcome(GATHER_EMPTY)
        self.manager.record_gather_outcome(GATHER_ERROR_404)
        self.manager.record_gather_outcome(GATHER_ERROR_OTHER)
        self.manager.record_gather_outcome(GATHER_ERROR_OTHER)
        self.manager.record_gather_outcome(GATHER_ERROR_OTHER)

        counters = self.manager.gather_counters()
        self.assertEqual(counters["gather_ok"], 2)
        self.assertEqual(counters["gather_empty"], 1)
        self.assertEqual(counters["gather_error_404"], 1)
        self.assertEqual(counters["gather_error_other"], 3)
        # Derived aggregate
        self.assertEqual(counters["gather_error"], 4)

    def test_unknown_outcome_is_ignored_without_new_attribute(self) -> None:
        """A caller typo must never invent a counter or kill a worker."""
        self.manager.record_gather_outcome("gather_bogus")
        counters = self.manager.gather_counters()
        self.assertEqual(sum(counters.values()), 0)
        self.assertFalse(hasattr(self.manager, "gather_bogus"))

    def test_concurrent_bumps_are_thread_safe(self) -> None:
        """8 threads x 250 bumps must land exactly 2000 (no lost updates)."""
        threads = [
            threading.Thread(
                target=lambda: [
                    self.manager.record_gather_outcome(GATHER_OK) for _ in range(250)
                ]
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.manager.gather_counters()["gather_ok"], 2000)


# ---------------------------------------------------------------------------
# Provider-instance registry + module-level helpers
# ---------------------------------------------------------------------------


class TestGatherOutcomeRegistry(unittest.TestCase):
    """Given ResultManagers keyed by provider instance,
    When the module-level record_gather_outcome() is called,
    Then the matching manager's counter moves and missing managers are a no-op.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_module_function_routes_to_live_manager(self) -> None:
        provider = _fake_provider("route-test")
        manager = _make_manager(self._tmp.name, provider)
        try:
            record_gather_outcome(provider, GATHER_EMPTY)
            self.assertEqual(manager.gather_counters()["gather_empty"], 1)
            self.assertIs(lookup_result_manager(provider), manager)
        finally:
            manager.stop()

    def test_module_function_noop_for_none_or_unregistered_provider(self) -> None:
        """Stage workers look providers up with .get() — None must not raise."""
        record_gather_outcome(None, GATHER_OK)  # must not raise
        record_gather_outcome(_fake_provider("never-registered"), GATHER_OK)
        self.assertIsNone(lookup_result_manager(None))

    def test_non_weakrefable_provider_never_breaks_persistence(self) -> None:
        """SimpleNamespace cannot be a weakref key: manager init, lookup and
        record must all degrade silently (observability must never be fatal).
        The double is deliberately NOT an IProvider — typed Any to pin that
        even a wrong-typed caller can never crash the pipeline."""
        provider: Any = SimpleNamespace(
            name="weakless",
            result=SimpleNamespace(folder="weakless", filenames=dict(_FILENAMES)),
        )
        manager = _make_manager(self._tmp.name, provider)
        try:
            record_gather_outcome(provider, GATHER_OK)  # must not raise
            self.assertIsNone(lookup_result_manager(provider))
            # Direct method path still works
            manager.record_gather_outcome(GATHER_OK)
            self.assertEqual(manager.gather_counters()["gather_ok"], 1)
        finally:
            manager.stop()

    def test_registry_releases_dead_manager(self) -> None:
        """Weak value: a finished run's manager must not stay reachable."""
        provider = _fake_provider("dead-test")
        manager = _make_manager(self._tmp.name, provider)
        manager.stop()
        del manager
        gc.collect()
        self.assertIsNone(lookup_result_manager(provider))


# ---------------------------------------------------------------------------
# AcquisitionStage._acquisition_worker integration
# ---------------------------------------------------------------------------


class TestAcquisitionWorkerBumpsCounters(unittest.TestCase):
    """Given a gather worker and a live result manager for its provider,
    When the fetch outcome is known (candidates / empty / exception),
    Then the matching counter is bumped exactly once per attempt.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.provider = _fake_provider("test")
        self.manager = _make_manager(self._tmp.name, self.provider)
        self.stage = AcquisitionStage(
            _resources({"test": self.provider}), handler=lambda output: None
        )
        # get_user_agent() needs a loaded global config (same patch as
        # tests/test_stage_retry_requeue.py)
        ua_patcher = mock.patch(
            "stage.definition.get_user_agent", return_value="test-agent"
        )
        ua_patcher.start()
        self.addCleanup(ua_patcher.stop)

    def tearDown(self) -> None:
        self.manager.stop()
        self._tmp.cleanup()

    def test_candidates_bump_gather_ok(self) -> None:
        service = Service(address="https://api.example.com", key="k-1")
        with mock.patch("stage.definition.client.collect", return_value=[service]):
            output = self.stage._acquisition_worker(_acquisition_task())

        assert output is not None  # narrowing for the type checker
        # One CHECK task + one MATERIAL result + the processed link recorded
        self.assertEqual(len(output.new_tasks), 1)
        self.assertEqual(len(output.results), 1)
        counters = self.manager.gather_counters()
        self.assertEqual(counters["gather_ok"], 1)
        self.assertEqual(counters["gather_empty"], 0)

    def test_empty_extraction_bumps_gather_empty(self) -> None:
        with mock.patch("stage.definition.client.collect", return_value=[]):
            output = self.stage._acquisition_worker(_acquisition_task())

        assert output is not None  # narrowing for the type checker
        self.assertEqual(output.new_tasks, [])
        self.assertEqual(self.manager.gather_counters()["gather_empty"], 1)

    def test_file_not_found_bumps_error_404(self) -> None:
        with mock.patch(
            "stage.definition.client.collect",
            side_effect=FileNotFoundError("File not found (HTTP 404)"),
        ):
            output = self.stage._acquisition_worker(_acquisition_task())

        self.assertIsNone(output)
        self.assertEqual(self.manager.gather_counters()["gather_error_404"], 1)
        self.assertEqual(self.manager.gather_counters()["gather_error_other"], 0)

    def test_retryable_error_bumps_error_other_and_reraises(self) -> None:
        """A requeued fetch counts the failed attempt AND still propagates."""
        with mock.patch(
            "stage.definition.client.collect",
            side_effect=ConnectionError("HTTP 503 error: gateway"),
        ):
            with self.assertRaises(ConnectionError):
                self.stage._acquisition_worker(_acquisition_task())

        self.assertEqual(self.manager.gather_counters()["gather_error_other"], 1)

    def test_worker_survives_without_registered_manager(self) -> None:
        """providers.get() miss → counter bump is a silent no-op."""
        orphan_stage = AcquisitionStage(_resources({}), handler=lambda output: None)
        with mock.patch("stage.definition.client.collect", return_value=[]):
            output = orphan_stage._acquisition_worker(_acquisition_task())

        assert output is not None  # narrowing for the type checker
        self.assertEqual(self.manager.gather_counters()["gather_ok"], 0)
        self.assertEqual(self.manager.gather_counters()["gather_empty"], 0)

    def test_per_attempt_counting_on_retries(self) -> None:
        """Two failed attempts of the SAME URL bump twice (attempt metric)."""
        with mock.patch(
            "stage.definition.client.collect",
            side_effect=TimeoutError("request timed out"),
        ):
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    self.stage._acquisition_worker(_acquisition_task())

        self.assertEqual(self.manager.gather_counters()["gather_error_other"], 2)


if __name__ == "__main__":
    unittest.main()
