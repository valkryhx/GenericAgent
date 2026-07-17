"""阶段 3：agentmain 从 llm.yaml 加载会话（不再读 mykey）。"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentmain import GenericAgent  # noqa: E402
import agentmain as agentmain_mod  # noqa: E402


class _FakeBackend:
    def __init__(self, name, model, history=None):
        self.name = name
        self.model = model
        self.history = list(history or [])


class _FakeClient:
    def __init__(self, name, model):
        self.backend = _FakeBackend(name, model)
        self.last_tools = ""


class LoadLlmSessionsYamlTest(unittest.TestCase):
    def setUp(self):
        agentmain_mod._llm_yaml_path = None
        agentmain_mod._llm_yaml_mtime_ns = None

    def test_load_llm_sessions_uses_yaml_profiles(self):
        c_default = _FakeClient("default", "claude-opus-4-8")
        c_grok = _FakeClient("grok", "grok-4.5")
        c_gpt = _FakeClient("gpt", "gpt-5.4")

        def fake_load(**_kwargs):
            # active_index=1 → grok
            return [c_default, c_grok, c_gpt], 1, str(REPO_ROOT / "llm.yaml"), 12345

        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        with patch.object(agentmain_mod, "load_clients_from_yaml", side_effect=fake_load):
            agent.load_llm_sessions()

        self.assertEqual(len(agent.llmclients), 3)
        self.assertIs(agent.llmclient, c_grok)
        self.assertEqual(agent.llm_no, 1)
        self.assertEqual(agent.get_llm_name(), "grok/grok-4.5")
        names = [agent.get_llm_name(c) for c in agent.llmclients]
        self.assertEqual(names, [
            "default/claude-opus-4-8",
            "grok/grok-4.5",
            "gpt/gpt-5.4",
        ])
        # 热重载：mtime 不变则不重建
        with patch.object(agentmain_mod, "load_clients_from_yaml") as again:
            # 伪造 path 存在 + mtime 匹配
            with patch.object(agentmain_mod.os.path, "exists", return_value=True), \
                 patch.object(agentmain_mod.os, "stat", return_value=types.SimpleNamespace(st_mtime_ns=12345)):
                agent.load_llm_sessions()
            again.assert_not_called()

    def test_load_preserves_history_and_selection_by_name(self):
        old = _FakeClient("grok", "grok-4.5")
        old.backend.history = [{"role": "user", "content": "keep-me"}]
        new_default = _FakeClient("default", "claude-opus-4-8")
        new_grok = _FakeClient("grok", "grok-4.5")

        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        agent.llmclients = [old]
        agent.llmclient = old
        agentmain_mod._llm_yaml_path = str(REPO_ROOT / "llm.yaml")
        agentmain_mod._llm_yaml_mtime_ns = 1  # 强制视为已变

        def fake_load(**_kwargs):
            return [new_default, new_grok], 0, str(REPO_ROOT / "llm.yaml"), 999

        with patch.object(agentmain_mod, "load_clients_from_yaml", side_effect=fake_load), \
             patch.object(agentmain_mod.os.path, "exists", return_value=True), \
             patch.object(agentmain_mod.os, "stat", return_value=types.SimpleNamespace(st_mtime_ns=999)):
            agent.load_llm_sessions()

        # 按旧名 grok 恢复下标
        self.assertIs(agent.llmclient, new_grok)
        self.assertEqual(agent.llm_no, 1)
        self.assertEqual(agent.llmclient.backend.history, [{"role": "user", "content": "keep-me"}])

    def test_load_raises_when_yaml_missing(self):
        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        with patch.object(
            agentmain_mod,
            "load_clients_from_yaml",
            side_effect=FileNotFoundError("未找到 llm.yaml"),
        ):
            with self.assertRaises(FileNotFoundError):
                agent.load_llm_sessions()


if __name__ == "__main__":
    unittest.main()
