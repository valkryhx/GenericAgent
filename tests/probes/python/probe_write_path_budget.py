"""Probe: GA 的原子写在 Windows 上的路径预算临界点。

WHY THIS FILE IS KEPT
---------------------
`atomic_write_json`（genericagent/agents/state.py:76-83）把临时文件写成
`.<name>.<pid>.<tid>.tmp`，凭空多出约 18 字符。目标路径本身合法（< 260）时，
临时名却可能越过 Windows MAX_PATH，`open()` 抛 FileNotFoundError，而
`workflow_draft` 的 `except Exception`（frontends/ink/bridge.py:1601）把它吞成
空 runId —— 使用者看到的是"workflow 不启动，没有任何报错"。

这个探针用**真实的** WorkflowStore.create_run 扫出精确临界点，让"是不是 bug"
变成一个数字，而不是推理。结论与 CC 2.1.319 的做法对比见
docs/20261116-CC319长路径处理与GA对比.md。

ROOT CAUSE IT GUARDS
--------------------
genericagent/agents/state.py:79
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")

WHY IT IS DETERMINISTIC
-----------------------
不跑 LLM、不联网。每轮在一个新建的临时目录里造出确定长度的 workspace，
path 长度是算术而非采样结果，同样的入参必然给出同样的 PASS/FAIL。

HOW TO RUN
----------
    python tests/probes/python/probe_write_path_budget.py

脚本自己打印结论（临界点 + 是否印证"目标合法、临时名越界"）。
退出码非 0 表示预期行为变了（例如有人修好了临时名长度），需要重新评估这份结论。
"""

import io
import os
import shutil
import sys
import tempfile
import threading

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from genericagent.workflow.models import WorkflowRun
from genericagent.workflow.store import WorkflowStore

MAX_PATH = 260


def real_tmp_name(run_dir: str) -> str:
    """按 atomic_write_json 的真实公式算临时文件名。

    不能硬编码位数：`.{name}.{pid}.{tid}.tmp` 的长度随 pid/tid 的十进制位数变化，
    早先手写 4 位 tid 导致算出的长度比真实值少 1，把"越界"误判成"未越界"。
    """
    return os.path.join(
        run_dir, f".final-result.json.{os.getpid()}.{threading.get_ident()}.tmp"
    )


def sweep() -> list[tuple[int, int, int, bool, str]]:
    rows = []
    for pad in (0, 20, 40, 50, 60, 63, 65, 68, 70, 75, 90):
        base = tempfile.mkdtemp(prefix="ga-pathprobe-")
        workspace = os.path.join(base, "w" * pad) if pad else base
        os.makedirs(workspace, exist_ok=True)
        store = WorkflowStore(root=os.path.join(workspace, "data", "projects"))
        run = WorkflowRun(run_id="wf_" + "b" * 32, session_id="session_" + "a" * 32, script="x")
        ok, err = True, ""
        try:
            store.create_run(run)
        except OSError as exc:
            ok, err = False, "%s: %s" % (type(exc).__name__, str(exc)[:40])
        run_dir = run.artifact_dir or ""
        tmp_len = len(real_tmp_name(run_dir)) if run_dir else 0
        rows.append((len(workspace), len(run_dir), tmp_len, ok, err))
        shutil.rmtree(base, ignore_errors=True)
    return rows


def main() -> int:
    print("用真实 WorkflowStore.create_run 扫描：workspace 多长时开始失败")
    print("(create_run 写 run.json / state.json / script.js / final-result.json / journal.jsonl)")
    print()
    print("%-8s %-10s %-10s %s" % ("ws长度", "run目录", "tmp名长", "结果"))
    print("-" * 66)

    rows = sweep()
    for ws_len, run_len, tmp_len, ok, err in rows:
        print("%-8d %-10d %-10d %s" % (ws_len, run_len, tmp_len, "OK" if ok else "FAIL " + err))

    print()
    first_fail = next((r for r in rows if not r[3]), None)
    last_ok = next((r for r in reversed(rows) if r[3]), None)
    if not first_fail:
        print("VERDICT: ✖ 没有观察到失败 —— 临时名长度可能已被修好，请重新评估本文档结论")
        return 1

    print("临界点：workspace %d 字符仍 OK，%d 字符开始失败" % (last_ok[0], first_fail[0]))
    print("对应 run 目录 %d..%d 字符，tmp 名 %d..%d 字符"
          % (last_ok[1], first_fail[1], last_ok[2], first_fail[2]))
    print()
    # 核心断言：失败时目标路径合法，是临时名越界
    target_len = first_fail[1] + 1 + len("final-result.json")
    print("失败现场：run 目录 %d 字符，目标 final-result.json %d 字符，tmp 名 %d 字符"
          % (first_fail[1], target_len, first_fail[2]))
    print("  MAX_PATH = %d" % MAX_PATH)

    target_legal = target_len < MAX_PATH
    tmp_illegal = first_fail[2] >= MAX_PATH
    print("  目标合法 (<260): %s" % ("✔" if target_legal else "✖"))
    print("  临时名越界 (>=260): %s" % ("✔" if tmp_illegal else "✖"))
    if target_legal and tmp_illegal:
        print()
        print("VERDICT: ✔ 缺陷成立 —— 目标路径合法，是原子写的临时名后缀把它推过 MAX_PATH")
        return 0
    print()
    print("VERDICT: ✖ 预期不成立，请重新分析")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
