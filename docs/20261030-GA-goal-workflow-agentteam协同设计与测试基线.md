# GA goal × workflow × agent-team 协同设计与测试基线

日期：2026-10-30
状态：**基线 v1（可继续深入）**
定位：这是后续继续做"协同深入测试与优化"的**唯一入口文档**。它同时记录
（A）当前的协同设计（含代码坐标），（B）已经跑过的测试与证据落盘位置，
（C）尚未覆盖的组合场景。配套变更记录见 `docs/20261030-GA-goal与workflow协同修复.md`。

> 结论一句话：goal / workflow / agent-team 三个子系统**已能协同**，核心机制是
> "后台有活就推迟 goal 求值（defer），活干完由 handoff 起新回合再判"。但**三面板同时
> 活跃的真实 LLM 组合场景尚未测**（见 §7），这是下一步要补的重点。

---

## 1. 三个子系统各自的运行模型（先分清，才好谈协同）

| 子系统 | 谁在跑 | 生命周期 | 完成信号 | 是否经 SubagentManager |
| --- | --- | --- | --- | --- |
| **goal** | 主会话本身 | 会话级，持续到达成/清除/failed | Stop 门禁求值 | 不涉及 |
| **workflow** | bridge 起的独立 daemon 线程（每次 run 一个） | run 级，终态即结束 | `_run_workflow_runtime` 返回 → `_queue_workflow_handoff` | **否**（自有 AgentScheduler） |
| **agent-team** | `SubagentManager` 管理的子进程/子 agent | 子 agent 生命周期 | 子 agent turn 结束 / process 退出 | **是** |

关键差异（这是历史 bug 的总根子）：
- agent-team 的子 agent 能被 `SubagentManager.list_descendants()` 看到；
- workflow **看不到**，它跑在 bridge 线程里。所以任何"看有没有后台活"的判定，
  如果只查 SubagentManager，就会漏掉 workflow。

要协同，就必须让 goal 的"有没有后台活在跑"这件事**同时覆盖两条通道**。

---

## 2. 协同设计（当前实现）

### 2.1 一句话机制

> 主 agent 回合即将结束时，Stop 门禁先问："现在还有后台活在产出吗？"
> 有（子 agent 在飞 / workflow 在飞 / 本回合刚启动过 workflow）→ **defer**：本轮不求值、
> 不 block、正常结束，并记一条 `goal_status(reason='deferred')`。
> 后台活回来（workflow handoff / 子 agent 下一轮）→ 新回合里 flag 已重置、run 已终态 →
> 正常求值：达成清除，未达成继续。

### 2.2 判定入口（唯一真源）

`GenericAgentHandler.run_stop_gate()`（`genericagent/core/handler.py:465`）在 goal 激活时：

```python
goal_defer = (
    goal_evaluator.has_in_flight_agents(agent, self)     # 子 agent 或 workflow 在飞
    or goal_evaluator.workflow_launched_this_turn(agent) # 本回合刚启动过 workflow
)
```

三条判定分别负责三种情况：
- `has_in_flight_agents` → 覆盖 **agent-team 在飞的子 agent** 和 **还在 running 的 workflow**；
- `workflow_launched_this_turn` → 覆盖**竞态**：workflow 在本回合内就 race 到 succeeded，
  Stop 时已无 running run，但结果还没 handoff，此时也不能对"残缺 transcript"求值。

### 2.3 goal 侧：在飞判定如何同时覆盖两条通道

文件 `genericagent/core/goal_evaluator.py`：

- `_IN_FLIGHT_WORKFLOW_STATUSES = {running, awaiting_approval}`（:174）
  只有这两个状态才算"还在产出"。`draft/succeeded/degraded/failed/cancelled/killed/interrupted` 都不算。

- `_workflow_runs_in_flight(agent)`（:207）——**workflow 在飞判定，fail-open**
  1. 优先用 `agent.workflow_in_flight_provider()`（bridge 注入的只读回调）；
  2. 没有 provider（headless / 单测）时回退 `agent.workflow_store.list_runs(session_id=...)`；
  3. 任何异常一律返回 `[]`。**这条 fail-open 很重要**：查询失败绝不能导致永远 defer、把用户回合卡死。

- `has_in_flight_workflows(agent)`（:252）——薄封装。

- `workflow_launched_this_turn(agent)`（:257）——读 `agent._workflow_launched_this_turn`。

- `list_in_flight_task_labels(agent, handler)`（:269）——给 2-B check-in 文案用：
  把子 agent 标签（`"<task> · <type>"`）与 workflow 标签（`"<workflowName> · workflow"`）合并。
  这样 check-in 消息能告诉模型"还有哪些活没回来"。

- `has_in_flight_agents(agent, handler)`（:301）——**先查 workflow，再查 SubagentManager**：

  ```
  if _workflow_runs_in_flight(agent): return True   # workflow 通道
  ... SubagentManager.list_descendants(...) ...      # agent-team 通道
  ```
  先查 workflow 的原因：它完全不经 SubagentManager，若先因"无 session/无 manager"提前
  return False 就会漏判。

### 2.4 bridge 侧：workflow 在飞回调（只读、能识别 stale）

文件 `frontends/ink/bridge.py`：

- `_workflow_in_flight_labels()`（:1926）：
  - 取当前 session 的 `workflow_store.list_runs(session_id=...)`；
  - 只保留 `status ∈ {running, awaiting_approval}` **且 launcher 线程仍 alive** 的 run；
  - **线程已死但状态仍写着 running 的 run 视为 stale 跳过**（否则崩溃后会永久 defer）；
  - 返回 `"<workflowName> · workflow"` 短标签。
- `_bind_agent()`（:423）把上面这个函数绑到 `agent.workflow_in_flight_provider`。
  只暴露一个查询函数，不把整个 store 塞进 core，保持 core 与 workflow 存储解耦。
- `workflow_launch_from_tool()`（:1269）成功起线程后置 `agent._workflow_launched_this_turn = True`。

### 2.5 app 侧：每回合重置 per-turn 标记

文件 `genericagent/core/app.py`：

- `run()` 每个回合开头：`self._workflow_launched_this_turn = False`（:1658）。
  只在"发起 workflow 的那一轮"点 True，handoff / 用户新回合自动清零。
- 2-B/3 唤醒调度器 `_ensure_goal_wake()`（:1578）用
  `has_in_flight=lambda a: goal_evaluator.list_in_flight_task_labels(a)`，
  因此 check-in 文案天然覆盖 workflow（无需另改）。

### 2.6 hook runtime：defer 时确实跳过求值

`genericagent/hooks/runtime.py:562`：Stop 事件在 `payload['goal_defer'] is True` 时
跳过 session-scoped 动态 hook（即 goal evaluator）。所以 defer 不是"求值后丢弃"，
而是**根本不做模型求值**（省 token、省时间）。静态 hook（如 beep）不受影响。

---

## 3. 端到端时序（三种协同场景）

### 3.1 goal + workflow（后台脚本）

```
用户: /goal <条件>                     -> active_goal 设定
用户: "用 workflow 后台启动脚本X"
  main turn:  load_skill / workflow(script)
              bridge.workflow_launch_from_tool()
                ├─ create run, _assign_run_workspace
                ├─ _workflow_threads[run]=thread; thread.start()
                └─ agent._workflow_launched_this_turn = True   ← 关键
              model 停下 -> Stop gate:
                has_in_flight_agents?  (run 若还 running -> True)
                or workflow_launched_this_turn? True
                => goal_defer=True, 记 status(deferred), 正常结束(done)
  [后台] workflow runtime 跑完 -> status=succeeded -> _queue_workflow_handoff
  handoff 新回合: app.run() 开头 _workflow_launched_this_turn=False
              run 已终态 -> has_in_flight=False
              -> 正常求值 transcript(已含 workflow 结果) -> met/not_met
```

### 3.2 goal + agent-team（后台子 agent）

```
用户: /goal <条件>
用户: "后台 spawn 一个子 agent 做X"
  main turn: spawn_agent(background=True) -> SubagentManager 记录 descendant
              model 停下 -> Stop gate:
                has_in_flight_agents? -> SubagentManager.list_descendants 有 running -> True
                => defer, 正常结束
  子 agent 完成 -> 下一回合(用户/subagent 完成触发)
              -> descendant 已 exited -> 正常求值
```

### 3.3 goal + workflow 的子 agent 内再 spawn（workflow 内 agent() 即子 agent）

workflow 脚本里的 `agent(...)` 是 workflow runtime 自己的子 agent（不是主会话 SubagentManager
的 descendant）。对主会话而言，它整体表现为"一个 running 的 workflow run"，所以走 §3.1 的
workflow 通道即可，**不需要**单独看 workflow 内部有几个子 agent。

> 提醒：workflow 内部子 agent 的产物默认落在 **run 自己的 workspace**
> （`<target>/workflow-runs/<runId>/`），不是用户给的根目录。若 goal 条件写的是根目录下的文件，
> 必须在脚本里显式用绝对路径，或把 goal 条件写成 run workspace 下的路径。这点已在
> `tests/real_goal_workflow_coop_e2e.py` 里用绝对路径覆盖（否则会出现"workflow 成功但 goal 判
> 文件不在根目录"的假阴性）。

---

## 4. UI 联动设计（面板与卡片如何共存）

文件 `frontends/ink/ui/src/App.tsx`：

- `activePanel`（:1673）= 各种全屏 overlay（mcp/model/permission/approval/goalProposal/
  theme/selector/footer/**workflowPanel**/**teamPanel**/hooks）之或。
- `showWorkflowStatusBar`（:1677）= 有 workflow 状态栏 **且无 activePanel**。
- `showGoalCard`（:1681）= 有 goal 卡片/徽标 **且无 activePanel**。
- 渲染顺序（:1972-1973）：BottomChrome 内先 **Goal 卡片**，再 **Workflow 状态栏**
  （两者都在 `!activePanel` 时同时出现，是"次要信息带"，纵向叠加，互不遮挡）。

由此得到三条 UI 协同规则：

1. **goal 卡片 + workflow 状态栏可以同屏**：两者都是非 overlay 的次要带，纵向堆叠。
2. **workflow panel / team panel 是 overlay**：打开任一面板时 `activePanel` 为真，
   goal 卡片与 workflow 状态栏都让位（不硬塞进 overlay，避免挤压和遮挡）。
3. **关闭 overlay 后二者自动恢复**（因为 `activePanel` 变回 null）。

这三条已由渲染帧测试覆盖（§5、§6）；但"三面板都活跃时同时拉到真实屏幕"的**真实 LLM**
组合场景尚未跑（§7）。

---

## 5. 测试资产清单

### 5.1 单元测试（无需网络，快）

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_goal_workflow_coop.py` | 13 | provider/store 回退、fail-open、终态不 defer、标签合并、bridge stale 线程跳过、per-turn flag |
| `tests/test_goal_phase2a.py` | — | 原有 2-A：子 agent 在飞 defer（未回归） |
| `tests/test_goal_wake.py` / `_integration.py` | — | 2-B check-in / 3 retry（未回归） |
| `tests/test_goal_phase5.py` | — | goal 元数据/遥测（未回归） |
| `tests/test_goal_loop_integration.py` | — | 主循环 Stop gate 集成（未回归） |

`test_goal_workflow_coop.py` 的重点用例：
- `test_provider_marks_workflow_in_flight_without_manager`：只有 workflow、无 subagent 也判在飞；
- `test_store_fallback_live_status_defers`：`running` / `awaiting_approval` 都 defer；
- `test_store_fallback_terminal_status_does_not_defer`：7 种终态/草稿都不 defer；
- `test_raising_provider_is_fail_open` / `test_store_fallback_survives_list_runs_error`：异常不抛、不永久 defer；
- `test_labels_merge_subagent_and_workflow`：check-in 文案同时列两类活；
- `test_dead_thread_is_skipped`：launcher 线程死但状态 running → 视为 stale，不 defer；
- `test_launched_this_turn_flag_defers`：per-turn flag 单独也能 defer。

### 5.2 真实 LLM e2e（默认模型，需要网络）

| 文件 | 场景 | 最近结果 |
| --- | --- | --- |
| `tests/real_goal_workflow_probe_e2e.py` | 修复前诊断探针（baseline） | 修复前 9 步/失败；修复后 6 步/成功 |
| `tests/real_goal_workflow_coop_e2e.py` | **goal+workflow 完整链** | **8/8** |
| `tests/real_goal_2a_defer_e2e.py` | goal+agent-team 子代理 defer | **6/6** |

### 5.3 渲染帧测试（真实 Ink 挂载，假 bridge，无网络）

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `frontends/ink/ui/src/goalUiRender.test.ts` | 11 | 单 goal 卡片/徽标/nudge/提案面板/`/goal` 补全 |
| `frontends/ink/ui/src/goalMultiAgentUiRender.test.ts` | 4 | **goal 卡片 + workflow 状态栏同屏**、宽度不溢出、team overlay 隐藏并恢复 goal 卡片、goal 激活时 `/team` 仍可打开 |

### 5.4 既有可复用 UI e2e 脚手架（真实 bridge，可组三面板场景）

- `frontends/ink/ui/scripts/real_ink_goal_ui_e2e.ts`（真实模型 + 真实 /goal + 真实渲染）
- `frontends/ink/ui/scripts/real_ink_team_panel_e2e.ts`（真实 /team 面板）
- `frontends/ink/ui/scripts/real_ink_team_swarm_e2e.ts`
- `frontends/ink/ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts`（真实 workflow 自主编排）

§7 的"三面板真实组合"会以这些为基础拼装。

---

## 6. 测试记录（本轮实测，含证据落盘）

> 全部使用真实默认模型 `default/deepseek-v4.1-flash`（`llm.yaml` 的 default profile）。
> 证据文件均在 `temp/` 下，不入库；本文记录关键数字，便于复查。

### 6.1 修复前后对照（goal + workflow）

**修复前**（`temp/goal_wf/probe_summary_20261030-153104.json`）：

```text
launch_turn          : { done: false, steps: 9, secs: 57.9, text: "" }
last_goal_defer      : false
last_turn_outcome    : { kind: completed, exit_reason: CURRENT_TASK_DONE }
workflow.status      : succeeded
marker               : <目标目录>\wf_out.txt 存在
goal_active_end      : false
```

问题坐实：发起 workflow 那一轮无法正常结束，单回合空转 9 步；workflow 本身成功。
（`last_turn_outcome` 说 completed，是因为探针最后 abort 后收尾，不代表这一轮健康结束。）

**中间态**（`temp/goal_wf/probe_summary_20261030-154459.json`，仅加了"在飞 workflow"判定）：

```text
launch_turn          : { done: true, steps: 6, secs: 18.8 }
last_goal_defer      : true
goal_iterations      : 0
```

已能正常结束，但这个探针的 goal 条件写的是"目标根目录下的 wf_out.txt"，而 workflow 子 agent
把文件写进了 **run workspace**（`workflow-runs/<runId>/wf_out.txt`），所以 `marker=null`
是探针条件写法问题，不是协同问题（见 §3.3 提醒）。这也说明"goal 条件路径"必须与 workflow
产物实际落点一致。

**最终**（`temp/goal_wf_coop/summary_20261030-154951.json`，完整链 + 绝对路径条件）：

```text
launch_turn          : { done: true, steps: 4, secs: 12.3 }
last_goal_defer      : true
goal_iterations      : 0            (未误求值)
workflow.status      : succeeded
marker wf_out.txt    : 写入目标目录，内容 WF_OK
goal after handoff   : 非 deferred 求值 -> met -> goal 清除
RESULT               : 8/8 checks passed
```

> 注：中间那次 `summary_20261030-154654.json` 是**尚未加 per-turn flag**时的 18 步版本，
> 正是它暴露了 §2.2 的竞态，促成了 `workflow_launched_this_turn` 这条修正。保留作为过程证据。

### 6.2 goal + agent-team（子 agent 通道）

`tests/real_goal_2a_defer_e2e.py` → **6/6**（`temp/goal_2a/summary_20261030-155200.json`）：

```text
PASS turn1 /goal set an active goal
PASS turn2 (spawn) completed a turn
PASS transcript has goal_status(reason='deferred') from the spawn turn
PASS active goal is still set after deferred turn (not cleared/failed)
PASS background sub-agent eventually wrote child_done.txt
PASS after child finished, goal evaluated (a non-deferred goal_status exists)
```

### 6.3 UI 联动（渲染帧）

`goalMultiAgentUiRender.test.ts` → **4/4**：
- goal 卡片与 workflow 状态栏同屏（`Goal active` + `release-build` + `1/2 agents done` + `/workflows`）；
- 64 列下所有帧均不超宽；
- `/team` overlay 打开时 goal 卡片隐藏，Esc 关闭后恢复；
- goal 激活时 `/team` 仍可正常打开。

`goalUiRender.test.ts` → 11/11（既有单 goal UI 未回归）。

### 6.4 回归

| 命令 | 结果 |
| --- | --- |
| `pytest tests/ -k goal` | 157 passed |
| `pytest tests/test_ink_bridge.py` | 149 passed (+3 subtests) |
| `pytest test_workflow_store + test_agent_control_workflow` | 38 passed |
| `cd frontends/ink/ui && npm test` | 506 passed |
| `npm run typecheck` | 0 error |

---

## 7. 尚未覆盖 / 下一步重点：三面板真实组合场景

### 7.1 为什么这个场景重要

当前已测：
- 执行正确性：goal+workflow（真实 LLM 8/8）、goal+agent-team（真实 LLM 6/6）——**分别**测的；
- UI 共存：goal 卡片 + workflow 状态栏、team overlay 隐藏/恢复 goal 卡片——**渲染帧**层面测的（假 bridge）。

**没测的是二者叠加的真实组合**：goal 激活 + 一个真实 workflow 正在跑 + 真实 agent-team 子 agent
正在跑，三者在**同一个真实会话**里同时活跃，且用真实 LLM 驱动，验证：
1. 执行层：两条后台通道同时存在时，defer 判定、回合结束、workflow 完成、goal 求值都正确；
2. 渲染层：真实 bridge 事件流下，goal 卡片 / workflow 状态栏 / team overlay / workflow overlay
   的显隐与切换都对，且不互相遮挡、不超宽。

这正是用户说的"不仅 UI 要对，实际执行也要正确"的组合场景。

### 7.2 拟定测试设计（`tests/real_goal_workflow_team_combo_e2e.py` 或 UI e2e）

单会话流程：
1. 起真实 bridge + 真实模型（default/deepseek-v4.1-flash），`/permissions full`。
2. `/goal <条件>`：条件用**绝对路径**，指向 workflow 产物真实落点（run workspace 或根目录，二者择一并在脚本里对齐）。
3. 一个回合里：
   a. 用 `workflow` 工具后台发起一个**故意跑久一点**的脚本（如先 sleep 再写文件），保证它在本回合结束前仍 `running`；
   b. 用 `spawn_agent(background=True)` 起 1~2 个**故意跑久一点**的团队子 agent（sleep 后写文件）。
4. 断言发起回合 `done=true`、`last_goal_defer=true`、步数不爆（<= 8）。
5. 趁两者都还在跑：
   - `/team` 打开 team 面板 → 帧里出现 `Team ...` / `Task board`，且此时 goal 卡片被 overlay 让位；
   - Esc 关闭 → goal 卡片 + workflow 状态栏恢复同屏；
   - `/workflows` 打开 workflow 面板 → 帧正常，关闭后恢复。
6. 等 workflow / 子 agent 都到终态；断言：
   - workflow 产物写到预期位置；
   - 子 agent 产物写到预期位置；
   - handoff / 后续回合里 goal 被非 deferred 求值（met 或合理的 not_met），不误判；
   - 全程无帧超宽、无 "Running ·" 卡死、无重复 Static 重绘。

### 7.3 验收标准（DoD）

- 执行：第 4、6 步全绿；`goal_status` 里既有 `deferred`（发起回合）又有非 deferred（完成回合）。
- 渲染：goal 卡片、workflow 状态栏、team 面板、workflow 面板四个表面都真实出现过；
  任意时刻**没有两个 overlay 同时可见**（overlay 互斥）、**没有次要带与 overlay 重叠**。
- 宽度：所有捕获帧每行 `stringWidth < columns`（建议 80 与 64 两档各跑一次）。
- 证据落盘：`temp/goal_combo/<tag>/`（事件流 json、帧 dump、summary.json）。

### 7.4 可能暴露的风险（提前列，便于定位）

- **workflow 与 team 同时 defer 时的 check-in 文案**：`list_in_flight_task_labels` 是否两类都列到；
- **workflow 先完成、子 agent 后完成**（或反之）时，handoff 回合与子 agent 完成回合交错，
  goal 会不会在一次回合里被"误当无在飞"而求值——需确认 `has_in_flight` 在任一通道仍有活时为真；
- **team 面板打开期间 workflow 结束**：overlay 打开时是否仍能收到并正确更新（不丢事件、不闪退）；
- **goal 卡片 + workflow 状态栏 + 输入框** 在 24 行终端里是否挤压到 message 视口（layoutMetrics 的
  `activityRows = 1 + goalRows` 是否够）；
- **真实 team 成员的 agent 是否算进 SubagentManager**：若不是（例如 team 用了不同注册方式），
  子 agent 通道的 defer 可能失效——这是本次组合测试要重点验证的假设。

---

## 8. 已知约束与设计红线（后续改动必须守住）

1. **fail-open 优先**：任何"查后台是否在飞"的失败都必须当作"不在飞"，绝不能永久 defer。
   否则一次 store/manager 故障就会把用户回合无限挂起。
2. **workflow 不看 goal**：workflow runtime 保持对 goal 无感知（`rg workflow goal.py` 零命中是对的）。
   协同只在"回合边界的 defer 判定"这一处发生，不要把 goal 逻辑渗进 workflow runtime。
3. **agent-team 走 SubagentManager，workflow 走 provider**：两条通道各自独立、互不依赖。
   新增后台形态（将来可能有 shell 后台任务等）时，应扩 `has_in_flight` 的通道集合，而非改 workflow。
4. **per-turn flag 必须每回合重置**：`_workflow_launched_this_turn` 只能由"本回合发起"点亮，
   下一回合必须清零，否则 goal 会永远 defer。
5. **overlay 互斥**：goal 卡片/状态栏是"非 overlay 次要带"，workflow/team 面板是"overlay"。
   新增面板必须进 `activePanel`，否则会与次要带重叠（历史上 goalProposalPanel 漏进 activePanel 就是这类 bug）。
6. **产物落点要对齐 goal 条件**：workflow 子 agent 默认写 run workspace，goal 条件若指根目录必须在
   脚本里用绝对路径；这是测试常见假阴性的根源。

---

## 9. 相关文档索引

| 文档 | 内容 |
| --- | --- |
| `docs/20261030-GA-goal与workflow协同修复.md` | 本次 bug 的根因、修复方案、变更清单、验证结果（实施记录） |
| `docs/20261028-GA-goal模式设计与实施方案.md` | goal 模式整体设计与阶段划分 |
| `docs/20261030-GA-goal阶段2B-3唤醒调度器实施设计.md` | 2-B check-in / 3 retry 唤醒调度器 |
| `docs/20261030-GA-goal阶段5-SDK元数据遥测nudge计划.md` | goal 元数据/遥测/nudge |
| `docs/20261030-GA-goal-UI测试报告.md` | goal UI 真实渲染测试报告（提案面板等） |
| `claudecode283_hack` 仓库 hooks-internals / agent-team-internals | CC 侧对照证据 |

---

## 10. 变更历史

- 2026-10-30 v1：建立协同基线。记录 goal×workflow 修复（`9152893`）、goal×agent-team 验证、
  渲染帧 UI 共存测试，并列出"三面板真实组合场景"为下一步重点（§7）。

---

## 11. 三面板真实组合场景：测试结果（v2 追加）

> 本章对应 §7 的落地。用户要求"不仅 UI 要对，实际执行也要正确"，因此做了两层验证：
> **执行正确性**（§11.1，受控 gate，确定性）+ **纯模型驱动全链**（§11.2）+ **真实 UI 渲染**
> （§11.3，真实 bridge/模型/Ink）。全部使用 `default/deepseek-v4.1-flash`。

### 11.1 受控 gate e2e（执行正确性，确定性证据）

`tests/real_goal_workflow_team_gate_e2e.py` → **17/17 PASS**（`temp/goal_gate/summary_*.json`）。

做法：真实 bridge + 真实 workflow Runtime（真子 agent）+ 真实 `SubagentManager` team_mode
子 agent（真子进程），两者都 sleep 30 后写 marker；在**两者都在飞**的瞬间直接调用
`handler.run_stop_gate()`，再等两者 settle 后再调一次。

关键证据：

```text
wf_labels_inflight   => ["gate-wf · workflow"]          # workflow 通道在飞
sub_labels_inflight  => ["teammate1:running/alive"]     # team 子 agent 通道在飞（进 SubagentManager）
both_inflight        => true                            # 两通道同时 in-flight
gate_action          => "none"                          # 门禁不 block
gate_last_goal_defer => true                            # 记为 defer
goal evaluator 调用   => 0 -> 0（defer 时根本没求值）
goal_status recorded => reason="deferred"
--- 两者 settle 后 ---
inflight_after_settle => false
gate2_last_goal_defer => false
evaluator 调用        => 0 -> 1（确实求值了）
wf_marker_ok / team_marker_ok => true / true
```

**这直接证实了 §7.4 的核心假设**：`team_mode` 子 agent 与普通子 agent 走同一个
`SubagentManager.spawn_agent(parent_session_id=主会话)`，注册到同一 registry 根，因此
`list_descendants(主会话)` 能看到它 → goal 的 defer 判定覆盖 agent-team 成立。
（此前 `tests/real_goal_2a_defer_e2e.py` 6/6 验证的是普通子 agent；本测试补上了 team_mode。）

### 11.2 纯模型驱动全链（真实 LLM 自主发起两通道）

`tests/real_goal_workflow_team_combo_e2e.py` → **13/13 PASS**（`temp/goal_combo/summary_*.json`）。

真实模型在真实会话里：turn1 用 `task_create`+`spawn_agent(team_mode)` 起一个 sleep 120 的
team 子 agent；turn2 用 `workflow` 工具（scriptPath）起一个 sleep 30 的 workflow。关键数字：

```text
team_spawn_turn   => { done: true, steps: 6 }
team_last_goal_defer => true
wf_launch_turn    => { done: true, steps: 4 }
wf_last_goal_defer   => true
wf_labels         => ["combo-wf · workflow"]
sub_labels        => ["teammate1:running/alive"]     # workflow 启动后 team 子 agent 仍在飞
both_inflight     => true                             # 两通道真正并发
marker_wf / marker_team => 均按目标绝对路径落盘
goal_met          => true                             # handoff 后非 deferred 求值达成
```

这证明：**模型自己发起的两个后台通道**，在真实回合里都正常 defer、正常结束、产物正确、
最终 goal 正确求值。两个 launch 回合分别是 6 步 / 4 步，无空转。

> 踩坑记录：最早一版 combo 用"单回合同时起 workflow+team"且嵌套三层引号写 Python sleep，
> 模型花 10 步、122 秒仍写错脚本，`done=false`；而且 sleep 30 的 workflow 在该回合结束前
> 已跑完，观察不到并发窗口。这说明：**组合场景要拿到稳定并发窗口，必须先起"慢"通道再起"快"
> 通道**，并让脚本路径/引号尽量扁平（用 scriptPath + 正斜杠路径）。这些经验已固化进上面两个
> 测试文件。

### 11.3 真实 UI 渲染（真实 bridge + 真实模型 + 真实 Ink）

`frontends/ink/ui/scripts/real_ink_goal_workflow_team_combo_e2e.ts` → **10/10 PASS**
（证据：`temp/goal_combo_ui/data_<run>/frames.txt` + `summary.json`，43MB 级帧转储）。

真实会话里同时存在 goal + running workflow + live team 子 agent，然后依次切换面板，抓帧断言：

| 检查 | 结果 | 真实帧摘录 |
| --- | --- | --- |
| goal 卡片真实渲染 | PASS | `◎ Goal active · running …` |
| workflow 状态栏真实渲染 | PASS | `› ◌ combo-ui-wf  0/0 agents done` |
| **goal 卡片 + workflow 状态栏同帧共存** | PASS | 同一帧同时含两者 |
| `/team` overlay 真实渲染 | PASS | `Team session-… · lead team-lead · 1 member(s)` / `Task board` / `[~] #1 write marker_team owner=teammate1` |
| team overlay 打开时 goal 卡片让位 | PASS | 该帧无 `Goal active` |
| 关闭 `/team` 后 goal 卡片恢复 | PASS | 再次出现 `Goal active` |
| `/workflows` overlay 真实渲染 | PASS | `Dynamic workflows` / `› ◌ combo-ui-wf 1 agent` |
| workflows overlay 打开时 goal 卡片让位 | PASS | 该帧无 `Goal active` |
| 关闭 `/workflows` 后 goal 卡片恢复 | PASS | 再次出现 `Goal active` |
| 所有帧不超终端宽度 | PASS | 逐行 `stringWidth < columns` |

这样"三面板"的四类表面（goal 卡片 / workflow 状态栏 / team 面板 / workflow 面板）都在
**真实 LLM 真实渲染**下被验证，且验证了 overlay 互斥、次要带与 overlay 不重叠、关闭后恢复。

### 11.4 关于"三个面板同时可见"

诚实说明设计边界：按 §4 的设计，**team 面板 / workflow 面板是 overlay，goal 卡片是次要带**，
因此"三个全屏面板同一帧全部可见"不是目标状态，也做不到（overlay 会占用底栏）。真实可达且
已验证的目标状态是：
- **goal 卡片 + workflow 状态栏** 同帧（次要带并存）——已验证；
- **任一 overlay（team 或 workflow 面板）打开时**，goal/状态栏让位——已验证；
- 在 overlay 打开期间后台的 workflow/team 仍在真实运行（帧里能看到 `running`/进度）——已验证。

也就是说：**三者同时"存在且活跃"是真实验证过的；三者同时"以三个全屏面板并排显示"不符合
overlay 设计，也不应追求。** 若未来产品希望 goal 卡片在 overlay 内也保留一行，需要在 overlay
内部单独设计一个 goal 摘要行，而不是靠底栏并列。

### 11.5 本轮结论

- 执行层：goal × workflow × agent-team 三通道并发时，defer 判定、回合结束、双产物落盘、
  最终求值全部正确（受控 17/17 + 纯模型 13/13）。
- 渲染层：goal 卡片 / workflow 状态栏 / team 面板 / workflow 面板四表面在真实会话里都真实
  出现，显隐/互斥/恢复/宽度全部正确（10/10）。
- 未发现新的产品缺陷；组合场景运行稳定。
