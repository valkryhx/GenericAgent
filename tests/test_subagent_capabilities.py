import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_capabilities import (  # noqa: E402
    INTERNAL_SENTINEL_TOOLS,
    ORCHESTRATION_TOOLS,
    build_subagent_capability_profile,
    filter_tool_schema_for_capabilities,
)
from agent_loop import exhaust  # noqa: E402
from ga import GenericAgentHandler  # noqa: E402


class SubagentCapabilityProfileTest(unittest.TestCase):
    def test_root_profile_keeps_orchestration_tools(self):
        profile = build_subagent_capability_profile(is_subagent=False)

        self.assertTrue(profile.allow_delegation)
        self.assertTrue(ORCHESTRATION_TOOLS.issubset(profile.allowed_tools))
        self.assertTrue(profile.allows("spawn_agent"))
        self.assertTrue(profile.allows("no_tool"))

    def test_child_profile_removes_orchestration_tools_by_default(self):
        profile = build_subagent_capability_profile(
            is_subagent=True,
            role_tools=["file_read", "code_run", "spawn_agent"],
        )

        self.assertFalse(profile.allow_delegation)
        self.assertTrue(profile.allows("file_read"))
        self.assertTrue(profile.allows("code_run"))
        self.assertFalse(profile.allows("spawn_agent"))
        self.assertTrue(INTERNAL_SENTINEL_TOOLS.issubset(profile.allowed_tools))

    def test_child_can_explicitly_opt_into_delegation(self):
        profile = build_subagent_capability_profile(
            is_subagent=True,
            role_tools=["file_read"],
            allow_delegation=True,
        )

        self.assertTrue(profile.allow_delegation)
        self.assertTrue(profile.allows("spawn_agent"))

    def test_team_mode_grants_collaboration_without_orchestration(self):
        available = [
            "file_read",
            "code_run",
            "send_message",
            "task_create",
            "task_get",
            "task_update",
            "task_list",
            "team_status",
            "spawn_agent",
            "wait_agent",
            "followup_task",
        ]
        profile = build_subagent_capability_profile(
            is_subagent=True,
            team_mode=True,
            available_tools=available,
        )

        self.assertTrue(profile.team_mode)
        self.assertFalse(profile.allow_delegation)
        # collaboration surface is present
        self.assertTrue(profile.allows("send_message"))
        self.assertTrue(profile.allows("task_create"))
        self.assertTrue(profile.allows("task_get"))
        self.assertTrue(profile.allows("task_update"))
        self.assertTrue(profile.allows("task_list"))
        self.assertTrue(profile.allows("team_status"))
        # orchestration surface stays closed
        self.assertFalse(profile.allows("spawn_agent"))
        self.assertFalse(profile.allows("wait_agent"))
        self.assertFalse(profile.allows("followup_task"))
        self.assertTrue(profile.to_dict()["team_mode"])

    def test_team_mode_off_keeps_a_plain_isolated_child(self):
        available = ["file_read", "code_run", "send_message", "task_create", "team_status"]
        profile = build_subagent_capability_profile(
            is_subagent=True,
            team_mode=False,
            available_tools=available,
        )

        self.assertFalse(profile.team_mode)
        self.assertFalse(profile.allows("send_message"))
        self.assertFalse(profile.allows("task_create"))
        self.assertFalse(profile.allows("team_status"))
        self.assertTrue(profile.allows("file_read"))

    def test_team_mode_is_ignored_for_delegating_or_root_profiles(self):
        delegating = build_subagent_capability_profile(
            is_subagent=True, allow_delegation=True, team_mode=True
        )
        root = build_subagent_capability_profile(is_subagent=False, team_mode=True)

        # team_mode only meaningfully flags a non-delegating child
        self.assertFalse(delegating.team_mode)
        self.assertFalse(root.team_mode)

    def test_unknown_tools_are_denied(self):
        profile = build_subagent_capability_profile(is_subagent=True, role_tools=["file_read"])

        self.assertFalse(profile.allows("made_up_tool"))
        self.assertFalse(profile.allows(""))
        self.assertTrue(profile.allows("no_tool"))

    def test_schema_filter_does_not_mutate_input(self):
        schema = [
            {"type": "function", "function": {"name": "file_read"}},
            {"type": "function", "function": {"name": "spawn_agent"}},
            {"type": "function", "function": {"name": "close_agent"}},
        ]
        profile = build_subagent_capability_profile(is_subagent=True, role_tools=["file_read"])

        filtered = filter_tool_schema_for_capabilities(schema, profile)

        self.assertEqual([item["function"]["name"] for item in filtered], ["file_read"])
        self.assertEqual(
            [item["function"]["name"] for item in schema],
            ["file_read", "spawn_agent", "close_agent"],
        )

    def test_workflow_tool_schema_is_visible_and_removed_from_children(self):
        for schema_name in ("tools_schema.json", "tools_schema_cn.json"):
            with self.subTest(schema_name=schema_name):
                schema = json.loads((REPO_ROOT / "assets" / schema_name).read_text(encoding="utf-8"))
                workflow = next(item["function"] for item in schema if item["function"]["name"] == "workflow")
                self.assertEqual(
                    {"script", "scriptPath", "name", "args", "resumeFromRunId", "timeoutSeconds"},
                    set(workflow["parameters"]["properties"]),
                )

        schema = [
            {"type": "function", "function": {"name": "workflow"}},
            {"type": "function", "function": {"name": "file_read"}},
        ]
        child = build_subagent_capability_profile(
            is_subagent=True,
            available_tools={"workflow", "file_read"},
        )
        filtered = filter_tool_schema_for_capabilities(schema, child)
        self.assertEqual([item["function"]["name"] for item in filtered], ["file_read"])

    def test_workflow_tool_forwards_cc_fields_to_the_bridge_launcher(self):
        captured = {}

        def launcher(**kwargs):
            captured.update(kwargs)
            return {"status": "async_launched", "runId": "wf_tool", "workflowName": "demo"}

        handler = GenericAgentHandler(
            SimpleNamespace(task_dir="", workflow_tool_launcher=launcher),
            cwd=".",
        )
        outcome = exhaust(handler.do_workflow({
            "script": "export const meta = { name: 'demo', description: 'demo' }",
            "scriptPath": None,
            "name": None,
            "args": {"value": 1},
            "resumeFromRunId": "wf_source",
            "timeoutSeconds": 12,
        }, None))

        self.assertEqual("async_launched", outcome.data["status"])
        self.assertEqual("wf_tool", outcome.data["runId"])
        self.assertEqual("export const meta = { name: 'demo', description: 'demo' }", captured["script"])
        self.assertIsNone(captured["script_path"])
        self.assertIsNone(captured["name"])
        self.assertEqual({"value": 1}, captured["args"])
        self.assertEqual("wf_source", captured["resume_from_run_id"])
        self.assertEqual(12, captured["timeout_seconds"])

    def test_workflow_tool_without_launcher_returns_structured_error(self):
        handler = GenericAgentHandler(SimpleNamespace(task_dir=""), cwd=".")
        outcome = exhaust(handler.do_workflow({}, None))

        self.assertEqual("error", outcome.data["status"])
        self.assertIn("launcher unavailable", outcome.data["error"])

    def test_handler_hard_denies_a_hallucinated_orchestration_call(self):
        handler = GenericAgentHandler(SimpleNamespace(task_dir=""), cwd=".")
        handler.capability_profile = build_subagent_capability_profile(
            is_subagent=True,
            role_tools=["file_read"],
        )
        generator = handler.dispatch("spawn_agent", {}, SimpleNamespace())

        with self.assertRaises(StopIteration) as stopped:
            while True:
                next(generator)

        result = stopped.exception.value
        self.assertEqual(result.data["status"], "error")
        self.assertEqual(result.data["capability"]["reason"], "tool_not_granted")


if __name__ == "__main__":
    unittest.main()
