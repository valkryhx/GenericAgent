from __future__ import annotations

import copy
import json
import os
import threading
import time
from types import SimpleNamespace
from typing import Protocol

from sensitive_redaction import sanitize, redact_sensitive_text
from workflow_models import AgentResult, DEFAULT_PERMISSION_PROFILE, DEFAULT_PERMISSION_POLICY_VERSION


class ChildAgentRunner(Protocol):
    def start(self, job) -> None: ...
    def poll(self, job) -> AgentResult | None: ...
    def cancel(self, job) -> None: ...


class FakeChildAgentRunner:
    def __init__(
        self,
        *,
        delay_ticks: int = 0,
        results: dict[str, dict] | None = None,
        fail_job_ids: set[str] | None = None,
        cancellable: bool = True,
    ):
        self.delay_ticks = max(0, int(delay_ticks))
        self.results = copy.deepcopy(results or {})
        self.fail_job_ids = set(fail_job_ids or set())
        self.cancellable = bool(cancellable)
        self._remaining: dict[str, int] = {}
        self.cancelled_job_ids: set[str] = set()

    def start(self, job) -> None:
        self._remaining[job.job_id] = self.delay_ticks

    def cancel(self, job) -> None:
        if self.cancellable:
            self.cancelled_job_ids.add(job.job_id)
            self._remaining[job.job_id] = 0

    def poll(self, job) -> AgentResult | None:
        if job.job_id in self.cancelled_job_ids:
            return AgentResult(job_id=job.job_id, status="cancelled", payload={"cancelled": True})
        remaining = self._remaining.get(job.job_id, 0)
        if remaining > 0:
            self._remaining[job.job_id] = remaining - 1
            return None
        if job.job_id in self.fail_job_ids:
            raise RuntimeError(f"fake child agent failed: {job.job_id}")
        payload = copy.deepcopy(self.results.get(job.job_id, {"summary": f"completed {job.job_id}"}))
        return AgentResult(job_id=job.job_id, payload=payload)


class NativeGPTChildAgentRunner:
    def __init__(
        self,
        *,
        config_name: str = "native_oai_config",
        session_factory=None,
        client_factory=None,
        tools_schema_factory=None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        enable_tools: bool = True,
        max_turns: int = 40,
    ):
        self.config_name = config_name
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.tools_schema_factory = tools_schema_factory
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.enable_tools = bool(enable_tools)
        self.max_turns = int(max_turns)
        self._states: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, job) -> None:
        executable, is_tool_client = self._new_executable()
        target = getattr(executable, "backend", executable)
        if self.max_tokens is not None and hasattr(target, "max_tokens"):
            target.max_tokens = self.max_tokens
        if self.system_prompt is not None and not is_tool_client and hasattr(target, "system"):
            target.system = self.system_prompt
        state = {"executable": executable, "session": target, "is_tool_client": is_tool_client, "result": None, "done": False}
        with self._lock:
            self._states[job.job_id] = state
        thread = threading.Thread(target=self._run_job, args=(job, state), daemon=True)
        state["thread"] = thread
        thread.start()

    def poll(self, job) -> AgentResult | None:
        with self._lock:
            state = self._states.get(job.job_id)
            if not state or not state.get("done"):
                return None
            return state.get("result")

    def cancel(self, job) -> None:
        with self._lock:
            state = self._states.get(job.job_id)
        if not state:
            return
        for target in (state.get("executable"), state.get("session")):
            if hasattr(target, "cancel_current_request"):
                target.cancel_current_request()

    def _new_executable(self):
        if self.client_factory is not None:
            try:
                return self.client_factory(self.config_name), True
            except TypeError:
                return self.client_factory(), True
        if self.session_factory is not None:
            try:
                return self.session_factory(self.config_name), False
            except TypeError:
                return self.session_factory(), False
        if self.enable_tools:
            from llmcore import resolve_client
            return resolve_client(self.config_name), True
        from llmcore import resolve_session
        return resolve_session(self.config_name), False

    def _run_job(self, job, state: dict) -> None:
        executable = state["executable"]
        session = state.get("session") or executable
        transcript_events: list[dict] = []
        prompt = self._build_prompt(job)
        transcript_ref = f"agents/{job.job_id}/transcript.jsonl"
        started_at = time.time()
        profile = self._permission_profile(job)
        version = self._permission_policy_version(job)
        transcript_events.append(
            {
                "type": "metadata",
                "runId": job.metadata.get("runId"),
                "jobId": job.job_id,
                "phase": job.phase,
                "label": job.metadata.get("label"),
                "options": copy.deepcopy(job.metadata.get("options") or {}),
                "permissionProfile": profile,
                "permissionPolicyVersion": version,
                "configName": self.config_name,
                "startedAt": started_at,
            }
        )
        message = {"role": "user", "content": [{"type": "text", "text": prompt}]}
        transcript_events.append({"type": "request", "messages": [copy.deepcopy(message)]})
        try:
            if state.get("is_tool_client") and self.enable_tools:
                answer, usage, tool_summary = self._run_tool_job(job, executable, prompt, transcript_events, profile, version)
            else:
                answer = "".join(str(chunk) for chunk in session.ask(message))
                usage = copy.deepcopy(getattr(session, "last_usage_tokens", None) or {})
                tool_summary = {}
            transcript_events.append({"type": "assistant", "text": answer})
            if usage:
                transcript_events.append({"type": "token_usage", "tokenUsage": usage})
            payload = {"summary": answer.strip(), "text": answer}
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload=payload,
                transcript_ref=transcript_ref,
                token_usage=usage,
                tool_summary=tool_summary,
                transcript_events=transcript_events,
            )
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            transcript_events.append({"type": "error", "error": error})
            result = AgentResult(
                job_id=job.job_id,
                status="failed",
                payload={"error": error},
                transcript_ref=transcript_ref,
                token_usage=copy.deepcopy(getattr(executable, "last_usage_tokens", None) or getattr(session, "last_usage_tokens", None) or {}),
                tool_summary=self._build_tool_summary(transcript_events),
                transcript_events=sanitize(transcript_events),
            )
        with self._lock:
            state["result"] = result
            state["done"] = True

    def _run_tool_job(self, job, client, prompt: str, transcript_events: list[dict], profile: str, version: str):
        from agent_loop import agent_runner_loop
        handler = self._build_handler(job, transcript_events, profile, version)
        tools_schema = self._load_tools_schema()
        output = "".join(
            str(chunk)
            for chunk in agent_runner_loop(
                client,
                self._build_system_prompt(),
                prompt,
                handler,
                tools_schema,
                max_turns=self.max_turns,
                verbose=False,
                initial_user_content=[{"type": "text", "text": prompt}],
            )
        )
        usage = copy.deepcopy(getattr(client, "last_usage_tokens", None) or getattr(getattr(client, "backend", None), "last_usage_tokens", None) or {})
        return output, usage, self._build_tool_summary(transcript_events)

    def _build_handler(self, job, transcript_events: list[dict], profile: str, version: str):
        from ga import GenericAgentHandler
        from workflow_permissions import ToolPermissionPolicy
        parent = SimpleNamespace(
            task_dir=self._child_cwd(job),
            verbose=False,
            llmclient=SimpleNamespace(backend=SimpleNamespace(history=[])),
            _turn_end_hooks={},
        )
        handler = GenericAgentHandler(parent, cwd=parent.task_dir)
        handler.workflow_permission_policy = ToolPermissionPolicy(profile=profile, options=copy.deepcopy(job.metadata.get("options") or {}))
        handler.workflow_permission_context = {
            "runId": job.metadata.get("runId"),
            "jobId": job.job_id,
            "permissionProfile": profile,
            "permissionPolicyVersion": version,
        }
        handler.workflow_permission_event_callback = lambda event: transcript_events.append(copy.deepcopy(event))

        def before(tool_name, args, response):
            transcript_events.append({
                "type": "tool_call",
                "toolName": tool_name,
                "args": copy.deepcopy({k: v for k, v in (args or {}).items() if not str(k).startswith("_")}),
            })

        def after(tool_name, args, response, ret):
            data = getattr(ret, "data", ret)
            if not isinstance(data, (dict, list, str, int, float, bool, type(None))):
                data = {"content": getattr(data, "content", str(data))}
            transcript_events.append({"type": "tool_result", "toolName": tool_name, "data": copy.deepcopy(data)})

        handler.tool_before_callback = before
        handler.tool_after_callback = after
        return handler

    def _load_tools_schema(self):
        if self.tools_schema_factory is not None:
            return copy.deepcopy(self.tools_schema_factory())
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "tools_schema.json"), "r", encoding="utf-8") as f:
            tools = json.load(f)
        if os.name != "nt":
            tools = json.loads(json.dumps(tools, ensure_ascii=False).replace("powershell", "bash"))
        try:
            from mcp_runtime import discover_mcp_tools_cached
            existing = {tool.get("function", {}).get("name") for tool in tools}
            for tool in discover_mcp_tools_cached():
                name = tool.get("function", {}).get("name")
                if name and name not in existing:
                    tools.append(copy.deepcopy(tool))
                    existing.add(name)
        except Exception:
            pass
        return copy.deepcopy(tools)

    def _build_system_prompt(self) -> str:
        base = self.system_prompt or "You are a workflow child agent. Complete only this assigned job and return a concise result."
        try:
            from skills_runtime import build_skill_prompt
            skill_prompt = build_skill_prompt()
        except Exception:
            skill_prompt = ""
        return base + ("\n" + skill_prompt if skill_prompt else "")

    def _build_tool_summary(self, transcript_events: list[dict]) -> dict:
        allowed = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_allowed" and event.get("toolName")]
        denied = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_denied" and event.get("toolName")]
        if not allowed and not denied:
            return {}
        return {
            "allowed": len(allowed),
            "denied": len(denied),
            "allowedTools": allowed,
            "deniedTools": denied,
        }

    def _child_cwd(self, job) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp", "workflow_child_agents", str(job.job_id))

    def _permission_profile(self, job) -> str:
        return job.metadata.get("permissionProfile") or DEFAULT_PERMISSION_PROFILE

    def _permission_policy_version(self, job) -> str:
        return job.metadata.get("permissionPolicyVersion") or DEFAULT_PERMISSION_POLICY_VERSION

    def _build_prompt(self, job) -> str:
        run_id = job.metadata.get("runId") or job.metadata.get("run_id") or ""
        label = job.metadata.get("label") or ""
        options = copy.deepcopy(job.metadata.get("options") or {})
        permission_profile = self._permission_profile(job)
        permission_policy_version = self._permission_policy_version(job)
        lines = [
            "You are a workflow child agent. Complete only this assigned job and return a concise result.",
            f"runId: {run_id}",
            f"jobId: {job.job_id}",
            f"phase: {job.phase or ''}",
            f"label: {label}",
            f"options: {options}",
            f"permissionProfile: {permission_profile}",
            f"permissionPolicyVersion: {permission_policy_version}",
            "",
            "Task:",
            job.prompt,
        ]
        return "\n".join(lines)
