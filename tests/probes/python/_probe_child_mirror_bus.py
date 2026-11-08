"""Defect 2, measured directly: a child must mirror into the parent's bus.

The child used to derive its mirror path as ``dirname(task_dir)/subagents``. On the
product layout the task dir is ``<bus>/tasks/<name>``, so that guess creates a *second*
bus at ``<bus>/tasks/subagents`` and writes every turn event there. The parent reads
``<bus>``, so the mirror was always one directory too deep.

This probe spawns a real child under the product layout and then checks, from the files
on disk:
  * the parent bus received the child's turn events  (the fix works)
  * no orphan bus was created beneath the task root  (the guess is gone)

Opt in with GA_RUN_REAL_API_E2E=1 for the model turn; without it the child still spawns
but its turn may not complete, and the probe says so instead of pretending.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
NAME = "mirror_probe_child"
REPORT = Path("D:/tmp/ga_probe_child_mirror.txt")
TURN_EVENTS = {"turn_started", "turn_completed", "agent_shutdown", "agent_exited", "agent_error"}


def bus_types(bus: Path) -> list[str]:
    path = bus / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(str(json.loads(line).get("type")))
        except json.JSONDecodeError:
            continue
    return out


def _rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())


def main() -> int:
    lines: list[str] = []
    out = lines.append
    verdict = 1

    try:
        from genericagent.agents.manager import SubagentManager
    except Exception as exc:  # noqa: BLE001
        out(f"import failed: {type(exc).__name__}: {exc}")
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return 1

    # Product layout: a project bucket plus a session id. This is the shape where the
    # old guess pointed at an orphan directory.
    from genericagent import paths

    roots = paths.project_roots(project=str(REPO), data_root=str(paths.RUNTIME_ROOT))
    session_id = f"mirror_probe_{int(time.time())}"
    manager = SubagentManager(
        project_root=str(roots.project_root),
        python_executable=sys.executable,
        project_state_root=str(roots.data_root),
        project_data_dir=str(roots.project_data_dir),
        parent_session_id=session_id,
        task_store_root=str(roots.tasks_dir),
        worktree_root=str(roots.project_worktrees_dir),
    )
    parent_bus = Path(manager.subagents_state_dir)
    task_root = Path(manager.task_root)
    orphan_bus = task_root / "subagents"

    out(f"parent bus (what the main agent reads) = {parent_bus}")
    out(f"task root                              = {task_root}")
    out(f"orphan bus (the old guess)             = {orphan_bus}")
    out("")

    llm_no = 0
    if OPT_IN:
        try:
            from genericagent.llm.client import load_clients_from_yaml

            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                _, active, *_ = load_clients_from_yaml(start_dir=REPO)
            llm_no = int(active or 0)
        except Exception as exc:  # noqa: BLE001
            out(f"profile lookup failed, llm_no stays 0: {type(exc).__name__}: {exc}")

    handle = manager.spawn_agent(
        NAME,
        "只输出这一行，不要调用工具：MIRROR_PROBE_OK",
        llm_no=llm_no,
        verbose=False,
        parent_session_id=session_id,
        parent_permission_mode="full_access",
        ipc_mode="socket",
    )
    task_dir = Path(handle.task_dir)
    out(f"child task dir                         = {task_dir}")
    out(f"child env bus (GA_SUBAGENTS_STATE_DIR)  = {manager.subagents_state_dir}")
    out("")

    if OPT_IN:
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                state = json.loads((task_dir / "state.json").read_text(encoding="utf-8", errors="replace"))
            except Exception:
                state = {}
            if state.get("turn_status") == "completed" or state.get("process_status") in {"shutdown", "failed"}:
                break
            time.sleep(2)

    parent_events = bus_types(parent_bus)
    out(f"parent bus event types ({len(parent_events)} rows): {sorted(set(parent_events))}")
    mirrored = sorted(set(parent_events) & TURN_EVENTS)
    out(f"  turn events seen in the parent bus : {mirrored}")
    out("")

    orphan_events = bus_types(orphan_bus)
    out(f"orphan bus exists: {orphan_bus.exists()}")
    out(f"orphan bus event rows: {len(orphan_events)}  types: {sorted(set(orphan_events))}")
    orphan_files = sorted(p.name for p in orphan_bus.iterdir()) if orphan_bus.is_dir() else []
    out(f"orphan bus files: {orphan_files}")
    out("")

    # The inbox had the identical wrong guess; check it separately or a half-fix passes.
    parent_inbox = parent_bus / "inbox.jsonl"
    orphan_inbox = orphan_bus / "inbox.jsonl"
    out(f"parent inbox rows : {_rows(parent_inbox)}")
    out(f"orphan inbox rows : {_rows(orphan_inbox)}")
    out("")

    got_parent = bool(mirrored)
    no_orphan = not orphan_files
    out(f"CHECK child mirrored into the parent bus : {got_parent}")
    out(f"CHECK no orphan bus beneath task root    : {no_orphan}")

    # A child that never ran a turn cannot prove mirroring; say so rather than pass.
    if not OPT_IN and not got_parent:
        out("")
        out("INCONCLUSIVE: no turn ran (set GA_RUN_REAL_API_E2E=1 to include a real turn)")

    verdict = 0 if (got_parent and no_orphan) else 2

    with contextlib.suppress(Exception):
        manager.close_agent(handle.task_name, reason="mirror_probe_done", grace_s=1.0)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
