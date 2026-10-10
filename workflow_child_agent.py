from __future__ import annotations

import copy
import json
import os
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from typing import Protocol

from sensitive_redaction import sanitize, redact_sensitive_text
from workflow_models import AgentResult, DEFAULT_PERMISSION_PROFILE, DEFAULT_PERMISSION_POLICY_VERSION
from workflow_tool_profiles import (
    capability_coverage,
    effective_tool_profile,
    filter_schema_for_denied,
    filter_schema_for_profile,
    unavailable_capabilities,
)


class ChildAgentRunner(Protocol):
    def start(self, job) -> None: ...
    def poll(self, job) -> AgentResult | None: ...
    def cancel(self, job) -> None: ...


def _live_usage(client) -> dict:
    for candidate in (client, getattr(client, "backend", None)):
        if candidate is None:
            continue
        usage = getattr(candidate, "last_usage_tokens", None)
        if isinstance(usage, dict) and usage:
            return copy.deepcopy(usage)
    return {}


def _summarize_tool_data(data, limit: int = 160) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = json.dumps(data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(data)
    return " ".join(text.split())[:limit]


class LiveJobTelemetry:
    """Throttled, best-effort live counters for one running workflow child.

    The host injects a sink (``NativeGPTChildAgentRunner.set_telemetry_sink``);
    without one every call is a no-op, so unit tests and other callers are
    unaffected. A child only writes its transcript when the job *ends*, so
    without this a multi-minute child is a frozen row in the Ink UI.
    """

    def __init__(
        self,
        sink,
        job_id: str,
        *,
        min_interval: float = 1.0,
        started_at: float | None = None,
        client=None,
    ):
        self._sink = sink
        self._job_id = str(job_id)
        self._min_interval = float(min_interval)
        self._client = client
        self._started_at = time.time() if started_at is None else float(started_at)
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._last_fingerprint: str | None = None
        self._tool_calls = 0
        self._last_tool_name: str | None = None
        self._last_tool_summary: str | None = None

    def note_tool_call(self, tool_name) -> None:
        with self._lock:
            self._tool_calls += 1
            self._last_tool_name = str(tool_name or "")

    def note_tool_result(self, tool_name, data) -> None:
        with self._lock:
            if tool_name:
                self._last_tool_name = str(tool_name)
            self._last_tool_summary = _summarize_tool_data(data)

    def update(self, handler, client=None, *, force: bool = False) -> None:
        if self._sink is None:
            return
        if client is None:
            client = self._client
        now = time.time()
        with self._lock:
            if not force and now - self._last_emit < self._min_interval:
                return
            payload = {
                "jobId": self._job_id,
                "turn": int(getattr(handler, "current_turn", 0) or 0),
                "toolCalls": self._tool_calls,
                "lastToolName": self._last_tool_name,
                "lastToolSummary": self._last_tool_summary,
                "tokenUsage": _live_usage(client),
                "elapsedSeconds": round(now - self._started_at, 1),
                "updatedAt": now,
            }
            try:
                fingerprint = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
            except (TypeError, ValueError):
                fingerprint = None
            if not force and fingerprint is not None and fingerprint == self._last_fingerprint:
                return
            self._last_emit = now
            self._last_fingerprint = fingerprint
        try:
            self._sink(self._job_id, payload)
        except Exception:
            pass


class FakeChildAgentRunner:
    def __init__(
        self,
        *,
        delay_ticks: int = 0,
        results: dict[str, dict] | None = None,
        fail_job_ids: set[str] | None = None,
        cancellable: bool = True,
        tool_calls: dict[str, list[str]] | None = None,
    ):
        self.delay_ticks = max(0, int(delay_ticks))
        self.results = copy.deepcopy(results or {})
        self.fail_job_ids = set(fail_job_ids or set())
        self.cancellable = bool(cancellable)
        # Optional transcript tool calls, so tests can exercise capability
        # evidence without a real tool-using child.
        self.tool_calls = {str(key): [str(name) for name in (value or [])] for key, value in (tool_calls or {}).items()}
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
        events = [
            {"type": "tool_call", "toolName": name, "args": {}}
            for name in self.tool_calls.get(job.job_id, [])
        ]
        return AgentResult(job_id=job.job_id, payload=payload, transcript_events=events)


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


def _strip_code_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _json_answer_candidates(text: str):
    """Yield progressively looser slices of a child answer worth parsing as JSON."""
    raw = str(text or "")
    segments = [raw]
    marker = "\nTurn "
    last = raw.rfind(marker)
    if last >= 0:
        # Tool-loop output prefixes every turn; the final answer is the last one.
        segments.insert(0, raw[last:])
    for segment in segments:
        fenced = _strip_code_fence(segment)
        if fenced:
            yield fenced
        for opener, closer in (("{", "}"), ("[", "]")):
            start = segment.find(opener)
            end = segment.rfind(closer)
            if 0 <= start < end:
                yield segment[start:end + 1]


def parse_json_answer(text: str):
    """Return the JSON value a schema-constrained child produced, else ``None``.

    The host validates ``payload``, so an answer that *is* JSON must be lifted out
    of prose/fences before validation; otherwise a correct answer is reported as a
    schema failure and the run fails closed.
    """
    for candidate in _json_answer_candidates(text):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _split_structured_payload(text: str) -> tuple[str, object | None]:
    """Return ``(prose_prefix, parsed_value)`` when ``text`` carries JSON.

    The prefix is whatever prose precedes the JSON value, so a bounded summary
    can keep the child's own headline without re-inlining the whole object.
    """
    raw = str(text or "")
    # Prose may contain braces of its own, so try each opening bracket in turn
    # (bounded) instead of assuming the first one starts the payload.
    starts = [index for index, char in enumerate(raw) if char in "{["][:64]
    for start in starts:
        value = parse_json_answer(raw[start:])
        if value is not None:
            return raw[:start], value
    value = parse_json_answer(raw)
    if value is not None:
        return "", value
    return raw, None


HANDOFF_SUMMARY_LIMIT = 2_000


def _compact_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _project_structured_value(value, budget: int, marker: str):
    """Shrink ``value`` into a *valid* JSON projection of at most ``budget`` bytes.

    Whole members are kept and the truncation is stated in-band, so a reader can
    never mistake the tail of a JSON object for the real payload.
    """

    def size(item) -> int:
        return len(_compact_json(item).encode("utf-8"))

    if size(value) <= budget:
        return value
    note = {"_truncated": True, "_note": marker}
    if isinstance(value, dict):
        projected: dict = {}
        for key, child in value.items():
            remaining = budget - size(projected) - size(note) - len(str(key)) - 8
            if remaining < 64:
                break
            projected[str(key)] = _project_structured_value(child, remaining, marker)
            if size(projected) + size(note) > budget:
                projected.pop(str(key), None)
                break
        projected.update(note)
        return projected
    if isinstance(value, list):
        items: list = []
        for child in value:
            remaining = budget - size(items) - size(note) - 8
            if remaining < 64:
                break
            candidate = _project_structured_value(child, remaining, marker)
            if size(items + [candidate]) + size(note) > budget:
                break
            items.append(candidate)
        items.append(note)
        return items
    if isinstance(value, str):
        return value[: max(0, budget - 24)].rstrip() + " …"
    return value


def _looks_like_a_cut_off_payload(text: str) -> bool:
    """True when ``text`` carries unbalanced brackets, i.e. a truncated JSON value.

    A fragment that survived an earlier fixed-length cut still *looks* like data
    to a reader, which is how the reported run ended up treating half of
    ``sources[0]`` as the upstream source list. Flagging it costs nothing and
    stops the fragment from passing as a complete answer.
    """
    if "{" not in text and "[" not in text:
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth != 0


def _truncation_marker(ref: str | None) -> str:
    """Say that a summary was bounded *and* where the complete value lives."""
    if ref:
        return f"truncated by the host; full value: {ref}"
    return "truncated by the host; full value: this job's result.json"


def bounded_structured_summary(text, limit: int = HANDOFF_SUMMARY_LIMIT, *, ref: str | None = None) -> str:
    """Bound ``text`` without ever handing on a half-parsed JSON fragment.

    Regression (run ``wf_d1790ea5023946e082b973d9d550de39``): a research child
    answered with a large ``{"sources": [...], "claims": [...]}`` object, and both
    the child payload and the handoff cut it at a fixed character count. The
    synthesis child then received JSON that stopped in the middle of
    ``sources[0]`` and reported the upstream sources as unrecoverable. A summary
    that is structured must stay parseable: keep a bounded projection and say
    where the full value lives.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    marker = _truncation_marker(ref)
    if len(raw) <= limit:
        if parse_json_answer(raw) is None and _looks_like_a_cut_off_payload(raw):
            return f"{raw}\n…[{marker}]"
        return raw
    prefix, structured = _split_structured_payload(raw)
    if structured is not None:
        budget = max(64, limit - len(marker) - 16)
        rendered = _compact_json(_project_structured_value(structured, budget, marker))
        head = "\n".join(
            line for line in prefix.splitlines() if not line.strip().startswith("```")
        ).strip()
        if head and len(head) + len(rendered) + 1 <= limit:
            return f"{head}\n{rendered}"
        if len(rendered) <= limit:
            return rendered
    cut = raw[: max(0, limit - len(marker) - 4)]
    newline = cut.rfind("\n")
    if newline >= limit // 2:
        cut = cut[:newline]
    return f"{cut.rstrip()}\n…[{marker}]"


def _declared_schema(options: dict | None):
    schema = (options or {}).get("schema") if isinstance(options, dict) else None
    return schema if isinstance(schema, dict) and schema else None


def _structured_output_contract(options: dict | None) -> str:
    """Explicit machine-readable output contract for a schema-constrained job.

    Embedding ``options`` as an opaque dict is not enough: the model never learns
    that its answer is validated. Step-Code appends the identical block, so keep
    the wording and the 32 KiB bound aligned with
    ``features/workflow/agent-runner.ts::buildAgentPrompt``.
    """
    schema = _declared_schema(options)
    if schema is None:
        return ""
    try:
        serialized = json.dumps(schema, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return "\n".join([
        "",
        "<workflow-structured-output>",
        "Return exactly one JSON value matching this JSON Schema. Do not wrap it in Markdown fences or add commentary.",
        serialized[:32_000],
        "</workflow-structured-output>",
    ])


def _retry_feedback_block(metadata: dict | None) -> str:
    """Tell a retried child why its previous answer was rejected.

    Without the previous validation issues a retry is a blind re-roll that
    reproduces the same failure.
    """
    feedback = (metadata or {}).get("retryFeedback") if isinstance(metadata, dict) else None
    if not isinstance(feedback, dict):
        return ""
    issues = [str(item).strip() for item in (feedback.get("issues") or []) if str(item).strip()]
    if not issues:
        return ""
    attempt = str(feedback.get("attempt") or "").strip()
    attribute = f' attempt="{int(attempt)}"' if attempt.isdigit() else ""
    return "\n".join([
        "",
        f"<workflow-retry{attribute}>",
        "Previous output failed schema validation: " + "; ".join(issues[:12]),
        "Return the corrected JSON value now, with no prose and no Markdown fences.",
        "</workflow-retry>",
    ])


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
        telemetry_min_interval: float = 1.0,
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
        self.telemetry_min_interval = float(telemetry_min_interval)
        self.last_capability_snapshot: dict = {}
        self._run_capability_schemas: dict[str, tuple[list[dict], dict]] = {}
        self.last_job_tool_profile: str = ""
        self.last_llm_binding: dict = {}
        self._states: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._telemetry_sink = None

    def set_telemetry_sink(self, sink) -> None:
        """Receive ``sink(job_id, payload)`` updates from running children.

        Optional and best-effort: a runner without a sink behaves exactly as
        before, so tests and non-Ink callers are unaffected.
        """
        self._telemetry_sink = sink if callable(sink) else None

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
            payload = self._build_success_payload(job, answer)
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
        live = LiveJobTelemetry(
            self._telemetry_sink,
            job.job_id,
            min_interval=self.telemetry_min_interval,
            client=client,
        )
        handler.live_telemetry = live
        live.update(handler, force=True)
        with self._lock:
            state["handler"] = handler
            cancelled = bool(state.get("cancelled"))
        if cancelled:
            handler.cancel()
        with mcp_cancellation_scope(handler.code_stop_signal):
            tools_schema = self._load_tools_schema(job)
        transcript_events.append({
            "type": "capability_snapshot",
            "runId": job.metadata.get("runId"),
            "jobId": job.job_id,
            "toolProfile": self.last_job_tool_profile,
            "capabilities": copy.deepcopy(self.last_capability_snapshot),
        })
        # Snapshot the workspace before the child runs. Any tool that writes --
        # file_write, file_patch, code_run, or a tool added later -- changes the
        # filesystem, so the before/after difference is the artifact ground truth
        # without hard-coding which tools are "writers".
        from workflow_workspace import diff_workspace, snapshot_workspace

        workspace_root = self._child_cwd(job)
        workspace_before = snapshot_workspace(workspace_root)
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
        tool_summary = self._build_tool_summary(transcript_events)
        written = diff_workspace(workspace_before, snapshot_workspace(workspace_root))
        if written:
            tool_summary["writtenPaths"] = written[:64]
        return output, usage, tool_summary

    def _live_update(self, handler, *, tool_name=None, tool_data=None, force: bool = False) -> None:
        """Best-effort live telemetry hook for a running child.

        The writer is attached to the handler (``handler.live_telemetry``) so
        this stays invisible to callers and test doubles that replace
        ``_build_handler``.
        """
        live = getattr(handler, "live_telemetry", None)
        if live is None:
            return
        if tool_name is not None:
            live.note_tool_call(tool_name)
        if tool_data is not None:
            live.note_tool_result(tool_name, tool_data)
        live.update(handler, force=force)

    def _build_handler(self, job, transcript_events: list[dict], profile: str, version: str):
        from ga import GenericAgentHandler
        from workflow_permissions import ToolPermissionPolicy
        parent = SimpleNamespace(
            task_dir=self._child_cwd(job),
            verbose=False,
            llmclient=SimpleNamespace(backend=SimpleNamespace(history=[])),
            _turn_end_hooks={},
        )
        handler = GenericAgentHandler(parent, cwd=parent.task_dir, workspace_root=parent.task_dir)
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
            self._live_update(handler, tool_name=tool_name)

        def after(tool_name, args, response, ret):
            data = getattr(ret, "data", ret)
            if not isinstance(data, (dict, list, str, int, float, bool, type(None))):
                data = {"content": getattr(data, "content", str(data))}
            transcript_events.append({"type": "tool_result", "toolName": tool_name, "data": copy.deepcopy(data)})
            self._live_update(handler, tool_name=tool_name, tool_data=data)

        handler.tool_before_callback = before
        handler.tool_after_callback = after
        return handler

    def clear_run_capabilities(self, run_id: str) -> None:
        self._run_capability_schemas.pop(str(run_id or ""), None)

    def prepare_run_capabilities(
        self,
        run_id: str,
        required_tools: list[str] | None = None,
        capabilities: list[str] | None = None,
    ) -> dict:
        """Resolve the run's capability snapshot *without* failing the run.

        Step-Code and Codex both treat an unavailable MCP server as a status the
        model can work around, not a fatal error: Codex emits
        ``McpStartupUpdateEvent`` with a reason, Step-Code simply never hands the
        child a tool it could not construct. Raising here turned one degraded
        search server into a dead run, and the child had no idea why -- so it
        fell back to hand-rolled scraping. Report coverage instead; the runtime
        decides whether the run is merely degraded.
        """

        run_id = str(run_id or "")
        if not run_id:
            raise ValueError("workflow capability preflight requires a run id")
        cached = self._run_capability_schemas.get(run_id)
        if cached is None:
            schemas = self._load_tools_schema()
            snapshot = copy.deepcopy(self.last_capability_snapshot)
            self._run_capability_schemas[run_id] = (copy.deepcopy(schemas), snapshot)
        else:
            schemas, snapshot = cached
        tool_names = sorted({name for name in (_tool_name(tool) for tool in schemas or []) if name})
        available = set(tool_names)
        missing_tools = sorted({str(name) for name in required_tools or [] if str(name) and str(name) not in available})
        coverage = capability_coverage(tool_names)
        unavailable = unavailable_capabilities(tool_names)
        snapshot = dict(snapshot or {})
        # One run keeps one environment snapshot: later packets accumulate what
        # the plan declared instead of replacing it, so the run-level report is
        # the union of every agent's declaration rather than whichever agent
        # happened to register last.
        previous = snapshot.get("capabilityReport") if isinstance(snapshot.get("capabilityReport"), dict) else {}
        declared_tools = sorted({*(str(item) for item in (previous.get("requiredTools") or [])), *(str(item) for item in required_tools or [])})
        declared_capabilities = sorted({*(str(item) for item in (previous.get("declaredCapabilities") or [])), *(str(item) for item in capabilities or [])})
        snapshot["toolNames"] = tool_names
        snapshot["capabilityCoverage"] = coverage
        snapshot["unavailableCapabilities"] = unavailable
        snapshot["capabilityReport"] = {
            "requiredTools": declared_tools,
            "missingTools": sorted({*(str(item) for item in (previous.get("missingTools") or [])), *missing_tools}),
            "declaredCapabilities": declared_capabilities,
            "capabilityCoverage": coverage,
            "unavailableCapabilities": unavailable,
        }
        self._run_capability_schemas[run_id] = (copy.deepcopy(schemas), copy.deepcopy(snapshot))
        self.last_capability_snapshot = copy.deepcopy(snapshot)
        return copy.deepcopy(snapshot)

    def _discover_tools_schema(self) -> list[dict]:
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

    def _load_tools_schema(self, job=None):
        """Return the tool schema one job may see.

        The run keeps one unfiltered discovery snapshot (so every child in a run
        sees the same environment); each job then gets the schema narrowed by its
        host-owned tool profile. With no job this returns the unfiltered set,
        which is what the capability preflight reports on.
        """

        metadata = (getattr(job, "metadata", {}) or {}) if job is not None else {}
        run_id = str(metadata.get("runId") or "")
        cached = self._run_capability_schemas.get(run_id) if run_id else None
        if cached is not None:
            tools = copy.deepcopy(cached[0])
            self.last_capability_snapshot = copy.deepcopy(cached[1])
        else:
            tools = self._discover_tools_schema()
            if run_id:
                self._run_capability_schemas[run_id] = (copy.deepcopy(tools), copy.deepcopy(self.last_capability_snapshot))
        if job is None:
            return tools
        profile_name, denied = effective_tool_profile(metadata.get("options") or {})
        self.last_job_tool_profile = profile_name
        return filter_schema_for_denied(tools, denied)

    def _build_system_prompt(self) -> str:
        """Compose the child prompt the same way the root agent does.

        Identity and general capability guidance come from the shared base
        prompt (`assets/sys_prompt.txt`); project facts come from GA_AGENTS.md.
        A child used to get only a one-line identity plus the project doc, so
        every operating rule had to be duplicated into GA_AGENTS.md to reach it.
        """

        script_dir = os.path.dirname(os.path.abspath(__file__))
        identity = self.system_prompt or "You are a workflow child agent. Complete only this assigned job and return a concise result."
        try:
            from ga_agents_runtime import build_ga_project_instructions, load_base_system_prompt
            base_prompt = load_base_system_prompt(script_dir, "_en" if os.environ.get("GA_LANG") == "en" else "")
            project_prompt = build_ga_project_instructions(script_dir, os.getcwd())
        except Exception:
            base_prompt = ""
            project_prompt = ""
        try:
            from skills_runtime import build_skill_prompt
            skill_prompt = build_skill_prompt()
        except Exception:
            skill_prompt = ""
        prompt = identity
        for extra in (base_prompt, project_prompt, skill_prompt):
            if extra:
                prompt += chr(10) + extra
        return prompt

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
        workspace_path = job.metadata.get("workspacePath")
        if workspace_path is not None:
            if not isinstance(workspace_path, str) or not workspace_path.strip():
                raise ValueError("job workspacePath must be a non-empty path")
            workspace = Path(workspace_path).expanduser().resolve()
            if not workspace.is_dir():
                raise ValueError("job workspacePath must be an existing directory")
            return str(workspace)
        workspace = Path(os.path.dirname(os.path.abspath(__file__))) / "temp" / "workflow_child_agents" / str(job.job_id)
        workspace.mkdir(parents=True, exist_ok=True)
        return str(workspace.resolve())

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
        tool_profile, _denied = effective_tool_profile(options)
        role = options.get("role") or ""
        # One line per canonical role. A role that is declared by the plan but
        # gets no instruction is a role the child has to invent, which is how
        # output shapes drifted between runs. Keep every entry to what the role
        # must produce and what counts as evidence.
        role_instructions = {
            "tests": "Write or run tests before implementation. Report the exact command, exit code, and observed RED/GREEN evidence; do not claim a test ran unless it did.",
            "implementation": "Make the smallest change for the assigned implementation. Preserve existing contracts and run relevant tests after edits; do not claim success from code inspection alone. List every changed path in your answer.",
            "verification": "Independently verify the result. Run the requested checks and report machine-observed commands, exit codes, and failures. Return the required structured verification fields. Never infer pass from another agent's summary; missing evidence means verificationPassed=false.",
            "review": "Review independently against the supplied rubric. Report only actionable findings with concrete evidence; do not modify files unless the task explicitly permits it.",
            "research": "Gather evidence from independent sources. Give a URL or workspace-relative path plus a fetch time for every claim, mark anything unverified as unverified, and never invent numbers, dates, version numbers or quotations. Write the full evidence list to the artifact path you were given and keep your answer to a bounded summary.",
            "synthesis": "Combine the upstream results you were handed into one deliverable. Work from the handoff summaries plus the artifact paths -- read those files instead of asking for the raw transcripts back. Preserve each claim's source, and where sources conflict show both instead of averaging.",
            "understanding": "Map the relevant code or material before anything changes. Report exact paths with line references and the constraints later stages must respect; do not modify files.",
            "contract": "State the interface the rest of the run must honour: paths, field names, formats, acceptance checks. Be precise enough that a later agent can implement against it without guessing.",
            "repair": "Fix exactly the reported failure. Reproduce it first, then make the smallest change that removes it, then re-run the same check and report the before/after evidence.",
            "summary": "Summarize what was actually produced: objective, artifacts with workspace-relative paths, verification evidence, and open gaps. Do not restate the plan.",
        }.get(str(role), "")
        dependency_handoff = job.metadata.get("dependencyHandoff") or []
        lines = [
            "You are a workflow child agent. Complete only this assigned job and return a concise result.",
            f"runId: {run_id}",
            f"jobId: {job.job_id}",
            f"phase: {job.phase or ''}",
            f"label: {label}",
            f"role: {role}",
            f"roleInstructions: {role_instructions}",
            f"options: {options}",
            f"permissionProfile: {permission_profile}",
            f"permissionPolicyVersion: {permission_policy_version}",
            f"toolProfile: {tool_profile}",
            f"workspacePath: {self._child_cwd(job)}",
            "workspacePolicy: project-temp-workspace-write-v1; all file/code paths are hard-limited to workspacePath.",
            "toolBoundary: the host already removed every tool outside this profile from your tool list; "
            "work with what you have instead of trying to reconstruct a missing tool by hand.",
            "sharedWorkspace: the root agent and every other job in this run share this workspace; touch only "
            "the paths assigned to you, never revert or rewrite another job's output, and never assume another "
            "job's file is yours to change.",
            "handoff: your final answer is read by the scheduler and by downstream agents, not by the end user. "
            "State objective -> what you did -> machine-observed evidence (command, exit code, path) -> "
            "artifacts (workspace-relative paths) -> blockers. Do not paste raw transcripts.",
        ]
        unavailable = list((self.last_capability_snapshot or {}).get("unavailableCapabilities") or [])
        if unavailable:
            lines.append(
                "unavailableCapabilities: " + ", ".join(str(item) for item in unavailable)
                + " (no tool in that class is connected in this environment; use an available alternative "
                "or report the gap explicitly -- do not fabricate results and do not fall back to ad-hoc scraping)."
            )
        if dependency_handoff:
            lines.extend([
                "",
                "Dependency handoff (bounded; upstream full transcripts are not included):",
            ])
            for item in dependency_handoff:
                lines.append(f"- label: {item.get('label') or ''}")
                lines.append(f"  status: {item.get('status') or ''}")
                lines.append(f"  summary: {item.get('summary') or ''}")
                if item.get("upstreamResultPath"):
                    lines.append(f"  upstreamResultPath (readable copy of that job's result): {item['upstreamResultPath']}")
                if item.get("resultRef"):
                    lines.append(f"  resultRef: {item['resultRef']}")
                if item.get("artifactRefs"):
                    lines.append(f"  artifactRefs: {', '.join(str(ref) for ref in item['artifactRefs'])}")
                owners = item.get("artifactOwners")
                if isinstance(owners, dict) and owners:
                    rendered = "; ".join(
                        f"{ref} <- {', '.join(str(w) for w in writers)}" if isinstance(writers, (list, tuple)) and writers else f"{ref} <- unknown"
                        for ref, writers in owners.items()
                    )
                    lines.append(f"  artifactOwners (which job wrote each ref): {rendered}")
                if item.get("handoffRef"):
                    lines.append(f"  handoffRef: {item['handoffRef']}")
                if item.get("blockingIssues"):
                    lines.append(f"  blockingIssues: {item['blockingIssues']}")
            lines.extend([
                "Path bases: `artifactRefs`, `handoffRef` and `upstreamResultPath` are relative to your workspacePath and you may read them. "
                "`resultRef` and `transcriptRef` are the run's internal audit refs under the run artifact directory, which your workspace "
                "limit does not expose: do not try to open them, and never report them as missing upstream data. When a summary is not "
                "enough, read `upstreamResultPath` -- it is the host's copy of that job's durable result.json.",
            ])
        lines.extend([
            "",
            "Task:",
            job.prompt,
        ])
        contract = _structured_output_contract(options)
        if contract:
            lines.append(contract)
        retry_feedback = _retry_feedback_block(job.metadata)
        if retry_feedback:
            lines.append(retry_feedback)
        return "\n".join(lines)

    @staticmethod
    def _build_success_payload(job, answer: str) -> dict:
        """Shape the child payload the host validates.

        A schema-constrained job must expose its JSON answer at the payload root,
        because that is exactly what ``_apply_schema_contract`` checks. Prose
        answers keep the historic ``{"summary", "text"}`` shape.
        """
        text = str(answer or "").strip()
        options = job.metadata.get("options") if isinstance(job.metadata, dict) else {}
        ref = getattr(job, "result_ref", None)
        if _declared_schema(options):
            parsed = parse_json_answer(answer)
            if isinstance(parsed, dict):
                payload = dict(parsed)
                payload.setdefault("summary", bounded_structured_summary(text, ref=ref))
                payload.setdefault("text", answer)
                return payload
            if isinstance(parsed, list):
                return {"result": parsed, "summary": bounded_structured_summary(text, ref=ref), "text": answer}
        return {"summary": text, "text": answer}
