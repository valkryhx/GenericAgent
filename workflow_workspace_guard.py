"""Best-effort in-process guard for workflow Python code_run children.

Path confinement (file reads / writes / removes) is always enforced: a child may
only touch paths inside its workspace.

Subprocess execution follows Claude Code's model. A workflow child is allowed to
run commands (the child's own tool permissions still gate whether it has
``code_run`` at all), and an optional per-agent ``bashCommandClamp`` narrows the
allowed command *forms* — mirroring CC's ``agent({bashCommandClamp:[...]})``.
Two escape hatches exist:

* ``GA_WORKFLOW_GUARD_STRICT=1`` restores the old behaviour of blocking every
  subprocess (fail-closed for a run that wants the stricter posture).
* ``GA_WORKFLOW_BASH_CLAMP`` (a JSON array of command-prefix rules, set by the
  host from ``agent({bashCommandClamp})``) scopes the child to matching forms;
  a command with shell structure the clamp cannot verify is denied (fail-closed),
  matching CC.

This guard is best-effort by design: it steers cooperative child code and is not
a security boundary (a child that can run Python can always reach the OS through
a path the in-process monkeypatch cannot observe).
"""
from __future__ import annotations

import builtins
import io
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


# Catastrophic commands that are denied even when subprocess is otherwise
# allowed. This is a last-resort backstop, not a complete denylist.
_DANGEROUS_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\brm\s+-[a-z]*r[a-z]*f",              # rm -rf (either flag order)
        r"\brm\s+-[a-z]*f[a-z]*r",
        r"\brm\s+-rf\s+/",                       # rm -rf /
        r"\bdel\s+/[fsq]",                       # Windows del /f /s /q
        r"\bformat\b\s+[a-z]:",                  # format C:
        r"\bmkfs(\.[a-z0-9]+)?\b",               # mkfs / mkfs.ext4
        r"\bdiskpart\b",
        r"\bshutdown\b",
        r"\bdd\b[^\n]*\bof=/dev/",               # dd of=/dev/...
        r":\(\)\s*\{\s*:\s*\|\s*:",              # fork bomb
        r"\bremove-item\b[^\n]*-recurse[^\n]*-force",
    )
)

# Shell structure (substitution / control flow / redirection) that a clamp rule
# set cannot decompose, so a clamped command carrying it is denied fail-closed.
_SHELL_STRUCTURE = re.compile(r"[;&|`<>]|\$\(|\$\{|\n")


def _normalize_command(args, kwargs):
    """Return ``(argv, shell_text)`` for a subprocess / os call, best effort."""
    command = args[0] if args else kwargs.get("args")
    if command is None:
        return [], None
    if isinstance(command, (list, tuple)):
        return [str(item) for item in command], None
    if isinstance(command, (bytes, bytearray)):
        command = command.decode("utf-8", "replace")
    if isinstance(command, str):
        if kwargs.get("shell"):
            return [], command
        try:
            return shlex.split(command), None
        except ValueError:
            return [command], None
    return [str(command)], None


def _matches_rule(argv, rule_tokens) -> bool:
    if not argv or not rule_tokens or len(rule_tokens) > len(argv):
        return False
    for index, token in enumerate(rule_tokens):
        candidate = argv[index]
        if candidate == token or os.path.basename(candidate) == os.path.basename(token):
            continue
        return False
    return True


def _build_subprocess_check(root: Path):
    """Build the subprocess policy checker from the process environment."""
    strict = str(os.environ.get("GA_WORKFLOW_GUARD_STRICT") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    clamp = None
    raw_clamp = os.environ.get("GA_WORKFLOW_BASH_CLAMP")
    if raw_clamp:
        try:
            parsed = json.loads(raw_clamp)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            clamp = [str(item) for item in parsed if str(item).strip()]

    def check(args, kwargs) -> None:
        argv, shell_text = _normalize_command(args, kwargs)
        text = shell_text if shell_text is not None else " ".join(argv)
        if strict:
            raise PermissionError("workflow code_run subprocess is disabled by workspace guard")
        for pattern in _DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(text):
                raise PermissionError("workflow code_run subprocess command is denied: " + text)
        if clamp:
            if shell_text is not None and _SHELL_STRUCTURE.search(shell_text):
                raise PermissionError(
                    "workflow code_run subprocess command has shell structure a bashCommandClamp "
                    "cannot verify: " + text
                )
            rules = [shlex.split(rule) for rule in clamp if rule]
            if not any(_matches_rule(argv, rule) for rule in rules):
                raise PermissionError(
                    "workflow code_run subprocess command matches no bashCommandClamp rule: " + text
                )
        # Confine the working directory to the workspace.
        cwd = kwargs.get("cwd")
        if cwd is None:
            kwargs["cwd"] = str(root)
        else:
            candidate = Path(str(cwd))
            resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise PermissionError(f"workflow code_run subprocess cwd outside workspace: {cwd}") from exc
            kwargs["cwd"] = str(resolved)

    return check


def install(root: str) -> None:
    workspace = Path(root).resolve()

    def resolve(value):
        if isinstance(value, int):
            return value
        path = Path(value)
        candidate = path.resolve() if path.is_absolute() else (workspace / path).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise PermissionError(f"workflow code path outside workspace: {value}") from exc
        return str(candidate)

    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    original_remove = os.remove
    original_unlink = os.unlink
    original_mkdir = os.mkdir
    original_makedirs = os.makedirs
    original_rename = os.rename
    original_replace = os.replace
    original_rmtree = shutil.rmtree

    def guarded_open(file, *args, **kwargs):
        return original_open(resolve(file), *args, **kwargs)

    def guarded_io_open(file, *args, **kwargs):
        return original_io_open(resolve(file), *args, **kwargs)

    def guarded_os_open(file, *args, **kwargs):
        return original_os_open(resolve(file), *args, **kwargs)

    def guarded_unary(fn):
        return lambda file, *args, **kwargs: fn(resolve(file), *args, **kwargs)

    def guarded_mkdir(path, *args, **kwargs):
        return original_mkdir(resolve(path), *args, **kwargs)

    def guarded_makedirs(name, *args, **kwargs):
        return original_makedirs(resolve(name), *args, **kwargs)

    def guarded_move(src, dst, *args, **kwargs):
        return original_rename(resolve(src), resolve(dst), *args, **kwargs)

    def guarded_replace(src, dst, *args, **kwargs):
        return original_replace(resolve(src), resolve(dst), *args, **kwargs)

    def guarded_rmtree(path, *args, **kwargs):
        return original_rmtree(resolve(path), *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_io_open
    os.open = guarded_os_open
    os.remove = guarded_unary(original_remove)
    os.unlink = guarded_unary(original_unlink)
    os.mkdir = guarded_mkdir
    os.makedirs = guarded_makedirs
    os.rename = guarded_move
    os.replace = guarded_replace
    shutil.rmtree = guarded_rmtree

    # Subprocess policy: allow (confined) by default, clamp to declared command
    # forms when configured, block everything under GA_WORKFLOW_GUARD_STRICT.
    check = _build_subprocess_check(workspace)

    original_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self, *args, **kwargs):
        check(args, kwargs)
        return original_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = guarded_popen_init

    original_os_system = os.system
    original_os_popen = os.popen

    def guarded_os_system(command):
        check((command,), {"shell": True})
        return original_os_system(command)

    def guarded_os_popen(command, *args, **kwargs):
        check((command,), {"shell": True})
        return original_os_popen(command, *args, **kwargs)

    os.system = guarded_os_system
    os.popen = guarded_os_popen
