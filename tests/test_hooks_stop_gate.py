"""Stage A: Stop hook is a turn-end gate (CC blockable_turn_end semantics).

The gate fires when the model wants to stop its turn (agent_runner_loop about
to break). Emitting Stop at that point is exactly what a Stop beep wants: beep
when the agent itself stops. A blocking Stop hook keeps the turn alive (its
reason injected into the next model input); ``preventContinuation`` ends the
turn with a message. When blocking causes another stop, Stop fires again with
``stop_hook_active=True`` (CC semantics), so a beep still fires per stop.

These tests drive the gate through the shared ``run_hook_event`` entry point
(patching it to a scripted stub) so tool hooks stay benign and only Stop is
exercised.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from genericagent.core.handler import GenericAgentHandler
from genericagent.core.loop import agent_runner_loop
from genericagent.hooks.models import HookAggregate


def _agg(**kw):
    a = HookAggregate(event="Stop")
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class StopOnlyRuntime:
    """Returns scripted aggregates for Stop; benign for every other event."""
    def __init__(self, aggregates):
        self._aggs = list(aggregates)
        self.stop_calls = []
        self.other_events = []

    def run_event(self, agent, event, data=None, *, emit=None):
        if event == "Stop":
            self.stop_calls.append(dict(data or {}))
            return self._aggs.pop(0) if self._aggs else HookAggregate(event="Stop")
        self.other_events.append(event)
        return HookAggregate(event=event)


class BoomRuntime:
    def run_event(self, agent, event, data=None, *, emit=None):
        if event == "Stop":
            raise RuntimeError("hook blew up")
        return HookAggregate(event=event)


class FakeResponse:
    def __init__(self, content="", tool_calls=None, thinking=""):
        self.content = content
        self.thinking = thinking
        self.tool_calls = tool_calls or []


class FakeClient:
    """Generator-style chat() (like the real client); records inputs."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.messages = []
        self.last_tools = ""

    def chat(self, messages, tools=None):
        self.calls += 1
        self.messages.append(messages)
        resp = self._responses.pop(0) if self._responses else FakeResponse("done")
        if False:
            yield None
        return resp


def make_handler(runtime):
    parent = SimpleNamespace(task_dir=None, verbose=False, hook_runtime=runtime, session_id="s", cwd=".")
    return GenericAgentHandler(parent, cwd=".", workspace_root=None)


def with_runtime(runtime, fn):
    """Patch the shared entry point so Stop is served by the scripted stub."""
    def fake(agent, event, data=None, *, emit=None):
        return runtime.run_event(agent, event, data, emit=emit)
    with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=fake):
        return fn()


def drain(gen):
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        return "".join(str(c) for c in chunks), stop.value


class StopGateUnitTests(unittest.TestCase):
    def test_gate_none_without_runtime(self):
        self.assertEqual(make_handler(None).run_stop_gate({})[0], "none")

    def test_gate_continue_on_block(self):
        rt = StopOnlyRuntime([_agg(decision="block", reason="not done yet")])
        h = make_handler(rt)
        action, text, agg = with_runtime(rt, lambda: h.run_stop_gate({"result": "CURRENT_TASK_DONE"}))
        self.assertEqual((action, text), ("continue", "not done yet"))
        self.assertTrue(agg.blocked)

    def test_gate_stop_on_prevent_continuation(self):
        rt = StopOnlyRuntime([_agg(prevent_continuation=True, reason="stop now")])
        h = make_handler(rt)
        self.assertEqual(with_runtime(rt, lambda: h.run_stop_gate({}))[:2], ("stop", "stop now"))

    def test_gate_none_when_not_blocked(self):
        rt = StopOnlyRuntime([_agg()])
        h = make_handler(rt)
        self.assertEqual(with_runtime(rt, lambda: h.run_stop_gate({}))[0], "none")

    def test_gate_marks_stop_emitted(self):
        rt = StopOnlyRuntime([_agg()])
        h = make_handler(rt)
        self.assertFalse(h.stop_emitted)
        with_runtime(rt, lambda: h.run_stop_gate({}))
        self.assertTrue(h.stop_emitted)

    def test_gate_phase_and_stop_hook_active(self):
        rt = StopOnlyRuntime([_agg(), _agg()])
        h = make_handler(rt)
        with_runtime(rt, lambda: (h.run_stop_gate({}), h.run_stop_gate({})))
        self.assertEqual(rt.stop_calls[0]["phase"], "turn_end")
        self.assertFalse(rt.stop_calls[0]["stop_hook_active"])
        self.assertTrue(rt.stop_calls[1]["stop_hook_active"])

    def test_gate_failsafe_after_max_checks(self):
        rt = StopOnlyRuntime([_agg(decision="block", reason="x")] * 10)
        h = make_handler(rt)
        actions = with_runtime(rt, lambda: [h.run_stop_gate({})[0] for _ in range(10)])
        self.assertEqual(actions[:8], ["continue"] * 8)
        self.assertEqual(actions[8:], ["none", "none"])
        self.assertEqual(len(rt.stop_calls), 8)  # no events past the failsafe

    def test_gate_swallows_hook_error(self):
        rt = BoomRuntime()
        h = make_handler(rt)
        # run_hook_event swallows errors and returns a benign aggregate.
        def fake(agent, event, data=None, *, emit=None):
            try:
                return rt.run_event(agent, event, data, emit=emit)
            except Exception:
                return HookAggregate(event=event)
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=fake):
            action, text, _ = h.run_stop_gate({})
        self.assertEqual(action, "none")


class StopGateLoopTests(unittest.TestCase):
    def _run(self, client, rt, max_turns=6):
        h = make_handler(rt)
        return with_runtime(rt, lambda: drain(agent_runner_loop(client, "sys", "hi", h, [], max_turns=max_turns, verbose=False)))

    def test_normal_stop_emits_stop_once(self):
        client = FakeClient([FakeResponse("answer")])
        rt = StopOnlyRuntime([_agg()])
        text, result = self._run(client, rt)
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.get("result"), "CURRENT_TASK_DONE")
        self.assertEqual(len(rt.stop_calls), 1)  # beep on the real stop

    def test_blocked_stop_keeps_loop_alive_then_finishes(self):
        client = FakeClient([FakeResponse("first answer"), FakeResponse("second answer")])
        rt = StopOnlyRuntime([_agg(decision="block", reason="keep going"), _agg()])
        text, result = self._run(client, rt)
        self.assertEqual(client.calls, 2)  # blocked turn did NOT end the loop
        self.assertEqual(result.get("result"), "CURRENT_TASK_DONE")
        # the reason is injected into the 2nd model input, not the display stream
        second_input = client.messages[1][-1]["content"]
        self.assertIn("keep going", second_input)
        self.assertEqual(len(rt.stop_calls), 2)  # one per real stop attempt

    def test_prevent_continuation_ends_loop(self):
        client = FakeClient([FakeResponse("only answer"), FakeResponse("unused")])
        rt = StopOnlyRuntime([_agg(prevent_continuation=True, reason="halt")])
        text, result = self._run(client, rt)
        self.assertEqual(client.calls, 1)  # stopped immediately
        self.assertIn("halt", text)        # surfaced in display
        self.assertEqual(result.get("result"), "CURRENT_TASK_DONE")

    def test_no_hook_behaviour_unchanged(self):
        client = FakeClient([FakeResponse("answer")])
        text, result = with_runtime(StopOnlyRuntime([]), lambda: drain(
            agent_runner_loop(client, "sys", "hi", make_handler(None), [], max_turns=6, verbose=False)))
        self.assertEqual(client.calls, 1)
        self.assertEqual(result.get("result"), "CURRENT_TASK_DONE")

if __name__ == "__main__":
    unittest.main()
