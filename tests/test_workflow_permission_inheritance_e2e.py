from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_loop import exhaust
from ga import GenericAgentHandler
from session_transcript import create_session, load_session, record_turn, record_workflow_event
from workflow_child_agent import AgentResult
from workflow_models import DEFAULT_PERMISSION_POLICY_VERSION, DEFAULT_PERMISSION_PROFILE, WorkflowRun
from workflow_permissions import ToolPermissionPolicy
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import AgentScheduler, SchedulerConfig
from workflow_store import WorkflowStore


class ParentStub:
    verbose = False

    def __init__(self, task_dir: str):
        self.task_dir = task_dir
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=[]))


class SemiRealPermissionChildRunner:
    def __init__(self, *, workdir: Path, skills_root: Path):
        self.workdir = workdir
        self.skills_root = skills_root
        self.started_job_ids: list[str] = []
        self.poll_count = 0
        self.mcp_calls: list[dict] = []
        self._results: dict[str, AgentResult] = {}

    def start(self, job) -> None:
        self.started_job_ids.append(job.job_id)
        self._results[job.job_id] = self._run_job(job)

    def poll(self, job) -> AgentResult | None:
        self.poll_count += 1
        return self._results.pop(job.job_id, None)

    def cancel(self, job) -> None:
        return None

    def _run_job(self, job) -> AgentResult:
        events: list[dict] = []
        handler = GenericAgentHandler(ParentStub(str(self.workdir)), cwd=str(self.workdir))
        handler.workflow_permission_policy = ToolPermissionPolicy(profile=job.metadata.get("permissionProfile"))
        handler.workflow_permission_context = {
            "runId": job.metadata.get("runId"),
            "jobId": job.job_id,
            "permissionProfile": job.metadata.get("permissionProfile"),
            "permissionPolicyVersion": job.metadata.get("permissionPolicyVersion"),
        }
        handler.workflow_permission_event_callback = events.append
        response = SimpleNamespace(content="")
        tool_results = []
        for tool_name, args in [
            ("file_write", {"path": "child-marker.txt", "content": "CHILD_TOOL_MARKER"}),
            ("load_skill", {"skill": "deterministic-permission", "search_roots": [str(self.skills_root)]}),
            ("mcp__deterministic__write_marker", {"marker": "MCP_PERMISSION_INHERITED"}),
        ]:
            events.append({"type": "tool_call", "toolName": tool_name})
            outcome = exhaust(handler.dispatch(tool_name, dict(args), response))
            events.append({"type": "tool_result", "toolName": tool_name, "status": _status_of(outcome.data)})
            tool_results.append({"toolName": tool_name, "result": outcome.data})
        allowed = [event.get("toolName") for event in events if event.get("type") == "tool_allowed"]
        denied = [event.get("toolName") for event in events if event.get("type") == "tool_denied"]
        return AgentResult(
            job_id=job.job_id,
            status="succeeded",
            payload={
                "summary": "deterministic permission inheritance complete",
                "markerFileExists": (self.workdir / "child-marker.txt").exists(),
                "activeSkill": handler.working.get("active_skill"),
                "activeSkillAllowedTools": handler.working.get("active_skill_allowed_tools"),
                "toolResults": tool_results,
            },
            token_usage={},
            tool_summary={"allowed": len(allowed), "denied": len(denied), "allowedTools": allowed, "deniedTools": denied},
            transcript_events=events,
        )


class PermissionCacheRunner:
    def __init__(self):
        self.started_job_ids: list[str] = []

    def start(self, job) -> None:
        self.started_job_ids.append(job.job_id)

    def poll(self, job) -> AgentResult | None:
        return AgentResult(
            job_id=job.job_id,
            payload={"summary": f"fresh {job.job_id}", "permissionProfile": job.metadata.get("permissionProfile")},
            transcript_events=[{"type": "metadata", "jobId": job.job_id, "permissionProfile": job.metadata.get("permissionProfile")}],
        )

    def cancel(self, job) -> None:
        return None


def _status_of(data):
    if isinstance(data, dict):
        return data.get("status", "success")
    return "success"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


class WorkflowPermissionInheritanceE2ETest(unittest.TestCase):
    def make_skill(self, root: Path) -> Path:
        skills_root = root / "skills"
        skill_dir = skills_root / "deterministic-permission"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: deterministic-permission\n"
            "description: deterministic permission test skill\n"
            "allowed-tools: file_write, load_skill, mcp__deterministic__write_marker\n"
            "---\n"
            "DETERMINISTIC_SKILL_MARKER\n",
            encoding="utf-8",
        )
        return skills_root

    def test_inherit_current_child_records_tool_skill_mcp_events_without_polluting_parent_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkflowStore(root / "workflow_store")
            skills_root = self.make_skill(root)
            workdir = root / "child_workdir"
            workdir.mkdir()
            parent_session_id = "session_parent_permission"
            parent_path = create_session(root=root / "parent_sessions", cwd=str(root), session_id=parent_session_id, frontend="test")
            record_turn(
                parent_path,
                session_id=parent_session_id,
                turn_id=1,
                source="test",
                user_text="parent prompt",
                assistant_text="parent response",
                backend_history_before=[],
                backend_history_after=[{"role": "assistant", "content": "PARENT_ONLY_MARKER"}],
            )
            run = store.create_run(
                WorkflowRun(
                    run_id="wf_permission_inherit",
                    session_id=parent_session_id,
                    script="agent('deterministic permission inheritance', {label:'permission-child'});",
                    status="running",
                    permission_profile=DEFAULT_PERMISSION_PROFILE,
                    permission_policy_version=DEFAULT_PERMISSION_POLICY_VERSION,
                )
            )
            record_workflow_event(parent_path, session_id=parent_session_id, run_id=run.run_id, event_type="workflow_started", artifact_dir=run.artifact_dir)
            runner = SemiRealPermissionChildRunner(workdir=workdir, skills_root=skills_root)

            def fake_mcp_call(tool_name, args):
                runner.mcp_calls.append({"toolName": tool_name, "args": dict(args)})
                return {"status": "success", "marker": "MCP_PERMISSION_INHERITED", "args": dict(args)}

            with mock.patch("mcp_runtime.call_mcp_tool", side_effect=fake_mcp_call):
                scheduler = AgentScheduler(store=store, run=run, runner=runner, config=SchedulerConfig(max_concurrent=1))
                job = scheduler.register_agent(prompt="deterministic permission inheritance", label="permission-child")
                scheduler.run_all()
            record_workflow_event(parent_path, session_id=parent_session_id, run_id=run.run_id, event_type="workflow_completed", artifact_dir=run.artifact_dir, result_ref=run.result_ref)
            loaded = store.load_run(run.run_id)
            artifact_dir = Path(loaded.artifact_dir)
            transcript_events = _read_jsonl(artifact_dir / "agents" / job.job_id / "transcript.jsonl")
            result_data = _read_json(artifact_dir / "agents" / job.job_id / "result.json")
            journal_types = [event["type"] for event in _read_jsonl(artifact_dir / "journal.jsonl")]
            parent_loaded = load_session(parent_path)
            parent_raw = Path(parent_path).read_text(encoding="utf-8")
            child_marker_exists = (workdir / "child-marker.txt").exists()

        self.assertTrue(child_marker_exists)
        self.assertEqual(["agent_1"], runner.started_job_ids)
        self.assertEqual([{"toolName": "mcp__deterministic__write_marker", "args": {"marker": "MCP_PERMISSION_INHERITED"}}], runner.mcp_calls)
        self.assertEqual(DEFAULT_PERMISSION_PROFILE, job.metadata["cacheKey"]["permissionProfile"])
        self.assertEqual(DEFAULT_PERMISSION_POLICY_VERSION, job.metadata["cacheKey"]["permissionPolicyVersion"])
        self.assertIn("file_write", result_data["toolSummary"]["allowedTools"])
        self.assertIn("load_skill", result_data["toolSummary"]["allowedTools"])
        self.assertIn("mcp__deterministic__write_marker", result_data["toolSummary"]["allowedTools"])
        self.assertEqual("deterministic-permission", result_data["payload"]["activeSkill"])
        self.assertNotIn("transcriptEvents", result_data)
        self.assertEqual(
            ["permission_profile_selected", "tool_allowed", "tool_allowed", "tool_allowed"],
            [event["type"] for event in transcript_events if event.get("type") in {"permission_profile_selected", "tool_allowed", "tool_denied"}],
        )
        self.assertIn("permission_profile_selected", journal_types)
        self.assertEqual(3, journal_types.count("tool_allowed"))
        self.assertLess(journal_types.index("tool_allowed"), journal_types.index("agent_completed"))
        self.assertEqual([{"role": "assistant", "content": "PARENT_ONLY_MARKER"}], parent_loaded.backend_history)
        self.assertIn("workflow_started", parent_raw)
        self.assertIn("workflow_completed", parent_raw)
        self.assertNotIn("CHILD_TOOL_MARKER", parent_raw)
        self.assertNotIn("DETERMINISTIC_SKILL_MARKER", parent_raw)
        self.assertNotIn("MCP_PERMISSION_INHERITED", parent_raw)
        self.assertNotIn("tool_allowed", parent_raw)

    def test_read_only_child_denies_write_and_non_readonly_mcp_with_same_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkflowStore(root / "workflow_store")
            skills_root = self.make_skill(root)
            workdir = root / "child_workdir"
            workdir.mkdir()
            run = store.create_run(
                WorkflowRun(
                    run_id="wf_permission_read_only",
                    session_id="session_read_only_permission",
                    script="agent('deterministic permission read only', {label:'permission-child'});",
                    status="running",
                    permission_profile="read_only",
                    permission_policy_version="read-only-v1",
                )
            )
            runner = SemiRealPermissionChildRunner(workdir=workdir, skills_root=skills_root)

            with mock.patch("mcp_runtime.call_mcp_tool", side_effect=AssertionError("denied MCP tool must not execute")):
                scheduler = AgentScheduler(store=store, run=run, runner=runner, config=SchedulerConfig(max_concurrent=1))
                job = scheduler.register_agent(prompt="deterministic permission read only", label="permission-child")
                scheduler.run_all()
            loaded = store.load_run(run.run_id)
            artifact_dir = Path(loaded.artifact_dir)
            transcript_events = _read_jsonl(artifact_dir / "agents" / job.job_id / "transcript.jsonl")
            result_data = _read_json(artifact_dir / "agents" / job.job_id / "result.json")
            journal_events = _read_jsonl(artifact_dir / "journal.jsonl")
            child_marker_exists = (workdir / "child-marker.txt").exists()

        self.assertFalse(child_marker_exists)
        self.assertEqual("read_only", job.metadata["cacheKey"]["permissionProfile"])
        self.assertEqual("read-only-v1", job.metadata["cacheKey"]["permissionPolicyVersion"])
        self.assertIn("load_skill", result_data["toolSummary"]["allowedTools"])
        self.assertIn("file_write", result_data["toolSummary"]["deniedTools"])
        self.assertIn("mcp__deterministic__write_marker", result_data["toolSummary"]["deniedTools"])
        denied_events = [event for event in transcript_events if event.get("type") == "tool_denied"]
        self.assertEqual(["file_write", "mcp__deterministic__write_marker"], [event.get("toolName") for event in denied_events])
        self.assertEqual("read_only_static_write_or_execute", denied_events[0]["reason"])
        self.assertEqual("read_only_mcp_unknown", denied_events[1]["reason"])
        self.assertEqual(2, [event.get("type") for event in journal_events].count("tool_denied"))

    def test_permission_profile_and_version_partition_runtime_resume_cache(self):
        script = "const r = await agent('same prompt', {label:'same'}); return r;"
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(Path(tmp) / "workflow_store")
            source_runner = PermissionCacheRunner()
            source = WorkflowRun(run_id="wf_perm_source", session_id="session_perm_cache", script=script, status="running")
            source_outcome = WorkflowRuntime(store=store, runner=source_runner, scheduler_config=SchedulerConfig(max_concurrent=1), timeout_seconds=5).run(source, args={"same": True})
            read_only_runner = PermissionCacheRunner()
            read_only = WorkflowRun(run_id="wf_perm_read_only", session_id="session_perm_cache", script=script, status="running", permission_profile="read_only", permission_policy_version="read-only-v1")
            read_only_outcome = WorkflowRuntime(store=store, runner=read_only_runner, scheduler_config=SchedulerConfig(max_concurrent=1), timeout_seconds=5).run(read_only, args={"same": True}, resume_from_run_id=source.run_id)
            version_runner = PermissionCacheRunner()
            version_changed = WorkflowRun(run_id="wf_perm_v2", session_id="session_perm_cache", script=script, status="running", permission_profile=DEFAULT_PERMISSION_PROFILE, permission_policy_version="inherit-current-v2")
            version_outcome = WorkflowRuntime(store=store, runner=version_runner, scheduler_config=SchedulerConfig(max_concurrent=1), timeout_seconds=5).run(version_changed, args={"same": True}, resume_from_run_id=source.run_id)
            hit_runner = PermissionCacheRunner()
            cache_hit = WorkflowRun(run_id="wf_perm_hit", session_id="session_perm_cache", script=script, status="running")
            cache_hit_outcome = WorkflowRuntime(store=store, runner=hit_runner, scheduler_config=SchedulerConfig(max_concurrent=1), timeout_seconds=5).run(cache_hit, args={"same": True}, resume_from_run_id=source.run_id)
            loaded = [store.load_run(item.run.run_id) for item in [source_outcome, read_only_outcome, version_outcome, cache_hit_outcome]]
            source_job, read_only_job, version_job, hit_job = [run.jobs[0] for run in loaded]
            read_only_events = [event.event_type for event in store.replay_events(read_only.run_id)]
            version_events = [event.event_type for event in store.replay_events(version_changed.run_id)]
            hit_events = [event.event_type for event in store.replay_events(cache_hit.run_id)]

        self.assertEqual(["agent_1"], source_runner.started_job_ids)
        self.assertEqual(["agent_1"], read_only_runner.started_job_ids)
        self.assertEqual(["agent_1"], version_runner.started_job_ids)
        self.assertEqual([], hit_runner.started_job_ids)
        self.assertEqual("succeeded", read_only_job.status)
        self.assertEqual("succeeded", version_job.status)
        self.assertEqual("cached", hit_job.status)
        self.assertNotIn("agent_cached", read_only_events)
        self.assertNotIn("agent_cached", version_events)
        self.assertIn("agent_cached", hit_events)
        for field in ["scriptHash", "argsHash", "callIndex", "promptHash", "optionsHash"]:
            self.assertEqual(source_job.metadata["cacheKey"][field], read_only_job.metadata["cacheKey"][field])
            self.assertEqual(source_job.metadata["cacheKey"][field], version_job.metadata["cacheKey"][field])
            self.assertEqual(source_job.metadata["cacheKey"][field], hit_job.metadata["cacheKey"][field])
        self.assertEqual("read_only", read_only_job.metadata["cacheKey"]["permissionProfile"])
        self.assertEqual("read-only-v1", read_only_job.metadata["cacheKey"]["permissionPolicyVersion"])
        self.assertEqual("inherit-current-v2", version_job.metadata["cacheKey"]["permissionPolicyVersion"])
        self.assertEqual(DEFAULT_PERMISSION_PROFILE, hit_job.metadata["cacheKey"]["permissionProfile"])
        self.assertEqual(DEFAULT_PERMISSION_POLICY_VERSION, hit_job.metadata["cacheKey"]["permissionPolicyVersion"])
        self.assertTrue(hit_job.metadata["transcriptRef"].endswith("agents/agent_1/transcript.jsonl"))


if __name__ == "__main__":
    unittest.main()
