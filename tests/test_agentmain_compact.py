"""agentmain 手动 /compact 路由测试。

覆盖：/compact 命令被 _handle_slash_cmd 拦截并调用 _manual_compact（返回 None、
不进入模型请求）；带指令的 /compact keep X 能把指令透传给 compact 核心；
compact 核心不可用时给出友好提示而非崩溃。
"""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import GenericAgent  # noqa: E402

# 捕获真实 agentmain 模块对象。别的测试（如 test_tgapp_stream_segments）会把
# sys.modules["agentmain"] 替换成 stub 且不还原，patch("agentmain.xxx") 会打到 stub、
# 与 GenericAgent 实际读取的真实模块脱节。用类的 __module__ 拿到真实模块对象，
# patch.object 直接作用其上，免疫 sys.modules 污染。
_AGENTMAIN = sys.modules[GenericAgent.__module__]


class _Q:
    """极简 display_queue：收集 put 的事件。"""
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def _make_agent():
    backend = type("Backend", (), {"history": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
    ]})()
    agent = GenericAgent.__new__(GenericAgent)
    agent.llmclient = type("Client", (), {"backend": backend, "last_tools": ""})()
    agent.history = ["[USER]: hi"]
    agent.handler = object()
    agent.log_path = None
    agent.session_path = None
    agent.session_id = ""
    return agent


class ManualCompactRoutingTest(unittest.TestCase):
    def test_compact_command_is_intercepted_and_calls_manual_compact(self):
        agent = _make_agent()
        q = _Q()
        with patch.object(GenericAgent, "_manual_compact") as mc:
            result = agent._handle_slash_cmd("/compact", q)
        # /compact 被拦截，不作为普通 query 下发（返回 None）
        self.assertIsNone(result)
        mc.assert_called_once()
        # 第一个位置参数是 instructions（空）
        self.assertEqual("", mc.call_args.args[0])

    def test_compact_passes_instructions(self):
        agent = _make_agent()
        q = _Q()
        with patch.object(GenericAgent, "_manual_compact") as mc:
            agent._handle_slash_cmd("/compact keep decisions", q)
        self.assertEqual("keep decisions", mc.call_args.args[0])

    def test_manual_compact_invokes_core_and_reports(self):
        agent = _make_agent()
        q = _Q()

        class _Result:
            ok = True
            message = "Compacted 2 messages into summary context."

        with patch.object(_AGENTMAIN, "compact_agent_context", return_value=_Result()) as core:
            agent._manual_compact("keep X", q)

        core.assert_called_once()
        # instructions 透传
        self.assertEqual("keep X", core.call_args.kwargs.get("instructions"))
        # 结果消息回显给用户
        self.assertTrue(any("Compacted 2 messages" in str(it.get("done", "")) for it in q.items))

    def test_manual_compact_reports_failure(self):
        agent = _make_agent()
        q = _Q()

        class _Result:
            ok = False
            message = "summary empty"

        with patch.object(_AGENTMAIN, "compact_agent_context", return_value=_Result()):
            agent._manual_compact("", q)

        self.assertTrue(any("summary empty" in str(it.get("done", "")) for it in q.items))

    def test_manual_compact_graceful_when_core_unavailable(self):
        agent = _make_agent()
        q = _Q()
        with patch.object(_AGENTMAIN, "compact_agent_context", None):
            agent._manual_compact("", q)
        # 不崩溃，给出提示
        self.assertTrue(any("/compact" in str(it.get("done", "")) for it in q.items))


if __name__ == "__main__":
    unittest.main()
