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

    def test_permission_request_allow_skips_user_approval_and_runs_tool(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PermissionRequest": {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }})
            handler = self.make_handler(td, runtime)
            from genericagent.core.permissions.policy import PermissionModePolicy
            handler.permission_mode_policy = PermissionModePolicy("ask")
            handler.permission_runtime = SimpleNamespace(
                wait_for_decision=lambda *a, **k: self.fail("user approval should be skipped")
            )
            Path(td, "workspace", "inside.txt").write_text("allowed", encoding="utf-8")
            outcome = exhaust(handler.dispatch("file_write", {"path": "inside.txt", "content": "ok"}, None))
            self.assertEqual(outcome.data["status"], "success")
            self.assertEqual(Path(td, "workspace", "inside.txt").read_text(encoding="utf-8"), "ok")
            self.assertIn("PermissionRequest", [name for name, _ in runtime.events])

    def test_permission_request_deny_does_not_execute_and_emits_denied(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PermissionRequest": {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": "blocked by hook"},
                }
            }})
            handler = self.make_handler(td, runtime)
            from genericagent.core.permissions.policy import PermissionModePolicy
            handler.permission_mode_policy = PermissionModePolicy("ask")
            handler.permission_runtime = SimpleNamespace(
                wait_for_decision=lambda *a, **k: self.fail("deny should skip user approval")
            )
            outcome = exhaust(handler.dispatch("file_write", {"path": "inside.txt", "content": "no"}, None))
            self.assertEqual(outcome.data["status"], "error")
            self.assertIn("blocked by hook", outcome.data["permission"]["reason"])
            self.assertFalse(Path(td, "workspace", "inside.txt").exists())
            events = [name for name, _ in runtime.events]
            self.assertIn("PermissionRequest", events)
            self.assertIn("PermissionDenied", events)

    def test_permission_request_updated_input_is_rechecked_by_path_acl(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PermissionRequest": {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "allow",
                        "updatedInput": {"path": "../outside.txt", "content": "escape"},
                    },
                }
            }})
            handler = self.make_handler(td, runtime)
            from genericagent.core.permissions.policy import PermissionModePolicy
            handler.permission_mode_policy = PermissionModePolicy("ask")
            handler.permission_runtime = SimpleNamespace(
                wait_for_decision=lambda *a, **k: self.fail("updated input should be rejected before approval")
            )
            outcome = exhaust(handler.dispatch("file_write", {"path": "inside.txt", "content": "safe"}, None))
            self.assertEqual(outcome.data["status"], "error")
            self.assertIn("path_acl", outcome.data)
            self.assertFalse(Path(td, "outside.txt").exists())

    def test_permission_request_without_verdict_still_uses_user_approval(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime()
            handler = self.make_handler(td, runtime)
            from genericagent.core.permissions.policy import PermissionModePolicy
            from genericagent.core.permissions.runtime import ACCEPT
            handler.permission_mode_policy = PermissionModePolicy("ask")
            seen = {}
            def wait(tool_name, args, reason, **kwargs):
                seen.update(tool_name=tool_name, reason=reason)
                return ACCEPT
            handler.permission_runtime = SimpleNamespace(wait_for_decision=wait)
            outcome = exhaust(handler.dispatch("file_write", {"path": "inside.txt", "content": "ok"}, None))
            self.assertEqual(outcome.data["status"], "success")
            self.assertEqual(seen["tool_name"], "file_write")

    def test_workflow_permission_request_allow_replaces_blocking_ask(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime({"PermissionRequest": {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }})
            handler = self.make_handler(td, runtime)
            from genericagent.workflow.permissions import ToolPermissionPolicy, EXPLICIT_APPROVAL
            handler.workflow_permission_policy = ToolPermissionPolicy(profile=EXPLICIT_APPROVAL)
            outcome = exhaust(handler.dispatch("file_write", {"path": "inside.txt", "content": "allowed"}, None))
            self.assertEqual(outcome.data.get("status"), "success")
            self.assertEqual(Path(td, "workspace", "inside.txt").read_text(encoding="utf-8"), "allowed")

    def test_workflow_permission_request_without_verdict_keeps_approval_required(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = RecordingHookRuntime()
            handler = self.make_handler(td, runtime)
            from genericagent.workflow.permissions import ToolPermissionPolicy, EXPLICIT_APPROVAL
            handler.workflow_permission_policy = ToolPermissionPolicy(profile=EXPLICIT_APPROVAL)
            outcome = exhaust(handler.dispatch("file_read", {"path": "inside.txt"}, None))
            self.assertEqual(outcome.data.get("status"), "approval_required")
            self.assertIn("PermissionRequest", [name for name, _ in runtime.events])

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
