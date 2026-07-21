import types
import unittest
from types import SimpleNamespace

from agentmain import GenericAgent, build_permission_mode_hint
from permission_policy import ASK, DEFAULT_PERMISSION_MODE, FULL_ACCESS, READ_ONLY


class PermissionModeTests(unittest.TestCase):
    def make_agent(self):
        # 绕过 __init__ 的重资源（llm.yaml / session transcript），只测权限档逻辑。
        agent = GenericAgent.__new__(GenericAgent)
        agent.permission_mode = DEFAULT_PERMISSION_MODE
        agent.handler = None
        return agent

    def test_default_permission_mode_is_full_access(self):
        agent = self.make_agent()
        self.assertEqual(FULL_ACCESS, agent.permission_mode)

    def test_set_permission_mode_updates_field(self):
        agent = self.make_agent()

        result = agent.set_permission_mode(READ_ONLY)

        self.assertEqual(READ_ONLY, result)
        self.assertEqual(READ_ONLY, agent.permission_mode)

    def test_set_permission_mode_normalizes_unknown_to_default(self):
        agent = self.make_agent()

        result = agent.set_permission_mode("nonsense")

        self.assertEqual(DEFAULT_PERMISSION_MODE, result)
        self.assertEqual(DEFAULT_PERMISSION_MODE, agent.permission_mode)

    def test_set_permission_mode_updates_live_handler_policy(self):
        agent = self.make_agent()
        agent.handler = SimpleNamespace(permission_mode_policy=None)

        agent.set_permission_mode(ASK)

        self.assertIsNotNone(agent.handler.permission_mode_policy)
        self.assertEqual(ASK, agent.handler.permission_mode_policy.mode)


class PermissionModeHintTests(unittest.TestCase):
    def test_full_access_and_default_inject_no_hint(self):
        self.assertEqual("", build_permission_mode_hint(FULL_ACCESS))
        self.assertEqual("", build_permission_mode_hint(None))
        # 未知档归一化到 full_access（默认），同样不注入
        self.assertEqual("", build_permission_mode_hint("nonsense"))

    def test_read_only_hint_mentions_denied_side_effects(self):
        hint = build_permission_mode_hint(READ_ONLY)
        self.assertIn("Read Only", hint)
        self.assertIn("deny", hint.lower())

    def test_ask_hint_mentions_per_call_approval(self):
        hint = build_permission_mode_hint(ASK)
        self.assertIn("Ask for approval", hint)
        self.assertIn("approval_required", hint)


if __name__ == "__main__":
    unittest.main()
