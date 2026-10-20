import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from genericagent.core.app import run_task_worker_loop


class FakeBackend:
    def __init__(self):
        self.history = []
        self.extra_sys_prompt = ""
        self.model = "fake"

    def chat(self, messages, tools=None):
        response = SimpleNamespace(content="direct answer", thinking="", tool_calls=[])
        if False:
            yield None
        return response


class RecordingRuntime:
    def __init__(self):
        self.calls = []

    def run_event(self, agent, event, data=None, *, emit=None):
        from genericagent.hooks.models import HookAggregate
        self.calls.append((getattr(agent, "agent_type", None), event, dict(data or {})))
        return HookAggregate(event=event)


class DirectAnswerAgent:
    """Minimal real GenericAgent surface for GenericAgent.run()."""

    def __init__(self, *, is_subagent=False):
        from genericagent.core.app import GenericAgent
        self._agent = GenericAgent.__new__(GenericAgent)
        a = self._agent
        a.lock = threading.Lock()
        a.task_dir = None
        a.history = []
        a.handler = None
        a.task_queue = queue.Queue()
        a.is_running = False
        a.stop_sig = False
        a.llm_no = 0
        a.inc_out = False
        a.verbose = False
        a.peer_hint = False
        a.is_subagent = is_subagent
        a.capability_profile = None
        a.capability_role_tools = None
        a.capability_allow_delegation = False
        a.capability_team_mode = False
        a.permission_mode = "full_access"
        a.subagent_permission_policy = None
        a.permission_runtime = None
        a.log_path = ""
        backend = FakeBackend()
        a.llmclient = SimpleNamespace(backend=backend, last_tools="", chat=backend.chat)
        a.llmclients = [a.llmclient]
        a.session_id = "session_test"
        a.session_path = None
        a.session_turn_id = 0
        a.agent_id = None
        a.agent_type = None
        a.hook_runtime = RecordingRuntime()
        a._test_hook_runtime = a.hook_runtime
        a.hook_emit = None

    def __getattr__(self, name):
        return getattr(self._agent, name)

    def __setattr__(self, name, value):
        if name == "_agent" or "_agent" not in self.__dict__:
            object.__setattr__(self, name, value)
            return
        setattr(self._agent, name, value)

    def run(self):
        return self._agent.run()


class HooksLifecycleTests(unittest.TestCase):
    def _run_direct(self, agent, text="hello"):
        dq = agent.put_task(text, source="user")
        thread = threading.Thread(target=agent.run, daemon=True)
        globals_ref = agent._agent.run.__globals__
        runtime = agent._test_hook_runtime
        def run_hook_event(_agent, event, data=None, *, emit=None):
            from genericagent.hooks.models import HookAggregate
            runtime.calls.append((getattr(_agent, "agent_type", None), event, dict(data or {})))
            return HookAggregate(event=event)
        with mock.patch.dict(globals_ref, {
            "get_system_prompt": lambda *_args, **_kwargs: "",
            "build_system_prompt_sections": lambda *_args, **_kwargs: {},
            "render_system_prompt": lambda *_args, **_kwargs: "",
            "note_system_prompt_sections": lambda *_args, **_kwargs: [],
            "load_tool_schema": lambda *_args, **_kwargs: None,
            "TOOLS_SCHEMA": [],
        }), mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=run_hook_event), mock.patch.object(globals_ref["session_transcript"], "current_backend_history", return_value=[]), mock.patch.object(
            globals_ref["session_transcript"], "record_agent_turn", return_value=None
        ):
            thread.start()
            items = []
            while True:
                item = dq.get(timeout=2)
                items.append(item)
                if "done" in item:
                    break
            thread.join(timeout=2)
        self.assertTrue(any("done" in item for item in items))
        return items

    def test_lead_stop_fires_on_user_turn_boundary(self):
        agent = DirectAnswerAgent(is_subagent=False)
        self._run_direct(agent)
        runtime = agent._test_hook_runtime
        events = [event for _, event, _ in runtime.calls]
        self.assertEqual(events.count("UserPromptSubmit"), 1)
        self.assertEqual(events.count("Stop"), 1)
        self.assertNotIn("SubagentStop", events)

    def test_subagent_does_not_emit_lead_stop(self):
        agent = DirectAnswerAgent(is_subagent=True)
        self._run_direct(agent)
        runtime = agent._test_hook_runtime
        events = [event for _, event, _ in runtime.calls]
        self.assertEqual(events.count("UserPromptSubmit"), 1)
        self.assertEqual(events.count("Stop"), 0)
        self.assertEqual(events.count("StopFailure"), 0)

    def test_worker_lifetime_emits_subagent_start_and_stop(self):
        class WorkerAgent:
            def __init__(self):
                self.peer_hint = True
                self.task_dir = None
                self.is_subagent = False
                self.hook_runtime = RecordingRuntime()
                self._test_hook_runtime = self.hook_runtime
                self.agent_id = None
                self.agent_type = None

            def put_task(self, raw, source="task"):
                dq = queue.Queue()
                dq.put({"done": "worker answer"})
                return dq

        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "worker"
            task_dir.mkdir(parents=True)
            (task_dir / "input.txt").write_text("do the task", encoding="utf-8")
            agent = WorkerAgent()
            def run_hook_event(_agent, event, data=None, *, emit=None):
                from genericagent.hooks.models import HookAggregate
                agent._test_hook_runtime.calls.append((getattr(_agent, "agent_type", None), event, dict(data or {})))
                return HookAggregate(event=event)
            with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=run_hook_event):
                run_task_worker_loop(agent, task_dir, reply_wait_iterations=0, sleep_fn=lambda _: None)
        runtime = agent._test_hook_runtime
        events = [event for _, event, _ in runtime.calls]
        self.assertEqual(events.count("SubagentStart"), 1)
        self.assertEqual(events.count("SubagentStop"), 1)
        self.assertNotIn("Stop", events)


if __name__ == "__main__":
    unittest.main()
