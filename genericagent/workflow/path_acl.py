"""Host-side, deterministic path ACL for workflow child tool calls.

Step-Code enforces path policy in one host-side pure function
(``features/workflow/tool-profile.ts::checkWorkflowToolCall``) that runs before
a child tool call executes. GA previously spread the same rule across the
in-process ``workflow_workspace_guard`` monkeypatch, ``GenericAgentHandler._get_abs_path``
and prompt text, which drifts and leaves gaps.

This module is the single source of truth for "may this path be read / written /
executed?". It is deliberately pure: it only decides, and the caller reports the
decision as a tool error. It resolves symlinks on the existing ancestor so a
symlink cannot escape the workspace, and it parses shell redirection/``mv``/``cp``/
``tee``/``of=`` targets for execute calls.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


READ = "read"
WRITE = "write"
EXECUTE = "execute"

READ_TOOL_NAMES = frozenset({"file_read"})
WRITE_TOOL_NAMES = frozenset({"file_write", "file_patch"})
EXECUTE_TOOL_NAMES = frozenset({"code_run", "web_execute_js"})
NON_FILESYSTEM_TOOL_NAMES = frozenset({"no_tool", "ask_user", "load_skill", "web_scan"})

FILE_REF_PATTERN = re.compile(r"\{\{file:(.+?):(\d+):(\d+)\}\}")


@dataclass(frozen=True)
class PathAclDecision:
    allowed: bool
    operation: str
    target: str = ""
    reason: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "operation": self.operation,
            "target": self.target,
            "reason": self.reason,
            "details": dict(self.details),
        }


def canonical_workspace_path(workspace_root, target: str) -> str:
    """Canonicalize ``target`` relative to the workspace, resolving symlinks.

    The existing ancestor is resolved with ``realpath`` and missing tail
    components are re-appended, so a not-yet-created file still gets a canonical
    path and a symlinked parent cannot be used to escape.
    """

    root = os.path.realpath(str(workspace_root))
    absolute = os.path.abspath(os.path.join(root, str(target or ".").strip() or "."))
    cursor = absolute
    missing: list[str] = []
    while not os.path.exists(cursor):
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        missing.append(os.path.basename(cursor))
        cursor = parent
    base = os.path.realpath(cursor)
    for part in reversed(missing):
        base = os.path.join(base, part)
    return os.path.normpath(base)


def is_inside_workspace(workspace_root, target: str) -> bool:
    root = os.path.normpath(os.path.realpath(str(workspace_root)))
    candidate = canonical_workspace_path(workspace_root, target)
    return candidate == root or candidate.startswith(root + os.sep)


def check_path_access(workspace_root, target: str, operation: str) -> PathAclDecision:
    """Decide whether ``target`` may be used for ``operation`` in the workspace."""

    raw = str(target or "").strip()
    if not raw:
        return PathAclDecision(False, operation, "", "tool call did not provide a target path")
    canonical = canonical_workspace_path(workspace_root, raw)
    root = os.path.normpath(os.path.realpath(str(workspace_root)))
    if not (canonical == root or canonical.startswith(root + os.sep)):
        return PathAclDecision(
            False,
            operation,
            canonical,
            f"{operation} target is outside the workspace: {raw}",
            {"workspaceRoot": root},
        )
    return PathAclDecision(True, operation, canonical)


def command_write_targets(command: str) -> list[str]:
    """Best-effort extraction of write targets from a shell command string."""

    if not command or not command.strip():
        return []
    targets: list[str] = []
    patterns = (
        # > file, >> file, 2> file, &> file
        re.compile(r"(?:^|[\s|;&])(?:\d*|&)>{1,2}\s*(?:'([^']*)'|\"([^\"]*)\"|([^\s|;&]+))"),
        # mv a b / cp a b  (destination is the last argument; the leading \s+
        # forces the first token to be consumed so we capture the destination)
        re.compile(r"\b(?:mv|cp)\b\s+[^|;&]*?\s+([^\s|;&]+)(?:\s|$)"),
        # tee [-a] file
        re.compile(r"\btee\b(?:\s+-\S+)*\s+([^\s|;&]+)"),
        # dd of=file
        re.compile(r"\bof=(?:'([^']*)'|\"([^\"]*)\"|([^\s|;&]+))"),
    )
    for pattern in patterns:
        for match in pattern.finditer(command):
            value = next((group for group in match.groups() if group), None)
            if value:
                targets.append(_strip_shell_quotes(value))
    return targets


def _strip_shell_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def file_ref_targets(text: str) -> list[str]:
    """Return the paths referenced by ``{{file:path:start:end}}`` markers."""

    return [match.group(1) for match in FILE_REF_PATTERN.finditer(str(text or ""))]


def check_tool_call(workspace_root, tool_name: str, args: dict | None) -> PathAclDecision:
    """Decide a workflow child tool call against the workspace path policy.

    Only filesystem-affecting tools are inspected; every other tool is allowed
    here (tool *capability* remains the permission profile's job). Ordering
    mirrors Step-Code: read tools -> the ``path`` argument; write tools ->
    ``path``; execute tools -> cwd plus parsed write targets; content-bearing
    write tools -> ``{{file:}}`` read references.
    """

    if not workspace_root:
        return PathAclDecision(True, READ, "", "no workspace root configured")
    name = str(tool_name or "")
    data = args if isinstance(args, dict) else {}
    if name in NON_FILESYSTEM_TOOL_NAMES:
        return PathAclDecision(True, READ, "", "tool does not touch the filesystem")

    if name in READ_TOOL_NAMES:
        target = str(data.get("path") or "")
        if not target.strip():
            return PathAclDecision(False, READ, "", "file_read requires a path")
        return check_path_access(workspace_root, target, READ)

    if name in WRITE_TOOL_NAMES:
        target = str(data.get("path") or "")
        if not target.strip():
            return PathAclDecision(False, WRITE, "", f"{name} requires a path")
        decision = check_path_access(workspace_root, target, WRITE)
        if not decision.allowed:
            return decision
        # file_write/file_patch expand {{file:...}} in their content; those
        # expansions must stay inside the workspace even when the output path is
        # legal, otherwise a legal write could smuggle an out-of-scope read.
        content = str(data.get("content") or data.get("new_content") or "")
        for reference in file_ref_targets(content):
            reference_decision = check_path_access(workspace_root, reference, READ)
            if not reference_decision.allowed:
                return PathAclDecision(
                    False,
                    READ,
                    reference_decision.target,
                    f"{{{{file:...}}}} reference is outside the workspace: {reference}",
                    {"viaTool": name},
                )
        return decision

    if name in EXECUTE_TOOL_NAMES:
        tool_type = str(data.get("type") or data.get("code_type") or "python").lower()
        if tool_type in {"python", "py", "javascript", "js"}:
            # An in-process interpreter runs under the code_run workspace guard;
            # its explicit cwd still has to be inside the workspace.
            cwd = str(data.get("cwd") or ".")
            return check_path_access(workspace_root, cwd, EXECUTE)
        command = str(data.get("code") or data.get("script") or data.get("command") or "")
        for target in command_write_targets(command):
            decision = check_path_access(workspace_root, target, EXECUTE)
            if not decision.allowed:
                return PathAclDecision(
                    False,
                    EXECUTE,
                    decision.target,
                    f"shell command writes outside the workspace: {target}",
                    {"viaTool": name},
                )
        return PathAclDecision(True, EXECUTE, "", "shell command write targets are inside the workspace")

    return PathAclDecision(True, READ, "", "tool is not path-classified")
