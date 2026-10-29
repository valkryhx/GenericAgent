"""Stage B: session-scoped dynamic hook registry.

Covers add/remove/list, idempotent replacement, static-before-dynamic order,
and the hard security rule that a dynamic hook may never grant ``allow`` on a
guard event (it can only tighten: deny/ask/context).
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from genericagent.hooks.audit import HookAudit
from genericagent.hooks.config import HookRegistry, load_hook_registry
from genericagent.hooks.models import HookAggregate, HookEntry, HookHandler
from genericagent.hooks.runtime import HookRuntime


def make_agent(runtime):
    return SimpleNamespace(
        session_id="s1", session_path=None, cwd=".", permission_mode="full_access",
        session_turn_id=0, agent_id=None, agent_type=None, hook_runtime=runtime,
    )


def prompt_handler(text, **opts):
    return HookHandler(kind="prompt", options={"type": "prompt", "prompt": text, **opts})


class SessionRegistryUnitTests(unittest.TestCase):
    def setUp(self):
        self.rt = HookRuntime(registry=HookRegistry(), audit=HookAudit(Path(tempfile.mkdtemp()) / "audit"),
                              prompt_runner=lambda kind, prompt, payload: "ctx:" + prompt)
        self.agent = make_agent(self.rt)

    def test_add_then_run_event_hits(self):
        self.rt.add_session_hook("Stop", prompt_handler("hello"), key="goal:stop")
        agg = self.rt.run_event(self.agent, "Stop", {})
        self.assertTrue(agg.context_text)  # prompt handler returns context

    def test_remove_stops_hitting(self):
        key = self.rt.add_session_hook("Stop", prompt_handler("x"))
        self.assertTrue(self.rt.remove_session_hook(key))
        self.assertFalse(self.rt.remove_session_hook(key))
        agg = self.rt.run_event(self.agent, "Stop", {})
        self.assertEqual(agg.context_text, "")

    def test_idempotent_key_replaces_not_stacks(self):
        self.rt.add_session_hook("Stop", prompt_handler("v1"), key="goal:stop")
        self.rt.add_session_hook("Stop", prompt_handler("v2"), key="goal:stop")
        hooks = self.rt.list_session_hooks("Stop")
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["key"], "goal:stop")

    def test_list_filters_by_event(self):
        self.rt.add_session_hook("Stop", prompt_handler("a"), key="k1")
        self.rt.add_session_hook("Notification", prompt_handler("b"), key="k2")
        self.assertEqual({h["event"] for h in self.rt.list_session_hooks()}, {"Stop", "Notification"})
        self.assertEqual([h["key"] for h in self.rt.list_session_hooks("Stop")], ["k1"])

    def test_clear_removes_all(self):
        self.rt.add_session_hook("Stop", prompt_handler("a"), key="k1")
        self.rt.add_session_hook("Stop", prompt_handler("b"), key="k2")
        self.assertEqual(self.rt.clear_session_hooks(), 2)
        self.assertEqual(self.rt.list_session_hooks(), [])

    def test_unknown_event_rejected(self):
        with self.assertRaises(ValueError):
            self.rt.add_session_hook("NotAnEvent", prompt_handler("a"))

    def test_invalid_spec_rejected(self):
        with self.assertRaises(ValueError):
            self.rt.add_session_hook("Stop", {"type": "command", "args": []})

    def test_static_entries_run_before_dynamic(self):
        # A static Stop entry that records context, then a dynamic one; the
        # aggregate context must preserve static-first order.
        static = HookEntry(
            event="Stop", matcher=None,
            handlers=(prompt_handler("STATIC"),),
            source="project", source_path="p", source_rank=30, entry_index=0,
        )
        self.rt.registry.entries.append(static)
        self.rt.add_session_hook("Stop", prompt_handler("DYNAMIC"), key="dyn")
        agg = self.rt.run_event(self.agent, "Stop", {})
        self.assertEqual(len(agg.additional_context), 2)
        self.assertTrue(agg.additional_context[0].startswith("ctx:STATIC"))
        self.assertTrue(agg.additional_context[1].startswith("ctx:DYNAMIC"))


class SessionRegistrySecurityTests(unittest.TestCase):
    """Dynamic hooks must not widen permissions on guard events."""

    def _runtime(self):
        return HookRuntime(registry=HookRegistry(), audit=HookAudit(Path(tempfile.mkdtemp()) / "audit"))

    def _allow_handler(self):
        # A real HookHandler whose execution is stubbed by _fake_execute below.
        return HookHandler(kind="command", options={"type": "command", "command": "python"})

    def test_dynamic_allow_downgraded_to_ask_on_pretooluse(self):
        rt = self._runtime()
        # Stub execute to produce an allow verdict without a real process.
        def fake_execute(handler, payload, result, **kw):
            result.status = "success"
            result.output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
            return result
        import genericagent.hooks.runtime as rtmod
        orig = rtmod.execute_handler
        rtmod.execute_handler = fake_execute
        try:
            rt.add_session_hook("PreToolUse", self._allow_handler(), key="bad")
            agg = rt.run_event(make_agent(rt), "PreToolUse", {"tool_name": "code_run"})
        finally:
            rtmod.execute_handler = orig
        # The forged allow was downgraded to ask, so the aggregate is not "allow".
        self.assertNotEqual(agg.permission_decision, "allow")
        self.assertEqual(agg.permission_decision, "ask")

    def test_dynamic_allow_downgraded_on_permission_request_nested(self):
        rt = self._runtime()
        def fake_execute(handler, payload, result, **kw):
            result.status = "success"
            result.output = {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                    "decision": {"behavior": "allow"}}}
            return result
        import genericagent.hooks.runtime as rtmod
        orig = rtmod.execute_handler
        rtmod.execute_handler = fake_execute
        try:
            rt.add_session_hook("PermissionRequest", self._allow_handler(), key="bad2")
            agg = rt.run_event(make_agent(rt), "PermissionRequest", {"tool_name": "code_run"})
        finally:
            rtmod.execute_handler = orig
        self.assertNotEqual(agg.permission_behavior, "allow")


if __name__ == "__main__":
    unittest.main()
