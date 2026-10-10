import json
import tempfile
import unittest
from pathlib import Path

from workflow_child_agent import FakeChildAgentRunner
from workflow_controller import WorkflowController
from workflow_models import WorkflowRun
from workflow_planner import WorkflowDraft, WorkflowPlanner
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore


class _RejectingPlanner:
    """Planner whose draft fails validation, so the run is rejected."""

    def plan(self, _task_text, _context):
        return WorkflowDraft(
            task_text="坏计划",
            context={},
            classification={"taskType": "research"},
            plan={"taskType": "research", "phases": []},
            validation={"ok": False, "issues": [{"code": "empty", "message": "no phases"}]},
            script="",
        )


class _CapabilityAwareFakeRunner(FakeChildAgentRunner):
    """Fake child runner plus the capability preflight hook the real runner has.

    Regression guard for the real-run failure ``'str' object has no attribute
    'job_id'``: the runtime emitted the capability-snapshot event through
    ``AgentScheduler._append(event_type, job, payload)`` with the run object
    passed as ``event_type`` and the event name passed as ``job``. That only
    fires when the runner implements ``prepare_run_capabilities``; the plain
    fake never did, so the whole suite stayed green while every real run that
    declared a capability died before the first child started.
    """

    def __init__(self, *, missing_tools=None, unavailable=None, **kwargs):
        super().__init__(**kwargs)
        self.capability_calls: list[dict] = []
        self._missing_tools = list(missing_tools or [])
        self._unavailable = list(unavailable or [])

    def prepare_run_capabilities(self, run_id, required_tools=None, capabilities=None):
        self.capability_calls.append(
            {
                "runId": run_id,
                "requiredTools": list(required_tools or []),
                "capabilities": list(capabilities or []),
            }
        )
        present = [name for name in ("file_read", "mcp__tavily__tavily_search") if name not in self._missing_tools]
        return {
            "toolNames": present,
            "unavailableCapabilities": list(self._unavailable),
            "capabilityReport": {
                "requiredTools": list(required_tools or []),
                "missingTools": list(self._missing_tools),
                "declaredCapabilities": list(capabilities or []),
                "unavailableCapabilities": list(self._unavailable),
            },
        }


class WorkflowControllerTest(unittest.TestCase):
    def test_create_planned_run_persists_explicit_verification_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = WorkflowDraft(
                task_text="write a small change",
                context={},
                classification={"taskType": "coding"},
                plan={
                    "taskType": "coding",
                    "phases": [{"title": "Build", "agents": [{"label": "impl", "role": "implementation"}]}],
                    "verification": {
                        "level": "inline",
                        "checks": [{"id": "diff", "kind": "diff", "required": True, "owner": "host"}],
                    },
                },
                validation={"ok": True, "issues": []},
                script="phase('Build')",
            )

            class Planner:
                def plan(self, *_args, **_kwargs):
                    return draft

            run = WorkflowController(WorkflowStore(root=tmp)).create_planned_run(
                session_id="session_test",
                task_text=draft.task_text,
                planner=Planner(),
            )

            self.assertEqual("inline", run.metadata["verificationContract"]["level"])
            self.assertEqual("diff", run.metadata["verificationContract"]["checks"][0]["id"])

    def test_create_draft_persists_run_without_starting_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))

            run = controller.create_draft(session_id="session_test", script="phase('Plan')")

            self.assertEqual("draft", run.status)
            self.assertEqual("session_test", run.session_id)
            self.assertEqual("phase('Plan')", run.script)
            self.assertIsNotNone(run.artifact_dir)
            self.assertEqual(run, controller.store.load_run(run.run_id))
            self.assertEqual([], controller.store.replay_events(run.run_id))

    def test_request_approval_moves_draft_to_awaiting_approval_and_records_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.create_draft(session_id="session_test", script="")

            updated = controller.request_approval(run.run_id)

            self.assertEqual("awaiting_approval", updated.status)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(["workflow_approval_requested"], [event.event_type for event in events])
            self.assertEqual([1], [event.sequence for event in events])

    def test_approve_moves_awaiting_approval_to_running_but_does_not_launch_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.request_approval(
                controller.create_draft(session_id="session_test", script="spawn('agent')").run_id
            )

            updated = controller.approve(run.run_id)

            self.assertEqual("running", updated.status)
            self.assertEqual([], updated.jobs)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(
                ["workflow_approval_requested", "workflow_started"],
                [event.event_type for event in events],
            )

    def test_deny_moves_awaiting_approval_to_cancelled_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.request_approval(
                controller.create_draft(session_id="session_test", script="").run_id
            )

            updated = controller.deny(run.run_id, reason="not allowed")

            self.assertEqual("cancelled", updated.status)
            self.assertEqual("not allowed", updated.error)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual("workflow_denied", events[-1].event_type)
            self.assertEqual({"reason": "not allowed"}, events[-1].payload)

    def test_cancel_and_stop_record_terminal_states_without_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            cancel_run = controller.approve(
                controller.request_approval(
                    controller.create_draft(session_id="session_test", script="").run_id
                ).run_id
            )
            stop_run = controller.approve(
                controller.request_approval(
                    controller.create_draft(session_id="session_test", script="").run_id
                ).run_id
            )

            cancelled = controller.cancel(cancel_run.run_id, reason="user cancel")
            killed = controller.stop(stop_run.run_id, reason="user stop")

            self.assertEqual("cancelled", cancelled.status)
            self.assertEqual("killed", killed.status)
            self.assertEqual("workflow_cancelled", controller.store.replay_events(cancel_run.run_id)[-1].event_type)
            self.assertEqual("workflow_killed", controller.store.replay_events(stop_run.run_id)[-1].event_type)

    def test_resume_projects_interrupted_state_through_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="", status="running"))
            controller = WorkflowController(store)

            projected = controller.resume(run.run_id)

            self.assertEqual("interrupted", projected.status)
            self.assertEqual("workflow_interrupted", store.replay_events(run.run_id)[-1].event_type)

    def test_create_planned_run_auto_approves_valid_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            planner = WorkflowPlanner()

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 workflow planner control plane",
                planner=planner,
                context={"constraints": ["不要读取 mykey.py", "不要提交"]},
            )

            self.assertEqual("running", run.status)
            self.assertIn("export const meta", run.script)
            self.assertEqual("workflow-draft.json", run.metadata["workflowDraftRef"])
            # This draft was built by a directly-constructed template planner and
            # declares no plannerMode, so the metadata is honestly "unknown". The
            # production path is always model-authored now (prompt_guided), with
            # "fallback_deterministic" reserved for a planner-model failure.
            self.assertEqual("unknown", run.metadata["plannerMode"])
            self.assertEqual("research", run.metadata["workflowTaskType"])
            persisted = controller.store.load_run(run.run_id)
            self.assertEqual("running", persisted.status)
            self.assertEqual(run.metadata, persisted.metadata)
            draft_path = Path(run.artifact_dir) / "workflow-draft.json"
            draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
            self.assertEqual("调研 workflow planner control plane", draft_data["taskText"])
            self.assertTrue(draft_data["validation"]["ok"])
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(["workflow_planned", "workflow_started"], [event.event_type for event in events])
            self.assertEqual([1, 2], [event.sequence for event in events])
            self.assertEqual("workflow-draft.json", events[0].payload["workflowDraftRef"])
            self.assertEqual("unknown", events[0].payload["plannerMode"])

    def test_create_planned_coding_run_persists_acceptance_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="实现一个必须 TDD 的解析器",
                planner=WorkflowPlanner(),
                context={"constraints": ["不要读取 mykey.py", "不要提交"]},
            )

            self.assertEqual("running", run.status)
            self.assertEqual(
                {
                    "required": True,
                    "failWorkflowOnError": True,
                    "checks": ["python_unittest", "verification_schema"],
                    "testsDeclared": True,
                },
                run.metadata["acceptanceContract"],
            )

    def test_create_planned_run_writes_plan_and_orchestration_projections(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            controller = WorkflowController(store)
            draft = WorkflowPlanner().plan("调研一个技术方案")

            class FixedPlanner:
                def plan(self, _task_text, _context):
                    return draft

            run = controller.create_planned_run(
                session_id="session_projection",
                task_text="调研一个技术方案",
                planner=FixedPlanner(),
            )

            artifact_dir = Path(run.artifact_dir)
            self.assertTrue((artifact_dir / "plan.md").exists())
            self.assertTrue((artifact_dir / "orchestration.md").exists())
            self.assertEqual(
                {"plan": "plan.md", "orchestration": "orchestration.md"},
                run.metadata["workflowContractRefs"],
            )
            self.assertIn("## Mode\nworkflow", (artifact_dir / "plan.md").read_text(encoding="utf-8"))
            self.assertIn("Parent critical path", (artifact_dir / "orchestration.md").read_text(encoding="utf-8"))

    def test_create_planned_run_publishes_a_progress_snapshot_immediately(self):
        """A planned run must have progress before its first job runs.

        Regression: ``workflow-progress.json`` was written only once the runtime
        started executing jobs, so asking for progress on a freshly planned run
        produced "workflow progress is not available" even though the run was
        healthy -- the Ink UI showed a spurious error straight after a
        ``/workflow`` plan.
        """
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            draft = WorkflowPlanner().plan("调研一个技术方案")

            class FixedPlanner:
                def plan(self, _task_text, _context):
                    return draft

            run = controller.create_planned_run(
                session_id="session_progress_publish",
                task_text="调研一个技术方案",
                planner=FixedPlanner(),
            )

            progress_ref = "workflow-progress.json"
            progress_path = Path(run.artifact_dir) / progress_ref
            self.assertTrue(progress_path.is_file())
            document = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(run.run_id, document["runId"])
            self.assertEqual("running", document["status"])

    def test_every_status_transition_refreshes_the_progress_snapshot(self):
        """The snapshot must not go stale while the run waits for approval."""
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.create_planned_run(
                session_id="session_progress_states",
                task_text="调研 workflow planner control plane",
                planner=WorkflowPlanner(),
                auto_approve=False,
            )
            progress_path = Path(run.artifact_dir) / "workflow-progress.json"

            def snapshot_status() -> str:
                return json.loads(progress_path.read_text(encoding="utf-8"))["status"]

            self.assertEqual("awaiting_approval", snapshot_status())
            controller.approve(run.run_id)
            self.assertEqual("running", snapshot_status())
            controller.stop(run.run_id, reason="test stop")
            self.assertEqual("killed", snapshot_status())

    def test_rejected_plan_still_publishes_a_progress_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.create_planned_run(
                session_id="session_progress_rejected",
                task_text="坏计划",
                planner=_RejectingPlanner(),
            )

            document = json.loads(
                (Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual("failed", document["status"])

    def test_create_planned_run_can_request_approval_when_auto_approve_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 workflow planner control plane",
                planner=WorkflowPlanner(),
                context={"constraints": ["不要读取 mykey.py", "不要提交"]},
                auto_approve=False,
            )

            self.assertEqual("awaiting_approval", run.status)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(
                ["workflow_planned", "workflow_approval_requested"],
                [event.event_type for event in events],
            )

    def test_create_planned_run_honors_explicit_approval_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            controller = WorkflowController(store)
            draft = WorkflowPlanner().plan("调研一个技术方案")
            draft.plan["orchestration"]["approvalRequired"] = True

            class FixedPlanner:
                def plan(self, _task_text, _context=None):
                    return draft

            run = controller.create_planned_run(
                session_id="session_gate",
                task_text="调研一个技术方案",
                planner=FixedPlanner(),
                auto_approve=True,
            )

            self.assertEqual("awaiting_approval", run.status)
            self.assertEqual("explicit_workflow_approval_gate", run.metadata["approvalGate"]["reason"])

    def test_create_planned_run_rejects_invalid_rendered_script_before_starting(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = WorkflowDraft(
                task_text="invalid rendered workflow",
                context={},
                classification={"taskType": "research"},
                plan={"taskType": "research", "phases": [{"title": "Collect", "agents": []}]},
                validation={"ok": True, "issues": []},
                script="const research/source_1.json = {}",
            )

            class Planner:
                def plan(self, *_args, **_kwargs):
                    return draft

            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.create_planned_run(
                session_id="session_test",
                task_text=draft.task_text,
                planner=Planner(),
            )

            self.assertEqual("failed", run.status)
            self.assertEqual("workflow_plan_rejected", run.error)
            events = controller.store.replay_events(run.run_id)
            self.assertIn("invalid_workflow_script", str(events[-1].payload["issues"]))

    def test_create_planned_run_records_rejected_draft_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            draft = WorkflowDraft(
                task_text="坏计划",
                context={"plannerMode": "prompt_guided_rejected"},
                classification={"taskType": "coding"},
                plan={"taskType": "coding", "phases": []},
                validation={"ok": False, "mode": "rejected", "issues": [{"code": "missing_phase"}]},
                script="",
            )

            class RejectedPlanner:
                def plan(self, task_text, context=None):
                    return draft

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="坏计划",
                planner=RejectedPlanner(),
            )

            self.assertEqual("failed", run.status)
            self.assertEqual("workflow_plan_rejected", run.error)
            self.assertEqual("prompt_guided_rejected", run.metadata["plannerMode"])
            self.assertEqual("workflow-draft.json", run.metadata["workflowDraftRef"])
            self.assertEqual("", run.script)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(["workflow_planned", "workflow_plan_rejected"], [event.event_type for event in events])
            self.assertEqual([{"code": "missing_phase"}], events[-1].payload["issues"])

    def test_create_planned_run_records_fallback_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            draft = WorkflowPlanner().plan("调研 fallback", context={"constraints": ["不要读取 mykey.py", "不要提交"]})
            draft.context["plannerMode"] = "fallback_deterministic"
            draft.validation["mode"] = "fallback_deterministic"

            class FallbackPlanner:
                def plan(self, task_text, context=None):
                    return draft

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 fallback",
                planner=FallbackPlanner(),
            )

            self.assertEqual("running", run.status)
            self.assertEqual("fallback_deterministic", run.metadata["plannerMode"])
            self.assertEqual("fallback_deterministic", controller.store.replay_events(run.run_id)[0].payload["plannerMode"])
            # A deterministic fallback is a lower-fidelity delivery and must be
            # visible to the caller instead of masquerading as a planned run.
            self.assertTrue(run.metadata["plannerDegraded"])
            self.assertEqual(
                ["planner_fallback_deterministic"],
                [issue["code"] for issue in run.metadata["workflowIssues"]],
            )

    def test_create_planned_run_script_executes_with_fake_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            controller = WorkflowController(store)
            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 Claude Code dynamic workflow",
                planner=WorkflowPlanner(),
                context={"constraints": ["不要读取 mykey.py", "不要提交"]},
            )

            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(
                    results={
                        "agent_1": {"sources": [], "claims": [], "risks": [], "summary": "collected"},
                        "agent_2": {"summary": "synthesized"},
                    },
                    # The deterministic research plan declares a web_search
                    # capability requirement, so the fake child must actually
                    # report a search call for the contract to be satisfied.
                    tool_calls={"agent_1": ["mcp__tavily__tavily_search"]},
                ),
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=3),
                timeout_seconds=5.0,
            ).run(run)

            self.assertEqual("succeeded", outcome.run.status)
            self.assertEqual(["workflow_planned", "workflow_started"], [event.event_type for event in store.replay_events(run.run_id)[:2]])
            self.assertEqual("workflow-draft.json", store.load_run(run.run_id).metadata["workflowDraftRef"])


    def test_declared_capabilities_emit_snapshot_without_failing_the_run(self):
        """A runner with capability preflight must not break the run.

        The snapshot event goes through ``AgentScheduler._append`` whose first
        positional argument is the event type, not the run. Passing the run
        first made ``job`` a string and crashed on ``job.job_id``.
        """

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            controller = WorkflowController(store)
            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 Claude Code dynamic workflow",
                planner=WorkflowPlanner(),
            )
            runner = _CapabilityAwareFakeRunner(
                results={
                    "agent_1": {"sources": [], "claims": [], "risks": [], "summary": "collected"},
                    "agent_2": {"summary": "synthesized"},
                },
                tool_calls={"agent_1": ["mcp__tavily__tavily_search"]},
            )

            outcome = WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=3),
                timeout_seconds=5.0,
            ).run(run)

            self.assertTrue(runner.capability_calls, "capability preflight was never invoked")
            self.assertEqual("succeeded", outcome.run.status)
            snapshot_events = [
                event for event in store.replay_events(run.run_id) if event.event_type == "workflow_capability_snapshot"
            ]
            self.assertEqual(1, len(snapshot_events))
            self.assertIsNone(snapshot_events[0].job_id)


    def test_missing_capability_degrades_the_run_instead_of_failing_it(self):
        """A disconnected search server is an environment fact, not a dead run.

        The capability preflight records a visible ``capability_unavailable``
        issue, the execution-contract evidence check treats the absent
        capability as explained, and the terminal state is ``degraded`` so the
        run can never report a clean pass.
        """

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            controller = WorkflowController(store)
            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 Claude Code dynamic workflow",
                planner=WorkflowPlanner(),
            )
            runner = _CapabilityAwareFakeRunner(
                results={
                    "agent_1": {"sources": [], "claims": [], "risks": [], "summary": "collected"},
                    "agent_2": {"summary": "synthesized"},
                },
                missing_tools=["mcp__tavily__tavily_search"],
                unavailable=["web_search"],
            )

            outcome = WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=3),
                timeout_seconds=5.0,
            ).run(run)

            self.assertEqual("degraded", outcome.run.status)
            codes = [issue.get("code") for issue in (outcome.run.metadata or {}).get("workflowIssues") or []]
            self.assertIn("capability_unavailable", codes)


    def test_every_published_result_ref_resolves_under_the_run_workspace(self):
        """A ref handed to a reader must open under the root the reader knows.

        Regression (run ``wf_92ad839265dc48f3ac56e5cb86f6e780``): the GA agent
        joined ``workspacePath`` with the bare ``resultRef:
        agents/agent_1/result.json``, got a well-formed path that does not exist,
        and reported the durable result as missing. ``resultRef`` is relative to
        the run's internal artifact directory, so every reader-facing surface now
        publishes the host's workspace-relative copy instead.
        """

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "temp"
            base.mkdir()
            store = WorkflowStore(root=Path(tmp) / "runs")
            controller = WorkflowController(store)
            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 Claude Code dynamic workflow",
                planner=WorkflowPlanner(),
                workspace_path=str(base),
            )
            runner = _CapabilityAwareFakeRunner(
                results={
                    "agent_1": {"sources": [], "claims": [], "risks": [], "summary": "collected"},
                    "agent_2": {"summary": "synthesized"},
                },
                tool_calls={"agent_1": ["mcp__tavily__tavily_search"]},
            )

            outcome = WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=3),
                timeout_seconds=5.0,
            ).run(run)

            self.assertEqual("succeeded", outcome.run.status)
            loaded = store.load_run(run.run_id)
            workspace = Path(loaded.metadata["workspacePath"])
            store.write_workflow_progress(loaded)
            progress = json.loads((Path(loaded.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
            item = progress["workflowProgress"][0]

            # The exact join the GA agent performed now lands on a real file.
            self.assertEqual("workflow-handoffs/result-agent_1.json", item["resultRef"])
            self.assertTrue((workspace / item["resultRef"]).is_file())
            self.assertTrue(Path(item["resultPath"]).is_file())
            self.assertEqual("agents/agent_1/result.json", item["runInternalResultRef"])
            # Nothing publishes the bare run-internal ref any more, including the
            # nested handoff copy that carried the trap.
            self.assertEqual(item["resultRef"], item["handoff"]["resultRef"])
            self.assertFalse((workspace / "agents/agent_1/result.json").is_file())

    def test_each_planned_run_gets_its_own_workspace_under_the_base_root(self):
        """Concurrent runs must not share a workspace directory.

        Regression: two runs wrote into the same ``temp/`` root, so the second
        run's artifacts overwrote the first run's, and a handoff could point at
        a file a different run had produced.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "temp"
            base.mkdir()
            controller = WorkflowController(WorkflowStore(root=Path(tmp) / "runs"))
            planner = WorkflowPlanner()

            first = controller.create_planned_run(
                session_id="session_test",
                task_text="调研一",
                planner=planner,
                workspace_path=str(base),
            )
            second = controller.create_planned_run(
                session_id="session_test",
                task_text="调研二",
                planner=planner,
                workspace_path=str(base),
            )

            first_workspace = Path(first.metadata["workspacePath"])
            second_workspace = Path(second.metadata["workspacePath"])
            self.assertNotEqual(first_workspace, second_workspace)
            self.assertEqual(base / "workflow-runs" / first.run_id, first_workspace)
            self.assertTrue(first_workspace.is_dir())
            self.assertTrue(second_workspace.is_dir())
            self.assertEqual(str(base.resolve()), first.metadata["workspaceBasePath"])

    def test_run_workspace_can_be_disabled_for_callers_that_need_the_shared_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "temp"
            base.mkdir()
            controller = WorkflowController(WorkflowStore(root=Path(tmp) / "runs"))

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="共享工作区",
                planner=WorkflowPlanner(),
                workspace_path=str(base),
                run_workspace=False,
            )

            self.assertNotIn("workspacePath", run.metadata)
            self.assertNotIn("workflow-runs", {path.name for path in base.iterdir()})


if __name__ == "__main__":
    unittest.main()
