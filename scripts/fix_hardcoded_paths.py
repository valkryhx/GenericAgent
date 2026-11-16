# -*- coding: utf-8 -*-
"""把仓库里写死的 `D:\\git_repos\\GenericAgent` 换成「相对本文件解析仓库根」。

## 为什么需要它

用户在别的机器 / 别的目录更新代码时，这 65 个探针与辅助脚本全部跑不起来：它们把
仓库根写成了绝对路径 `D:\\git_repos\\GenericAgent`，而真实仓库在 `D:\\git_codes\\`，
换机器更是完全不同。仓库里已有正确写法可抄（`bridge_blocking_probe.py`：
`ROOT = Path(__file__).resolve().parents[3]`）。

## 替换规则

对每个命中文件，按「文件所在目录相对仓库根的层数 N」注入两行：

    REPO = str(Path(__file__).resolve().parents[N])
    REPO_POSIX = "/" + Path(REPO).as_posix()[0].lower() + Path(REPO).as_posix()[2:]

然后：

| 原写法 | 新写法 |
| --- | --- |
| `r"D:\\git_repos\\GenericAgent\\a\\b"` | `str(Path(REPO) / "a" / "b")` |
| `Path(r"D:\\git_repos\\GenericAgent\\a\\b")` | `Path(REPO) / "a" / "b"` |
| `r"D:\\git_repos\\GenericAgent"` | `REPO` |
| `f"cd {REPO_POSIX} && ./ga"` | `f"cd {REPO_POSIX} && ./ga"` |

`REPO_POSIX` 是给 `bash -lc` 用的：Git Bash 认 POSIX 形式（`D:\\a\\b` → `/d/a/b`），
直接塞 Windows 路径会因为反斜杠被当成转义而失败。

## 为什么 ROOT 用 str 而不是 Path

多处把它直接赋给 `env["PYTHONPATH"]`（必须是 str）；需要 Path 运算的地方显式
`Path(REPO)`。反过来（REPO 是 Path）会让 `env[...] = REPO` 在运行时抛 TypeError。

## 为什么注入点取「最后一个顶层 import 之后」

用 AST 定位，而不是正则猜行号：文件里既有模块 docstring 也有条件 import，
正则很容易插进 docstring 内部或 `if TYPE_CHECKING:` 块里。

## 不处理的情况（刻意）

- `rewrite_doc2.py` 里描述**另一个仓库**（`D:\\git_repos\\claude_hack`）的文档正文：
  那是内容不是路径，改了就是伪造。
- 只出现在标识符里的 `git_repository` 之类：子串命中，不是路径。

用法：
    python scripts/fix_hardcoded_paths.py            # 预演（打印将要改的内容）
    python scripts/fix_hardcoded_paths.py --apply    # 落地
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO = str(Path(__file__).resolve().parents[1])
REPO_POSIX = "/" + Path(REPO).as_posix()[0].lower() + Path(REPO).as_posix()[2:]

REPO_MARK = REPO
SKIP_PARTS = {"node_modules", "temp", "build", "dist", "docs", ".git", "__pycache__"}
SKIP_FILES = {
    # 文档正文里描述另一个仓库的路径，改了就是伪造历史记录。
    "tests/probes/python/rewrite_doc2.py",
}


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def candidates(root: pathlib.Path) -> list[pathlib.Path]:
    out = []
    for path in root.rglob("*"):
        if path.suffix not in {".py", ".ts", ".tsx", ".ps1", ".mjs", ".js"}:
            continue
        rel = path.relative_to(root)
        if SKIP_PARTS & set(rel.parts):
            continue
        if rel.as_posix() in SKIP_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # 只认「引号里出现仓库绝对路径」，排除 git_repository 这类标识符命中。
        if "git_repos" not in text:
            continue
        if "\\git_repos\\GenericAgent" not in text and "/git_repos/GenericAgent" not in text:
            continue
        out.append(path)
    return sorted(out)


def depth(path: pathlib.Path, root: pathlib.Path) -> int:
    """文件所在目录相对仓库根的层数（= Path(__file__).resolve().parents[N] 的 N）。"""
    return len(path.relative_to(root).parts) - 1


def segments(rest: str) -> str:
    """`\\frontends\\ink\\ui` → ` / "frontends" / "ink" / "ui"`。"""
    parts = [p for p in re.split(r"[\\/]+", rest) if p]
    return "".join(f' / "{p}"' for p in parts)


# 顺序要紧：先吃「Path(r"...")」整体，再吃裸字符串。否则先替换内部会产出
# `Path(str(Path(REPO) / ...))` 这种多一层括号的套娃（实测会留下不配对的 `))`）。
PATH_WRAPPED = re.compile(r"""Path\(\s*r?["']D:\\git_repos\\GenericAgent((?:\\[^"'\s)]+)*)["']\s*\)""")
WINDOWS_PATH = re.compile(r"""r?["']D:\\git_repos\\GenericAgent((?:\\[^"'\s)]+)*)["']""")
BASH_CD = re.compile(r"""["']cd /d/git_repos/GenericAgent""")
ROOT_STRING = re.compile(r"""^ROOT = str\(Path\(REPO\)\)$""", re.MULTILINE)
# 文件自己已经声明了 REPO/ROOT 这类名字时，注入变量必须换名，否则变成
# `REPO = Path(REPO)` 这种自我引用（实测会 NameError）。
OWN_REPO = re.compile(r"^(REPO|ROOT|ROOT_DIR|REPO_DIR)\s*=", re.MULTILINE)


def pick_names(text: str) -> tuple[str, str]:
    """挑一组不与文件已有变量冲突的注入名：(仓库根变量名, POSIX 变量名)。"""
    used = {m.group(1) for m in OWN_REPO.finditer(text)}
    base = "REPO"
    if base not in used:
        return "REPO", "REPO_POSIX"
    base = "GA_ROOT"
    return base, f"{base}_POSIX"


def inject_point(text: str) -> int:
    """返回注入行号（0-based）：**开头连续 import 段**的结尾。

    不能用「文件里最后一个 import」——探针把 import 分散在正文里是常事
    （`table_raw.py:83` 就是一个），那样注入点会落到文件中间、甚至切进一个
    多行表达式。这里只吃开头的 import 段：模块 docstring（若有）之后，
    连续出现的 `import` / `from ... import` 全部算在内。
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 0
    body = list(tree.body)
    # 跳过开头的模块 docstring
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    last = 0
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = max(last, node.end_lineno or 0)
            continue
        break
    return last


def transform(path: pathlib.Path, root: pathlib.Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    original = text
    n = depth(path, root)
    # 名字要在替换前定：替换后文件里一定出现 `REPO`，那时再也分不清是不是它自己的。
    root_name, posix_name = pick_names(text)

    # 1) `Path(r"...\a\b")` → `Path(REPO) / "a" / "b"`（根目录则是 `Path(REPO)`）
    def repl_path(match: re.Match[str]) -> str:
        tail = segments(match.group(1) or "")
        return f"Path({root_name}){tail}" if tail else f"Path({root_name})"

    text = PATH_WRAPPED.sub(repl_path, text)

    # 2) 裸字符串 → `str(Path(REPO) / ...)`；恰好是仓库根时直接 `REPO`。
    def repl_windows(match: re.Match[str]) -> str:
        tail = segments(match.group(1) or "")
        return f"str(Path({root_name}){tail})" if tail else root_name

    text = WINDOWS_PATH.sub(repl_windows, text)

    if BASH_CD.search(text):
        text = BASH_CD.sub(f'f"cd {{{posix_name}}}', text)

    # `ROOT = str(Path(REPO))` 与 `ROOT = REPO` 等价，取后者。
    text = ROOT_STRING.sub(f"ROOT = {root_name}", text)

    if text == original:
        return original, original

    lines = text.split("\n")
    at = inject_point(text)
    block = [f"{root_name} = str(Path(__file__).resolve().parents[{n}])"]
    if posix_name in text:
        block.append(
            f'{posix_name} = "/" + Path({root_name}).as_posix()[0].lower() '
            f"+ Path({root_name}).as_posix()[2:]"
        )
    if "from pathlib import Path" not in text and "import pathlib" not in text:
        block.insert(0, "from pathlib import Path")
    # 只在这一行之后本来就不是空行时补一个分隔空行，避免出现两个连续空行。
    if at < len(lines) and lines[at].strip():
        block.append("")
        # 再补一行：`lines[at]` 若是「紧贴着的第三方 import」，注入块前面也需要呼吸。
        if at > 0 and lines[at - 1].strip():
            block.insert(0, "")

    lines[at:at] = block
    return original, "\n".join(lines)


def main() -> int:
    apply = "--apply" in sys.argv
    root = repo_root()
    files = candidates(root)
    print(f"仓库根: {root}\n命中文件: {len(files)}\n")
    changed = 0
    for path in files:
        before, after = transform(path, root)
        if before == after:
            continue
        changed += 1
        if apply:
            path.write_text(after, encoding="utf-8", newline="\n")
        print(f"  {'改' if apply else '将改'} {path.relative_to(root)}")
    print(f"\n{'已改' if apply else '将改'} {changed} 个文件")
    if not apply:
        print("\n（预演。加 --apply 落地。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())