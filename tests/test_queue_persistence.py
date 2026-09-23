#!/usr/bin/env python3

"""Queue persistence must never lose or strand tasks.

Three code-verified defects (not yet observed in 24h of production logs):

1. ``BasePipelineStage.get_pending_tasks`` drained the live queue with
   ``get_nowait()`` and re-put with ``put_nowait()``. A concurrent producer
   could fill the transient free slot, the re-put then hit ``queue.Full`` and
   the task was DROPPED with only a warning ("lost task during persistence").
2. During that drain window the queue looked empty; with ``active_workers``
   momentarily 0, ``Pipeline.is_finished()`` could permanently latch
   ``stop_accepting()`` on a stage that upstream work was about to feed
   again - every later ``put_task`` was then discarded.
3. A discarded ``put_task`` was a warning whose return value the pipeline
   ignored; it is now an ERROR plus a total_errors increment — and every
   task-identity log site (discard / queue-full / requeue) prints the safe
   identity ``provider:TaskClass:sha256(dedup-id)[:12]``, never the raw id or
   the dataclass repr: CHECK/INSPECT dedup ids embed the raw candidate key
   (``check:<provider>:<key>:<address>:<endpoint>``), reprs embed
   ``Service(key='<raw>')``, and the global RedactionFilter provably misses
   prefix-less key formats (SerpApi 64-hex has no pattern on purpose).
"""

from __future__ import annotations

import hashlib
import queue as queue_mod
import tempfile
import threading
import time
import unittest
from typing import List, Optional
from unittest import mock

from core.models import CheckTask, ProviderTask, SearchTask, Service
from manager.pipeline import Pipeline
from manager.queue import QueueManager
from stage import base as stage_base
from stage.base import BasePipelineStage, StageOutput, StageResources
from tools.retry import RetryPolicy

# Key-looking literal built by concatenation (repo convention) so secret
# scanners never see a complete fake credential in one piece.
_LIVE_LOOKING_KEY = "sk-live" + "key0123456789abcdef"


def _identity(task: ProviderTask) -> str:
    """Expected safe identity for _StubStage (its dedup id IS task_id)."""
    digest = hashlib.sha256(task.task_id.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{task.provider}:{type(task).__name__}:{digest}"


class _StubStage(BasePipelineStage):
    """Minimal concrete stage (no workers started; queue driven by tests)."""

    def _validate_task_type(self, task: ProviderTask) -> bool:
        return isinstance(task, ProviderTask)

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        return None

    def _generate_id(self, task: ProviderTask) -> str:
        return task.task_id


def _make_stage(
    name: str = "search",
    queue_size: int = 1000,
    retry_policy: Optional[RetryPolicy] = None,
    stage_cls: type[_StubStage] = _StubStage,
) -> _StubStage:
    resources = StageResources(
        limiter=mock.MagicMock(),
        providers={},
        config=mock.MagicMock(),
        task_configs={},
        auth=mock.MagicMock(),
    )
    return stage_cls(
        name=name,
        resources=resources,
        handler=lambda _output: None,
        queue_size=queue_size,
        retry_policy=retry_policy,
    )


def _task(query: str) -> SearchTask:
    return SearchTask(provider="openai", query=query, regex="x", page=1)


def _check_task(key: str) -> CheckTask:
    """A CheckTask whose dataclass repr embeds Service(key='<raw>')."""
    return CheckTask(provider="openai", service=Service(key=key))


class TestPendingTaskSnapshot(unittest.TestCase):
    def test_snapshot_is_non_destructive_and_order_preserving(self):
        # Given a stage with pending tasks
        stage = _make_stage()
        tasks = [_task(f"q{i}") for i in range(5)]
        for task in tasks:
            stage.queue.put_nowait(task)

        # When the persistence snapshot is taken (twice)
        first = stage.get_pending_tasks()
        second = stage.get_pending_tasks()

        # Then the queue is untouched and the snapshot is faithful
        self.assertEqual(5, stage.queue.qsize())
        self.assertEqual([t.query for t in tasks], [t.query for t in first])
        self.assertIs(tasks[0], first[0])
        self.assertEqual([t.query for t in first], [t.query for t in second])

    def test_racing_producer_loses_no_task_during_snapshot(self):
        # Given a small bounded queue that is nearly full, and a producer
        # hammering put() while snapshots run. Under the old drain-and-reput
        # implementation the producer slipped into the transient free slot and
        # the re-put then overflowed with queue.Full, dropping tasks.
        stage = _make_stage(queue_size=8)
        initial = [_task(f"init{i}") for i in range(6)]
        for task in initial:
            stage.queue.put_nowait(task)

        produced: list[ProviderTask] = []
        stop = threading.Event()

        def _producer():
            i = 0
            while not stop.is_set():
                task = _task(f"new{i}")
                try:
                    stage.queue.put(task, timeout=0.01)
                except queue_mod.Full:
                    continue
                produced.append(task)
                i += 1

        producer = threading.Thread(target=_producer, daemon=True)
        producer.start()
        snapshots = 0
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                stage.get_pending_tasks()
                snapshots += 1
        finally:
            stop.set()
            producer.join(timeout=5)

        # Then nothing was lost: no consumer exists, so the queue must hold
        # exactly initial + successfully produced tasks (conservation), and
        # no "lost task during persistence" may have been logged.
        self.assertGreater(snapshots, 0)
        self.assertFalse(producer.is_alive())
        self.assertEqual(len(initial) + len(produced), stage.queue.qsize())

        live_ids = {id(t) for t in stage.get_pending_tasks()}
        self.assertTrue({id(t) for t in initial} <= live_ids)
        self.assertTrue({id(t) for t in produced} <= live_ids)

    def test_save_all_queues_round_trips_without_consuming(self):
        # Given a live stage with pending tasks and a QueueManager
        with tempfile.TemporaryDirectory() as workspace:
            manager = QueueManager(workspace=workspace)
            stage = _make_stage()
            for i in range(3):
                self.assertTrue(stage.put_task(_task(f"q{i}")))

            # When the periodic/shutdown persistence runs
            manager.save_all_queues({"search": stage})

            # Then the snapshot was written AND the live queue still has them
            loaded = manager.load_queue_state("search")
            self.assertEqual(3, stage.queue.qsize())
            self.assertEqual(sorted(f"q{i}" for i in range(3)), sorted(t.query for t in loaded))


class TestPutTaskDiscardIsSurfaced(unittest.TestCase):
    def test_discard_after_stop_accepting_is_error_and_counted(self):
        # Given a stage that stopped accepting, and a task whose repr embeds a
        # raw Service key (which the global RedactionFilter misses for
        # prefix-less formats).
        stage = _make_stage()
        stage.stop_accepting()
        task = _check_task(_LIVE_LOOKING_KEY)

        # When the task is discarded
        with mock.patch.object(stage_base.logger, "error") as error_mock:
            accepted = stage.put_task(task)

        # Then it is surfaced at ERROR, counted, and logged with the digest
        # identity only — never the raw task id and never the repr, so no key
        # material can reach the log.
        self.assertFalse(accepted)
        self.assertEqual(1, stage.total_errors)
        error_mock.assert_called_once()
        message = error_mock.call_args[0][0]
        self.assertIn("not accepting tasks, discard", message)
        self.assertIn(_identity(task), message)
        self.assertNotIn(task.task_id, message)
        self.assertNotIn(_LIVE_LOOKING_KEY, message)

    def test_queue_full_discard_is_error_and_counted_without_raw_key(self):
        # Given a full queue (no consumer) and a key-bearing CheckTask
        stage = _make_stage(queue_size=1)
        self.assertTrue(stage.put_task(_task("fills")))
        doomed = _check_task(_LIVE_LOOKING_KEY)

        # When the second task overflows the queue (queue.Full path)
        with mock.patch.object(stage_base.logger, "error") as error_mock:
            accepted = stage.put_task(doomed)

        # Then ERROR + counted + digest identity only: neither raw key nor raw
        # task id
        self.assertFalse(accepted)
        self.assertEqual(1, stage.total_errors)
        error_mock.assert_called_once()
        message = error_mock.call_args[0][0]
        self.assertIn("queue is full, task discarded", message)
        self.assertIn(_identity(doomed), message)
        self.assertNotIn(doomed.task_id, message)
        self.assertNotIn(_LIVE_LOOKING_KEY, message)


class _FailingStage(_StubStage):
    """Stage whose execution always fails and propagates to the worker loop."""

    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        raise RuntimeError("probe exploded")

    def _handle_processing_error(self, task: ProviderTask, error: Exception) -> Optional[StageOutput]:
        # Re-raise so the failure reaches _worker_loop's requeue path (the
        # default handler would swallow it inside process_task).
        raise error


class _OneShotRetryPolicy(RetryPolicy):
    """Permits exactly one requeue; deterministic, no sleeping."""

    def __init__(self) -> None:
        self.calls = 0

    def should_retry(self, attempt: int, error: Exception) -> bool:
        self.calls += 1
        return self.calls == 1

    def get_delay(self, attempt: int) -> float:
        return 0.0


class TestRequeueWarningIdentity(unittest.TestCase):
    def test_requeue_warning_prints_digest_identity_never_raw_task(self):
        # Given a failing check-stage task and a one-shot retry policy
        stage = _make_stage(name="check", stage_cls=_FailingStage, retry_policy=_OneShotRetryPolicy())
        task = _check_task(_LIVE_LOOKING_KEY)
        stage.queue.put_nowait(task)
        stage.running = True

        seen = threading.Event()
        warnings: List[str] = []

        def _capture(message, *args) -> None:
            warnings.append(message)
            seen.set()

        # When the real worker loop hits its requeue path
        with mock.patch.object(stage_base.logger, "warning", side_effect=_capture), mock.patch.object(
            stage_base.logger, "error"
        ):
            worker = threading.Thread(target=stage._worker_loop, name="requeue-test", daemon=True)
            worker.start()
            try:
                self.assertTrue(seen.wait(timeout=5))
            finally:
                stage.running = False
                worker.join(timeout=5)

        # Then the requeue line carries the digest identity — never the repr
        # (which embeds Service(key='<raw>')) and never the raw task id.
        self.assertFalse(worker.is_alive())
        message = warnings[0]
        self.assertIn("requeued successfully", message)
        self.assertIn(_identity(task), message)
        self.assertNotIn(task.task_id, message)
        self.assertNotIn(_LIVE_LOOKING_KEY, message)
        self.assertNotIn("service=", message)


class _FakeStageDefinition:
    def __init__(self, produces_for: list[str]):
        self.produces_for = produces_for


def _make_pipeline(stages: dict[str, _StubStage], order: list[str], defs: dict[str, _FakeStageDefinition]) -> Pipeline:
    """Pipeline shell for is_finished tests (skips the heavyweight __init__)."""
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.stages = stages
    pipeline._order_cache = order
    pipeline.get_stage_def = lambda name: defs.get(name)  # type: ignore[method-assign]
    return pipeline


class TestStopAcceptingNeedsCoherentDrain(unittest.TestCase):
    def _search_to_gather(self, search: _StubStage, gather: _StubStage) -> Pipeline:
        return _make_pipeline(
            stages={"search": search, "gather": gather},
            order=["search", "gather"],
            defs={"search": _FakeStageDefinition(["gather"]), "gather": _FakeStageDefinition([])},
        )

    def test_transient_emptiness_does_not_permanently_stop_a_stage(self):
        # Given search momentarily empty/idle (the old persistence-drain
        # window) while its downstream gather still has a backlog
        search = _make_stage("search")
        gather = _make_stage("gather")
        gather.queue.put_nowait(_task("g1"))
        gather.queue.put_nowait(_task("g2"))
        pipeline = self._search_to_gather(search, gather)

        # When the completion poll runs
        finished = pipeline.is_finished()

        # Then no stage latched stop_accepting and the pipeline is not done.
        # (Old code permanently stopped search here; every later put_task -
        # e.g. search pagination - was then discarded.)
        self.assertFalse(finished)
        self.assertTrue(search.accepting)
        self.assertTrue(gather.accepting)
        self.assertTrue(search.put_task(_task("late-page")))

    def test_active_upstream_worker_blocks_the_whole_dag_latch(self):
        # Given an in-flight search worker (task dequeued, output pending)
        search = _make_stage("search")
        gather = _make_stage("gather")
        with search.work_lock:
            search.active_workers += 1
        pipeline = self._search_to_gather(search, gather)

        finished = pipeline.is_finished()

        self.assertFalse(finished)
        self.assertTrue(search.accepting)
        self.assertTrue(gather.accepting)

    def test_fully_quiescent_dag_finishes_and_closes_every_stage(self):
        # Given the whole DAG drained (normal completion path)
        search = _make_stage("search")
        gather = _make_stage("gather")
        pipeline = self._search_to_gather(search, gather)

        # When completion is polled
        finished = pipeline.is_finished()

        # Then a single pass latches stop_accepting in dependency order and
        # reports the pipeline finished
        self.assertTrue(finished)
        self.assertFalse(search.accepting)
        self.assertFalse(gather.accepting)


if __name__ == "__main__":
    unittest.main()
