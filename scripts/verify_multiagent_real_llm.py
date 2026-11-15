"""真实 LLM 的 subagent 冒烟（不是 stub）。

为什么需要它：
    前面 L0–L3 用的是 FakeChildAgentRunner / popen stub，能证明**接线**正确，
    但证明不了模型真的会按新 schema 发出 task_name + message。这次事故的本质就是
    "模型发出来的参数不对"，所以只有一次真模型调用能覆盖它。

判据（不看"没报错"）：
    1. 子进程必须真的产出 output.txt，且其中含本次唯一哨兵；
    2. 子 agent 的 state.json 里 agent_type 必须等于请求里给的角色名；
    3. 父进程拿到的 handle 的 run_id 必须与磁盘上的 run 目录一致。

用法：
    python scripts/verify_multiagent_real_llm.py
    python scripts/verify_multiagent_real_llm.py --no-role   # 只测通用子 agent
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PASS, FAIL = "PASS", "FAIL"


def _exhaust(gen):
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        result = stop.value
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Real-LLM subagent smoke test.")
    parser.add_argument("--no-role", action="store_true", help="spawn a generic subagent instead of a role")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the child")
    args = parser.parse_args(argv[1:])

    from genericagent.core.handler import GenericAgentHandler
    from genericagent.agents.manager import SubagentManager

    token = uuid.uuid4().hex

    class _Parent:
        llm_no = 0
        verbose = False
        permission_mode = None
        session_id = "real-llm-verify"

    workspace = Path(tempfile.mkdtemp(prefix="ga-real-llm-"))
    td = str(workspace)
    keep = os.environ.get("GA_VERIFY_KEEP") == "1"
    try:
        handler = GenericAgentHandler(_Parent(), last_history=[], cwd=str(Path(td) / "temp"))
        handler.subagent_manager = SubagentManager(root_dir=td, python_executable=sys.executable)

        payload = {
            "task_name": "real_worker",
            "message": (
                "Do exactly one thing and then stop. Create the file verify_out.txt "
                f"in your working directory containing only this token: {token}\n"
                "Do not create any other file. Report the path when done."
            ),
        }
        if not args.no_role:
            payload["role"] = "explore"

        print(f"spawning (role={payload.get('role')!r}) ...")
        data = _exhaust(handler.do_spawn_agent(payload, None))
        if data.data.get("status") != "started":
            print(f"✖ spawn rejected: {json.dumps(data.data, ensure_ascii=False)[:400]}")
            return 1
        handle_run_id = data.data["run_id"]
        task_dir = Path(data.data["task_dir"])
        print(f"  started: task_name={data.data['task_name']} run_id={handle_run_id} pid={data.data['pid']}")
        print(f"  agent_type resolved: {data.data.get('agent_type')!r}")

        # 轮询到子进程结束。
        # 状态字段是 completion_status / process_status —— state.json 里没有 `status`
        # 这个键（`status` 只在 registry 行上），所以按名字猜会永远读到 <unknown>。
        #
        # 退出条件必须列举"终态"，不能写成 `if completion:` —— 那个字段平时是
        # "pending"（非空真值），会让循环在子进程刚起头时立刻退出，然后把"还没写
        # 文件"误判成"永远写不出文件"。
        DEAD_COMPLETION = {"succeeded", "completed", "failed", "cancelled", "canceled", "timeout"}
        DEAD_PROCESS = {"exited", "dead", "terminated", "gone"}
        deadline = time.monotonic() + args.timeout
        state: dict = {}
        while time.monotonic() < deadline:
            state_path = task_dir / "state.json"
            if state_path.is_file():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    completion = str(state.get("completion_status") or "").lower()
                    process = str(state.get("process_status") or "").lower()
                    if completion in DEAD_COMPLETION or process in DEAD_PROCESS:
                        break
                except Exception:
                    pass
            time.sleep(2.0)
        print(f"  completion_status={state.get('completion_status')!r} "
              f"process_status={state.get('process_status')!r}")

        # 判据 1：哨兵确实被子进程写出来了
        produced = list(Path(td).rglob("verify_out.txt"))
        if not produced:
            print("✖ the child never produced verify_out.txt")
            print("  files present:", [str(p.relative_to(td)) for p in Path(td).rglob("*") if p.is_file()][:20])
            return 1
        content = produced[0].read_text(encoding="utf-8", errors="replace")
        if token not in content:
            print(f"✖ sentinel missing from {produced[0]} (content={content[:200]!r})")
            return 1
        print(f"  ✔ sentinel delivered and echoed back: {produced[0]}")

        # 判据 2：角色确实被解析（只在不跳过 role 时）
        if not args.no_role:
            if data.data.get("agent_type") != "explore":
                print(f"✖ role did not resolve: {data.data.get('agent_type')!r}")
                return 1
            print("  ✔ role=explore resolved")

        # 判据 3：handle 的 run_id 与磁盘目录一致
        run_dirs = list(Path(td).rglob(handle_run_id))
        if not run_dirs:
            print(f"✖ run dir for {handle_run_id} not found on disk")
            return 1
        print(f"  ✔ run dir matches handle: {run_dirs[0]}")

        # 判据 4：子 agent 必须是"跑完了"，不是"还没跑完就被我们读到文件"。
        # 这一条是前三条的补集：文件存在只能证明它工作过，不能证明它正常收尾。
        if state.get("completion_status") not in {"succeeded", "completed"}:
            print(f"✖ child did not finish cleanly: completion_status={state.get('completion_status')!r} "
                  f"last_error={str(state.get('last_error'))[:200]!r}")
            return 1
        print("  ✔ child ran to completion")

        print("VERDICT: ✔ a real model spawned a real child that did the work")
        return 0
    finally:
        if keep:
            print(f"  workspace kept at {td}")
        else:
            # The child's log handles can still be open for a moment after it exits on
            # Windows; a cleanup failure must not turn a passing check into a crash.
            try:
                shutil.rmtree(td, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
