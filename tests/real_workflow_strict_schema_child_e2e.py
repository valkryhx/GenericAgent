"""Real-LLM E2E for the strict-schema child contract.

Opt in with ``GA_RUN_REAL_WORKFLOW_STRICT_SCHEMA_E2E=1``. Defaults to the local
``llm.yaml`` ``profiles.default`` and refuses to run when the resolved model does
not match ``GA_REAL_API_EXPECTED_MODEL``.

Regression target (2026-10-09): a research workflow whose plan declared
``SOURCE_SCHEMA`` (required: sources/claims/risks) failed the whole run because
the child was never told its answer is machine-validated JSON. It answered in
prose, and the display compactor then shrank its fenced JSON block to
``... (N lines)`` before validation. Both paths are exercised here with a real
child: a strict schema job must end ``succeeded`` with a JSON payload.

Only sanitized statuses, artifact names and tool names are printed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sensitive_redaction import sanitize  # noqa: E402
from workflow_llm import binding_from_profile  # noqa: E402
from workflow_models import WorkflowRun  # noqa: E402
from workflow_child_agent import NativeGPTChildAgentRunner  # noqa: E402
from workflow_runtime import WorkflowRuntime  # noqa: E402
from workflow_scheduler import SchedulerConfig  # noqa: E402
from workflow_store import WorkflowStore
from workflow_workspace import workspace_writes_with_writer  # noqa: E402

PROFILE = os.environ.get("GA_WORKFLOW_LLM_PROFILE") or "default"
OPT_IN = os.environ.get("GA_RUN_REAL_WORKFLOW_STRICT_SCHEMA_E2E") == "1"


def check_profile(summary: dict[str, Any]) -> bool:
    binding = binding_from_profile(PROFILE)
    expected = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "")
    summary["profile"] = {
        "profileName": binding.profile_name,
        "model": binding.model_id,
        "expectedModel": expected or None,
    }
    summary["profileOk"] = bool(binding.model_id) and (not expected or binding.model_id == expected)
    return summary["profileOk"]


STRICT_SCHEMA_SCRIPT = r"""
phase('Source Discovery')
const result = await agent(`This is a research assignment. Do not call tools; answer from general knowledge only.
Task: list three fictional-but-well-formed bibliographic sources, two claims, and one risk.
Return sources, claims and risks as structured data.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not write files.`, {
  label: 'source-discovery',
  schema: {
    type: 'object',
    required: ['sources', 'claims', 'risks'],
    properties: {
      sources: { type: 'array', items: { type: 'object' } },
      claims: { type: 'array', items: { type: 'object' } },
      risks: { type: 'array' }
    }
  }
})
return {
  sourceCount: Array.isArray(result.sources) ? result.sources.length : -1,
  claimCount: Array.isArray(result.claims) ? result.claims.length : -1,
  riskCount: Array.isArray(result.risks) ? result.risks.length : -1
}
"""

ARTIFACT_HANDOFF_SCRIPT = r"""
phase('Source Discovery')
const research = await agent(`Write a short workspace-relative research note using file_write.
Path: research_notes.md
Content: "# Research notes\n- point one\n- point two\n"
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials.`, {
  label: 'source-discovery',
  deliverables: ['research_notes.md']
})
phase('Synthesis')
const report = await agent(`Write a short workspace-relative report using code_run (not file_write).
Run a small Python snippet that writes the file 'synthesis_report.md' with exactly the content:
"# Synthesis report\nDELIVERED\n"
Use pathlib.Path('synthesis_report.md').write_text(..., encoding='utf-8') so the host observes a
filesystem change rather than a file_write tool call.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials.`, {
  label: 'synthesis',
  dependsOn: ['source-discovery'],
  deliverables: ['synthesis_report.md']
})
return { notesRef: research.resultRef, reportRef: report.resultRef }
"""


def run_artifact_handoff_case(store: WorkflowStore, workspace: Path) -> dict[str, Any]:
    """A child-written file must appear as a resolvable workspace artifactRef."""
    run = store.create_run(
        WorkflowRun(
            run_id="wf_artifact_handoff_e2e",
            session_id="workflow_artifact_handoff_e2e",
            script=ARTIFACT_HANDOFF_SCRIPT,
            status="running",
        )
    )
    runner = NativeGPTChildAgentRunner(profile_name=PROFILE, max_tokens=1500, max_turns=6)
    try:
        WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
            timeout_seconds=420.0,
        ).run(run, args={"workspacePath": str(workspace)})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    loaded = store.load_run("wf_artifact_handoff_e2e")
    handoff_refs: list[str] = []
    owners_by_path: dict[str, list[str]] = {}
    tool_names_by_label: dict[str, list[str]] = {}
    for job in loaded.jobs:
        observed = workspace_writes_with_writer((job.metadata or {}).get("observedArtifacts"))
        for entry in observed:
            handoff_refs.append(entry["path"])
            if entry["writer"]:
                owners_by_path.setdefault(entry["path"], [])
                if entry["writer"] not in owners_by_path[entry["path"]]:
                    owners_by_path[entry["path"]].append(entry["writer"])
        events = store.read_agent_transcript_events(loaded, (job.metadata or {}).get("transcriptRef"))
        tool_names_by_label[str((job.metadata or {}).get("label") or job.job_id)] = sorted(
            {
                str(event.get("toolName") or event.get("tool_name") or "")
                for event in events
                if event.get("type") == "tool_call"
            }
            - {""}
        )
    # The handoff is what a downstream agent actually reads, so ownership has to
    # be visible there and not only on the job metadata.
    # In this E2E every stage runs in a child process, so the persisted run-level
    # ``artifactOwnership`` index is the layer that must carry the mapping (a
    # child keeps no in-process handoff dict). Per-job metadata is asserted
    # separately above.
    # The persisted progress record is the run-level ownership index; it must
    # exist even for stages that ran in a child process and kept no handoff dict.
    progress_owners: dict[str, list[str]] = {}
    progress_path = store._run_dir(loaded) / "workflow-progress.json"
    if progress_path.is_file():
        progress_document = json.loads(progress_path.read_text(encoding="utf-8"))
        recorded = progress_document.get("artifactOwnership")
        if isinstance(recorded, dict):
            progress_owners = {
                str(ref): [str(writer) for writer in writers]
                for ref, writers in recorded.items()
                if isinstance(writers, (list, tuple))
            }
    # The bridge handoff is what the GA agent actually consumes, so ownership
    # must be visible there too.
    handoff_document = {}
    try:
        from frontends.ink_bridge import GenericAgentBridge

        # ``GenericAgentBridge`` builds its agent eagerly, so hand it an inert
        # stub: this check only needs the pure payload builder.
        class _InertAgent:
            inc_out = False
            verbose = False
            session_id = "workflow_artifact_handoff_e2e"

        bridge = GenericAgentBridge(
            agent_factory=lambda: _InertAgent(),
            emit=lambda event: None,
            workspace_root=str(workspace),
        )
        payload = bridge._workflow_handoff_payload(store.load_run("wf_artifact_handoff_e2e"))
        handoff_document = json.loads(
            payload.split("<workflow_handoff>", 1)[1].split("</workflow_handoff>", 1)[0]
        )
    except Exception as exc:  # pragma: no cover - diagnostic path
        handoff_document = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {
        "runStatus": loaded.status,
        "observedArtifacts": sanitize(handoff_refs),
        "handoffOwnership": sanitize(handoff_document.get("artifactOwnership") or {}),
        "handoffIntermediateOwners": sanitize(
            (handoff_document.get("intermediateResults") or [{}])[0].get("artifactOwners") or {}
            if handoff_document.get("intermediateResults") else {}
        ),

        "observedArtifactOwners": sanitize(owners_by_path),
        "persistedArtifactOwnership": sanitize(progress_owners),
        "toolNamesByJob": sanitize(tool_names_by_label),
        "filesOnDisk": sorted(
            name for name in ("research_notes.md", "synthesis_report.md") if (workspace / name).is_file()
        ),
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_workflow_strict_schema_e2e_"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "profileRequested": PROFILE,
        "root": str(root),
        "workspace": str(workspace),
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_WORKFLOW_STRICT_SCHEMA_E2E=1 to run"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 1
        store = WorkflowStore(root=str(root / "runs"))
        run = store.create_run(
            WorkflowRun(
                run_id="wf_strict_schema_e2e",
                session_id="workflow_strict_schema_e2e",
                script=STRICT_SCHEMA_SCRIPT,
                status="running",
            )
        )
        runner = NativeGPTChildAgentRunner(profile_name=PROFILE, max_tokens=1500, max_turns=3)
        started = time.time()
        try:
            outcome = WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=420.0,
            ).run(run, args={"workspacePath": str(workspace)})
            summary["runtimeOutcome"] = sanitize(
                getattr(outcome, "status", str(outcome)) if outcome is not None else None
            )
        except Exception as exc:
            summary["issues"].append(f"runtime_error:{type(exc).__name__}: {str(exc)[:400]}")
        summary["elapsedSeconds"] = round(time.time() - started, 1)

        loaded = store.load_run("wf_strict_schema_e2e")
        summary["runStatus"] = loaded.status
        jobs = [
            {
                "jobId": job.job_id,
                "label": job.metadata.get("label"),
                "status": job.status,
                "error": sanitize(job.error)[:300] if job.error else None,
                "schemaValidation": sanitize(
                    (job.metadata.get("schemaValidation") or {}).get("ok")
                ),
            }
            for job in loaded.jobs
        ]
        summary["jobs"] = jobs
        summary["workflowIssues"] = sanitize((loaded.metadata or {}).get("workflowIssues") or [])[:5]
        summary["passed"] = (
            loaded.status == "succeeded"
            and all(job["status"] == "succeeded" for job in jobs)
            and all(job["schemaValidation"] is True for job in jobs)
            and bool(jobs)
        )
        if os.environ.get("GA_RUN_REAL_WORKFLOW_ARTIFACT_HANDOFF_E2E") == "1":
            summary["artifactHandoff"] = run_artifact_handoff_case(store, workspace)
            handoff = summary["artifactHandoff"]
            summary["passed"] = bool(
                summary["passed"]
                and handoff.get("runStatus") == "succeeded"
                and "synthesis_report.md" in (handoff.get("filesOnDisk") or [])
                and "synthesis_report.md" in (handoff.get("observedArtifacts") or [])
            )
            # The whole point of the filesystem diff: the synthesis child must be
            # observed even though it wrote through code_run, not file_write.
            synthesis_tools = (handoff.get("toolNamesByJob") or {}).get("synthesis") or []
            summary["synthesisUsedFileWrite"] = "file_write" in synthesis_tools
            summary["synthesisObservedWithoutFileWrite"] = bool(
                "synthesis_report.md" in (handoff.get("observedArtifacts") or [])
                and "file_write" not in synthesis_tools
            )
            # Ownership must come from the diff at write time, so each artifact
            # names the job that produced it.
            owners = handoff.get("observedArtifactOwners") or {}
            summary["observedArtifactOwners"] = owners
            summary["ownershipRecorded"] = bool(
                owners.get("research_notes.md") == ["source-discovery"]
                and owners.get("synthesis_report.md") == ["synthesis"]
            )
            summary["passed"] = bool(summary["passed"] and summary["ownershipRecorded"])
            # Ownership must be readable run-wide by any consumer, including
            # stages that ran in a child process and kept no local handoff.
            persisted = handoff.get("persistedArtifactOwnership") or {}
            summary["persistedOwnershipRecorded"] = bool(
                persisted.get("synthesis_report.md") == ["synthesis"]
                and persisted.get("research_notes.md") == ["source-discovery"]
            )
            # The handoff must expose the same index at the top level, so a
            # downstream agent reads ownership from one place.
            summary["handoffExposesOwnership"] = bool(
                (handoff.get("handoffOwnership") or {}).get("synthesis_report.md") == ["synthesis"]
            )
            summary["passed"] = bool(summary["passed"] and summary["handoffExposesOwnership"])
            summary["passed"] = bool(summary["passed"] and summary["persistedOwnershipRecorded"])
    except Exception as exc:  # pragma: no cover - diagnostic path
        summary["issues"].append(f"{type(exc).__name__}: {str(exc)[:400]}")
    print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
