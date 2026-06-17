from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from sensitive_redaction import sanitize
from workflow_models import AgentResult, WorkflowEvent, WorkflowJob, WorkflowRun


def copy_tool_summary(value) -> dict:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def copy_token_usage(value) -> dict:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ROOT = PROJECT_ROOT / "temp" / "sessions"


class WorkflowStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT

    def create_run(self, run: WorkflowRun) -> WorkflowRun:
        artifact_dir = self._artifact_dir(run.session_id, run.run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run.artifact_dir = str(artifact_dir)
        self._write_json(artifact_dir / "run.json", sanitize(run.to_dict()))
        self._write_json(artifact_dir / "state.json", sanitize(run.to_dict()))
        (artifact_dir / "script.js").write_text(run.script or "", encoding="utf-8")
        (artifact_dir / "journal.jsonl").touch(exist_ok=True)
        final_result = artifact_dir / "final-result.json"
        if not final_result.exists():
            self._write_json(final_result, {})
        return run

    def save_run(self, run: WorkflowRun) -> WorkflowRun:
        artifact_dir = self._run_dir(run)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run.artifact_dir = str(artifact_dir)
        self._preserve_external_kill(run, artifact_dir)
        self._write_json(artifact_dir / "run.json", sanitize(run.to_dict()))
        self._write_json(artifact_dir / "state.json", sanitize(run.to_dict()))
        (artifact_dir / "script.js").write_text(run.script or "", encoding="utf-8")
        return run

    def load_run(self, run_id: str) -> WorkflowRun:
        artifact_dir = self._find_run_dir(run_id)
        data_path = artifact_dir / "state.json"
        if not data_path.exists():
            data_path = artifact_dir / "run.json"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        run = WorkflowRun.from_dict(data)
        run.artifact_dir = str(artifact_dir)
        script_path = artifact_dir / "script.js"
        if script_path.exists():
            run.script = script_path.read_text(encoding="utf-8")
        return run

    def append_event(self, run: WorkflowRun | str, event: WorkflowEvent) -> WorkflowEvent:
        artifact_dir = self._run_dir(run) if isinstance(run, WorkflowRun) else self._find_run_dir(run)
        with (artifact_dir / "journal.jsonl").open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(json.dumps(sanitize(event.to_dict()), ensure_ascii=False, separators=(",", ":")) + "\n")
        return event

    def append_permission_event(self, run: WorkflowRun, raw_event: dict) -> WorkflowEvent:
        raw_run_id = raw_event.get("runId") or raw_event.get("run_id")
        if raw_run_id and raw_run_id != run.run_id:
            raise ValueError(f"permission event runId mismatch: {raw_run_id} != {run.run_id}")
        event_type = str(raw_event.get("type") or raw_event.get("eventType") or raw_event.get("event_type") or "")
        if event_type not in {"permission_profile_selected", "tool_allowed", "tool_denied"}:
            raise ValueError(f"unsupported permission event type: {event_type}")
        payload = {
            "toolName": raw_event.get("toolName") or raw_event.get("tool_name"),
            "profile": raw_event.get("profile"),
            "decision": raw_event.get("decision"),
            "reason": raw_event.get("reason"),
            "permission": raw_event.get("permission") or {},
        }
        for key, value in raw_event.items():
            if key not in {"type", "eventType", "event_type", "runId", "run_id", "sessionId", "session_id", "jobId", "job_id"} and key not in payload:
                payload[key] = value
        event = WorkflowEvent(
            run_id=run.run_id,
            session_id=run.session_id,
            job_id=raw_event.get("jobId") or raw_event.get("job_id"),
            event_type=event_type,
            sequence=self._next_sequence(run.run_id),
            payload=payload,
        )
        return self.append_event(run, event)

    def write_agent_result(self, run: WorkflowRun, job: WorkflowJob, result: AgentResult) -> str:
        result_ref = f"agents/{job.job_id}/result.json"
        result_path = self._run_dir(run) / result_ref
        result_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(result_path, sanitize(result.to_artifact_dict()))
        job.result_ref = result_ref
        return result_ref

    def read_agent_result(self, run: WorkflowRun | str, job: WorkflowJob | str) -> AgentResult:
        artifact_dir = self._run_dir(run) if isinstance(run, WorkflowRun) else self._find_run_dir(run)
        if isinstance(job, WorkflowJob):
            result_ref = job.result_ref or f"agents/{job.job_id}/result.json"
        else:
            result_ref = f"agents/{job}/result.json"
        result_path = artifact_dir / result_ref
        if not result_path.exists():
            raise FileNotFoundError(str(result_path))
        return AgentResult.from_dict(json.loads(result_path.read_text(encoding="utf-8")))

    def write_agent_transcript(self, run: WorkflowRun, job: WorkflowJob, events_or_messages: list[dict]) -> str:
        transcript_ref = f"agents/{job.job_id}/transcript.jsonl"
        transcript_path = self._run_dir(run) / transcript_ref
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with transcript_path.open("w", encoding="utf-8", errors="replace") as fh:
            for event in events_or_messages:
                fh.write(json.dumps(sanitize(event), ensure_ascii=False, separators=(",", ":")) + "\n")
        return transcript_ref

    def copy_agent_transcript(self, source_run: WorkflowRun | str, source_ref: str, target_run: WorkflowRun, target_job: WorkflowJob) -> str | None:
        if not source_ref:
            return None
        source_path = self._run_dir(source_run) / source_ref if isinstance(source_run, WorkflowRun) else self._find_run_dir(source_run) / source_ref
        if not source_path.exists():
            return None
        target_ref = f"agents/{target_job.job_id}/transcript.jsonl"
        target_path = self._run_dir(target_run) / target_ref
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("r", encoding="utf-8", errors="replace") as source_fh:
            with target_path.open("w", encoding="utf-8", errors="replace") as target_fh:
                for line in source_fh:
                    if not line.strip():
                        continue
                    row = sanitize(json.loads(line))
                    target_fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return target_ref

    def write_final_result(self, run: WorkflowRun, payload: dict) -> str:
        result_ref = "final-result.json"
        self._write_json(self._run_dir(run) / result_ref, sanitize(payload))
        run.result_ref = result_ref
        return result_ref

    def write_workflow_draft(self, run: WorkflowRun, draft) -> str:
        draft_ref = "workflow-draft.json"
        payload = draft.to_dict() if hasattr(draft, "to_dict") else copy.deepcopy(draft)
        self._write_json(self._run_dir(run) / draft_ref, sanitize(payload))
        return draft_ref

    def write_workflow_progress(self, run: WorkflowRun) -> str:
        progress_ref = "workflow-progress.json"
        progress = {
            "runId": run.run_id,
            "sessionId": run.session_id,
            "status": run.status,
            "workflowProgress": [self._build_job_progress(run, job, index) for index, job in enumerate(run.jobs, start=1)],
        }
        self._write_json(self._run_dir(run) / progress_ref, sanitize(progress))
        return progress_ref

    def _build_job_progress(self, run: WorkflowRun, job: WorkflowJob, index: int) -> dict:
        result = None
        if job.result_ref:
            try:
                result = self.read_agent_result(run, job)
            except FileNotFoundError:
                result = None
        transcript_ref = job.metadata.get("transcriptRef") or (result.transcript_ref if result else None)
        transcript_events = self._read_agent_transcript_events(run, transcript_ref) if transcript_ref else []
        tool_calls = self._extract_tool_calls(transcript_events)
        loaded_skills = self._extract_loaded_skills(transcript_events)
        capabilities = self._extract_capability_summary(transcript_events)
        tool_summary = copy_tool_summary(result.tool_summary if result else job.metadata.get("toolSummary") or {})
        allowed_tools = list(tool_summary.get("allowedTools") or self._extract_permission_tools(transcript_events, "tool_allowed"))
        denied_tools = list(tool_summary.get("deniedTools") or self._extract_permission_tools(transcript_events, "tool_denied"))
        skill_load_events = self._extract_skill_load_events(transcript_events)
        payload = result.payload if result else {}
        progress = {
            "type": "workflow_agent",
            "index": index,
            "agentId": job.job_id,
            "jobId": job.job_id,
            "label": job.metadata.get("label"),
            "phase": job.phase,
            "phaseTitle": job.phase,
            "state": job.status,
            "resultRef": job.result_ref,
            "transcriptRef": transcript_ref,
            "lastToolName": tool_calls[-1] if tool_calls else None,
            "lastToolSummary": self._last_tool_summary(transcript_events),
            "toolCalls": tool_calls,
            "skillToolCalls": tool_calls.count("load_skill"),
            "skillLoadEvents": skill_load_events,
            "allowedTools": allowed_tools,
            "deniedTools": denied_tools,
            "loadedSkills": loaded_skills,
            "missingRequiredSkills": [],
            "capability": copy.deepcopy(capabilities),
            "capabilities": capabilities,
            "tokenUsage": copy_token_usage(result.token_usage if result else job.metadata.get("tokenUsage") or {}),
            "promptPreview": self._preview(job.prompt),
            "resultPreview": self._preview(payload.get("summary") if isinstance(payload, dict) and payload.get("summary") is not None else payload),
            "error": job.error,
        }
        return progress

    def _read_agent_transcript_events(self, run: WorkflowRun, transcript_ref: str | None) -> list[dict]:
        if not transcript_ref:
            return []
        transcript_path = self._run_dir(run) / transcript_ref
        if not transcript_path.exists():
            return []
        events: list[dict] = []
        for line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def _extract_tool_calls(events: list[dict]) -> list[str]:
        return [event.get("toolName") for event in events if event.get("type") == "tool_call" and event.get("toolName")]

    @staticmethod
    def _extract_permission_tools(events: list[dict], event_type: str) -> list[str]:
        return [event.get("toolName") for event in events if event.get("type") == event_type and event.get("toolName")]

    @staticmethod
    def _extract_skill_load_events(events: list[dict]) -> list[dict]:
        skill_events: list[dict] = []
        for event in events:
            if event.get("type") != "tool_result" or event.get("toolName") != "load_skill":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            item = {
                "name": data.get("name"),
                "status": data.get("status"),
                "source": data.get("source"),
                "path": data.get("path"),
                "baseDir": data.get("base_dir"),
                "allowedTools": data.get("allowed_tools") or [],
            }
            cleaned = {key: value for key, value in item.items() if value not in (None, "", [])}
            if cleaned:
                skill_events.append(cleaned)
        return skill_events


    @staticmethod
    def _extract_loaded_skills(events: list[dict]) -> list[str]:
        loaded: list[str] = []
        for event in events:
            if event.get("type") != "tool_call" or event.get("toolName") != "load_skill":
                continue
            args = event.get("args") or {}
            skill = args.get("skill") or args.get("name") or args.get("skill_name")
            if isinstance(skill, str) and skill and skill not in loaded:
                loaded.append(skill)
        for event in events:
            if event.get("type") != "tool_result" or event.get("toolName") != "load_skill":
                continue
            data = event.get("data") or {}
            skill = data.get("name") if isinstance(data, dict) else None
            if isinstance(skill, str) and skill and skill not in loaded:
                loaded.append(skill)
        return loaded

    @staticmethod
    def _extract_capability_summary(events: list[dict]) -> dict:
        capabilities = {}
        for event in events:
            if event.get("type") == "capability_snapshot" and isinstance(event.get("capabilities"), dict):
                capabilities = event.get("capabilities") or {}
        mcp_discovery = capabilities.get("mcpDiscovery") if isinstance(capabilities.get("mcpDiscovery"), dict) else {}
        mcp_tool_names = capabilities.get("mcpToolNames") if isinstance(capabilities.get("mcpToolNames"), list) else []
        return {
            "loadSkillAvailable": bool(capabilities.get("loadSkillAvailable")),
            "fileReadAvailable": bool(capabilities.get("fileReadAvailable")),
            "mcpDiscoveryStatus": mcp_discovery.get("status"),
            "mcpDiscoveryInjectedToolCount": mcp_discovery.get("injectedToolCount"),
            "mcpToolCount": len(mcp_tool_names),
        }

    @staticmethod
    def _last_tool_summary(events: list[dict]) -> str | None:
        for event in reversed(events):
            if event.get("type") != "tool_result" or not event.get("toolName"):
                continue
            data = event.get("data")
            if isinstance(data, dict):
                for key in ("path", "file", "name", "status", "error"):
                    if data.get(key):
                        return str(data.get(key))[:160]
        return None

    @staticmethod
    def _preview(value, limit: int = 240) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = " ".join(text.split())
        return text[:limit]

    def replay_events(self, run_id: str) -> list[WorkflowEvent]:
        artifact_dir = self._find_run_dir(run_id)
        journal_path = artifact_dir / "journal.jsonl"
        if not journal_path.exists():
            return []
        events: list[WorkflowEvent] = []
        for line in journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            events.append(WorkflowEvent.from_dict(json.loads(line)))
        return events

    def project_resume_state(self, run_id: str) -> WorkflowRun:
        run = self.load_run(run_id)
        changed = False
        if run.status == "running":
            run.status = "interrupted"
            changed = True
        for job in run.jobs:
            if job.status == "running":
                job.status = "stale"
                changed = True
        running_job_ids = {
            event.job_id
            for event in self.replay_events(run_id)
            if event.event_type in {"job_running", "agent_started"} and event.job_id
        }
        known_job_ids = {job.job_id for job in run.jobs}
        for job_id in sorted(running_job_ids - known_job_ids):
            run.jobs.append(WorkflowJob(job_id=job_id, status="stale"))
            changed = True
        if changed:
            next_sequence = max((event.sequence for event in self.replay_events(run_id)), default=0) + 1
            self.append_event(
                run,
                WorkflowEvent(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    event_type="workflow_interrupted",
                    sequence=next_sequence,
                ),
            )
            self.save_run(run)
            self.write_workflow_progress(run)
        return run

    def _artifact_dir(self, session_id: str, run_id: str) -> Path:
        return self.root / session_id / "workflows" / run_id

    def _run_dir(self, run: WorkflowRun) -> Path:
        if run.artifact_dir:
            return Path(run.artifact_dir)
        return self._artifact_dir(run.session_id, run.run_id)

    def _find_run_dir(self, run_id: str) -> Path:
        matches = list(self.root.glob(f"*/workflows/{run_id}"))
        if not matches:
            raise FileNotFoundError(run_id)
        return matches[0]

    def _next_sequence(self, run_id: str) -> int:
        return max((event.sequence for event in self.replay_events(run_id)), default=0) + 1

    def _preserve_external_kill(self, run: WorkflowRun, artifact_dir: Path) -> None:
        if run.status == "killed":
            return
        state_path = artifact_dir / "state.json"
        if not state_path.exists():
            return
        current = WorkflowRun.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        if current.status != "killed":
            return
        run.status = "killed"
        run.error = current.error

    @staticmethod
    def _write_json(path: Path, data: dict):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
