"""Executors for the five Claude Code-compatible hook handler kinds."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

import requests

from genericagent.hooks.models import (
    GUARD_EVENTS,
    STOP_LIKE_EVENTS,
    HookHandler,
    HookPayload,
    HookResult,
)


# CC 303 m0459:57412-57435: stop/notification-like events ignore
# onFailure=block.  A broken beep or notification must never trap a turn.
# Derived from the shared STOP_LIKE_EVENTS set plus task/idle notifications so
# the stop-like vocabulary has a single source of truth.
ON_FAILURE_IGNORED_EVENTS = frozenset(STOP_LIKE_EVENTS | {"TaskCompleted", "TeammateIdle"})


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _substitute(value: str, payload: dict[str, Any]) -> str:
    text = str(value)
    replacements = {
        "$ARGUMENTS": _json_text(payload),
        "$HOOK_EVENT": str(payload.get("hook_event_name") or ""),
        "$SESSION_ID": str(payload.get("session_id") or ""),
        "$CWD": str(payload.get("cwd") or ""),
    }
    for key, replacement in replacements.items():
        text = text.replace(key, replacement)
    return text


def _expand_path_templates(value: Any, payload: dict[str, Any]) -> Any:
    """Expand CC mcp_tool ``${path.to.input}`` values recursively."""
    if isinstance(value, dict):
        return {str(k): _expand_path_templates(v, payload) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_path_templates(v, payload) for v in value]
    if not isinstance(value, str) or "${" not in value:
        return value
    def replace(match):
        path = match.group(1).strip()
        current: Any = payload
        for part in path.split("."):
            if not part:
                continue
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                return match.group(0)
        if isinstance(current, (dict, list)):
            return _json_text(current)
        return str(current)
    import re
    return re.sub(r"\$\{([^{}]+)\}", replace, value)


def _http_url_allowed(url: str, patterns: list[str] | None) -> bool:
    """Apply CC's HTTP target policy before any network I/O.

    ``None`` means the setting is absent (allow all), while ``[]`` is an
    explicit deny-all policy. Patterns use shell-style ``*`` wildcards.
    """
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if patterns is None:
        return True
    if not patterns:
        return False
    value = str(url)
    return any(fnmatch.fnmatchcase(value, str(pattern)) for pattern in patterns)


def _parse_stdout(result: HookResult, text: str, *, allow_text: bool = False) -> None:
    result.stdout = text or ""
    stripped = result.stdout.strip()
    if not stripped:
        result.output = {}
        return
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        if allow_text:
            # Plain text is a valid command-hook result in CC.  Wrapping it in
            # hookSpecificOutput without hookEventName would fail the very
            # validator that precedes consumption, so carry it as a top-level
            # GA extension instead.
            result.output = {"additionalContext": stripped}
            return
        result.status = "invalid_output"
        result.error = f"hook stdout is not a single JSON object: {exc}"
        return
    if not isinstance(parsed, dict):
        if allow_text:
            result.output = {"additionalContext": stripped}
            return
        result.status = "invalid_output"
        result.error = "hook stdout JSON must be an object"
        return
    result.output = parsed


def _command_execute(handler: HookHandler, payload: dict[str, Any], result: HookResult) -> None:
    command = str(handler.options.get("command") or "")
    raw_args = handler.options.get("args")
    if isinstance(raw_args, list):
        argv = [_substitute(command, payload)] + [_substitute(str(item), payload) for item in raw_args]
        shell = False
    else:
        shell_name = str(handler.options.get("shell") or "").strip().lower()
        if shell_name == "powershell" or (os.name == "nt" and shell_name == "pwsh"):
            argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        elif shell_name == "bash":
            argv = ["bash", "-lc", command]
        else:
            argv = command
        shell = not isinstance(argv, list)
    env = os.environ.copy()
    env.update({
        "GA_HOOK_EVENT": str(payload.get("hook_event_name") or ""),
        "GA_HOOK_RUN_ID": result.hook_run_id,
        "GA_HOOK_SESSION_ID": str(payload.get("session_id") or ""),
    })
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    try:
        proc = subprocess.run(
            argv,
            input=_json_text(payload).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=handler.timeout,
            shell=shell,
            env=env,
            startupinfo=startupinfo,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        result.status = "timeout"
        result.error = f"hook timed out after {handler.timeout:g}s"
        result.stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        result.stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return
    except OSError as exc:
        result.status = "error"
        result.error = f"cannot start hook command: {exc}"
        return
    result.exit_code = int(proc.returncode)
    result.stdout = proc.stdout.decode("utf-8", errors="replace")
    result.stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode == 2:
        result.status = "blocking"
        result.blocking = True
        _parse_stdout(result, result.stdout, allow_text=True)
        if not result.output:
            result.output = {"decision": "block", "reason": result.stderr.strip() or result.stdout.strip() or "hook blocked"}
    elif proc.returncode != 0:
        result.status = "error"
        result.error = f"hook exited with code {proc.returncode}"
        _parse_stdout(result, result.stdout, allow_text=True)
    else:
        result.status = "success"
        _parse_stdout(result, result.stdout)
    # Failure->block promotion is centralized in execute_handler so all five
    # handler kinds share one policy (guard default + explicit onFailure).


def _run_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ga-hook") as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            raise TimeoutError(f"hook timed out after {timeout:g}s") from exc


def _prompt_execute(handler: HookHandler, payload: dict[str, Any], result: HookResult, prompt_runner) -> None:
    if prompt_runner is None:
        result.status = "error"
        result.error = "prompt/agent hook runner is not configured"
        return
    prompt = _substitute(str(handler.options.get("prompt") or ""), payload)
    if "$ARGUMENTS" not in str(handler.options.get("prompt") or ""):
        prompt += "\n\n<hook_input>\n" + _json_text(payload) + "\n</hook_input>"
    if handler.kind == "agent":
        prompt = "You are an isolated GenericAgent hook verifier. " + prompt
    try:
        value = _run_with_timeout(lambda: prompt_runner(handler.kind, prompt, payload), handler.timeout)
    except TimeoutError as exc:
        result.status = "timeout"
        result.error = str(exc)
        return
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        return
    result.status = "success"
    if isinstance(value, dict):
        result.output = value
    else:
        text = str(value or "")
        result.stdout = text
        _parse_stdout(result, text, allow_text=True)


def _http_execute(
    handler: HookHandler,
    payload: dict[str, Any],
    result: HookResult,
    *,
    allowed_urls: list[str] | None = None,
    global_allowed_env_vars: list[str] | None = None,
) -> None:
    url = str(handler.options.get("url") or "")
    if not _http_url_allowed(url, allowed_urls):
        result.status = "error"
        result.error = "http hook URL is blocked by allowedHttpHookUrls policy"
        return
    headers = handler.options.get("headers") if isinstance(handler.options.get("headers"), dict) else {}
    allowed = handler.options.get("allowedEnvVars")
    handler_names = {str(item) for item in allowed} if isinstance(allowed, list) else set()
    # CC intersects the handler list with the global allowlist. With no handler
    # list there is nothing to interpolate. With no global list, the handler
    # list is authoritative.
    allowed_names = handler_names
    if global_allowed_env_vars is not None:
        allowed_names = handler_names & {str(item) for item in global_allowed_env_vars}
    resolved_headers: dict[str, str] = {}
    for key, value in headers.items():
        text = str(value)
        for name in allowed_names:
            text = text.replace(f"${name}", str(os.environ.get(name, "")))
            text = text.replace(f"${{{name}}}", str(os.environ.get(name, "")))
        resolved_headers[str(key)] = text
    try:
        # CC requires HTTP hooks not to follow redirects. A redirect could turn
        # an allowlisted endpoint into an arbitrary outbound request.
        response = requests.post(
            url,
            headers=resolved_headers,
            json=payload,
            timeout=handler.timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        return
    result.exit_code = int(response.status_code)
    result.stdout = str(response.text or "")
    if response.status_code < 200 or response.status_code >= 300:
        result.status = "error"
        result.error = f"http hook returned status {response.status_code}"
        return
    result.status = "success"
    _parse_stdout(result, result.stdout, allow_text=False)


def _mcp_execute(handler: HookHandler, payload: dict[str, Any], result: HookResult, mcp_call) -> None:
    tool = str(handler.options.get("tool") or "").strip()
    server = str(handler.options.get("server") or "").strip()
    if not tool.startswith("mcp__") and server:
        from genericagent.mcp.runtime import build_mcp_tool_name
        tool = build_mcp_tool_name(server, tool)
    if not tool.startswith("mcp__"):
        result.status = "error"
        result.error = "mcp_tool handler must resolve to an mcp__ server tool"
        return
    raw_input = handler.options.get("input")
    args = _expand_path_templates(raw_input, payload) if isinstance(raw_input, (dict, list)) else payload
    caller = mcp_call
    if caller is None:
        from genericagent.mcp.runtime import call_mcp_tool
        caller = call_mcp_tool
    try:
        value = _run_with_timeout(lambda: caller(tool, args), handler.timeout)
    except TimeoutError as exc:
        result.status = "timeout"
        result.error = str(exc)
        return
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        return
    result.status = "success"
    if isinstance(value, dict):
        result.output = value
        if value.get("status") == "error":
            result.status = "error"
            result.error = str(value.get("msg") or value.get("error") or "MCP hook tool failed")
    elif isinstance(value, list):
        result.output = {"systemMessage": _json_text(value)}
    else:
        result.output = {"systemMessage": str(value or "")}


def _validate_output(handler: HookHandler, result: HookResult) -> None:
    """Validate the CC fields GA consumes, without rejecting forward-compatible fields."""
    if result.status not in {"success", "blocking"} or not result.output:
        return
    output = result.output_dict
    specific = result.hook_specific
    if not specific:
        return
    event_name = str(specific.get("hookEventName") or "").strip()
    if not event_name:
        result.status = "invalid_output"
        result.error = 'hookSpecificOutput is missing required field "hookEventName"'
        return
    if event_name != result.event:
        result.status = "invalid_output"
        result.error = f"hookSpecificOutput.hookEventName {event_name!r} does not match {result.event!r}"
        return
    if handler.kind == "http" and str(output.get("decision") or "").strip().lower() not in {"", "approve", "block"}:
        result.status = "invalid_output"
        result.error = f"unsupported hook decision: {output.get('decision')!r}"
        return
    if result.event == "PermissionRequest":
        decision = specific.get("decision")
        if not isinstance(decision, dict):
            result.status = "invalid_output"
            result.error = (
                'PermissionRequest decision must be '
                '{"behavior":"allow"} or {"behavior":"deny","message":"..."}'
            )
            return
        behavior = str(decision.get("behavior") or "").strip().lower()
        if behavior not in {"allow", "deny"}:
            result.status = "invalid_output"
            result.error = f"unsupported PermissionRequest decision.behavior: {decision.get('behavior')!r}"
            return
        if behavior == "allow":
            updated = decision.get("updatedInput")
            if updated is not None and not isinstance(updated, dict):
                result.status = "invalid_output"
                result.error = "PermissionRequest decision.updatedInput must be an object"
                return
            updated_permissions = decision.get("updatedPermissions")
            if updated_permissions is not None and not isinstance(updated_permissions, list):
                result.status = "invalid_output"
                result.error = "PermissionRequest decision.updatedPermissions must be an array"
                return
        elif decision.get("interrupt") is not None and not isinstance(decision.get("interrupt"), bool):
            result.status = "invalid_output"
            result.error = "PermissionRequest decision.interrupt must be a boolean"


def _apply_failure_policy(handler: HookHandler, result: HookResult) -> None:
    """Promote a failed hook to a blocking verdict per CC 303 rules.

    A hook is *failed* when it errored, timed out, or produced invalid output
    (running/blocking/success results are left untouched). The policy:
      - Stop-likes (Stop/SubagentStop/StopFailure/TaskCompleted/TeammateIdle):
        never promote - a broken beep/notification must not trap a turn.
      - Guard events (PreToolUse/PermissionRequest/UserPromptSubmit):
        fail-closed by default (an unset onFailure still blocks; CC m0459:158274
        Ilt()/F1e()). An explicit ``onFailure: continue`` opts out.
      - Every other event: opt-in only via ``onFailure: block``.
    """
    if result.status not in {"error", "timeout", "invalid_output"}:
        return
    if result.event in ON_FAILURE_IGNORED_EVENTS:
        return
    policy = handler.on_failure_explicit
    if policy == "continue":
        return
    should_block = policy == "block" or result.event in GUARD_EVENTS
    if not should_block:
        return
    result.status = "blocking"
    result.blocking = True
    result.output = {"decision": "block", "reason": result.error or "hook failed"}


def execute_handler(
    handler: HookHandler,
    payload: dict[str, Any],
    result: HookResult,
    *,
    prompt_runner=None,
    mcp_call=None,
    allowed_http_urls: list[str] | None = None,
    global_allowed_env_vars: list[str] | None = None,
) -> HookResult:
    started = time.perf_counter()
    try:
        if handler.kind == "command":
            _command_execute(handler, payload, result)
        elif handler.kind in ("prompt", "agent"):
            _prompt_execute(handler, payload, result, prompt_runner)
        elif handler.kind == "http":
            _http_execute(
                handler,
                payload,
                result,
                allowed_urls=allowed_http_urls,
                global_allowed_env_vars=global_allowed_env_vars,
            )
        elif handler.kind == "mcp_tool":
            _mcp_execute(handler, payload, result, mcp_call)
        else:
            result.status = "error"
            result.error = f"unsupported hook handler type: {handler.kind}"
    except Exception as exc:
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        _validate_output(handler, result)
        _apply_failure_policy(handler, result)
        result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result
