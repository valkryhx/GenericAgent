import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from genericagent.core.handler import GenericAgentHandler
from genericagent.core.loop import exhaust
from genericagent.hooks.config import HookRegistry
from genericagent.hooks.models import HookEntry, HookHandler
from genericagent.hooks.runtime import HookRuntime


class RecordingHookRuntime(HookRuntime):
    def __init__(self, outputs=None):
        super().__init__(HookRegistry())
        self.events = []
        self.outputs = outputs or {}

    def run_event(self, agent, event, data=None, *, emit=None):
        from genericagent.hooks.models import HookAggregate, HookResult
        self.events.append((event, dict(data or {})))
        payload = data or {}
        output = self.outputs.get(event)
        if output is None:
            return HookAggregate(event=event)
        handler = HookHandler("command", {"command": "test"})
        result = HookResult(event, handler, "test", "test", "h", "success", output=output)
        aggregate = HookAggregate(event=event)
        aggregate.add(result)
        return aggregate


class HooksToolRuntimeTests(unittest.TestCase):
    def make_handler(self, td, runtime):
        parent = SimpleNamespace(task_dir=str(Path(td) / "temp"), permission_mode="full_access")
        handler = GenericAgentHandler(parent, last_history=[], cwd=str(Path(td) / "temp"), workspace_root=str(Path(td) / "workspace"))
        handler.hook_runtime = runtime
        handler.hook_parent_agent = parent
        Path(td, "workspace").mkdir(exist_ok=True)
        Path(td, "temp").mkdir(exist_ok=True)
        return handler

    def test_pre_tool_updated_input_is_rechecked_by_path_acl(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PreToolUse": {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": {"path": "../outside.txt"},
                }
            }})
            handler = self.make_handler(td, runtime)
            outcome = exhaust(handler.dispatch("file_read", {"path": "inside.txt"}, None))
            self.assertEqual(outcome.data["status"], "error")
            self.assertIn("path_acl", outcome.data)
            events = [name for name, _ in runtime.events]
            self.assertIn("PreToolUse", events)
            self.assertIn("PostToolUseFailure", events)

    def test_pre_tool_deny_emits_permission_denied_and_does_not_execute(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PreToolUse": {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "blocked by policy",
                }
            }})
            handler = self.make_handler(td, runtime)
            outcome = exhaust(handler.dispatch("file_read", {"path": "inside.txt"}, None))
            self.assertEqual(outcome.data["status"], "error")
            events = [name for name, _ in runtime.events]
            self.assertIn("PreToolUse", events)
            self.assertIn("PermissionDenied", events)

    def test_post_tool_updated_tool_output_replaces_outcome_data(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PostToolUse": {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": {"status": "success", "value": "rewritten"},
                }
            }})
            handler = self.make_handler(td, runtime)
            # file_read of an existing file returns its real result, then the
            # post hook replacement is the authoritative model-visible result.
            Path(td, "workspace", "inside.txt").write_text("original", encoding="utf-8")
            outcome = exhaust(handler.dispatch("file_read", {"path": "inside.txt"}, None))
            self.assertEqual(outcome.data, {"status": "success", "value": "rewritten"})
            events = [name for name, _ in runtime.events]
            self.assertIn("PostToolUse", events)

    def test_hook_context_is_appended_to_next_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PostToolUse": {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "remember this",
                }
            }})
            handler = self.make_handler(td, runtime)
            Path(td, "workspace", "inside.txt").write_text("original", encoding="utf-8")
            outcome = exhaust(handler.dispatch("file_read", {"path": "inside.txt"}, None))
            self.assertIn("<hook_context>", outcome.next_prompt or "")
            self.assertIn("remember this", outcome.next_prompt or "")


if __name__ == "__main__":
    unittest.main()

class WorkflowChildHookScopeTests(unittest.TestCase):
    def test_workflow_child_uses_job_scoped_hook_proxy_not_leader_agent(self):
        from genericagent.hooks.config import HookRegistry
        from genericagent.hooks.runtime import HookRuntime
        from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
        from genericagent.workflow.models import WorkflowJob

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            runtime = HookRuntime(HookRegistry())
            leader = SimpleNamespace(
                hook_runtime=runtime,
                hook_emit=lambda event: None,
                session_id="session_1",
                session_path=str(Path(td) / "session.jsonl"),
                permission_mode="full_access",
                history=["LEADER_ONLY"],
                task_dir=str(Path(td) / "leader-task"),
            )
            runner = NativeGPTChildAgentRunner()
            runner.hook_agent = leader
            job = WorkflowJob(
                job_id="agent_1",
                prompt="inspect",
                metadata={"runId": "wf_test", "workspacePath": str(workspace)},
            )
            handler = runner._build_handler(job, [], "read_only", "v1")
            proxy = handler.hook_parent_agent
            self.assertIsNot(proxy, leader)
            self.assertIs(proxy.hook_runtime, runtime)
            self.assertEqual(proxy.agent_type, "workflow_child")
            self.assertEqual(proxy.agent_id, "wf_test:agent_1")
            self.assertEqual(proxy.history if hasattr(proxy, "history") else None, None)
            self.assertEqual(str(proxy.task_dir), str(workspace))
