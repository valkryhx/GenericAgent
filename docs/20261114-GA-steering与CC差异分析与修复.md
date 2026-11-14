# GA steering 与 agent team 对齐 CC 的差异分析与修复（2026-11-14）

## 0. 结论先行

这一轮复刻 CC 的工作分两块，**两块都已落地**：

**A. steering / interrupt** — GA 只复刻了 CC 的一半：入队（queue）复刻了，出队路径漏了三条。

1. **排队消息只在「回合内的一次 fold 点」有投递通路**。`pending_input.py` 的注释写着
   「队列故意跨回合存活，turn N 最后 fold 点之后到达的消息要活到 turn N+1」，
   但**没有任何代码在 turn N+1 投递它** —— 全仓 `pending_prompts` 的消费者只有
   `core/loop.py` 一个 fold 点。消息永久滞留，UI 却说「will be sent at the next tool boundary」。
2. **Esc 只取消，不 steering**。CC 的 Esc = 打断当前生成 + 立刻把队列里的消息作为
   新 prompt 重新发出去；GA 的 `abort_current_turn()` 只置 `stop_sig`。
3. **回合自然结束时，队列没人接手**。CC 把排队消息当成下一回合跑；GA 留着等用户
   下次开口，然后 `_drain_carried_prompts` 把它当成那个无关请求的前缀 —— 用户体感是
   「我的话被静默拖延了」。

外加一个**延迟**差异（不丢消息，但体感等同卡死）：

4. **fold 点粒度是 tool batch，不是 tool result**。模型自己写
   `code_run(timeout=360)` 内部 `sleep(30)` 轮询时，steering 会被吞 5 分钟以上。

**B. agent team** — 真机 session 暴露四个具体缺陷，全部已修：

1. **长 message 报 `message is required`** —— 真实原因是工具调用参数流被截断，
   `_try_parse_tool_args` 静默降级成 `{"_raw": ...}`，handler 却报「缺 message」。
   模型因此误判成「消息太长不能传」，永久改用短消息绕路。
2. **不传 `task_name` 会撞名** —— 缺少自动改名，只能靠模型重试。
3. **不传 `team_task_id` 会绑错任务** —— 按 token 打分挑选，长 message 里的词
   会命中标题完全不同的任务。真机案例：给「Enter 路由」派的 agent 被绑到
   「光标渲染」的任务上，lead 事后手工修标题和 owner。
4. **缺活性心跳** —— `state.json` 没有 `heartbeat_at`，`output.txt` 停滞时无法
   区分「进程死了」和「长工具调用期间镜像没刷」，supervisor 唯一动作是杀，
   把已完成的成果一起丢掉。

## 1. 真机证据

`temp/ink_bridge_backend.log`（2026-11-14 00:46–01:22）：

```
[Agent] wait_agent 不阻塞；改为 sleep+轮询状态文件
[Action] Running python ... code_run(timeout=360)
0 {'review_ink_routing': ('running', 0, 'pending'), ...}   ← 每 30s 一行，11 行
...
Abort current task...                                      ← 用户按 Esc
User aborted the task.
```

队列内容直到 01:06:38 才以 `[GA_USER_MIDTURN_MESSAGE]` 折叠进模型：

```
[GA_USER_MIDTURN_MESSAGE]
The user sent a new message while you were working:
你刚才为何似乎任务建立给sub时出错了好几次
咋了？
为啥不回复呢
你为何不说话了
[/GA_USER_MIDTURN_MESSAGE]
```

同一 dump 里最后一条用户输入是**裸的新回合**（无 MIDTURN 包装）：

```
=== Prompt === 2026-11-14 01:22:45
[TEXT] [TEAM ORCHESTRATION OPT-IN] ... 为何3个sub卡死
```

它不是被 steering 投递的，而是因为 bridge 判到 agent 已 idle，走了 `queue_prompt()`
的 promote 分支 —— **steering 只有在「恰好不忙」时才真能到达模型**。

agent-team 侧的坑，模型自己在工作记忆里记下了：

```
坑：spawn_agent 传长中文 message 时参数会被截断导致 message required 报错；
解决=短 message 或写任务书文件让子代理读。spawn 时必须显式 task_name。
```

## 2. CC 的语义（对照目标）

| 行为 | CC | GA 修复前 | GA 修复后 |
| --- | --- | --- | --- |
| 执行中输入 | 入队，不打断 | ✅ 已复刻 | ✅ |
| fold 点投递 | 每个 tool-result 边界 | 每个 tool-**batch** 边界 | 保持（见 §5 取舍） |
| 回合起点投递 | 有 | **缺** | ✅ `_drain_carried_prompts` |
| 回合结束时投递 | 立刻当下一回合跑 | **缺** | ✅ `_start_queued_turn_after_end` |
| 按 Esc | 打断 + **立刻用队列重新提问** | 只 abort | ✅ |
| workflow/team 运行中 Esc | 同上 | 只 abort | ✅ |

## 3. 修复清单

### A1 回合起点 drain + 专用包装

`pending_input.render_queued_prompts(items, *, when=...)`：

- `"mid_turn"`（默认，原行为）：fold 点用，「while you were working」；
- `"turn_start"`：「while the previous turn was still finishing」。

回合起点用 mid-turn 措辞是**错的** —— 那个回合已结束，模型会把新消息当成对上一个
请求的补充说明。

`GenericAgent._drain_carried_prompts()` 在 `run()` 里、构造 messages 之前调用，
以 `when="turn_start"` 追加到本轮 query。不抛异常：队列坏掉不能连累用户这一回合。

### A2 Esc = 打断 + 立刻用队列重新提问

`GenericAgentBridge.abort_current_turn()`：

1. **先** `_take_queued_prompt_text()` 取空队列（在 abort 之前取，避免与收尾竞争）；
2. 忙 → `agent.abort()` + `status: stopping`，队列非空则起
   `ga-ink-queued-after-stop` watcher，等 idle 后 `submit()`；
3. 不忙但队列非空 → 直接 `submit()`；
4. 都不满足 → 原样 `stop()`。

watcher 有 120s 上限；超时或 submit 抛错都发 `queued_prompt_dropped` 错误事件。

UI（`state.ts`）：queued 占位行在消息真正变成 `user` 事件时被移除，否则 Esc 重发
会让同一句话在 transcript 里出现两次。提示行改为 `Esc sends it now`。

### A3 回合结束时把队列跑成下一回合

`_consume_display_queue` 收到 `done`、发完 `idle` 之后调用
`_start_queued_turn_after_end()`：取空队列并交给同一个 `_submit_when_idle` watcher。
不内联 submit，因为这个方法本身跑在消费线程里，`_is_consuming()` 仍为真。

### A4 fold 点粒度 —— 明确不改（见 §5）

### B1 截断的参数流不再误报为「缺 message」

`do_spawn_agent` 增加前置分支：`args` 含 `_raw` 且缺 `task_name`/`message` 时，返回

```json
{"status": "error", "reason": "tool_args_unparsed",
 "msg": "The tool-call arguments did not arrive as valid JSON (N chars, ends with ...).
         This is a transport-level truncation, not a limit on the message you may send:
         the message field was never delivered. Retry the same call with the same full
         message. If it truncates again, split the work across several spawns rather than
         shortening the message into something the child cannot act on."}
```

真正缺字段（无 `_raw`）仍然报 `message is required`。

### B2 team 任务绑定拒绝歧义猜测

`_resolve_team_task` 在按 token 打分之后增加 margin 检查：最高分与次高分**相等**时
返回 `reason: "team_task_ambiguous"`，并列出候选 id + subject，要求调用方显式传
`team_task_id`。宁可多一次廉价重试，也不要一个静默绑错的 teammate。

### B3 活性心跳

**子进程侧**（`core/app.py`）：

- `_subagent_state()` 每次写状态时写 `heartbeat_at`（与 `updated_at` 分开 ——
  `updated_at` 会被父进程的每次 reconcile 刷新，不能当进度信号）；
- 新增 `_touch_heartbeat()`，在**每次工具调用前**和**每次工具完成后**推进心跳，
  覆盖「一个长工具调用占满整回合」这种情况。

**父进程侧**（`agents/manager.py`）：

- 新增 `_output_is_stalled(task_dir, raw)`，按可靠性排序读取**子进程自己产生的**信号：
  `heartbeat_at` → `stdout.log` mtime → `events.jsonl` mtime。阈值 90s（故意宽松，
  一次慢工具调用不会误判）；
- `_refresh_state` 输出三个新字段：`progress_stalled` / `progress_evidence` /
  `progress_age_seconds`，与 `process_status` **并列**而不互相覆盖。
  进程是否死亡由进程检查负责；「停滞」只表示子进程不再证明自己活着。
- `AgentState` 增加对应字段，`_subagent_state_payload` 透传给模型。

关键点：**「停滞」不等于「死亡」**。旧的 supervisor 把两者混为一谈，才会杀掉正在
长工具调用里的子进程。

## 4. 验收

### 单测（全绿）

| 文件 | 新增内容 |
| --- | --- |
| `tests/test_midturn_prompt_queue.py` | +5：turn_start 措辞、未知 when 回退、`CarriedPromptDeliveryTest` 四条 |
| `tests/test_bridge_queue_prompt.py` | +11：取空顺序/保序、Esc+队列、Esc 空队列、idle+队列、watcher 等 idle、watcher 超时上报、回合结束跑队列 |
| `tests/test_ga_subagent_tools.py` | +2：截断参数流报 `tool_args_unparsed`、真空 message 仍报缺失 |
| `tests/test_subagent_manager.py` | +4：`SubagentLivenessProbeTest`（新鲜心跳/旧心跳但 stdout 增长/全无信号/无 state） |
| `frontends/ink/ui/src/composerDuringRun.test.ts` | +1：Esc 提示文案 |
| `frontends/ink/ui/src/queuedPromptVisibility.test.ts` | +2：占位行被真实回合取代、不误删未投递的占位行 |

### 实测结果

| 套件 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests -t .` | 2666 tests, **OK**（skipped=8） |
| `npm test`（Ink UI） | **730 pass / 0 fail** |
| `npm run typecheck` | 通过 |
| `tests/test_*subagent*.py` 连跑 3 次 | OK（3/3） |

### 真机 e2e（已跑通）

`tests/real_ink_bridge_interrupt_e2e.py` 新增 `check_esc_sends_the_queued_prompt`
（U5/U6），在真实 LLM 回合上验证：

```
"escSendsQueuedPrompt": {
  "longTaskId": 3,
  "queueAccepted": true,
  "queueDepthBeforeStop": 1,
  "statusRightAfterStop": "stopping",
  "queuedPromptBecameANewTurn": true,
  "secondsToNewTurn": 0.407,
  "newTaskId": 4,
  "queueEmptyAfterTake": true,
  "newTurnCompleted": true,
  "modelAnsweredTheQueuedText": true
}
```

Esc 之后 0.4 秒内队列消息成为新回合（taskId 4 > 长任务的 3），模型复述了只存在于
排队文本里的随机 marker。

**过程中修掉了一个假绿**：第一版复用了 `SHORT_MARKER`，断言匹配到了更早一轮的
`user` 事件（`newTaskId: 2` 小于长任务的 3、`secondsToNewTurn: 0.0`）。改用独立的
`ESC_MARKER` 并要求 `taskId > longTask` 之后才是真结果。

运行方式（profile 名随环境变化，需按 `llm.yaml` 指定）：

```powershell
$env:GA_RUN_REAL_API_E2E="1"
$env:GA_REAL_API_EXPECTED_NAME="default"
$env:GA_REAL_API_EXPECTED_MODEL="moma/deepseek-latest-flash"
python tests/real_ink_bridge_interrupt_e2e.py
```

## 5. workflow 执行期间的 steering（2026-11-14 补测）

问题：workflow 跑在 `_run_workflow_runtime` 的独立后台线程里，主 agent 的回合在
`do_workflow` 返回 `async_launched` 后立即结束。那么「workflow 执行中」用户说话，
消息还能及时到达吗？

### 5.1 实测到的两条路径

| 路径 | 触发条件 | 修复前延迟 | 修复后延迟 |
| --- | --- | --- | --- |
| `submit`（新回合） | 主 agent 已 idle（**最常见**） | 2.0s | 2.0s |
| `queue` + fold（折叠进当前回合） | 主 agent 还在生成（如 `wait_agent` 里） | **14.5s**（切片 10s）/**57.1s**（切片 120s） | **4.0s / 5.0s** |

`submit` 路径本来就正常：workflow 不占用全局 `status`，composer 不会锁。

### 5.2 修掉的真实缺陷：`wait_agent` 是用户消息的黑洞

`SubagentManager.wait_agents` 的 docstring 写着「returns as soon as anything
happens」，但**用户排队的消息不在它监视的「anything」里**。它盲等完整个
`WAIT_SLICE_MAX_S`（默认 10s），或者更糟 —— 如果模型在 `wait_agent` 里传了
`timeout_seconds: 300`，它仍然只等一个切片，但那个切片期间消息完全无法送达。

负对照证明了延迟就是切片本身：把 `WAIT_SLICE_MAX_S` 从 10s 提到 120s，同一条
消息的送达时间从 14.5s 涨到 **57.1s**。消息从头到尾一直在队列里。

修复（`agents/manager.py` + `core/handler.py`）：

1. `wait_agents(..., user_input_probe=None)` —— 每轮循环**先**检查用户输入；
2. `GenericAgentHandler._pending_user_input_probe()` 把 `pending_prompts` 的
   `len()` 包成探针（一次加锁计数，代价可忽略）；
3. 命中时返回 `_interrupted_by_user_result`：`timed_out=False`、
   `interrupted_by_user=True`、`recommended_next_action="respond_to_user"`。

三个设计要点，每个都有对应的单测：

- **不是 timeout**。timeout 的语义是「你等的东西没发生」，会诱导模型立刻重试；
  这里是「用户说话比子 agent 重要，别等了」。
- **不推荐 `wait_agent`**。如果 `recommended_next_action` 还是 `wait_agent`，
  模型会马上重新进入同一个等待，把刚收到的消息又吞掉。
- **探针抛错不影响等待**。队列坏掉不能连累模型拿不到子 agent 结果。

对照组（同一个 manager，只差一个探针）：

```
WITHOUT probe: elapsed=3.00s interrupted=False
WITH probe:    elapsed=0.02s interrupted=True  action=respond_to_user
```

### 5.3 真机证据

`tests/real_workflow_steering_e2e.py`（新增），真实 LLM + 真实 workflow runtime：

```json
"steeringDuringWorkflow": {
  "workflowWasRunningBeforeSteer": true,
  "agentIdleAtSteerTime": true,      // 常见路径
  "steerRoute": "submit",
  "modelAnsweredTheSteer": true,
  "secondsToModelAnswer": 2.028,
  "workflowStillRunningWhenAnswered": true
},
"steeringWhileBusy": {
  "agentBusyAndWorkflowLive": true,  // 难路径：workflow 活着且主 agent 在生成
  "steerRoute": "queue",
  "queueDepthAfterSteer": 1,
  "modelAnsweredTheSteer": true,
  "secondsToModelAnswer": 5.005,
  "foldedIntoRunningTurn": true,     // 折叠进当前回合，没有新 user 事件
  "queueEmptiedAfterDelivery": true,
  "workflowStillRunningWhenAnswered": true,
  "withinTwiceTheWaitSlice": true
}
```

判定用随机 marker：它只存在于排队文本里，模型复述它才证明消息真的到了。

```powershell
$env:GA_RUN_REAL_API_E2E="1"
$env:GA_REAL_API_EXPECTED_NAME="default"
python tests/real_workflow_steering_e2e.py

# 负对照：把切片提到 120s，修复前会让延迟涨到 ~57s，修复后仍是 ~4s
$env:GA_E2E_WAIT_SLICE_MAX_S="120"
```

### 5.4 过程中修掉的测试缺陷（都写进来，因为都是「假绿」）

1. **`handoffTurnObserved` 是空检查**：第一版找带 `<workflow_handoff>` 的 `user`
   事件，但 `_queue_workflow_handoff` 走 `put_task`、只发 `status: running`，
   永远不会有那个事件。改判「`workflow_final` 之后出现的新 running taskId」。
2. **只断言新 `user` 事件会漏掉 fold 路径**：排队投递根本不产生 `user` 事件，
   按事件类型断言等于只测了简单路径。改判「marker 是否被模型复述」+ 事后从证据
   推断实际走了哪条路。
3. **workflow 窗口太短**：两个子任务各 20s，steer 答得慢一点就撞上 workflow 结束，
   `workflowStillRunningWhenAnswered` 随机失败。窗口拉到各 60s。

## 6. 明确不做的部分（取舍）

- **每次 tool call 之间都开 fold 点**：会把 `_run_parallel_tools` 的收口逻辑改成
  流式任务队列，回归面覆盖整个 loop。本轮改为让 `wait_agent`（最常见的长时间同步
  调用）主动感知用户输入，覆盖了实际的黑洞，而不动并行工具收口。
- **收到 steering 就 kill 当前 code_run**：那会把「随手补一句」变成「意外中断正在跑
  的编译/测试」，与 CC 语义（只有 Esc 打断）相反。
- **自动给 `task_name` 改名**：`_next_available_task_name` 对**活着的**同名 agent
  是拒绝而不是改名，这是有意的 —— 复用同名会覆盖那个 agent 唯一的产出证据。
  当前错误信息已给出 `followup_task` / `close_agent` / 换名三条出路。
- **主动杀掉停滞的子进程**：本轮只做**如实报告**（`progress_stalled`）。是否该杀、
  杀之前要不要先要求子进程落一份 findings，是需要单独定策略的 supervisor 主题。
  至少现在上层能拿到区分「死」与「停滞」的依据，不会再仅凭 output.txt 停滞就杀。
