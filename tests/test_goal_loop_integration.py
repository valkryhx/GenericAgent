"""Goal + real agent_runner_loop: unmet goal blocks turn end, met clears it.

Drives the real loop with a scripted Stop aggregate (ok:false then ok:true) so
we exercise the actual continue path + goal accounting without any network.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

from genericagent.core import goal as goal_core
from genericagent.core import goal_evaluator as ge
from genericagent.core.loop import agent_runner_loop
from genericagent.hooks.models import HookAggregate


class FakeResponse:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.thinking = ""
        self.tool_calls = tool_calls or []


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.messages = []
        self.last_tools = ""
        self.backend = SimpleNamespace(history=[], name="fake", extra_sys_prompt="")

    def chat(self, messages, tools=None):
        self.calls += 1
        self.messages.append(messages)
        resp = self._responses.pop(0) if self._responses else FakeResponse("done")
        if False:
            yield None
        return resp


def _verdict_agg(ok, reason):
    agg = HookAggregate(event="Stop")
    r = SimpleNamespace(handler=SimpleNamespace(kind="prompt", output_schema={"type": "object"}),
                        status=("success" if ok else "blocking"),
                        output={"ok": ok, "reason": reason})
    agg.results.append(r)
    if not ok:
        agg.decision = "block"
        agg.reason = reason
    return agg


class _DummyRuntime:
    def run_event(self, agent, event, data=None, *, emit=None):
        return HookAggregate(event=event)


class GoalLoopIntegrationTests(unittest.TestCase):
    def _build(self):
        from genericagent.core.app import GenericAgent
        from genericagent.core.handler import GenericAgentHandler
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = goal_core.ActiveGoal(condition="get it done")
        a.is_subagent = False
        a.hook_runtime = _DummyRuntime()
        a.session_path = None
        a.session_id = "s"
        a.history = []
        a.task_dir = None
        a.verbose = False
        a.stop_sig = False
        a._record_goal_status = lambda **kw: None
        a._record_active_goal = lambda: None
        h = GenericAgentHandler(a, [], ".", workspace_root=None)
        h.hook_runtime = a.hook_runtime
        h.permission_runtime = None
        h.max_turns = 6
        return a, h

    def test_unmet_blocks_then_met_clears(self):
        a, h = self._build()
        client = FakeClient([FakeResponse("attempt 1"), FakeResponse("attempt 2")])
        aggs = [_verdict_agg(False, "not yet"), _verdict_agg(True, "done")]

        def fake_run(agent, event, data=None, *, emit=None):
            if event == "Stop":
                return aggs.pop(0) if aggs else _verdict_agg(True, "done")
            return HookAggregate(event=event)

        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=fake_run):
            chunks = []
            gen = agent_runner_loop(client, "sys", "hi", h, [], max_turns=6, verbose=False)
            try:
                while True:
                    chunks.append(next(gen))
            except StopIteration as stop:
                result = stop.value

        # First stop was blocked -> loop continued -> second model call happened.
        self.assertEqual(client.calls, 2)
        # The unmet reason was injected into the 2nd model input.
        self.assertIn("not yet", client.messages[1][-1]["content"])
        # Goal was cleared once the evaluator reported ok.
        self.assertIsNone(a.active_goal)
        self.assertEqual(result.get("result"), "CURRENT_TASK_DONE")


if __name__ == "__main__":
    unittest.main()
