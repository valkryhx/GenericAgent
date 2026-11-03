# GA goal 与 workflow 协同：`/goal … 使用workflow` 不生效 根因与修复

日期：2026-11-03
状态：已实施并通过真实 LLM 端到端验证（5/5）

## 1. 用户现象

在 GA 源码目录用 Ink 前端（`.\ga`）执行：

```text
/goal 使用workflow 看看最近4个commit的内容 最后总结，形成的内容写到 d:\git_codes\GenericAgent\bug\tests
```

观察到三处疑点：

1. 用户已经手动 `/goal`，可模型后续又调用了 `propose_goal`；
2. workflow 最终跑完，但界面**没有出现 goal met / achieved**；
3. 不确定 goal 与 workflow 到底有没有协同（goal 有没有做结果检查）。

## 2. 结论先行（一句话）

这次**不是** goal 求值器或 workflow 求值的问题，而是 **goal 从头就没被武装**。
Ink bridge 在处理「显式 workflow 请求」时，会把一段 `[WORKFLOW OPT-IN]` 模型提醒**拼在用户原始文本前面**，
于是真正交给 `GenericAgent.run()` 的字符串不再以 `/` 开头。`_handle_slash_cmd()` 第一行
`if not raw_query.startswith('/'): return raw_query` 直接把它当普通对话透传，
`/goal` 被当成自然语言，`active_goal` 始终为 `None`。

后果链：没有 active goal → 没有 goal 卡片 → Stop gate 不求值 → `_clear_goal()` 从不执行 →
没有 `terminal.met` 元数据 → 界面自然看不到 met/achieved。workflow 本身一直是成功的，与 goal 无关。

## 3. 证据

### 3.1 代码根因

- `frontends/ink/bridge.py::submit()`：显式 workflow 时
  `text = workflow_opt_in_reminder(activation) + "\n\n" + text`，再 `put_task(text, ...)`。
- `genericagent/core/app.py::_handle_slash_cmd()`：`if not raw_query.startswith('/'): return raw_query`。
  前缀把 `/goal` 顶到了第二行，命令失配。

### 3.2 确定性探针

裸 `/goal ...` → `active_goal` 被设置并返回 kickoff；加 `[WORKFLOW OPT-IN]` 前缀后 →
`active_goal=None` 且原样透传。已在真实会话与本仓库复现。

### 3.3 真实会话

`session_d0252625b08d4e659dc9a1aefb34fbf5.jsonl`：

- turn 1 user 原文以 `[WORKFLOW OPT-IN]` 开头，后面才是 `/goal ...`；
- 事件类型只有 `message / session_start / turn_end / working_state`，
  **没有 `active_goal`、也没有 `goal_status`**，坐实 goal 没设上；
- `propose_goal` 实际返回 `goal_proposal_rejected`（`GA_PROPOSE_GOAL_ENABLED` 关闭），
  并没有覆盖或改写 goal。

## 4. CC 303 对照：goal 与 workflow / 后台任务如何配合

参考 `D:\git_codes\claudecode283_hack\docs\20261028-CC-goal模式实现调研.md`：

- goal = 一条带 prompt 的会话级 **Stop hook**；求值器是一个只读、无工具的独立模型调用。
- **后台任务仍在运行时推迟求值**（§6.1）：`blockable_turn_end` 发现有在跑的 agent/shell，
  就把目标 hook 从 Stop 集合里摘掉、置 `goalPass="deferred"`，交给定时 check-in 机制。
- **真正求值**（§6.2）：后台任务结束后照常执行 Stop hooks；met/impossible 时摘掉目标 hook、
  清 active goal、写 `goal_status` 并推 `notifyMetadataChanged({goal:{...met:true}})`；
  未满足则注入 reason、增加 iterations、保留目标继续。
- **ProposeGoal 的开关语义**（§4.2）：`tengu_propose_goal` 关闭时 `ProposeGoal.isEnabled()`
  返回 false，**工具根本不出现在模型工具列表**，而不是“展示后再拒绝”。
  `/goal` 命令本身不受此开关控制，用户手动设置目标始终可用。

GA 侧的对应机制其实已经存在（`handler.run_stop_gate()`、`goal_evaluator.has_in_flight_agents/
workflow_launched_this_turn`、bridge 的 `workflow_in_flight_provider`、workflow handoff 新回合）。
**因此本次协同问题与这些机制无关，纯粹是 `/goal` 没被解析**。goal 一旦真正武装，
本次真实测试证明 workflow handoff 回合会正常触发 goal 求值并达成 met。

## 5. 修复方案（已实施）

### 修复 1：把「命令解析」与「模型提醒」分离（核心）

- `GenericAgent.put_task()` 新增 `model_prefix` 参数；任务队列新增 `model_prefix` 字段。
- `GenericAgent.run()` 先用**未修改的** `raw_query` 调 `_handle_slash_cmd()`：
  - 命令返回 `None`（本地命令，如 `/goal clear`）→ 照常结束回合，丢弃前缀；
  - 命令返回 kickoff/普通 prompt → 再把 `model_prefix` 拼到**结果**前面交给模型。
- 抽成可单测的 `_prepare_model_query(raw_query, model_prefix, display_queue)`。
- `bridge.submit()` 不再在 `text` 上拼前缀，而是把它作为 `model_prefix` 传给 `put_task`。
  `raw_text`（transcript 显示的原文）保持不变。

这样 `/goal`、`/compact`、`/resume`、`/session.*` 等所有 slash 命令都不会再被前缀破坏；
`[WORKFLOW OPT-IN]` 仍然会送到模型（附加在 kickoff/prompt 上），workflow 行为不变。

> 为什么不采用“在 `_handle_slash_cmd` 里识别并剥掉 `[WORKFLOW OPT-IN]`”的补丁：
> 那是脆弱的字符串耦合，只堵住这一个前缀，且会污染所有命令解析分支。分离数据流才是根治。

### 修复 2：`propose_goal` 关闭时从工具表隐藏（对齐 CC `isEnabled()`）

- 新增 `goal_proposal.filter_tool_schema(schema, *, is_subagent, in_plan_mode)`：
  当 `GA_PROPOSE_GOAL_ENABLED` 未开、或子代理、或 plan mode 时，从 schema 中移除 `propose_goal`。
- `app.py` 在每轮构建 `effective_tools_schema`（capabilities 过滤之后）调用它。
- `do_propose_goal` 的调用时 gate **保留**，作为纵深防御。

效果：模型不再“能看到、调一次、被拒绝”，从而不再出现用户误解的“明明我设了 goal 它还提议”。
新目标覆盖旧目标的产品语义保持不变（与 CC 一致），不额外加“已有 goal 就拒绝 propose”的规则——
减少打扰应通过隐藏工具 + 提示引导，而不是改变 CC 对齐的 gate 语义。

## 6. 测试与验证

### 6.1 确定性单测（无 LLM）

- 新增 `tests/test_goal_workflow_slash_prefix.py`（5 条）：
  - 带 `[WORKFLOW OPT-IN]` 前缀的 `/goal` 仍会武装 goal，且前缀会被带到 kickoff 上；
  - 裸 `/goal` 正常武装；
  - 前缀不会遮蔽本地命令（`/goal clear` 仍返回 None）；
  - 普通 prompt 会附加前缀；无前缀时恒等。
- 扩充 `tests/test_goal_phase4.py::SchemaVisibilityTests`（4 条）：
  `propose_goal` 在功能关闭 / 子代理 / plan mode 时从 schema 消失，开启时保留。

回归：`test_goal_phase0/1/2a/4/5/6 + test_goal_workflow_coop + resume + wake + loop_integration`
共 **154 passed**。

### 6.2 真实 LLM 端到端（走 `bridge.submit` 真实入口）

新增 `tests/real_goal_workflow_slash_submit_e2e.py`：直接调用 Ink 的真实入口
`bridge.submit("/goal … 使用workflow …")`，真实默认模型 `default/deepseek-v4.1-flash`，断言：

1. `bridge.submit` 后 `agent.active_goal` 被武装（旧 bug 的核心）；
2. launch 回合没有把 goal 误判为未达成（`premature_negative==0`，不 spin）；
3. workflow 到达终态 `succeeded` 且写出目标文件；
4. 存在非 `deferred` 的 `goal_status` 且 `met=True`。

**结果：连续 5/5 通过**（目标目录 `D:\test20260416\_goal_wf_slash_*`，每个 run 的产物已保留）。

真实运行还揭示了时序差异（也写进了测试注释）：
- 若 workflow 较慢，launch 回合结束前它仍在跑 → Stop gate 写 `reason:"deferred"`，
  handoff 新回合再求值 met（如 140100 run）；
- 若 workflow 很快完成，与 launch 回合合并 → 同一回合直接判 met（如 140327 run，只有 1 个 turn_end）。
两者都是**正确**的，因此“必须出现 deferred”不能作为硬断言，改为“launch 回合不 premature 判负 + 最终 met”。

### 6.3 用户可见表现

修好之后，`/goal … 使用workflow` 会：
- 立即出现 goal 卡片（active）；
- launch 回合结束时 goal 不抢跑、不误判；
- workflow 完成后的 handoff 回合执行 goal 求值，达成时卡片消失并推 `terminal.met=True` 徽标
  （`_clear_goal → _notify_goal_metadata(terminal=…)`，`state.ts` 消费并渲染）。

## 7. 关键代码坐标

- `frontends/ink/bridge.py::submit()` —— 前缀改为 `model_prefix`，`put_task(..., model_prefix=...)`
- `genericagent/core/app.py::put_task()` —— 新增 `model_prefix` 字段
- `genericagent/core/app.py::_prepare_model_query()` —— 先解析命令、再拼模型前缀
- `genericagent/core/goal_proposal.py::filter_tool_schema()` —— 按开关隐藏 `propose_goal`
- `genericagent/core/app.py`（run 内 `effective_tools_schema` 之后）—— 调用上述过滤
- `genericagent/core/app.py::_clear_goal()` / `_notify_goal_metadata()` —— met 徽标发射链（未改）

## 8. 未做 / 边界

- 未修改 goal 求值语义、workflow 调度、agent-team 机制——本次问题与它们无关。
- 未新增“已有 active goal 就拒绝 propose_goal”——与 CC 对齐，保持新目标覆盖旧目标。
- `propose_goal` 的 feature flag / setting 读法未改；仅改“是否暴露给模型”。

---

## 9. 追加：goal + agent team 仍不 met 的第二个成因（前导空格）

### 9.1 现象（2026-11-03 14:36 会话 `session_bbb00ffd…`）

用户先进了 `/team on`，再发 `/goal 使用 agent team 并行完成：在 D:\1234 …`，team 全部成功
（3 个 teammate 并行写好 a/b/c.md，index.md 汇总），**但同样没有 goal met 卡片**。

### 9.2 事件取证

该会话事件类型只有 `session_start / message / turn_end / working_state`，
**没有 `active_goal`、也没有 `goal_status`** —— 和上一次 workflow 是同一类“goal 没武装”。

### 9.3 关键区别与真正成因

transcript 首条 user 是：`[TEAM ORCHESTRATION OPT-IN]\n\n /goal …`
—— `/goal` 前面**有一个前导空格**（注意上一次 workflow 是 `\n\n/goal`，无空格）。

第 5 节的修复把前缀与命令分成两个字段传递，`run()` 先解析原始 `raw_query`、再把前缀拼到
**原始文本**前面。用户输入本身带了前导空格 `" /goal …"`，于是：
- `_handle_slash_cmd(" /goal …")` 里 `raw_query.startswith('/')` 为 **False**（有前导空格），
  命令再次失配 → 前缀被拼回原始串 → 模型看到的仍是「前缀 + ` /goal …`」自然语言，
  goal 从未武装。这是**独立于第 5 节的第二个坑**：命令解析对前导空白太严格。

确定性复现：
- `"/goal do x"` → `active_goal = do x`
- `" /goal do x"` → `active_goal = None`（旧）
- `"\n/goal do x"` → `active_goal = None`（旧）

### 9.4 修复

`_handle_slash_cmd` 改为先判 `str(raw_query).strip().startswith('/')`，命中后
`raw_query = str(raw_query).strip()` 再进入各分支（分支内部本就 `.strip()`，无副作用）。
裸 `/goal`、带前导空格/换行/制表符的 `/goal` 现在都能武装目标。

### 9.5 验证

- 确定性：`tests/test_goal_workflow_slash_prefix.py` 新增
  `test_leading_whitespace_goal_still_arms`（空格/多空格/换行/制表符四种前导）。
  连同 phase4 schema 测试 **40 passed**。
- 真实 LLM e2e：新增 `tests/real_goal_team_slash_submit_e2e.py`，用**带前导空格**的
  `/goal 使用 agent team …` 走真实 `bridge.submit`，默认模型 `default/deepseek-v4.1-flash`：
  - goal 立即武装（旧代码此处为 None）；
  - team 写出 a.md/b.md/c.md + index.md（非空）；
  - 最终 `goal_status met=True`。
  **实测 3/3 通过**。

### 9.6 结论

goal + workflow 与 goal + agent team 走的是同一条“slash 命令被模型前缀/空白顶掉”的脆弱路径。
第 5 节修好了“前缀拼接顺序”，本节修好了“前导空白”。两者合起来，`/goal` 无论后面接
workflow 还是 team（含用户手滑带空格），都能正确武装并被 Stop gate 求值。
