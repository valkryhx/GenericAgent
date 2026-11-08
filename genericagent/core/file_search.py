"""文件搜索：`file_glob` 与 `file_grep` 的共用实现。

**为什么补这两个工具**：GA 之前没有搜索类工具，模型只能靠 `file_read(keyword=)`
（只能搜单个已知文件内部、只回第一个匹配）或 `code_run` 里自己拼 `grep`。
后者的问题不是不能用，而是**结果规模由模型自己决定**——`code_run` 的输出形状
完全取决于模型写的命令，缺乏统一的边界约束。

**为什么是两个工具而不是一个**：Claude Code 319 把它们分开，且结果上限差 5 倍
（Glob 100,000 / Grep 20,000，见 `docs/20261107-CC内置工具实现调研.md` §10.1）。
理由写在源码注释里：「Glob 只回路径（短），Grep 回内容行（长）」。合并成一个
「万能搜索」会让只想列文件名的调用也背上内容行的预算。

**为什么优先用 rg**：`ripgrep` 已经处理了 `.gitignore`、二进制文件跳过、
大文件流式读取这些琐碎但影响正确性的事。没有 rg 时回退到纯 Python 实现，
功能对齐但会慢一些——所以回退路径必须明确告知调用方（见 `engine` 字段）。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 与 Claude Code 对齐：Glob 硬上限 100 个文件（调研文档 §10.2 `U7o()`）。
GLOB_MAX_FILES = 100
# Grep 默认最多返回多少条（行）。CC 用 250，这里保持一致。
GREP_DEFAULT_HEAD_LIMIT = 250
# 单个文件超过这个大小就跳过内容搜索（避免把 GB 级日志读进内存）。
GREP_MAX_FILE_BYTES = 4 * 1024 * 1024
# 走 Python 回退时最多遍历多少个文件，防止在巨型树上卡死。
WALK_MAX_FILES = 20_000

_BINARY_SNIFF_BYTES = 8192
_COMMON_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
})


@dataclass
class GlobResult:
    filenames: list[str] = field(default_factory=list)
    total_matches: int = 0
    truncated: bool = False
    engine: str = "python"
    note: str = ""


@dataclass
class GrepMatch:
    path: str
    line_number: int
    text: str


@dataclass
class GrepResult:
    matches: list[GrepMatch] = field(default_factory=list)
    total_matches: int = 0
    truncated: bool = False
    engine: str = "python"
    mode: str = "content"
    note: str = ""


def find_ripgrep() -> str | None:
    """Locate ``rg``, falling back to common Git/Chocolatey install locations.

    ``shutil.which`` covers PATH. The extra probes matter on Windows, where rg
    often ships alongside Git but is not on PATH.
    """

    found = shutil.which("rg")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "usr" / "bin" / "rg.exe",
        Path("C:/ProgramData/chocolatey/bin/rg.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ripgrep" / "rg.exe",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _is_probably_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True
    return b"\x00" in chunk


def _iter_files(root: Path, *, pattern: str | None = None):
    """Yield files under ``root``, skipping VCS/dependency directories."""

    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _COMMON_IGNORE_DIRS]
        for name in filenames:
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            seen += 1
            if seen > WALK_MAX_FILES:
                return
            yield Path(dirpath) / name


def glob_files(root: str, pattern: str, *, limit: int = GLOB_MAX_FILES) -> GlobResult:
    """Match ``pattern`` under ``root``. Returns relative paths, sorted."""

    base = Path(root)
    if not base.is_dir():
        return GlobResult(note=f"not a directory: {root}")

    rg = find_ripgrep()
    matches: list[str] = []
    engine = "python"
    if rg:
        # `--files` lists files; `--glob` applies the pattern. Hidden files are
        # included because a bare `**/*.json` should still find `.eslintrc.json`.
        command = [rg, "--files", "--hidden", "--glob", pattern, str(base)]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30, check=False,
            )
            if completed.returncode in (0, 1):
                engine = "rg"
                matches = [
                    line.strip() for line in (completed.stdout or "").splitlines() if line.strip()
                ]
        except (OSError, subprocess.SubprocessError):
            matches = []

    if engine != "rg":
        # Pure-Python fallback: match the whole relative path, so `src/**/*.py`
        # behaves like it does with ripgrep.
        for path in _iter_files(base):
            rel = path.relative_to(base).as_posix()
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern):
                matches.append(str(path))

    normalized: list[str] = []
    for raw in matches:
        try:
            normalized.append(Path(raw).resolve().relative_to(base.resolve()).as_posix())
        except (ValueError, OSError):
            normalized.append(raw)
    normalized.sort()

    total = len(normalized)
    truncated = total > limit
    return GlobResult(
        filenames=normalized[:limit],
        total_matches=total,
        truncated=truncated,
        engine=engine,
    )


def _compile(pattern: str, ignore_case: bool):
    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE
    return re.compile(pattern, flags)


def grep_files(
    root: str,
    pattern: str,
    *,
    glob: str | None = None,
    ignore_case: bool = False,
    context: int = 0,
    head_limit: int = GREP_DEFAULT_HEAD_LIMIT,
    mode: str = "content",
) -> GrepResult:
    """Search file contents under ``root``.

    ``mode`` is one of ``content`` / ``files_with_matches`` / ``count`` and only
    affects how results are aggregated; the scan is the same.
    """

    base = Path(root)
    if not base.is_dir():
        return GrepResult(note=f"not a directory: {root}", mode=mode)

    normalized_mode = str(mode or "content").strip().lower()
    if normalized_mode not in {"content", "files_with_matches", "count"}:
        return GrepResult(note=f"unsupported output_mode: {mode}", mode=mode)

    try:
        regex = _compile(pattern, ignore_case)
    except re.error as exc:
        return GrepResult(note=f"invalid regular expression: {exc}", mode=normalized_mode)

    matches: list[GrepMatch] = []
    total = 0
    truncated = False

    for path in _iter_files(base, pattern=glob):
        try:
            if path.stat().st_size > GREP_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        if _is_probably_binary(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        file_hits: list[int] = []
        for index, line in enumerate(lines):
            if not regex.search(line):
                continue
            total += 1
            if normalized_mode == "content" and not truncated:
                for offset in range(-context, context + 1):
                    position = index + offset
                    if 0 <= position < len(lines):
                        matches.append(GrepMatch(
                            path=str(path), line_number=position + 1, text=lines[position],
                        ))
                if total >= head_limit:
                    truncated = True
            elif normalized_mode == "files_with_matches":
                if not truncated:
                    matches.append(GrepMatch(path=str(path), line_number=0, text=""))
                if total >= head_limit:
                    truncated = True
            else:  # count
                file_hits.append(index + 1)
            if truncated and normalized_mode == "content":
                break

        if normalized_mode == "count" and file_hits:
            if len(matches) >= head_limit:
                truncated = True
                break
            matches.append(GrepMatch(path=str(path), line_number=0, text=str(len(file_hits))))

    # files_with_matches dedupes: one entry per file, but `total` counts hits.
    if normalized_mode == "files_with_matches":
        deduped: list[GrepMatch] = []
        seen: set[str] = set()
        for match in matches:
            if match.path in seen:
                continue
            seen.add(match.path)
            deduped.append(match)
        matches = deduped
        truncated = len(deduped) >= head_limit

    return GrepResult(
        matches=matches,
        total_matches=total,
        truncated=truncated,
        mode=normalized_mode,
        engine="python",
    )


def format_glob_result(result: GlobResult) -> str:
    if result.note:
        return f"[file_glob] {result.note}"
    if not result.filenames:
        return "[file_glob] no files matched"
    lines = list(result.filenames)
    if result.truncated:
        lines.append(
            f"... showing first {len(result.filenames)} of {result.total_matches} matches"
            " (narrow the pattern or pass `path` to scope the search)"
        )
    return "\n".join(lines)


def format_grep_result(result: GrepResult) -> str:
    if result.note:
        return f"[file_grep] {result.note}"
    if not result.matches:
        return "[file_grep] no matches found"
    lines: list[str] = []
    if result.mode == "count":
        for match in result.matches:
            lines.append(f"{match.path}:{match.text}")
    elif result.mode == "files_with_matches":
        lines.extend(match.path for match in result.matches)
    else:
        for match in result.matches:
            lines.append(f"{match.path}:{match.line_number}:{match.text}")
    if result.truncated:
        lines.append(
            f"... truncated at the result limit (mode={result.mode});"
            " narrow the pattern, add `glob`, or lower `head_limit`"
        )
    return "\n".join(lines)
