"""Opt-in real ``gpt-5.6-luna`` cross-engine control-plane E2E.

The process and workflow calls are deliberately sequential.  Both children receive the same
bounded marker request, then the common facade is used to inspect their durable records,
events, result references, workspace metadata, and capability boundaries.

Run directly with ``GA_RUN_REAL_P2_1_E2E=1 python tests/real_p2_1_agent_control_e2e.py``.
The default path is a pure skip and does not construct an executable or make a network call.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


TARGET_PROFILE = "luna"
EXPECTED_MODEL = "gpt-5.6-luna"
EXPECTED_PROVIDER = "gpt-super-responses"
MARKER = "GA_P2_1_LUNA_OK_20260806"
OPT_IN = os.environ.get("GA_RUN_REAL_P2_1_E2E") == "1"

PROCESS_PROMPT = f"不要调用工具，只输出这一行精确文本：`{MARKER}`"
WORKFLOW_SCRIPT = f"""
const result = await agent('不要调用工具，只输出这一行精确文本：`{MARKER}`', {{ label: 'p2-1-luna-child' }})
return {{ summary: result.summary }}
"""


def _wait_for(predicate, *, timeout_s: float, interval_s: float = 0.25):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    return None


def _resolve_target() -> tuple[int, dict[str, str]]:
    """Resolve both the process client index and the workflow binding from llm.yaml."""
    from llm_client import load_clients_from_yaml
    from llm_config import find_llm_config, load_llm_config
    from workflow_llm import binding_from_profile

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        clients, _active, _config_path, _mtime = load_clients_from_yaml(start_dir=REPO)

    process_index = None
    for index, client in enumerate(clients):
        backend = getattr(client, "backend", None)
        profile = str(getattr(backend, "name", "") or "")
        model = str(getattr(backend, "model", "") or "")
        if profile == TARGET_PROFILE and model == EXPECTED_MODEL:
            process_index = index
            break
    if process_index is None:
        raise RuntimeError(f"target profile/model not found: {TARGET_PROFILE}/{EXPECTED_MODEL}")

    config_path = find_llm_config(start_dir=REPO)
    if not config_path:
        raise FileNotFoundError("llm.yaml not found")
    config = load_llm_config(config_path)
    binding = binding_from_profile(TARGET_PROFILE, config=config)
    if binding.model_id != EXPECTED_MODEL:
        raise RuntimeError(f"profile model mismatch for {TARGET_PROFILE}")
    resolved = config.resolve(TARGET_PROFILE)
    provider = str(getattr(resolved, "provider_key", "") or "")
    if provider != EXPECTED_PROVIDER:
        raise RuntimeError(f"profile provider mismatch for {TARGET_PROFILE}")
    return process_index, {
        "profile": TARGET_PROFILE,
        "model": binding.model_id,
        "provider": provider,
    }


def _process_state(manager, task_name: str):
    try:
        return manager.probe_agent(task_name)
    except (FileNotFoundError, OSError, ValueError):
        return None


def _find_marker_file(task_dir: Path) -> str | None:
    for path in sorted(task_dir.glob("output*.txt")):
        try:
            if MARKER in path.read_text(encoding="utf-8", errors="replace"):
                return path.name
        except OSError:
            continue
    return None


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_error(exc: Exception, roots: list[Path]) -> str:
    message = f"{type(exc).__name__}: {exc}"
    for root in roots:
        message = message.replace(str(root), "<test-root>")
    return message[:500]


def _record_shape(record) -> dict[str, Any]:
    return {
        "engine": record.engine,
        "kind": record.record_kind,
        "status": record.status,
        "sourceStatus": record.source_status,
        "workspacePresent": bool(record.workspace and Path(record.workspace).is_dir()),
        "permissionProfilePresent": bool(record.permission_profile),
        "permissionPolicyPresent": bool(record.permission_policy_version),
        "artifactCount": len(record.artifact_refs),
        "transcriptPresent": bool(record.transcript_ref),
    }


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": not OPT_IN,
        "profile": TARGET_PROFILE,
        "model": EXPECTED_MODEL,
        "issues": [],
    }
    if not OPT_IN:
        summary["reason"] = "set GA_RUN_REAL_P2_1_E2E=1 to run the real gpt-5.6-luna E2E"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    roots: list[Path] = [REPO]
    manager = None
    process_task_name: str | None = None
    process_pid: int | None = None
    temp_dir = None
    try:
        process_index, target = _resolve_target()
        summary.update({"provider": target["provider"], "processClientIndex": process_index})

        from agent_control import UnifiedAgentControl
        from agent_control_process import ProcessSubagentAdapter
        from agent_control_workflow import WorkflowChildAdapter
        from agent_runtime_models import make_workflow_source_cursor
        from subagent_manager import SubagentManager
        from workflow_child_agent import NativeGPTChildAgentRunner
        from workflow_controller import WorkflowController
        from workflow_models import WorkflowRun
        from workflow_runtime import WorkflowRuntime
        from workflow_scheduler import SchedulerConfig
        from workflow_store import WorkflowStore

        try:
            temp_dir = tempfile.TemporaryDirectory(prefix="ga_real_p2_1_")
            temp_root = Path(temp_dir.name)
            process_root = temp_root / "process"
            workflow_root = temp_root / "workflow-store"
            workflow_workspace = temp_root / "workflow-workspace"
            process_root.mkdir()
            workflow_workspace.mkdir()
            roots.append(temp_root)

            # Run the real process engine first, and keep its process alive until the facade
            # has read the common records and result references.
            manager = SubagentManager(root_dir=process_root, python_executable=sys.executable)
            handle = manager.spawn_agent(
                "p2_1_process",
                PROCESS_PROMPT,
                llm_no=process_index,
                verbose=False,
                parent_session_id="real_p2_1_agent_control",
                parent_permission_mode="read_only",
            )
            process_task_name = handle.task_name
            process_pid = handle.pid
            process_task_dir = Path(handle.task_dir)
            terminal_state = _wait_for(
                lambda: (
                    state
                    if (state := _process_state(manager, handle.task_name)) is not None
                    and state.turn_status in {"completed", "errored", "failed", "interrupted"}
                    else None
                ),
                timeout_s=180.0,
            )
            if terminal_state is None:
                raise TimeoutError("process child did not reach a terminal turn state")
            if terminal_state.turn_status != "completed":
                raise RuntimeError(f"process child turn failed: {terminal_state.turn_status}")
            process_output_file = _wait_for(
                lambda: _find_marker_file(process_task_dir),
                timeout_s=30.0,
            )
            if process_output_file is None:
                raise RuntimeError("process child marker output missing")
            process_raw_state = _read_json(process_task_dir / "state.json")
            if process_raw_state.get("llm_no") != process_index:
                summary["issues"].append("process_llm_index_mismatch")

            # Run one real workflow child only after the process call has completed.  The
            # runner uses a fresh session with tools disabled, so this is a separate execution
            # path while still using the same YAML profile.
            store = WorkflowStore(root=workflow_root)
            run = store.create_run(
                WorkflowRun(
                    run_id="wf_p2_1_luna",
                    session_id="real_p2_1_agent_control",
                    script=WORKFLOW_SCRIPT,
                    status="running",
                )
            )
            controller = WorkflowController(store=store)
            runner = NativeGPTChildAgentRunner(
                profile_name=TARGET_PROFILE,
                enable_tools=False,
                max_tokens=256,
                max_turns=4,
            )
            outcome = WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=1),
                timeout_seconds=180.0,
            ).run(run, args={"workspacePath": str(workflow_workspace)})
            loaded_run = store.load_run(run.run_id)
            if loaded_run.status != "succeeded":
                summary["issues"].append("workflow_run_not_succeeded")
            if len(loaded_run.jobs) != 1 or loaded_run.jobs[0].status != "succeeded":
                summary["issues"].append("workflow_child_not_succeeded")
            if MARKER not in json.dumps(outcome.result, ensure_ascii=False):
                summary["issues"].append("workflow_marker_missing")
            if (runner.last_llm_binding or {}).get("llmModel") != EXPECTED_MODEL:
                summary["issues"].append("workflow_llm_model_mismatch")

            control = UnifiedAgentControl(
                [ProcessSubagentAdapter(manager), WorkflowChildAdapter(store, controller)]
            )
            records = control.list_records(include_terminal=True)
            process_records = [record for record in records if record.record_kind == "process_agent"]
            workflow_runs = [record for record in records if record.record_kind == "workflow_run"]
            workflow_children = [record for record in records if record.record_kind == "workflow_child"]
            if len(process_records) != 1:
                summary["issues"].append("process_record_count_mismatch")
            if len(workflow_runs) != 1:
                summary["issues"].append("workflow_run_record_count_mismatch")
            if len(workflow_children) != 1:
                summary["issues"].append("workflow_child_record_count_mismatch")

            if process_records and workflow_runs and workflow_children:
                process_record = process_records[0]
                workflow_run_record = workflow_runs[0]
                workflow_child_record = workflow_children[0]
                record_ids = {
                    process_record.execution_id,
                    workflow_run_record.execution_id,
                    workflow_child_record.execution_id,
                }
                if len(record_ids) != 3:
                    summary["issues"].append("execution_id_collision")
                if process_record.status != "succeeded":
                    summary["issues"].append("process_status_projection_mismatch")
                if workflow_run_record.status != "succeeded":
                    summary["issues"].append("workflow_run_status_projection_mismatch")
                if workflow_child_record.status != "succeeded":
                    summary["issues"].append("workflow_child_status_projection_mismatch")
                if not all(record.workspace and Path(record.workspace).is_dir() for record in (process_record, workflow_run_record, workflow_child_record)):
                    summary["issues"].append("workspace_metadata_missing")
                if not all(record.permission_profile for record in (process_record, workflow_run_record, workflow_child_record)):
                    summary["issues"].append("permission_profile_missing")
                if not workflow_run_record.permission_policy_version or not workflow_child_record.permission_policy_version:
                    summary["issues"].append("workflow_permission_policy_missing")
                if "permissionOptions" not in process_record.metadata:
                    summary["issues"].append("process_permission_metadata_missing")

                process_result = control.read_result(process_record.execution_id)
                workflow_run_result = control.read_result(workflow_run_record.execution_id)
                workflow_child_result = control.read_result(workflow_child_record.execution_id)
                if not process_result.final_text_ref or not process_result.transcript_ref:
                    summary["issues"].append("process_result_references_missing")
                if not workflow_child_result.final_text_ref or not workflow_child_result.transcript_ref:
                    summary["issues"].append("workflow_child_result_references_missing")
                if not workflow_run_result.final_text_ref:
                    summary["issues"].append("workflow_run_result_reference_missing")

                process_actions = set(process_record.capabilities.actions)
                required_process_actions = {
                    "read",
                    "events",
                    "result",
                    "artifacts",
                    "interrupt",
                    "close",
                    "message",
                    "followup",
                    "resume",
                    "attach",
                    "detach",
                }
                if not required_process_actions.issubset(process_actions):
                    summary["issues"].append("process_capability_boundary_mismatch")
                workflow_child_actions = set(workflow_child_record.capabilities.actions)
                if {"mailbox", "resume", "message", "followup", "cancel", "close"} & workflow_child_actions:
                    summary["issues"].append("workflow_child_capability_boundary_mismatch")

                event_batch = control.events_since()
                expected_cursor = make_workflow_source_cursor(loaded_run.run_id)
                if "process" not in event_batch.next_cursors or expected_cursor not in event_batch.next_cursors:
                    summary["issues"].append("source_cursor_key_missing")
                event_ids = [event.event_id for event in event_batch.events if event.event_id]
                if len(event_ids) != len(set(event_ids)):
                    summary["issues"].append("event_id_collision")

                summary.update(
                    {
                        "records": [_record_shape(record) for record in records],
                        "eventCount": len(event_batch.events),
                        "cursorKeys": sorted(event_batch.next_cursors),
                        "processCapabilityCount": len(process_actions),
                        "workflowChildCapabilities": sorted(workflow_child_actions),
                        "artifactCounts": {
                            "process": len(process_record.artifact_refs),
                            "workflowRun": len(workflow_run_record.artifact_refs),
                            "workflowChild": len(workflow_child_record.artifact_refs),
                        },
                    }
                )
            summary["passed"] = not summary["issues"]
        finally:
            # Cleanup is intentionally deferred to the outer finally, after the process
            # manager has closed and waited for the child file handles.
            pass
    except Exception as exc:
        summary["passed"] = False
        summary["issues"].append("exception")
        summary["error"] = _safe_error(exc, roots)
    finally:
        if manager is not None and process_task_name:
            try:
                manager.close_agent(
                    process_task_name,
                    reason="p2_1_e2e_cleanup",
                    grace_s=2.0,
                )
            except Exception:
                pass
            with contextlib.suppress(Exception):
                if process_pid and manager.process_exists(process_pid):
                    manager.terminate_process(process_pid)
            _wait_for(
                lambda: not process_pid or not manager.process_exists(process_pid),
                timeout_s=10.0,
                interval_s=0.2,
            )
        if temp_dir is not None:
            with contextlib.suppress(Exception):
                temp_dir.cleanup()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


@unittest.skipUnless(OPT_IN, "set GA_RUN_REAL_P2_1_E2E=1 to run the real cross-engine E2E")
class RealP21AgentControlE2ETest(unittest.TestCase):
    def test_real_luna_cross_engine_control_plane(self):
        self.assertEqual(0, main())


if __name__ == "__main__":
    raise SystemExit(main())
