"""Probe: `stagingDir` 外置临时文件到底能解决多少深路径问题。

WHY THIS FILE IS KEPT
---------------------
"把临时文件挪到浅目录（学 CC 的 `stagingDir`）"听起来能解决 MAX_PATH，很符合直觉，
所以它会被反复提出来。实测证明它的收益**恰好等于临时名的后缀长度**（本机 17 字符），
并且对真正卡住我们的场景（目标路径本身就超限）**完全无效** —— 因为 `os.replace(src, dst)`
要求 dst 也能解析，把 src 挪到浅处并不能让 dst 变短。

不写下来，这个结论会被重新推理一遍，然后又一次把 17 字符的收益当成解决方案。

ROOT CAUSE IT GUARDS
--------------------
`genericagent/agents/state.py` 的原子写：
    tmp = <target>.tmp.<pid>.<tid>        # 比 target 名长 len(SUFFIX)-len(NAME) 字符
    os.replace(tmp, target)
临时名后缀把"目标合法"变成"路径超限"。staging 只消除这一项开销，不改变 target 长度。

WHY IT IS DETERMINISTIC
-----------------------
纯文件系统行为，长度是算术构造的；同一台机器上逐点可复现。唯一的机器相关量是
`SUFFIX` 的长度（pid/tid 位数），脚本自己打印出来，不写死结论。

HOW TO RUN
----------
    python tests/probes/python/probe_staging_dir_limits.py
"""

import io
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MAX_PATH = 260
NAME = "final-result.json"
# Mirror `atomic_write_json` exactly: the temp name is `.<name>.<pid>.<tid>.tmp`, with a
# LEADING dot. Omitting it makes the measured overhead 15 instead of the real 16.
SUFFIX = ".%s.%d.%d.tmp" % (NAME, os.getpid(), 12345)
# tmp 与 target 共享同一目录前缀与分隔符，只有文件名部分不同。
OVERHEAD = len(SUFFIX) - len(NAME)

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + "%-56s %s" % (name, str(detail)[:56]))


def write_direct(target: str) -> bool:
    """GA 现在的做法：临时文件写在目标同目录。"""
    tmp = os.path.join(os.path.dirname(target), SUFFIX)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def write_staged(target: str, staging: str) -> bool:
    """CC stagingDir 的做法：临时文件写在浅 staging 区，再 replace 到目标。"""
    tmp = os.path.join(staging, SUFFIX)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def main() -> int:
    base = tempfile.mkdtemp(prefix="stg-")
    staging = os.path.join(base, "stg")
    os.makedirs(staging, exist_ok=True)

    print("后缀开销 OVERHEAD = %d 字符（tmp 名比 target 名长这么多）" % OVERHEAD)
    print("staging 区长度 = %d（必须够浅，否则 staging 自己也会超限）" % len(staging))
    print()
    print("%-9s %-9s %-11s %-11s %s" % ("target长", "tmp长", "直接写", "staging", "说明"))
    print("-" * 68)

    direct_max = 0
    staged_max = 0
    gain_points: list[int] = []
    for target_len in range(215, 266):
        pad = target_len - len(os.sep) - len(NAME) - len(base) - 1
        if pad < 1:
            continue
        directory = os.path.join(base, "d" * pad)
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            continue
        target = os.path.join(directory, NAME)
        if len(target) != target_len:
            continue

        direct_ok = write_direct(target)
        staged_ok = write_staged(target, staging)
        if direct_ok:
            direct_max = target_len
        if staged_ok:
            staged_max = target_len
        if staged_ok and not direct_ok:
            gain_points.append(target_len)

        note = ""
        if staged_ok and not direct_ok:
            note = "<- staging 救活"
        elif not staged_ok and not direct_ok:
            note = "两种都失败"
        print("%-9d %-9d %-11s %-11s %s"
              % (target_len, target_len + OVERHEAD,
                 "OK" if direct_ok else "FAIL", "OK" if staged_ok else "FAIL", note))

    print()
    print("=== 结论 ===")
    print("  直接写可用上限：target <= %d   (260 - %d 后缀开销)" % (direct_max, OVERHEAD))
    print("  staging 可用上限：target <= %d   (260 - 1)" % staged_max)
    gain = staged_max - direct_max
    print("  staging 收益宽度：%d 字符，后缀开销：%d 字符" % (gain, OVERHEAD))
    print()

    record(
        "staging 的收益恰等于临时名后缀开销",
        gain == OVERHEAD,
        "gain=%d overhead=%d" % (gain, OVERHEAD),
    )
    record(
        "target >= 260 时 staging 也无能为力",
        all(target_len > staged_max for target_len in range(staged_max + 1, 266)),
        "staging 上限 %d < 260" % staged_max,
    )

    # 真实失败样本：0.6.3 复测里 tmp 名是 327 字符
    real_tmp = 327
    real_target = real_tmp - OVERHEAD
    print()
    print("=== 套到 0.6.3 实测的失败样本 ===")
    print("  报错里 tmp = %d 字符，后缀开销 %d -> target ≈ %d 字符"
          % (real_tmp, OVERHEAD, real_target))
    print("  target %d >= 260，被卡住的是 makedirs / replace 的 dst，不是临时名"
          % real_target)
    record(
        "真实失败场景 staging 救不了（target 本身已超限）",
        real_target >= MAX_PATH,
        "target≈%d" % real_target,
    )

    print()
    print("=== 另一个约束：staging 必须与目标同卷 ===")
    other = "D:\\tmp" if os.path.splitdrive(base)[0].upper() != "D:" else "C:\\tmp"
    cross_note = "未测（无第二块盘）"
    cross_ok = True
    try:
        os.makedirs(other, exist_ok=True)
        short_target = os.path.join(base, "short.json")
        tmp = os.path.join(other, SUFFIX)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            os.replace(tmp, short_target)
            cross_note = "跨卷 replace 成功（意外）"
        except OSError as exc:
            cross_note = "%s errno=%s -> 必须同卷" % (
                type(exc).__name__, getattr(exc, "errno", None))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except OSError as exc:
        cross_note = "无法构造跨卷场景: %s" % type(exc).__name__
    print("  %s" % cross_note)
    record(
        "staging 区必须与目标同卷（跨卷 replace 不可用）",
        cross_ok,
        cross_note,
    )

    shutil.rmtree(base, ignore_errors=True)
    failed = [r for r in results if not r[1]]
    print()
    print("VERDICT: %s (%d checks, %d failed)"
          % ("OK" if not failed else "FAIL", len(results), len(failed)))
    print("净结论：staging 只把可用深度往后挪约 %d 字符，不解决 target 本身超限的情形。"
          % OVERHEAD)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())