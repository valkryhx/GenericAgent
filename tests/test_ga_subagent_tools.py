import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agent_loop import exhaust  # noqa: E402
from ga import GenericAgentHandler, get_global_memory  # noqa: E402
from subagent_manager import SubagentManager  # noqa: E402
from subagent_state import atomic_write_json  # noqa: E402


class FakeParent:
    def __init__(self):
        backend = type(
            "Backend",
            (),
            {
                "history": [
                    {"role": "user", "content": "parent context"},
                    {"role": "assistant", "content": "parent answer"},
                ]
            },
        )()
        self.llmclient = type("Client", (), {"backend": backend})()
        self.verbose = False
        self.task_dir = None
        self.llm_no = 1
        self.session_id = "parent_session"


class GaSubagentToolsTest(unittest.TestCase):
    def make_handler(self, root, **manager_kwargs):
        handler = GenericAgentHandler(FakeParent(), last_history=[], cwd=str(Path(root) / "temp"))
        handler.subagent_manager = SubagentManager(root_dir=root, **manager_kwargs)
        return handler

    def test_spawn_agent_tool_defaults_to_context_fork_and_keeps_prompt_out_of_command(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            class FakeProcess:
                pid = 7654

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            handler = self.make_handler(td, popen=fake_popen, python_executable="python-test")

            outcome = exhaust(
                handler.do_spawn_agent(
                    {
                        "task_name": "context_worker",
                        "message": "inspect inherited context",
                    },
                    response=None,
                )
            )

            task_dir = Path(td) / "temp" / "context_worker"
            self.assertEqual(outcome.data["status"], "started")
            self.assertEqual(outcome.data["fork_turns"], "all")
            self.assertTrue(outcome.data["run_id"].startswith("run_"))
            self.assertEqual(Path(outcome.data["artifact_dir"]), Path(td) / "temp" / "subagents" / "runs" / outcome.data["run_id"])
            self.assertEqual(outcome.data["permission_profile"], "inherit-current-permissions")
            self.assertEqual((task_dir / "input.txt").read_text(encoding="utf-8"), "inspect inherited context")
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                FakeParent().llmclient.backend.history,
            )
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[0], "python-test")
            self.assertIn("--task_root", cmd)
            self.assertIn(str(Path(td)), cmd)
            self.assertNotIn("inspect inherited context", cmd)
            self.assertEqual(Path(kwargs["cwd"]), Path(td))

    def test_spawn_agent_tool_inherits_parent_permission_mode_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            parent = FakeParent()
            parent.permission_mode = "read_only"
            handler = GenericAgentHandler(parent, last_history=[], cwd=str(Path(td) / "temp"))
            handler.subagent_manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 246})(),
                python_executable="python-test",
            )

            outcome = exhaust(
                handler.do_spawn_agent(
                    {
                        "task_name": "inherit_worker",
                        "message": "inspect inherited permission mode",
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "started")
            self.assertEqual(outcome.data["permission_profile"], "inherit-current-permissions")
            self.assertEqual(outcome.data["parent_permission_mode"], "read_only")
            state = json.loads((Path(td) / "temp" / "inherit_worker" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["parent_permission_mode"], "read_only")

    def test_spawn_agent_tool_passes_permission_profile_and_options(self):
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(
                td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 222})(),
                python_executable="python-test",
            )

            outcome = exhaust(
                handler.do_spawn_agent(
                    {
                        "task_name": "readonly_worker",
                        "message": "inspect only",
                        "permission_profile": "read_only",
                        "denied_tools": ["code_run"],
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "started")
            self.assertEqual(outcome.data["permission_profile"], "read_only")
            state = json.loads((Path(td) / "temp" / "readonly_worker" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["permission_profile"], "read_only")
            self.assertEqual(state["permission_options"], {"denied_tools": ["code_run"]})

    def test_spawn_agent_tool_applies_agent_type_role_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            roles_dir = Path(td) / ".ga" / "subagents"
            roles_dir.mkdir(parents=True)
            (roles_dir / "researcher.json").write_text(
                json.dumps(
                    {
                        "name": "researcher",
                        "description": "Read-only researcher",
                        "system_prompt": "Inspect only and cite paths.",
                        "permission_profile": "read_only",
                        "allowed_tools": ["file_read", "grep"],
                        "fork_turns_default": "none",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            handler = self.make_handler(
                td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 333})(),
                python_executable="python-test",
            )

            outcome = exhaust(
                handler.do_spawn_agent(
                    {
                        "agent_type": "researcher",
                        "message": "Find relevant tests.",
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "started")
            self.assertEqual(outcome.data["agent_type"], "researcher")
            self.assertEqual(outcome.data["task_name"], "researcher")
            self.assertEqual(outcome.data["fork_turns"], "none")
            self.assertEqual(outcome.data["permission_profile"], "read_only")
            self.assertEqual(outcome.data["permission_options"], {"allowed_tools": ["file_read", "grep"]})
            task_dir = Path(td) / "temp" / "researcher"
            prompt = (task_dir / "input.txt").read_text(encoding="utf-8")
            self.assertIn("[GA_SUBAGENT_ROLE]", prompt)
            self.assertIn("Inspect only and cite paths.", prompt)
            self.assertIn("Find relevant tests.", prompt)
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["agent_type"], "researcher")
            self.assertTrue(state["role_source_path"].endswith("researcher.json"))

    def test_spawn_agent_tool_rejects_unknown_agent_type(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            handler = self.make_handler(
                td,
                popen=lambda *args, **kwargs: calls.append((args, kwargs)),
                python_executable="python-test",
            )

            outcome = exhaust(
                handler.do_spawn_agent(
                    {
                        "agent_type": "missing",
                        "message": "Find relevant tests.",
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "error")
            self.assertIn("missing", outcome.data["msg"])
            self.assertEqual(calls, [])

    def test_global_memory_distinguishes_tool_cwd_from_workspace_root(self):
        prompt = get_global_memory()

        self.assertIn("tool scratch/output dir", prompt)
        self.assertIn("workspace root =", prompt)
        self.assertIn("README.md", prompt)

    def test_wait_agent_tool_reports_completed_without_reading_final_output(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "done_worker"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("finished work\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "done_worker",
                    "agent_path": "/root/done_worker",
                    "pid": 123,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            outcome = exhaust(
                handler.do_wait_agent(
                    {
                        "target": "done_worker",
                        "timeout_seconds": 0,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "changed")
            self.assertEqual(outcome.data["agents"][0]["turn_status"], "completed")
            self.assertEqual(outcome.data["agents"][0]["process_status"], "waiting_reply")
            self.assertNotIn("final_output", outcome.data["agents"][0])
            self.assertIn("read_agent_result", outcome.data["result_hint"])

            read_outcome = exhaust(
                handler.do_read_agent_result(
                    {
                        "target": "done_worker",
                        "max_output_chars": 2000,
                    },
                    response=None,
                )
            )
            self.assertEqual(read_outcome.data["status"], "success")
            self.assertEqual(read_outcome.data["agent"]["final_output"], "finished work")

    def test_wait_agent_empty_targets_falls_back_to_target(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td) / "temp"
            (temp_root / "ga-real-e2e-tmp").mkdir(parents=True)
            (temp_root / "ga-real-e2e-tmp" / "state.json").write_text("{}", encoding="utf-8")
            task_dir = temp_root / "done_worker"
            task_dir.mkdir()
            output_path = task_dir / "output.txt"
            output_path.write_text("finished work\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "done_worker",
                    "agent_path": "/root/done_worker",
                    "pid": 123,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            outcome = exhaust(
                handler.do_wait_agent(
                    {
                        "target": "/root/done_worker",
                        "targets": [],
                        "timeout_seconds": 0,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "changed")
            self.assertEqual([agent["task_name"] for agent in outcome.data["agents"]], ["done_worker"])
            self.assertNotIn("final_output", outcome.data["agents"][0])

    def test_message_followup_and_interrupt_tools_write_mailbox_reply_and_stop_file(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "mail_worker"
            task_dir.mkdir(parents=True)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "mail_worker",
                    "agent_path": "/root/mail_worker",
                    "pid": 999,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            send_outcome = exhaust(handler.do_send_message({"target": "mail_worker", "message": "note only"}, None))
            followup_outcome = exhaust(handler.do_followup_task({"target": "mail_worker", "message": "run next"}, None))
            interrupt_outcome = exhaust(handler.do_interrupt_agent({"target": "mail_worker", "reason": "test"}, None))

            self.assertEqual(send_outcome.data["status"], "queued")
            self.assertEqual(followup_outcome.data["status"], "queued")
            rows = [
                json.loads(line)
                for line in (task_dir / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["content"] for row in rows], ["note only", "run next"])
            self.assertEqual([row["delivery_mode"] for row in rows], ["queue_only", "trigger_turn"])
            self.assertEqual([row["trigger_turn"] for row in rows], [False, True])
            self.assertFalse((task_dir / "reply.txt").exists())
            self.assertEqual(interrupt_outcome.data["status"], "interrupt_requested")
            self.assertEqual((task_dir / "_stop").read_text(encoding="utf-8"), "test")

    def test_close_agent_tool_returns_previous_status_and_closed_state(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "close_worker"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("ready\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "close_worker",
                    "agent_path": "/root/close_worker",
                    "pid": 888,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, terminate_process=lambda pid: None, sleep=lambda _: None)

            outcome = exhaust(handler.do_close_agent({"target": "/root/close_worker", "reason": "test_cleanup"}, None))

            self.assertEqual(outcome.data["status"], "closed")
            self.assertEqual(outcome.data["target"], "/root/close_worker")
            self.assertEqual(outcome.data["previous_status"]["turn_status"], "completed")
            self.assertEqual(outcome.data["previous_status"]["process_status"], "waiting_reply")
            self.assertEqual(outcome.data["closed_state"]["process_status"], "killed")
            self.assertEqual(Path(outcome.data["final_output_path"]), output_path)
            self.assertEqual((task_dir / "_stop").read_text(encoding="utf-8"), "test_cleanup")

    def test_close_agent_tool_can_cleanup_worktree_and_return_summary(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "close_worktree"
            task_dir.mkdir(parents=True)
            worktree = Path(td) / "temp" / "subagents" / "worktrees" / "run_close_worktree"
            worktree.mkdir(parents=True)
            (worktree / "leftover.txt").write_text("leftover", encoding="utf-8")
            output_path = task_dir / "output.txt"
            output_path.write_text("ready\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "close_worktree",
                    "agent_path": "/root/close_worktree",
                    "pid": None,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "waiting_reply",
                    "output_path": str(output_path),
                    "final_output_path": str(output_path),
                    "isolation": "worktree",
                    "worktree_path": str(worktree),
                },
            )
            calls = []

            def fake_runner(cmd, **kwargs):
                calls.append((cmd, kwargs))
                if cmd[-1] == "--short":
                    return type("Result", (), {"returncode": 0, "stdout": " M agentmain.py\n", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": " agentmain.py | 1 +\n", "stderr": ""})()

            handler = self.make_handler(td, process_exists=lambda pid: False, sleep=lambda _: None, worktree_runner=fake_runner)

            outcome = exhaust(
                handler.do_close_agent(
                    {"target": "close_worktree", "reason": "done", "grace_seconds": 0, "cleanup_worktree": True},
                    None,
                )
            )

            self.assertEqual(outcome.data["status"], "closed")
            self.assertFalse(worktree.exists())
            self.assertEqual(outcome.data["closed_state"]["worktree_summary"]["changed_files"], ["agentmain.py"])
            self.assertEqual(outcome.data["closed_state"]["worktree_cleanup"]["status"], "removed")
            self.assertEqual(
                calls[-1][0],
                ["git", "-C", str(Path(td)), "worktree", "remove", "--force", str(worktree)],
            )

    def test_close_agent_tool_cascade_closes_descendants_and_reports_them(self):
        """The LLM has no other way to clean up a subtree: without cascade it must walk the
        tree itself, and it cannot see agents it did not personally spawn."""
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(
                td, process_exists=lambda _pid: True, terminate_process=lambda _pid: None, sleep=lambda _: None
            )
            manager = handler.subagent_manager
            manager.registry.max_depth = 0
            for parent, name, pid in (("/root", "lead", 11), ("/root/lead", "helper", 22), ("/root", "bystander", 33)):
                entry = manager.registry.create_child(
                    parent, name, Path(td) / "temp" / name, Path(td) / "temp" / name / "state.json", pid=pid
                )
                task_dir = Path(entry.task_dir)
                task_dir.mkdir(parents=True, exist_ok=True)
                (task_dir / "output.txt").write_text(f"{name} mid-turn", encoding="utf-8")
                atomic_write_json(
                    task_dir / "state.json",
                    {
                        "schema_version": 1,
                        "task_name": name,
                        "agent_path": str(entry.agent_path),
                        "run_id": entry.run_id,
                        "pid": pid,
                        "round": 0,
                        "turn_status": "running",
                        "process_status": "alive",
                        "output_path": str(task_dir / "output.txt"),
                    },
                )

            outcome = exhaust(
                handler.do_close_agent(
                    {"target": "/root/lead", "reason": "test_cleanup", "grace_seconds": 0, "cascade": True}, None
                )
            )

            self.assertEqual(outcome.data["status"], "closed")
            self.assertEqual([row["agent_path"] for row in outcome.data["closed_descendants"]], ["/root/lead/helper"])
            self.assertEqual(outcome.data["closed_descendants"][0]["status"], "closed")
            self.assertEqual(manager.registry.get("/root/lead/helper").status, "closed")
            self.assertEqual(manager.registry.get("/root/bystander").status, "running")

    def test_close_agent_tool_reports_an_empty_descendant_list_when_cascade_is_off(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "solo_worker"
            task_dir.mkdir(parents=True)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "solo_worker",
                    "agent_path": "/root/solo_worker",
                    "pid": None,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "waiting_reply",
                },
            )
            handler = self.make_handler(td, process_exists=lambda _pid: False, sleep=lambda _: None)

            outcome = exhaust(handler.do_close_agent({"target": "solo_worker", "grace_seconds": 0}, None))

            self.assertEqual(outcome.data["closed_descendants"], [])

    def test_read_agent_result_supports_artifact_id(self):
        with tempfile.TemporaryDirectory() as td:
            from subagent_artifacts import SubagentArtifactStore

            task_dir = Path(td) / "temp" / "artifact_worker"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("artifact result body\n\n[ROUND END]\n", encoding="utf-8")
            run_dir = Path(td) / "temp" / "subagents" / "runs" / "run_demo"
            artifact = SubagentArtifactStore(run_dir).record_final_output(output_path, round_no=0)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "artifact_worker",
                    "agent_path": "/root/artifact_worker",
                    "run_id": "run_demo",
                    "artifact_dir": str(run_dir),
                    "pid": 123,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "waiting_reply",
                    "output_path": str(output_path),
                    "final_output_path": str(output_path),
                    "final_output_ref": artifact["artifact_id"],
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            outcome = exhaust(
                handler.do_read_agent_result(
                    {
                        "target": "artifact_worker",
                        "artifact_id": artifact["artifact_id"],
                        "max_output_chars": 2000,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "success")
            self.assertEqual(outcome.data["agent"]["artifact_id"], "final_output_round_0")
            self.assertEqual(outcome.data["agent"]["final_output"], "artifact result body")

    def test_read_agent_result_can_include_transcript_replay_summary(self):
        with tempfile.TemporaryDirectory() as td:
            from subagent_transcript import SubagentTranscriptStore

            task_dir = Path(td) / "temp" / "replay_worker"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("replay result\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "replay_worker",
                    "agent_path": "/root/replay_worker",
                    "parent_session_id": "parent_session",
                    "run_id": "run_replay",
                    "pid": 123,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "waiting_reply",
                    "output_path": str(output_path),
                    "final_output_path": str(output_path),
                },
            )
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")
            store.write_metadata(session_id="parent_session", run_id="run_replay", agent_path="/root/replay_worker")
            store.append_event("parent_session", "run_replay", "request", {"prompt": "summarize"})
            store.append_event("parent_session", "run_replay", "assistant", {"content": "analysis"})
            store.append_event("parent_session", "run_replay", "final_output", {"artifact_id": "final_output_round_0", "final_output": "replay result"})

            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            outcome = exhaust(
                handler.do_read_agent_result(
                    {
                        "target": "replay_worker",
                        "include_transcript_replay": True,
                        "max_output_chars": 2000,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "success")
            replay = outcome.data["agent"]["transcript_replay"]
            self.assertEqual(replay["session_id"], "parent_session")
            self.assertEqual(replay["run_id"], "run_replay")
            self.assertEqual(replay["agent_path"], "/root/replay_worker")
            self.assertEqual(replay["request"]["prompt"], "summarize")
            self.assertEqual(replay["assistant_messages"][0]["content"], "analysis")
            self.assertEqual(replay["final_output"]["final_output"], "replay result")

    def test_read_agent_result_can_include_resume_context_from_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            from subagent_transcript import SubagentTranscriptStore

            task_dir = Path(td) / "temp" / "resume_worker"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("resume result\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "resume_worker",
                    "agent_path": "/root/resume_worker",
                    "parent_session_id": "parent_session",
                    "run_id": "run_resume_tool",
                    "pid": 123,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "waiting_reply",
                    "output_path": str(output_path),
                    "final_output_path": str(output_path),
                },
            )
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")
            store.write_metadata(session_id="parent_session", run_id="run_resume_tool", agent_path="/root/resume_worker")
            store.append_event("parent_session", "run_resume_tool", "request", {"prompt": "continue analysis"})
            store.append_event("parent_session", "run_resume_tool", "tool_call", {"tool_name": "file_read", "args": {"path": "README.md"}})
            store.append_event("parent_session", "run_resume_tool", "tool_result", {"tool_name": "file_read", "status": "success", "result": "README body"})
            store.append_event("parent_session", "run_resume_tool", "assistant", {"content": "observed README"})
            store.append_event("parent_session", "run_resume_tool", "final_output", {"final_output": "resume result"})

            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            outcome = exhaust(
                handler.do_read_agent_result(
                    {
                        "target": "resume_worker",
                        "include_resume_context": True,
                        "max_output_chars": 2000,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "success")
            resume = outcome.data["agent"]["resume_context"]
            self.assertEqual(resume["agent_path"], "/root/resume_worker")
            self.assertEqual(resume["request_prompt"], "continue analysis")
            self.assertEqual(resume["final_output"], "resume result")
            self.assertEqual(resume["backend_history"][0]["role"], "user")
            self.assertIn("continue analysis", resume["backend_history"][0]["content"])
            self.assertIn("file_read", resume["backend_history"][1]["content"])
            self.assertIn("README body", resume["backend_history"][2]["content"])

    def test_read_agent_result_can_include_replay_timeline_and_edited_resume_context(self):
        with tempfile.TemporaryDirectory() as td:
            from subagent_transcript import SubagentTranscriptStore

            task_dir = Path(td) / "temp" / "timeline_worker"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("timeline result\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "timeline_worker",
                    "agent_path": "/root/timeline_worker",
                    "parent_session_id": "parent_session",
                    "run_id": "run_timeline_tool",
                    "pid": 123,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "waiting_reply",
                    "output_path": str(output_path),
                    "final_output_path": str(output_path),
                },
            )
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")
            store.write_metadata(session_id="parent_session", run_id="run_timeline_tool", agent_path="/root/timeline_worker")
            store.append_event("parent_session", "run_timeline_tool", "request", {"prompt": "original prompt"})
            store.append_event("parent_session", "run_timeline_tool", "assistant", {"content": "old analysis"})
            store.append_event("parent_session", "run_timeline_tool", "final_output", {"final_output": "timeline result"})
            timeline = store.build_replay_timeline("parent_session", "run_timeline_tool")

            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)
            outcome = exhaust(
                handler.do_read_agent_result(
                    {
                        "target": "timeline_worker",
                        "include_transcript_timeline": True,
                        "include_resume_context": True,
                        "resume_context_edits": {
                            timeline[1]["event_key"]: {"resume_content": "edited prompt"},
                            timeline[2]["event_key"]: {"drop": True},
                        },
                        "max_output_chars": 2000,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "success")
            agent = outcome.data["agent"]
            self.assertEqual(agent["transcript_timeline"][1]["resume_content"], "original prompt")
            self.assertTrue(agent["transcript_timeline"][1]["editable"])
            self.assertEqual(agent["resume_context"]["backend_history"][0]["content"], "edited prompt")
            self.assertNotIn("old analysis", json.dumps(agent["resume_context"]["backend_history"], ensure_ascii=False))
            self.assertEqual(agent["resume_context"]["applied_edit_count"], 2)

    def test_resume_agent_tool_restarts_closed_subagent_from_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            from subagent_transcript import SubagentTranscriptStore

            calls = []

            class FakeProcess:
                pid = 9090

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            task_dir = Path(td) / "temp" / "resume_tool"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("old tool answer\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "resume_tool",
                    "agent_path": "/root/resume_tool",
                    "parent_session_id": "parent_session",
                    "run_id": "run_resume_tool_exec",
                    "pid": None,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "shutdown",
                    "output_path": str(output_path),
                    "final_output_path": str(output_path),
                    "permission_profile": "read_only",
                    "parent_permission_mode": "read_only",
                    "permission_options": {},
                    "llm_no": 1,
                    "verbose": False,
                },
            )
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")
            store.write_metadata(session_id="parent_session", run_id="run_resume_tool_exec", agent_path="/root/resume_tool")
            store.append_event("parent_session", "run_resume_tool_exec", "request", {"prompt": "old tool task"})
            store.append_event("parent_session", "run_resume_tool_exec", "assistant", {"content": "old tool analysis"})
            store.append_event("parent_session", "run_resume_tool_exec", "final_output", {"final_output": "old tool answer"})
            handler = self.make_handler(td, popen=fake_popen, python_executable="python-test", process_exists=lambda pid: False, sleep=lambda _: None)
            entry = handler.subagent_manager.registry.create_child(
                parent_path="/root",
                task_name="resume_tool",
                task_dir=task_dir,
                state_path=task_dir / "state.json",
                parent_session_id="parent_session",
                permission_profile="read_only",
                parent_permission_mode="read_only",
            )
            handler.subagent_manager.registry.update(entry.agent_path, run_id="run_resume_tool_exec")
            handler.subagent_manager.registry.mark_closed(entry.agent_path, previous_status="completed", closed_status="shutdown")

            outcome = exhaust(
                handler.do_resume_agent(
                    {
                        "target": "resume_tool",
                        "message": "continue via tool",
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "resumed")
            self.assertEqual(outcome.data["target"], "/root/resume_tool")
            self.assertEqual(outcome.data["handle"]["pid"], 9090)
            self.assertEqual(outcome.data["handle"]["run_id"], "run_resume_tool_exec")
            self.assertEqual(outcome.data["resume_context"]["request_prompt"], "old tool task")
            self.assertEqual((task_dir / "input.txt").read_text(encoding="utf-8"), "continue via tool")
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["round"], 1)
            self.assertEqual(Path(state["output_path"]), task_dir / "output1.txt")
            self.assertEqual(calls[0][0][0], "python-test")

    def test_foreground_background_tools_request_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "handoff_tool"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("ready\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "handoff_tool",
                    "agent_path": "/root/handoff_tool",
                    "run_id": "run_handoff_tool",
                    "pid": None,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "background": True,
                    "output_path": str(output_path),
                    "final_output_path": None,
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: False, sleep=lambda _: None)

            foreground = exhaust(handler.do_foreground_agent({"target": "handoff_tool", "reason": "inspect"}, None))
            background = exhaust(handler.do_background_agent({"target": "handoff_tool", "reason": "queue"}, None))

            self.assertEqual(foreground.data["status"], "handoff_requested")
            self.assertEqual(foreground.data["handoff_mode"], "foreground")
            self.assertTrue(foreground.data["previous_state"]["background"])
            self.assertFalse(foreground.data["updated_state"]["background"])
            self.assertEqual(background.data["handoff_mode"], "background")
            self.assertFalse(background.data["previous_state"]["background"])
            self.assertTrue(background.data["updated_state"]["background"])
            self.assertEqual(background.data["updated_state"]["handoff_reason"], "queue")

    def test_attach_agent_tool_streams_output_slice_and_detaches(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "attach_tool"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("live chunk alpha\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "attach_tool",
                    "agent_path": "/root/attach_tool",
                    "run_id": "run_attach_tool",
                    "pid": None,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "background": True,
                    "output_path": str(output_path),
                    "final_output_path": None,
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: False, sleep=lambda _: None)

            attached = exhaust(handler.do_attach_agent({"target": "attach_tool", "max_chars": 4, "reason": "watch"}, None))
            resumed_stream = exhaust(handler.do_attach_agent({"target": "attach_tool", "since_offset": attached.data["next_stream_offset"]}, None))
            detached = exhaust(handler.do_detach_agent({"target": "attach_tool", "reason": "queue"}, None))

            self.assertEqual(attached.data["status"], "attached")
            self.assertEqual(attached.data["handoff_mode"], "foreground")
            self.assertEqual(attached.data["stream_text"], "live")
            self.assertTrue(attached.data["stream_truncated"])
            self.assertEqual(attached.data["next_stream_offset"], 4)
            self.assertFalse(attached.data["state"]["background"])
            self.assertEqual(resumed_stream.data["stream_text"], " chunk alpha\n")
            self.assertFalse(resumed_stream.data["stream_truncated"])
            self.assertEqual(detached.data["status"], "detached")
            self.assertEqual(detached.data["handoff_mode"], "background")
            self.assertTrue(detached.data["state"]["background"])
            self.assertEqual(detached.data["state"]["attach_status"], "detached")

    def test_tool_schemas_include_subagent_tools(self):
        expected = {"spawn_agent", "list_agents", "wait_agent", "read_agent_result", "resume_agent", "send_message", "followup_task", "foreground_agent", "background_agent", "attach_agent", "detach_agent", "interrupt_agent", "close_agent"}
        for schema_name in ["tools_schema.json", "tools_schema_cn.json"]:
            with self.subTest(schema_name=schema_name):
                raw = json.loads((REPO_ROOT / "assets" / schema_name).read_text(encoding="utf-8"))
                names = {item["function"]["name"] for item in raw}
                self.assertTrue(expected.issubset(names))
                wait_schema = next(item["function"] for item in raw if item["function"]["name"] == "wait_agent")
                self.assertNotIn("poll_interval_seconds", wait_schema["parameters"]["properties"])
                read_schema = next(item["function"] for item in raw if item["function"]["name"] == "read_agent_result")
                self.assertIn("artifact_id", read_schema["parameters"]["properties"])
                self.assertIn("include_transcript_replay", read_schema["parameters"]["properties"])
                self.assertIn("include_resume_context", read_schema["parameters"]["properties"])
                self.assertIn("include_transcript_timeline", read_schema["parameters"]["properties"])
                self.assertIn("resume_context_edits", read_schema["parameters"]["properties"])
                close_schema = next(item["function"] for item in raw if item["function"]["name"] == "close_agent")
                self.assertIn("cleanup_worktree", close_schema["parameters"]["properties"])
                self.assertIn("cascade", close_schema["parameters"]["properties"])
                spawn_schema = next(item["function"] for item in raw if item["function"]["name"] == "spawn_agent")
                properties = spawn_schema["parameters"]["properties"]
                for prop in ["agent_type", "background", "isolation", "ipc_mode"]:
                    self.assertIn(prop, properties)

    def test_spawn_agent_tool_reports_a_live_name_conflict_as_actionable_guidance(self):
        """The refusal message is written for the model, so it must arrive without traceback noise.

        `format_error` appends ``@ file:line, func -> `source``` to every exception it formats.
        For a genuine crash that is the useful part; for this refusal the useful part is
        "use followup_task / close_agent / another task_name", and the file/line suffix invites
        the model to treat a deliberate policy answer as a GA bug and retry with a mangled name.
        """
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(
                td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 4242})(),
                python_executable="python-test",
                # Liveness is what separates "refuse" from "rename", so the test owns it instead
                # of depending on whether pid 4242 happens to exist on the host.
                process_exists=lambda _pid: True,
            )

            first = exhaust(handler.do_spawn_agent({"task_name": "reviewer", "message": "review once"}, response=None))
            second = exhaust(handler.do_spawn_agent({"task_name": "reviewer", "message": "review again"}, response=None))

            self.assertEqual(first.data["status"], "started")
            self.assertEqual(second.data["status"], "error")
            self.assertEqual(second.data["reason"], "name_conflict")
            self.assertEqual(second.data["agent_path"], "/root/reviewer")
            msg = second.data["msg"]
            self.assertIn("followup_task", msg)
            self.assertIn("close_agent", msg)
            self.assertNotIn("subagent_registry.py:", msg)
            self.assertNotIn("SubagentNameConflictError:", msg)
            self.assertNotIn(" -> `", msg)

    def test_spawn_agent_tool_still_reports_other_failures_with_traceback_context(self):
        """Only the name conflict loses the file/line suffix; real crashes still need it."""
        with tempfile.TemporaryDirectory() as td:

            def exploding_popen(*_args, **_kwargs):
                raise OSError("popen exploded")

            handler = self.make_handler(td, popen=exploding_popen, python_executable="python-test")

            outcome = exhaust(handler.do_spawn_agent({"task_name": "boom", "message": "go"}, response=None))

            self.assertEqual(outcome.data["status"], "error")
            self.assertNotIn("reason", outcome.data)
            self.assertIn("popen exploded", outcome.data["msg"])
            self.assertIn(" -> `", outcome.data["msg"])

    def test_resume_agent_tool_reports_a_live_agent_as_actionable_guidance(self):
        """Same reasoning as the spawn conflict: this is a policy answer, not a traceback."""
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(
                td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 4343})(),
                python_executable="python-test",
                process_exists=lambda _pid: True,
            )
            exhaust(handler.do_spawn_agent({"task_name": "worker", "message": "start"}, response=None))

            outcome = exhaust(handler.do_resume_agent({"target": "worker", "message": "keep going"}, response=None))

            self.assertEqual(outcome.data["status"], "error")
            self.assertEqual(outcome.data["reason"], "name_conflict")
            self.assertEqual(outcome.data["agent_path"], "/root/worker")
            self.assertIn("followup_task", outcome.data["msg"])
            self.assertNotIn(" -> `", outcome.data["msg"])

    def test_tool_schemas_tell_the_model_when_a_submission_id_is_required(self):
        """The dedup only fires when the model supplies the id, so the schema has to demand it.

        GA has no auto-generated submission id (agent_loop has no replay path, so an id derived
        there would differ on the only real duplication path: the model re-emitting the call in a
        new turn). That makes the wording the entire enforcement mechanism — "Optional idempotency
        key" reads as "skip me". Codex gets `item_id` supplied by stating the obligation.
        """
        required_by_schema = {
            "tools_schema.json": ["Set this whenever you retry", "not sure whether the previous call took effect"],
            "tools_schema_cn.json": ["重试", "不确定上一次调用是否生效"],
        }
        for schema_name, phrases in required_by_schema.items():
            with self.subTest(schema_name=schema_name):
                raw = json.loads((REPO_ROOT / "assets" / schema_name).read_text(encoding="utf-8"))
                by_name = {item["function"]["name"]: item["function"] for item in raw}
                for tool in ["spawn_agent", "send_message", "followup_task", "close_agent", "resume_agent"]:
                    properties = by_name[tool]["parameters"]["properties"]
                    self.assertIn("submission_id", properties, f"{tool} cannot be made idempotent")
                    description = properties["submission_id"]["description"]
                    for phrase in phrases:
                        self.assertIn(phrase, description, f"{tool} does not tell the model when to set it")

    def test_tool_schemas_teach_codex_style_subagent_delegation(self):
        required_by_schema = {
            "tools_schema.json": [
                "critical path",
                "concrete, bounded, and self-contained",
                "Do not redo delegated subagent tasks yourself",
                "Call wait_agent sparingly",
                "final answer contract",
            ],
            "tools_schema_cn.json": [
                "关键路径",
                "具体、有边界、自包含",
                "不要重复执行已经委派给子智能体的任务",
                "谨慎调用 wait_agent",
                "最终结果契约",
            ],
        }
        for schema_name, required_phrases in required_by_schema.items():
            with self.subTest(schema_name=schema_name):
                raw = json.loads((REPO_ROOT / "assets" / schema_name).read_text(encoding="utf-8"))
                spawn_schema = next(item["function"] for item in raw if item["function"]["name"] == "spawn_agent")
                schema_text = json.dumps(spawn_schema, ensure_ascii=False)
                for phrase in required_phrases:
                    self.assertIn(phrase, schema_text)


if __name__ == "__main__":
    unittest.main()
