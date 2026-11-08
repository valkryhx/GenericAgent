"""退出码语义化：把「非零 = 错误」拆成「非零但正常」。

**问题**：`handler.code_run` 现在写的是

    status = "success" if exit_code == 0 else "error"

`grep` 没匹配到返回 1、`diff` 有差异返回 1、`test` 判断为假返回 1——全是
`status: "error"`。调研文档（`docs/20261107-CC内置工具实现调研.md` §16.1）把它
列为 P0，原话是「模型最常见的误判来源」。

**为什么不能只按命令名判断**：`code_run` 在 Windows 上把 `type=bash`
交给 **PowerShell** 执行（`handler.py` 的 `code_run()`：`if os.name == 'nt':
cmd = ["powershell", ...]`）。此时命令里即使写着 `grep`，真正跑的是
PowerShell，`grep` 可能根本不存在，退出码含义完全不同。所以分类函数必须
拿到**实际执行器**，在非 POSIX shell 下退回保守判断。

**保守原则**：拿不准时给 `error`。宁可让模型多看一眼，也不要让它把真失败
当成「没匹配」而继续往下走。
"""

from __future__ import annotations

import re
import shlex

SUCCESS = "success"
ERROR = "error"
# 非零但有明确「不是失败」含义的退出码。
INFORMATIONAL = "informational"

# 退出码 1 表示「没有结果 / 结果为假 / 内容有差异」，而不是执行失败。
_EXIT_ONE_INFORMATIONAL: dict[str, str] = {
    "grep": "no matches",
    "egrep": "no matches",
    "fgrep": "no matches",
    "rg": "no matches",
    "ag": "no matches",
    "ack": "no matches",
    "diff": "files differ",
    "test": "condition is false",
    "[": "condition is false",
    "find": "some paths were unreadable",
    "cmp": "files differ",
}

# 常见的「命令前缀」，解析时跳过，好让真正的命令名浮出来。
_PREFIX_COMMANDS = frozenset({"sudo", "env", "time", "command", "nohup", "exec"})

# 分隔符：一条命令里可能有多个，取第一个非前缀命令判断足够。
_COMMAND_SPLIT = re.compile(r"\|\||&&|[;|]")


def _first_command_name(command: str) -> str:
    """Best-effort: the name of the first real command in a shell command line."""

    text = str(command or "").strip()
    if not text:
        return ""
    # Skip leading env assignments (`FOO=1 grep ...`) and shell builtins we know.
    for segment in _COMMAND_SPLIT.split(text):
        candidate = segment.strip()
        if not candidate:
            continue
        # Strip leading `VAR=value` assignments.
        while True:
            stripped = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+", "", candidate)
            if stripped == candidate:
                break
            candidate = stripped
        try:
            parts = shlex.split(candidate, posix=True)
        except ValueError:
            parts = candidate.split()
        for part in parts:
            if part in _PREFIX_COMMANDS:
                continue
            # `python -c "..."` etc.: the first non-prefix token is the command.
            return part.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return ""


def classify_exit_code(
    *,
    code_type: str,
    command: str,
    exit_code: int,
    executor_is_posix_shell: bool,
) -> str:
    """Classify an exit code into `success` / `error` / `informational`.

    ``executor_is_posix_shell`` must be False when the command ran under a
    non-POSIX interpreter (PowerShell on Windows, Python, etc.); the POSIX
    convention that exit 1 means "no matches" does not transfer.
    """

    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        return ERROR
    if code == 0:
        return SUCCESS
    if code != 1:
        # Only exit 1 has the widely-shared "no results" meaning. Anything else
        # (2 = usage error, 127 = not found, 130 = interrupted) is a real problem.
        return ERROR
    if not executor_is_posix_shell:
        return ERROR
    kind = str(code_type or "").strip().lower()
    if kind not in {"bash", "sh", "shell"}:
        # `python`/`powershell` scripts define their own exit conventions.
        return ERROR
    name = _first_command_name(command)
    if name in _EXIT_ONE_INFORMATIONAL:
        return INFORMATIONAL
    if name.startswith("grep") or name.startswith("rg"):
        # `grep-2.5`, `rg` variants; still "no matches" semantics.
        return INFORMATIONAL
    return ERROR


def exit_code_note(*, code_type: str, command: str, exit_code: int, executor_is_posix_shell: bool) -> str:
    """A short human-readable reason for a non-zero exit code, or ''.

    Included in the streamed `[Status]` line so the model does not have to infer
    what an exit code means from the tool name.
    """

    if classify_exit_code(
        code_type=code_type,
        command=command,
        exit_code=exit_code,
        executor_is_posix_shell=executor_is_posix_shell,
    ) != INFORMATIONAL:
        return ""
    name = _first_command_name(command)
    meaning = _EXIT_ONE_INFORMATIONAL.get(name)
    if meaning is None:
        meaning = "no matches"
    return f"exit 1 means {meaning}, not a failure"
