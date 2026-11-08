"""Real-child probe: does a spawned subagent actually get working tools?

Spawns a real OS child with a real LLM profile and a task that CANNOT be answered
without a tool (it must read a file whose content it cannot guess, and run a command).
A child with a stripped tool surface answers "I have no tools" -- which is exactly the
incident being verified. Prints the child's output and the durable state it wrote.
"""

from __future__ import annotations

import io
import contextlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

MARKER = "GA_SURFACE_PROBE_20261108_7K3Q"


def resolve_profile() -> int:
    from genericagent.llm.client import load_clients_from_yaml

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        clients, active, *_ = load_clients_from_yaml(start_dir=REPO)
    return int(active or 0)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def main() -> int:
    from genericagent.agents.manager import SubagentManager

    seed = Path("D:/tmp") / "ga_surface_probe_input.txt"
    seed.parent.mkdir(parents=True, exist_ok=True)
    seed.write_text(f"seed-line {MARKER}\n", encoding="utf-8", newline="\n")

    llm_no = resolve_profile()
    manager = SubagentManager(root_dir=REPO, python_executable=sys.executable)
    task = "ga_surface_probe"
    handle = manager.spawn_agent(
        task,
        "你必须用工具完成，不许凭猜测作答：\n"
        f"1) 用 file_read 读取文件 {seed.as_posix()}，把它的完整内容原样贴出来。\n"
        "2) 用 code_run 执行 `echo CODE_RUN_OK`，把输出原样贴出来。\n"
        "3) 最后单独一行输出 DONE。\n"
        "如果你没有这些工具，就明确说明你有哪些工具。",
        llm_no=llm_no,
        verbose=False,
        parent_session_id="ga_surface_probe_session",
        parent_permission_mode="full_access",
        ipc_mode="socket",
    )
    task_dir = Path(handle.task_dir)
    print("task_dir:", task_dir)

    deadline = time.time() + 300
    while time.time() < deadline:
        state = json.loads((task_dir / "state.json").read_text(encoding="utf-8", errors="replace"))
        if state.get("process_status") in {"shutdown", "failed", "exited"}:
            break
        if state.get("turn_status") == "waiting_reply":
            break
        time.sleep(2)

    state = json.loads((task_dir / "state.json").read_text(encoding="utf-8", errors="replace"))
    print("process_status:", state.get("process_status"), "turn_status:", state.get("turn_status"))
    print("capability_profile:", state.get("capability_profile"), state.get("capability_options"))

    reports = []
    for path in sorted(task_dir.glob("output*.txt")):
        body = path.read_text(encoding="utf-8", errors="replace")
        reports.append({"file": path.name, "chars": len(body)})
        print(f"\n--- {path.name} ---\n{body}")

    tool_marks = {
        "has_seed_marker": any(MARKER in p.read_text(encoding="utf-8", errors="replace")
                               for p in task_dir.glob("output*.txt")),
        "has_code_run_ok": any("CODE_RUN_OK" in p.read_text(encoding="utf-8", errors="replace")
                               for p in task_dir.glob("output*.txt")),
    }
    print("\n== verdict ==")
    print(json.dumps(tool_marks, indent=2))
    print("event types:", [r.get("type") for r in read_jsonl(task_dir / "events.jsonl")])

    with contextlib.suppress(Exception):
        manager.close_agent(handle.task_name, reason="surface_probe_done", grace_s=1.0)
    return 0 if all(tool_marks.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
