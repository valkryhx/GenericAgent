"""Isolated B-project/resume/legacy verifier for real_project_scope_e2e."""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "frontends"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from genericagent import paths
from genericagent.core.redaction import sanitize


def _under(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def _set_project_environment(project: Path, data_root: Path) -> None:
    os.environ.update({
        "GA_PROJECT_ROOT": str(project),
        "GA_DATA_DIR": str(data_root),
        "GA_LAUNCH_CWD": str(project),
        "GA_ORIGINAL_CWD": str(project),
        "GA_LLM_CONFIG": str(REPO_ROOT / "llm.yaml"),
    })
    for key in ("GA_PROJECT_KEY", "GA_PROJECT_DATA_DIR", "GA_SESSION_DATA_DIR", "GA_PARENT_SESSION_ID", "GA_TASK_ROOT", "GA_TEAM_KEY"):
        os.environ.pop(key, None)

def _write_legacy_fixture(data_root: Path) -> tuple[Path, Path, bytes]:
    legacy_sessions = data_root / "temp" / "sessions"
    legacy_session = legacy_sessions / "model_responses_legacy_project_scope.txt"
    legacy_session.parent.mkdir(parents=True, exist_ok=True)
    legacy_session.write_text("=== Prompt ===\nLEGACY_READONLY_SESSION\n=== Response ===\nlegacy reply\n", encoding="utf-8", newline="\n")
    legacy_run_dir = legacy_sessions / "legacy_session" / "workflows" / "wf_legacy_scope"
    legacy_run_dir.mkdir(parents=True, exist_ok=True)
    (legacy_run_dir / "script.js").write_text("export const meta = { name: 'legacy', description: 'legacy' }\nreturn { legacy: true }\n", encoding="utf-8", newline="\n")
    state = {
        "version": 1,
        "artifactDir": str(legacy_run_dir),
        "permissionProfile": "inherit-current-permissions",
        "permissionPolicyVersion": "1",
        "error": None,
        "runId": "wf_legacy_scope",
        "sessionId": "legacy_session",
        "status": "succeeded",
        "script": "",
        "jobs": [],
        "metadata": {"workflowName": "legacy"},
        "resultRef": None,
    }
    state_path = legacy_run_dir / "state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return legacy_session, legacy_run_dir, state_path.read_bytes()

def _child_check_scope() -> int:
    payload = json.loads(os.environ["GA_PROJECT_SCOPE_CHILD"])
    project_a = Path(payload["projectA"]).resolve()
    project_b = Path(payload["projectB"]).resolve()
    data_root = Path(payload["dataRoot"]).resolve()
    session_a_path = Path(payload["sessionAPath"]).resolve()
    run_id = str(payload["runId"])
    report: dict[str, Any] = {}
    bridge = None
    try:
        _set_project_environment(project_b, data_root)
        from frontends.ink.bridge import GenericAgentBridge
        bridge = GenericAgentBridge(emit=lambda _event: None)
        agent_b = bridge.agent
        project_a_data = data_root / "projects" / paths.project_key_exact(project_a)
        project_b_data = Path(agent_b.project_data_dir).resolve()
        report["bProjectKey"] = str(agent_b.project_key)
        report["bProjectRoot"] = str(agent_b.project_root)
        report["bDataDir"] = str(project_b_data)
        report["bDiffersFromA"] = project_a_data.resolve() != project_b_data and Path(agent_b.project_root).resolve() == project_b

        from frontends.terminal.continue_cmd import list_sessions
        normal = {Path(row[0]).resolve() for row in list_sessions(**bridge._resume_exclusion_kwargs(include_legacy=False))}
        all_rows = {Path(row[0]).resolve() for row in list_sessions(**bridge._resume_exclusion_kwargs(include_legacy=True))}
        report["bResumeExcluded"] = session_a_path not in normal
        report["bAllResumeExcluded"] = session_a_path not in all_rows
        b_runs = bridge.workflow_store.list_runs(session_id=str(agent_b.session_id or ""))
        report["bWorkflowExcluded"] = all(str(run.run_id) != run_id for run in b_runs)

        bridge.resume_session(str(session_a_path))
        resumed = bridge.agent
        report["resumeRootRebound"] = Path(resumed.project_root or "").resolve() == project_a and Path(bridge.project_dir).resolve() == project_a
        report["resumeTranscriptRebound"] = Path(resumed.session_path or "").resolve() == session_a_path
        report["resumeIdentityRestored"] = str(resumed.project_key) == paths.project_key_exact(project_a) and Path(resumed.project_data_dir).resolve() == project_a_data.resolve()
        report["resumeWorkflowRebound"] = Path(bridge.workflow_store.root).resolve() == project_a_data.resolve()
        report["resumeSubagentRebound"] = _under(bridge.subagent_manager.session_data_dir, project_a_data)
        rebound_runs = bridge.workflow_store.list_runs(session_id=str(resumed.session_id or ""))
        report["resumeSeesWorkflow"] = any(str(run.run_id) == run_id for run in rebound_runs)

        legacy_session, legacy_run_dir, legacy_state_before = _write_legacy_fixture(data_root)
        all_sessions = list_sessions(**bridge._resume_exclusion_kwargs(include_legacy=True))
        report["legacySessionVisibleAll"] = any(Path(row[0]).resolve() == legacy_session.resolve() for row in all_sessions)
        all_runs = bridge._list_legacy_workflow_runs()
        report["legacyWorkflowVisibleAll"] = any(run.run_id == "wf_legacy_scope" for run in all_runs)
        default_runs = bridge.workflow_store.list_runs(session_id=str(resumed.session_id or ""))
        report["legacyExcludedDefault"] = all(run.run_id != "wf_legacy_scope" for run in default_runs)
        legacy_run = next((run for run in all_runs if run.run_id == "wf_legacy_scope"), None)
        report["legacyDetailOpens"] = legacy_run is not None and Path(legacy_run.artifact_dir).resolve() == legacy_run_dir.resolve()
        report["legacyProgressOpens"] = legacy_run is not None and bridge._workflow_progress_from_run(legacy_run).get("runId") == "wf_legacy_scope"
        report["legacyUnchanged"] = (legacy_run_dir / "state.json").read_bytes() == legacy_state_before and not (legacy_run_dir / "workflow-progress.json").exists()
        report["legacySessionExists"] = legacy_session.is_file()
        report["notesUnchanged"] = (project_a / "notes.md").is_file()
        report["error"] = None
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        if bridge is not None:
            try:
                agent = bridge.agent
                agent.abort()
                bridge.stop()
            except Exception:
                pass
    result_path = Path(os.environ.get("GA_PROJECT_SCOPE_RESULT_PATH", "project-scope-child-result.json"))
    result_path.write_text(json.dumps(sanitize(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0 if report.get("error") is None else 1


if __name__ == "__main__":
    raise SystemExit(_child_check_scope())
