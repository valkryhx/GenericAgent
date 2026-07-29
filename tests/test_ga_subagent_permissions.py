import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import run_task_worker_loop  # noqa: E402
from agent_loop import StepOutcome, exhaust  # noqa: E402
from ga import GenericAgentHandler  # noqa: E402
from subagent_permissions import (  # noqa: E402
    EXPLICIT_APPROVAL,
    INHERIT_CURRENT_PERMISSIONS,
    READ_ONLY,
    RESTRICTED_MCP,
    build_subagent_permission_policy,
    normalize_permission_metadata,
)


class SubagentPermissionPolicyTest(unittest.TestCase):
    def test_normalize_defaults_to_inherit_current_permissions(self):
        metadata = normalize_permission_metadata({})

        self.assertEqual(metadata["permission_profile"], INHERIT_CURRENT_PERMISSIONS)
        self.assertEqual(metadata["options"], {})

    def test_inherit_current_permissions_uses_parent_read_only_mode(self):
        policy = build_subagent_permission_policy(
            {
                "permission_profile": INHERIT_CURRENT_PERMISSIONS,
                "parent_permission_mode": "read_only",
            }
        )

        self.assertEqual(policy.evaluate("file_read", {}).action, "allow")
        decision = policy.evaluate("file_write", {})
        self.assertEqual(decision.action, "deny")
        self.assertEqual(decision.details["parent_permission_mode"], "read_only")

    def test_inherit_current_permissions_uses_parent_ask_mode_without_running_mutations(self):
        policy = build_subagent_permission_policy(
            {
                "permission_profile": INHERIT_CURRENT_PERMISSIONS,
                "parent_permission_mode": "ask",
            }
        )

        self.assertEqual(policy.evaluate("file_read", {}).action, "allow")
        decision = policy.evaluate("file_write", {})
        self.assertEqual(decision.action, "ask")
        self.assertEqual(decision.details["parent_permission_mode"], "ask")

    def test_inherit_current_permissions_full_access_allows_default_tools(self):
        policy = build_subagent_permission_policy(
            {
                "permission_profile": INHERIT_CURRENT_PERMISSIONS,
                "parent_permission_mode": "full_access",
            }
        )

        decision = policy.evaluate("file_write", {})
        self.assertEqual(decision.action, "allow")
        self.assertEqual(decision.details["parent_permission_mode"], "full_access")

    def test_read_only_allows_reads_and_denies_writes(self):
        policy = build_subagent_permission_policy({"permission_profile": READ_ONLY})

        self.assertEqual(policy.evaluate("file_read", {}).action, "allow")
        for tool_name in ["file_write", "file_patch", "code_run"]:
            with self.subTest(tool_name=tool_name):
                decision = policy.evaluate(tool_name, {})
                self.assertEqual(decision.action, "deny")
                self.assertEqual(decision.profile, READ_ONLY)

    def test_explicit_approval_returns_ask_without_waiting_for_ui(self):
        policy = build_subagent_permission_policy({"permission_profile": EXPLICIT_APPROVAL})

        decision = policy.evaluate("file_write", {})

        self.assertEqual(decision.action, "ask")
        self.assertEqual(decision.reason, "explicit_approval_required")

    def test_restricted_mcp_honors_allow_and_deny_options(self):
        policy = build_subagent_permission_policy(
            {
                "permission_profile": RESTRICTED_MCP,
                "allowed_mcp_servers": ["memory"],
                "denied_mcp_tools": ["mcp__memory__delete_entities"],
            }
        )

        self.assertEqual(policy.evaluate("file_write", {}).action, "allow")
        self.assertEqual(policy.evaluate("mcp__memory__search_nodes", {}).action, "allow")
        denied = policy.evaluate("mcp__memory__delete_entities", {})
        self.assertEqual(denied.action, "deny")
        self.assertEqual(denied.reason, "restricted_mcp_tool_denied")
        self.assertEqual(policy.evaluate("mcp__other__read", {}).action, "deny")


class SubagentWorkerPermissionLoadTest(unittest.TestCase):
    def test_worker_loads_permission_policy_from_state_before_turn(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "readonly_worker"
            task_dir.mkdir(parents=True)
            (task_dir / "input.txt").write_text("inspect", encoding="utf-8")
            (task_dir / "state.json").write_text(
                '{"permission_profile":"read_only","permission_options":{}}',
                encoding="utf-8",
            )
            observed = {}

            class DoneQueue:
                def get(self, timeout=None):
                    return {"done": "ok"}

            class FakeAgent:
                peer_hint = True
                task_dir = None
                llmclient = SimpleNamespace(backend=SimpleNamespace(history=[]))

                def put_task(self, raw, source="task"):
                    observed["raw"] = raw
                    observed["policy_decision"] = self.subagent_permission_policy.evaluate("file_write", {}).action
                    return DoneQueue()

            run_task_worker_loop(
                FakeAgent(),
                task_dir,
                reply_wait_iterations=0,
                reply_sleep_s=0,
                sleep_fn=lambda _: None,
            )

            self.assertEqual(observed["raw"], "inspect")
            self.assertEqual(observed["policy_decision"], "deny")


class ParentStub:
    task_dir = ""
    verbose = False
    stop_sig = False

    def __init__(self):
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=[]))


class SpyHandler(GenericAgentHandler):
    def __init__(self):
        super().__init__(ParentStub())
        self.calls = []

    def do_file_write(self, args, response):
        self.calls.append("tool:file_write")
        return StepOutcome({"status": "success"}, next_prompt="\n")

    def do_file_read(self, args, response):
        self.calls.append("tool:file_read")
        return StepOutcome("read", next_prompt="\n")


class SubagentPermissionDispatchTest(unittest.TestCase):
    def response(self):
        return SimpleNamespace(content="")

    def test_handler_read_only_policy_blocks_write_without_workflow_policy(self):
        handler = SpyHandler()
        handler.subagent_permission_policy = build_subagent_permission_policy({"permission_profile": READ_ONLY})

        allowed = exhaust(handler.dispatch("file_read", {}, self.response()))
        denied = exhaust(handler.dispatch("file_write", {}, self.response()))

        self.assertEqual("read", allowed.data)
        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("error", denied.data["status"])
        self.assertEqual("deny", denied.data["permission"]["action"])
        self.assertEqual(READ_ONLY, denied.data["permission"]["profile"])

    def test_handler_explicit_approval_returns_approval_required_without_hanging(self):
        handler = SpyHandler()
        handler.subagent_permission_policy = build_subagent_permission_policy({"permission_profile": EXPLICIT_APPROVAL})

        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))

        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("approval_required", outcome.data["status"])
        self.assertEqual("ask", outcome.data["permission"]["action"])


if __name__ == "__main__":
    unittest.main()
