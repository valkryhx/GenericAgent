import unittest
from types import SimpleNamespace

from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
from genericagent.workflow.permissions import ToolPermissionPolicy


OBJECT_SCHEMA = {
    "type": "object",
    "properties": {"file": {"type": "string"}, "ok": {"type": "boolean"}},
    "required": ["file", "ok"],
}


def run_gen(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def make_job(options=None, result_ref=None):
    return SimpleNamespace(job_id="agent_1", phase="Verify", result_ref=result_ref,
                           metadata={"options": options or {}})


class StructuredOutputToolInjectionTest(unittest.TestCase):
    def setUp(self):
        self.runner = NativeGPTChildAgentRunner()

    def test_object_schema_appends_the_tool(self):
        tools = [{"type": "function", "function": {"name": "file_read", "parameters": {}}}]
        out = self.runner._with_structured_output_tool(tools, {"schema": OBJECT_SCHEMA})
        names = [t["function"]["name"] for t in out]
        self.assertIn("structured_output", names)
        injected = out[-1]["function"]
        self.assertEqual(OBJECT_SCHEMA, injected["parameters"])
        self.assertIn("exactly once", injected["description"])

    def test_no_schema_is_untouched(self):
        tools = [{"type": "function", "function": {"name": "file_read", "parameters": {}}}]
        self.assertEqual(tools, self.runner._with_structured_output_tool(tools, {}))

    def test_non_object_schema_is_untouched(self):
        tools = [{"type": "function", "function": {"name": "file_read", "parameters": {}}}]
        schema = {"type": "array", "items": {"type": "string"}}
        self.assertEqual(tools, self.runner._with_structured_output_tool(tools, {"schema": schema}))

    def test_not_injected_twice(self):
        tools = [{"type": "function", "function": {"name": "structured_output", "parameters": {}}}]
        out = self.runner._with_structured_output_tool(tools, {"schema": OBJECT_SCHEMA})
        self.assertEqual(1, len(out))


class StructuredOutputDispatchTest(unittest.TestCase):
    def _handler(self, schema):
        from genericagent.core.handler import GenericAgentHandler

        handler = GenericAgentHandler(SimpleNamespace(task_dir=None))
        handler.workflow_output_schema = schema
        return handler

    def test_valid_args_end_the_turn_with_the_object(self):
        handler = self._handler(OBJECT_SCHEMA)
        outcome = run_gen(handler.do_structured_output({"file": "a.py", "ok": True, "_index": 0, "_tool_num": 1}, None))
        self.assertIsNone(outcome.next_prompt)
        self.assertEqual({"status": "ok", "structured_output": {"file": "a.py", "ok": True}}, outcome.data)
        self.assertEqual({"file": "a.py", "ok": True}, handler.workflow_structured_output)

    def test_invalid_args_return_a_retry_prompt_and_do_not_stash(self):
        handler = self._handler(OBJECT_SCHEMA)
        outcome = run_gen(handler.do_structured_output({"file": "a.py"}, None))
        self.assertEqual("error", outcome.data["status"])
        self.assertTrue(outcome.next_prompt and "structured_output" in outcome.next_prompt)
        self.assertFalse(hasattr(handler, "workflow_structured_output"))

    def test_no_schema_accepts_any_object(self):
        handler = self._handler(None)
        outcome = run_gen(handler.do_structured_output({"anything": 1}, None))
        self.assertEqual("ok", outcome.data["status"])


class StructuredOutputPayloadTest(unittest.TestCase):
    def test_structured_value_wins_over_text_parsing(self):
        job = make_job({"schema": OBJECT_SCHEMA}, result_ref="agents/agent_1/result.json")
        payload = NativeGPTChildAgentRunner._build_success_payload(job, "ignored prose", structured={"file": "a.py", "ok": False})
        self.assertEqual("a.py", payload["file"])
        self.assertFalse(payload["ok"])

    def test_text_fallback_still_works(self):
        job = make_job({"schema": OBJECT_SCHEMA}, result_ref="agents/agent_1/result.json")
        payload = NativeGPTChildAgentRunner._build_success_payload(job, '{"file": "a.py", "ok": true}')
        self.assertEqual("a.py", payload["file"])


class StructuredOutputPermissionTest(unittest.TestCase):
    def test_allowed_for_read_only_and_verify(self):
        for profile in ("read_only", "verify"):
            decision = ToolPermissionPolicy(profile=profile).evaluate("structured_output")
            self.assertEqual("allow", decision.action, profile)


if __name__ == "__main__":
    unittest.main()
