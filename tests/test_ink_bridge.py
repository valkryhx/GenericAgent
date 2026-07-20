import copy
import io
import json
import os
import subprocess
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
if str(FRONTENDS) not in sys.path:
    sys.path.insert(0, str(FRONTENDS))


from ink_bridge import GenericAgentBridge, encode_event, make_stdout_emitter, run_jsonl_loop  # noqa: E402
from workflow_child_agent import AgentResult  # noqa: E402
from workflow_models import WorkflowJob  # noqa: E402
from workflow_planner import WorkflowDraft  # noqa: E402
from workflow_runtime import WorkflowRuntime  # noqa: E402
from workflow_scheduler import SchedulerConfig  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.history = []
        self.last_usage_tokens = None


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""


class PartialFailureRunner:
    def __init__(self):
        self.started_job_ids = []
        self.cancelled_job_ids = set()
        self.success_done = False

    def start(self, job):
        self.started_job_ids.append(job.job_id)

    def poll(self, job):
        if job.job_id == "agent_1" and not self.success_done:
            self.success_done = True
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "bridge success artifact"},
                transcript_events=[{"type": "assistant", "text": "bridge success transcript"}],
            )
        if job.job_id == "agent_2" and self.success_done:
            return AgentResult(
                job_id=job.job_id,
                status="failed",
                payload={"error": "bridge partial failure"},
                transcript_events=[{"type": "error", "error": "bridge partial failure"}],
            )
        return None

    def cancel(self, job):
        self.cancelled_job_ids.add(job.job_id)


class RateLimitRunner(PartialFailureRunner):
    error = "HTTP 429 Too Many Requests: provider rate limit exceeded"

    def poll(self, job):
        if job.job_id == "agent_1" and not self.success_done:
            self.success_done = True
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "bridge success before 429"},
                transcript_events=[{"type": "assistant", "text": "bridge success before 429 transcript"}],
            )
        if job.job_id == "agent_2" and self.success_done:
            return AgentResult(
                job_id=job.job_id,
                status="failed",
                payload={"error": self.error, "statusCode": 429, "category": "rate_limit"},
                transcript_events=[{"type": "error", "error": self.error, "statusCode": 429}],
            )
        return None


class StopThenResumeRunner:
    def __init__(self):
        self.started_job_ids = []
        self.cancelled_job_ids = []
        self.first_completed = False
        self.second_started = threading.Event()
        self.second_cancelled_once = False

    def start(self, job):
        self.started_job_ids.append(job.job_id)
        if job.job_id == "agent_2":
            self.second_started.set()

    def poll(self, job):
        if job.job_id == "agent_1" and not self.first_completed:
            self.first_completed = True
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "prefix cached by resume"},
                transcript_events=[{"type": "assistant", "text": "prefix transcript"}],
            )
        if job.job_id == "agent_2" and self.second_cancelled_once:
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "fresh after stop"},
                transcript_events=[{"type": "assistant", "text": "fresh transcript"}],
            )
        return None

    def cancel(self, job):
        self.cancelled_job_ids.append(job.job_id)
        if job.job_id == "agent_2":
            self.second_cancelled_once = True


class PlannedRunFakeRuntime:
    started_run_ids = []

    def __init__(self, *, store, timeout_seconds=10.0):
        self.store = store
        self.timeout_seconds = timeout_seconds

    def run(self, run, *, args=None, resume_from_run_id=None):
        from workflow_models import WorkflowEvent

        self.__class__.started_run_ids.append(run.run_id)
        self.store.append_event(
            run,
            WorkflowEvent(
                run_id=run.run_id,
                session_id=run.session_id,
                event_type="workflow_log",
                sequence=max((event.sequence for event in self.store.replay_events(run.run_id)), default=0) + 1,
                payload={"message": "planned runtime saw args", "args": args},
            ),
        )
        payload = {"runId": run.run_id, "status": "succeeded", "result": {"ok": True, "args": args}}
        run.jobs.append(
            WorkflowJob(
                job_id="agent_1",
                prompt="planned runtime fake agent",
                status="succeeded",
                phase="Plan",
                metadata={"label": "planner"},
            )
        )
        run.status = "succeeded"
        self.store.write_final_result(run, payload)
        self.store.save_run(run)
        self.store.write_workflow_progress(run)
        return type("RuntimeResult", (), {"run": run, "result": payload["result"]})()


def make_workflow_plan_draft(task_text="计划任务", *, ok=True, planner_mode="prompt_guided", task_type="planning"):
    plan = {
        "taskType": task_type,
        "meta": {"name": "planned-bridge-demo", "description": "Bridge planned workflow demo"},
        "phases": [
            {
                "title": "Plan",
                "agents": [
                    {
                        "label": "planner",
                        "prompt": "不要读取 mykey.py、mykey.json、mcp.json；不要提交。制定计划。",
                        "dependsOn": [],
                    }
                ],
            }
        ],
        "schemas": {},
        "artifacts": ["plan"],
        "constraints": ["no_secret_files", "no_git_commit"],
    }
    return WorkflowDraft(
        task_text=task_text,
        context={"plannerMode": planner_mode},
        classification={"taskType": task_type},
        plan=plan,
        validation={"ok": ok, "issues": [] if ok else [{"code": "missing_phase"}], "mode": "rejected" if not ok else planner_mode},
        script="export const meta = { name: 'planned-bridge-demo', description: 'Bridge planned workflow demo' }\nphase('Plan')\nreturn await agent('不要读取 mykey.py、mykey.json、mcp.json；不要提交。制定计划。', { label: 'planner', phase: 'Plan' })" if ok else "",
    )


class FakeWorkflowPlanner:
    def __init__(self, draft):
        self.draft = draft
        self.calls = []

    def plan(self, task_text, context=None):
        self.calls.append((task_text, copy.deepcopy(context or {})))
        return self.draft


class FakeAgent:
    def __init__(self):
        self.inc_out = False
        self.verbose = True
        self.is_running = False
        self.aborted = False
        self.prompts = []
        self.queues = []
        self.history = []
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.llm_no = 0

    def run(self):
        return None

    def put_task(self, text, source="user", images=None):
        self.prompts.append((text, source, list(images or [])))
        self.history.append(f"[USER]: {text}")
        self.llmclient.backend.history.append({"role": "user", "content": text})
        dq = queue.Queue()
        self.queues.append(dq)
        return dq

    def abort(self):
        self.aborted = True

    def list_llms(self):
        return [
            (0, "NativeOAISession/gpt-native", self.llm_no == 0),
            (1, "NativeOAISession/kimi-native", self.llm_no == 1),
        ]

    def select_llm(self, selector):
        if str(selector).lower() in {"1", "kimi"}:
            self.llm_no = 1
            return {"ok": True, "index": 1, "name": "NativeOAISession/kimi-native", "model": "moonshotai/kimi-k2.6"}
        return {"ok": False, "code": "not_found", "message": "model not found"}


class InkBridgeTest(unittest.TestCase):
    def test_encode_event_writes_compact_json_line(self):
        line = encode_event({"type": "assistant_delta", "text": "你好\nworld"})

        self.assertEqual({"type": "assistant_delta", "text": "你好\nworld"}, json.loads(line))
        self.assertTrue(line.endswith("\n"))

    def test_encode_event_escapes_non_ascii_for_pipe_transport(self):
        line = encode_event({"type": "assistant_delta", "text": "公益token暂停"})

        self.assertNotIn("公益", line)
        self.assertIn("\\u", line)
        self.assertEqual({"type": "assistant_delta", "text": "公益token暂停"}, json.loads(line))

    def test_submit_emits_user_and_stream_events(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        task_id = bridge.submit("hello")
        agent.queues[0].put({"next": "he"})
        agent.queues[0].put({"next": "llo"})
        agent.queues[0].put({"done": "hello"})
        bridge.wait_for_idle(timeout=1)

        self.assertEqual(1, task_id)
        self.assertTrue(agent.inc_out)
        self.assertEqual([("hello", "user", [])], agent.prompts)
        self.assertEqual(
            [
                {"type": "user", "taskId": 1, "text": "hello"},
                {"type": "status", "status": "running", "taskId": 1},
                {"type": "assistant_delta", "taskId": 1, "text": "he"},
                {"type": "assistant_delta", "taskId": 1, "text": "llo"},
                {"type": "assistant_done", "taskId": 1, "text": "hello"},
                {"type": "status", "status": "idle", "taskId": 1},
            ],
            events,
        )

    def test_submit_forwards_images_to_put_task(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        images = [{"path": r"D:\shots\a.png", "placeholder": "[Image #1]", "source": "clipboard"}]

        task_id = bridge.submit("看图 [Image #1]", images=images)
        agent.queues[0].put({"done": "ok"})
        bridge.wait_for_idle(timeout=1)

        self.assertEqual(1, task_id)
        self.assertEqual([("看图 [Image #1]", "user", images)], agent.prompts)

    def test_submit_emits_token_usage_when_backend_usage_changes(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("hello")
        agent.llmclient.backend.last_usage_tokens = {
            "input_tokens": 11,
            "output_tokens": 17,
            "total_tokens": 28,
        }
        agent.queues[0].put({"done": "hello"})
        bridge.wait_for_idle(timeout=1)

        self.assertIn(
            {"type": "token_usage", "taskId": 1, "inputTokens": 11, "outputTokens": 17, "totalTokens": 28},
            events,
        )

    def test_busy_submit_is_rejected_without_calling_agent(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("first")
        result = bridge.submit("second")

        self.assertEqual(-1, result)
        self.assertEqual([("first", "user", [])], agent.prompts)
        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_new_session_replaces_agent_and_resets_visible_state(self):
        agents = [FakeAgent(), FakeAgent()]
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agents.pop(0), emit=events.append)
        old_agent = bridge.agent

        bridge.submit("first")
        old_agent.queues[0].put({"done": "first reply"})
        bridge.wait_for_idle(timeout=1)

        event_count = len(events)
        bridge.new_session()
        new_session_events = events[event_count:]
        task_id = bridge.submit("second")
        bridge.agent.queues[0].put({"done": "second reply"})
        bridge.wait_for_idle(timeout=1)

        self.assertIsNot(old_agent, bridge.agent)
        self.assertTrue(old_agent.aborted)
        self.assertEqual(1, task_id)
        self.assertEqual([], old_agent.prompts[1:])
        self.assertEqual([("second", "user", [])], bridge.agent.prompts)
        self.assertEqual([
            {"type": "history_replace", "messages": []},
            {"type": "system", "text": "Started a new session."},
            {"type": "status", "status": "idle"},
        ], new_session_events)

    def test_new_session_is_rejected_while_agent_is_running(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("busy")
        bridge.new_session()

        self.assertIs(agent, bridge.agent)
        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_stop_aborts_running_agent(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("first")
        bridge.stop()

        self.assertTrue(agent.aborted)
        self.assertEqual({"type": "status", "status": "stopping"}, events[-1])

    def test_list_resume_sessions_emits_picker_options(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with patch("ink_bridge.continue_list", return_value=[("session-a.txt", 1000.0, "first prompt", 2)]):
            bridge.list_resume_sessions()

        self.assertEqual(
            {
                "type": "resume_sessions",
                "sessions": [
                    {
                        "id": "session-a.txt",
                        "mtime": 1000.0,
                        "preview": "first prompt",
                        "rounds": 2,
                    }
                ],
            },
            events[-1],
        )

    def test_list_resume_sessions_excludes_current_log_and_session(self):
        agent = FakeAgent()
        agent.log_path = "current-log.txt"
        agent.session_id = "session_current"
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with patch("ink_bridge.continue_list", return_value=[]) as list_sessions:
            bridge.list_resume_sessions()

        list_sessions.assert_called_once_with(
            exclude_pid=os.getpid(),
            exclude_path="current-log.txt",
            exclude_session_id="session_current",
        )

    def test_resume_session_by_index_excludes_current_log_and_session(self):
        agent = FakeAgent()
        agent.log_path = "current-log.txt"
        agent.session_id = "session_current"
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.continue_list", return_value=[("session-a.txt", 1000.0, "first prompt", 2)]) as list_sessions,
            patch.object(bridge, "resume_session") as resume_session,
        ):
            bridge.resume_session_by_index(1)

        list_sessions.assert_called_once_with(
            exclude_pid=os.getpid(),
            exclude_path="current-log.txt",
            exclude_session_id="session_current",
        )
        resume_session.assert_called_once_with("session-a.txt")

    def test_resume_session_replaces_history_messages(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.continue_reset") as reset,
            patch("ink_bridge.continue_restore", side_effect=lambda agent, _path: (
                setattr(agent.llmclient.backend, "history", [
                    {"role": "user", "content": "old q"},
                    {"role": "assistant", "content": "old a"},
                ]) or ("✅ 已恢复 1 轮完整对话", True)
            )) as restore,
            patch("ink_bridge.continue_extract", return_value=[
                {"role": "user", "content": "old q"},
                {"role": "assistant", "content": "old a"},
            ]),
        ):
            bridge.resume_session("session-a.txt")

        reset.assert_called_once_with(agent, message=None)
        restore.assert_called_once_with(agent, "session-a.txt")
        self.assertIn(
            {
                "type": "history_replace",
                "messages": [
                    {"role": "user", "text": "old q", "taskId": 1},
                    {"role": "assistant", "text": "old a", "taskId": 1},
                ],
            },
            events,
        )
        bridge.rewind(1)
        self.assertEqual([], agent.llmclient.backend.history)
        self.assertEqual({"type": "rewind_done", "taskId": 1, "text": "old q"}, events[-1])

    def test_resume_transcript_uses_exact_backend_history_before_each_turn_for_rewind(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            first_after = [
                {"role": "system", "content": "seed"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "a1"},
            ]
            second_after = first_after + [
                {"role": "tool", "content": "extra"},
                {"role": "user", "content": "two"},
                {"role": "assistant", "content": "a2"},
            ]
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="one",
                assistant_text="a1",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="two",
                assistant_text="a2",
                backend_history_before=first_after,
                backend_history_after=second_after,
            )
            agent = FakeAgent()
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            with patch("ink_bridge.continue_reset", lambda *_args, **_kwargs: None):
                bridge.resume_session(str(transcript))

            self.assertEqual([], bridge._rewind_snapshots[1]["backend_history"])
            self.assertEqual(first_after, bridge._rewind_snapshots[2]["backend_history"])

    def test_resume_transcript_replaces_ui_with_full_conversation_from_start(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="高斯分布是什么",
                assistant_text="高斯分布回答",
                backend_history_before=[],
                backend_history_after=[{"role": "user", "content": "高斯分布是什么"}],
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="高斯生平",
                assistant_text="高斯生平回答",
                backend_history_before=[{"role": "user", "content": "高斯分布是什么"}],
                backend_history_after=[{"role": "user", "content": "高斯生平"}],
            )
            agent = FakeAgent()
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            with patch("ink_bridge.continue_reset", lambda *_args, **_kwargs: None):
                bridge.resume_session(str(transcript))

            history_replace = next(event for event in events if event["type"] == "history_replace")
            self.assertEqual(
                [
                    {"role": "user", "text": "高斯分布是什么", "taskId": 1},
                    {"role": "assistant", "text": "高斯分布回答", "taskId": 1},
                    {"role": "user", "text": "高斯生平", "taskId": 2},
                    {"role": "assistant", "text": "高斯生平回答", "taskId": 2},
                ],
                history_replace["messages"],
            )

    def test_resume_transcript_switches_active_session_and_next_turn_appends_to_same_file(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_old")
            first_after = [{"role": "user", "content": "old question"}]
            session_transcript.record_turn(
                transcript,
                session_id="session_old",
                turn_id=1,
                source="user",
                user_text="old question",
                assistant_text="old answer",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            agent = FakeAgent()
            agent.session_id = "session_new"
            agent.session_path = str(Path(tmp) / "session_new.jsonl")
            agent.session_turn_id = 0
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            with patch("ink_bridge.continue_reset", lambda *_args, **_kwargs: None):
                bridge.resume_session(str(transcript))
            agent.llmclient.backend.history = first_after + [{"role": "user", "content": "follow up"}]
            session_transcript.record_agent_turn(
                agent,
                user_text="follow up",
                assistant_text="follow answer",
                source="user",
                backend_history_before=first_after,
            )

            loaded = session_transcript.load_session(transcript)
            self.assertEqual("session_old", agent.session_id)
            self.assertEqual(str(transcript), agent.session_path)
            self.assertEqual(2, agent.session_turn_id)
            self.assertEqual(["old question", "follow up"], [turn.user_text for turn in loaded.turns])
            self.assertFalse(Path(agent.session_path).with_name("session_new.jsonl").exists())

    def test_rewind_restores_checkpoint_before_selected_task(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        agent.history = ["before"]
        agent.llmclient.backend.history = [{"role": "user", "content": "before"}]
        bridge.submit("first")
        agent.queues[0].put({"done": "first done"})
        bridge.wait_for_idle(timeout=1)
        first_history = copy.deepcopy(agent.llmclient.backend.history)
        bridge.submit("second")
        agent.queues[1].put({"done": "second done"})
        bridge.wait_for_idle(timeout=1)

        bridge.rewind(2)

        self.assertEqual(["before", "[USER]: first"], agent.history)
        self.assertEqual(first_history, agent.llmclient.backend.history)
        self.assertIsNone(agent.handler)
        self.assertEqual({"type": "rewind_done", "taskId": 2, "text": "second"}, events[-1])

    def test_rewind_records_transcript_branch_boundary_and_resets_next_turn_id(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            first_after = [
                {"role": "user", "content": "1+1= ?"},
                {"role": "assistant", "content": "2"},
            ]
            second_after = first_after + [
                {"role": "user", "content": "1-2= ?"},
                {"role": "assistant", "content": "-1"},
            ]
            agent = FakeAgent()
            agent.session_id = "session_test"
            agent.session_path = str(transcript)
            agent.session_turn_id = 2
            agent.llmclient.backend.history = copy.deepcopy(second_after)
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
            bridge._task_seq = 2
            bridge._rewind_snapshots = {
                1: {"text": "1+1= ?", "history": [], "backend_history": [], "last_tools": ""},
                2: {"text": "1-2= ?", "history": [], "backend_history": first_after, "last_tools": ""},
            }
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="1+1= ?",
                assistant_text="2",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="1-2= ?",
                assistant_text="-1",
                backend_history_before=first_after,
                backend_history_after=second_after,
            )

            bridge.rewind(1)
            agent.llmclient.backend.history = [
                {"role": "user", "content": "1+10= ?"},
                {"role": "assistant", "content": "11"},
            ]
            session_transcript.record_agent_turn(
                agent,
                user_text="1+10= ?",
                assistant_text="11",
                source="user",
                backend_history_before=[],
            )

            loaded = session_transcript.load_session(transcript)
            raw_types = [
                json.loads(line)["type"]
                for line in Path(transcript).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual({"type": "rewind_done", "taskId": 1, "text": "1+1= ?"}, events[-1])
            self.assertEqual(1, agent.session_turn_id)
            self.assertEqual(["session_start", "turn", "turn", "rewind", "turn"], raw_types)
            self.assertEqual(["1+10= ?"], [turn.user_text for turn in loaded.turns])

    def test_emit_mcp_status(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        payload = {"config_path": "mcp.json", "servers": [], "tools": [], "errors": {}}

        with patch("mcp_runtime.mcp_status", return_value=payload):
            bridge.mcp_status()

        self.assertEqual({"type": "mcp_status", **payload}, events[-1])

    def test_emit_mcp_action_result_then_status(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        payload = {"config_path": "mcp.json", "servers": [], "tools": [], "errors": {}}

        with (
            patch("mcp_runtime.reconnect_mcp_server", return_value={"server": {"name": "demo", "status": "connected"}}),
            patch("mcp_runtime.mcp_status", return_value=payload),
        ):
            bridge.mcp_reconnect("demo")

        self.assertEqual({"type": "system", "text": "MCP server demo reconnect: connected"}, events[-2])
        self.assertEqual({"type": "mcp_status", **payload}, events[-1])

    def test_model_status_emits_available_models(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.model_status()

        self.assertEqual(
            {
                "type": "model_status",
                "models": [
                    {"index": 0, "name": "NativeOAISession/gpt-native", "current": True},
                    {"index": 1, "name": "NativeOAISession/kimi-native", "current": False},
                ],
            },
            events[-1],
        )

    def test_model_switch_uses_agent_selector_then_emits_status(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.model_switch("kimi")

        self.assertEqual(1, agent.llm_no)
        self.assertEqual({"type": "model_switch_result", "ok": True, "message": "Set model to NativeOAISession/kimi-native"}, events[-2])
        self.assertEqual("model_status", events[-1]["type"])
        self.assertTrue(events[-1]["models"][1]["current"])

    def test_model_switch_rejects_while_busy(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        bridge.submit("busy")

        bridge.model_switch("kimi")

        self.assertEqual(0, agent.llm_no)
        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_compact_replaces_backend_history_and_clears_rewind_checkpoints(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        agent.llmclient.backend.history = [
            {"role": "user", "content": [{"type": "text", "text": "old context"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
        ]
        bridge._rewind_snapshots[1] = {"backend_history": [{"role": "user", "content": "old"}]}

        with patch("ink_bridge.compact_agent_context") as compact:
            compact.return_value.ok = True
            compact.return_value.summary = "summary text"
            compact.return_value.original_messages = 2
            compact.return_value.compacted_messages = 2
            compact.return_value.message = "Compacted 2 messages into summary context."
            bridge.compact("keep file names")

        compact.assert_called_once()
        self.assertEqual({}, bridge._rewind_snapshots)
        self.assertEqual({"type": "status", "status": "running"}, events[-5])
        self.assertEqual({"type": "activity", "label": "Compacting conversation"}, events[-4])
        self.assertEqual(
            {
                "type": "history_replace",
                "messages": [
                    {"role": "system", "text": "Compacted 2 messages into summary context."},
                ],
            },
            events[-3],
        )
        self.assertNotIn("local_command_output", {e.get("type") for e in events})
        self.assertEqual({"type": "activity", "label": None}, events[-2])
        self.assertEqual({"type": "status", "status": "idle"}, events[-1])

    def test_compact_records_transcript_compact_event(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            agent = FakeAgent()
            agent.session_id = "session_test"
            agent.session_path = str(transcript)
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
            compacted = [{"role": "user", "content": "summary"}]

            with patch("ink_bridge.compact_agent_context") as compact:
                compact.return_value.ok = True
                compact.return_value.summary = "summary text"
                compact.return_value.original_messages = 4
                compact.return_value.compacted_messages = 1
                compact.return_value.message = "Compacted 4 messages into summary context."
                agent.llmclient.backend.history = compacted
                bridge.compact("keep important details")

            loaded = session_transcript.load_session(transcript)
            self.assertEqual(compacted, loaded.backend_history)

    def test_compact_rejects_while_busy(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        bridge.submit("busy")

        bridge.compact("")

        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_manual_compact_failure_is_reported_as_local_command_output(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with patch("ink_bridge.compact_agent_context") as compact:
            compact.return_value.ok = False
            compact.return_value.message = "No conversation history to compact."
            bridge.compact("")

        self.assertEqual({"type": "status", "status": "running"}, events[-5])
        self.assertEqual({"type": "activity", "label": "Compacting conversation"}, events[-4])
        self.assertEqual(
            {"type": "local_command_output", "text": "Compact failed: No conversation history to compact."},
            events[-3],
        )
        self.assertEqual({"type": "activity", "label": None}, events[-2])
        self.assertEqual({"type": "status", "status": "idle"}, events[-1])

    def test_submit_stops_when_required_auto_compact_fails(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.should_auto_compact_agent", return_value=True),
            patch("ink_bridge.compact_agent_context") as compact,
        ):
            compact.return_value.ok = False
            compact.return_value.message = "summary failed"
            result = bridge.submit("large prompt")

        self.assertEqual(-1, result)
        self.assertEqual([], agent.prompts)
        self.assertEqual({"type": "error", "code": "auto_compact_failed", "message": "summary failed"}, events[-1])

    def test_auto_compact_circuit_breaker_disables_after_consecutive_failures(self):
        # 连续失败达上限后，熔断器停用自动压缩：不再调 compact_agent_context，
        # 并放行请求（返回非 -1），让用户仍能继续对话（硬裁剪安全网仍在 llmcore 层）。
        from ink_bridge import MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES

        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.should_auto_compact_agent", return_value=True),
            patch("ink_bridge.compact_agent_context") as compact,
        ):
            compact.return_value.ok = False
            compact.return_value.message = "summary failed"
            # 前 N 次失败：每次都尝试压缩、返回 -1（拦住请求）。
            for _ in range(MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES):
                self.assertEqual(-1, bridge.submit("large prompt"))
            self.assertEqual(MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES, compact.call_count)

            # 熔断后：不再调用 compact，且放行请求（不再是 -1）。
            result = bridge.submit("large prompt")
            self.assertNotEqual(-1, result)
            self.assertEqual(MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES, compact.call_count)

        bridge.wait_for_idle(timeout=1)

    def test_auto_compact_success_resets_failure_counter(self):
        # 一次成功压缩把连续失败计数清零，避免历史失败累积误触发熔断。
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.should_auto_compact_agent", return_value=True),
            patch("ink_bridge.compact_agent_context") as compact,
        ):
            compact.return_value.ok = False
            compact.return_value.message = "summary failed"
            self.assertEqual(-1, bridge.submit("p"))
            self.assertEqual(1, bridge._auto_compact_failures)

            compact.return_value.ok = True
            compact.return_value.message = "Compacted."
            bridge.submit("p2")
            self.assertEqual(0, bridge._auto_compact_failures)

        bridge.wait_for_idle(timeout=1)

    def test_submit_auto_compact_replaces_visible_history_before_new_user_message(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.should_auto_compact_agent", return_value=True),
            patch("ink_bridge.compact_agent_context") as compact,
        ):
            compact.return_value.ok = True
            compact.return_value.message = "Compacted 8 messages into summary context."
            result = bridge.submit("follow up")

        bridge.wait_for_idle(timeout=1)

        self.assertEqual(1, result)
        history_replace_index = next(index for index, event in enumerate(events) if event["type"] == "history_replace")
        user_index = next(index for index, event in enumerate(events) if event["type"] == "user")
        self.assertLess(history_replace_index, user_index)
        self.assertEqual(
            {
                "type": "history_replace",
                "messages": [
                    {"role": "system", "text": "Auto Compacted 8 messages into summary context."},
                ],
            },
            events[history_replace_index],
        )

    def test_skill_status_emits_discovered_skill_metadata(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: demo\n"
                "description: Demo skill\n"
                "---\n"
                "# Demo\n",
                encoding="utf-8",
            )

            bridge.skill_status(search_roots=[str(Path(tmp) / "skills")])

        self.assertEqual("skill_status", events[-1]["type"])
        self.assertEqual(
            [
                {
                    "name": "demo",
                    "description": "Demo skill",
                    "source": "local",
                    "path": str((skill_dir / "SKILL.md").resolve(strict=False)),
                }
            ],
            events[-1]["skills"],
        )

    def test_skill_invoke_loads_skill_name_and_submits_args_as_request(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: demo\n"
                "description: Demo skill\n"
                "---\n"
                "Use $ARGUMENTS from ${GA_SKILL_DIR}.",
                encoding="utf-8",
            )

            bridge.skill_invoke("demo", "中文 args with spaces", search_roots=[str(Path(tmp) / "skills")])

        self.assertEqual(1, len(agent.prompts))
        prompt, source, images = agent.prompts[0]
        self.assertEqual("user", source)
        self.assertIn('The user invoked skill "demo"', prompt)
        self.assertIn("Use 中文 args with spaces from", prompt)
        self.assertIn("<arguments>\n中文 args with spaces\n</arguments>", prompt)
        self.assertEqual("user", events[0]["type"])
        self.assertEqual("/demo 中文 args with spaces", events[0]["text"])
        self.assertNotIn("<skill>", events[0]["text"])
        self.assertEqual("status", events[1]["type"])


    def test_workflow_plan_creates_auto_approved_run_and_starts_runtime(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        PlannedRunFakeRuntime.started_run_ids = []
        draft = make_workflow_plan_draft(task_text="规划 bridge workflow", task_type="planning")
        planner = FakeWorkflowPlanner(draft)
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: PlannedRunFakeRuntime(**kwargs),
                workflow_planner_factory=lambda: planner,
            )

            run_id = bridge.workflow_plan(
                "规划 bridge workflow",
                context={"source": "test"},
                auto_approve=True,
                args={"value": 42},
                timeout_seconds=2,
            )
            bridge.wait_for_workflow_idle(run_id, timeout=2)

            self.assertTrue(run_id.startswith("wf_"))
            self.assertEqual([("规划 bridge workflow", {"source": "test"})], planner.calls)
            self.assertEqual([run_id], PlannedRunFakeRuntime.started_run_ids)
            run_events = [event for event in events if event["type"] == "workflow_run"]
            self.assertGreaterEqual(len(run_events), 2)
            self.assertEqual("running", run_events[0]["run"]["status"])
            self.assertEqual("workflow-draft.json", run_events[0]["run"]["metadata"]["workflowDraftRef"])
            self.assertEqual("prompt_guided", run_events[0]["run"]["metadata"]["plannerMode"])
            self.assertEqual("planning", run_events[0]["run"]["metadata"]["workflowTaskType"])
            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("workflow_planned", workflow_events)
            self.assertIn("workflow_started", workflow_events)
            self.assertIn("workflow_log", workflow_events)
            final_event = next(event for event in events if event["type"] == "workflow_final")
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assertEqual({"ok": True, "args": {"value": 42}}, final_event["result"]["result"])
            progress_events = [event for event in events if event["type"] == "workflow_progress"]
            self.assertTrue(progress_events)
            self.assertEqual(run_id, progress_events[-1]["progress"]["runId"])
            self.assertEqual("succeeded", progress_events[-1]["progress"]["status"])
            event_types = [event["type"] for event in events]
            self.assertLess(event_types.index("workflow_run", event_types.index("workflow_event")), event_types.index("workflow_progress"))
            self.assertLess(event_types.index("workflow_progress"), event_types.index("workflow_final"))

    def test_workflow_plan_can_create_awaiting_approval_run_when_auto_approve_false(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        PlannedRunFakeRuntime.started_run_ids = []
        planner = FakeWorkflowPlanner(make_workflow_plan_draft(task_text="规划但等待审批"))
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: PlannedRunFakeRuntime(**kwargs),
                workflow_planner_factory=lambda: planner,
            )

            run_id = bridge.workflow_plan("规划但等待审批", auto_approve=False)

            self.assertTrue(run_id.startswith("wf_"))
            self.assertEqual([], PlannedRunFakeRuntime.started_run_ids)
            run_event = next(event for event in events if event["type"] == "workflow_run")
            self.assertEqual("awaiting_approval", run_event["run"]["status"])
            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertEqual(["workflow_planned", "workflow_approval_requested"], workflow_events)

    def test_workflow_plan_emits_rejected_plan_without_runtime(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        PlannedRunFakeRuntime.started_run_ids = []
        planner = FakeWorkflowPlanner(make_workflow_plan_draft(task_text="坏计划", ok=False, planner_mode="prompt_guided_rejected"))
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: PlannedRunFakeRuntime(**kwargs),
                workflow_planner_factory=lambda: planner,
            )

            run_id = bridge.workflow_plan("坏计划")

            self.assertTrue(run_id.startswith("wf_"))
            self.assertEqual([], PlannedRunFakeRuntime.started_run_ids)
            run_event = next(event for event in events if event["type"] == "workflow_run")
            self.assertEqual("failed", run_event["run"]["status"])
            self.assertEqual("workflow_plan_rejected", run_event["run"]["error"])
            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertEqual(["workflow_planned", "workflow_plan_rejected"], workflow_events)

    def test_workflow_detail_includes_workflow_progress_and_draft_when_available(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        draft = make_workflow_plan_draft(task_text="规划 detail 数据契约")
        planner = FakeWorkflowPlanner(draft)
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_planner_factory=lambda: planner,
            )
            run_id = bridge.workflow_plan("规划 detail 数据契约", auto_approve=False)
            run = bridge.workflow_store.load_run(run_id)
            run.jobs.append(
                WorkflowJob(
                    job_id="agent_1",
                    prompt="不要读取 mykey.py、mykey.json、mcp.json；不要提交。制定计划。",
                    status="succeeded",
                    phase="Plan",
                    metadata={"label": "planner"},
                )
            )
            bridge.workflow_store.save_run(run)
            bridge.workflow_store.write_workflow_progress(run)
            events.clear()

            bridge.workflow_detail(run_id)

            detail = next(event for event in events if event["type"] == "workflow_detail")
            self.assertEqual("workflow-draft.json", detail["run"]["metadata"]["workflowDraftRef"])
            self.assertEqual("规划 detail 数据契约", detail["draft"]["taskText"])
            self.assertEqual("planned-bridge-demo", detail["draft"]["plan"]["meta"]["name"])
            self.assertTrue(detail["draft"]["validation"]["ok"])
            self.assertEqual(run_id, detail["progress"]["runId"])
            self.assertEqual("session_workflow", detail["progress"]["sessionId"])
            self.assertEqual("planner", detail["progress"]["workflowProgress"][0]["label"])
            self.assertEqual("Plan", detail["progress"]["workflowProgress"][0]["phaseTitle"])
            self.assertNotIn("transcriptEvents", json.dumps(detail, ensure_ascii=False))

    def test_workflow_progress_emits_current_progress_without_detail_payload(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        draft = make_workflow_plan_draft(task_text="规划 live progress")
        planner = FakeWorkflowPlanner(draft)
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_planner_factory=lambda: planner,
            )
            run_id = bridge.workflow_plan("规划 live progress", auto_approve=False)
            run = bridge.workflow_store.load_run(run_id)
            run.status = "running"
            run.jobs.append(
                WorkflowJob(
                    job_id="agent_1",
                    prompt="不要读取 mykey.py、mykey.json、mcp.json；不要提交。制定计划。",
                    status="running",
                    phase="Plan",
                    metadata={"label": "planner", "tokenUsage": {"totalTokens": 1234}},
                )
            )
            bridge.workflow_store.save_run(run)
            bridge.workflow_store.write_workflow_progress(run)
            events.clear()

            bridge.workflow_progress(run_id)

            progress_event = next(event for event in events if event["type"] == "workflow_progress")
            self.assertEqual(run_id, progress_event["progress"]["runId"])
            self.assertEqual("running", progress_event["progress"]["status"])
            self.assertEqual("planner", progress_event["progress"]["workflowProgress"][0]["label"])
            self.assertEqual(1234, progress_event["progress"]["workflowProgress"][0]["tokenUsage"]["totalTokens"])
            self.assertNotIn("script", progress_event)
            self.assertNotIn("transcriptEvents", json.dumps(progress_event, ensure_ascii=False))

    def test_workflow_detail_allows_missing_workflow_progress_and_draft(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 1")
            events.clear()

            bridge.workflow_detail(run_id)

            detail = next(event for event in events if event["type"] == "workflow_detail")
            self.assertEqual(run_id, detail["run"]["runId"])
            self.assertIsNone(detail["draft"])
            self.assertIsNone(detail["progress"])
            self.assertEqual("return 1", detail["script"])

    def test_workflow_draft_creates_run_and_emits_approval_event(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)

            run_id = bridge.workflow_draft('export const meta = { name: "demo" }\nreturn { ok: true }')

            self.assertTrue(run_id.startswith("wf_"))
            self.assertEqual("workflow_draft", events[-2]["type"])
            self.assertEqual(run_id, events[-2]["run"]["runId"])
            self.assertEqual("awaiting_approval", events[-2]["run"]["status"])
            self.assertEqual("workflow_event", events[-1]["type"])
            self.assertEqual("workflow_approval_requested", events[-1]["event"]["type"])
            self.assertEqual(run_id, events[-1]["event"]["runId"])

    def test_bridge_emit_sanitizes_events_before_raw_emitter(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.emit({
            "type": "probe",
            "apiKey": "bridge-api-secret",
            "nested": {"Authorization": "Bearer bridge-bearer-secret"},
            "text": "token=bridge-token-secret request_id=req_123",
        })

        serialized = json.dumps(events[-1], ensure_ascii=False)
        self.assertIn("request_id=req_123", serialized)
        self.assertIn("[REDACTED]", serialized)
        for secret in ["bridge-api-secret", "bridge-bearer-secret", "bridge-token-secret"]:
            self.assertNotIn(secret, serialized)

    def test_workflow_detail_redacts_script_and_event_payloads(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 'Bearer bridge-script-secret request_id=req_123'")
            from workflow_models import WorkflowEvent
            run = bridge.workflow_store.load_run(run_id)
            bridge.workflow_store.append_event(
                run,
                WorkflowEvent(
                    run_id=run_id,
                    session_id=run.session_id,
                    event_type="workflow_log",
                    sequence=99,
                    payload={"apiKey": "bridge-event-secret", "message": "x-api-key: bridge-xkey-secret request_id=req_123"},
                ),
            )

            bridge.workflow_detail(run_id)

            detail = next(event for event in events if event["type"] == "workflow_detail")
            serialized = json.dumps(detail, ensure_ascii=False)
            self.assertIn("request_id=req_123", serialized)
            self.assertIn("[REDACTED]", serialized)
            for secret in ["bridge-script-secret", "bridge-event-secret", "bridge-xkey-secret"]:
                self.assertNotIn(secret, serialized)

    def test_workflow_run_event_final_payloads_are_sanitized(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class SecretRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                from workflow_models import WorkflowEvent
                self.store.append_event(
                    run,
                    WorkflowEvent(
                        run_id=run.run_id,
                        session_id=run.session_id,
                        event_type="workflow_log",
                        sequence=99,
                        payload={"message": "Authorization: Bearer bridge-event-secret request_id=req_123"},
                    ),
                )
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"apiKey": "bridge-final-secret", "message": "Cookie: sid=bridge-cookie-secret request_id=req_123"}}
                self.store.write_final_result(run, payload)
                run.status = "succeeded"
                run.error = "Bearer bridge-run-secret"
                self.store.save_run(run)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: SecretRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return { ok: true }")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            serialized = json.dumps(events, ensure_ascii=False)
            self.assertIn("request_id=req_123", serialized)
            self.assertIn("[REDACTED]", serialized)
            for secret in ["bridge-event-secret", "bridge-final-secret", "bridge-cookie-secret", "bridge-run-secret"]:
                self.assertNotIn(secret, serialized)

    def test_workflow_approve_runs_runtime_and_emits_final_result(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class FakeRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store
                self.timeout_seconds = timeout_seconds

            def run(self, run, *, args=None, resume_from_run_id=None):
                from workflow_models import WorkflowEvent
                self.store.append_event(run, WorkflowEvent(run_id=run.run_id, session_id=run.session_id, event_type="workflow_log", sequence=99, payload={"message": "runtime saw args", "args": args, "resumeFromRunId": resume_from_run_id}))
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"ok": True, "args": args}}
                self.store.write_final_result(run, payload)
                run.status = "succeeded"
                self.store.save_run(run)
                return type("RuntimeResult", (), {"run": run, "result": payload["result"]})()

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: FakeRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return { ok: true }")
            approved = bridge.workflow_approve(run_id, args={"value": 7}, timeout_seconds=3)
            bridge.wait_for_workflow_idle(run_id, timeout=1)

            self.assertTrue(approved)
            event_types = [event["type"] for event in events]
            self.assertIn("workflow_run", event_types)
            self.assertIn("workflow_final", event_types)
            final_event = next(event for event in events if event["type"] == "workflow_final")
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assertEqual({"ok": True, "args": {"value": 7}}, final_event["result"]["result"])
            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("workflow_started", workflow_events)
            self.assertIn("workflow_log", workflow_events)

    def test_workflow_approve_failed_terminal_run_emits_failed_final_fallback(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class FailingTerminalRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                run.status = "failed"
                run.error = "deterministic failure"
                run.result_ref = None
                self.store.save_run(run)
                return type("RuntimeResult", (), {"run": run, "result": None})()

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: FailingTerminalRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return { ok: false }")
            self.assertTrue(bridge.workflow_approve(run_id))
            bridge.wait_for_workflow_idle(run_id, timeout=1)

            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual(run_id, final_event["result"]["runId"])
            self.assertEqual("failed", final_event["result"]["status"])
            self.assertEqual("deterministic failure", final_event["result"]["error"])
            self.assertIsNone(final_event["result"]["resultRef"])
            self.assertEqual("missing_ref", final_event["result"]["artifactError"])

    def test_workflow_runtime_exception_emits_error_final_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class ExplodingRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                run.status = "failed"
                run.error = "runtime exploded"
                self.store.save_run(run)
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: ExplodingRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("throw new Error('boom')")
            self.assertTrue(bridge.workflow_approve(run_id))
            bridge.wait_for_workflow_idle(run_id, timeout=1)

            self.assertTrue(any(event["type"] == "workflow_run" and event["run"]["runId"] == run_id for event in events))
            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual(run_id, final_event["result"]["runId"])
            self.assertEqual("failed", final_event["result"]["status"])
            self.assertEqual("runtime exploded", final_event["result"]["error"])
            self.assertIsNone(final_event["result"]["resultRef"])
            self.assertEqual("missing_ref", final_event["result"]["artifactError"])
            error_event = next(event for event in events if event["type"] == "error" and event["code"] == "workflow_run_failed")
            self.assertEqual("boom", error_event["message"])
            self.assertEqual({"type": "activity", "label": None}, events[-2])
            self.assertEqual({"type": "status", "status": "idle"}, events[-1])

    def assert_workflow_failed_final_error_and_idle(
        self,
        *,
        bridge,
        events,
        run_id,
        marker,
        expect_workflow_failed_event=True,
    ):
        thread = bridge._workflow_threads[run_id]
        self.assertFalse(thread.is_alive())

        run = bridge.workflow_store.load_run(run_id)
        self.assertEqual("failed", run.status)
        self.assertIn(marker, run.error)

        final_path = Path(run.artifact_dir) / "final-result.json"
        self.assertTrue(final_path.exists())
        final_payload = json.loads(final_path.read_text(encoding="utf-8"))
        self.assertEqual("failed", final_payload["status"])
        self.assertIn(marker, final_payload["error"])

        terminal_run = [event for event in events if event["type"] == "workflow_run" and event["run"]["runId"] == run_id][-1]
        self.assertEqual("failed", terminal_run["run"]["status"])
        self.assertIn(marker, terminal_run["run"]["error"])

        final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
        self.assertEqual("failed", final_event["result"]["status"])
        self.assertIn(marker, final_event["result"]["error"])

        workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
        if expect_workflow_failed_event:
            self.assertIn("workflow_failed", workflow_events)

        error_event = next(event for event in events if event["type"] == "error" and event["code"] == "workflow_run_failed")
        self.assertIn(marker, error_event["message"])
        self.assert_bridge_idle_tail(events)

    def workflow_event_types_by_run(self, events):
        workflow_events_by_run = {}
        for event in events:
            if event.get("type") != "workflow_event":
                continue
            workflow_events_by_run.setdefault(event["event"]["runId"], []).append(event["event"]["type"])
        return workflow_events_by_run

    def assert_bridge_idle_tail(self, events):
        self.assertEqual({"type": "activity", "label": None}, events[-2])
        self.assertEqual({"type": "status", "status": "idle"}, events[-1])

    def test_workflow_approve_timeout_emits_failed_final_error_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return await new Promise(() => {})")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=0.2))
            bridge.wait_for_workflow_idle(run_id, timeout=3)

            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="deadline",
            )

    def test_workflow_approve_real_runtime_script_throw_emits_failed_final_error_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("throw new Error('GA_P8_TOP_LEVEL_THROW')")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="GA_P8_TOP_LEVEL_THROW",
            )

    def test_workflow_approve_real_runtime_pipeline_throw_emits_failed_final_error_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        script = """
const values = await pipeline([1, 2], value => {
  if (value === 2) {
    throw new Error('GA_P8_PIPELINE_STAGE_THROW')
  }
  return value
})
return values
"""

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="GA_P8_PIPELINE_STAGE_THROW",
            )

    def test_workflow_approve_forbidden_script_emits_failed_final_error_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return process.env")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="process",
            )

    def test_workflow_approve_real_runtime_parallel_thunk_throw_emits_failed_final_error_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        script = """
const results = await parallel([
  () => 1,
  () => { throw new Error('GA_P8_PARALLEL_THUNK_THROW') }
])
return results
"""

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="GA_P8_PARALLEL_THUNK_THROW",
            )

    def test_workflow_approve_real_runtime_non_serializable_return_emits_failed_final_error_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 1n")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="BigInt",
            )

    def test_workflow_approve_invalid_agent_options_emits_failed_final_error_and_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return await agent('p', 'bad-options')")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="agent options must be a plain object",
            )

    def test_workflow_final_and_detail_do_not_inline_large_child_transcripts(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class LargeTranscriptRunner:
            def __init__(self):
                self.started_job_ids = []

            def start(self, job):
                self.started_job_ids.append(job.job_id)

            def poll(self, job):
                marker = f"GA_BRIDGE_LARGE_TRANSCRIPT_{job.job_id}"
                transcript_events = [
                    {"type": "assistant", "index": index, "text": f"{marker}-event-{index:04d}"}
                    for index in range(1001)
                ]
                return AgentResult(
                    job_id=job.job_id,
                    status="succeeded",
                    payload={"summary": f"summary {job.job_id}"},
                    transcript_events=transcript_events,
                )

            def cancel(self, job):
                return None

        runner = LargeTranscriptRunner()

        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=2),
                timeout_seconds=timeout_seconds,
            )

        script = """
const results = await parallel([() => agent('large one'), () => agent('large two')])
return results.map(result => result.summary)
"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=runtime_factory,
            )
            run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=5.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            final_text = json.dumps(final_event, ensure_ascii=False)
            self.assertNotIn("transcriptEvents", final_text)
            self.assertNotIn("GA_BRIDGE_LARGE_TRANSCRIPT", final_text)

            bridge.workflow_detail(run_id)
            detail_event = [event for event in events if event["type"] == "workflow_detail" and event["run"]["runId"] == run_id][-1]
            detail_text = json.dumps(detail_event, ensure_ascii=False)
            self.assertNotIn("transcriptEvents", detail_text)
            self.assertNotIn("GA_BRIDGE_LARGE_TRANSCRIPT", detail_text)
            self.assertIn("resultRef", detail_text)
            self.assert_bridge_idle_tail(events[:-1])

    def test_workflow_approve_parallel_partial_failure_emits_failed_final_error_and_preserves_artifacts(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runner = PartialFailureRunner()

        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=4),
                timeout_seconds=timeout_seconds,
            )

        script = """
const results = await parallel([() => agent('bridge success'), () => agent('bridge failure')])
return results
"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=runtime_factory,
            )
            run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            run = bridge.workflow_store.load_run(run_id)
            artifact_dir = Path(run.artifact_dir)
            self.assertEqual(["agent_1", "agent_2"], runner.started_job_ids)
            self.assertEqual(["succeeded", "failed"], [job.status for job in run.jobs])

            success_result = json.loads((artifact_dir / run.jobs[0].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("succeeded", success_result["status"])
            self.assertEqual("bridge success artifact", success_result["payload"]["summary"])
            self.assertNotIn("transcriptEvents", success_result)
            self.assertTrue((artifact_dir / run.jobs[0].metadata["transcriptRef"]).exists())

            failed_result = json.loads((artifact_dir / run.jobs[1].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("failed", failed_result["status"])
            self.assertEqual("bridge partial failure", failed_result["payload"]["error"])
            self.assertNotIn("transcriptEvents", failed_result)
            self.assertTrue((artifact_dir / run.jobs[1].metadata["transcriptRef"]).exists())

            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("agent_completed", workflow_events)
            self.assertIn("agent_failed", workflow_events)
            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="bridge partial failure",
            )

    def test_workflow_approve_provider_429_rate_limit_emits_failed_final_error_and_preserves_artifacts(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runner = RateLimitRunner()

        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=4),
                timeout_seconds=timeout_seconds,
            )

        script = """
const results = await parallel([() => agent('bridge success before 429'), () => agent('bridge provider 429')])
return results
"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=runtime_factory,
            )
            run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            run = bridge.workflow_store.load_run(run_id)
            artifact_dir = Path(run.artifact_dir)
            self.assertIn("429", run.error)
            self.assertIn("rate limit", run.error.lower())
            self.assertEqual(["agent_1", "agent_2"], runner.started_job_ids)
            self.assertEqual(["succeeded", "failed"], [job.status for job in run.jobs])

            success_result = json.loads((artifact_dir / run.jobs[0].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("succeeded", success_result["status"])
            self.assertEqual("bridge success before 429", success_result["payload"]["summary"])
            self.assertNotIn("transcriptEvents", success_result)
            self.assertTrue((artifact_dir / run.jobs[0].metadata["transcriptRef"]).exists())

            failed_result = json.loads((artifact_dir / run.jobs[1].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("failed", failed_result["status"])
            self.assertEqual(429, failed_result["payload"]["statusCode"])
            self.assertEqual("rate_limit", failed_result["payload"]["category"])
            self.assertIn("Too Many Requests", failed_result["payload"]["error"])
            self.assertNotIn("transcriptEvents", failed_result)
            self.assertTrue((artifact_dir / run.jobs[1].metadata["transcriptRef"]).exists())

            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("agent_completed", workflow_events)
            self.assertIn("agent_failed", workflow_events)
            self.assert_workflow_failed_final_error_and_idle(
                bridge=bridge,
                events=events,
                run_id=run_id,
                marker="429",
            )

    def test_workflow_stop_after_completed_prefix_cancels_running_child_and_resume_uses_prefix(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runner = StopThenResumeRunner()

        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
                timeout_seconds=timeout_seconds,
            )

        script = """
const first = await agent('prefix succeeds')
const second = await agent('stop while this is running')
return { marker: 'GA_STOP_RESUME_DONE', first: first.summary, second: second.summary }
"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=runtime_factory,
            )
            source_run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(source_run_id, timeout_seconds=5.0))
            self.assertTrue(runner.second_started.wait(timeout=3.0))

            self.assertTrue(bridge.workflow_stop(source_run_id, reason="user stop after prefix"))
            bridge.wait_for_workflow_idle(source_run_id, timeout=5)
            source_thread = bridge._workflow_threads[source_run_id]
            self.assertFalse(source_thread.is_alive())

            source = bridge.workflow_store.load_run(source_run_id)
            source_artifact_dir = Path(source.artifact_dir)
            self.assertEqual("killed", source.status)
            self.assertIn("user stop after prefix", source.error)
            self.assertEqual(["succeeded", "cancelled"], [job.status for job in source.jobs])
            self.assertEqual(["agent_2"], runner.cancelled_job_ids)
            self.assertTrue((source_artifact_dir / source.jobs[0].result_ref).exists())
            self.assertTrue((source_artifact_dir / source.jobs[0].metadata["transcriptRef"]).exists())
            source_final = json.loads((source_artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("killed", source_final["status"])
            self.assertIn("user stop after prefix", source_final["error"])

            source_terminal_run = [event for event in events if event["type"] == "workflow_run" and event["run"]["runId"] == source_run_id][-1]
            self.assertEqual("killed", source_terminal_run["run"]["status"])
            source_final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == source_run_id)
            self.assertEqual("killed", source_final_event["result"]["status"])
            self.assertIn("user stop after prefix", source_final_event["result"]["error"])

            resumed_run_id = bridge.workflow_resume(source_run_id, timeout_seconds=5.0)
            self.assertTrue(resumed_run_id)
            bridge.wait_for_workflow_idle(resumed_run_id, timeout=5)
            resumed_thread = bridge._workflow_threads[resumed_run_id]
            self.assertFalse(resumed_thread.is_alive())

            resumed = bridge.workflow_store.load_run(resumed_run_id)
            resumed_artifact_dir = Path(resumed.artifact_dir)
            self.assertEqual("succeeded", resumed.status)
            self.assertEqual(["cached", "succeeded"], [job.status for job in resumed.jobs])
            self.assertEqual(source_run_id, resumed.jobs[0].metadata["cachedFromRunId"])
            self.assertEqual("agent_1", resumed.jobs[0].metadata["cachedFromJobId"])
            self.assertTrue((resumed_artifact_dir / resumed.jobs[0].result_ref).exists())
            self.assertTrue((resumed_artifact_dir / resumed.jobs[0].metadata["transcriptRef"]).exists())
            self.assertTrue((resumed_artifact_dir / resumed.jobs[1].result_ref).exists())
            self.assertTrue((resumed_artifact_dir / resumed.jobs[1].metadata["transcriptRef"]).exists())
            resumed_final = json.loads((resumed_artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("succeeded", resumed_final["status"])
            self.assertEqual("GA_STOP_RESUME_DONE", resumed_final["result"]["marker"])

            workflow_events_by_run = self.workflow_event_types_by_run(events)
            self.assertIn("agent_cancelled", workflow_events_by_run[source_run_id])
            self.assertIn("workflow_killed", workflow_events_by_run[source_run_id])
            self.assertIn("agent_cached", workflow_events_by_run[resumed_run_id])
            self.assertIn("agent_completed", workflow_events_by_run[resumed_run_id])
            final_events = [event for event in events if event["type"] == "workflow_final"]
            self.assertEqual({source_run_id, resumed_run_id}, {event["runId"] for event in final_events})
            self.assert_bridge_idle_tail(events)

    def test_workflow_resume_cache_hit_replays_prefix_without_restarting_cached_child(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runner = StopThenResumeRunner()

        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
                timeout_seconds=timeout_seconds,
            )

        script = """
const first = await agent('prefix succeeds')
const second = await agent('stop while this is running')
return { marker: 'GA_STOP_RESUME_DONE', first: first.summary, second: second.summary }
"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=runtime_factory,
            )
            source_run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(source_run_id, timeout_seconds=5.0))
            self.assertTrue(runner.second_started.wait(timeout=3.0))

            self.assertTrue(bridge.workflow_stop(source_run_id, reason="user stop after prefix"))
            bridge.wait_for_workflow_idle(source_run_id, timeout=5)
            self.assertFalse(bridge._workflow_threads[source_run_id].is_alive())

            source = bridge.workflow_store.load_run(source_run_id)
            self.assertEqual("killed", source.status)
            self.assertEqual(["succeeded", "cancelled"], [job.status for job in source.jobs])
            self.assertEqual(["agent_1", "agent_2"], runner.started_job_ids)
            self.assertEqual(["agent_2"], runner.cancelled_job_ids)

            resumed_run_id = bridge.workflow_resume(source_run_id, timeout_seconds=5.0)
            self.assertTrue(resumed_run_id)
            bridge.wait_for_workflow_idle(resumed_run_id, timeout=5)
            self.assertFalse(bridge._workflow_threads[resumed_run_id].is_alive())

            resumed = bridge.workflow_store.load_run(resumed_run_id)
            resumed_artifact_dir = Path(resumed.artifact_dir)
            self.assertEqual("succeeded", resumed.status)
            self.assertEqual(["cached", "succeeded"], [job.status for job in resumed.jobs])
            self.assertEqual(["agent_1", "agent_2", "agent_2"], runner.started_job_ids)
            self.assertEqual(source_run_id, resumed.jobs[0].metadata["cachedFromRunId"])
            self.assertEqual("agent_1", resumed.jobs[0].metadata["cachedFromJobId"])

            cached_result_path = resumed_artifact_dir / resumed.jobs[0].result_ref
            self.assertTrue(cached_result_path.exists())
            cached_result = json.loads(cached_result_path.read_text(encoding="utf-8"))
            self.assertEqual("succeeded", cached_result["status"])
            self.assertEqual("prefix cached by resume", cached_result["payload"]["summary"])
            self.assertNotIn("transcriptEvents", cached_result)
            self.assertTrue((resumed_artifact_dir / resumed.jobs[0].metadata["transcriptRef"]).exists())
            self.assertTrue((resumed_artifact_dir / resumed.jobs[1].result_ref).exists())
            self.assertTrue((resumed_artifact_dir / resumed.jobs[1].metadata["transcriptRef"]).exists())

            resumed_final = json.loads((resumed_artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("succeeded", resumed_final["status"])
            self.assertEqual("GA_STOP_RESUME_DONE", resumed_final["result"]["marker"])
            self.assertEqual("prefix cached by resume", resumed_final["result"]["first"])
            self.assertEqual("fresh after stop", resumed_final["result"]["second"])

            workflow_events_by_run = self.workflow_event_types_by_run(events)
            self.assertIn("agent_cancelled", workflow_events_by_run[source_run_id])
            self.assertIn("workflow_killed", workflow_events_by_run[source_run_id])
            self.assertIn("agent_cached", workflow_events_by_run[resumed_run_id])
            self.assertIn("agent_completed", workflow_events_by_run[resumed_run_id])
            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == resumed_run_id)
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assert_bridge_idle_tail(events)

    def test_workflow_resume_cache_miss_when_args_change_starts_fresh_child(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class ArgsChangingRunner:
            def __init__(self):
                self.started_job_ids = []
                self.started_prompts = []

            def start(self, job):
                self.started_job_ids.append(job.job_id)
                self.started_prompts.append(job.prompt)

            def poll(self, job):
                return AgentResult(
                    job_id=job.job_id,
                    status="succeeded",
                    payload={"summary": f"fresh {job.prompt}"},
                    transcript_events=[{"type": "assistant", "text": f"transcript {job.prompt}"}],
                )

            def cancel(self, job):
                return None

        runner = ArgsChangingRunner()

        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
                timeout_seconds=timeout_seconds,
            )

        script = """
const result = await agent('inspect ' + args.target)
return { marker: 'GA_ARGS_MISS_DONE', summary: result.summary }
"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=runtime_factory,
            )
            source_run_id = bridge.workflow_draft(script)
            self.assertTrue(bridge.workflow_approve(source_run_id, args={"target": "old"}, timeout_seconds=5.0))
            bridge.wait_for_workflow_idle(source_run_id, timeout=5)
            self.assertFalse(bridge._workflow_threads[source_run_id].is_alive())

            source = bridge.workflow_store.load_run(source_run_id)
            source_artifact_dir = Path(source.artifact_dir)
            self.assertEqual("succeeded", source.status)
            self.assertEqual(["succeeded"], [job.status for job in source.jobs])
            source_final = json.loads((source_artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("GA_ARGS_MISS_DONE", source_final["result"]["marker"])
            self.assertEqual("fresh inspect old", source_final["result"]["summary"])

            resumed_run_id = bridge.workflow_resume(source_run_id, args={"target": "new"}, timeout_seconds=5.0)
            self.assertTrue(resumed_run_id)
            self.assertNotEqual(source_run_id, resumed_run_id)
            bridge.wait_for_workflow_idle(resumed_run_id, timeout=5)
            self.assertFalse(bridge._workflow_threads[resumed_run_id].is_alive())

            resumed = bridge.workflow_store.load_run(resumed_run_id)
            resumed_artifact_dir = Path(resumed.artifact_dir)
            self.assertEqual("succeeded", resumed.status)
            self.assertEqual(["succeeded"], [job.status for job in resumed.jobs])
            self.assertNotIn("cachedFromRunId", resumed.jobs[0].metadata)
            self.assertNotIn("cachedFromJobId", resumed.jobs[0].metadata)
            self.assertEqual(["agent_1", "agent_1"], runner.started_job_ids)
            self.assertEqual(["inspect old", "inspect new"], runner.started_prompts)

            resumed_result = json.loads((resumed_artifact_dir / resumed.jobs[0].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("succeeded", resumed_result["status"])
            self.assertEqual("fresh inspect new", resumed_result["payload"]["summary"])
            self.assertNotIn("transcriptEvents", resumed_result)
            self.assertTrue((resumed_artifact_dir / resumed.jobs[0].metadata["transcriptRef"]).exists())

            resumed_final = json.loads((resumed_artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("succeeded", resumed_final["status"])
            self.assertEqual("GA_ARGS_MISS_DONE", resumed_final["result"]["marker"])
            self.assertEqual("fresh inspect new", resumed_final["result"]["summary"])

            workflow_events_by_run = self.workflow_event_types_by_run(events)
            self.assertNotIn("agent_cached", workflow_events_by_run[resumed_run_id])
            self.assertIn("agent_started", workflow_events_by_run[resumed_run_id])
            self.assertIn("agent_completed", workflow_events_by_run[resumed_run_id])
            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == resumed_run_id)
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assert_bridge_idle_tail(events)

    def test_workflow_resume_cache_miss_when_tool_context_changes_starts_fresh_child(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class ToolContextRunner:
            def __init__(self):
                self.started_job_ids = []

            def start(self, job):
                self.started_job_ids.append(job.job_id)

            def poll(self, job):
                return AgentResult(
                    job_id=job.job_id,
                    status="succeeded",
                    payload={"summary": "fresh inspect tools"},
                    transcript_events=[{"type": "assistant", "text": "fresh tool transcript"}],
                )

            def cancel(self, job):
                return None

        runner = ToolContextRunner()

        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
                timeout_seconds=timeout_seconds,
            )

        script = """
const result = await agent('inspect tools')
return { marker: 'GA_TOOL_CONTEXT_MISS_DONE', summary: result.summary }
"""
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=runtime_factory,
            )
            source_run_id = bridge.workflow_draft(script)
            source = bridge.workflow_store.load_run(source_run_id)
            source.metadata["toolContext"] = {"allowedTools": ["Read"], "toolSchemaHash": "schema-v1"}
            bridge.workflow_store.save_run(source)
            self.assertTrue(bridge.workflow_approve(source_run_id, args={"same": True}, timeout_seconds=5.0))
            bridge.wait_for_workflow_idle(source_run_id, timeout=5)

            source = bridge.workflow_store.load_run(source_run_id)
            self.assertEqual("succeeded", source.status)
            self.assertEqual(["succeeded"], [job.status for job in source.jobs])
            self.assertEqual(["agent_1"], runner.started_job_ids)

            with patch("threading.Thread.start", lambda _thread: None):
                resumed_run_id = bridge.workflow_resume(source_run_id, args={"same": True}, timeout_seconds=5.0)
            self.assertTrue(resumed_run_id)
            resumed = bridge.workflow_store.load_run(resumed_run_id)
            resumed.metadata["toolContext"] = {"allowedTools": ["Read", "Write"], "toolSchemaHash": "schema-v1"}
            bridge.workflow_store.save_run(resumed)
            bridge._workflow_threads[resumed_run_id].start()
            bridge.wait_for_workflow_idle(resumed_run_id, timeout=5)
            self.assertFalse(bridge._workflow_threads[resumed_run_id].is_alive())

            resumed = bridge.workflow_store.load_run(resumed_run_id)
            resumed_artifact_dir = Path(resumed.artifact_dir)
            self.assertEqual("succeeded", resumed.status)
            self.assertEqual(["succeeded"], [job.status for job in resumed.jobs])
            self.assertNotIn("cachedFromRunId", resumed.jobs[0].metadata)
            self.assertNotIn("cachedFromJobId", resumed.jobs[0].metadata)
            self.assertEqual(["agent_1", "agent_1"], runner.started_job_ids)
            self.assertNotEqual(source.jobs[0].metadata["cacheKey"]["toolContextHash"], resumed.jobs[0].metadata["cacheKey"]["toolContextHash"])

            resumed_result = json.loads((resumed_artifact_dir / resumed.jobs[0].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("succeeded", resumed_result["status"])
            self.assertEqual("fresh inspect tools", resumed_result["payload"]["summary"])
            resumed_final = json.loads((resumed_artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("succeeded", resumed_final["status"])
            self.assertEqual("GA_TOOL_CONTEXT_MISS_DONE", resumed_final["result"]["marker"])
            self.assertEqual("fresh inspect tools", resumed_final["result"]["summary"])

            workflow_events_by_run = self.workflow_event_types_by_run(events)
            self.assertNotIn("agent_cached", workflow_events_by_run[resumed_run_id])
            self.assertIn("agent_started", workflow_events_by_run[resumed_run_id])
            self.assertIn("agent_completed", workflow_events_by_run[resumed_run_id])
            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == resumed_run_id)
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assert_bridge_idle_tail(events)

    def test_workflow_resume_runs_new_run_with_resume_source(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runtime_calls = []

        class FakeRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store
                self.timeout_seconds = timeout_seconds

            def run(self, run, *, args=None, resume_from_run_id=None):
                runtime_calls.append({"runId": run.run_id, "args": args, "resumeFromRunId": resume_from_run_id})
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"resumedFrom": resume_from_run_id, "args": args}}
                self.store.write_final_result(run, payload)
                run.status = "succeeded"
                self.store.save_run(run)
                return type("RuntimeResult", (), {"run": run, "result": payload["result"]})()

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: FakeRuntime(**kwargs),
            )
            source_run_id = bridge.workflow_draft("return { ok: true }")
            source = bridge.workflow_store.load_run(source_run_id)
            source.status = "succeeded"
            bridge.workflow_store.save_run(source)

            resumed = bridge.workflow_resume(source_run_id, args={"value": 9}, timeout_seconds=4)
            bridge.wait_for_workflow_idle(resumed, timeout=1)

            self.assertTrue(resumed.startswith("wf_"))
            self.assertNotEqual(source_run_id, resumed)
            self.assertEqual([{"runId": resumed, "args": {"value": 9}, "resumeFromRunId": source_run_id}], runtime_calls)
            self.assertTrue(any(event["type"] == "workflow_run" and event["run"]["runId"] == resumed for event in events))
            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == resumed)
            self.assertEqual({"resumedFrom": source_run_id, "args": {"value": 9}}, final_event["result"]["result"])

    def test_workflow_resume_allows_failed_killed_and_interrupted_source_runs(self):
        for terminal_status in ("failed", "killed", "interrupted"):
            with self.subTest(status=terminal_status):
                agent = FakeAgent()
                agent.session_id = f"session_workflow_{terminal_status}"
                events = []
                runtime_calls = []

                class FakeRuntime:
                    def __init__(self, *, store, timeout_seconds=10.0):
                        self.store = store

                    def run(self, run, *, args=None, resume_from_run_id=None):
                        runtime_calls.append(resume_from_run_id)
                        run.status = "succeeded"
                        self.store.write_final_result(run, {"runId": run.run_id, "status": "succeeded", "result": {}})
                        self.store.save_run(run)

                with tempfile.TemporaryDirectory() as tmp:
                    bridge = GenericAgentBridge(
                        agent_factory=lambda: agent,
                        emit=events.append,
                        workflow_root=tmp,
                        workflow_runtime_factory=lambda **kwargs: FakeRuntime(**kwargs),
                    )
                    source_run_id = bridge.workflow_draft("return 1")
                    source = bridge.workflow_store.load_run(source_run_id)
                    source.status = terminal_status
                    bridge.workflow_store.save_run(source)

                    resumed = bridge.workflow_resume(source_run_id)
                    bridge.wait_for_workflow_idle(resumed, timeout=1)

                    self.assertTrue(resumed.startswith("wf_"))
                    self.assertEqual([source_run_id], runtime_calls)
                    self.assertFalse(any(event["type"] == "error" and event.get("code") == "workflow_resume_failed" for event in events))

    def test_workflow_resume_rejects_blank_run_id_with_protocol_error(self):
        agent = FakeAgent()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)

            resumed = bridge.workflow_resume("", args={"value": 1})

        self.assertEqual("", resumed)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_bad_run_id", events[-1]["code"])

    def test_workflow_resume_rejects_unfinished_source_run(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            source_run_id = bridge.workflow_draft("return 1")

            resumed = bridge.workflow_resume(source_run_id)

        self.assertEqual("", resumed)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_resume_failed", events[-1]["code"])
        self.assertIn("from awaiting_approval", events[-1]["message"])

    def test_workflow_resume_rejects_denied_cancelled_source_run(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runtime_calls = []

        class FakeRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                runtime_calls.append(run.run_id)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: FakeRuntime(**kwargs),
            )
            source_run_id = bridge.workflow_draft("return 1")
            bridge.workflow_deny(source_run_id, reason="user deny")

            resumed = bridge.workflow_resume(source_run_id)

        self.assertEqual("", resumed)
        self.assertEqual([], runtime_calls)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_resume_failed", events[-1]["code"])
        self.assertIn("from cancelled", events[-1]["message"])

    def test_workflow_list_detail_and_stop_commands(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 1")

            bridge.workflow_list()
            bridge.workflow_detail(run_id)
            bridge.workflow_deny(run_id, reason="user deny")

            runs_event = next(event for event in events if event["type"] == "workflow_runs")
            detail_event = next(event for event in events if event["type"] == "workflow_detail")
            run_events = [event for event in events if event["type"] == "workflow_run"]
            self.assertEqual([run_id], [run["runId"] for run in runs_event["runs"]])
            self.assertEqual("return 1", detail_event["script"])
            self.assertEqual("cancelled", run_events[-1]["run"]["status"])
            workflow_events = [event["event"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("workflow_denied", [event["type"] for event in workflow_events])
            denied = next(event for event in workflow_events if event["type"] == "workflow_denied")
            self.assertEqual("user deny", denied["payload"]["reason"])

    def test_workflow_stop_rejects_blank_run_id_with_protocol_error(self):
        agent = FakeAgent()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)

            stopped = bridge.workflow_stop("", reason="user")

        self.assertFalse(stopped)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_bad_run_id", events[-1]["code"])

    def test_stop_stops_active_running_workflow_instead_of_reporting_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runtime_started = threading.Event()
        release_runtime = threading.Event()

        class BlockingRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                runtime_started.set()
                release_runtime.wait(timeout=2)
                return None

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: BlockingRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return 1")
            self.assertTrue(bridge.workflow_approve(run_id))
            self.assertTrue(runtime_started.wait(timeout=1))

            bridge.stop()
            release_runtime.set()
            bridge.wait_for_workflow_idle(run_id, timeout=1)

            run = bridge.workflow_store.load_run(run_id)
            self.assertEqual("killed", run.status)
            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("workflow_killed", workflow_events)
            self.assertTrue(any(event["type"] == "workflow_run" and event["run"]["status"] == "killed" for event in events))
            self.assertTrue(
                any(
                    event["type"] == "workflow_final"
                    and event["runId"] == run_id
                    and event["result"]["status"] == "killed"
                    for event in events
                )
            )

    def test_workflow_final_payload_falls_back_for_terminal_run_without_or_with_bad_result_ref(self):
        agent = FakeAgent()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 1")
            run = bridge.workflow_store.load_run(run_id)
            run.status = "failed"
            run.error = "no result"
            run.result_ref = None
            self.assertEqual(
                {"runId": run_id, "status": "failed", "error": "no result", "resultRef": None, "artifactError": "missing_ref"},
                bridge._workflow_final_payload(run),
            )

            run.status = "killed"
            run.error = "bad result"
            run.artifact_dir = tmp
            run.result_ref = "missing-result.json"
            self.assertEqual(
                {"runId": run_id, "status": "killed", "error": "bad result", "resultRef": "missing-result.json", "artifactError": "missing"},
                bridge._workflow_final_payload(run),
            )

    def test_workflow_final_emits_fallback_and_idle_when_final_result_missing_after_runtime(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class MissingFinalRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"ok": True}}
                self.store.write_final_result(run, payload)
                final_path = Path(run.artifact_dir) / run.result_ref
                final_path.unlink()
                run.status = "succeeded"
                self.store.save_run(run)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: MissingFinalRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return { ok: true }")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual(run_id, final_event["result"]["runId"])
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assertEqual("final-result.json", final_event["result"]["resultRef"])
            self.assertEqual("missing", final_event["result"]["artifactError"])
            serialized = json.dumps(final_event, ensure_ascii=False)
            self.assertNotIn("Traceback", serialized)
            self.assertFalse(bridge._workflow_threads[run_id].is_alive())
            self.assert_bridge_idle_tail(events)

    def test_workflow_final_emits_fallback_and_idle_when_final_result_json_is_corrupt(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class CorruptFinalRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"ok": True}}
                self.store.write_final_result(run, payload)
                final_path = Path(run.artifact_dir) / run.result_ref
                final_path.write_text('{"broken": ', encoding="utf-8")
                run.status = "succeeded"
                self.store.save_run(run)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: CorruptFinalRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return { ok: true }")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual(run_id, final_event["result"]["runId"])
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assertEqual("final-result.json", final_event["result"]["resultRef"])
            self.assertEqual("invalid_json", final_event["result"]["artifactError"])
            serialized = json.dumps(events, ensure_ascii=False)
            self.assertNotIn("JSONDecodeError", serialized)
            self.assertNotIn('"broken"', serialized)
            self.assertFalse(bridge._workflow_threads[run_id].is_alive())
            self.assert_bridge_idle_tail(events)

    def test_workflow_final_large_result_uses_bounded_sanitized_fallback(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        marker = "GA_BRIDGE_HUGE_FINAL_RESULT"

        class LargeFinalRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"blob": marker * 5000}}
                self.store.write_final_result(run, payload)
                run.status = "succeeded"
                self.store.save_run(run)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: LargeFinalRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return { ok: true }")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            final_text = json.dumps(final_event, ensure_ascii=False)
            self.assertLess(len(final_text), 64 * 1024)
            self.assertNotIn(marker, final_text)
            self.assertEqual("too_large", final_event["result"]["artifactError"])
            self.assertTrue(final_event["result"]["artifactTruncated"])
            self.assertGreater(final_event["result"]["artifactSize"], 64 * 1024)
            self.assertEqual("final-result.json", final_event["result"]["resultRef"])
            self.assert_bridge_idle_tail(events)

    def test_workflow_final_corrupt_or_large_payload_does_not_leak_secrets(self):
        agent = FakeAgent()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 1")
            run = bridge.workflow_store.load_run(run_id)
            run.status = "failed"
            run.error = "Authorization: Bearer bridge-final-secret token=bridge-token-secret request_id=req_123"
            run.result_ref = None

            payload = bridge._workflow_final_payload(run)

            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertIn("request_id=req_123", serialized)
            self.assertIn("[REDACTED]", serialized)
            self.assertNotIn("bridge-final-secret", serialized)
            self.assertNotIn("bridge-token-secret", serialized)

    def test_workflow_final_rejects_result_ref_outside_artifact_dir_with_fallback(self):
        agent = FakeAgent()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 1")
            outside_path = Path(tmp).parent / "bridge-outside-result.json"
            outside_path.write_text(json.dumps({"secret": "GA_BRIDGE_OUTSIDE_RESULT"}), encoding="utf-8")
            try:
                run = bridge.workflow_store.load_run(run_id)
                run.status = "succeeded"
                run.artifact_dir = tmp
                run.result_ref = os.path.relpath(outside_path, tmp)

                payload = bridge._workflow_final_payload(run)

                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertEqual("invalid_result_ref", payload["artifactError"])
                self.assertNotIn("GA_BRIDGE_OUTSIDE_RESULT", serialized)
            finally:
                try:
                    outside_path.unlink()
                except FileNotFoundError:
                    pass

    def test_workflow_detail_keeps_result_refs_only_and_never_reads_agent_result_artifacts(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        marker = "GA_BRIDGE_CORRUPT_AGENT_RESULT"

        class CorruptAgentResultRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                from workflow_models import WorkflowEvent

                result_ref = "agents/agent_1/result.json"
                result_path = Path(run.artifact_dir) / result_ref
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text('{"huge":"' + marker * 1000, encoding="utf-8")
                self.store.append_event(
                    run,
                    WorkflowEvent(
                        run_id=run.run_id,
                        session_id=run.session_id,
                        event_type="agent_completed",
                        sequence=99,
                        payload={"jobId": "agent_1", "resultRef": result_ref, "result": {"transcriptRef": "agents/agent_1/transcript.jsonl"}},
                    ),
                )
                self.store.write_final_result(run, {"runId": run.run_id, "status": "succeeded", "jobs": [{"jobId": "agent_1", "resultRef": result_ref}]})
                run.status = "succeeded"
                self.store.save_run(run)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: CorruptAgentResultRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return await agent('detail refs only')")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            events.clear()
            bridge.workflow_detail(run_id)

            self.assertFalse(any(event.get("code") == "workflow_detail_failed" for event in events))
            detail_event = next(event for event in events if event["type"] == "workflow_detail" and event["run"]["runId"] == run_id)
            detail_text = json.dumps(detail_event, ensure_ascii=False)
            self.assertIn("resultRef", detail_text)
            self.assertIn("agents/agent_1/result.json", detail_text)
            self.assertNotIn(marker, detail_text)
            self.assertNotIn("huge", detail_text)

    def test_jsonl_loop_routes_mcp_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "mcp_status"}) + "\n"
            + json.dumps({"type": "mcp_reconnect", "server": "demo"}) + "\n"
            + json.dumps({"type": "mcp_enable", "server": "demo"}) + "\n"
            + json.dumps({"type": "mcp_disable", "server": "demo"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.mcp_status.assert_called_once_with()
        bridge.mcp_reconnect.assert_called_once_with("demo")
        bridge.mcp_enable.assert_called_once_with("demo")
        bridge.mcp_disable.assert_called_once_with("demo")

    def test_jsonl_loop_routes_model_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "model_status"}) + "\n"
            + json.dumps({"type": "model_switch", "selector": "kimi"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.model_status.assert_called_once_with()
        bridge.model_switch.assert_called_once_with("kimi")

    def test_jsonl_loop_routes_skill_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "skill_status"}) + "\n"
            + json.dumps({"type": "skill_invoke", "skill": "demo", "args": "hello world"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.skill_status.assert_called_once_with()
        bridge.skill_invoke.assert_called_once_with("demo", "hello world")

    def test_jsonl_loop_routes_compact_command(self):
        stdin = io.StringIO(
            json.dumps({"type": "compact", "instructions": "keep decisions"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.compact.assert_called_once_with("keep decisions")


    def test_jsonl_loop_routes_workflow_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "workflow_draft", "script": "return 1"}) + "\n"
            + json.dumps({"type": "workflow_approve", "runId": "wf_1", "args": {"x": 1}, "timeoutSeconds": 2}) + "\n"
            + json.dumps({"type": "workflow_resume", "runId": "wf_1", "args": {"y": 2}, "timeoutSeconds": 3}) + "\n"
            + json.dumps({"type": "workflow_list"}) + "\n"
            + json.dumps({"type": "workflow_detail", "runId": "wf_1"}) + "\n"
            + json.dumps({"type": "workflow_progress", "runId": "wf_1"}) + "\n"
            + json.dumps({"type": "workflow_deny", "runId": "wf_1", "reason": "no"}) + "\n"
            + json.dumps({"type": "workflow_stop", "runId": "wf_1", "reason": "user"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.workflow_draft.assert_called_once_with("return 1")
        bridge.workflow_approve.assert_called_once_with("wf_1", args={"x": 1}, timeout_seconds=2.0)
        bridge.workflow_resume.assert_called_once_with("wf_1", args={"y": 2}, timeout_seconds=3.0)
        bridge.workflow_list.assert_called_once_with()
        bridge.workflow_detail.assert_called_once_with("wf_1")
        bridge.workflow_progress.assert_called_once_with("wf_1")
        bridge.workflow_deny.assert_called_once_with("wf_1", reason="no")
        bridge.workflow_stop.assert_called_once_with("wf_1", reason="user")

    def test_jsonl_loop_dispatches_workflow_plan_command(self):
        stdin = io.StringIO(
            json.dumps({
                "type": "workflow_plan",
                "taskText": "规划 JSONL workflow",
                "context": {"source": "jsonl"},
                "autoApprove": False,
                "args": {"x": 1},
                "timeoutSeconds": 4,
            }) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.workflow_plan.assert_called_once_with(
            "规划 JSONL workflow",
            context={"source": "jsonl"},
            auto_approve=False,
            args={"x": 1},
            timeout_seconds=4.0,
        )

    def test_default_workflow_planner_factory_uses_env_builder(self):
        agent = FakeAgent()
        events = []
        sentinel = object()
        with patch("workflow_planner.build_workflow_planner_from_env", return_value=sentinel) as builder:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            planner = bridge._make_workflow_planner()

        self.assertIs(sentinel, planner)
        builder.assert_called_once_with()

    def test_bridge_script_can_import_agentmain_when_run_from_repo_root(self):
        proc = subprocess.run(
            [sys.executable, str(FRONTENDS / "ink_bridge.py")],
            input=json.dumps({"type": "shutdown"}) + "\n",
            text=True,
            capture_output=True,
            cwd=REPO_ROOT,
            timeout=20,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual("ready", events[0]["type"])


if __name__ == "__main__":
    unittest.main()
