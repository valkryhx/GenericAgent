"""The bridge must accept a mid-turn prompt instead of answering ``busy``.

``submit()`` rejects while the agent runs, which is correct for "start a new turn" and
wrong for "the user typed something". This covers the bridge half of the mid-turn
queue; the fold-point half lives in ``test_midturn_prompt_queue.py``.

The behaviour that matters to the user: typing during a run is accepted, reported as
queued, and never silently dropped. The behaviour that matters to the system: it does
not become a second turn, and it does not disappear when the first turn ends.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.core.pending_input import PendingPromptQueue  # noqa: E402


def _bridge_with_fake_agent(busy=True):
    """A bridge whose agent looks busy, without starting a process or the real runtime."""
    from frontends.ink.bridge import GenericAgentBridge

    agent = mock.MagicMock()
    agent.is_running = busy
    agent.pending_prompts = PendingPromptQueue()

    bridge = GenericAgentBridge.__new__(GenericAgentBridge)
    bridge.agent = agent
    bridge._consume_thread = None
    bridge.emit = mock.MagicMock()
    bridge.submit = mock.MagicMock(return_value=7)
    return bridge, agent


class QueuePromptWhileBusyTest(unittest.TestCase):
    def test_a_prompt_typed_during_a_run_is_queued(self):
        bridge, agent = _bridge_with_fake_agent(busy=True)

        accepted = bridge.queue_prompt("change direction")

        self.assertTrue(accepted)
        self.assertEqual(1, len(agent.pending_prompts))
        drained = agent.pending_prompts.drain()
        self.assertEqual("change direction", drained[0].text)

    def test_the_ui_is_told_it_was_queued(self):
        bridge, _agent = _bridge_with_fake_agent(busy=True)

        bridge.queue_prompt("change direction")

        events = [call.args[0] for call in bridge.emit.call_args_list]
        self.assertTrue(
            any(event.get("type") == "prompt_queued" for event in events),
            "the user got no acknowledgement that the message was accepted",
        )

    def test_it_does_not_start_a_second_turn(self):
        bridge, _agent = _bridge_with_fake_agent(busy=True)

        bridge.queue_prompt("change direction")

        bridge.submit.assert_not_called()

    def test_blank_input_is_rejected_without_queueing(self):
        bridge, agent = _bridge_with_fake_agent(busy=True)

        accepted = bridge.queue_prompt("   ")

        self.assertFalse(accepted)
        self.assertEqual(0, len(agent.pending_prompts))


class QueuePromptWhileIdleTest(unittest.TestCase):
    def test_an_idle_agent_promotes_the_prompt_to_a_real_turn(self):
        """The turn can end between the UI deciding to queue and the call arriving."""
        bridge, agent = _bridge_with_fake_agent(busy=False)

        accepted = bridge.queue_prompt("do it now")

        self.assertTrue(accepted)
        bridge.submit.assert_called_once_with("do it now")
        self.assertEqual(0, len(agent.pending_prompts), "an idle prompt must not also queue")


class EscapeKeepsAndSendsTheQueuedPromptTest(unittest.TestCase):
    """Esc is "stop talking and answer this", not "throw everything away".

    Claude Code's interrupt ends the current generation and immediately re-asks with the
    text the user queued. GA's ``abort_current_turn`` used to only set ``stop_sig``, so a
    prompt the user typed during the run was dropped together with the turn -- the user
    got neither the old answer nor their new question.
    """

    def _bridge(self, busy=True, queued=""):
        from frontends.ink.bridge import GenericAgentBridge

        agent = mock.MagicMock()
        agent.is_running = busy
        agent.pending_prompts = PendingPromptQueue()
        if queued:
            agent.pending_prompts.push(queued)
        bridge = GenericAgentBridge.__new__(GenericAgentBridge)
        bridge.agent = agent
        bridge._consume_thread = None
        bridge._shutdown_requested = False
        bridge._task_seq = 0
        bridge.emit = mock.MagicMock()
        bridge.submit = mock.MagicMock(return_value=1)
        bridge.stop = mock.MagicMock()
        return bridge, agent

    def test_the_queued_text_is_taken_before_the_abort(self):
        bridge, agent = self._bridge(busy=True, queued="answer this instead")

        text = bridge._take_queued_prompt_text()

        self.assertIn("answer this instead", text)
        self.assertEqual(0, len(agent.pending_prompts), "the text must not also stay queued")

    def test_several_queued_messages_are_kept_in_order(self):
        bridge, agent = self._bridge(busy=True)
        agent.pending_prompts.push("first")
        agent.pending_prompts.push("second")

        text = bridge._take_queued_prompt_text()

        self.assertLess(text.index("first"), text.index("second"))

    def test_an_empty_queue_takes_nothing(self):
        bridge, _agent = self._bridge(busy=True)

        self.assertEqual("", bridge._take_queued_prompt_text())

    def test_esc_with_a_queued_prompt_aborts_and_resubmits_it(self):
        bridge, _agent = self._bridge(busy=True, queued="answer this instead")

        with mock.patch("frontends.ink.bridge.threading.Thread") as thread_cls:
            bridge.abort_current_turn()

        bridge.agent.abort.assert_called_once()
        thread_cls.assert_called_once()
        self.assertEqual(
            "ga-ink-queued-after-stop",
            thread_cls.call_args.kwargs.get("name"),
            "no watcher was started to submit the message once the turn unwinds",
        )
        # The submit itself runs inside the watcher thread, which is replaced here.
        bridge.submit.assert_not_called()

    def test_esc_without_a_queued_prompt_is_still_a_plain_abort(self):
        bridge, _agent = self._bridge(busy=True)

        with mock.patch("frontends.ink.bridge.threading.Thread") as thread_cls:
            bridge.abort_current_turn()

        bridge.agent.abort.assert_called_once()
        thread_cls.assert_not_called()
        bridge.submit.assert_not_called()

    def test_esc_while_idle_with_a_queued_prompt_submits_it(self):
        """Nothing was running, so there is nothing to abort -- but the text still runs."""
        bridge, _agent = self._bridge(busy=False, queued="do it now")

        bridge.abort_current_turn()

        bridge.submit.assert_called_once_with("do it now")
        bridge.agent.abort.assert_not_called()
        bridge.stop.assert_not_called()

    def test_esc_while_idle_without_a_queued_prompt_closes_the_session(self):
        bridge, _agent = self._bridge(busy=False)

        bridge.abort_current_turn()

        bridge.stop.assert_called_once()
        bridge.submit.assert_not_called()

    def test_the_watcher_submits_once_the_turn_goes_idle(self):
        """The submit must wait for ``is_running`` to clear, or it answers ``busy``."""
        bridge, agent = self._bridge(busy=True, queued="wait for idle")
        submitted = []

        def record(text):
            submitted.append(text)
            return 1

        bridge.submit = record
        captured = {}

        class _DeferredThread:
            """Records the target instead of running it, so the test drives the wake-up."""

            def __init__(self, target=None, daemon=None, name=None):
                captured["target"] = target
                captured["name"] = name

            def start(self):
                return None

        with mock.patch("frontends.ink.bridge.threading.Thread", _DeferredThread):
            bridge.abort_current_turn()

        self.assertEqual("ga-ink-queued-after-stop", captured["name"])
        self.assertEqual([], submitted, "the submit ran before the turn unwound")

        # The aborted turn finishes its finally block and clears is_running; only then is
        # a submit going to be accepted instead of answered with "busy".
        agent.is_running = False
        captured["target"]()

        self.assertEqual(["wait for idle"], submitted)

    def test_a_watcher_that_never_sees_idle_reports_the_drop(self):
        from unittest import mock as _mock

        bridge, _agent = self._bridge(busy=True, queued="never sent")
        emitted = []
        bridge.emit = emitted.append

        class _ImmediateThread:
            def __init__(self, target=None, daemon=None, name=None):
                self._target = target

            def start(self):
                self._target()

        clock = iter([0.0, 200.0])
        with mock.patch("frontends.ink.bridge.threading.Thread", _ImmediateThread), \
                mock.patch("frontends.ink.bridge.time.monotonic", lambda: next(clock)), \
                mock.patch("frontends.ink.bridge.time.sleep", lambda _s: None):
            bridge.abort_current_turn()

        self.assertTrue(
            any(event.get("code") == "queued_prompt_dropped" for event in emitted),
            "a message that could not be delivered vanished without telling the user",
        )
        bridge.submit.assert_not_called()


class QueuedPromptRunsWhenTheTurnEndsTest(unittest.TestCase):
    """CC runs a queued message as the next turn; it must not wait for the user to speak.

    The old behaviour left the text in the queue until the user's *next* prompt, which
    the turn-start drain then delivered as a prefix to that unrelated request. From the
    user's side their message was silently delayed -- often by minutes -- and then
    attached to a different question.
    """

    def _bridge(self, queued=""):
        from frontends.ink.bridge import GenericAgentBridge

        agent = mock.MagicMock()
        agent.is_running = False
        agent.pending_prompts = PendingPromptQueue()
        if queued:
            agent.pending_prompts.push(queued)
        bridge = GenericAgentBridge.__new__(GenericAgentBridge)
        bridge.agent = agent
        bridge._consume_thread = None
        bridge._shutdown_requested = False
        bridge._task_seq = 0
        bridge.emit = mock.MagicMock()
        bridge.submit = mock.MagicMock(return_value=9)
        return bridge, agent

    def test_a_queued_prompt_starts_a_turn_as_soon_as_the_run_ends(self):
        bridge, agent = self._bridge(queued="now that you are done, do X")

        with mock.patch("frontends.ink.bridge.threading.Thread") as thread_cls:
            bridge._start_queued_turn_after_end()

        thread_cls.assert_called_once()
        self.assertEqual("ga-ink-queued-after-stop", thread_cls.call_args.kwargs.get("name"))
        self.assertEqual(
            0, len(agent.pending_prompts),
            "the text stayed queued, so a later user prompt would carry it again",
        )

    def test_nothing_queued_starts_nothing(self):
        bridge, _agent = self._bridge()

        with mock.patch("frontends.ink.bridge.threading.Thread") as thread_cls:
            bridge._start_queued_turn_after_end()

        thread_cls.assert_not_called()

    def test_the_new_turn_carries_the_queued_text(self):
        bridge, agent = self._bridge(queued="answer this now")
        submitted = []
        bridge.submit = lambda text: (submitted.append(text), 9)[1]
        captured = {}

        class _DeferredThread:
            def __init__(self, target=None, daemon=None, name=None):
                captured["target"] = target

            def start(self):
                return None

        with mock.patch("frontends.ink.bridge.threading.Thread", _DeferredThread):
            bridge._start_queued_turn_after_end()
        agent.is_running = False
        captured["target"]()

        self.assertEqual(["answer this now"], submitted)


if __name__ == "__main__":
    unittest.main()
