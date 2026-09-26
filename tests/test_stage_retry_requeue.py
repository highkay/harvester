#!/usr/bin/env python3

"""Finding B regression: the stage retry machinery must be reachable.

``BasePipelineStage._worker_loop`` only requeues when task processing RAISES,
but every stage worker used to catch everything and ``return None`` — and even
a worker-level re-raise was swallowed one frame up by ``process_task``'s
catch-all (default ``_handle_processing_error`` returns None). Net effect:
``max_retries_requeued`` / ``retry_policy`` never fired and a dork or gather
fetch that died on a 503/TLS EOF/rate-limiter denial was lost for the run.

Now SearchStage/AcquisitionStage re-raise exactly the classes the shared
predicate ``RetryCore.should_retry_error`` treats as retryable
(ConnectionError, TimeoutError, "rate limit"/"too many requests" markers) and
override ``_handle_processing_error`` to let them escape ``process_task``;
``search.client.collect`` stopped swallowing them into ``[]``. Check/Inspect
deliberately stay conservative (non-idempotent verdict routing — see the note
on CheckStage._check_worker).
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import List, Optional
from unittest import mock

from config.schemas import StageConfig, TaskConfig
from core.enums import PipelineStage
from core.exceptions import NetworkError
from core.models import (
    AcquisitionTask,
    CheckResult,
    CheckTask,
    InspectTask,
    Patterns,
    ProviderTask,
    SearchTask,
    Service,
)
from core.types import IProvider
from search import client
from stage.base import StageOutput, StageResources
from stage.definition import AcquisitionStage, CheckStage, InspectStage, SearchStage
from tools.retry import FixedRetry


class _Auth:
    """Minimal IAuthProvider stub."""

    def get_session(self):
        return ""

    def get_token(self):
        return "token"

    def get_credential(self, prefer_token: bool = True):
        return "token", "api"

    def get_user_agent(self) -> str:
        return "test-agent"


def _resources(provider: str = "test", providers: Optional[dict] = None) -> StageResources:
    return StageResources(
        limiter=mock.MagicMock(),
        providers=providers or {},
        config=mock.MagicMock(),
        task_configs={provider: TaskConfig(name=provider, provider_type="openai_like", stages=StageConfig())},
        auth=_Auth(),
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Bounded poll — no fixed sleeps, deterministic termination."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _search_task() -> SearchTask:
    return SearchTask(provider="test", query='"SOME_DORK"', regex="", page=1, use_api=True, max_pages=10)


def _acquisition_task() -> AcquisitionTask:
    return AcquisitionTask(provider="test", url="https://example.com/files/env.txt", key_pattern="k-x")


# ---------------------------------------------------------------------------
# Precondition: collect() must stop swallowing retryable transport failures
# ---------------------------------------------------------------------------


class TestCollectPropagatesRetryableTransportErrors(unittest.TestCase):
    def test_connection_error_propagates(self):
        with mock.patch.object(client, "http_get", side_effect=ConnectionError("HTTP 503 error: gateway")):
            with self.assertRaises(ConnectionError):
                client.collect(key_pattern="k", url="https://example.com/f")

    def test_timeout_error_propagates(self):
        with mock.patch.object(client, "http_get", side_effect=TimeoutError("request timed out")):
            with self.assertRaises(TimeoutError):
                client.collect(key_pattern="k", url="https://example.com/f")

    def test_non_retryable_errors_still_degrade_to_empty(self):
        errors = (
            FileNotFoundError("File not found (HTTP 404)"),
            NetworkError("Authentication failed (HTTP 401)"),
            ValueError("bad thing"),
            RuntimeError("boom"),
        )
        for err in errors:
            with self.subTest(err=type(err).__name__):
                with mock.patch.object(client, "http_get", side_effect=err):
                    self.assertEqual([], client.collect(key_pattern="k", url="https://example.com/f"))


# ---------------------------------------------------------------------------
# Worker + process_task: retryable escapes, non-retryable stays a logged drop
# ---------------------------------------------------------------------------


class TestRetryableEscapesProcessTask(unittest.TestCase):
    def test_search_connection_error_escapes(self):
        stage = SearchStage(_resources(), handler=lambda _o: None)
        with mock.patch.object(
            client, "search_with_count", side_effect=ConnectionError("rate limiter denied for github_api")
        ):
            with self.assertRaises(ConnectionError):
                stage.process_task(_search_task())

    def test_search_timeout_error_escapes(self):
        stage = SearchStage(_resources(), handler=lambda _o: None)
        with mock.patch.object(client, "search_with_count", side_effect=TimeoutError("request timed out")):
            with self.assertRaises(TimeoutError):
                stage.process_task(_search_task())

    def test_search_non_retryable_error_is_swallowed(self):
        stage = SearchStage(_resources(), handler=lambda _o: None)
        with mock.patch.object(client, "search_with_count", side_effect=RuntimeError("boom")):
            self.assertIsNone(stage.process_task(_search_task()))

    def test_acquisition_connection_error_escapes(self):
        stage = AcquisitionStage(_resources(), handler=lambda _o: None)
        with mock.patch.object(client, "collect", side_effect=ConnectionError("HTTP 503 error: gateway")), (
            mock.patch("stage.definition.get_user_agent", return_value="test-agent")
        ):
            with self.assertRaises(ConnectionError):
                stage.process_task(_acquisition_task())

    def test_acquisition_non_retryable_error_is_swallowed(self):
        stage = AcquisitionStage(_resources(), handler=lambda _o: None)
        with mock.patch.object(client, "collect", side_effect=RuntimeError("boom")), mock.patch(
            "stage.definition.get_user_agent", return_value="test-agent"
        ):
            self.assertIsNone(stage.process_task(_acquisition_task()))


# ---------------------------------------------------------------------------
# End-to-end: the real _worker_loop requeues a retryable search failure
# ---------------------------------------------------------------------------


class TestWorkerLoopRequeue(unittest.TestCase):
    def _stage(self, handler) -> SearchStage:
        return SearchStage(
            _resources(),
            handler=handler,
            max_retries=2,
            thread_count=1,
            retry_policy=FixedRetry(max_retries=2, delay=0.0),
        )

    def _drive(self, stage: SearchStage, task: ProviderTask, worker_done) -> threading.Thread:
        stage.queue.put_nowait(task)
        stage.running = True
        worker = threading.Thread(target=stage._worker_loop, name="requeue-test", daemon=True)
        worker.start()
        worker_done.wait(timeout=5)
        return worker

    def test_retryable_search_failure_is_requeued_and_succeeds(self):
        # Given a first attempt that dies on the rate-limiter denial and a
        # second attempt that succeeds
        link = "https://github.com/o/r/blob/main/.env"
        outputs: List[StageOutput] = []
        done = threading.Event()

        def handler(output: StageOutput) -> None:
            outputs.append(output)
            done.set()

        search_mock = mock.MagicMock(
            side_effect=[ConnectionError("rate limiter denied for github_api"), ([link], 1, "")]
        )
        stage = self._stage(handler)
        task = _search_task()

        # When the real worker loop processes the task
        with mock.patch.object(client, "search_with_count", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            worker = self._drive(stage, task, done)
            stage.running = False
            worker.join(timeout=5)

        # Then the task WAS requeued (second attempt happened) and produced output
        self.assertTrue(done.is_set(), "handler never saw the retry's output")
        self.assertEqual(2, search_mock.call_count)
        self.assertEqual(1, task.attempts)
        gather_tasks = [
            t for t, name in outputs[0].new_tasks if name == PipelineStage.GATHER.value and isinstance(t, AcquisitionTask)
        ]
        self.assertEqual([link], [t.url for t in gather_tasks])
        self.assertTrue(_wait_until(stage.queue.empty))

    def test_non_retryable_search_failure_is_dropped_without_requeue(self):
        done = threading.Event()
        search_mock = mock.MagicMock(side_effect=RuntimeError("boom"))
        stage = self._stage(lambda _o: done.set())
        task = _search_task()

        with mock.patch.object(client, "search_with_count", search_mock):
            stage.queue.put_nowait(task)
            stage.running = True
            worker = threading.Thread(target=stage._worker_loop, name="drop-test", daemon=True)
            worker.start()
            settled = _wait_until(lambda: stage.total_processed >= 1)
            stage.running = False
            worker.join(timeout=5)

        self.assertTrue(settled)
        self.assertEqual(0, task.attempts)  # never requeued
        self.assertEqual(1, search_mock.call_count)  # never re-run
        self.assertEqual(0, stage.queue.qsize())
        self.assertFalse(done.is_set())  # no output routed


# ---------------------------------------------------------------------------
# Check/Inspect stay conservative: retryable errors must NOT requeue
# ---------------------------------------------------------------------------


class _ExplodingProvider(IProvider):
    """Stub provider whose probes die on a retryable transport error."""

    def __init__(self) -> None:
        self.check_calls = 0
        self.inspect_calls = 0

    @property
    def name(self) -> str:
        return "test"

    @property
    def conditions(self) -> List:
        return []

    @property
    def result(self):
        raise NotImplementedError

    def get_patterns(self) -> Patterns:
        return Patterns()

    def check(self, token: str, address: str = "", endpoint: str = "", model: str = "", **kwargs) -> CheckResult:
        self.check_calls += 1
        raise ConnectionError("probe transport died")

    def inspect(self, token: str, address: str = "", endpoint: str = "", **kwargs) -> List[str]:
        self.inspect_calls += 1
        raise ConnectionError("inspect transport died")


class TestCheckInspectStayConservative(unittest.TestCase):
    def test_check_worker_swallows_retryable_error_no_requeue(self):
        provider = _ExplodingProvider()
        stage = CheckStage(_resources(providers={"test": provider}), handler=lambda _o: None, max_retries=2)
        task = CheckTask(provider="test", service=Service(key="candidate"))

        # process_task must NOT raise: a check retry could double-write a
        # verdict (ResultBuffer.add does not dedupe) after a partial handler
        # failure, and the requeued live probe can even flip the verdict.
        self.assertIsNone(stage.process_task(task))
        self.assertEqual(1, provider.check_calls)

    def test_inspect_worker_swallows_retryable_error_no_requeue(self):
        provider = _ExplodingProvider()
        stage = InspectStage(_resources(providers={"test": provider}), handler=lambda _o: None, max_retries=2)
        task = InspectTask(provider="test", service=Service(key="candidate"))

        self.assertIsNone(stage.process_task(task))
        self.assertEqual(1, provider.inspect_calls)


# ---------------------------------------------------------------------------
# The retry budget's TERMINAL drop must be visible (2026-09-26 incident)
# ---------------------------------------------------------------------------


class TestTerminalDropIsLogged(unittest.TestCase):
    """A budget-exhausted task is dropped silently unless we say so.

    Measured 2026-09-26 08:00: the openrouter run had ONE condition; its single
    search task was denied by the process-wide github_api limiter on all three
    attempts, the retry policy refused the fourth, and *nothing* logged the
    drop — the pipeline finished in 54 s with links=0 and ``error_message``
    NULL. ``put_task``'s discard warning only covers the dedup path, never this
    one.
    """

    def _stage(self, handler) -> SearchStage:
        return SearchStage(
            _resources(),
            handler=handler,
            max_retries=1,
            thread_count=1,
            retry_policy=FixedRetry(max_retries=1, delay=0.0),
        )

    def test_drop_after_budget_logs_warning(self):
        stage = self._stage(lambda _o: None)
        task = _search_task()
        search_mock = mock.MagicMock(
            side_effect=ConnectionError("rate limiter denied for github_api")
        )

        with mock.patch.object(client, "search_with_count", search_mock), mock.patch.object(
            client, "get_link_index", return_value=None
        ):
            with self.assertLogs("stage", level="WARNING") as logs:
                stage.queue.put_nowait(task)
                stage.running = True
                worker = threading.Thread(
                    target=stage._worker_loop, name="terminal-drop", daemon=True
                )
                worker.start()
                settled = _wait_until(
                    lambda: task.attempts >= stage.max_retries and stage.queue.empty()
                )
                stage.running = False
                worker.join(timeout=5)

        self.assertTrue(settled, "worker never settled the task")
        self.assertEqual(stage.max_retries, task.attempts)
        self.assertEqual(search_mock.call_count, stage.max_retries + 1)
        self.assertTrue(
            any(
                "task dropped after" in r.getMessage()
                and "retry budget exhausted" in r.getMessage()
                for r in logs.records
            ),
            [r.getMessage() for r in logs.records],
        )

    def test_limiter_denial_logs_at_warning_not_error(self):
        stage = SearchStage(_resources(), handler=lambda _o: None)
        with mock.patch.object(
            client,
            "search_with_count",
            side_effect=ConnectionError("rate limiter denied for github_api"),
        ):
            with self.assertLogs("stage", level="WARNING") as logs:
                with self.assertRaises(ConnectionError):
                    stage.process_task(_search_task())

        levels = {r.levelname for r in logs.records}
        self.assertIn("WARNING", levels)
        self.assertNotIn("ERROR", levels)

    def test_transport_failure_still_logs_at_error(self):
        stage = SearchStage(_resources(), handler=lambda _o: None)
        with mock.patch.object(
            client,
            "search_with_count",
            side_effect=ConnectionError("HTTP 503 error: gateway"),
        ):
            with self.assertLogs("stage", level="ERROR") as logs:
                with self.assertRaises(ConnectionError):
                    stage.process_task(_search_task())

        self.assertTrue(any("HTTP 503" in r.getMessage() for r in logs.records))


if __name__ == "__main__":
    unittest.main()
