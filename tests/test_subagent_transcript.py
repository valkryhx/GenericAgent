import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_transcript import SubagentTranscriptStore  # noqa: E402


class SubagentTranscriptStoreTest(unittest.TestCase):
    def test_spawn_writes_metadata_and_events_redacting_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")

            store.write_metadata(session_id="parent", run_id="run_1", agent_path="/root/demo")
            store.append_event("parent", "run_1", "request", {"prompt": "token=secret_value api_key=abc123"})
            store.append_event("parent", "run_1", "final_output", {"path": "output.txt"})

            meta = json.loads((Path(td) / "temp" / "sessions" / "parent" / "subagents" / "run_1.meta.json").read_text(encoding="utf-8"))
            rows = [
                json.loads(line)
                for line in (Path(td) / "temp" / "sessions" / "parent" / "subagents" / "run_1.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(meta["agent_path"], "/root/demo")
            self.assertEqual([row["type"] for row in rows], ["metadata", "request", "final_output"])
            self.assertNotIn("secret_value", json.dumps(rows, ensure_ascii=False))
            self.assertNotIn("abc123", json.dumps(rows, ensure_ascii=False))
    def test_replay_reconstructs_sidechain_summary_from_events(self):
        with tempfile.TemporaryDirectory() as td:
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")

            store.write_metadata(session_id="parent", run_id="run_1", agent_path="/root/demo")
            store.append_event("parent", "run_1", "request", {"prompt": "inspect repo"})
            store.append_event("parent", "run_1", "permission_decision", {"tool_name": "file_read", "action": "allow"})
            store.append_event("parent", "run_1", "tool_call", {"tool_name": "file_read", "args": {"path": "README.md"}})
            store.append_event("parent", "run_1", "tool_result", {"tool_name": "file_read", "status": "success"})
            store.append_event("parent", "run_1", "assistant", {"content": "analysis done"})
            store.append_event("parent", "run_1", "final_output", {"artifact_id": "final_output_round_0", "final_output": "done"})
            store.append_event("parent", "run_1", "agent_closed", {"closed_process_status": "shutdown", "reason": "done"})
            events_path = Path(td) / "temp" / "sessions" / "parent" / "subagents" / "run_1.jsonl"
            with open(events_path, "a", encoding="utf-8") as f:
                f.write("{not json}\n")

            summary = store.replay("parent", "run_1")

            self.assertEqual(summary["session_id"], "parent")
            self.assertEqual(summary["run_id"], "run_1")
            self.assertEqual(summary["agent_path"], "/root/demo")
            self.assertEqual(summary["event_count"], 8)
            self.assertEqual(summary["invalid_event_count"], 1)
            self.assertEqual(summary["request"]["prompt"], "inspect repo")
            self.assertEqual(summary["permission_decisions"][0]["action"], "allow")
            self.assertEqual(summary["tool_calls"][0]["tool_name"], "file_read")
            self.assertEqual(summary["tool_results"][0]["status"], "success")
            self.assertEqual(summary["assistant_messages"][0]["content"], "analysis done")
            self.assertEqual(summary["final_output"]["artifact_id"], "final_output_round_0")
            self.assertEqual(summary["closed"]["closed_process_status"], "shutdown")
            self.assertEqual(summary["last_event_type"], "agent_closed")

    def test_build_resume_context_projects_replay_into_backend_history(self):
        with tempfile.TemporaryDirectory() as td:
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")

            store.write_metadata(session_id="parent", run_id="run_resume", agent_path="/root/resume")
            store.append_event("parent", "run_resume", "request", {"prompt": "continue analysis"})
            store.append_event("parent", "run_resume", "tool_call", {"tool_name": "file_read", "args": {"path": "README.md"}})
            store.append_event("parent", "run_resume", "tool_result", {"tool_name": "file_read", "status": "success", "result": "README body"})
            store.append_event("parent", "run_resume", "assistant", {"content": "observed README"})
            store.append_event("parent", "run_resume", "final_output", {"final_output": "done"})
            store.append_event("parent", "run_resume", "agent_closed", {"closed_process_status": "shutdown"})

            context = store.build_resume_context("parent", "run_resume")

            self.assertEqual(context["status"], "terminal")
            self.assertEqual(context["agent_path"], "/root/resume")
            self.assertEqual(context["request_prompt"], "continue analysis")
            self.assertEqual(context["final_output"], "done")
            self.assertEqual([row["role"] for row in context["backend_history"]], ["user", "assistant", "user", "assistant", "assistant"])
            self.assertIn("continue analysis", context["backend_history"][0]["content"])
            self.assertIn("file_read", context["backend_history"][1]["content"])
            self.assertIn("README body", context["backend_history"][2]["content"])
            self.assertEqual(context["backend_history"][3]["content"], "observed README")
            self.assertIn("done", context["backend_history"][4]["content"])
            self.assertFalse(context["can_continue_turn"])
    def test_replay_timeline_supports_editable_resume_projection(self):
        with tempfile.TemporaryDirectory() as td:
            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")

            store.write_metadata(session_id="parent", run_id="run_timeline", agent_path="/root/timeline")
            store.append_event("parent", "run_timeline", "request", {"prompt": "original task"})
            store.append_event("parent", "run_timeline", "tool_call", {"tool_name": "file_read", "args": {"path": "README.md"}})
            store.append_event("parent", "run_timeline", "tool_result", {"tool_name": "file_read", "status": "success", "result": "old README"})
            store.append_event("parent", "run_timeline", "assistant", {"content": "old analysis"})
            store.append_event("parent", "run_timeline", "final_output", {"final_output": "old final"})

            timeline = store.build_replay_timeline("parent", "run_timeline")
            projection = store.build_resume_context(
                "parent",
                "run_timeline",
                edits={
                    timeline[1]["event_key"]: {"resume_content": "edited task"},
                    timeline[3]["event_key"]: {"drop": True},
                    timeline[4]["event_key"]: {"resume_content": "edited analysis"},
                },
            )

            self.assertEqual([item["index"] for item in timeline], list(range(len(timeline))))
            self.assertEqual(timeline[0]["type"], "metadata")
            self.assertFalse(timeline[0]["editable"])
            self.assertEqual(timeline[1]["resume_role"], "user")
            self.assertEqual(timeline[1]["resume_content"], "original task")
            self.assertEqual(timeline[2]["resume_role"], "assistant")
            self.assertIn("file_read", timeline[2]["resume_content"])
            self.assertEqual(timeline[3]["resume_role"], "user")
            self.assertIn("old README", timeline[3]["resume_content"])
            self.assertEqual(timeline[4]["resume_role"], "assistant")
            self.assertEqual(timeline[5]["resume_role"], "assistant")
            self.assertIn("GA_SUBAGENT_FINAL_OUTPUT", timeline[5]["resume_content"])
            self.assertEqual([row["content"] for row in projection["backend_history"]], [
                "edited task",
                timeline[2]["resume_content"],
                "edited analysis",
                timeline[5]["resume_content"],
            ])
            self.assertEqual(projection["timeline_event_count"], len(timeline))
            self.assertEqual(projection["applied_edit_count"], 3)

