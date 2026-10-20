import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from genericagent.core.app import run_task_worker_loop


class FakeBackend:
    def __init__(self, responses=None):
        self.history = []
        self.extra_sys_prompt = ""
        self.model = "fake"
        self.responses = list(responses or [])
        self.last_tools = ""

    def chat(self, messages, tools=None):
        response = self.responses.pop(0) if self.responses else SimpleNamespace(
            content="direct answer", thinking="", tool_calls=[]
        )
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

    def __init__(self, *, is_subagent=False, responses=None):
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
        backend = FakeBackend(responses=responses)
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
    def _run_direct(self, agent, text="hello", *, record_hooks=True):
        dq = agent.put_task(text, source="user")
        thread = threading.Thread(target=agent.run, daemon=True)
        globals_ref = agent._agent.run.__globals__
        runtime = agent._test_hook_runtime

        def run_hook_event(_agent, event, data=None, *, emit=None):
            if record_hooks:
                from genericagent.hooks.models import HookAggregate
                runtime.calls.append((getattr(_agent, "agent_type", None), event, dict(data or {})))
                return HookAggregate(event=event)
            return runtime.run_event(_agent, event, data, emit=emit)
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

    def test_stop_fires_once_after_three_internal_llm_turns(self):
        # Three model calls happen inside one user request. Only the outer
        # GenericAgent.run() boundary may emit Stop.
        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="update_working_checkpoint", arguments='{"key_info":"keep going"}'),
            id="call_1",
        )
        responses = [
            SimpleNamespace(content="step one", thinking="", tool_calls=[tool_call]),
            SimpleNamespace(content="step two", thinking="", tool_calls=[tool_call]),
            SimpleNamespace(content="final answer", thinking="", tool_calls=[]),
        ]
        agent = DirectAnswerAgent(is_subagent=False, responses=responses)
        self._run_direct(agent)
        runtime = agent._test_hook_runtime
        events = [event for _, event, _ in runtime.calls]
        self.assertEqual(agent.llmclient.backend.responses, [])
        self.assertEqual(events.count("UserPromptSubmit"), 1)
        self.assertEqual(events.count("Stop"), 1)
        self.assertEqual(events.count("SessionEnd"), 0)
        stop = next(data for _, event, data in runtime.calls if event == "Stop")
        self.assertEqual(stop["last_assistant_message"], "final answer")
        self.assertEqual(stop["exit_reason"], "completed")

    def test_configured_stop_hook_runs_once_after_three_internal_llm_turns(self):
        from genericagent.hooks.audit import HookAudit
        from genericagent.hooks.config import load_hook_registry
        from genericagent.hooks.runtime import HookRuntime

        tool_call = SimpleNamespace(
            function=SimpleNamespace(name="update_working_checkpoint", arguments='{"key_info":"keep going"}'),
            id="call_1",
        )
        responses = [
            SimpleNamespace(content="step one", thinking="", tool_calls=[tool_call]),
            SimpleNamespace(content="step two", thinking="", tool_calls=[tool_call]),
            SimpleNamespace(content="final answer", thinking="", tool_calls=[]),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_dir = root / ".genericagent"
            config_dir.mkdir()
            marker = root / "stop-fired.txt"
            script = root / "stop_hook.py"
            nl = chr(10)
            script.write_text(
                "import sys" + nl
                + "from pathlib import Path" + nl
                + "sys.stdin.read()" + nl
                + "Path(sys.argv[1]).open('ab').write(b'stop' + bytes([10]))" + nl,
                encoding="utf-8",
                newline=nl,
            )
            (config_dir / "hooks.json").write_text(json.dumps({
                "hooks": {
                    "Stop": [{
                        "hooks": [{
                            "type": "command",
                            "command": "python",
                            "args": [str(script), str(marker)],
                            "timeout": 5,
                        }]
                    }]
                }
            }), encoding="utf-8")
            registry = load_hook_registry(config_dir=config_dir, home=root / "home")
            runtime = HookRuntime(registry, audit=HookAudit(root / "audit"))
            agent = DirectAnswerAgent(is_subagent=False, responses=responses)
            agent.hook_runtime = runtime
            agent._test_hook_runtime = runtime
            try:
                self._run_direct(agent, record_hooks=False)
            finally:
                runtime.close()

            self.assertEqual(marker.read_text(encoding="utf-8"), "stop" + nl)
            audits = runtime.recent_events(10)
            self.assertEqual([row["event"] for row in audits], ["Stop"])
            self.assertEqual(audits[0]["input"]["last_assistant_message"], "final answer")
            self.assertEqual(audits[0]["input"]["exit_reason"], "completed")

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
