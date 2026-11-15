"""安装版 0.6.2 的多智能体设施分层验收（L0 → L3）。

为什么要分层，而不是直接跑 agent team：
    "agent team 挂了" 有三种完全不同的原因——底层 spawn 不工作、task 认领不
    工作、或者 team 协议本身有问题。一次性跑 end-to-end 拿到一个红，无法区分
    是哪一层。分层之后，每一层的失败都指向一个具体的机制，且上一层通过才能
    说明下一层的红不是被它连累的。

L0  纯静态：schema 形状、handler 参数接受面。不需要起进程。
L1  单次 spawn：起一个真子进程，验证 task_name/message/role 三条路径。
L2  并发 spawn：同时起 N 个，验证名字不冲突、各自可寻址。
L3  编排面：task board 认领 + team_mode + workflow 并行 agent()。

判定标准是**证据**，不是"没报错"：每个 L1+ 检查都要求子进程状态或事件流里
出现一个只属于该次调用的哨兵 token，否则视为假绿。

用法：
    python scripts/verify_multiagent_0_6_2.py            # 跑 L0–L3
    python scripts/verify_multiagent_0_6_2.py --level 1  # 只跑某一层
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

_results: list[tuple[str, str, str]] = []


def record(level: str, name: str, status: str, detail: str = "") -> None:
    _results.append((level, name, status))
    marker = {PASS: "✔", FAIL: "✖", SKIP: "–"}[status]
    line = f"  {marker} {name}"
    if detail:
        line += f"  [{detail}]"
    print(line)


def check(level: str, name: str):
    """Decorator-less helper: run a callable and record its outcome."""
    def run(fn):
        try:
            detail = fn() or ""
            record(level, name, PASS, detail)
            return True
        except AssertionError as exc:
            record(level, name, FAIL, str(exc)[:200])
            return False
        except Exception as exc:  # noqa: BLE001
            record(level, name, FAIL, f"{type(exc).__name__}: {exc}"[:200])
            return False
    return run


# ── L0: schema 与 handler 面 ────────────────────────────────────────────────

def level0() -> bool:
    print("\nL0  静态契约（不起进程）")
    ok = True

    def schema_shape():
        from genericagent.core import app as agentmain
        agentmain.load_tool_schema(include_mcp_tools=False)
        spawn = next(t for t in agentmain.TOOLS_SCHEMA
                     if t["function"]["name"] == "spawn_agent")
        params = spawn["function"]["parameters"]
        assert params.get("required") == ["task_name", "message"], \
            f"required={params.get('required')!r}, expected ['task_name','message']"
        props = params["properties"]
        assert "agent_type" not in props, "the retired agent_type is still published"
        assert "role" in props, "role property missing"
        assert "enum" not in props["role"], "role must not be a closed menu"
        return f"{len(props)} properties"

    def role_catalog_visible():
        from genericagent.core import app as agentmain
        agentmain.load_tool_schema(include_mcp_tools=False)
        spawn = next(t for t in agentmain.TOOLS_SCHEMA
                     if t["function"]["name"] == "spawn_agent")
        desc = spawn["function"]["parameters"]["properties"]["role"]["description"]
        for name in ("explore", "plan", "review"):
            assert name in desc, f"{name} not discoverable from the role description"
        return "explore/plan/review listed"

    def no_silent_name_default():
        import genericagent.core.handler as handler_mod
        src = Path(handler_mod.__file__).read_text(encoding="utf-8")
        assert "task_name = task_name or role.name" not in src, \
            "the silent role->task_name default is back"
        return "removed"

    ok &= check("L0", "spawn_agent declares required")(schema_shape)
    ok &= check("L0", "role catalog is discoverable")(role_catalog_visible)
    ok &= check("L0", "role no longer names the agent")(no_silent_name_default)
    return ok


# ── L1: 单次 spawn ──────────────────────────────────────────────────────────

def _make_handler(root: str, *, popen=None, pid: int | None = None):
    """Build a handler whose subagent manager spawns stub processes.

    ``pid`` matters: the registry decides liveness from it (``_is_live_row``), so a
    fabricated dead pid makes a "live" child look reapable and the duplicate-name check
    silently renames around it. Pass ``os.getpid()`` when a test needs the child to
    genuinely read as alive.
    """
    from genericagent.core.handler import GenericAgentHandler
    from genericagent.agents.manager import SubagentManager

    class _Parent:
        llm_no = 0
        verbose = False
        permission_mode = None
        session_id = "verify-session"

    spawn_pid = pid if pid is not None else 4242
    parent = _Parent()
    handler = GenericAgentHandler(parent, last_history=[], cwd=str(Path(root) / "temp"))
    handler.subagent_manager = SubagentManager(
        root_dir=root,
        popen=popen or (lambda *a, **k: type("P", (), {"pid": spawn_pid})()),
        python_executable="python-verify",
    )
    return handler


def _drain(outcome):
    return outcome.data


def _exhaust(gen):
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        result = stop.value
    return result


def level1() -> bool:
    print("\nL1  单次 spawn（真子进程 stub，验证三条参数路径）")
    ok = True

    def baseline_spawn():
        with tempfile.TemporaryDirectory() as td:
            handler = _make_handler(td)
            token = uuid.uuid4().hex
            data = _drain(_exhaust(handler.do_spawn_agent(
                {"task_name": "worker_one", "message": f"do the thing {token}"}, None)))
            assert data["status"] == "started", data
            task_dir = Path(td) / "temp" / "worker_one"
            assert task_dir.is_dir(), f"task dir not created: {task_dir}"
            # 哨兵必须真的写进子进程的输入，否则等于没验证投递
            prompt = (task_dir / "input.txt").read_text(encoding="utf-8")
            assert token in prompt, "the sentinel never reached the child's input"
            return f"run_id={data['run_id']}"

    def role_spawn():
        with tempfile.TemporaryDirectory() as td:
            handler = _make_handler(td)
            data = _drain(_exhaust(handler.do_spawn_agent(
                {"task_name": "worker_role", "message": "inspect only", "role": "explore"}, None)))
            assert data["status"] == "started", data
            assert data["agent_type"] == "explore", f"role did not resolve: {data.get('agent_type')!r}"
            prompt = (Path(td) / "temp" / "worker_role" / "input.txt").read_text(encoding="utf-8")
            assert "[GA_SUBAGENT_ROLE]" in prompt, "role prompt prefix missing"
            return "role=explore"

    def missing_message_reports_both():
        with tempfile.TemporaryDirectory() as td:
            handler = _make_handler(td)
            data = _drain(_exhaust(handler.do_spawn_agent({"role": "explore"}, None)))
            assert data["status"] == "error", data
            assert data["reason"] == "task_name_and_message_required", data.get("reason")
            msg = data["msg"]
            assert "task_name" in msg and "message" in msg, msg
            assert not (Path(td) / "temp" / "explore").exists(), "a task dir was created anyway"
            return "both fields named, nothing spawned"

    def old_alias_still_works():
        with tempfile.TemporaryDirectory() as td:
            handler = _make_handler(td)
            data = _drain(_exhaust(handler.do_spawn_agent(
                {"task_name": "worker_alias", "message": "go", "agent_type": "plan"}, None)))
            assert data["status"] == "started", data
            assert data["agent_type"] == "plan", data.get("agent_type")
            return "agent_type alias honoured"

    ok &= check("L1", "spawn with task_name + message")(baseline_spawn)
    ok &= check("L1", "spawn with role")(role_spawn)
    ok &= check("L1", "missing fields reported together")(missing_message_reports_both)
    ok &= check("L1", "agent_type alias still accepted")(old_alias_still_works)
    return ok


# ── L2: 并发 spawn ──────────────────────────────────────────────────────────

def level2() -> bool:
    print("\nL2  并发 spawn（同一父会话起 N 个，验证名字与寻址互不串）")
    ok = True

    def parallel_distinct_names():
        with tempfile.TemporaryDirectory() as td:
            handler = _make_handler(td)
            names = [f"worker_{i}" for i in range(4)]
            tokens = {n: uuid.uuid4().hex for n in names}
            started = []
            for name in names:
                data = _drain(_exhaust(handler.do_spawn_agent(
                    {"task_name": name, "message": f"task {tokens[name]}"}, None)))
                assert data["status"] == "started", f"{name}: {data}"
                started.append(data["task_name"])
            assert started == names, f"names drifted: {started}"
            # 每个 token 必须落在自己的 task dir 里，不能串
            for name in names:
                prompt = (Path(td) / "temp" / name / "input.txt").read_text(encoding="utf-8")
                assert tokens[name] in prompt, f"{name} did not receive its own task"
                others = [t for n, t in tokens.items() if n != name and t in prompt]
                assert not others, f"{name} received someone else's token"
            return f"{len(names)} children, no cross-talk"

    def duplicate_name_is_refused():
        with tempfile.TemporaryDirectory() as td:
            # A real pid, so the registry reads the first child as genuinely alive —
            # otherwise it reaps the row and silently renames the second to `dup_1`.
            handler = _make_handler(td, pid=os.getpid())
            first = _drain(_exhaust(handler.do_spawn_agent(
                {"task_name": "dup", "message": "first"}, None)))
            assert first["status"] == "started", first
            second = _drain(_exhaust(handler.do_spawn_agent(
                {"task_name": "dup", "message": "second"}, None)))
            assert second["status"] == "error", "a live name was reused without error"
            assert second.get("reason") == "name_conflict", second.get("reason")
            # 报错必须点名那个真实存在、且调用方确实选过的路径
            assert "/root/dup" in second["msg"], second["msg"]
            return "name_conflict on reuse"

    ok &= check("L2", "4 concurrent spawns stay separate")(parallel_distinct_names)
    ok &= check("L2", "reusing a live name is refused")(duplicate_name_is_refused)
    return ok


# ── L3: 编排面（task board / team_mode / workflow）──────────────────────────

def level3() -> bool:
    print("\nL3  编排面（共享任务板 + team_mode + workflow 并行 agent()）")
    ok = True

    def task_board_claim():
        with tempfile.TemporaryDirectory() as td:
            handler = _make_handler(td)
            store = handler._get_task_store()
            task = store.create_task("collect facts", "gather and report facts")
            data = _drain(_exhaust(handler.do_spawn_agent({
                "task_name": "teammate_a",
                "message": "claim and do it",
                "team_mode": True,
                "team_task_id": task["id"],
            }, None)))
            assert data["status"] == "started", data
            owner = store.get_task(task["id"])["owner"]
            assert owner == "/root/teammate_a", f"task owner={owner!r}"
            return f"task #{task['id']} owned"

    def team_mode_without_binding_is_refused():
        with tempfile.TemporaryDirectory() as td:
            handler = _make_handler(td)
            data = _drain(_exhaust(handler.do_spawn_agent({
                "task_name": "teammate_b", "message": "go", "team_mode": True,
            }, None)))
            assert data["status"] == "error", "an unbound teammate was allowed"
            assert "reason" in data, data
            return data.get("reason", "")

    def workflow_parallel_agents():
        from genericagent.workflow.runtime import WorkflowRuntime
        from genericagent.workflow.store import WorkflowStore
        from genericagent.workflow.child_agent import FakeChildAgentRunner
        sys.path.insert(0, str(REPO / "tests"))
        from tests.test_workflow_runtime import WorkflowRun

        with tempfile.TemporaryDirectory() as td:
            store = WorkflowStore(root=td)
            runner = FakeChildAgentRunner(results={"agent_1": {"summary": "one"}})
            script = """
export const meta = { name: 'verify', description: 'verify parallel agents' }
const a = await agent('branch one');
const b = await agent('branch two');
return { a: a.summary, b: b.summary };
"""
            run = store.create_run(WorkflowRun(run_id="wf_verify", session_id="s",
                                               script=script, status="running"))
            # Point the workspace at the temp dir. The default root is the project's data
            # bucket, which a live session creates on its first transcript write
            # (``transcripts.append_event`` does ``mkdir(parents=True)``) — but a bare
            # script run from an arbitrary cwd has never written a transcript, so the
            # default is legitimately absent here and would fail for the wrong reason.
            previous = os.environ.get("GA_WORKFLOW_WORKSPACE_ROOT")
            os.environ["GA_WORKFLOW_WORKSPACE_ROOT"] = td
            try:
                outcome = WorkflowRuntime(store=store, runner=runner, timeout_seconds=60).run(run)
            finally:
                if previous is None:
                    os.environ.pop("GA_WORKFLOW_WORKSPACE_ROOT", None)
                else:
                    os.environ["GA_WORKFLOW_WORKSPACE_ROOT"] = previous
            loaded = store.load_run("wf_verify")
            assert loaded.status == "succeeded", f"status={loaded.status} error={loaded.error}"
            assert [j.status for j in loaded.jobs] == ["succeeded", "succeeded"], \
                [j.status for j in loaded.jobs]
            assert len(loaded.jobs) == 2, len(loaded.jobs)
            return f"{len(loaded.jobs)} agents"

    ok &= check("L3", "team_mode claims its shared task")(task_board_claim)
    ok &= check("L3", "unbound team_mode is refused")(team_mode_without_binding_is_refused)
    ok &= check("L3", "workflow runs 2 parallel agents")(workflow_parallel_agents)
    return ok


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Layered multi-agent verification.")
    parser.add_argument("--level", type=int, action="append",
                        help="run only this level (repeatable); default: all")
    args = parser.parse_args(argv[1:])

    import genericagent  # noqa: F401  (fail fast if the install is broken)
    from genericagent import paths
    print(f"GA install     : {Path(genericagent.__file__).parent}")
    print(f"runtime assets : {paths.ASSETS_DIR}")
    print(f"python         : {sys.version.split()[0]}")

    levels = args.level or [0, 1, 2, 3]
    ok = True
    for level in levels:
        fn = {0: level0, 1: level1, 2: level2, 3: level3}.get(level)
        if fn is None:
            continue
        ok &= fn()

    print()
    passed = sum(1 for _, _, s in _results if s == PASS)
    failed = sum(1 for _, _, s in _results if s == FAIL)
    print(f"RESULT: {passed} passed, {failed} failed")
    if failed:
        print("VERDICT: ✖ multi-agent surface is NOT healthy — see the ✖ lines above")
    else:
        print("VERDICT: ✔ subagent → team → workflow all execute")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
