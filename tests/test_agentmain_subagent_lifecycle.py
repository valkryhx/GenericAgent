import json
import queue
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import _subagent_event, run_task_worker_loop, start_task_background  # noqa: E402


class FakeAgent:
    def __init__(self, done_text):
        backend = type("Backend", (), {"history": []})()
        self.llmclient = type("Client", (), {"backend": backend})()
        self.done_text = done_text
        self.peer_hint = True
        self.task_dir = None

    def put_task(self, raw, source="task"):
        self.seen_raw = raw
        self.seen_source = source
        result = queue.Queue()
        result.put({"done": self.done_text})
        return result


class MultiRoundFakeAgent:
    def __init__(self, done_texts):
        backend = type("Backend", (), {"history": []})()
        self.llmclient = type("Client", (), {"backend": backend})()
        self.done_texts = list(done_texts)
        self.seen_raws = []
        self.peer_hint = True
        self.task_dir = None

    def put_task(self, raw, source="task"):
        self.seen_raws.append(raw)
        result = queue.Queue()
        result.put({"done": self.done_texts.pop(0)})
        return result


class FakeToolCallFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = json.dumps(arguments, ensure_ascii=False)


class FakeToolCall:
    def __init__(self, name, arguments, call_id="call_1"):
        self.id = call_id
        self.function = FakeToolCallFunction(name, arguments)


class FakeResponse:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.thinking = ""
        self.tool_calls = tool_calls or []


class ToolTranscriptBackend:
    def __init__(self, fixture_path):
        self.fixture_path = str(fixture_path)
        self.history = []
        self.extra_sys_prompt = ""
        self.model = "test-model"
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            response = FakeResponse(
                "need file",
                [FakeToolCall("file_read", {"path": self.fixture_path, "show_linenos": False})],
            )
        else:
            response = FakeResponse("final answer from backend")
        if False:
            yield None
        return response


class PermissionTranscriptBackend:
    def __init__(self):
        self.history = []
        self.extra_sys_prompt = ""
        self.model = "test-model"
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            response = FakeResponse(
                "need write",
                [FakeToolCall("file_write", {"path": "blocked.txt", "content": "deny me"})],
            )
        else:
            response = FakeResponse("mutation denied and reported")
        if False:
            yield None
        return response


class TranscriptGenericAgent:
    def __init__(self, backend):
        from agentmain import GenericAgent

        self._agent = GenericAgent.__new__(GenericAgent)
        self._agent.lock = threading.Lock()
        self._agent.task_dir = None
        self._agent.history = []
        self._agent.handler = None
        self._agent.task_queue = queue.Queue()
        self._agent.is_running = False
        self._agent.stop_sig = False
        self._agent.llm_no = 0
        self._agent.inc_out = False
        self._agent.verbose = False
        self._agent.peer_hint = False
        self._agent.permission_mode = "full_access"
        self._agent.subagent_permission_policy = None
        self._agent.permission_runtime = None
        self._agent.log_path = str(REPO_ROOT / "temp" / "test-subagent-transcript.log")
        self._agent.llmclient = SimpleNamespace(backend=backend, last_tools="", chat=backend.chat)
        self._agent.llmclients = [self._agent.llmclient]
        self._agent.session_id = "session_demo"
        self._agent.session_path = None

    def __getattr__(self, name):
        return getattr(self._agent, name)

    def __setattr__(self, name, value):
        if name == "_agent" or "_agent" not in self.__dict__:
            object.__setattr__(self, name, value)
            return
        setattr(self._agent, name, value)

    def run(self):
        return self._agent.run()


class AgentMainSubagentLifecycleTest(unittest.TestCase):
    def test_start_task_background_writes_initial_state_registry_and_event(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            class FakeProcess:
                pid = 13579

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            pid = start_task_background(
                "cli_task",
                input_text="long prompt must stay out of child command",
                llm_no=3,
                verbose=True,
                root_dir=td,
                popen=fake_popen,
                python_executable="python-test",
            )

            task_dir = Path(td) / "temp" / "cli_task"
            self.assertEqual(pid, 13579)
            self.assertEqual(
                (task_dir / "input.txt").read_text(encoding="utf-8"),
                "long prompt must stay out of child command",
            )
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[0], "python-test")
            self.assertIn("--task", cmd)
            self.assertIn("cli_task", cmd)
            self.assertIn("--nobg", cmd)
            self.assertIn("--llm_no", cmd)
            self.assertIn("3", cmd)
            self.assertIn("--verbose", cmd)
            self.assertNotIn("long prompt must stay out of child command", cmd)
            self.assertEqual(Path(kwargs["cwd"]), Path(td))
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["pid"], 13579)
            self.assertEqual(state["turn_status"], "pending")
            self.assertEqual(state["process_status"], "alive")
            self.assertEqual(state["input_path"], str(task_dir / "input.txt"))
            events = [
                json.loads(line)
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["type"], "agent_started")
            self.assertEqual(events[-1]["pid"], 13579)
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["agents"]["/root/cli_task"]["pid"], 13579)

    def test_start_task_background_writes_context_fork_history_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            history = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ]

            class FakeProcess:
                pid = 24680

            def fake_popen(_cmd, **_kwargs):
                return FakeProcess()

            start_task_background(
                "forked_cli_task",
                input_text="new task prompt",
                root_dir=td,
                popen=fake_popen,
                python_executable="python-test",
                fork_turns="2",
                fork_history=history,
            )

            task_dir = Path(td) / "temp" / "forked_cli_task"
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                history[-2:],
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["fork_turns"], "2")

    def test_subagent_event_mirrors_agent_closed_to_parent_inbox(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_close"
            task_dir.mkdir(parents=True)

            _subagent_event(
                task_dir,
                {
                    "type": "agent_closed",
                    "task_name": "demo_close",
                    "reason": "parent_cleanup",
                },
            )

            events = [
                json.loads(line)["type"]
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            inbox = [
                json.loads(line)["type"]
                for line in (Path(td) / "temp" / "subagents" / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events, ["agent_closed"])
            self.assertEqual(inbox, ["agent_closed"])

    def test_subagent_event_appends_sidechain_transcript_when_session_is_known(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_transcript"
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(
                json.dumps(
                    {
                        "task_name": "demo_transcript",
                        "agent_path": "/root/demo_transcript",
                        "run_id": "run_demo",
                        "parent_session_id": "session_demo",
                    }
                ),
                encoding="utf-8",
            )

            _subagent_event(
                task_dir,
                {
                    "type": "turn_completed",
                    "task_name": "demo_transcript",
                    "summary": "done token=secret-value",
                },
            )

            transcript_path = Path(td) / "temp" / "sessions" / "session_demo" / "subagents" / "run_demo.jsonl"
            rows = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["type"], "turn_completed")
            self.assertIn("token=<redacted>", rows[-1]["payload"]["summary"])

    def test_task_worker_loop_writes_output_state_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_task"
            agent = FakeAgent("final answer")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="do the task",
                reply_wait_iterations=0,
                sleep_fn=lambda _: None,
            )

            self.assertEqual(agent.seen_raw, "do the task")
            self.assertEqual(agent.seen_source, "task")
            self.assertFalse(agent.peer_hint)
            self.assertEqual(agent.task_dir, str(task_dir))
            self.assertEqual(
                (task_dir / "output.txt").read_text(encoding="utf-8"),
                "final answer\n\n[ROUND END]\n",
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "completed")
            self.assertEqual(state["process_status"], "exited")
            self.assertEqual(state["final_output_path"], str(task_dir / "output.txt"))
            events = [
                json.loads(line)["type"]
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                events,
                ["turn_started", "turn_completed", "agent_waiting_reply", "agent_exited"],
            )
            parent_events = [
                json.loads(line)["type"]
                for line in (Path(td) / "temp" / "subagents" / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(parent_events, ["turn_completed", "agent_waiting_reply", "agent_exited"])

    def test_task_worker_loop_resumes_from_state_round_without_overwriting_previous_output(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_resume_worker"
            task_dir.mkdir(parents=True)
            old_output = task_dir / "output.txt"
            old_output.write_text("old answer\n\n[ROUND END]\n", encoding="utf-8")
            (task_dir / "input.txt").write_text("continue task", encoding="utf-8")
            (task_dir / "_history.json").write_text(
                json.dumps([{"role": "assistant", "content": "old answer"}], ensure_ascii=False),
                encoding="utf-8",
            )
            (task_dir / "state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "task_name": "demo_resume_worker",
                        "agent_path": "/root/demo_resume_worker",
                        "run_id": "run_resume_worker",
                        "parent_session_id": "session_resume_worker",
                        "round": 1,
                        "turn_status": "pending",
                        "process_status": "alive",
                        "output_path": str(task_dir / "output1.txt"),
                        "final_output_path": str(old_output),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            agent = FakeAgent("resumed answer")

            run_task_worker_loop(
                agent,
                task_dir,
                reply_wait_iterations=0,
                sleep_fn=lambda _: None,
            )

            self.assertEqual(agent.seen_raw, "continue task")
            self.assertEqual(agent.llmclient.backend.history, [{"role": "assistant", "content": "old answer"}])
            self.assertEqual(old_output.read_text(encoding="utf-8"), "old answer\n\n[ROUND END]\n")
            self.assertEqual(
                (task_dir / "output1.txt").read_text(encoding="utf-8"),
                "resumed answer\n\n[ROUND END]\n",
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["round"], 1)
            self.assertEqual(state["final_output_path"], str(task_dir / "output1.txt"))
            events = [
                json.loads(line)
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[0]["round"], 1)
            self.assertEqual(events[1]["round"], 1)

    def test_task_worker_loop_records_sidechain_request_tool_result_permission_and_final_output(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_transcript_full"
            task_dir.mkdir(parents=True)
            (task_dir / "fixture.txt").write_text("fixture body", encoding="utf-8")
            (task_dir / "input.txt").write_text("inspect fixture", encoding="utf-8")
            (task_dir / "state.json").write_text(
                json.dumps(
                    {
                        "task_name": "demo_transcript_full",
                        "agent_path": "/root/demo_transcript_full",
                        "run_id": "run_transcript_full",
                        "parent_session_id": "session_transcript_full",
                    }
                ),
                encoding="utf-8",
            )
            agent = TranscriptGenericAgent(ToolTranscriptBackend(task_dir / "fixture.txt"))
            agent._agent.task_dir = str(task_dir)

            worker = threading.Thread(target=agent.run, daemon=True)
            globals_ref = agent._agent.run.__globals__
            with unittest.mock.patch.dict(globals_ref, {
                "get_system_prompt": lambda *_args, **_kwargs: "",
                "load_tool_schema": lambda *_args, **_kwargs: None,
                "TOOLS_SCHEMA": [],
            }), unittest.mock.patch.object(globals_ref["session_transcript"], "current_backend_history", return_value=[]), unittest.mock.patch.object(
                globals_ref["session_transcript"], "record_agent_turn", return_value=None
            ):
                worker.start()
                run_task_worker_loop(
                    agent,
                    task_dir,
                    reply_wait_iterations=0,
                    sleep_fn=lambda _: None,
                )
                worker.join(timeout=2)

            transcript_path = Path(td) / "temp" / "sessions" / "session_transcript_full" / "subagents" / "run_transcript_full.jsonl"
            rows = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
            types = [row["type"] for row in rows]
            self.assertIn("request", types)
            self.assertIn("tool_call", types)
            self.assertIn("tool_result", types)
            self.assertIn("assistant", types)
            self.assertIn("final_output", types)
            self.assertEqual(
                next(row for row in rows if row["type"] == "request")["payload"]["prompt"],
                "inspect fixture",
            )
            tool_call = next(row for row in rows if row["type"] == "tool_call")
            self.assertEqual(tool_call["payload"]["tool_name"], "file_read")
            self.assertEqual(tool_call["payload"]["args"]["path"], str(task_dir / "fixture.txt"))
            tool_result = next(row for row in rows if row["type"] == "tool_result")
            self.assertEqual(tool_result["payload"]["tool_name"], "file_read")
            self.assertIn("fixture body", tool_result["payload"]["data"])
            assistant = next(row for row in rows if row["type"] == "assistant")
            self.assertIn("final answer from backend", assistant["payload"]["content"])
            final_output = next(row for row in rows if row["type"] == "final_output")
            self.assertIn("final answer from backend", final_output["payload"]["final_output"])
            self.assertEqual(final_output["payload"]["artifact_id"], "final_output_round_0")

    def test_task_worker_loop_records_sidechain_permission_decision_for_denied_tool(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_permission_transcript"
            task_dir.mkdir(parents=True)
            (task_dir / "input.txt").write_text("try mutation", encoding="utf-8")
            (task_dir / "state.json").write_text(
                json.dumps(
                    {
                        "task_name": "demo_permission_transcript",
                        "agent_path": "/root/demo_permission_transcript",
                        "run_id": "run_permission_transcript",
                        "parent_session_id": "session_permission_transcript",
                        "permission_profile": "inherit-current-permissions",
                        "parent_permission_mode": "read_only",
                    }
                ),
                encoding="utf-8",
            )
            agent = TranscriptGenericAgent(PermissionTranscriptBackend())
            agent._agent.task_dir = str(task_dir)

            worker = threading.Thread(target=agent.run, daemon=True)
            globals_ref = agent._agent.run.__globals__
            with unittest.mock.patch.dict(globals_ref, {
                "get_system_prompt": lambda *_args, **_kwargs: "",
                "load_tool_schema": lambda *_args, **_kwargs: None,
                "TOOLS_SCHEMA": [],
            }), unittest.mock.patch.object(globals_ref["session_transcript"], "current_backend_history", return_value=[]), unittest.mock.patch.object(
                globals_ref["session_transcript"], "record_agent_turn", return_value=None
            ):
                worker.start()
                run_task_worker_loop(
                    agent,
                    task_dir,
                    reply_wait_iterations=0,
                    sleep_fn=lambda _: None,
                )
                worker.join(timeout=2)

            self.assertFalse((task_dir / "blocked.txt").exists())
            transcript_path = Path(td) / "temp" / "sessions" / "session_permission_transcript" / "subagents" / "run_permission_transcript.jsonl"
            rows = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
            permission = next(row for row in rows if row["type"] == "permission_decision")
            self.assertEqual(permission["payload"]["tool_name"], "file_write")
            self.assertEqual(permission["payload"]["decision"]["action"], "deny")
            self.assertEqual(
                permission["payload"]["decision"]["details"]["parent_permission_mode"],
                "read_only",
            )

    def test_task_worker_loop_stops_gracefully_when_stop_file_appears_during_reply_wait(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_stop"
            agent = FakeAgent("final answer")
            sleep_calls = {"count": 0}

            def sleep_fn(_):
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    (task_dir / "_stop").parent.mkdir(parents=True, exist_ok=True)
                    (task_dir / "_stop").write_text("stop now", encoding="utf-8")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="do the task",
                reply_wait_iterations=5,
                reply_sleep_s=0,
                sleep_fn=sleep_fn,
            )

            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "completed")
            self.assertEqual(state["process_status"], "shutdown")
            events = [
                json.loads(line)["type"]
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("agent_shutdown", events)
            self.assertNotIn("agent_exited", events)

    def test_task_worker_loop_consumes_trigger_message_from_mailbox_without_reply_file(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_mailbox"
            agent = MultiRoundFakeAgent(["first answer", "second answer"])
            sleep_calls = {"count": 0}

            def sleep_fn(_):
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    row = {
                        "schema_version": 1,
                        "id": "msg_1",
                        "author": "/root",
                        "recipient": "/root/demo_mailbox",
                        "content": "second task from mailbox",
                        "trigger_turn": True,
                        "priority": "normal",
                        "created_at": "2026-07-13T00:00:00+08:00",
                        "consumed_at": None,
                    }
                    (task_dir / "mailbox.jsonl").write_text(
                        json.dumps(row, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="first task",
                reply_wait_iterations=1,
                reply_sleep_s=0,
                sleep_fn=sleep_fn,
            )

            self.assertEqual(agent.seen_raws, ["first task", "second task from mailbox"])
            self.assertFalse((task_dir / "reply.txt").exists())
            self.assertEqual(
                (task_dir / "output1.txt").read_text(encoding="utf-8"),
                "second answer\n\n[ROUND END]\n",
            )
            mailbox_rows = [
                json.loads(line)
                for line in (task_dir / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIsNotNone(mailbox_rows[0]["consumed_at"])
            events = [
                json.loads(line)["type"]
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events.count("message_consumed"), 1)

    def test_task_worker_loop_clears_matching_reply_file_after_consuming_mailbox_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_mailbox_reply"
            agent = MultiRoundFakeAgent(["first answer", "second answer"])
            sleep_calls = {"count": 0}

            def sleep_fn(_):
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    row = {
                        "schema_version": 1,
                        "id": "msg_1",
                        "author": "/root",
                        "recipient": "/root/demo_mailbox_reply",
                        "content": "second task from mailbox",
                        "trigger_turn": True,
                        "priority": "normal",
                        "created_at": "2026-07-13T00:00:00+08:00",
                        "consumed_at": None,
                    }
                    (task_dir / "mailbox.jsonl").write_text(
                        json.dumps(row, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    (task_dir / "reply.txt").write_text("second task from mailbox", encoding="utf-8")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="first task",
                reply_wait_iterations=1,
                reply_sleep_s=0,
                sleep_fn=sleep_fn,
            )

            self.assertEqual(agent.seen_raws, ["first task", "second task from mailbox"])
            self.assertFalse((task_dir / "reply.txt").exists())


class MalformedControlFileTest(unittest.TestCase):
    """S12: a corrupt `_history.json` killed the subagent with a bare JSONDecodeError.

    Measured (temp/_probe_malformed.py) on a truncated `_history.json`:
      raised JSONDecodeError, no state.json turn_status/process_status, no last_error,
      events.jsonl absent, and the malformed file already consumed (deleted) — so the child
      died before writing a single trace of why, and the evidence went with it.

    `state.json` was already tolerant: every read goes through `read_json_or_none()`, which
    returns None on JSONDecodeError. The asymmetry was the defect — two persisted control
    files, one guarded and one not.
    """

    TRUNCATED_HISTORY = '[{"role": "user", "content": "tru'

    def test_malformed_history_does_not_kill_the_child_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "bad_history"
            task_dir.mkdir(parents=True)
            (task_dir / "_history.json").write_text(self.TRUNCATED_HISTORY, encoding="utf-8")
            agent = FakeAgent("answer despite bad history")
            agent.llmclient.backend.history = ["PRESET"]

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="do the task",
                reply_wait_iterations=0,
                sleep_fn=lambda _: None,
            )

            # The turn still runs: a corrupt resume projection costs the forked context, not
            # the task. Losing the whole run would turn a recoverable file into a dead agent.
            self.assertEqual(
                (task_dir / "output.txt").read_text(encoding="utf-8"),
                "answer despite bad history\n\n[ROUND END]\n",
            )
            self.assertEqual(agent.llmclient.backend.history, ["PRESET"])

            events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            corrupt = [e for e in events if e["type"] == "history_load_failed"]
            self.assertEqual(len(corrupt), 1, f"no history_load_failed event in {[e['type'] for e in events]}")
            self.assertIn("_history.json", corrupt[0]["path"])
            self.assertTrue(corrupt[0]["error"])

            # The bad bytes are the only evidence of what the parent tried to hand over, so
            # they get set aside rather than dropped by consume_file.
            quarantined = sorted(p.name for p in task_dir.glob("_history.json.*"))
            self.assertEqual(len(quarantined), 1, f"malformed history was not quarantined: {quarantined}")
            self.assertEqual(
                (task_dir / quarantined[0]).read_text(encoding="utf-8"),
                self.TRUNCATED_HISTORY,
            )
            self.assertFalse((task_dir / "_history.json").exists())

    def test_malformed_state_json_still_lets_the_turn_complete(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "bad_state"
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text('{"run_id": "run_000007", "parent_sess', encoding="utf-8")
            agent = FakeAgent("answer despite bad state")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="do the task",
                reply_wait_iterations=0,
                sleep_fn=lambda _: None,
            )

            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "completed")
            self.assertEqual(state["agent_path"], "/root/bad_state")
            events = [json.loads(line)["type"] for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("turn_completed", events)

    def test_malformed_state_json_is_quarantined_before_it_is_overwritten(self):
        """The corrupt bytes are the only record of run_id / parent_session_id / artifact_dir.

        Measured: a truncated state.json did not crash the child (every read goes through
        read_json_or_none), but the first `_subagent_state()` write replaced the whole file
        from an empty dict — so run_id "run_000007" was gone, and with it the sidechain
        transcript path and the artifact dir. The turn looked healthy and the linkage was
        silently severed.
        """
        corrupt = '{"run_id": "run_000007", "parent_sess'
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "quarantine_state"
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(corrupt, encoding="utf-8")
            agent = FakeAgent("answer")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="do the task",
                reply_wait_iterations=0,
                sleep_fn=lambda _: None,
            )

            kept = sorted(p.name for p in task_dir.glob("state.json.malformed.*"))
            self.assertEqual(len(kept), 1, f"corrupt state.json was overwritten without a copy: {kept}")
            self.assertEqual((task_dir / kept[0]).read_text(encoding="utf-8"), corrupt)
            events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            failures = [e for e in events if e["type"] == "state_load_failed"]
            self.assertEqual(len(failures), 1, f"no state_load_failed event in {[e['type'] for e in events]}")
            self.assertIn("state.json", failures[0]["path"])
            self.assertTrue(failures[0]["error"])


class InterruptDuringRunningTurnTest(unittest.TestCase):
    """S12: `interrupt_agent` only took effect at a turn boundary, never inside one.

    Measured (temp/_probe_interrupt_midturn.py, temp/_probe_interrupt_tool.py):
      - long provider call: `_stop` written 0.5s into a 6s call → abort observed 5.5s later,
        i.e. exactly when the call returned. Interrupt latency == turn length.
      - long tool call: `_stop` written 0.5s into a 6s tool → `agent.stop_sig` never became
        True at all, the tool never saw `code_stop_signal`, and the agent went on to run a
        second LLM turn. `stop_stop_to_done` 16s.

    Cause: the only `_stop` check sat inside `for chunk in gen` (`agentmain.py`), and a
    subagent spawns with `verbose=False`, where `agent_loop` drives both the provider call
    (`exhaust(response_gen)`) and every tool (`exhaust(proxy())`) without yielding a chunk.
    No chunk means no check, so the file sat unread for the whole call.
    """

    def _agent(self, task_dir, client, permission_mode="read_only"):
        from agentmain import GenericAgent

        agent = GenericAgent.__new__(GenericAgent)
        agent.lock = threading.Lock()
        agent.task_dir = str(task_dir)
        agent.history = []
        agent.handler = None
        agent.task_queue = queue.Queue()
        agent.is_running = False
        agent.stop_sig = False
        agent.llm_no = 0
        agent.inc_out = False
        # Subagents spawn non-verbose (subagent_manager.spawn_agent verbose=False), which is
        # the mode where agent_loop swallows every yield. Testing verbose=True would hide
        # the defect behind the chunk stream.
        agent.verbose = False
        agent.peer_hint = False
        agent.permission_mode = permission_mode
        agent.subagent_permission_policy = None
        agent.permission_runtime = None
        agent.log_path = str(task_dir / "log.txt")
        agent.session_id = None
        agent.session_path = None
        agent.session_turn_id = 0
        agent.llmclient = client
        agent.llmclients = [client]
        return agent

    def test_stop_file_aborts_the_agent_while_the_provider_call_is_still_running(self):
        class BlockingChatClient:
            def __init__(self):
                self.backend = SimpleNamespace(history=[], extra_sys_prompt="")
                self.last_tools = ""
                self.entered = threading.Event()
                self.cancel_requested = threading.Event()

            def chat(self, messages=None, tools=None):
                self.entered.set()
                # A real provider call yields nothing until the first bytes arrive; the
                # fail-safe keeps the test bounded when the abort never comes.
                self.cancel_requested.wait(20.0)
                return FakeResponse("late answer")
                yield

            def cancel_current_request(self):
                self.cancel_requested.set()

            def reset_cancel(self):
                self.cancel_requested.clear()

        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "interrupt_llm"
            task_dir.mkdir(parents=True)
            client = BlockingChatClient()
            agent = self._agent(task_dir, client)

            threading.Thread(target=agent.run, daemon=True).start()
            agent.put_task("do the task", source="task")
            self.assertTrue(client.entered.wait(60.0), "provider call never started")

            (task_dir / "_stop").write_text("parent_interrupt", encoding="utf-8")

            self.assertTrue(
                client.cancel_requested.wait(2.0),
                "_stop did not cancel the in-flight provider call; interrupt still waits for the turn to end",
            )

    def test_stop_file_aborts_the_agent_while_a_tool_call_is_still_running(self):
        class ToolThenDoneClient:
            def __init__(self):
                self.backend = SimpleNamespace(history=[], extra_sys_prompt="")
                self.last_tools = ""
                self.calls = 0

            def chat(self, messages=None, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse("", [FakeToolCall("code_run", {"code": "probe", "code_type": "python"})])
                return FakeResponse("second turn should never be reached")
                yield

            def cancel_current_request(self):
                pass

            def reset_cancel(self):
                pass

        entered = threading.Event()
        saw_stop_signal = threading.Event()

        def blocking_tool(handler_self, args, response=None, **kwargs):
            from agent_loop import StepOutcome

            entered.set()
            if handler_self.code_stop_signal.wait(20.0):
                saw_stop_signal.set()
            return StepOutcome({"status": "ok"}, next_prompt="continue")
            yield

        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "interrupt_tool"
            task_dir.mkdir(parents=True)
            client = ToolThenDoneClient()
            agent = self._agent(task_dir, client, permission_mode="full_access")

            from ga import GenericAgentHandler

            with unittest.mock.patch.object(GenericAgentHandler, "do_code_run", blocking_tool):
                threading.Thread(target=agent.run, daemon=True).start()
                agent.put_task("do the task", source="task")
                self.assertTrue(entered.wait(60.0), "tool never started")

                (task_dir / "_stop").write_text("parent_interrupt", encoding="utf-8")

                self.assertTrue(
                    saw_stop_signal.wait(2.0),
                    "_stop did not reach code_stop_signal; a running tool cannot be interrupted",
                )

            self.assertEqual(client.calls, 1, "the agent kept going and started another LLM turn after _stop")


class ReplyWaitSchedulingTest(unittest.TestCase):
    """R3: poll granularity and idle lifetime were one knob doing two unrelated jobs.

    reply_wait_iterations=300 x reply_sleep_s=2 set both the wake-up granularity (latency)
    and the total idle lifetime (600s, when the for/else declares agent_exited). So halving
    latency halved the subagent's lifetime. With realtime signalling in place the coupling
    is backwards: the interval should grow to save CPU, but growing it shortens the life.
    """

    def _run(self, task_dir, **kwargs):
        agent = FakeAgent("final answer")
        clock = {"now": 0.0}
        waits = []

        def sleep_fn(seconds):
            waits.append(seconds)
            clock["now"] += float(seconds)

        run_task_worker_loop(
            agent,
            task_dir,
            input_text="do the task",
            sleep_fn=sleep_fn,
            monotonic_fn=lambda: clock["now"],
            **kwargs,
        )
        return waits, clock["now"]

    def test_poll_interval_does_not_change_idle_lifetime(self):
        with tempfile.TemporaryDirectory() as td:
            fine_waits, fine_elapsed = self._run(Path(td) / "temp" / "fine", poll_interval_s=0.01, idle_timeout_s=1.0)
            coarse_waits, coarse_elapsed = self._run(Path(td) / "temp" / "coarse", poll_interval_s=0.5, idle_timeout_s=1.0)

            self.assertEqual(len(fine_waits), 100)
            self.assertEqual(len(coarse_waits), 2)
            self.assertAlmostEqual(fine_elapsed, 1.0, places=6)
            self.assertAlmostEqual(coarse_elapsed, 1.0, places=6)

    def test_a_poll_interval_longer_than_the_lifetime_is_clamped_to_the_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            waits, elapsed = self._run(Path(td) / "temp" / "clamped", poll_interval_s=5.0, idle_timeout_s=1.0)

            self.assertEqual(waits, [1.0])
            self.assertAlmostEqual(elapsed, 1.0, places=6)

    def test_default_idle_lifetime_still_matches_the_previous_300x2s(self):
        with tempfile.TemporaryDirectory() as td:
            waits, elapsed = self._run(Path(td) / "temp" / "default")

            self.assertAlmostEqual(elapsed, 600.0, places=6)
            self.assertEqual(len(waits), 300)

    def test_legacy_iteration_params_keep_their_exact_meaning(self):
        with tempfile.TemporaryDirectory() as td:
            waits, elapsed = self._run(Path(td) / "temp" / "legacy", reply_wait_iterations=3, reply_sleep_s=0.5)

            self.assertEqual(waits, [0.5, 0.5, 0.5])
            self.assertAlmostEqual(elapsed, 1.5, places=6)

    def test_idle_timeout_exit_is_still_reported_as_agent_exited(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "exited"

            self._run(task_dir, poll_interval_s=0.25, idle_timeout_s=0.5)

            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["process_status"], "exited")
            events = [json.loads(line)["type"] for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("agent_exited", events)


    def test_env_overrides_supply_the_schedule_for_spawned_children(self):
        """The child is launched by SubagentManager, so the knobs need a non-argument path.

        Nothing in the spawn command line carries timing, so without an env path the split
        would only be reachable from tests.
        """
        from agentmain import resolve_reply_wait_schedule_from_env

        with unittest.mock.patch.dict(
            "os.environ", {"GA_SUBAGENT_POLL_INTERVAL_S": "0.25", "GA_SUBAGENT_IDLE_TIMEOUT_S": "900"}
        ):
            self.assertEqual(resolve_reply_wait_schedule_from_env(), {"poll_interval_s": 0.25, "idle_timeout_s": 900.0})

        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_reply_wait_schedule_from_env(), {})

        with unittest.mock.patch.dict("os.environ", {"GA_SUBAGENT_POLL_INTERVAL_S": "not-a-number"}):
            self.assertEqual(resolve_reply_wait_schedule_from_env(), {})


if __name__ == "__main__":
    unittest.main()
