"""Matcher and handler-level ``if`` filtering for hooks."""

from __future__ import annotations

import fnmatch
import json
import re

from genericagent.hooks.models import HookEntry, HookHandler, HookPayload


def _target_text(payload: HookPayload) -> str:
    data = payload.data
    return str(data.get("tool_name") or data.get("agent_type") or data.get("task_type") or "")


def _tool_args_text(payload: HookPayload) -> str:
    data = payload.data
    args = data.get("tool_input") or data.get("args") or {}
    if isinstance(args, dict):
        for key in ("command", "cmd", "script", "path", "file_path", "query", "prompt"):
            value = args.get(key)
            if isinstance(value, str):
                return value
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def _match_pattern(pattern: str, value: str) -> bool:
    pattern = str(pattern or "").strip()
    if not pattern or pattern == "*":
        return True
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2:
        try:
            return re.search(pattern[1:-1], value) is not None
        except re.error:
            return False
    if "|" in pattern:
        return any(_match_pattern(part, value) for part in pattern.split("|"))
    return fnmatch.fnmatchcase(value, pattern) or value == pattern


def entry_matches(entry: HookEntry, payload: HookPayload) -> bool:
    return not entry.matcher or _match_pattern(entry.matcher, _target_text(payload))


def handler_matches(handler: HookHandler, payload: HookPayload) -> bool:
    target = _target_text(payload)
    rule = str(handler.options.get("if") or "").strip()
    if not rule:
        return True
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\((.*)\)$", rule, re.DOTALL)
    if match:
        tool_name, inner = match.group(1), match.group(2).strip()
        actual = _tool_args_text(payload) if tool_name == "Bash" else target
        return _match_pattern(inner, actual)
    return _match_pattern(rule, target)
