# GA Ink `/workflow` 交互卡顿与进度可见性：调研与实施（2026-10-10）

## 1. 现象（用户实测）

在 GA Ink 里执行 `/workflow 使用 workflow 来调研一下 openai ...` 时：

1. **输入框卡顿**：输入 `/`、`/workflow`、以及正文时，按键回显有明显延迟。
2. **回车后长时间没有反馈**：按 Enter 之后要"卡一会儿"才出现 workflow 的子 agent（状态条/面板）。
3. **`j`/`k` 与上下键体验差**：UI 提示里有 `↑↓ agent · j/k scroll`，但按 `j`/`k` 常常"无效"，
   而且看不出子 agent 到底跑到哪一步。
4. **进度信息贫乏**：workflow 执行期间只能看到 `◌ agent_1`，看不到"第几个 turn"、"实时 token"、
   当前工具调用；一个子 agent 跑几分钟时整块 UI 是静止的。
5. **整体卡顿**：workflow 运行期间整个 Ink UI（含输入框）都不跟手。

## 2. 结论（根因）

四个互相独立、可以分别证伪的根因：

### R1（回车后卡顿）planner 调用在 JSONL 命令线程里同步执行

`frontends/ink_bridge.py::run_jsonl_loop`（`frontends/ink_bridge.py:1926`）是**单线程逐行**处理 stdin：
`for line in stdin: ... bridge.<cmd>(...)`。`cmd_type == "workflow_plan"` 直接调用
`bridge.workflow_plan(...)`（`frontends/ink_bridge.py:2013` 附近），而 `workflow_plan`
（`frontends/ink_bridge.py:700`）在同一个线程里同步执行**一次真实 LLM planner 调用**
（`self._make_workflow_planner()` + `self.workflow_controller.create_planned_run(...)`）。

后果：

- `workflow_run` 事件要等 planner 返回才 emit（`frontends/ink_bridge.py:731`），期间 UI 没有任何 workflow 信息。
- 这段时间内 JSONL loop 无法处理**任何**后续命令（`workflow_detail` / `workflow_progress` / `mcp_status` / `stop`）。
- 已有的正确写法就在同一个文件里：`_submit_recommended_workflow`（`frontends/ink_bridge.py:307`）
  把 planner 放进后台线程（`_workflow_planning_thread`），先 emit `activity: Planning workflow`。
  **`/workflow` 这条主路径漏掉了同样的处理**。

### R2（卡顿 / UI 不跟手）进度轮询每 0.25s 做全量重活

`_watch_workflow_progress`（`frontends/ink_bridge.py:1022`）每 0.25s 一轮，每轮做：

1. `self.workflow_store.load_run(run_id)`——本机实测 **11.8 ms/次**（读 `state.json` + `script.js`，
   走 `read_json_retrying`）；
2. `_workflow_artifact_payload(run, "workflow-progress.json")`——读盘 + `json.loads` + `sanitize`；
3. `json.dumps(..., sort_keys=True)` 全量序列化做去重比较；
4. 只要内容变化就 `self._emit_agent_read_model()` → `_emit_agent_events()` +
   `_emit_agent_snapshot()`（`list_records(include_terminal=True)` + 全量 `json.dumps` 指纹比较）
   再 `emit({"type": "workflow_progress", ...})`。

实测（本机，`wf_0ee3b2963d2a4062a96243546277b70e`）：

```
load_run              11.76 ms
read progress json     0.23 ms  (12828 bytes)
json.dumps 去重        0.18 ms
```

即**每个 tick 的固定成本 ≈12 ms，4 次/秒**，而其中绝大部分是"读一个没变过的 state.json"。
每一次 `workflow_progress` / `agent_event` / `agent_snapshot` 事件都会触发 Ink 侧一次完整
re-render（`App.tsx` 的 `dispatch` → 整树 render）。事件频率 × 单帧成本一起把输入回显挤掉了。

### R3（`j`/`k` 无效）按键路由把字母键让给了输入框

- `App.tsx`（`frontends/ink-ui/src/App.tsx:1395`）里状态条按键处理的条件是
  `showWorkflowStatusBar && workflowStatusBar && input.trim() === ''`；
- workflow 面板（overview 模式）的按键处理在 `workflowPanel.ts:507`，
  **只处理 `key.return`**，其余按键（含 `j`/`k`/上下/翻页）返回 `null`，于是穿透到 `handleInput`
  被当成普通字符写进输入框；
- 输入框一旦非空，状态条那一段也不再接管按键，`j`/`k` 就彻底"无效"；
- agent_detail 模式虽然实现了 `j`/`k`（`workflowPanel.ts:519-520`），但提示文案
  （`workflowPanel.ts:346`：`↑↓ agent · j/k scroll · esc back`）与真实可用键不完全一致，
  且必须先在 overview 里按 Enter 才能进入，用户很难自己发现。

### R4（看不到内容）详情面板被硬截断、且没有实时数据

- `workflowAgentDetailRows`（`frontends/ink-ui/src/workflowPanel.ts:334`）用
  `detailRows.slice(panel.scrollOffset)` 取"从偏移到结尾"的全部行，不做高度裁剪；
- `App.tsx`（`frontends/ink-ui/src/App.tsx:1480`）又对 `workflowPanelRows(...)` 取
  `Math.min(..., 8)`，**详情被硬截成 8 行**，滚动后既看不到完整 prompt/outcome，
  也没有"当前在第几行/共几行"的位置指示；
- `agentDetailRightRows`（`workflowPanel.ts:350`）只取 `toolCalls.slice(-3)`——**只显示最后 3 条工具调用**；
- `resolveAgentDetail`（`workflowPanel.ts:304`）的 `tokenText` 只在 agent 产出 `tokenUsage` 后才有值，
  **运行中没有实时 token**；
- 数据侧根本没有 turn 计数：`workflow_store._build_job_progress`（`workflow_store.py:453`）
  只在 job 结束时由 `write_agent_transcript`（`workflow_scheduler.py:621`）落盘 transcript，
  运行中的子 agent 在磁盘上**没有任何** turn/token 记录。

## 3. Codex 对照（为什么它不卡、进度为什么清楚）

参考实现：`D:\git_codes\codex\codex-rs\tui`。

| 维度 | Codex 做法 | 证据 | GA 现状 |
|---|---|---|---|
| 重绘调度 | 状态变化只 `schedule_frame()`，帧率由限流器统一钳到 ≤120 FPS，draw 合并 | `tui/src/tui/frame_requester.rs:49`、`tui/src/tui/frame_rate_limiter.rs`（`MIN_FRAME_INTERVAL = 8.33ms`） | 每个 bridge 事件直接 `dispatch` 一次全树 render，没有合并 |
| 可变 UI 状态 | 放在 `Arc<Mutex<ViewState>>`，不每帧重建 | `tui/src/app/agents_overview.rs`（`view_state: Arc<Mutex<...>>`） | `workflowStatusBarFromState` 每次 `state` 变就重算，`state` 每事件都换新对象 |
| usage 刷新 | **按需、只对选中任务、60s 一次**，`pending_usage` 防重入 | `tui/src/app/agents_overview_usage.rs:27`（`REFRESH_INTERVAL = 60s`） | 每 0.25s 全量重算所有 agent 的 token |
| 活动预览 | 有界：3 行 / 6 条 / 240 个字符，只从"已送达事件"派生，不拉历史 | `tui/src/app/agent_status_feed.rs`（`AGENT_STATUS_PREVIEW_LINES=3`、`_ITEMS=6`、`_GRAPHEMES=240`）、`agents_overview_details.rs`（`PREVIEW_CHARS=512`） | 全量 progress + 全量 agent snapshot |
| 列表导航 | `move_selection(forward)` 环绕、`visible_indices()` 过滤、`page_selection`（PgUp/PgDn）翻页、JumpTop/JumpBottom | `tui/src/app/agents_overview_view.rs:305/345/766` | overview 只认 Enter；无翻页、无首尾跳转 |
| 选择稳定性 | 遍历按"首次见到的 spawn 顺序"，thread 一出现就保持位置 | `tui/src/app/agent_navigation.rs`（`AgentNavigationState.order`） | 按 phase/label 拼装，运行中重排会跳 |
| 快捷键 | 集中在 keymap，支持搜索/分组切换，`Alt+←/→` 且带 word-motion 回退 | `tui/src/multi_agents.rs` | 单字母散落在 `App.tsx` 的 if 链里，且被输入框抢走 |

**可以直接借的三条**：

1. **事件→帧的节流/合并**：状态变化不应等于一次同步全量 render；至少要按 mtime/size 去重，
   并对"高频但低信息量"的通道（agent snapshot / usage）单独降频。
2. **有界预览**：预览/摘要类数据必须有硬上限（行数、条数、字符数），不允许把全量 progress 灌进 UI。
3. **导航是独立状态机**：选择项按稳定顺序持有，翻页/首尾/环绕都是显式动作，且键位集中在 keymap，
   不会因为"输入框是否为空"而整段失效。

## 4. 实施方案（分问题、分步骤）

### P1 —— `/workflow` 规划不再阻塞命令线程（对应 R1）

- `ink_bridge` 新增 `workflow_plan_async(...)`：做 busy 检查 → 置 `_workflow_planning = True` →
  立即 emit `status: running` + `activity: Planning workflow` → 起后台线程跑现有同步 `workflow_plan`
  （`_activation_internal=True`，与 `_submit_recommended_workflow` 同构）。
- `run_jsonl_loop` 的 `workflow_plan` 分支改调 `workflow_plan_async`；同步 `workflow_plan`
  保留给程序化调用与既有测试。
- 回归测试（红→绿）：用一个"慢 planner"（sleep）跑 `workflow_plan_async`，随后立刻发
  `mcp_status`，断言 `mcp_status` 的响应**先于** `workflow_run` 到达；同步路径下必然超时失败。

### P2 —— 进度推送按"真的变了"才发（对应 R2）

- `_watch_workflow_progress`：启动时 `load_run` **一次**并缓存 `artifact_dir`；
  之后每 tick 只 `os.stat` `workflow-progress.json`，mtime/size 都没变就 continue。
- 变化时才读盘/解析/emit；`_emit_agent_read_model()` 改为按需：
  有新的 `agent_event` 才发事件，`agent_snapshot` 额外限频（默认 ≥1s 且记录数变化才发）。
- 新增 `workflow_live` 通道（见 P4），与 `workflow_progress` 分开限频。
- 回归测试：用桩 store 统计 `load_run` 调用次数，断言"progress 文件未变时不再 load_run"。

### P3 —— 按键模型与提示文案（对应 R3）

- overview 模式支持：`↑/↓`、`j/k` 移动 phase，`PgUp/PgDn` 翻页，`g/G` 首尾，
  `Enter` 进入 agent 详情，`Esc` 关闭。
- workflow 面板打开时**独占字母键**（`j/k/g/G/x/r/s` 等不再落进输入框）。
- 提示文案与实现一致，并把"先 Enter 进入详情"写清楚。
- 回归测试：`workflowPanel.test.ts` 覆盖 overview 的 `j/k/PgDn/g/G`；
  `App.test.ts` 覆盖"面板打开时按 j 不写入输入框"。

### P4 —— 实时 turn / token / 工具（对应 R4 的数据侧）

- 子 agent 运行期写一份**轻量**的 `agents/<jobId>/live.json`（原子写、内部限频 ≤1s）：
  `{jobId, turn, toolCalls, lastToolName, lastToolSummary, tokenUsage, elapsedSeconds, updatedAt}`。
  数据来源：`handler.current_turn`（`agent_loop.py:211` 每轮赋值）+
  `handler.tool_before_callback/tool_after_callback`（`workflow_child_agent.py:646-658`）+
  `client.last_usage_tokens`。
- 宿主 `WorkflowStore.write_job_live_telemetry/read_job_live_telemetry` 负责读写；
  `WorkflowRuntime` 把 sink 注入 runner（`runner.set_telemetry_sink(...)`）。
- Ink bridge 新增 `{"type": "workflow_live", "runId", "jobs":[...]}` 事件（≤1s，只在变化时发）；
  UI 在 `state.liveTelemetry[runId][jobId]` 里保存，用于状态条与详情页显示
  `turn N · M tools · X tok`。
- **不变量**：`workflow-progress.json` 仍是唯一 durable 快照，live 通道只是 UI 观测，
  不参与任何验收判定。

### P5 —— 详情面板可用（对应 R4 的 UI 侧）

- `workflowAgentDetailRows` 改为按面板高度裁剪，并输出滚动位置（`12-19/40`）。
- 活动行从"最后 3 条"改为"最后 N 条（按面板高度）"，并带上 turn 序号。
- 状态条在运行中显示 `agent_1 · turn 3 · 12 tools · 45.2k tok`。

## 5. 验证

- Python：`python -m unittest tests.test_ink_bridge`、`tests.test_workflow_store`、`tests.test_workflow_scheduler`。
- Ink UI：`cd frontends/ink-ui && npm test` + `npm run typecheck`。
- 真实 E2E（可选，用本机 `llm.yaml` 的 default profile）：`/workflow` 一条中等难度任务，
  观察"回车后 1s 内出现 Planning workflow"、"运行中 turn/token 每秒更新"、"j/k 生效"。

## 6. 进度记录

见文末"实施进度"小节（逐步追加）。

## 7. 实施进度

全部 P1–P5 已在 `main` 上实施完成，未使用 worktree。

### P1 —— planner 移出 JSONL 命令线程（对应 R1/R2）

- `frontends/ink_bridge.py` 新增 `workflow_plan_async(...)` + `_run_planned_workflow(...)`：
  busy 检查 → 置 `_workflow_planning` → 立即 `emit` `status: running` + `activity: Planning workflow`
  → 后台线程跑原同步 `workflow_plan(_activation_internal=True)`。
- `run_jsonl_loop` 的 `workflow_plan` 分支改调 `workflow_plan_async`，stdin 循环不再被 planner
  的一次真实 LLM 调用阻塞。
- 回归：`tests/test_ink_bridge.py::test_jsonl_loop_dispatches_workflow_plan_command`（断言改为
  `workflow_plan_async`）、`test_jsonl_loop_does_not_block_on_workflow_planning`（慢 planner 0.6s，
  断言 loop 耗时 < 0.3s 且 `mcp_status` 先于 `plan_done`）。

### P2 —— 进度轮询降负载（对应 R1）

- 新增模块级 `_file_signature(path)`（mtime_ns+size）与 `_read_json_object(path)`。
- `_watch_workflow_progress` 重写：启动时只 `load_run` 一次拿 `artifact_dir`，之后每 tick 只 `os.stat`
  `workflow-progress.json`，签名未变即跳过（原来每 0.25s 都 `load_run` ≈11.8ms + 全量 `json.dumps`）。
- `_emit_agent_read_model()` 限频 `_AGENT_READ_MODEL_MIN_INTERVAL = 1.0` 秒。
- 回归：`test_progress_watcher_does_not_reload_the_run_when_progress_is_unchanged`、
  `test_progress_watcher_publishes_live_child_telemetry`。

### P4 —— 子 agent 运行期 live 遥测（对应 R4 数据侧）

- `workflow_store.py`：`live_telemetry_path` / `write_job_live_telemetry` / `read_job_live_telemetry`
  （写 `agents/<jobId>/live.json`，原子写，仅 UI 观测通道）。
- `workflow_child_agent.py`：`LiveJobTelemetry`（sink、min_interval=1.0s、fingerprint 去重、`force`）；
  `NativeGPTChildAgentRunner.set_telemetry_sink()`；`_run_tool_job` 里 `handler.live_telemetry = live`；
  `_live_update(...)` 由 `tool_before_callback`/`tool_after_callback` 调用。
  **`_build_handler` 签名保持 `(job, transcript_events, profile, version)` 不变**——writer 挂在
  handler 属性上，因为测试替身会替换 `_build_handler`。
- `workflow_runtime.py`：`_wire_runner_telemetry(run)` 在构造 `AgentScheduler` 前把 store 写盘 sink
  注入 runner。
- payload：`{jobId, turn, toolCalls, lastToolName, lastToolSummary, tokenUsage, elapsedSeconds, updatedAt}`。
- 回归：`tests/test_workflow_child_agent.py::LiveJobTelemetryTest`（5 例）、
  `tests/test_workflow_runtime.py::RunnerTelemetryWiringTest`（2 例）。

### P3 / P5 —— UI 侧按键模型与详情面板（对应 R3/R4 UI 侧）

- `protocol.ts`：新增 `WorkflowLiveJob` 类型与 `workflow_live` 事件。
- `state.ts`：新增 `liveTelemetry[runId][jobId]`；`workflow_live` handler 在 run 未知时忽略（不复活旧 run）。
- `workflowPanel.ts`：`WORKFLOW_PANEL_MAX_ROWS=14`、`ACTIVITY_TAIL_ROWS=12`；overview 支持
  `↑/↓ j/k`、`PgUp/PgDn`、`g/G`、`Enter` 进详情、`Esc` 返回；详情按高度裁剪 + 位置指示；
  新增 `workflowPanelCapturesKey(...)` 让面板独占字母键。
- `workflowStatusBar.ts`：运行中显示 `turn N · M tools · X tok`（token 优先取 live）。
- `App.tsx`：`workflow_live` 分支 `dispatch` + `setWorkflowPanel(workflowPanelWithLive(...))`；
  workflow 面板高度不再硬截断为 8 行；按键路由末尾 `if (workflowPanelCapturesKey(...)) return`。
- 回归：`workflowPanel.test.ts`（新增 4 例）、`workflowList.app.test.ts`
  `App keeps workflow panel keys out of the composer`。

### 验证命令与结果（本轮实测）

- `python -m unittest tests.test_ink_bridge tests.test_workflow_child_agent tests.test_workflow_store
  tests.test_workflow_runtime tests.test_workflow_scheduler tests.test_workflow_integration
  tests.test_workflow_controller tests.test_agent_control_workflow`
  → **Ran 363 tests, OK**。
- `cd frontends/ink-ui && npx tsc --noEmit` → 干净。
- `cd frontends/ink-ui && npm test` → **404 passed / 0 failed**。

### 关键实测数据（回归依据）

- 旧 `_watch_workflow_progress` 单 tick：`load_run` 11.76ms + read progress json 0.23ms（12828B）
  + `json.dumps` 去重 0.18ms；4 次/秒 → 输入框卡顿。
- 真实 run `wf_0ee3b2963d2a4062a96243546277b70e`：`workflow-progress.json` 12828B、2 entries。

### 不变量（不要回退）

1. planner 不得在 JSONL stdin 线程内同步执行。
2. `workflow-progress.json` 仍是唯一 durable 快照；`live.json` / `workflow_live` 只服务 UI 观测，
   不参与任何验收判定。
3. workflow 面板打开时字母键归面板所有，不得落进输入框。

## 8. 真实运行观测（2026-10-10）

用桥接层真实探针 `frontends/ink-ui/scripts/_probe_workflow_progress.ts` 跑了一次真实
`/workflow`（profile `cc-deepseek-v4.1-flash-chat`，真实 Tavily MCP，真实 planner），
量测三个用户现象。探针在 `ready` 后立即发 `workflow_plan`，并**紧接着**再发一条
`mcp_status` 命令，用"第二条命令何时被处理"来证明命令线程没有被规划阻塞。

| 观测量 | 值 | 说明 |
|---|---|---|
| `ready` | 2.0 s | bridge 就绪 |
| `mcp_ready` | 82.1 s | MCP 发现完成（见下方遗留问题 1） |
| `planSentToFirstStatusMs` | **1 ms** | 发出 `workflow_plan` 后 1ms 即出现 `status: running`（现象 2 修复） |
| `planSentToFirstActivityMs` | **1 ms** | 1ms 即出现 `activity: Planning workflow`，用户立刻看到反馈 |
| 第二条命令返回 | 发计划后 **10 ms** | 规划进行中，命令线程仍能处理下一条命令（现象 1 修复：不再阻塞） |
| `workflow_progress` 事件数 | 33 | 进度持续推送（P2） |
| `workflow_live` 事件数 | 12 | 子代理实时遥测持续推送（P4） |
| `maxLiveTurn` | 3 | live 里的 turn 计数随执行推进 |
| `returnedToIdle` | true | `workflow_final` 后 58.6 s 回到 idle（这 58.6 s 是 host 把结果交给 agent 循环、由 LLM 生成最终答复的时间） |
| `planSentToWorkflowFinalMs` | 299.1 s | 本次 run 的端到端时长 |

**结论**：现象 1（输入框卡顿）与现象 2（回车后迟迟不显示子代理）的根因已被 P1+P2 消除，
有 1 ms / 10 ms 的硬证据；现象 3 的"实时进度"由 `workflow_progress`(33) + `workflow_live`(12)
共同提供，`turn` 计数真实推进。

### P4 token 字段的说明

本次 live 样本的 `tokenUsage` 为 `{}`。经核查这**不是**遥测通道的问题，而是本次运行里
三个子代理的 LLM 调用全部以 `!!!Error: SSLError` 结束（每个 3 轮、每轮重试 4 次仍失败），
没有任何一次 LLM 响应真正完成，自然没有 usage 可记。用真实 client 单独验证：

```python
_live_usage(make_tool_client(binding_from_profile('cc-deepseek-v4.1-flash-chat')))
# -> {'input_tokens': 102, 'output_tokens': 313, 'total_tokens': 415, ...}
```

即 `_live_usage` 能从真实 client 的 backend 取到规范化 usage；`LiveJobTelemetry` 单测
（`tests/test_workflow_child_agent.py::LiveJobTelemetryTest`）也断言 payload 携带
`tokenUsage`。所以通道正确，本次空值是上游瞬时故障的连带结果。

### 本次暴露的、不在本 PR 范围的遗留问题

1. **MCP 冷启动耗时**：`ready`→`mcp_ready` 用了 82 s。GA 已按 Codex 的做法在输入框下方
   显示 MCP 连接状态，但"等 MCP 全部就绪"这一前置条件本身太长，是后续单独一轮的优化项。
2. **子代理 LLM `SSLError`**：本次 workflow 终态 `failed`，原因是 3 个 child 的 LLM 调用
   全部 SSL 失败并耗尽重试，子代理把错误文本当答案返回，导致 `verify` 阶段失败。属上游
   瞬时网络故障 + 子代理重试预算问题，与 P1–P5 无关，但会让真实 E2E 偶发失败。
3. **E2E harness 的 idle 断言**：`real_ink_ui_workflow_autonomous_e2e.ts` 原先在
   `workflow_final` 到达的同一瞬间断言 `state.status === 'idle'`。由于 host 会把 workflow
   结果交给 agent 循环续写最终答复（这正是用户此前要求的行为），idle 会晚到几十秒。
   已改为 `waitFor(idle, 180s)` 再断言，避免把正确的产品行为判成失败。

