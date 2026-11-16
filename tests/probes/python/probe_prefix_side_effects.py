"""Probe: `\\\\?\\` 长路径前缀的副作用边界。

WHY THIS FILE IS KEPT
---------------------
有人会提"给 atomic_write_json 套 `\\\\?\\` 前缀就能突破 MAX_PATH" —— 算术上对，
但前缀会**关掉 Win32 的全部路径规范化**。这个探针把"什么输入会让它找不到路径"
测成一张确定的表，避免下次再凭直觉拍板。

实测结论（本机 Windows，LongPathsEnabled=0）：
  * 前缀不加进文件名 —— 同一路径双向可见（前缀写->普通读、普通写->前缀读）
  * 但以下输入套前缀必定失败：正斜杠 / 相对路径 / 含 `..` / 重复前缀
  * 结尾的点不裁剪 —— **不报错，静默写到另一个文件**（最危险的一种）
  * 长路径下前缀写出的文件，普通方式 exists=False、listdir 抛异常
    => 把"写失败"换成了"读失败"，不是修复

ROOT CAUSE IT GUARDS
--------------------
候选修复方案本身。CC 2.1.319 从不主动加该前缀，并在校验层把它判为可疑形式
（见 docs/20261116-CC319长路径处理与GA对比.md 第 1 节）。

WHY IT IS DETERMINISTIC
-----------------------
纯文件系统行为，不涉及并发或时序；每个用例在独立的临时目录里跑。

HOW TO RUN
----------
    python tests/probes/python/probe_prefix_side_effects.py
"""

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PREFIX = "\\\\?\\"
rows: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    rows.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + "%-52s %s" % (name, detail[:70]))


def main() -> int:
    base = tempfile.mkdtemp(prefix="lp-")
    print("temp base:", base, "(len %d)" % len(base))
    print()

    # 1. 同一文件？—— 决定"前缀会不会导致路径找不到"
    print("=== 1. 前缀与普通路径是否指向同一个文件 ===")

    def same_file() -> str:
        plain = os.path.join(base, "same.txt")
        with open(PREFIX + plain, "w", encoding="utf-8") as fh:
            fh.write("written-via-prefix")
        with open(plain, encoding="utf-8") as fh:
            assert fh.read() == "written-via-prefix"
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write("written-via-plain")
        with open(PREFIX + plain, encoding="utf-8") as fh:
            assert fh.read() == "written-via-plain"
        return "双向可见，是同一个文件（前缀不进文件名）"

    try:
        record("前缀与普通路径指向同一个文件", True, same_file())
    except Exception as exc:  # noqa: BLE001
        record("前缀与普通路径指向同一个文件", False, "%s: %s" % (type(exc).__name__, exc))
    print()

    # 2. 危险输入清单 —— 实现里必须挡掉的分支
    print("=== 2. 套前缀后必定失败的输入（实现必须挡掉）===")

    def fwd_slash():
        with open(PREFIX + os.path.join(base, "f.txt").replace("\\", "/"), "w") as fh:
            fh.write("x")

    def relative():
        cwd = os.getcwd()
        os.chdir(base)
        try:
            with open(PREFIX + "rel.txt", "w") as fh:
                fh.write("x")
        finally:
            os.chdir(cwd)

    def dotdot():
        with open(PREFIX + os.path.abspath(base) + "\\..\\up.txt", "w") as fh:
            fh.write("x")

    def double_prefix():
        with open(PREFIX + PREFIX + os.path.join(base, "dd.txt"), "w") as fh:
            fh.write("x")

    for label, fn in (
        ("含正斜杠", fwd_slash),
        ("相对路径", relative),
        ("含 ..", dotdot),
        ("重复加前缀", double_prefix),
    ):
        try:
            fn()
            record("前缀 + " + label, False, "竟然成功（本机行为，仍不该依赖）")
        except OSError as exc:
            record("前缀 + " + label, True, "%s（确认会找不到）" % type(exc).__name__)
    print()

    # 3. 最危险的一种：不报错但落错文件
    print("=== 3. 结尾的点：不报错，静默落到另一个文件 ===")
    with open(os.path.join(base, "trail."), "w", encoding="utf-8") as fh:
        fh.write("via-plain")
    with open(PREFIX + os.path.join(base, "trail2."), "w", encoding="utf-8") as fh:
        fh.write("via-prefix")
    plain_exists = Path(os.path.join(base, "trail")).exists()
    stripped_exists = Path(os.path.join(base, "trail2")).exists()
    exact_exists = Path(PREFIX + os.path.join(base, "trail2.")).exists()
    record(
        "普通写 'trail.' 被裁剪为 'trail'",
        plain_exists,
        "trail exists=%s" % plain_exists,
    )
    record(
        "前缀写 'trail2.' 未被裁剪（落在不同文件）",
        (not stripped_exists) and exact_exists,
        "'trail2'=%s 'trail2.'=%s" % (stripped_exists, exact_exists),
    )
    print()

    # 4. 读侧代价：前缀写出的长路径文件，普通方式看不见
    print("=== 4. 前缀写的长路径文件，普通访问是否可用 ===")
    long_dir = os.path.join(base, "d" * 120, "data", "projects", "p" * 40,
                            "session_" + "a" * 32, "workflows", "wf_" + "b" * 32)
    os.makedirs(PREFIX + long_dir, exist_ok=True)
    target = os.path.join(long_dir, "final-result.json")
    with open(PREFIX + target, "w", encoding="utf-8") as fh:
        fh.write("x")
    name_count = len(os.listdir(PREFIX + long_dir))
    record("前缀 listdir 只看到 1 个文件（没有副本）", name_count == 1, str(name_count))
    ordinary_visible = os.path.exists(target)
    record(
        "但普通 exists 看不见它",
        not ordinary_visible,
        "exists=%s => 下游未加前缀的读取会失败" % ordinary_visible,
    )
    print()

    shutil.rmtree(PREFIX + base, ignore_errors=True)
    shutil.rmtree(base, ignore_errors=True)

    failed = [r for r in rows if not r[1]]
    print("VERDICT: %s (%d checks, %d failed)"
          % ("OK" if not failed else "FAIL", len(rows), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
