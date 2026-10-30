# GA goal 与 workflow / agent-team 协同：现状、根因与修复

日期：2026-10-30
范围：`genericagent/core/goal_evaluator.py`、`genericagent/core/handler.py`、
`frontends/ink/bridge.py`、`genericagent/core/goal_wake.py`，以及对应测试。

## 1. 用户问题（原话要点）

> 真实渲染测试扩展到 agent-team / workflow 面板的 goal 联动……goal + 多智能体是复杂且重要的场景，
> 不仅 UI 要对，**实际执行也要正确**。我甚至不清楚 workflow + goal 应该如何同时使用，**你真的支持吗**。

结论先给：**修复前，goal 与 workflow 各自能跑，但同时使用会出现"主 agent 空转、回合结束不了"的真问题。**
不是 UI 小瑕疵，是执行期语义缺陷。

## 2. 硬事实（代码核对，均为零命中/单点命中）

1. `rg -n "workflow" genericagent/core/goal_evaluator.py genericagent/core/goal.py` → **零命中**。
   即 goal 求值器完全不知道 workflow 存在。
2. `rg -n "active_goal|goal_defer|has_in_flight" genericagent/workflow/` → **零命中**。
   workflow 运行时也完全不看 goal。
3. `has_in_flight_agents()` / `list_in_flight_task_labels()` 只读 `SubagentManager.list_descendants()`。
   **workflow 不经 SubagentManager**：它走 `bridge.workflow_launch_from_tool()` →
   `threading.Thread(target=self._run_workflow_runtime, daemon=True)`，用独立的 `AgentScheduler`。
4. 因此 goal 激活 + 后台 workflow 时，Stop gate 的 `has_in_flight_agents()` 恒为 False →
   直接求值当前（还没有产物的）transcript → 判 not met → `blocked` → `('continue', reason)`
   → 主 agent 在同一回合里不断重跑，直到 `STOP_HOOK_BLOCK_CAP`（默认 8）或外力 abort。

## 3. 真实 LLM 复现证据（修复前）

探针：`tests/real_goal_workflow_probe_e2e.py`（真实 deepseek-v4.1-flash）
落盘：`temp/goal_wf/probe_summary_20261030-153104.json`

关键字段：

```text
goal_active_after_set : true
launch_turn           : { done: false, steps: 9, secs: 57.9 }
last_goal_defer       : false          <-- workflow 完全未被识别为在飞
last_turn_outcome     : { kind: completed, exit_reason: CURRENT_TASK_DONE }
workflow.status       : succeeded      <-- workflow 本身没问题
marker                : ...\wf_out.txt <-- 产物写出来了
```

解读：workflow 后台能正常跑完并把文件写出来；但**发起 workflow 的那一轮 `done:false`，主 agent
单回合跑了 9 个模型步**（block→continue 死循环），只有探针的 hard cap/abort 才让它停下。
这就是"回合结束不了"。根因就是第 2 节第 3、4 条。

## 4. 正确语义应该是什么

CC 的 goal 在 Stop 时，如果**后台工作仍在产出**，会 defer（推迟求值），让这一轮正常结束；
等后台任务回来后的新回合再评估。GA 的 2-A 已经实现了"子 agent 在飞就 defer"，
**唯独漏了 workflow 这条后台通道**。

修复后的期望：
- 发起 workflow 的回合：`done:true`，`last_goal_defer:true`，模型步数正常（1~2 步），不空转。
- workflow 终态后 handoff 起新回合：此时无在飞 workflow → 正常求值 → goal met 或继续。
- 崩溃/被杀但状态仍写着 `running` 的残留 run **不能**永久 defer（必须 fail-open）。

## 5. 修复方案（本次实施）

### 5.1 bridge 侧：注入"在飞 workflow"只读判定

`frontends/ink/bridge.py` 增加 `_workflow_in_flight_labels()`：
- 按当前 session 取 `workflow_store.list_runs(session_id=...)`；
- 只把 `status in {running, awaiting_approval}` **且启动线程仍存活**的 run 视为在飞
  （线程已死但状态还是 running 的，按 stale 跳过，避免永久 defer）；
- 返回 `"<workflowName> · workflow"` 形式的短标签。
- 在 `_bind_agent()` 里挂到 `agent.workflow_in_flight_provider`（只读回调，不把整个 store 塞进 core）。

### 5.2 goal_evaluator：合并 workflow 在飞判定

- 新增 `list_in_flight_workflow_labels(agent)`：优先用 `agent.workflow_in_flight_provider`；
  无 provider 时（headless/单测）回退到 `agent.workflow_store.list_runs(session_id=...)`。
  任何异常一律返回 `[]`（fail-open，绝不因查询失败而永久 defer）。
- `list_in_flight_task_labels()` 把子 agent 标签与 workflow 标签合并（2-B check-in 文案随之覆盖 workflow）。
- `has_in_flight_agents()` → 子 agent 在飞 **或** workflow 在飞 都为 True。

### 5.3 不改的东西

- 不改 workflow 运行时、不改 Stop gate 的 continue/defer 分支（`handler.py` 已支持 `goal_defer`）。
- 不改 workflow/agent-team 的 UI 布局规则（面板仍是 overlay），只补测试证明共存与恢复。

## 6. 实施记录（本次已完成）

### 6.1 关键新增：本回合启动过 workflow 也必须 defer

只按"当前有 running 的 workflow"判定**不够**。真实跑测发现一个竞态：
若 workflow 在 launch 回合内（模型还没停下之前）就跑到 `succeeded`，那么 Stop 时
`has_in_flight_workflows()` 已为 False，goal 又会对**还没 handoff 的单轮 transcript** 求值，
误判 not met → block → 主 agent 自己动手补写（实测 18 步）。

因此补一条正交通道：**本回合通过 workflow 工具启动过 workflow，就 defer。**
- bridge 在 `workflow_launch_from_tool()` 成功起线程后置 `agent._workflow_launched_this_turn = True`。
- `app.run()` 每个回合开头把它重置为 False。
- `goal_evaluator.workflow_launched_this_turn(agent)` 读它；`handler.run_stop_gate()` 的
  `goal_defer` 判定 = `has_in_flight_agents(...) or workflow_launched_this_turn(agent)`。

这样 guarantee：发起 workflow 的那一轮一定正常结束；真正的求值发生在 workflow handoff
起的新回合（此时 flag 已被重置，且 run 已终态）。

### 6.2 改动清单

- `genericagent/core/goal_evaluator.py`
  - `_IN_FLIGHT_WORKFLOW_STATUSES = {running, awaiting_approval}`
  - `_workflow_runs_in_flight(agent)`：优先 provider，回退 `agent.workflow_store`；**全程 fail-open**
  - `has_in_flight_workflows(agent)`
  - `workflow_launched_this_turn(agent)`
  - `list_in_flight_task_labels()` 合并子 agent + workflow 标签（check-in 文案覆盖 workflow）
  - `has_in_flight_agents()` 先查 workflow，再查 SubagentManager
- `genericagent/core/handler.py`
  - `run_stop_gate()` 的 `goal_defer` 增加 `workflow_launched_this_turn` 分支
- `frontends/ink/bridge.py`
  - 新增 `_workflow_in_flight_labels()`（只读；跳过 launcher 线程已死的 stale run）
  - `_bind_agent()` 绑定 `agent.workflow_in_flight_provider`
  - `workflow_launch_from_tool()` 置 `_workflow_launched_this_turn`
- `genericagent/core/app.py`
  - 每回合开头重置 `_workflow_launched_this_turn`

### 6.3 测试

- 单测 `tests/test_goal_workflow_coop.py`（13）：provider/store 回退、fail-open、终态不 defer、
  标签合并、bridge stale 线程跳过、per-turn flag。
- 真实 LLM e2e `tests/real_goal_workflow_coop_e2e.py`（8/8）：完整 goal+workflow 链。
- 真实 LLM `tests/real_goal_2a_defer_e2e.py`（6/6）：goal+agent-team 子代理通道。
- 渲染帧 `frontends/ink/ui/src/goalMultiAgentUiRender.test.ts`（4）：goal 卡片 + workflow 状态栏共存、
  宽度不溢出、team overlay 覆盖时隐藏 goal 卡片、关闭后恢复、goal 激活时 `/team` 仍可打开。

## 7. 验证结果（真实模型 default/deepseek-v4.1-flash）

修复前探针（`temp/goal_wf/probe_summary_20261030-153104.json`）：
`launch_turn.done=false, steps=9, last_goal_defer=false`。

修复后协同 e2e（`temp/goal_wf_coop/summary_20261030-154951.json`）：

```text
launch_turn            : { done: True, steps: 4, secs: 12.3 }
last_goal_defer        : True
goal_iterations_after  : 0
workflow.status        : succeeded
marker wf_out.txt      : 写入目标目录，内容 WF_OK
goal after handoff     : 非 deferred 求值 -> met -> goal cleared
```

回归：
- `pytest -k goal`：157 passed
- `pytest tests/test_ink_bridge.py`：149 passed
- `pytest test_workflow_store + test_agent_control_workflow`：38 passed
- Ink UI `npm test`：506 passed；`npm run typecheck`：0 错误

## 8. 用户问的"workflow + goal 该怎么一起用"

现在可以这样用，且是被支持的：
1. `/goal <可验证的停止条件>` 设目标。
2. 正常用一句话让主 agent 用 `workflow` 工具后台发起脚本（或 `/workflow on` 后让它自主编排）。
3. 发起那一轮会立即正常结束（defer），workflow 在后台跑。
4. workflow 结束会 handoff 起一个新回合，此时才判定 goal：达成则清除，未达成则继续。

关键点：goal 求值**永远发生在 workflow handoff 之后的新回合**，不会抢跑、不会空转。
