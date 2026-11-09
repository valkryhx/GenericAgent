"""The worker must not wait forever for a completion signal that already lost the race.

`run_task_worker_loop` treats the display queue's ``done`` item as "the turn finished".
The model's final answer is durable in the transcript *before* that item, so a lost or
delayed ``done`` leaves a finished teammate marked ``running`` until the 300s timeout --
and a parent close during that window writes ``interrupted``.

Real incident (2026-11-09, 15-task scale run): w1 produced its final assistant text at
15:55:25, never got ``done``, and was closed at 15:57:21 as ``interrupted`` while every
task it owned was ``completed``. Signature scan: 1 of 95 child task dirs.
"""

import queue
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.core.app import await_turn_done  # noqa: E402


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _FakeQueue:
    """Returns the given items in order, then raises Empty without blocking."""

    def __init__(self, items):
        self.items = list(items)

    def get_nowait(self):
        if self.items:
            return self.items.pop(0)
        raise queue.Empty


class AwaitTurnDoneTest(unittest.TestCase):
    def _run(self, items, *, assistant_text=None, running=False, timeout_s=300.0, grace_s=5.0):
        clock = _Clock()
        seen = []
        item, recovered = await_turn_done(
            _FakeQueue(items),
            on_item=seen.append,
            agent_is_running=lambda: running,
            assistant_text_getter=lambda: assistant_text,
            timeout_s=timeout_s,
            recovery_grace_s=grace_s,
            poll_s=1.0,
            monotonic_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )
        return item, recovered, seen, clock.now

    def test_a_done_item_is_returned_as_is(self):
        item, recovered, seen, _ = self._run([{'next': 'a'}, {'done': 'answer'}])
        self.assertEqual(item, {'done': 'answer'})
        self.assertFalse(recovered)
        self.assertEqual(seen, [{'next': 'a'}])

    def test_a_lost_done_is_recovered_from_the_durable_assistant_text(self):
        # The agent stopped; the round's final text exists; no `done` ever arrives.
        item, recovered, _, elapsed = self._run([], assistant_text='final answer', running=False, grace_s=5.0)
        self.assertTrue(recovered, 'a finished turn must not stall on a lost display item')
        self.assertEqual(item['done'], 'final answer')
        self.assertGreaterEqual(elapsed, 5.0)

    def test_recovery_waits_while_the_agent_still_runs(self):
        # Still generating: the assistant text from an earlier phase is not a verdict yet.
        with self.assertRaises(TimeoutError):
            self._run([], assistant_text='partial', running=True, timeout_s=20.0, grace_s=5.0)

    def test_each_queue_item_resets_the_recovery_window(self):
        clock = _Clock()

        def on_item(_item):
            clock.sleep(10.0)

        item, recovered = await_turn_done(
            _FakeQueue([{'next': 'a'}, {'next': 'b'}]),
            on_item=on_item,
            agent_is_running=lambda: False,
            assistant_text_getter=lambda: 'final',
            timeout_s=60.0,
            recovery_grace_s=5.0,
            poll_s=1.0,
            monotonic_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )
        self.assertTrue(recovered)
        self.assertEqual(item['done'], 'final')
        # Two items consumed 20s of fake time; recovery may only start 5s after the last.
        self.assertGreaterEqual(clock.now, 25.0)

    def test_a_long_active_turn_is_not_cut_off(self):
        # Streaming a >5-minute turn keeps producing items; the idle timer must reset on
        # each one (the original per-get timeout behaved that way).
        clock = _Clock()

        def on_item(_item):
            clock.sleep(100.0)

        with self.assertRaises(TimeoutError):
            await_turn_done(
                _FakeQueue([{'next': 'x'}] * 10),
                on_item=on_item,
                agent_is_running=lambda: True,
                assistant_text_getter=lambda: None,
                timeout_s=300.0,
                recovery_grace_s=5.0,
                poll_s=1.0,
                monotonic_fn=clock.monotonic,
                sleep_fn=clock.sleep,
            )
        self.assertGreaterEqual(clock.now, 1300.0, 'a long active turn was cut off early')

    def test_without_a_final_answer_the_wait_still_times_out(self):
        with self.assertRaises(TimeoutError):
            self._run([], assistant_text=None, running=False, timeout_s=20.0, grace_s=5.0)


if __name__ == '__main__':
    unittest.main()
