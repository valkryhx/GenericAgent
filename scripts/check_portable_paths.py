# -*- coding: utf-8 -*-
"""校验：仓库里每个探针/脚本解析出的「仓库根」是否真的指向当前 checkout。

## 为什么需要它

2026-11-16 之前有 65 个文件把仓库根写死成 `D:\\git_repos\\GenericAgent`。那在
原作者的机器上能跑，在 `D:\\git_codes\\GenericAgent` 或任何别的机器上都直接
FileNotFoundError。迁移成 `Path(__file__).resolve().parents[N]` 之后，**层级数 N
是否算对**成了新的失败面：N 少 1 就指到父目录，多 1 就指到子目录，而语法检查、
import 检查都发现不了——只有真的解析一次才知道。

## 判据

对每个含 `REPO/GA_ROOT = str(Path(__file__).resolve().parents[N])` 的文件：

    Path(该文件).resolve().parents[N] 必须 == 仓库根

并额外报告：文件里是否还有 `git_repos` 残留（除刻意的例外）。

## 为什么必须保留

它是「跨机器 / 跨目录可移植」这条性质的唯一机器判据。换机器时先跑它，
比逐个探针试跑快得多。

用法：
    python scripts/check_portable_paths.py
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PATTERN = re.compile(r"^(?:REPO|GA_ROOT) = str\(Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]\)$",
                     re.MULTILINE)
SKIP_PARTS = {"node_modules", "temp", "build", "dist", ".git", "__pycache__"}
# 刻意保留绝对路径的文件：
#  - fix_hardcoded_paths.py 自身要匹配旧路径才能做迁移
#  - check_portable_paths.py 自身要在文档里描述旧路径
#  - rewrite_doc2.py 里的 claude_hack 路径是文档正文，不是本仓库路径
ALLOW_ABS = {
    "scripts/fix_hardcoded_paths.py",
    "scripts/check_portable_paths.py",
    "tests/probes/python/rewrite_doc2.py",
}
# `git_repository` / `git_repositories` 之类的标识符，不是路径。
NOT_A_PATH = re.compile(r"git_repos(?!\\|/)")


def has_hardcoded_path(text: str) -> bool:
    return "\\git_repos\\GenericAgent" in text or "/git_repos/GenericAgent" in text


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    files = [p for p in root.rglob("*.py")
             if not (SKIP_PARTS & set(p.relative_to(root).parts))]

    checked = ok = 0
    wrong: list[tuple[str, int, str]] = []
    leftover: list[str] = []
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if rel not in ALLOW_ABS and has_hardcoded_path(text):
            leftover.append(rel)
        for match in PATTERN.finditer(text):
            checked += 1
            n = int(match.group(1))
            resolved = path.resolve().parents[n]
            if resolved == root:
                ok += 1
            else:
                wrong.append((rel, n, str(resolved)))
            break  # 每个文件看第一处即可，注入块只会有一处

    print(f"仓库根: {root}")
    print(f"含 parents[N] 的文件: {checked}   解析正确: {ok}   解析错误: {len(wrong)}")
    for rel, n, got in wrong:
        print(f"  ✖ {rel}  parents[{n}] → {got}")
    if leftover:
        print(f"\n仍有 git_repos 残留的文件（{len(leftover)}）:")
        for rel in leftover:
            print(f"  - {rel}")

    ok_all = not wrong and not leftover
    print("\n" + ("✔ 全部可移植：任何目录/机器下都能定位到仓库根" if ok_all
                  else "✖ 存在不可移植的文件"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())