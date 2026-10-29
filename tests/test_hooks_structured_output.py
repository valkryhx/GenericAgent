"""Stage D: structured output channel for prompt/agent hooks.

The goal evaluator contract is {ok, reason, impossible?} (CC 303 m0459). When a
handler declares outputSchema, the reply is parsed strictly: non-JSON or a
schema mismatch is ``invalid_output`` with NO plain-text fallback. A valid triple
is interpreted as a decision (ok:false blocks; impossible:true is terminal but
non-blocking).
"""
import unittest

from genericagent.hooks.models import HookAggregate, HookHandler, HookResult
from genericagent.hooks.runner import execute_handler, _validate_against_schema, _strict_json

EVAL_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}, "impossible": {"type": "boolean"}},
    "required": ["ok", "reason"],
    "additionalProperties": False,
}


def _run_structured(reply, schema=EVAL_SCHEMA, event="Stop"):
    handler = HookHandler("prompt", {"prompt": "judge", "outputSchema": schema})
    result = HookResult(event, handler, "test", "", "h", "running")
    execute_handler(handler, {"hook_event_name": event}, result, prompt_runner=lambda k, p, d: reply)
    return result


class StructuredParseTests(unittest.TestCase):
    def test_valid_met(self):
        r = _run_structured('{"ok": true, "reason": "done"}')
        self.assertEqual(r.status, "success")
        self.assertEqual(r.output["ok"], True)

    def test_valid_json_in_code_fence(self):
        r = _run_structured('```json\n{"ok": false, "reason": "missing"}\n```')
        self.assertEqual(r.status, "success")
        self.assertEqual(r.output["ok"], False)

    def test_non_json_is_invalid_output_no_fallback(self):
        r = _run_structured("Let me evaluate whether the condition is satisfied...")
        self.assertEqual(r.status, "invalid_output")
        self.assertEqual(r.additional_context, "")  # never falls back to additionalContext

    def test_missing_required_field_is_invalid(self):
        r = _run_structured('{"reason": "no ok field"}')
        self.assertEqual(r.status, "invalid_output")
        self.assertIn("missing required field", r.error)

    def test_additional_property_is_invalid(self):
        r = _run_structured('{"ok": true, "reason": "x", "extra": 1}')
        self.assertEqual(r.status, "invalid_output")
        self.assertIn("unexpected field", r.error)

    def test_wrong_type_is_invalid(self):
        r = _run_structured('{"ok": "yes", "reason": "x"}')
        self.assertEqual(r.status, "invalid_output")
        self.assertIn("expected type", r.error)

    def test_without_schema_plain_text_still_works(self):
        handler = HookHandler("prompt", {"prompt": "x"})
        result = HookResult("PostToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PostToolUse"}, result,
                        prompt_runner=lambda k, p, d: "plain context")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.additional_context, "plain context")


class StructuredDecisionTests(unittest.TestCase):
    def _agg(self, output):
        handler = HookHandler("prompt", {"prompt": "judge", "outputSchema": EVAL_SCHEMA})
        agg = HookAggregate("Stop")
        agg.add(HookResult("Stop", handler, "test", "", "h", "success", output=output))
        return agg

    def test_ok_true_is_not_blocked(self):
        agg = self._agg({"ok": True, "reason": "done"})
        self.assertFalse(agg.blocked)
        self.assertEqual(agg.reason, "done")

    def test_ok_false_blocks(self):
        agg = self._agg({"ok": False, "reason": "not yet"})
        self.assertTrue(agg.blocked)
        self.assertEqual(agg.decision, "block")
        self.assertEqual(agg.reason, "not yet")

    def test_impossible_is_terminal_but_not_blocked(self):
        agg = self._agg({"ok": False, "impossible": True, "reason": "cannot be done"})
        self.assertFalse(agg.blocked)
        self.assertEqual(agg.reason, "cannot be done")

    def test_plain_dict_ok_is_ignored_for_non_evaluator_hook(self):
        # A command hook returning {"ok": ...} must not be read as a decision.
        handler = HookHandler("command", {"command": "x"})
        agg = HookAggregate("Stop")
        agg.add(HookResult("Stop", handler, "test", "", "h", "success", output={"ok": False, "reason": "x"}))
        self.assertFalse(agg.blocked)


class SchemaValidatorTests(unittest.TestCase):
    def test_min_enum_ok(self):
        self.assertEqual(_validate_against_schema("a", {"type": "string", "enum": ["a", "b"]}), "")

    def test_min_enum_fail(self):
        self.assertIn("not in enum", _validate_against_schema("c", {"type": "string", "enum": ["a"]}))

    def test_nested_items(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertIn("[1]", _validate_against_schema([1, "x"], schema))

    def test_strict_json_code_fence(self):
        self.assertEqual(_strict_json("```json\n{\"a\": 1}\n```"), {"a": 1})


if __name__ == "__main__":
    unittest.main()
