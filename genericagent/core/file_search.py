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
    # GA's own runtime tree: ``.ga/`` holds subagent worktrees/registries and a
    # checkout may keep ``.worktrees/`` copies of whole sources. Searching them
    # returns duplicate hits and buries the real ones -- the reason a model
    # gives up on file_grep and shells out to grep instead.
    ".ga", ".worktrees",
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


def _grep_with_ripgrep(
    root: str,
    pattern: str,
    *,
    glob: str | None,
    ignore_case: bool,
    context: int,
    head_limit: int,
    mode: str,
) -> GrepResult | None:
    """Run the same query through ripgrep, or ``None`` when it cannot be used.

    The Python walk below is correct but scans every file: on the real checkout a
    repo-wide ``files_with_matches`` query took ~19.6s, which is why the model
    gave up on ``file_grep`` and shelled out to ``grep``. ripgrep answers the
    same query in ~100ms, honours ``.gitignore`` and skips binaries, and the
    explicit ``--glob`` negations keep GA's own runtime dirs out of the results.

    Any rg failure (missing binary, unsupported regex syntax, unexpected exit
    code) returns ``None`` so the caller can fall back to the Python scan.
    """
    rg = find_ripgrep()
    if not rg:
        return None
    command = [rg, "--no-messages", "--color", "never", "--line-number", "--no-heading"]
    if ignore_case:
        command.append("--ignore-case")
    if context:
        command += ["--context", str(context)]
    if glob:
        command += ["--glob", glob]
    for ignored in sorted(_COMMON_IGNORE_DIRS):
        command += ["--glob", "!**/" + ignored + "/**"]
    if mode == "files_with_matches":
        command.append("--files-with-matches")
    elif mode == "count":
        command.append("--count-matches")
    command += ["--", pattern, "."]
    try:
        completed = subprocess.run(
            command, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode not in (0, 1):
        return None

    base = Path(root)

    def _absolute(path_text: str) -> str:
        # rg prints a dot-separated relative path with the platform separator;
        # a plain lstrip would leave a leading backslash, which pathlib then
        # treats as drive-root-relative, so join the cleaned relative part.
        normalized = path_text.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return str(base / normalized)

    matches: list[GrepMatch] = []
    total = 0
    truncated = False
    line_pattern = re.compile(r"^(.*?)([:-])(\d+)([:-])(.*)$")
    for raw in completed.stdout.splitlines():
        if not raw:
            continue
        if mode == "files_with_matches":
            total += 1
            if len(matches) >= head_limit:
                truncated = True
                continue
            matches.append(GrepMatch(path=_absolute(raw), line_number=0, text=""))
            continue
        if mode == "count":
            path_text, _, count_text = raw.rpartition(":")
            try:
                count = int(count_text)
            except ValueError:
                continue
            total += count
            if len(matches) >= head_limit:
                truncated = True
                continue
            matches.append(GrepMatch(path=_absolute(path_text), line_number=0, text=str(count)))
            continue
        parsed = line_pattern.match(raw)
        if parsed is None:
            continue
        path_text, separator, line_text, _, content = parsed.groups()
        if separator == ":":
            total += 1
            if total > head_limit:
                truncated = True
                continue
        matches.append(GrepMatch(
            path=_absolute(path_text),
            line_number=int(line_text),
            text=content,
        ))
    return GrepResult(matches=matches, total_matches=total, truncated=truncated,
                      mode=mode, engine="rg")


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


def _glob_to_regex(pattern: str) -> str:
    """Translate a glob into a regex where ``*`` does NOT cross ``/``.

    This is the whole point of not using ``fnmatch``. ``fnmatch`` is a *filename*
    matcher: its ``*`` happily matches ``/``, so ``src/*.py`` also matched
    ``src/sub/c.py`` and a bare ``*`` meant "everything, recursively". A path glob
    has to treat the separator as a hard boundary.

    ``**`` is the globstar token and crosses separators; ``**/`` also matches zero
    directories, so ``src/**/*.py`` finds ``src/a.py`` as well as ``src/sub/c.py``.
    """
    out: list[str] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**", index):
                # `**/` matches zero or more whole directories.
                if pattern.startswith("**/", index):
                    out.append("(?:[^/]+/)*")
                    index += 3
                else:
                    out.append(".*")
                    index += 2
                continue
            out.append("[^/]*")
            index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = index + 1
            if end < length and pattern[end] in "!^":
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape(char))
                index += 1
            else:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                index = end + 1
        else:
            out.append(re.escape(char))
            index += 1
    return "".join(out)


def _path_matches(relative: str, pattern: str) -> bool:
    """Does the root-relative posix path ``relative`` match ``pattern``?

    Two forms are accepted for the same intent, because both are natural to write:

    * ``*.py``      -- no separator anywhere in the pattern. Read as "any file with
      this name, at any depth", which is what every caller means by it and what the
      old implementation did.
    * ``src/*.py``  -- a pattern containing a separator is anchored at the root and
      matched component by component.

    ``**/src/*.py`` and ``src/*.py`` therefore agree, instead of disagreeing as they
    did when ripgrep and the fallback each had their own idea.
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    normalized = pattern.lstrip("./") if pattern.startswith("./") else pattern
    # A leading `**/` is redundant: this matcher is already anchored at the root and
    # a pattern without a separator matches at any depth. Stripping it FIRST is what
    # makes `**/*.md` collapse to the bare-filename case instead of being read as
    # "one or more directories", which used to return nothing for root-level files.
    while normalized.startswith("**/"):
        normalized = normalized[3:]
    if "/" not in normalized.rstrip("/"):
        # Bare filename pattern: match the basename at any depth.
        return re.fullmatch(_glob_to_regex(normalized), relative.rsplit("/", 1)[-1]) is not None
    return re.fullmatch(_glob_to_regex(normalized), relative) is not None


def glob_files(root: str, pattern: str, *, limit: int = GLOB_MAX_FILES) -> GlobResult:
    """Match ``pattern`` under ``root``. Returns root-relative paths, sorted.

    Both engines run the SAME matcher over the same candidate set, so a result never
    depends on whether ripgrep happens to be installed. ripgrep is still used to
    *collect* candidates (it applies .gitignore and skips binaries); the pattern is
    applied here rather than by ``rg --glob`` because rg matches a glob against the
    absolute path when the search root is an argument, which made every
    directory-scoped pattern silently match nothing.
    """

    base = Path(root)
    if not base.is_dir():
        return GlobResult(note=f"not a directory: {root}")
    if not str(pattern or "").strip():
        return GlobResult(note="file_glob requires `pattern`, e.g. **/*.py")

    rg = find_ripgrep()
    candidates: list[str] = []
    engine = "python"
    if rg:
        # Ask rg for every file (no --glob) so it applies ignore rules; the pattern
        # is applied on our side afterwards.
        command = [rg, "--files", "--hidden"]
        for ignored in sorted(_COMMON_IGNORE_DIRS):
            command += ["--glob", f"!**/{ignored}/**"]
        command.append(str(base))
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30, check=False,
            )
            if completed.returncode in (0, 1):
                engine = "rg"
                candidates = [
                    line.strip() for line in (completed.stdout or "").splitlines() if line.strip()
                ]
        except (OSError, subprocess.SubprocessError):
            candidates = []

    if engine != "rg":
        candidates = [str(path) for path in _iter_files(base)]

    matches: list[str] = []
    for raw in candidates:
        try:
            relative = Path(raw).resolve().relative_to(base.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if _path_matches(relative, pattern):
            matches.append(relative)

    normalized = sorted(set(matches))
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

    fast = _grep_with_ripgrep(str(root), str(pattern), glob=glob, ignore_case=ignore_case,
                              context=context, head_limit=head_limit, mode=normalized_mode)
    if fast is not None:
        return fast

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
