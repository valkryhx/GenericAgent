"""端到端验证：父进程选官方 DS 后，子 agent 是否自动继承同一 profile。

不使用 mock：真的起一个 genericagent.core.app 子进程，读它 stdout.log 里的
"[Info] LLM sessions from ..." 行，确认父子解析到同一个 profile。

用法：
    python tests/probe_subagent_inherits_official_ds.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def main() -> int:
    from genericagent.llm.config import load_llm_config
    from genericagent.llm.client import load_clients_from_yaml

    clients, active_index, cfg_path, _mtime = load_clients_from_yaml()
    names = [c.backend.name for c in clients]
    target = "官方-ds-v41-1017"
    if target not in names:
        print(json.dumps({"passed": False, "reason": f"{target} 不在配置中", "profiles": names}, ensure_ascii=False, indent=2))
        return 2
    llm_no = names.index(target)
    print(f"父进程配置: {cfg_path}")
    print(f"父进程选择: {target} (llm_no={llm_no})")

    work = Path(tempfile.mkdtemp(prefix="ga_ds_probe_"))
    task_dir = work / "task"
    task_dir.mkdir(parents=True, exist_ok=True)

    env = {**os.environ}
    env["GA_LLM_CONFIG"] = str(cfg_path)
    env["GA_DATA_DIR"] = str(work / "data")
    cmd = [
        sys.executable, "-m", "genericagent.core.app",
        "--task", "ds_probe",
        "--nobg",
        "--task_dir", str(task_dir),
        "--project_root", str(REPO),
        "--state_root", str(work / "data"),
        "--llm_no", str(llm_no),
        "--verbose",
    ]
    print("启动子进程:", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=str(work),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()

    lines = [ln for ln in (out or "").splitlines() if "LLM sessions from" in ln]
    print()
    print("=== 子进程 `LLM sessions` 行（初始化时打印，仍是 active_profile）===")
    for ln in lines:
        print(" ", ln)
    if not lines:
        print("=== stdout (前 30 行) ===")
        for ln in (out or "").splitlines()[:30]:
            print(" ", ln)
        print("=== stderr (末 15 行) ===")
        for ln in (err or "").splitlines()[-15:]:
            print(" ", ln)

    # 权威证据是 startup.jsonl 的 model_selected 行：它在 next_llm(--llm_no)
    # 之后写入，记录的是最终生效的 profile。`LLM sessions` 那行只反映
    # active_profile，早于 --llm_no 生效，用它判断会得出错误结论。
    selected = None
    startup_path = task_dir / "startup.jsonl"
    if startup_path.is_file():
        for line in startup_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("phase") == "model_selected":
                selected = row
    print()
    print("=== 子进程 startup.jsonl / model_selected（权威）===")
    print(" ", json.dumps(selected, ensure_ascii=False) if selected else "未找到")

    ok = False
    detail = "子进程未写入 model_selected"
    if selected:
        detail = (
            f"子进程 llm_no={selected.get('llm_no')} "
            f"profile={selected.get('model_profile')} model={selected.get('model_id')}"
        )
        ok = (
            str(selected.get("llm_no")) == str(llm_no)
            and selected.get("model_profile") == target
        )

    print()
    print(json.dumps({
        "passed": ok,
        "expected_profile": target,
        "expected_llm_no": llm_no,
        "child_detail": detail,
        "child_model_selected": selected,
        "child_session_lines": lines,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())