#!/usr/bin/env python3

"""CompletionEventManager: notify-once semantics under listener failures.

The old implementation held a non-reentrant lock while invoking listeners and
only set ``_completion_notified = True`` when ALL callbacks succeeded, so one
raising listener made every later ``is_finished()`` poll re-fire the whole
listener list (web push hooks are meant to run exactly once per run), and a
listener calling ``is_notified`` / ``add_listener`` would deadlock on the
still-held lock.
"""

from __future__ import annotations

import threading
import unittest

from manager.task import CompletionEventManager


class TestCompletionListenerNotifyOnce(unittest.TestCase):
    def setUp(self):
        self.fired: list[str] = []

    def _recorder(self, name: str):
        def _callback():
            self.fired.append(name)

        return _callback

    def test_raising_listener_does_not_cause_refire(self):
        # Given one healthy listener, one raising listener, one more healthy one
        manager = CompletionEventManager()

        def _boom():
            raise RuntimeError("listener failed")

        manager.add_listener(self._recorder("a"))
        manager.add_listener(_boom)
        manager.add_listener(self._recorder("b"))

        # When completion is notified repeatedly (is_finished() poll pattern)
        manager.notify_completion()
        manager.notify_completion()
        manager.notify_completion()

        # Then each listener ran exactly once and the flag is latched despite
        # the raising listener
        self.assertTrue(manager.is_notified)
        self.assertEqual(1, self.fired.count("a"))
        self.assertEqual(1, self.fired.count("b"))

    def test_flag_latches_when_every_listener_raises(self):
        manager = CompletionEventManager()

        def _boom():
            raise RuntimeError("all fail")

        manager.add_listener(_boom)

        manager.notify_completion()

        self.assertTrue(manager.is_notified)

    def test_notify_completion_does_not_propagate_listener_errors(self):
        manager = CompletionEventManager()

        def _boom():
            raise ValueError("nope")

        manager.add_listener(_boom)

        manager.notify_completion()  # must not raise

        self.assertTrue(manager.is_notified)


class TestCompletionListenerReentrancy(unittest.TestCase):
    def test_reentrant_listener_does_not_deadlock(self):
        # Given a listener that re-enters the manager (is_notified,
        # add_listener, notify_completion)
        manager = CompletionEventManager()
        observed: list[bool] = []
        nested_fired: list[int] = []

        def _reentrant():
            observed.append(manager.is_notified)
            manager.add_listener(lambda: nested_fired.append(1))
            manager.notify_completion()

        manager.add_listener(_reentrant)
        finished = threading.Event()

        # When notification runs (on a thread so a deadlock fails the test
        # instead of hanging the suite)
        def _run():
            manager.notify_completion()
            finished.set()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()

        # Then it completes, the flag was already latched inside the listener,
        # and the mid-notification addition was NOT fired by this pass
        self.assertTrue(finished.wait(timeout=5), "notify_completion deadlocked on a re-entrant listener")
        self.assertEqual([True], observed)
        self.assertEqual([], nested_fired)
        self.assertTrue(manager.is_notified)

        # and a later notify still does not fire the late-added listener
        manager.notify_completion()
        self.assertEqual([], nested_fired)


if __name__ == "__main__":
    unittest.main()
