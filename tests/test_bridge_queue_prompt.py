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


if __name__ == "__main__":
    unittest.main()
