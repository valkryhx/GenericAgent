import json
import tempfile
import unittest
from pathlib import Path

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import WorkflowPlanner
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore


class WorkflowPlannerCompilerTest(unittest.TestCase):
    def test_research_task_plans_validates_renders_persists_and_runs_with_fake_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            planner = WorkflowPlanner()

            draft = planner.plan(
                "调研 Claude Code dynamic workflow 如何根据任务动态生成脚本",
                context={"sessionId": "session_planner", "constraints": ["不要读取 mykey.py", "不要提交"]},
            )

            self.assertEqual("research", draft.classification["taskType"])
            self.assertEqual("read_only", draft.classification["readWriteMode"])
            self.assertTrue(draft.validation["ok"], draft.validation)
            self.assertEqual([], draft.validation["issues"])
            self.assertEqual("dynamic-workflow-research", draft.plan["meta"]["name"])
            self.assertEqual(["Source Discovery", "Synthesis"], [phase["title"] for phase in draft.plan["phases"]])
            self.assertIn("export const meta", draft.script)
            self.assertIn("phase('Source Discovery')", draft.script)
            self.assertIn("label: 'source-discovery'", draft.script)
            self.assertNotIn("require(", draft.script)
            self.assertNotIn("process.", draft.script)

            store = WorkflowStore(root=tmp)
            run = store.create_run(
                WorkflowRun(
                    run_id="wf_planner_slice",
                    session_id="session_planner",
                    script=draft.script,
                    status="running",
                    metadata={"workflowDraft": draft.to_dict()},
                )
            )
            draft_ref = store.write_workflow_draft(run, draft)
            self.assertEqual("workflow-draft.json", draft_ref)
            persisted_draft = json.loads((Path(run.artifact_dir) / draft_ref).read_text(encoding="utf-8"))
            self.assertEqual(draft.plan, persisted_draft["plan"])
            self.assertEqual(draft.validation, persisted_draft["validation"])
            self.assertNotIn("sk-", json.dumps(persisted_draft, ensure_ascii=False))

            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=3),
                timeout_seconds=5.0,
            ).run(run)

            self.assertEqual("succeeded", outcome.run.status)
            self.assertEqual(["Source Discovery", "Synthesis"], outcome.phases)
            self.assertEqual(["agent_1", "agent_2"], [job.job_id for job in store.load_run(run.run_id).jobs])
            self.assertEqual("completed agent_2", outcome.result["synthesis"]["summary"])
    def test_coding_task_plans_understand_tests_implementation_and_verification_in_order(self):
        planner = WorkflowPlanner()

        draft = planner.plan(
            "实现 Dynamic Workflow Planner / Compiler 的最小 TDD 切片",
            context={"sessionId": "session_planner", "constraints": ["不要读取 mykey.py", "不要提交"]},
        )

        self.assertEqual("coding", draft.classification["taskType"])
        self.assertEqual("may_write", draft.classification["readWriteMode"])
        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual(
            ["Understand", "Tests", "Implementation", "Verification"],
            [phase["title"] for phase in draft.plan["phases"]],
        )
        self.assertEqual(
            [[], ["understand"], ["write-tests"], ["implement"]],
            [phase["agents"][0].get("dependsOn", []) for phase in draft.plan["phases"]],
        )
        self.assertLess(draft.script.index("label: 'write-tests'"), draft.script.index("label: 'implement'"))
        self.assertNotIn("await parallel([", draft.script)


if __name__ == "__main__":
    unittest.main()
