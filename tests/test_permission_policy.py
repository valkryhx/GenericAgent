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

    def test_ask_mode_without_runtime_denies_mutating_tool(self):
        # P1：无 approval UI / runtime 时 ask fail-closed → deny（不再返回 approval_required 空信号）
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(ASK)
        handler.permission_runtime = None
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("error", outcome.data["status"])
        self.assertEqual("deny", outcome.data["permission"]["action"])

    def test_workflow_policy_takes_precedence_over_mode_policy(self):
        # workflow child policy must win when both happen to be set
        from workflow_permissions import ToolPermissionPolicy

        handler = SpyHandler()
        handler.workflow_permission_policy = ToolPermissionPolicy()  # inherit -> allow
        handler.permission_mode_policy = build_permission_mode_policy(READ_ONLY)
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual({"status": "success"}, outcome.data)
        self.assertIn("tool:file_write", handler.calls)

    def test_mid_session_switch_full_access_to_ask_requires_approval(self):
        """默认 full_access 可直接写；中途切到 ask 后写操作须 accept 才执行。"""
        from permission_runtime import PermissionRuntime

        handler = SpyHandler()
        # 与 agentmain 一致：先挂 full_access policy
        handler.permission_mode_policy = build_permission_mode_policy(FULL_ACCESS)
        handler.permission_runtime = None

        # 1) full_access：file_write 直接执行
        before_switch = exhaust(handler.dispatch("file_write", {"path": "a.txt"}, self.response()))
        self.assertEqual({"status": "success"}, before_switch.data)
        self.assertEqual(1, handler.calls.count("tool:file_write"))

        # 2) 中途切档（模拟 /permissions 或 set_permission_mode 同步 live handler）
        handler.permission_mode_policy = build_permission_mode_policy(ASK)

        # 3a) 切到 ask 且无 runtime：fail-closed deny，工具 body 不跑
        handler.permission_runtime = None
        denied = exhaust(handler.dispatch("file_write", {"path": "b.txt"}, self.response()))
        self.assertEqual("error", denied.data["status"])
        self.assertEqual("deny", denied.data["permission"]["action"])
        self.assertEqual(1, handler.calls.count("tool:file_write"))

        # 3b) 挂 runtime 并 accept：才第二次执行 body
        import threading
        import time

        runtime = PermissionRuntime()
        events = []
        runtime.set_emit(lambda ev: events.append(dict(ev)))
        handler.permission_runtime = runtime

        def auto_accept():
            for _ in range(200):
                reqs = [e for e in events if e.get("type") == "permission_request"]
                if reqs:
                    rid = reqs[-1].get("requestId")
                    if rid:
                        runtime.resolve(rid, "accept")
                        return
                time.sleep(0.01)

        th = threading.Thread(target=auto_accept, daemon=True)
        th.start()
        accepted = exhaust(handler.dispatch("file_write", {"path": "c.txt"}, self.response()))
        th.join(timeout=2)
        self.assertEqual({"status": "success"}, accepted.data)
        self.assertEqual(2, handler.calls.count("tool:file_write"))
        self.assertTrue(any(e.get("type") == "permission_request" for e in events))


class MidSessionSwitchViaAgentTest(unittest.TestCase):
    """走 agent.set_permission_mode 同步 live handler 的路径（更接近 UI）。"""

    def response(self):
        return SimpleNamespace(content="")

    def test_agent_set_permission_mode_switches_live_dispatch_gate(self):
        from agentmain import GenericAgent
        from permission_runtime import PermissionRuntime

        agent = GenericAgent.__new__(GenericAgent)
        agent.permission_mode = FULL_ACCESS
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(FULL_ACCESS)
        agent.handler = handler

        # full_access 直写
        ok = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual({"status": "success"}, ok.data)
        self.assertIn("tool:file_write", handler.calls)

        # UI 切 ask：set_permission_mode 更新 live handler.policy
        agent.set_permission_mode(ASK)
        self.assertEqual(ASK, agent.permission_mode)
        self.assertEqual(ASK, handler.permission_mode_policy.mode)

        # 未挂 runtime → ask fail-closed deny
        handler.permission_runtime = None
        denied = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual("error", denied.data["status"])
        self.assertEqual(1, handler.calls.count("tool:file_write"))

        # 挂 runtime 并 accept
        runtime = PermissionRuntime()
        events = []
        runtime.set_emit(lambda ev: events.append(dict(ev)))
        handler.permission_runtime = runtime

        import threading
        import time

        def auto_accept():
            for _ in range(200):
                for e in events:
                    if e.get("type") == "permission_request" and e.get("requestId"):
                        runtime.resolve(e["requestId"], "accept")
                        return
                time.sleep(0.01)

        th = threading.Thread(target=auto_accept, daemon=True)
        th.start()
        accepted = exhaust(handler.dispatch("file_write", {}, self.response()))
        th.join(timeout=2)
        self.assertEqual({"status": "success"}, accepted.data)
        self.assertEqual(2, handler.calls.count("tool:file_write"))

    def test_multi_switch_ask_readonly_full_access_ask_cycle(self):
        """多次切档：ask → read_only → full_access → ask，每段行为都要正确。

        贴近用户连按 /permissions 的路径：始终走 agent.set_permission_mode，
        并在各档下混合 file_read / file_write，验证：
        - ask：读放行；写须 accept 才执行；deny 不执行
        - read_only：读放行；写直接 deny（无 permission_request）
        - full_access：读写直通，不弹审批
        - 再切回 ask：写又要审批（不残留 full_access 放行）
        """
        import threading
        import time

        from agentmain import GenericAgent
        from permission_runtime import PermissionRuntime

        agent = GenericAgent.__new__(GenericAgent)
        agent.permission_mode = FULL_ACCESS
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(FULL_ACCESS)
        agent.handler = handler

        runtime = PermissionRuntime()
        events: list[dict] = []
        runtime.set_emit(lambda ev: events.append(dict(ev)))
        handler.permission_runtime = runtime
        agent.permission_runtime = runtime

        def write_count() -> int:
            return handler.calls.count("tool:file_write")

        def read_count() -> int:
            return handler.calls.count("tool:file_read")

        def request_count() -> int:
            return sum(1 for e in events if e.get("type") == "permission_request")

        def settle_count() -> int:
            return sum(1 for e in events if e.get("type") == "permission_request_settled")

        def auto_resolve(decision: str, after_n_requests: int):
            """等第 after_n_requests 个 permission_request 出现后 resolve 最新 rid。"""

            def _run():
                for _ in range(400):
                    reqs = [e for e in events if e.get("type") == "permission_request"]
                    if len(reqs) >= after_n_requests:
                        rid = reqs[-1].get("requestId")
                        if rid:
                            runtime.resolve(rid, decision)
                            return
                    time.sleep(0.01)

            th = threading.Thread(target=_run, daemon=True)
            th.start()
            return th

        # ── Phase 1: ask ─────────────────────────────────────────────
        agent.set_permission_mode(ASK)
        self.assertEqual(ASK, agent.permission_mode)
        self.assertEqual(ASK, handler.permission_mode_policy.mode)

        # 读：不审批
        r1 = exhaust(handler.dispatch("file_read", {"path": "r1"}, self.response()))
        self.assertEqual("read", r1.data)
        self.assertEqual(1, read_count())
        self.assertEqual(0, request_count())

        # 写 + accept
        th = auto_resolve("accept", after_n_requests=1)
        w_accept = exhaust(handler.dispatch("file_write", {"path": "w1"}, self.response()))
        th.join(timeout=2)
        self.assertEqual({"status": "success"}, w_accept.data)
        self.assertEqual(1, write_count())
        self.assertEqual(1, request_count())
        self.assertEqual(1, settle_count())

        # 写 + deny：body 不再增加
        th = auto_resolve("deny", after_n_requests=2)
        w_deny = exhaust(handler.dispatch("file_write", {"path": "w2"}, self.response()))
        th.join(timeout=2)
        self.assertEqual("error", w_deny.data["status"])
        self.assertEqual("deny", w_deny.data["permission"]["action"])
        self.assertEqual(1, write_count())
        self.assertEqual(2, request_count())

        # ── Phase 2: read_only ───────────────────────────────────────
        agent.set_permission_mode(READ_ONLY)
        self.assertEqual(READ_ONLY, handler.permission_mode_policy.mode)

        r2 = exhaust(handler.dispatch("file_read", {"path": "r2"}, self.response()))
        self.assertEqual("read", r2.data)
        self.assertEqual(2, read_count())

        # 写：直接 deny，不走 permission_request（policy 层 deny，不是 ask wait）
        req_before_ro = request_count()
        w_ro = exhaust(handler.dispatch("file_write", {"path": "w3"}, self.response()))
        self.assertEqual("error", w_ro.data["status"])
        self.assertEqual("deny", w_ro.data["permission"]["action"])
        self.assertEqual(READ_ONLY, w_ro.data["permission"]["profile"])
        self.assertEqual(1, write_count())
        self.assertEqual(req_before_ro, request_count())  # 无新 request

        # ── Phase 3: full_access ─────────────────────────────────────
        agent.set_permission_mode(FULL_ACCESS)
        self.assertEqual(FULL_ACCESS, handler.permission_mode_policy.mode)

        req_before_fa = request_count()
        w_fa = exhaust(handler.dispatch("file_write", {"path": "w4"}, self.response()))
        self.assertEqual({"status": "success"}, w_fa.data)
        self.assertEqual(2, write_count())
        self.assertEqual(req_before_fa, request_count())  # 直通，不弹窗

        r3 = exhaust(handler.dispatch("file_read", {"path": "r3"}, self.response()))
        self.assertEqual("read", r3.data)
        self.assertEqual(3, read_count())

        # ── Phase 4: 再切回 ask（不能残留 full_access）────────────────
        agent.set_permission_mode(ASK)
        self.assertEqual(ASK, agent.permission_mode)
        self.assertEqual(ASK, handler.permission_mode_policy.mode)

        # 4a) 临时摘掉 runtime：fail-closed deny（无 UI）
        handler.permission_runtime = None
        req_before_fc = request_count()
        w_failclosed = exhaust(handler.dispatch("file_write", {"path": "w5"}, self.response()))
        self.assertEqual("error", w_failclosed.data["status"])
        self.assertEqual("deny", w_failclosed.data["permission"]["action"])
        self.assertEqual(2, write_count())
        self.assertEqual(req_before_fc, request_count())

        # 4b) 挂回 runtime + accept：须再弹一次审批后才执行
        handler.permission_runtime = runtime
        th = auto_resolve("accept", after_n_requests=3)
        w_ask_again = exhaust(handler.dispatch("file_write", {"path": "w6"}, self.response()))
        th.join(timeout=2)
        self.assertEqual({"status": "success"}, w_ask_again.data)
        self.assertEqual(3, write_count())
        self.assertEqual(3, request_count())
        self.assertGreaterEqual(settle_count(), 3)

        # 4c) 读在 ask 下仍始终放行
        r4 = exhaust(handler.dispatch("file_read", {"path": "r4"}, self.response()))
        self.assertEqual("read", r4.data)
        self.assertEqual(4, read_count())


if __name__ == "__main__":
    unittest.main()
