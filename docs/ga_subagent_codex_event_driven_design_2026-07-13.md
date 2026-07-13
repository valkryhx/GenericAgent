# GA Subagent 事件驱动设计：借鉴 Codex，但保留 GA 的轻量实现

## 先说结论

Codex 调 subagent 也有 prompt。它的 `spawn_agent.message` 就是给子智能体的初始任务说明。

关键差异不在“有没有 prompt”，而在 prompt 之后的机制：

- Codex 把 prompt 封装成 `InterAgentCommunication`，投递到子 agent 的输入队列，并用 `trigger_turn=true` 触发子 agent 开始工作；
- 子 agent 完成时，最终回答进入 `AgentStatus::Completed(Some(final_message))`；
- 子 agent 会主动把完成通知推回父 agent 的 mailbox；
- `wait_agent` 只等待 mailbox/status 变化，不负责反复读取子进程输出文件；
- 父 agent 看到的是结构化子 agent 通知，而不是自己去猜 `output.txt`、`[ROUND END]`、PID 是否还活着分别意味着什么。

GA 后续优化应优先补这个控制面，而不是只优化 subagent prompt。prompt 仍然要存在，但它应该只是任务输入；生命周期、完成状态、最终结果、artifact 和中断状态必须由结构化事件来承载。

## 为什么现在需要改

今天这轮 GA 测试暴露了两个不同层面的故障。

`docx_report` 是真实失败：任务同时要求联网核实和生成 docx，子 agent 被搜索核实牵走，没进入 docx 创建动作。父端中断后，worker 没有立即进入 `interrupted`，而是继续等队列里的 `done`，最后 300 秒超时，变成 `_queue.Empty`。

`msft_stock` 更复杂：模型后端实际生成过中文结论，但 `output.txt` 只留下工具调用轨迹和空的 `Turn 7 ... [ROUND END]`。父端读结果文件时看不到干净结论，于是判断“MSFT无结论，父进程核价兜底”。

这说明 GA 当前机制有三个根本问题：

1. 父端把 `output.txt` 当作结果权威来源，但它实际只是一个人类可读的运行轨迹/快照。
2. `[ROUND END]` 被当成完成标志，但它不能证明最终回答有效，也不能表达 interrupted、errored、artifact-only completed 等状态。
3. 中断、完成、等待 reply、进程退出都混在文件和 PID 状态里，父端只能猜。

Codex 的做法能提供更好的方向：prompt 可以继续是自然语言，但结果和生命周期必须事件化、结构化、由子端主动推送。

## Codex 做法的关键点

### 1. prompt 仍然存在，但被包装成 inter-agent message

Codex MultiAgentV2 的 `spawn_agent` 需要 `message` 和 `task_name`。

`message` 不是被当成 loose text 写入某个 `input.txt` 后就结束，而是转换成初始 `Op`。当目标是普通文本任务时，它会被包装成 `InterAgentCommunication`：

```text
author         父 agent path
recipient      子 agent path
content        message
trigger_turn   true
```

这意味着 prompt 是正式消息，不只是启动参数。

### 2. wait 不读取最终内容

Codex v2 的 `wait_agent` 只订阅 mailbox 变化。它的返回值类似：

```json
{
  "message": "Wait completed.",
  "timed_out": false
}
```

它刻意不把子 agent 的完整回答塞进 `wait_agent` 返回值里。完成内容是通过子 agent 的完成通知进入父 agent 的 mailbox，再进入父 agent 后续上下文。

这能避免两个问题：

- 父 agent 为了看进度反复 poll；
- 大段子结果在工具返回中重复出现，污染上下文。

### 3. completed 状态携带 final message

Codex 的 `AgentStatus` 里有：

```text
Completed(Option<String>)
```

这个 `String` 来自 `TurnCompleteEvent.last_agent_message`，也就是模型最终回答。它不是父端事后从 transcript 或 stdout 里猜出来的。

这点正好对应 GA 的 `msft_stock` 问题：后端真实 final answer 和 `output.txt` 文件内容出现偏差时，父端需要一个更权威的 final answer 字段。

### 4. 子 agent 完成时主动通知父 agent

Codex 的 spawned child 进入 `TurnComplete` 或 `TurnAborted` 后，会调用类似 `forward_child_completion_to_parent()` 的逻辑，把完成状态包装成子 agent notification，发给父线程。

完成通知使用 `trigger_turn=false`。这很关键：

- 它会唤醒父端等待；
- 但不会强行让父 agent 开新轮；
- 父 agent 在自己的执行节奏里消费这个通知。

GA 现在应该借鉴这个语义，而不是继续让父端“主动查岗”。

## GA 的目标设计

### 总体目标

把 GA subagent 从：

```text
spawn 子进程
父端轮询 output.txt / state.json
靠 [ROUND END] 和 PID 猜状态
需要时读 output.txt 汇总
```

改成：

```text
spawn 子进程
父端发送结构化任务消息
子端运行并产出权威 final_text / final_artifacts
子端主动写 parent inbox 通知父端
父端 wait_agent 只等通知
父端根据通知做 reduce 或 fallback
```

实现上仍然可以用文件。GA 不需要立刻做 Rust/Codex 那种内存级 async thread runtime。短期最务实的方案是：文件继续作为跨进程 transport，但文件内容必须升级为结构化 event/message，而不是让父端读人类输出文本。

### 非目标

这次设计不要求：

- 重写 GA agent loop；
- 废弃 `input.txt`、`output.txt`、`reply.txt`；
- 一次性实现真正 IPC/socket；
- 完全复制 Codex thread manager；
- 让 `wait_agent` 自动总结或压缩子任务全文；
- 让 prompt 自动解决所有任务边界问题。

旧协议可以保留，但新语义要逐步迁移到结构化控制面。

## 新控制面的核心概念

### AgentPath

每个 subagent 都有 canonical path：

```text
/root
/root/msft_stock
/root/docx_report
/root/research/france
```

短期 GA 可以继续只支持一级：

```text
/root/{task_name}
```

但数据结构应预留层级路径。这样后续子 agent 再 spawn 子 agent 时不需要重构。

### InterAgentMessage

新增统一消息结构，用于 spawn 初始任务、send_message、followup_task、完成通知。

```json
{
  "schema_version": 1,
  "id": "msg_20260713_000001",
  "type": "task",
  "author": "/root",
  "recipient": "/root/msft_stock",
  "content": "用中文检查微软 MSFT 近期股价表现...",
  "trigger_turn": true,
  "created_at": "2026-07-13T14:02:36+08:00"
}
```

`type` 建议先支持：

- `task`：触发一轮工作的任务消息；
- `message`：只入队，不触发新 turn；
- `notification`：子端发给父端的状态通知；
- `control`：stop/interruption/shutdown 等控制信号。

`trigger_turn` 的语义借鉴 Codex：

- `true`：目标 agent 空闲时应启动/继续一轮；
- `false`：只投递消息，不强行启动目标 agent。

### AgentStatus

GA 当前已经有 `turn_status` 和 `process_status`。建议保留这两个字段，同时增加更接近 Codex 的 `agent_status` 派生视图。

```json
{
  "agent_status": {
    "completed": "最终中文结论..."
  }
}
```

或：

```json
{
  "agent_status": "running"
}
```

建议映射：

- `pending_init`
- `running`
- `interrupted`
- `completed`
- `errored`
- `shutdown`
- `not_found`

其中 `completed` 必须能关联最终内容：

```json
{
  "turn_status": "completed",
  "process_status": "waiting_reply",
  "final_text": "最终答案正文",
  "final_artifacts": []
}
```

如果 `final_text` 为空，但存在有效 artifact，也可以 completed，但状态必须明确是 artifact completed：

```json
{
  "turn_status": "completed",
  "completion_kind": "artifact",
  "final_text": "已生成文件：D:\\git_codes\\GenericAgent\\temp\\outputs\\x.docx",
  "final_artifacts": [
    {
      "path": "D:\\git_codes\\GenericAgent\\temp\\outputs\\x.docx",
      "kind": "docx",
      "exists": true,
      "size": 37216
    }
  ]
}
```

这能避免 docx 任务“模型说完成但文件不存在”，也能避免文本任务“只有 `[ROUND END]` 但没有答案”。

## 文件布局

保留现有布局：

```text
temp/{task_name}/
  input.txt
  output.txt
  output1.txt
  reply.txt
  mailbox.jsonl
  events.jsonl
  state.json
  stdout.log
  stderr.log
```

新增或强化：

```text
temp/subagents/
  registry.json
  inbox.jsonl
```

### `temp/{task_name}/mailbox.jsonl`

这是发给该 subagent 的消息队列。

父端 `spawn_agent` 写入初始 task message；`followup_task` 写入 task message；`send_message` 写入 message。

为了兼容当前 worker，`followup_task(trigger_turn=true)` 可以继续同步写 `reply.txt`，但 `mailbox.jsonl` 应成为权威消息源。

### `temp/subagents/inbox.jsonl`

这是发给父端的消息队列。

子端完成、报错、中断、artifact 生成后，都向 parent inbox 追加 notification。

示例：

```json
{
  "schema_version": 1,
  "type": "subagent_notification",
  "author": "/root/msft_stock",
  "recipient": "/root",
  "task_name": "msft_stock",
  "agent_path": "/root/msft_stock",
  "status": {
    "completed": "截至 2026-07-10，MSFT 收于 385.10 美元..."
  },
  "result_ref": {
    "state_path": "D:\\git_codes\\GenericAgent\\temp\\msft_stock\\state.json",
    "output_path": "D:\\git_codes\\GenericAgent\\temp\\msft_stock\\output.txt"
  },
  "created_at": "2026-07-13T14:06:33+08:00"
}
```

如果 final text 很长，可以在 inbox 里放截断版，同时 `state.json` 放完整或指向完整文件：

```json
{
  "status": {
    "completed": "长结果前 4000 字..."
  },
  "truncated": true,
  "result_ref": {
    "final_text_path": "D:\\git_codes\\GenericAgent\\temp\\msft_stock\\final.txt"
  }
}
```

## 状态文件设计

`state.json` 是程序判断状态的权威快照。

建议结构：

```json
{
  "schema_version": 2,
  "task_name": "msft_stock",
  "agent_path": "/root/msft_stock",
  "parent_agent_path": "/root",
  "parent_session_id": "session_c8f8baadc0274ac0a1c026bc31833abc",
  "pid": 7844,
  "round": 0,
  "turn_status": "completed",
  "process_status": "waiting_reply",
  "completion_kind": "text",
  "started_at": "2026-07-13T14:02:36+08:00",
  "updated_at": "2026-07-13T14:06:33+08:00",
  "last_round_started_at": "2026-07-13T14:02:37+08:00",
  "last_round_end_at": "2026-07-13T14:06:33+08:00",
  "last_task_message": "用中文检查微软（MSFT）近期股价表现...",
  "input_message_id": "msg_...",
  "input_path": "D:\\git_codes\\GenericAgent\\temp\\msft_stock\\input.txt",
  "output_path": "D:\\git_codes\\GenericAgent\\temp\\msft_stock\\output.txt",
  "raw_output_path": "D:\\git_codes\\GenericAgent\\temp\\msft_stock\\output.txt",
  "final_text": "截至我能联网核实的最新公开数据...",
  "final_text_path": null,
  "final_output_path": "D:\\git_codes\\GenericAgent\\temp\\msft_stock\\output.txt",
  "final_output_sha256": "1151eca9...",
  "final_artifacts": [],
  "last_error": null,
  "close_reason": null
}
```

字段约束：

- `turn_status=completed` 时，必须至少满足一个条件：
  - `final_text.trim()` 非空；
  - `final_artifacts` 至少一个有效文件；
  - `completion_kind=empty_ack`，且任务类型明确允许空结果。
- `process_status=waiting_reply` 不代表任务没完成，只代表 worker 进程保活等待 followup。
- `final_output_path` 兼容旧逻辑，仍指向带 `[ROUND END]` 的原始 output 文件。
- `final_text` 是父 agent 消费的权威答案；`raw_output_path` 是排查用轨迹。
- `last_error` 不应把用户中断误记为 `_queue.Empty`；中断应进入 `interrupted`。

## 子 agent 执行流程

### spawn

父端调用：

```text
spawn_agent(task_name="msft_stock", message="用中文检查微软...")
```

GA manager 执行：

1. 校验 `task_name`；
2. 创建 `/root/{task_name}`；
3. 写 `state.json` 为 `pending_init/starting`；
4. 写 `mailbox.jsonl` 初始 task message；
5. 为兼容旧 worker，同步写 `input.txt`；
6. 启动 worker；
7. 写 `registry.json`；
8. 写 `agent_started` 到 `events.jsonl` 和 parent inbox。

### running

worker 执行期间：

- `output.txt` 继续保存可读轨迹；
- `events.jsonl` 写 `turn_started`、`output_snapshot` 等轻量事件；
- 不要求父端读 `output.txt` 判断进度；
- 父端如需等，只用 `wait_agent` 等 parent inbox 或状态事件。

### completion

worker 收到模型 `done` 后：

1. 从 `done` 里提取完整 assistant final text；
2. 写 `raw_output_path`，保留工具轨迹和 `[ROUND END]`；
3. 写 `final_text` 或 `final_text_path`；
4. 扫描/校验 `final_artifacts`；
5. 如果 `final_text` 为空且无 artifact，标记 `errored` 或 `completed_empty_invalid`，不直接 completed；
6. 写 `state.json`；
7. 写 `turn_completed` 到 `events.jsonl`；
8. 写 `subagent_notification` 到 `temp/subagents/inbox.jsonl`；
9. 进入 `waiting_reply` 或按策略退出。

### interrupt

父端调用：

```text
interrupt_agent(target="docx_report", reason="长时间未生成docx")
```

新语义：

1. 写 control message 到 `mailbox.jsonl`；
2. 写 `_stop` 兼容旧 worker；
3. 立即把 state 更新为 `turn_status=interrupted` 或 `interrupt_requested`；
4. worker 观察到 stop 后调用 `abort()`；
5. worker 不再继续等 `dq.get(timeout=300)`；
6. 如果已有 partial output，保存 `partial_output_path`；
7. parent inbox 收到 interrupted notification。

不再允许“用户中断后 300 秒队列超时，最后变成 `_queue.Empty`”这种状态。

## 父 agent 工具语义

### `spawn_agent`

保留现有工具名。

输入：

```json
{
  "task_name": "msft_stock",
  "message": "用中文检查微软（MSFT）近期股价表现...",
  "fork_turns": "none"
}
```

输出只返回 handle：

```json
{
  "task_name": "/root/msft_stock",
  "status": "started"
}
```

不要返回长输出。

### `wait_agent`

职责：等待 parent inbox 有新消息或目标状态变化。

返回：

```json
{
  "timed_out": false,
  "message": "Wait completed.",
  "updates": [
    {
      "type": "subagent_notification",
      "agent_path": "/root/msft_stock",
      "status": "completed"
    }
  ]
}
```

原则：

- 默认不返回完整 final text；
- 可以返回短 preview，但不要让模型把 wait 当成 read；
- 如果工具系统能把 inbox notification 注入下一轮上下文，优先用注入方式；
- 如果当前 GA 暂时做不到上下文注入，则 `wait_agent` 可以返回结构化通知块，但仍避免返回 raw output。

### `read_agent_result`

短期保留，但语义调整为“读取权威 final result”，不是“读取 output.txt”。

返回：

```json
{
  "status": "success",
  "agent": {
    "agent_path": "/root/msft_stock",
    "turn_status": "completed",
    "completion_kind": "text",
    "final_text": "截至我能联网核实的最新公开数据...",
    "final_artifacts": []
  }
}
```

如果 `final_text` 缺失但 `output.txt` 有 `[ROUND END]`，应返回：

```json
{
  "status": "invalid_result",
  "reason": "completed marker exists but final_text is empty",
  "agent": {
    "agent_path": "/root/msft_stock",
    "raw_output_path": "..."
  }
}
```

这能直接防住 `msft_stock` 这类“状态 completed，但父端读不到有效答案”的问题。

### `send_message` 与 `followup_task`

借鉴 Codex 区分：

- `send_message`：`trigger_turn=false`，只入队；
- `followup_task`：`trigger_turn=true`，目标空闲时触发下一轮。

GA 当前已有类似工具，但实现仍要向 mailbox 语义收敛。

## 对 prompt 的定位

prompt 仍然重要，但它不是可靠性的核心。

建议把 subagent prompt 分成两层：

1. 任务 prompt：父 agent 写给子 agent 的自然语言任务；
2. 系统 envelope：GA 自动附加的结构化约束。

例如文件生成任务，父 agent 的自然语言可能是：

```text
生成一个简短 docx 报告并保存到指定路径，最终返回文件路径。
```

GA 自动附加的 envelope 应包含：

```text
必须产出 final_text。
如果创建文件，必须在 final_artifacts 中登记 path/kind/exists/size。
如果无法完成，必须返回 errored(reason)，不要空 completed。
```

这样可以减少父 agent 每次都把“你必须怎样收尾”写进 prompt 的负担。

换句话说：prompt 可以简单，收尾协议必须强。

## Artifact 任务设计

docx、xlsx、图片、PDF 这类任务不能只靠最终文本判断。

建议 worker 完成时扫描显式输出路径，写入：

```json
{
  "final_artifacts": [
    {
      "path": "D:\\git_codes\\GenericAgent\\temp\\outputs\\gpt52_msft_brief.docx",
      "kind": "docx",
      "exists": true,
      "size": 37216,
      "sha256": "..."
    }
  ]
}
```

completion 判定：

- 文本调研任务：必须有 `final_text`；
- 文件生成任务：必须有 `final_artifacts[*].exists=true`，并有简短 `final_text` 说明；
- 混合任务：两者都要有；
- 只有工具轨迹、没有 final text、没有 artifact：不能 completed。

这能防住 `docx_report` 的问题：它可以搜索很久，但只要没生成 docx，就不能 completed；如果被中断，也应明确 interrupted，而不是 errored queue timeout。

## 中断和超时设计

### 当前问题

`agentmain.py` 当前 worker 在：

```python
while 'done' not in (item := dq.get(timeout=300)):
```

这里等待 `done`。如果父端写 `_stop`，agent loop 内部可能 abort，但 worker 层仍可能等不到队列 item，最后 `_queue.Empty`。

### 新设计

worker 等待队列时应使用短 timeout 循环：

```text
每 0.5s 检查：
  - dq 是否有 item
  - 是否收到 stop/control message
  - 子模型是否已取消
  - 是否超过 turn timeout
```

收到 stop 后：

1. 调用 `agent.abort()`；
2. 保存 partial output；
3. 写 `turn_status=interrupted`；
4. 写 parent inbox notification；
5. 不把它记成 error。

真正 error 只用于：

- backend 报错；
- worker 崩溃；
- 文件写入失败；
- 模型完成但 final_text/artifact 无效；
- 明确超出任务级 timeout。

## Result capture 设计

GA 必须区分三类文本：

1. `raw_output`：给人排查看的完整运行轨迹，包括 summary、tool call、tool result、final answer、`[ROUND END]`；
2. `final_text`：给父 agent reduce 的权威答案；
3. `notification_text`：写入 parent inbox 的完成通知，可能是 `final_text` 的截断版。

`msft_stock` 的问题就是这三者混在一起了。后端 final answer 存在于 model response log，但 `output.txt` 没保留干净答案。新设计要求在模型 `done` 到达时立刻捕获 `final_text`，并把它写入 state 或 final 文件。

最小实现可以先这样：

- `item['done']` 原样写 `raw_output_path`；
- 同时把 `item['done']` 去掉运行轨迹包装后写 `final_text`；
- 如果 `item['done']` 为空，但 `full_resp` 非空，用 `full_resp` 兜底；
- 如果两者都为空，不写 completed。

更稳的实现是让 agent loop 在最终回答阶段产出一个明确字段，例如：

```json
{
  "final_text": "...",
  "assistant_turn_text": "...",
  "tool_trace_text": "..."
}
```

但这需要更深地改 runner，第一阶段可以先在 worker 层修。

## 父端 reduce 流程

新推荐流程：

```text
1. 父 agent spawn 多个 subagent
2. 父 agent 做本地非重叠工作
3. 父 agent 调 wait_agent 等通知
4. wait_agent 返回某 agent completed/errored/interrupted
5. 父 agent 对 completed agent 调 read_agent_result
6. 父 agent 对 errored/interrupted agent 判断是否 fallback、重派或报告失败
7. 父 agent 汇总
```

如果后续 GA 能做到 Codex 那样把 parent inbox notification 自动注入模型上下文，则第 5 步可以减少：

```text
wait_agent 只唤醒
notification 自带 completed(final_text)
父 agent 直接 reduce
需要原始输出时再 read_agent_result
```

短期为了兼容当前工具循环，可以先保留 `read_agent_result`，但它必须读取 `final_text`，不是读取 raw `output.txt`。

## 和现有实现的兼容策略

### 保留 `[ROUND END]`

`[ROUND END]` 仍保留给旧脚本和人工排查。

但新逻辑中它只能作为兼容信号：

- 有 `[ROUND END]` 可以推测一轮结束；
- 不能单独证明结果有效；
- 不能覆盖 `state.json` 中的 `errored/interrupted`；
- 不能代替 `final_text`。

### 保留 `output.txt`

`output.txt` 继续作为 raw transcript。

但父端默认不要再读它做业务判断。业务判断走：

- `state.json`
- `parent inbox`
- `final_text`
- `final_artifacts`

### 保留 `reply.txt`

`reply.txt` 可以作为 `followup_task(trigger_turn=true)` 的兼容触发器。

但权威消息仍写入 `mailbox.jsonl`，并带 `trigger_turn=true`。

### 保留 `read_agent_result`

保留，但语义升级：

- 默认读 `final_text`；
- 必要时提供 `include_raw_output=true` 调试参数；
- 不再把 raw output 当作 final result。

## 分阶段落地建议

### Phase 1：补 final_text 和 completion validation

目标：先解决 `msft_stock` 这类结果捕获问题。

改动：

- worker 完成时写 `final_text`；
- `read_agent_result` 优先返回 `final_text`；
- 如果 `[ROUND END]` 存在但 `final_text` 为空，返回 `invalid_result`；
- 测试覆盖“只有工具轨迹没有答案不能 success”。

这是最小高收益改动。

### Phase 2：修 interrupt 语义

目标：解决 `docx_report` 这类中断后变 `_queue.Empty` 的问题。

改动：

- `interrupt_agent` 写 state 为 `interrupt_requested`；
- worker 用短 timeout 循环检查 `_stop`；
- stop 后写 `interrupted`；
- parent inbox 收到 interrupted；
- `_queue.Empty` 只作为真正异常，不作为正常中断结果。

### Phase 3：parent inbox 变成主通道

目标：让父端不再主动查 output。

改动：

- 所有 terminal event 都写 `temp/subagents/inbox.jsonl`；
- `wait_agent` 优先等 inbox；
- `wait_agent` 不返回 raw output；
- 工具 schema 和 SOP 明确禁止把 wait 当 read。

### Phase 4：artifact registry

目标：让 docx/xlsx/pdf/image 任务可验证。

改动：

- 支持 `final_artifacts`；
- 文件存在、大小、sha256 校验；
- artifact-only 任务可 completed；
- 文件缺失则 errored/invalid_result。

### Phase 5：自动注入 subagent notification

目标：更接近 Codex。

改动：

- parent inbox 消息被消费后，以 `<subagent_notification>...</subagent_notification>` 形式注入父 agent 下一次模型输入；
- `wait_agent` 只返回 “Wait completed”；
- 父 agent 在上下文里看到 completed(final_text)；
- 长结果用 `result_ref`，避免上下文爆炸。

这一阶段能真正从“显式 read_agent_result reduce”走向 Codex 的 push 模式。

## 需要修改的主要文件

这不是实施计划，只列设计上会涉及的边界。

### `agentmain.py`

负责 worker 生命周期：

- queue wait 改短 timeout 循环；
- 捕获 `final_text`；
- 写 `final_artifacts`；
- 中断写 `interrupted`；
- 完成时写 parent inbox notification。

### `subagent_state.py`

负责底层文件协议：

- atomic write state；
- append event；
- append parent inbox；
- message id；
- final text/artifact helper；
- Windows 下 `os.replace` 重试继续保留。

### `subagent_manager.py`

负责父端控制面：

- spawn 写 InterAgentMessage；
- wait 监听 inbox；
- read result 读 final_text；
- interrupt 写 control message；
- list 展示 agent_status；
- close 做 graceful shutdown。

### `ga.py`

负责工具语义：

- `wait_agent` 不读 raw output；
- `read_agent_result` 返回 final_text/final_artifacts；
- 工具输出中区分 `invalid_result`；
- anchor prompt 或 next prompt 可以注入 pending subagent notifications。

### `memory/subagent.md`

更新 SOP：

- 不再鼓励父 agent 读 output 查岗；
- 推荐 spawn → wait notification → read final result；
- 明确 `waiting_reply` 不是 running；
- 明确文件任务必须校验 artifact。

### `assets/tools_schema*.json`

更新工具描述：

- `wait_agent` 是事件等待；
- `read_agent_result` 是显式读取权威结果；
- `send_message` 与 `followup_task` 区分 trigger_turn。

## 测试设计

至少需要这些回归测试。

### final_text 捕获

输入：fake agent 返回最终答案。

期望：

- `state.json.final_text == "最终答案"`；
- parent inbox 有 completed notification；
- `read_agent_result` 返回 final_text；
- raw output 仍有 `[ROUND END]`。

### 空 completed 不应 success

输入：fake agent 最终 `done` 为空，但 output 有工具轨迹和 `[ROUND END]`。

期望：

- 不返回 `status=success`；
- `read_agent_result` 返回 `invalid_result`；
- parent inbox 标记 invalid/errored；
- 父端不会误以为子任务成功。

### interrupt 不应变 `_queue.Empty`

输入：worker 正在等待模型输出，父端 interrupt。

期望：

- state 进入 `interrupted`；
- parent inbox 有 interrupted notification；
- stderr 不出现 `_queue.Empty` 作为最终错误；
- process 可 graceful exit 或 waiting followup，但语义明确。

### artifact completed

输入：子 agent 创建 docx 文件并返回路径。

期望：

- `final_artifacts[0].exists == true`；
- `size > 0`；
- `completion_kind == "artifact"` 或 `"text_and_artifact"`；
- `read_agent_result` 返回 artifact 信息。

### artifact missing

输入：子 agent 声称生成 docx，但文件不存在。

期望：

- `read_agent_result` 不返回 success；
- state 标记 `invalid_result` 或 `errored`；
- parent notification 明确文件缺失。

### wait 不读 raw output

输入：子 agent output 很长。

期望：

- `wait_agent` 返回通知摘要；
- 不包含完整 raw output；
- `read_agent_result` 才返回 final_text；
- raw output 只在显式调试参数下返回。

## 设计取舍

### 为什么不马上做真正 IPC

GA 当前是 Python 子进程 + 文件协议。直接引入 socket/async bus 会扩大改动面，也会增加 Windows 兼容风险。

用 JSONL 文件做 mailbox/inbox 可以先拿到事件驱动语义：

- 跨进程可靠；
- 易排查；
- 易测试；
- 保留现有 temp 目录习惯；
- 后续可以替换 transport，不影响上层工具语义。

### 为什么还保留 `read_agent_result`

Codex v2 的理想形态是完成通知进入父 agent 上下文，`wait_agent` 不返回内容。

GA 目前工具循环和上下文注入机制还不完全等价。短期如果完全去掉 `read_agent_result`，父 agent 可能拿不到完整子结果。所以先保留它，但语义改成读取 `final_text/final_artifacts`。

等 Phase 5 实现 notification 注入后，`read_agent_result` 可以退化成调试/长结果读取工具。

### 为什么不能只靠 prompt

prompt 能减少子 agent 跑偏，但不能解决：

- 后端 final answer 没写入 output；
- interrupt 被记成 queue timeout；
- 文件 artifact 不存在；
- 父端误读 `waiting_reply`；
- 父端轮询 raw output 污染上下文。

这些都必须靠控制面解决。

## 成功标准

实现后，下面这些行为应成立：

1. 子 agent 完成时，父端无需读 `output.txt` 也能收到 completed notification。
2. 文本任务 completed 时一定有非空 `final_text`。
3. 文件任务 completed 时一定有有效 `final_artifacts`。
4. `wait_agent` 不返回 raw output，不诱导父端查岗。
5. `read_agent_result` 读取的是权威 final result，不是工具轨迹。
6. interrupt 后状态是 `interrupted`，不是 `_queue.Empty`。
7. `process_status=waiting_reply` 不会被父端误判为任务仍在 running。
8. 父端 fallback 时能明确知道是哪个 subagent failed/interrupted/invalid，而不是模糊地说“无结论”。

## 推荐下一步

先做 Phase 1 和 Phase 2。

原因：

- Phase 1 直接解决 `msft_stock` 的“后端有答案但父端读不到”；
- Phase 2 直接解决 `docx_report` 的“interrupt 变 queue timeout”；
- 两者改动都集中在 worker finalization 和 state/result 读取，风险可控；
- parent inbox 和上下文注入可以在后续逐步增强。

最小可交付版本应该做到：

```text
subagent 完成 -> state.final_text 有权威答案 -> parent inbox 有 completed -> read_agent_result 返回 final_text
subagent 被中断 -> state.interrupted -> parent inbox 有 interrupted -> 不出现 queue.Empty 误报
```

做到这一步后，GA 的 subagent 机制就已经从“靠文件猜状态”迈向 Codex 式“事件驱动 + 权威 final result”。
