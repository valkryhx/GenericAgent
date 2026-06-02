import unittest
from types import SimpleNamespace

from agent_loop import StepOutcome, exhaust
from ga import GenericAgentHandler
from workflow_permissions import PermissionDecision, ToolPermissionPolicy, parse_mcp_tool_name


class ToolPermissionPolicyTest(unittest.TestCase):
    def test_inherit_current_allows_existing_tool_by_default(self):
        policy = ToolPermissionPolicy()

        decision = policy.evaluate("file_write", {"path": "x.txt"})

        self.assertEqual("allow", decision.action)
        self.assertEqual("inherit_current", decision.reason)

    def test_read_only_denies_write_patch_shell_and_browser_execute(self):
        policy = ToolPermissionPolicy(profile="read_only")

        for tool_name in ["file_write", "file_patch", "code_run", "web_execute_js"]:
            with self.subTest(tool_name=tool_name):
                decision = policy.evaluate(tool_name, {})
                self.assertEqual("deny", decision.action)
                self.assertEqual("read_only_static_write_or_execute", decision.reason)

    def test_read_only_allows_safe_read_discovery_tools(self):
        policy = ToolPermissionPolicy(profile="read_only")

        for tool_name in ["file_read", "web_scan", "no_tool", "ask_user", "load_skill", "status_report", "list_files"]:
            with self.subTest(tool_name=tool_name):
                decision = policy.evaluate(tool_name, {})
                self.assertEqual("allow", decision.action)

    def test_restricted_mcp_allows_only_configured_server_or_tool(self):
        policy = ToolPermissionPolicy(
            profile="restricted_mcp",
            options={
                "allowed_mcp_servers": ["memory"],
                "allowed_mcp_tools": ["mcp__exa__web_search_exa"],
                "denied_mcp_servers": ["danger"],
                "denied_mcp_tools": ["mcp__memory__delete_entities"],
            },
        )

        self.assertEqual("allow", policy.evaluate("file_write", {}).action)
        self.assertEqual("allow", policy.evaluate("mcp__memory__search_nodes", {}).action)
        self.assertEqual("allow", policy.evaluate("mcp__exa__web_search_exa", {}).action)
        denied_tool = policy.evaluate("mcp__memory__delete_entities", {})
        self.assertEqual("deny", denied_tool.action)
        self.assertEqual("restricted_mcp_tool_denied", denied_tool.reason)
        denied_server = policy.evaluate("mcp__danger__read", {})
        self.assertEqual("deny", denied_server.action)
        self.assertEqual("restricted_mcp_server_denied", denied_server.reason)
        unknown = policy.evaluate("mcp__unknown__read", {})
        self.assertEqual("deny", unknown.action)
        self.assertEqual("restricted_mcp_not_allowed", unknown.reason)

    def test_explicit_approval_returns_ask_without_interactive_ui(self):
        policy = ToolPermissionPolicy(profile="explicit_approval")

        decision = policy.evaluate("file_read", {})

        self.assertEqual("ask", decision.action)
        self.assertEqual("explicit_approval_required", decision.reason)

    def test_parse_mcp_tool_name(self):
        parsed = parse_mcp_tool_name("mcp__context7__query-docs")

        self.assertEqual("context7", parsed.server)
        self.assertEqual("query-docs", parsed.tool)


class ParentStub:
    task_dir = ""
    verbose = False

    def __init__(self):
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=[]))


class SpyHandler(GenericAgentHandler):
    def __init__(self):
        super().__init__(ParentStub())
        self.calls = []
        self.events = []
        self.workflow_permission_event_callback = self.events.append

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

    def _dispatch_mcp_tool(self, tool_name, args, response):
        self.calls.append("mcp:" + tool_name)
        if False:
            yield None
        return StepOutcome({"status": "success"}, next_prompt="\n")


class GenericAgentPermissionGateTest(unittest.TestCase):
    def response(self):
        return SimpleNamespace(content="")

    def test_permission_hook_blocks_static_tool_before_execution(self):
        handler = SpyHandler()
        handler.workflow_permission_policy = ToolPermissionPolicy(profile="read_only")

        outcome = exhaust(handler.dispatch("file_write", {"path": "x.txt"}, self.response()))

        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("error", outcome.data["status"])
        self.assertEqual("deny", outcome.data["permission"]["action"])
        self.assertEqual(["permission_profile_selected", "tool_denied"], [event["type"] for event in handler.events])

    def test_permission_hook_blocks_mcp_tool_before_execution(self):
        handler = SpyHandler()
        handler.workflow_permission_policy = ToolPermissionPolicy(profile="read_only")

        outcome = exhaust(handler.dispatch("mcp__unknown__delete", {}, self.response()))

        self.assertNotIn("mcp:mcp__unknown__delete", handler.calls)
        self.assertEqual("error", outcome.data["status"])
        self.assertEqual("deny", outcome.data["permission"]["action"])
        self.assertEqual(["permission_profile_selected", "tool_denied"], [event["type"] for event in handler.events])

    def test_non_workflow_handler_without_policy_keeps_dispatch_compatible(self):
        handler = SpyHandler()

        static_outcome = exhaust(handler.dispatch("file_read", {}, self.response()))
        mcp_outcome = exhaust(handler.dispatch("mcp__memory__search_nodes", {}, self.response()))

        self.assertEqual("read", static_outcome.data)
        self.assertEqual({"status": "success"}, mcp_outcome.data)
        self.assertEqual(
            [
                "before:file_read",
                "tool:file_read",
                "after:file_read",
                "before:mcp__memory__search_nodes",
                "mcp:mcp__memory__search_nodes",
                "after:mcp__memory__search_nodes",
            ],
            handler.calls,
        )
        self.assertEqual([], handler.events)

    def test_permission_events_include_run_job_tool_and_profile(self):
        handler = SpyHandler()
        handler.workflow_permission_policy = ToolPermissionPolicy(profile="read_only")
        handler.workflow_permission_context = {
            "runId": "wf_test",
            "jobId": "agent_1",
            "permissionProfile": "read_only",
        }

        allowed = exhaust(handler.dispatch("file_read", {}, self.response()))
        denied = exhaust(handler.dispatch("file_write", {}, self.response()))

        self.assertEqual("read", allowed.data)
        self.assertEqual("error", denied.data["status"])
        self.assertEqual(
            ["permission_profile_selected", "tool_allowed", "tool_denied"],
            [event["type"] for event in handler.events],
        )
        profile_event = handler.events[0]
        self.assertEqual("wf_test", profile_event["runId"])
        self.assertEqual("agent_1", profile_event["jobId"])
        self.assertEqual("read_only", profile_event["profile"])
        for event, decision in zip(handler.events[1:], ["allow", "deny"]):
            self.assertEqual("wf_test", event["runId"])
            self.assertEqual("agent_1", event["jobId"])
            self.assertEqual("read_only", event["profile"])
            self.assertEqual(decision, event["decision"])
            self.assertIn("reason", event)
        self.assertEqual("file_read", handler.events[1]["toolName"])
        self.assertEqual("file_write", handler.events[2]["toolName"])


if __name__ == "__main__":
    unittest.main()
