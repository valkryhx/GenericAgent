import threading
import time
import unittest
from types import SimpleNamespace

from agent_loop import StepOutcome, exhaust
from ga import GenericAgentHandler
from permission_policy import ASK, FULL_ACCESS, READ_ONLY, build_permission_mode_policy
from permission_runtime import (
    ACCEPT,
    DENY,
    PermissionRuntime,
    format_args_preview,
    normalize_decision,
)


class NormalizeAndPreviewTest(unittest.TestCase):
    def test_normalize_accept_aliases(self):
        for raw in ("accept", "Allow", "allow_once", "approved", "YES"):
            self.assertEqual(ACCEPT, normalize_decision(raw))

    def test_normalize_unknown_is_deny(self):
        self.assertEqual(DENY, normalize_decision("nope"))
        self.assertEqual(DENY, normalize_decision(None))

    def test_args_preview_truncates(self):
        preview = format_args_preview({"content": "x" * 500}, max_len=40)
        self.assertLessEqual(len(preview), 40)
        self.assertTrue(preview.endswith("..."))


class PermissionRuntimeTest(unittest.TestCase):
    def test_headless_without_emit_is_deny(self):
        runtime = PermissionRuntime()
        self.assertEqual(DENY, runtime.wait_for_decision("file_write", {}, "r"))

    def test_accept_via_resolve(self):
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)

        result = {}

        def waiter():
            result["d"] = runtime.wait_for_decision("file_write", {"path": "a"}, "need")

        t = threading.Thread(target=waiter)
        t.start()
        for _ in range(50):
            if events and events[0].get("type") == "permission_request":
                break
            time.sleep(0.02)
        self.assertTrue(events)
        req = events[0]
        self.assertEqual("permission_request", req["type"])
        self.assertEqual("file_write", req["toolName"])
        self.assertTrue(runtime.resolve(req["requestId"], "accept"))
        t.join(timeout=2)
        self.assertEqual(ACCEPT, result.get("d"))
        self.assertTrue(any(e.get("type") == "permission_request_settled" for e in events))

    def test_deny_via_resolve(self):
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        result = {}

        def waiter():
            result["d"] = runtime.wait_for_decision("code_run", {}, "need")

        t = threading.Thread(target=waiter)
        t.start()
        for _ in range(50):
            if events:
                break
            time.sleep(0.02)
        runtime.resolve(events[0]["requestId"], "deny")
        t.join(timeout=2)
        self.assertEqual(DENY, result.get("d"))

    def test_resolve_once_ignores_duplicate(self):
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        result = {}

        def waiter():
            result["d"] = runtime.wait_for_decision("file_write", {}, "need")

        t = threading.Thread(target=waiter)
        t.start()
        for _ in range(50):
            if events:
                break
            time.sleep(0.02)
        rid = events[0]["requestId"]
        self.assertTrue(runtime.resolve(rid, "accept"))
        self.assertFalse(runtime.resolve(rid, "deny"))
        t.join(timeout=2)
        self.assertEqual(ACCEPT, result.get("d"))

    def test_cancel_all_denies_pending(self):
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        result = {}

        def waiter():
            result["d"] = runtime.wait_for_decision("file_write", {}, "need")

        t = threading.Thread(target=waiter)
        t.start()
        for _ in range(50):
            if events:
                break
            time.sleep(0.02)
        n = runtime.cancel_all()
        self.assertGreaterEqual(n, 1)
        t.join(timeout=2)
        self.assertEqual(DENY, result.get("d"))
        self.assertEqual(0, runtime.pending_count())

    def test_stop_check_denies(self):
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        stop = {"v": False}

        def waiter():
            return runtime.wait_for_decision(
                "file_write", {}, "need", stop_check=lambda: stop["v"], poll_seconds=0.05
            )

        result = {}

        def run():
            result["d"] = waiter()

        t = threading.Thread(target=run)
        t.start()
        for _ in range(50):
            if events:
                break
            time.sleep(0.02)
        stop["v"] = True
        t.join(timeout=2)
        self.assertEqual(DENY, result.get("d"))


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


class HandlerBlockingAskTest(unittest.TestCase):
    def response(self):
        return SimpleNamespace(content="")

    def test_ask_without_runtime_denies_and_does_not_execute(self):
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(ASK)
        handler.permission_runtime = None
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("error", outcome.data["status"])
        self.assertEqual("deny", outcome.data["permission"]["action"])

    def test_ask_accept_executes_tool(self):
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(ASK)
        handler.permission_runtime = runtime
        outcome_box = {}

        def run():
            outcome_box["o"] = exhaust(handler.dispatch("file_write", {"path": "x"}, self.response()))

        t = threading.Thread(target=run)
        t.start()
        for _ in range(50):
            if events and events[0].get("type") == "permission_request":
                break
            time.sleep(0.02)
        self.assertTrue(events)
        runtime.resolve(events[0]["requestId"], "accept")
        t.join(timeout=2)
        self.assertIn("tool:file_write", handler.calls)
        self.assertEqual({"status": "success"}, outcome_box["o"].data)

    def test_ask_deny_does_not_execute(self):
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(ASK)
        handler.permission_runtime = runtime
        outcome_box = {}

        def run():
            outcome_box["o"] = exhaust(handler.dispatch("file_write", {}, self.response()))

        t = threading.Thread(target=run)
        t.start()
        for _ in range(50):
            if events:
                break
            time.sleep(0.02)
        runtime.resolve(events[0]["requestId"], "deny")
        t.join(timeout=2)
        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("error", outcome_box["o"].data["status"])

    def test_read_only_still_denies_without_prompt(self):
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(READ_ONLY)
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        handler.permission_runtime = runtime
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual([], events)
        self.assertNotIn("tool:file_write", handler.calls)
        self.assertEqual("deny", outcome.data["permission"]["action"])

    def test_full_access_no_prompt(self):
        handler = SpyHandler()
        handler.permission_mode_policy = build_permission_mode_policy(FULL_ACCESS)
        events = []
        runtime = PermissionRuntime()
        runtime.set_emit(events.append)
        handler.permission_runtime = runtime
        outcome = exhaust(handler.dispatch("file_write", {}, self.response()))
        self.assertEqual([], events)
        self.assertIn("tool:file_write", handler.calls)
        self.assertEqual({"status": "success"}, outcome.data)


if __name__ == "__main__":
    unittest.main()
