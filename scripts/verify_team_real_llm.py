"""真实 LLM 的 agent team 冒烟：共享任务板 + 多个 teammate 并行。

和 `verify_multiagent_real_llm.py` 的区别：
    那个测的是"一个子 agent 能不能干活"，这个测的是"多个 teammate 能不能
    各自认领任务、并行干活、互不串味"。它覆盖 team_mode + team_task_id +
    task board 这条链路，也就是用户报告出问题的那条。

判据（都是"只存在于本次调用"的哨兵）：
    1. 每个 teammate 只写自己那份文件，且内容含自己的 token；
    2. 每个 teammate 的文件里不许出现别人的 token（防串写）；
    3. 任务板里每个任务都被对应 teammate 认领（owner == /root/<name>）；
    4. 所有 teammate 都跑到 completed。

用法：
    python scripts/verify_team_real_llm.py
    python scripts/verify_team_real_llm.py --teammates 2
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


def _exhaust(gen):
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        result = stop.value
    return result


DEAD_COMPLETION = {"succeeded", "completed", "failed", "cancelled", "canceled", "timeout"}
DEAD_PROCESS = {"exited", "dead", "terminated", "gone"}


def wait_for(handler, task_dirs: dict[str, Path], timeout: float) -> dict[str, dict]:
    """Poll every child's state.json until all reach a terminal state."""
    states: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done = 0
        for name, task_dir in task_dirs.items():
            state_path = task_dir / "state.json"
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            states[name] = state
            completion = str(state.get("completion_status") or "").lower()
            if completion in DEAD_COMPLETION:
                done += 1
        if done == len(task_dirs):
            return states
        time.sleep(2.0)
    return states


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Real-LLM agent team smoke test.")
    parser.add_argument("--teammates", type=int, default=2, help="how many teammates to spawn")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv[1:])

    from genericagent.core.handler import GenericAgentHandler
    from genericagent.agents.manager import SubagentManager

    class _Parent:
        llm_no = 0
        verbose = False
        permission_mode = None
        session_id = "real-team-verify"

    workspace = Path(tempfile.mkdtemp(prefix="ga-real-team-"))
    td = str(workspace)
    keep = os.environ.get("GA_VERIFY_KEEP") == "1"
    try:
        handler = GenericAgentHandler(_Parent(), last_history=[], cwd=str(Path(td) / "temp"))
        handler.subagent_manager = SubagentManager(root_dir=td, python_executable=sys.executable)
        store = handler._get_task_store()

        tokens: dict[str, str] = {}
        task_ids: dict[str, str] = {}
        task_dirs: dict[str, Path] = {}

        for index in range(args.teammates):
            name = f"mate_{index + 1}"
            token = uuid.uuid4().hex
            tokens[name] = token
            task = store.create_task(
                f"write note {index + 1}",
                f"Create the file note_{index + 1}.txt containing only this token: {token}",
            )
            task_ids[name] = task["id"]

            print(f"spawning {name} for task #{task['id']} ...")
            data = _exhaust(handler.do_spawn_agent({
                "task_name": name,
                "message": (
                    f"Create the file note_{index + 1}.txt in your working directory "
                    f"containing only this token: {token}\n"
                    "Do not create any other file, and do not touch other files. "
                    "When done, mark your task completed on the task board."
                ),
                "team_mode": True,
                "team_task_id": task["id"],
            }, None))
            payload = data.data
            if payload.get("status") != "started":
                print(f"✖ spawn rejected: {json.dumps(payload, ensure_ascii=False)[:400]}")
                return 1
            print(f"  started: task_name={payload['task_name']} pid={payload['pid']}")
            task_dirs[name] = Path(payload["task_dir"])

        print("\nwaiting for teammates ...")
        states = wait_for(handler, task_dirs, args.timeout)
        failures: list[str] = []

        # 判据 4：都跑完了
        for name in task_dirs:
            completion = str((states.get(name) or {}).get("completion_status") or "").lower()
            print(f"  {name}: completion={completion or '<none>'}")
            if completion not in {"succeeded", "completed"}:
                failures.append(f"{name} did not complete cleanly ({completion or 'none'})")

        # 判据 1 + 2：各自产出、互不串写
        for index, name in enumerate(task_dirs):
            produced = list(workspace.rglob(f"note_{index + 1}.txt"))
            if not produced:
                failures.append(f"{name} never produced note_{index + 1}.txt")
                continue
            content = produced[0].read_text(encoding="utf-8", errors="replace")
            if tokens[name] not in content:
                failures.append(f"{name}: its own token missing from note_{index + 1}.txt")
            for other, other_token in tokens.items():
                if other != name and other_token in content:
                    failures.append(f"{name}: note_{index + 1}.txt contains {other}'s token (cross-write)")
            print(f"  {name}: note_{index + 1}.txt ✔")

        # 判据 3：任务板归属
        for name, task_id in task_ids.items():
            task = store.get_task(task_id)
            owner = task.get("owner") if isinstance(task, dict) else None
            expected = f"/root/{name}"
            print(f"  task #{task_id} owner={owner!r}")
            if owner != expected:
                failures.append(f"task #{task_id} owner={owner!r}, expected {expected!r}")

        if failures:
            print("\n✖ FAILURES:")
            for item in failures:
                print(f"   - {item}")
            return 1

        print("\nVERDICT: ✔ the team claimed, ran, and produced disjoint outputs")
        return 0
    finally:
        if keep:
            print(f"  workspace kept at {td}")
        else:
            try:
                shutil.rmtree(td, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
