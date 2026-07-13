# GA Subagent 主动查岗问题优化方案

## 背景

GA 的 subagent 已经有 `state.json`、`events.jsonl`、`mailbox.jsonl`，但父端工具语义仍然偏“主动查看”：

- `wait_agent` 的工具描述写着会返回 final output；
- `wait_agent` 的实现默认读取并返回子任务最终输出摘要；
- `memory/subagent.md` 仍建议主 agent 空闲时读 `output.txt` 观察进度；
- 工具 schema 暴露 `poll_interval_seconds`，让模型把等待理解成可频繁轮询。

这会诱导父 agent 反复调用 wait/list/read output，表现得像“查岗”。Codex 的做法更接近事件驱动：subagent 完成或发消息时把更新送到父端 mailbox；主 agent 用 `wait_agent` 等 mailbox/status 变化，只有需要最终内容时再显式读取结果。

## 目标

把 GA 对主模型暴露的 subagent 协作语义改成“事件等待 + 显式收集结果”：

1. subagent 关键生命周期事件写入 root 级 parent inbox；
2. `wait_agent` 只等待 mailbox/status update，不默认读取 final output；
3. 新增 `read_agent_result` 专门读取已完成 subagent 的最终输出；
4. schema/SOP 不再鼓励轮询 output；
5. 保留现有文件 IO 和兼容标记 `[ROUND END]`，降低改造风险。

## 设计

### 1. Parent inbox

新增统一父端事件流：

```text
temp/subagents/inbox.jsonl
```

worker 在关键状态点写入事件：

- `agent_started`
- `turn_completed`
- `agent_waiting_reply`
- `agent_exited`
- `agent_shutdown`
- `agent_error`
- `agent_closed`

事件只带状态、路径和元数据，不内联大段 final output。这样 `wait_agent` 可以像 Codex 一样被解释成“等待 mailbox 更新”。

### 2. wait_agent 职责收窄

`wait_agent` 返回：

- `status`: `changed` / `timeout`
- `events`: parent inbox 或状态变化摘要
- `agents`: 相关 subagent 的结构化状态
- `result_hint`: 若有 completed agent，提示用 `read_agent_result` 收结果

它不再返回 `final_output`。这能避免父模型把 wait 当成“读取/总结子输出”的工具反复调用。

### 3. read_agent_result 显式收结果

新增工具：

```text
read_agent_result(target, max_output_chars=8000)
```

只在以下场景使用：

- `wait_agent` 报告目标 completed；
- reduce 阶段需要汇总所有子任务结果；
- 用户明确要求查看某个 subagent 输出。

### 4. 内部仍可用轻量 polling

Python 文件 IO 没有真正的跨进程 async channel，所以 manager 内部仍可用短间隔检查文件大小/状态作为实现细节。但该细节不暴露给 LLM；schema 移除 `poll_interval_seconds`，SOP 也不再要求父 agent 读 output 查进度。

## 兼容性

- 旧 worker 只写 `events.jsonl`/`output.txt` 时，`wait_agents()` 仍用 event 文件变化和 `[ROUND END]` 兼容推导状态；
- `reply.txt` 继续兼容；
- `list_agents(include_output=True)` 仍可作为显式调试/收集入口；
- `completed/waiting_reply` 继续表示“本轮已完成，进程保活等待 followup”，不是卡住。

## 分步实施

1. 在 `subagent_state.py` 增加 parent inbox 写入 helper。
2. 在 worker 生命周期事件中镜像关键事件到 `temp/subagents/inbox.jsonl`。
3. 调整 `SubagentManager.wait_agents()`：优先等待 parent inbox 更新，回退到旧 event/state 检查。
4. 调整 `ga.py::do_wait_agent()`：不再返回 final output。
5. 新增 `ga.py::do_read_agent_result()`。
6. 更新中英文工具 schema 和 `memory/subagent.md`。
7. 更新测试，覆盖：
   - worker 完成时 parent inbox 收到事件；
   - `wait_agent` 返回状态但不带 final output；
   - `read_agent_result` 能读取 completed 输出；
   - schema 包含新工具且不暴露 polling interval。

## 预期效果

父 agent 的自然行为会从：

```text
反复 wait/list/read output → 判断是否完成
```

变成：

```text
spawn → wait_agent 等事件 → completed 后 read_agent_result → reduce
```

这更接近 Codex 的 mailbox-driven 协作模型，也能降低子任务已完成但父端继续“查岗”、误判 `waiting_reply`、或重复读取长输出造成上下文污染的概率。
