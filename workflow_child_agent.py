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


def _tool_name(tool: dict) -> str | None:
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) and name else None


def _merge_tools_by_name(base: list[dict], additions: list[dict]) -> list[dict]:
    merged = copy.deepcopy(base or [])
    seen = {_tool_name(tool) for tool in merged}
    for tool in additions or []:
        name = _tool_name(tool)
        if name and name not in seen:
            merged.append(copy.deepcopy(tool))
            seen.add(name)
    return merged


def _merge_required_capability_tools(candidate: list[dict], baseline: list[dict]) -> list[dict]:
    """Preserve worker capabilities when legacy factories return a minimal schema.

    Workflow child agents are the actors that perform the work.  A legacy
    zero-argument tools_schema_factory may still add test-specific tools, but it
    must not silently remove skill loading or discovered MCP tools from real
    workflow agents.
    """
    required_names: set[str] = set()
    for tool in baseline or []:
        name = _tool_name(tool)
        if name in {"file_read", "load_skill"} or (name and name.startswith("mcp__")):
            required_names.add(name)
    selected = [tool for tool in baseline or [] if _tool_name(tool) in required_names]
    return _merge_tools_by_name(candidate or [], selected)


def _build_capability_snapshot(tools: list[dict], mcp_discovery: dict) -> dict:
    tool_names = sorted(name for name in (_tool_name(tool) for tool in tools or []) if name)
    mcp_tool_names = [name for name in tool_names if name.startswith("mcp__")]
    return {
        "toolSchemaCount": len(tool_names),
        "toolNames": tool_names,
        "loadSkillAvailable": "load_skill" in tool_names,
        "fileReadAvailable": "file_read" in tool_names,
        "mcpToolNames": mcp_tool_names,
        "mcpDiscovery": copy.deepcopy(mcp_discovery),
    }


class NativeGPTChildAgentRunner:
    """Real workflow child agent.

    LLM resolution priority for each job start:
      1. client_factory / session_factory (tests)
      2. binding_provider() — typically main-session /model snapshot
      3. profile_name — fixed llm.yaml profile
      4. workflow_llm.binding_from_env() — GA_WORKFLOW_LLM_PROFILE or active_profile

    Does **not** default to mykey resolve_client("native_oai_config").
    """

    def __init__(
        self,
        *,
        config_name: str | None = None,
        profile_name: str | None = None,
        binding_provider=None,
        session_factory=None,
        client_factory=None,
        tools_schema_factory=None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        enable_tools: bool = True,
        max_turns: int = 40,
    ):
        # config_name kept for factory call signature / legacy tests; not used for mykey resolve by default.
        self.config_name = config_name if config_name is not None else (profile_name or "")
        self.profile_name = profile_name
        self.binding_provider = binding_provider
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.tools_schema_factory = tools_schema_factory
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.enable_tools = bool(enable_tools)
        self.max_turns = int(max_turns)
        self.last_capability_snapshot: dict = {}
        self.last_llm_binding: dict = {}
        self._states: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, job) -> None:
        executable, is_tool_client, llm_meta = self._new_executable()
        target = getattr(executable, "backend", executable)
        if self.max_tokens is not None and hasattr(target, "max_tokens"):
            target.max_tokens = self.max_tokens
        if self.system_prompt is not None and not is_tool_client and hasattr(target, "system"):
            target.system = self.system_prompt
        state = {
            "executable": executable,
            "session": target,
            "is_tool_client": is_tool_client,
            "llm_meta": dict(llm_meta or {}),
            "handler": None,
            "cancelled": False,
            "result": None,
            "done": False,
        }
        with self._lock:
            self._states[job.job_id] = state
            self.last_llm_binding = dict(llm_meta or {})
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
            if state:
                state["cancelled"] = True
        if not state:
            return
        for target in (state.get("executable"), state.get("session")):
            if hasattr(target, "cancel_current_request"):
                target.cancel_current_request()
        handler = state.get("handler")
        if handler is not None:
            handler.cancel()

    def _new_executable(self):
        """Return (executable, is_tool_client, llm_meta_dict)."""
        factory_key = self.config_name or self.profile_name or "workflow"
        if self.client_factory is not None:
            try:
                return self.client_factory(factory_key), True, {"llmProfile": str(factory_key), "llmModel": "", "llmSource": "client_factory"}
            except TypeError:
                return self.client_factory(), True, {"llmProfile": str(factory_key), "llmModel": "", "llmSource": "client_factory"}
        if self.session_factory is not None:
            try:
                return self.session_factory(factory_key), False, {"llmProfile": str(factory_key), "llmModel": "", "llmSource": "session_factory"}
            except TypeError:
                return self.session_factory(), False, {"llmProfile": str(factory_key), "llmModel": "", "llmSource": "session_factory"}

        from workflow_llm import (
            binding_from_env,
            binding_from_profile,
            make_session,
            make_tool_client,
        )

        if self.binding_provider is not None:
            binding = self.binding_provider()
        elif self.profile_name:
            binding = binding_from_profile(self.profile_name)
        else:
            binding = binding_from_env()
        meta = binding.as_metadata()
        # Keep config_name in sync for any code reading it (progress / debug).
        self.config_name = binding.profile_name
        if self.enable_tools:
            return make_tool_client(binding), True, meta
        return make_session(binding), False, meta

    def _run_job(self, job, state: dict) -> None:
        executable = state["executable"]
        session = state.get("session") or executable
        transcript_events: list[dict] = []
        prompt = self._build_prompt(job)
        transcript_ref = f"agents/{job.job_id}/transcript.jsonl"
        started_at = time.time()
        profile = self._permission_profile(job)
        version = self._permission_policy_version(job)
        llm_meta = dict(state.get("llm_meta") or {})
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
                "configName": llm_meta.get("llmProfile") or self.config_name,
                "llmProfile": llm_meta.get("llmProfile") or self.config_name or "",
                "llmModel": llm_meta.get("llmModel") or "",
                "llmSource": llm_meta.get("llmSource") or "",
                "startedAt": started_at,
            }
        )
        message = {"role": "user", "content": [{"type": "text", "text": prompt}]}
        transcript_events.append({"type": "request", "messages": [copy.deepcopy(message)]})
        try:
            if state.get("is_tool_client") and self.enable_tools:
                answer, usage, tool_summary = self._run_tool_job(job, state, executable, prompt, transcript_events, profile, version)
            else:
                answer = "".join(str(chunk) for chunk in session.ask(message))
                usage = copy.deepcopy(getattr(session, "last_usage_tokens", None) or {})
                tool_summary = {}
            answer = redact_sensitive_text(answer)
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
                transcript_events=sanitize(transcript_events),
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
            if state.get("cancelled"):
                result = AgentResult(
                    job_id=job.job_id,
                    status="cancelled",
                    payload={"cancelled": True},
                    transcript_ref=transcript_ref,
                    token_usage=copy.deepcopy(result.token_usage),
                    tool_summary=copy.deepcopy(result.tool_summary),
                    transcript_events=copy.deepcopy(result.transcript_events),
                )
            state["result"] = result
            state["done"] = True

    def _run_tool_job(self, job, state: dict, client, prompt: str, transcript_events: list[dict], profile: str, version: str):
        from agent_loop import agent_runner_loop
        from mcp_runtime import mcp_cancellation_scope
        handler = self._build_handler(job, transcript_events, profile, version)
        with self._lock:
            state["handler"] = handler
            cancelled = bool(state.get("cancelled"))
        if cancelled:
            handler.cancel()
        with mcp_cancellation_scope(handler.code_stop_signal):
            tools_schema = self._load_tools_schema()
        transcript_events.append({
            "type": "capability_snapshot",
            "runId": job.metadata.get("runId"),
            "jobId": job.job_id,
            "capabilities": copy.deepcopy(self.last_capability_snapshot),
        })
        try:
            chunks = []
            for chunk in agent_runner_loop(
                client,
                self._build_system_prompt(),
                prompt,
                handler,
                tools_schema,
                max_turns=self.max_turns,
                verbose=False,
                initial_user_content=[{"type": "text", "text": prompt}],
            ):
                chunks.append(str(chunk))
                with self._lock:
                    if state.get("cancelled"):
                        break
            output = "".join(chunks)
        finally:
            with self._lock:
                if state.get("handler") is handler:
                    state["handler"] = None
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
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "tools_schema.json"), "r", encoding="utf-8") as f:
            tools = json.load(f)
        if os.name != "nt":
            tools = json.loads(json.dumps(tools, ensure_ascii=False).replace("powershell", "bash"))
        mcp_discovery = {"status": "ok", "injectedToolCount": 0}
        try:
            from mcp_runtime import discover_mcp_tools_cached
            discovered = discover_mcp_tools_cached()
            before = {_tool_name(tool) for tool in tools}
            tools = _merge_tools_by_name(tools, discovered)
            after = {_tool_name(tool) for tool in tools}
            mcp_discovery = {"status": "ok", "injectedToolCount": len([name for name in after - before if name and name.startswith("mcp__")])}
        except Exception as exc:
            mcp_discovery = {
                "status": "error",
                "errorType": type(exc).__name__,
                "error": redact_sensitive_text(str(exc))[:300],
                "injectedToolCount": 0,
            }
        baseline = copy.deepcopy(tools)
        if self.tools_schema_factory is not None:
            try:
                transformed = self.tools_schema_factory(copy.deepcopy(tools))
            except TypeError:
                transformed = self.tools_schema_factory()
                transformed = _merge_required_capability_tools(transformed, baseline)
            tools = copy.deepcopy(transformed or [])
        self.last_capability_snapshot = _build_capability_snapshot(tools, mcp_discovery)
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
        options = sanitize(copy.deepcopy(job.metadata.get("options") or {}))
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
