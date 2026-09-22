"""Durability tests for the storage layer's failure paths.

Pinned 2026-09-22 while reviewing the write path every production scan uses
(4-core NAS, 6-8 concurrent scans, where silent data loss is the worst failure
class). None of these paths showed up in 24h of prod logs - they are latent
defects proved by reading the code, now covered:

* ``AtomicFileWriter.append_atomic`` was decorated with ``@handle_exceptions``
  and *no* ``reraise``, so a full disk / permission error / AV lock was
  swallowed inside the writer and reported to callers as success.
* ``ResultManager._flush_buffer`` cleared the buffer (``buffer.flush()``)
  *before* writing and then advanced ``stats.last_save`` unconditionally, so a
  failed write dropped the whole batch forever while metrics claimed a save.
* ``_periodic_flush`` slept uninterruptibly (``time.sleep(save_interval)``) and
  never re-checked ``running`` afterwards, so the thread could write *after*
  ``stop()`` had already flushed and cleaned up the strategy.
* ``SnapshotManager.stop()`` held ``_lock`` across ``join()`` while
  ``build_snapshot()`` needs the same ``_lock`` for its stats update, so stop
  always burned its 5s join timeout, and a still-running periodic builder could
  write the very same ``snapshot_path + ".tmp"`` as the stop-path builder.

Every test uses tmp dirs and in-process fakes; no network, no real providers.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from core.enums import ResultType
from core.models import ResultStorage
from state.models import PersistenceMetrics
from storage import snapshot as snapshot_module
from storage import strategies as strategies_module
from storage.atomic import AtomicFileWriter
from storage.persistence import ResultBuffer, ResultManager
from storage.strategies import ShardStrategy, SimpleFileStrategy, SnapshotManager

_PROVIDER_FILENAMES: Dict[str, str] = {
    "valid": "valid-keys.txt",
    "no_quota": "no-quota-keys.txt",
    "wait_check": "wait-check-keys.txt",
    "invalid": "invalid-keys.txt",
    "material": "material.txt",
    "summary": "summary.json",
    "links": "links.txt",
}

_DISK_FULL = OSError(28, "No space left on device")


class _StubProvider:
    """Minimal IProvider stand-in: only name + result layout are read."""

    def __init__(self, name: str = "durability") -> None:
        self.name = name
        self.result = ResultStorage(folder=name, filenames=dict(_PROVIDER_FILENAMES))


class _FlakyStrategy:
    """Persistence strategy double that fails on demand and records every batch."""

    def __init__(self) -> None:
        self.batches: List[List[Any]] = []
        self.error: Optional[BaseException] = None
        self.cleaned_up = False

    def write_data(self, result_type: str, items: List[Any], stats: PersistenceMetrics) -> None:
        if self.error is not None:
            raise self.error
        self.batches.append(list(items))

    def supports_snapshots(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.cleaned_up = True


def _thread_named(name: str) -> Optional[threading.Thread]:
    for thread in threading.enumerate():
        if thread.name == name:
            return thread
    return None


def _run_in_thread(target: Callable[[], Any], done: threading.Event) -> threading.Thread:
    """Run `target` in a daemon thread that sets `done` when it returns."""

    def runner() -> None:
        try:
            target()
        finally:
            done.set()

    thread = threading.Thread(target=runner, daemon=True, name="durability-test-helper")
    thread.start()
    return thread


def _gated_builder_factory(entered: threading.Event, release: threading.Event) -> Any:
    """Build a BaseSnapshotManager double that blocks mid-build on `release`."""

    class _GatedBuilder:
        def __init__(self, shard_root: str, snapshot_path: str) -> None:
            self.shard_root = shard_root
            self.snapshot_path = snapshot_path

        def build_snapshot(self) -> int:
            entered.set()
            release.wait(timeout=5.0)
            return 0

    return _GatedBuilder


class _BuildConcurrencyProbe:
    """Records the peak number of concurrent snapshot builds."""

    def __init__(self, work_seconds: float = 0.12) -> None:
        self.work_seconds = work_seconds
        self.peak_concurrent = 0
        self.first_build_entered = threading.Event()
        self._in_flight = 0
        self._guard = threading.Lock()

    def builder_class(self) -> Any:
        probe = self

        class _ProbeBuilder:
            def __init__(self, shard_root: str, snapshot_path: str) -> None:
                self.shard_root = shard_root
                self.snapshot_path = snapshot_path

            def build_snapshot(self) -> int:
                probe._build_started()
                time.sleep(probe.work_seconds)
                probe._build_finished()
                return 0

        return _ProbeBuilder

    def _build_started(self) -> None:
        with self._guard:
            self._in_flight += 1
            self.peak_concurrent = max(self.peak_concurrent, self._in_flight)
            if self._in_flight == 1:
                self.first_build_entered.set()

    def _build_finished(self) -> None:
        with self._guard:
            self._in_flight -= 1


class _RecordingOs:
    """Proxy over the real `os` that records and gates `os.replace` calls.

    Scoped to `storage.snapshot`'s module attribute so the rest of the process
    keeps the untouched stdlib module.
    """

    def __init__(self, real: Any, replaced: List[str], barrier: threading.Barrier) -> None:
        self._real = real
        self._replaced = replaced
        self._barrier = barrier

    def replace(self, src: str, dst: str) -> None:
        self._replaced.append(src)
        # Hold both builders at the rename point so their temp files coexist.
        self._barrier.wait(timeout=5.0)
        self._real.replace(src, dst)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _RacingBuildOutcome:
    """Runs one real snapshot build per thread and records what came back.

    Two independent builders publishing the same snapshot is the cross-manager /
    cross-process case the per-builder temp name has to protect: at most one may
    lose the race for the final rename (Windows denies it with a sharing
    violation), but the snapshot on disk must stay complete either way.
    """

    def __init__(self) -> None:
        self.published: List[int] = []
        self.rename_collisions: List[OSError] = []
        self._guard = threading.Lock()

    def run(self, shard_root: str, snapshot_path: str) -> None:
        builder = snapshot_module.SnapshotManager(shard_root, snapshot_path)
        try:
            count = builder.build_snapshot()
        except OSError as exc:
            with self._guard:
                self.rename_collisions.append(exc)
            return

        with self._guard:
            self.published.append(count)


def _write_shard(shard_dir: str, result_type: str, records: int) -> str:
    """Create one real NDJSON shard and return its path."""
    os.makedirs(shard_dir, exist_ok=True)
    shard_path = os.path.join(shard_dir, f"{result_type}_20260922_000000_000.ndjson")
    with open(shard_path, "w", encoding="utf-8") as handle:
        for index in range(records):
            handle.write(json.dumps({"value": f"http://example.com/{index}"}) + "\n")
    return shard_path


class _TmpDirCase(unittest.TestCase):
    """Base case owning one throwaway workspace."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.workspace = tmp.name
        self.addCleanup(tmp.cleanup)


class _TmpManagerCase(_TmpDirCase):
    """Base case that builds ResultManagers in a tmp dir and always stops them."""

    def setUp(self) -> None:
        super().setUp()
        self._managers: List[ResultManager] = []
        self.addCleanup(self._stop_managers)

    def _stop_managers(self) -> None:
        for manager in self._managers:
            try:
                manager.stop()
            except Exception:
                pass

    def _manager(
        self,
        *,
        name: str = "durability",
        batch_size: int = 50,
        save_interval: float = 3600.0,
        shutdown_timeout: float = 1.0,
        simple: bool = True,
    ) -> ResultManager:
        """Build a ResultManager whose periodic thread stays out of the way."""
        manager = ResultManager(
            _StubProvider(name),
            self.workspace,
            batch_size=batch_size,
            save_interval=save_interval,
            simple=simple,
            shutdown_timeout=shutdown_timeout,
        )
        self._managers.append(manager)
        return manager


class TestAppendAtomicPropagatesErrors(_TmpDirCase):
    """Defect 1: a failed append was swallowed inside the writer."""

    def _blocked_target(self) -> str:
        """A path that cannot be opened for append: POSIX raises
        IsADirectoryError, Windows raises PermissionError - both OSError."""
        blocked = os.path.join(self.workspace, "blocked")
        os.makedirs(blocked)
        return blocked

    def test_append_atomic_reraises_oserror(self) -> None:
        """Given a path that cannot be opened for append,
        When append_atomic writes to it,
        Then the OSError reaches the caller instead of returning None."""
        blocked = self._blocked_target()

        with self.assertRaises(OSError):
            AtomicFileWriter.append_atomic(blocked, ["line-1", "line-2"])

    def _unwritable_child(self) -> str:
        """A path whose *parent* is a regular file, so both writers fail inside
        ``os.makedirs`` with an OSError (FileExistsError / NotADirectoryError)."""
        blocker = os.path.join(self.workspace, "blocker.txt")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        return os.path.join(blocker, "target.txt")

    def test_append_atomic_matches_write_atomic_contract(self) -> None:
        """Both writers must propagate: one swallowing and one raising is how a
        failed simple-mode write used to look like a successful save."""
        target = self._unwritable_child()

        with self.assertRaises(OSError):
            AtomicFileWriter.write_atomic(target, "{}")
        with self.assertRaises(OSError):
            AtomicFileWriter.append_atomic(target, ["line-1"])

    def test_simple_file_strategy_reraises_write_failure(self) -> None:
        """Given the real SimpleFileStrategy pointing at an unwritable path,
        When write_data is called,
        Then it propagates so the manager can re-queue the batch."""
        strategy = SimpleFileStrategy(self.workspace, {ResultType.LINKS.value: self._blocked_target()})

        with self.assertRaises(OSError):
            strategy.write_data(ResultType.LINKS.value, ["http://example.com/1"], PersistenceMetrics())

    def test_shard_strategy_reraises_write_failure(self) -> None:
        """Given a real ShardStrategy whose fsync fails (the ENOSPC shape),
        When write_data is called,
        Then it propagates instead of reporting a saved batch."""
        strategy = ShardStrategy(self.workspace, {ResultType.LINKS.value: "links.txt"})

        with mock.patch.object(os, "fsync", side_effect=_DISK_FULL):
            with self.assertRaises(OSError):
                strategy.write_data(ResultType.LINKS.value, ["http://example.com/1"], PersistenceMetrics())

    def test_successful_append_is_unchanged(self) -> None:
        """Normal path guard: one item per line, trailing newline added."""
        filepath = os.path.join(self.workspace, "links.txt")

        AtomicFileWriter.append_atomic(filepath, ["line-1", "line-2\n"])

        with open(filepath, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "line-1\nline-2\n")


class TestResultBufferRequeue(unittest.TestCase):
    """The re-queue primitive that keeps a failed batch alive."""

    def test_requeue_restores_the_batch_in_order(self) -> None:
        buffer = ResultBuffer("links", batch_size=4, flush_interval=3600.0, max_pending=8)
        for url in ("a", "b", "c"):
            buffer.add(url)

        dropped = buffer.requeue(buffer.flush())

        self.assertEqual(dropped, 0)
        self.assertEqual(buffer.flush(), ["a", "b", "c"])

    def test_requeue_keeps_the_failed_batch_ahead_of_new_items(self) -> None:
        """Items added while the failed write was in flight are newer, so the
        re-queued batch must land in front of them."""
        buffer = ResultBuffer("links", batch_size=4, flush_interval=3600.0, max_pending=8)
        buffer.add("old-1")
        buffer.add("old-2")
        batch = buffer.flush()
        buffer.add("new")

        buffer.requeue(batch)

        self.assertEqual(buffer.flush(), ["old-1", "old-2", "new"])

    def test_requeue_is_bounded_and_reports_dropped_items(self) -> None:
        """The re-queue must not grow without limit while a disk stays broken."""
        buffer = ResultBuffer("links", batch_size=2, flush_interval=3600.0, max_pending=3)
        for url in ("a", "b", "c"):
            buffer.add(url)

        self.assertEqual(buffer.requeue(buffer.flush()), 0)
        self.assertEqual(buffer.size(), 3)

        buffer.add("d")  # arrived while the previous batch was unwritable

        dropped = buffer.requeue(buffer.flush())

        self.assertEqual(dropped, 1)
        self.assertEqual(buffer.flush(), ["a", "b", "c"])

    def test_requeue_dedupes_hashable_items(self) -> None:
        """Links re-queued while an identical link is already buffered are
        merged, not duplicated."""
        buffer = ResultBuffer("links", batch_size=4, flush_interval=3600.0, max_pending=16)
        buffer.add("shared")

        dropped = buffer.requeue(["shared", "fresh"])

        self.assertEqual(dropped, 0)
        self.assertEqual(buffer.size(), 2)

    def test_requeue_passes_unhashable_items_through(self) -> None:
        """Service-like records are unhashable; they must still be re-queued."""
        buffer = ResultBuffer("valid", batch_size=4, flush_interval=3600.0, max_pending=16)
        buffer.add(["unhashable-list"])

        dropped = buffer.requeue([{"key": "unhashable-dict"}])

        self.assertEqual(dropped, 0)
        self.assertEqual(buffer.size(), 2)


class TestFailedFlushKeepsTheBatch(_TmpManagerCase):
    """Defect 2: a failed write cleared the buffer and still claimed a save."""

    def test_failed_write_requeues_batch_and_does_not_advance_last_save(self) -> None:
        manager = self._manager()
        strategy = _FlakyStrategy()
        manager.strategy = strategy
        manager.stats.last_save = 0.0  # sentinel: never saved

        manager.add_result(ResultType.LINKS.value, ["http://example.com/a", "http://example.com/b"])
        strategy.error = _DISK_FULL

        manager._flush_buffer(ResultType.LINKS.value)  # When the write fails

        # Then the batch is back in the buffer, nothing was written, last_save
        # did not advance and the failure is counted.
        self.assertEqual(manager.buffers[ResultType.LINKS.value].size(), 2)
        self.assertEqual(strategy.batches, [])
        self.assertEqual(manager.stats.last_save, 0.0)
        self.assertEqual(manager.failed_flushes, 1)
        self.assertEqual(manager.dropped_items, 0)

        # And the very same batch survives to the next successful attempt.
        strategy.error = None
        manager._flush_buffer(ResultType.LINKS.value, force=True)

        self.assertEqual(strategy.batches, [["http://example.com/a", "http://example.com/b"]])
        self.assertEqual(manager.buffers[ResultType.LINKS.value].size(), 0)
        self.assertGreater(manager.stats.last_save, 0.0)
        self.assertEqual(manager.dropped_items, 0)

    def test_successful_write_still_advances_last_save(self) -> None:
        """Normal path guard: the happy flow is untouched."""
        manager = self._manager()
        strategy = _FlakyStrategy()
        manager.strategy = strategy
        manager.stats.last_save = 0.0

        manager.add_result(ResultType.LINKS.value, ["http://example.com/ok"])
        manager._flush_buffer(ResultType.LINKS.value, force=True)

        self.assertEqual(strategy.batches, [["http://example.com/ok"]])
        self.assertGreater(manager.stats.last_save, 0.0)
        self.assertEqual(manager.failed_flushes, 0)

    def test_retry_is_throttled_but_shutdown_forces_a_final_attempt(self) -> None:
        """A permanently broken disk must not turn into a retry/log storm on the
        hot add path, yet the shutdown flush must always try once more."""
        manager = self._manager()
        strategy = _FlakyStrategy()
        strategy.error = _DISK_FULL
        manager.strategy = strategy
        manager.stats.last_save = 0.0

        manager.add_result(ResultType.LINKS.value, ["http://example.com/only"])

        manager._flush_buffer(ResultType.LINKS.value)  # attempt 1 -> fails, arms cooldown
        manager._flush_buffer(ResultType.LINKS.value)  # attempt 2 -> throttled
        self.assertEqual(manager.failed_flushes, 1)

        manager.flush_all()  # shutdown path forces one more attempt

        self.assertEqual(manager.failed_flushes, 2)
        # Never lost and never claimed as saved:
        self.assertEqual(manager.buffers[ResultType.LINKS.value].size(), 1)
        self.assertEqual(manager.stats.last_save, 0.0)

    def test_manager_counts_items_dropped_by_the_requeue_bound(self) -> None:
        """Given a buffer whose re-queue bound is exhausted,
        When the write fails,
        Then the manager records how many items were truly lost."""
        manager = self._manager(batch_size=4)
        strategy = _FlakyStrategy()
        strategy.error = _DISK_FULL
        manager.strategy = strategy
        manager.buffers[ResultType.LINKS.value].max_pending = 2

        manager.add_result(ResultType.LINKS.value, ["http://1", "http://2", "http://3"])
        manager._flush_buffer(ResultType.LINKS.value, force=True)

        self.assertEqual(manager.buffers[ResultType.LINKS.value].size(), 2)
        self.assertEqual(manager.dropped_items, 1)
        self.assertEqual(manager.failed_flushes, 1)


class TestPeriodicFlushShutdown(_TmpManagerCase):
    """Defect 3: the periodic thread could outlive stop() and write afterwards."""

    def test_periodic_flush_writes_due_batches_while_running(self) -> None:
        """Guard against 'fixing' durability by never flushing at all."""
        manager = self._manager(save_interval=0.15)
        strategy = _FlakyStrategy()
        manager.strategy = strategy

        manager.add_result(ResultType.LINKS.value, ["http://example.com/due"])

        deadline = time.monotonic() + 3.0
        while not strategy.batches and time.monotonic() < deadline:
            time.sleep(0.02)

        self.assertEqual(strategy.batches, [["http://example.com/due"]])

    def test_stop_joins_the_periodic_thread_before_returning(self) -> None:
        """Given a periodic thread sleeping past the join budget,
        When stop() is called,
        Then stop() wakes it and returns with the thread finished."""
        manager = self._manager(save_interval=1.2, shutdown_timeout=1.0)
        strategy = _FlakyStrategy()
        manager.strategy = strategy
        manager.add_result(ResultType.LINKS.value, ["http://example.com/late"])

        manager.stop()

        self.assertFalse(manager.flush_thread.is_alive())
        self.assertFalse(manager.running)

    def test_periodic_flush_never_writes_after_stop(self) -> None:
        """The periodic thread must be gone by the time stop() returns.

        Old code slept uninterruptibly: stop() burned its join budget, ran
        flush_all() + strategy.cleanup(), and the still-sleeping thread woke up
        afterwards and flushed whatever a late worker had buffered - writing
        into an already cleaned-up strategy.
        """
        manager = self._manager(save_interval=1.2, shutdown_timeout=1.0)
        strategy = _FlakyStrategy()
        manager.strategy = strategy

        manager.stop()

        self.assertEqual(strategy.batches, [], "nothing was buffered, so nothing may be written")
        self.assertTrue(strategy.cleaned_up)

        # A producer that is still finishing after stop() must not be flushed by
        # the periodic thread past cleanup.
        manager.add_result(ResultType.LINKS.value, ["http://example.com/after-stop"])

        # Wait past the point where an uninterruptible sleep would have woken up.
        time.sleep(1.2 + 0.6)

        self.assertEqual(strategy.batches, [], "periodic flush wrote after stop() cleaned up the strategy")
        self.assertFalse(manager.flush_thread.is_alive())


class TestSnapshotManagerConcurrency(_TmpDirCase):
    """Defect 4: stop() deadlocked on its own lock and builds could interleave."""

    def setUp(self) -> None:
        super().setUp()
        self.result_type = ResultType.LINKS.value
        self.shard_root = os.path.join(self.workspace, "shards", self.result_type)
        os.makedirs(self.shard_root, exist_ok=True)

    def _manager(self, provider_name: str = "durability") -> SnapshotManager:
        return SnapshotManager(self.workspace, [self.result_type], provider_name)

    def test_stop_does_not_hold_the_manager_lock_across_join(self) -> None:
        """Given a periodic builder blocked mid-build,
        When stop() joins it,
        Then the manager lock stays free for other readers (get_stats)."""
        manager = self._manager("prov")
        entered = threading.Event()
        release = threading.Event()
        stop_done = threading.Event()
        stats_done = threading.Event()

        try:
            gated_builder = _gated_builder_factory(entered, release)
            with mock.patch.object(strategies_module, "BaseSnapshotManager", gated_builder):
                manager.start_periodic(interval_sec=0)
                self.assertTrue(entered.wait(timeout=3.0), "periodic builder never started a build")

                stopper = _run_in_thread(manager.stop, stop_done)
                reader = _run_in_thread(manager.get_stats, stats_done)

                self.assertTrue(
                    stats_done.wait(timeout=1.0),
                    "stop() held the manager lock across join(), blocking get_stats()",
                )

                release.set()
                self.assertTrue(stop_done.wait(timeout=3.0), "stop() did not finish")
                stopper.join(timeout=1.0)
                reader.join(timeout=1.0)

            self.assertGreaterEqual(manager.get_stats()["snapshot_count"], 1)
            self.assertIsNone(_thread_named("snapshot-prov"))
        finally:
            release.set()
            manager.stop()

    def test_stop_returns_promptly_when_the_thread_is_sleeping(self) -> None:
        """An interruptible sleep: a 60s interval must not delay shutdown."""
        manager = self._manager("idle")
        manager.start_periodic(interval_sec=60)
        self.assertIsNotNone(_thread_named("snapshot-idle"))

        started = time.monotonic()
        manager.stop()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, f"stop() took {elapsed:.2f}s for a sleeping snapshot thread")
        self.assertIsNone(_thread_named("snapshot-idle"))

    def test_periodic_manager_can_be_restarted_after_stop(self) -> None:
        """The stop event must be cleared again, else a restart exits at once."""
        manager = self._manager("restart")
        manager.start_periodic(interval_sec=0.05)
        manager.stop()

        manager.start_periodic(interval_sec=0.05)
        restarted = _thread_named("snapshot-restart")
        try:
            self.assertIsNotNone(restarted)
            deadline = time.monotonic() + 3.0
            while manager.get_stats()["snapshot_count"] == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(manager.get_stats()["snapshot_count"], 1)
            self.assertTrue(restarted.is_alive())
        finally:
            manager.stop()

    def test_stop_is_idempotent(self) -> None:
        manager = self._manager("twice")
        manager.start_periodic(interval_sec=0.05)

        manager.stop()
        manager.stop()  # must not raise, must not resurrect the thread

        self.assertIsNone(_thread_named("snapshot-twice"))

    def test_concurrent_builds_are_serialised(self) -> None:
        """Two builders of the same snapshot must never run at the same time."""
        manager = self._manager()
        probe = _BuildConcurrencyProbe(work_seconds=0.15)

        with mock.patch.object(strategies_module, "BaseSnapshotManager", probe.builder_class()):
            first = threading.Thread(target=manager.build_snapshot, args=(self.result_type,), daemon=True)
            first.start()
            self.assertTrue(probe.first_build_entered.wait(timeout=3.0))

            second = threading.Thread(target=manager.build_snapshot, args=(self.result_type,), daemon=True)
            second.start()
            first.join(timeout=5.0)
            second.join(timeout=5.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(probe.peak_concurrent, 1, "two snapshot builders ran concurrently")

    def test_concurrent_builds_of_one_snapshot_use_distinct_temp_files(self) -> None:
        """End to end with the real builder: two racing builds must not share one
        temp file, and the published snapshot must stay complete and valid."""
        records = 300
        _write_shard(self.shard_root, self.result_type, records)
        snapshots_dir = os.path.join(self.workspace, "snapshots")
        os.makedirs(snapshots_dir, exist_ok=True)
        snapshot_path = os.path.join(snapshots_dir, f"{self.result_type}.json")
        replaced: List[str] = []
        outcome = _RacingBuildOutcome()
        barrier = threading.Barrier(2, timeout=5.0)

        with mock.patch.object(snapshot_module, "os", _RecordingOs(os, replaced, barrier)):
            threads = [
                threading.Thread(target=outcome.run, args=(self.shard_root, snapshot_path), daemon=True)
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)

        # The defect itself: one shared "<snapshot>.tmp" written by both builders.
        self.assertEqual(len(replaced), 2, f"expected both builders to reach the rename, got {replaced}")
        self.assertEqual(len(set(replaced)), 2, f"concurrent builders shared a temp file: {replaced}")

        self.assertTrue(outcome.published, "no builder published a snapshot")
        self.assertEqual(set(outcome.published), {records})
        self.assertLessEqual(len(outcome.rename_collisions), 1, "both builders hit the rename collision")

        with open(snapshot_path, encoding="utf-8") as handle:
            published = json.load(handle)
        self.assertEqual(len(published), records)
        self.assertEqual(published[0], {"value": "http://example.com/0"})
        self.assertEqual([name for name in os.listdir(snapshots_dir) if name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
