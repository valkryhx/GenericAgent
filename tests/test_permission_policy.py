import unittest
from types import SimpleNamespace

from agent_loop import StepOutcome, exhaust
from ga import GenericAgentHandler
from permission_policy import (
    ASK,
    DEFAULT_PERMISSION_MODE,
    FULL_ACCESS,
    PERMISSION_MODES,
    READ_ONLY,
    PermissionModePolicy,
    build_permission_mode_policy,
    classify_tool,
    normalize_permission_mode,
)


READ_TOOLS = ["file_read", "web_scan", "no_tool", "ask_user", "load_skill", "list_agents", "status_report"]
MUTATING_TOOLS = ["file_write", "file_patch", "code_run", "web_execute_js", "spawn_agent"]
MCP_READ = "mcp__memory__search_nodes"
MCP_WRITE = "mcp__memory__delete_entities"


class NormalizeModeTest(unittest.TestCase):
    def test_default_mode_is_full_access(self):
        self.assertEqual(FULL_ACCESS, DEFAULT_PERMISSION_MODE)

    def test_known_modes_pass_through(self):
        for mode in PERMISSION_MODES:
            self.assertEqual(mode, normalize_permission_mode(mode))

    def test_unknown_mode_falls_back_to_default(self):
        self.assertEqual(DEFAULT_PERMISSION_MODE, normalize_permission_mode("nonsense"))
        self.assertEqual(DEFAULT_PERMISSION_MODE, normalize_permission_mode(None))
        self.assertEqual(DEFAULT_PERMISSION_MODE, normalize_permission_mode(""))


class ClassifyToolTest(unittest.TestCase):
    def test_read_tools_classified_as_read(self):
        for tool_name in READ_TOOLS:
            with self.subTest(tool_name=tool_name):
                kind, _reason = classify_tool(tool_name)
                self.assertEqual("read", kind)

    def test_write_and_execute_tools_classified_as_mutating(self):
        for tool_name in MUTATING_TOOLS:
            with self.subTest(tool_name=tool_name):
                kind, _reason = classify_tool(tool_name)
                self.assertEqual("mutating", kind)

    def test_mcp_read_vs_write_classification(self):
        self.assertEqual("read", classify_tool(MCP_READ)[0])
        self.assertEqual("mutating", classify_tool(MCP_WRITE)[0])

    def test_unknown_static_tool_is_mutating(self):
        self.assertEqual("mutating", classify_tool("frobnicate_the_thing")[0])


class FullAccessModeTest(unittest.TestCase):
    def test_full_access_allows_everything(self):
        policy = PermissionModePolicy(FULL_ACCESS)
        for tool_name in READ_TOOLS + MUTATING_TOOLS + [MCP_READ, MCP_WRITE, "unknown_tool"]:
            with self.subTest(tool_name=tool_name):
                decision = policy.evaluate(tool_name, {})
                self.assertEqual("allow", decision.action)
        self.assertEqual(FULL_ACCESS, policy.evaluate("file_write", {}).profile)


class ReadOnlyModeTest(unittest.TestCase):
    def test_read_only_allows_read_tools(self):
        policy = PermissionModePolicy(READ_ONLY)
        for tool_name in READ_TOOLS + [MCP_READ]:
            with self.subTest(tool_name=tool_name):
                self.assertEqual("allow", policy.evaluate(tool_name, {}).action)

    def test_read_only_denies_write_execute_and_unknown(self):
        policy = PermissionModePolicy(READ_ONLY)
        for tool_name in MUTATING_TOOLS + [MCP_WRITE, "unknown_tool"]:
            with self.subTest(tool_name=tool_name):
                decision = policy.evaluate(tool_name, {})
                self.assertEqual("deny", decision.action)
                self.assertEqual(READ_ONLY, decision.profile)


class AskModeTest(unittest.TestCase):
    def test_ask_allows_read_tools(self):
        policy = PermissionModePolicy(ASK)
        for tool_name in READ_TOOLS + [MCP_READ]:
            with self.subTest(tool_name=tool_name):
                self.assertEqual("allow", policy.evaluate(tool_name, {}).action)

    def test_ask_requires_approval_for_write_execute_and_unknown(self):
        policy = PermissionModePolicy(ASK)
        for tool_name in MUTATING_TOOLS + [MCP_WRITE, "unknown_tool"]:
            with self.subTest(tool_name=tool_name):
                decision = policy.evaluate(tool_name, {})
                self.assertEqual("ask", decision.action)
                self.assertEqual(ASK, decision.profile)


class BuildPolicyTest(unittest.TestCase):
    def test_build_returns_policy_for_known_modes(self):
        for mode in PERMISSION_MODES:
            policy = build_permission_mode_policy(mode)
            self.assertIsInstance(policy, PermissionModePolicy)
            self.assertEqual(mode, policy.mode)

    def test_build_falls_back_to_default_for_unknown(self):
        policy = build_permission_mode_policy("garbage")
        self.assertEqual(DEFAULT_PERMISSION_MODE, policy.mode)


class ParentStub:
    task_dir = ""
    verbose = False

    def __init__(self):
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=[]))


class SpyHandler(GenericAgentHandler):
    def __init__(self):
        super().__init__(ParentStub())
        self.calls = []

    def tool_before_callback(self, tool_name, args, response):
        self.calls.append("before:" + tool_name)

    def tool_after_callback(self, tool_name, args, response, ret):
        self.calls.append("after:" + tool_name)

    def do_file_write(self, args, response):
        self.calls.append("tool:file_write")
        return StepOutcome({"status": "success"}, next_prompt="\n")

    def do_file_read(self, args, response):
        self.calls.append("tool:file_read")
        return StepOutcome("read", next_prompt="\n")


class HandlerModeGateTest(unittest.TestCase):
    def response(self):
        return SimpleNamespace(content="")

    def test_no_policy_keeps_dispatch_compatible(self):
        handler = SpyHandler()
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual({"status": "success"}, outcome.data)
        self.assertIn("tool:file_write", handler.calls)

    def test_full_access_executes_mutating_tool(self):
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(FULL_ACCESS)
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual({"status": "success"}, outcome.data)
        self.assertIn("tool:file_write", handler.calls)

    def test_read_only_blocks_mutating_tool_before_execution(self):
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(READ_ONLY)
        outcome = exhaust(handler.dispatch("file_write", {"path": "x.txt"}, self.response()))
        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("error", outcome.data["status"])
        self.assertEqual("deny", outcome.data["permission"]["action"])

    def test_read_only_allows_read_tool(self):
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(READ_ONLY)
        outcome = exhaust(handler.dispatch("file_read", {}, self.response()))
        self.assertEqual("read", outcome.data)
        self.assertIn("tool:file_read", handler.calls)

    def test_ask_mode_returns_approval_required_for_mutating_tool(self):
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(ASK)
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("approval_required", outcome.data["status"])
        self.assertEqual("ask", outcome.data["permission"]["action"])

    def test_workflow_policy_takes_precedence_over_mode_policy(self):
        # workflow child policy must win when both happen to be set
        from workflow_permissions import ToolPermissionPolicy

        handler = SpyHandler()
        handler.workflow_permission_policy = ToolPermissionPolicy()  # inherit -> allow
        handler.permission_mode_policy = build_permission_mode_policy(READ_ONLY)
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual({"status": "success"}, outcome.data)
        self.assertIn("tool:file_write", handler.calls)


if __name__ == "__main__":
    unittest.main()
