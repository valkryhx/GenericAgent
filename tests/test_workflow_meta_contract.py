import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_script_validation import validate_rendered_workflow_script
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "workflow_script_contract.js"
NODE = "node.exe" if sys.platform.startswith("win") else "node"


def contract_result(script: str) -> dict:
    completed = subprocess.run(
        [NODE, str(CONTRACT_PATH), "--check"],
        input=script,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=5,
        check=False,
    )
    payload = json.loads((completed.stdout or "").strip() or "{}")
    payload["_returncode"] = completed.returncode
    return payload


def with_meta(body: str = "return 1") -> str:
    return "export const meta = { name: 'contract-demo', description: 'contract demo' }\n" + body


class WorkflowMetaContractTest(unittest.TestCase):
    def assert_contract_error(self, script: str, expected: str) -> None:
        result = contract_result(script)
        self.assertFalse(result.get("ok"), result)
        self.assertIn(expected, result.get("error", ""))

    def test_accepts_valid_meta_and_extracts_body(self):
        script = with_meta("phase('Build')\nreturn 1")
        result = contract_result(script)

        self.assertTrue(result["ok"], result)
        self.assertEqual("contract-demo", result["meta"]["name"])
        self.assertEqual("contract demo", result["meta"]["description"])
        self.assertIn("phase('Build')", result["scriptBody"])
        self.assertNotIn("export const meta", result["scriptBody"])

    def test_accepts_phases_title_when_to_use_and_negative_number(self):
        script = (
            "export const meta = {\n"
            "  name: 'demo',\n"
            "  description: 'demo',\n"
            "  title: 'Demo title',\n"
            "  whenToUse: 'when needed',\n"
            "  phases: [{ title: 'Build', detail: 'detail', model: 'sonnet' }],\n"
            "  offset: -2,\n"
            "}\n"
            "return 1"
        )
        result = contract_result(script)

        self.assertTrue(result["ok"], result)
        self.assertEqual("Demo title", result["meta"]["title"])
        self.assertEqual("when needed", result["meta"]["whenToUse"])
        self.assertEqual([{"title": "Build", "detail": "detail", "model": "sonnet"}], result["meta"]["phases"])

    def test_rejects_meta_not_first_statement(self):
        self.assert_contract_error(
            "const before = 1\nexport const meta = { name: 'demo', description: 'demo' }\nreturn 1",
            "must be the FIRST statement",
        )

    def test_rejects_const_meta_without_export(self):
        self.assert_contract_error(
            "const meta = { name: 'demo', description: 'demo' }\nreturn 1",
            "must be the FIRST statement",
        )

    def test_rejects_non_const_meta(self):
        self.assert_contract_error(
            "export let meta = { name: 'demo', description: 'demo' }\nreturn 1",
            "must be the FIRST statement",
        )

    def test_rejects_non_object_meta(self):
        self.assert_contract_error(
            "export const meta = ['demo', 'demo']\nreturn 1",
            "must be the FIRST statement",
        )

    def test_rejects_variable_initializer(self):
        self.assert_contract_error(
            "const name = 'demo'\nexport const meta = { name, description: 'demo' }\nreturn 1",
            "must be the FIRST statement",
        )
        self.assert_contract_error(
            "export const meta = { name: name, description: 'demo' }\nreturn 1",
            "non-literal node type in meta: Identifier",
        )

    def test_rejects_function_call_in_meta(self):
        self.assert_contract_error(
            "export const meta = { name: makeName(), description: 'demo' }\nreturn 1",
            "non-literal node type in meta: CallExpression",
        )

    def test_rejects_spread_in_meta(self):
        self.assert_contract_error(
            "export const meta = { ...base, name: 'demo', description: 'demo' }\nreturn 1",
            "only plain properties allowed in meta",
        )

    def test_rejects_computed_key_in_meta(self):
        self.assert_contract_error(
            "export const meta = { name: 'demo', description: 'demo', ['x']: 1 }\nreturn 1",
            "computed keys not allowed in meta",
        )

    def test_rejects_method_and_accessor_in_meta(self):
        self.assert_contract_error(
            "export const meta = { name: 'demo', description: 'demo', run() {} }\nreturn 1",
            "methods/accessors not allowed in meta",
        )
        self.assert_contract_error(
            "export const meta = { name: 'demo', description: 'demo', get title() { return 'x' } }\nreturn 1",
            "methods/accessors not allowed in meta",
        )

    def test_rejects_reserved_key_in_meta(self):
        self.assert_contract_error(
            "export const meta = { name: 'demo', description: 'demo', __proto__: {} }\nreturn 1",
            "reserved key name not allowed in meta: __proto__",
        )

    def test_rejects_template_interpolation_in_meta(self):
        self.assert_contract_error(
            "export const meta = { name: 'demo', description: `x${name}` }\nreturn 1",
            "template interpolation not allowed in meta",
        )

    def test_rejects_sparse_array_in_meta(self):
        self.assert_contract_error(
            "export const meta = { name: 'demo', description: 'demo', phases: [,] }\nreturn 1",
            "sparse arrays not allowed",
        )

    def test_rejects_missing_name(self):
        self.assert_contract_error(
            "export const meta = { description: 'demo' }\nreturn 1",
            "meta.name must be a non-empty string",
        )

    def test_rejects_missing_description(self):
        self.assert_contract_error(
            "export const meta = { name: 'demo' }\nreturn 1",
            "meta.description must be a non-empty string",
        )

    def test_rejects_oversized_script(self):
        script = "export const meta = { name: 'demo', description: 'demo' }\n" + (" " * 524289)
        self.assert_contract_error(script, "Script exceeds 524288 bytes")

    def test_planner_preflight_rejects_invalid_meta(self):
        result = validate_rendered_workflow_script("return 1")

        self.assertFalse(result["ok"])
        self.assertIn("must be the FIRST statement", result["error"])

    def test_runtime_rejects_invalid_meta_before_vm(self):
        script = "export const meta = { name: 'demo' }\nreturn 1"
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_bad_meta", session_id="session", script=script, status="running"))
            runtime = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=1),
                timeout_seconds=5.0,
            )

            with self.assertRaises(RuntimeError) as raised:
                runtime.run(run)
            loaded = store.load_run(run.run_id)

        self.assertIn("meta.description must be a non-empty string", str(raised.exception))
        self.assertEqual("failed", loaded.status)
        self.assertIn("meta.description must be a non-empty string", loaded.error or "")

    def test_runtime_rejects_missing_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_no_meta", session_id="session", script="return 1", status="running"))
            runtime = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=1),
    
                timeout_seconds=5.0,
            )

            with self.assertRaises(RuntimeError) as raised:
                runtime.run(run)
            loaded = store.load_run(run.run_id)

        self.assertIn("must be the FIRST statement", str(raised.exception))
        self.assertEqual("failed", loaded.status)


if __name__ == "__main__":
    unittest.main()
