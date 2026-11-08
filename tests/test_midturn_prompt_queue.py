"""A prompt typed while a turn is running must reach the model in that same turn.

The composer can already accept the text (the UI half is
``frontends/ink/ui/src/composerDuringRun.test.ts``), but accepting it is worthless if
the model never sees it. Claude Code folds a mid-turn message into the running turn at
the next tool boundary -- "The user sent a new message while you were working:" -- and
these tests pin the GA equivalent:

  1. the queue is a plain, ordered, drainable store (no turn coupling)
  2. the bridge queues while busy instead of rejecting with ``busy``
  3. the fold point emits the queued text as part of the next prompt
  4. drained messages cannot be delivered twice, and a message that arrived after the
     fold point waits for the next one
  5. the text is wrapped so the model can tell it apart from the original request

The fold point is the same one subagent notifications already use
(``core/loop.py``, after the tool batch, before the next ``chat()``), so a turn that is
stuck in a long synchronous tool call still delivers at the first boundary it reaches.
"""

import sys
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.core.pending_input import (  # noqa: E402
    QueuedPrompt,
    PendingPromptQueue,
    WRAP_PREFIX,
    WRAP_SUFFIX,
    render_queued_prompts,
)


class PendingPromptQueueTest(unittest.TestCase):
    def test_preserves_order_and_drains(self):
        queue = PendingPromptQueue()
        queue.push("first")
        queue.push("second")

        drained = queue.drain()

        self.assertEqual(["first", "second"], [item.text for item in drained])
        self.assertEqual([], queue.drain(), "a drained message came back")

    def test_drain_is_atomic_against_a_concurrent_push(self):
        """A push racing with a drain must land in the next drain, not be lost."""
        queue = PendingPromptQueue()
        for index in range(50):
            queue.push(f"seed-{index}")
        seen = []
        errors = []

        def drainer():
            try:
                for _ in range(50):
                    seen.extend(item.text for item in queue.drain())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def pusher():
            try:
                for index in range(50):
                    queue.push(f"late-{index}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=drainer), threading.Thread(target=pusher)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([], errors)
        remaining = [item.text for item in queue.drain()]
        everything = set(seen) | set(remaining)
        expected = {f"seed-{i}" for i in range(50)} | {f"late-{i}" for i in range(50)}
        self.assertEqual(expected, everything, "a push was lost while a drain was running")

    def test_len_and_clear(self):
        queue = PendingPromptQueue()
        queue.push("one")
        queue.push("two")
        self.assertEqual(2, len(queue))
        queue.clear()
        self.assertEqual(0, len(queue))
        self.assertEqual([], queue.drain())

    def test_blank_text_is_rejected(self):
        queue = PendingPromptQueue()
        self.assertFalse(queue.push("   "))
        self.assertEqual(0, len(queue))


class RenderQueuedPromptsTest(unittest.TestCase):
    def test_the_model_is_told_this_arrived_mid_turn(self):
        text = render_queued_prompts([QueuedPrompt(text="do the other thing")])

        self.assertIn(WRAP_PREFIX, text)
        self.assertIn(WRAP_SUFFIX, text)
        self.assertIn("do the other thing", text)
        # The wrapper has to say *when* it arrived, or the model cannot tell whether it
        # supersedes the request it is already working on.
        self.assertIn("while you were working", text)

    def test_several_messages_keep_their_order(self):
        text = render_queued_prompts(
            [QueuedPrompt(text="alpha"), QueuedPrompt(text="bravo")]
        )
        self.assertLess(text.index("alpha"), text.index("bravo"))

    def test_nothing_queued_renders_nothing(self):
        self.assertEqual("", render_queued_prompts([]))


class FoldPointInjectsQueuedTextTest(unittest.TestCase):
    """The loop must fold queue text in, the same way it folds notifications in."""

    def _handler(self, queue):
        class _Parent:
            task_dir = None
            is_subagent = False
            project_root = None

        class _Handler:
            parent = _Parent()

            def pending_prompts(self):
                return queue

        return _Handler()

    def test_the_fold_point_returns_the_queued_text_once(self):
        from genericagent.core.loop import _consume_queued_prompts

        queue = PendingPromptQueue()
        queue.push("steer me")
        handler = self._handler(queue)

        first = _consume_queued_prompts(handler)
        second = _consume_queued_prompts(handler)

        self.assertIn("steer me", first)
        self.assertEqual("", second, "the same queued prompt was delivered twice")

    def test_a_handler_without_a_queue_is_not_an_error(self):
        """Every existing caller and test stub lacks this; they must keep working."""
        from genericagent.core.loop import _consume_queued_prompts

        class _Parent:
            task_dir = None
            is_subagent = False
            project_root = None

        class _Handler:
            parent = _Parent()

        self.assertEqual("", _consume_queued_prompts(_Handler()))


if __name__ == "__main__":
    unittest.main()
