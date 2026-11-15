"""真实 LLM 的 workflow 冒烟：并行 agent() + 子 agent 实际干活。

为什么要单独写它：
    现有的 workflow 验证（scripts/verify_multiagent_0_6_2.py L3）用的是
    FakeChildAgentRunner，只证明"接线"对，证明不了真模型按当前 schema 能跑通。
    这次改动的两处（spawn_agent 的 required、role 取代 agent_type）都在 schema
    层，只有一次真模型调用能覆盖。

同时回答一个具体问题：**workflow 子 agent 的 schema 里没有 role，有没有影响？**

    workflow 子 agent 的 schema 直接来自 assets/tools_schema.json
    （child_agent.py:_discover_tools_schema），不经过
    app._apply_subagent_role_schema —— 所以既没有 role 也没有 agent_type。
    这个脚本第三条判据就是量它：把子 agent 真实看到的 spawn_agent 参数面打印出来，
    并断言 required 与主会话一致（这是本次改动真正影响到 workflow 的那部分）。

判据（都是"只存在于本次调用"的哨兵）：
    1. workflow 跑完，两个 agent() 都 succeeded；
    2. 每个子 agent 写出的文件里含自己那个唯一 token，且不含别人的；
    3. 子 agent 看到的 spawn_agent schema 与主会话一致（required 两项）；
    4. 子 agent 的 schema 里没有 role —— 记录事实，不当作失败（已知且已决定不修）。

用法：
    python scripts/verify_workflow_real_llm.py
    python scripts/verify_workflow_real_llm.py --profile default --timeout 300
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


WORKFLOW_SCRIPT = r"""
export const meta = { name: 'verify-workflow-parallel', description: 'two children write disjoint files' }
phase('Write')
const results = await parallel([
  () => agent(`Create the file alpha_${args.tag}.txt in your working directory containing only this token: ${args.tokenA}. Do not create any other file.`),
  () => agent(`Create the file beta_${args.tag}.txt in your working directory containing only this token: ${args.tokenB}. Do not create any other file.`),
])
return { count: results.length, summaries: results.map(r => String((r && r.summary) || '').slice(0, 120)) };
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Real-LLM workflow smoke test.")
    parser.add_argument("--profile", default=os.environ.get("GA_WORKFLOW_LLM_PROFILE", "default"))
    parser.add_argument("--timeout", type=float, default=420.0)
    args = parser.parse_args(argv[1:])

    from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
    from genericagent.workflow.models import WorkflowRun
    from genericagent.workflow.runtime import WorkflowRuntime
    from genericagent.workflow.store import WorkflowStore

    tag = uuid.uuid4().hex[:8]
    token_a = uuid.uuid4().hex
    token_b = uuid.uuid4().hex
    workspace = Path(tempfile.mkdtemp(prefix="ga-real-wf-"))
    keep = os.environ.get("GA_VERIFY_KEEP") == "1"
    failures: list[str] = []
    notes: list[str] = []

    try:
        store = WorkflowStore(root=str(workspace / "store"))
        runner = NativeGPTChildAgentRunner(profile_name=args.profile, enable_tools=True, max_turns=12)

        # ── 判据 3/4：子 agent 真实看到的工具面（不启动子进程，纯 schema） ──
        tools = runner._load_tools_schema()
        spawn = next(
            (t for t in tools if (t.get("function") or {}).get("name") == "spawn_agent"),
            None,
        )
        if spawn is None:
            failures.append("the workflow child has no spawn_agent at all")
        else:
            params = spawn["function"]["parameters"]
            required = params.get("required")
            props = sorted(params["properties"])
            print(f"  spawn_agent required : {required}")
            print(f"  spawn_agent props    : {len(props)} 个")
            print(f"  has role             : {'role' in props}")
            print(f"  has agent_type       : {'agent_type' in props}")
            if required != ["task_name", "message"]:
                failures.append(f"workflow child schema required={required!r}, expected ['task_name','message']")
            if "role" not in props:
                # 已知、已决定不修（用户 2026-11-15 的决定）。记录事实，不算失败。
                notes.append(
                    "workflow 子 agent 的 spawn_agent 没有 role —— 它走的是 "
                    "assets/tools_schema.json 原始文件，不经过 _apply_subagent_role_schema。"
                    "影响：子 agent 无法主动请求预配置角色；它仍然可以 spawn，只是拿不到角色前缀。"
                )

        # ── 判据 1/2：真模型跑 workflow ──
        script = WORKFLOW_SCRIPT
        run = store.create_run(WorkflowRun(
            run_id=f"wf_real_{tag}", session_id="real-wf-verify",
            script=script, status="running",
        ))
        previous = os.environ.get("GA_WORKFLOW_WORKSPACE_ROOT")
        os.environ["GA_WORKFLOW_WORKSPACE_ROOT"] = str(workspace)
        try:
            # 显式给足预算：deadline 在 node worker 启动之前就开始计时
            # （runtime.py:284），所以它还要覆盖 spawn + ready 握手。
            WorkflowRuntime(
                store=store, runner=runner, timeout_seconds=args.timeout,
            ).run(run, args={
                "tag": tag, "tokenA": token_a, "tokenB": token_b,
                "workspacePath": str(workspace),
            })
        finally:
            if previous is None:
                os.environ.pop("GA_WORKFLOW_WORKSPACE_ROOT", None)
            else:
                os.environ["GA_WORKFLOW_WORKSPACE_ROOT"] = previous

        loaded = store.load_run(run.run_id)
        statuses = [j.status for j in loaded.jobs]
        print(f"\n  run status : {loaded.status}")
        print(f"  jobs       : {len(loaded.jobs)}  statuses={statuses}")
        if loaded.error:
            print(f"  error      : {str(loaded.error)[:300]}")
        if loaded.status != "succeeded":
            failures.append(f"workflow status={loaded.status!r} error={str(loaded.error)[:200]!r}")
        if len(loaded.jobs) != 2:
            failures.append(f"expected 2 jobs, got {len(loaded.jobs)}")
        for job in loaded.jobs:
            if job.status != "succeeded":
                failures.append(f"job {job.job_id} status={job.status!r} error={str(job.error)[:200]!r}")

        # ── 判据 2：哨兵与防串写 ──
        for name, token, other_token in (
            (f"alpha_{tag}.txt", token_a, token_b),
            (f"beta_{tag}.txt", token_b, token_a),
        ):
            matches = list(workspace.rglob(name))
            if not matches:
                present = [str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file()][:20]
                failures.append(f"{name} was never created; files present: {present}")
                continue
            content = matches[0].read_text(encoding="utf-8", errors="replace")
            if token not in content:
                failures.append(f"{name} does not contain its own token (content={content[:160]!r})")
            elif other_token in content:
                failures.append(f"{name} contains the other child's token (cross-write)")
            else:
                print(f"  {name}: own token ✔, no cross-write ✔")

        print()
        if notes:
            print("NOTES（记录事实，不判失败）")
            for note in notes:
                print(f"  - {note}")
            print()

        if failures:
            print("✖ FAILURES:")
            for item in failures:
                print(f"   - {item}")
            return 1
        print("VERDICT: ✔ a real model ran a 2-child workflow end to end")
        return 0
    finally:
        if keep:
            print(f"  workspace kept at {workspace}")
        else:
            try:
                shutil.rmtree(workspace, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
