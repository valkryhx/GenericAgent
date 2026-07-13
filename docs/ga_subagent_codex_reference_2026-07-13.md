# GA Subagent 优化笔记：Codex Subagent 机制可借鉴点

## 0. 结论先行

Codex 的 subagent 机制最值得 GA 借鉴的不是 Rust async 实现，而是控制面设计：

- subagent 有正式 registry、canonical path、状态枚举和父子关系；
- `wait_agent` 等待状态/邮箱变化，不靠轮询 output 文本；
- `send_message` 和 `followup_task` 区分“只入队”和“触发下一轮”；
- `close_agent` 关闭前返回 previous status，并处理 descendants；
- turn 完成状态和进程存活状态是两件事。

GA 不需要一次性复刻 Codex。更现实的改法是保留现有 Python 子进程和文件 IO，在 `temp/{task}/` 下补 `state.json`、`events.jsonl`、`mailbox.jsonl`，再把父进程里的临时轮询/清理逻辑收敛成 `subagent_manager.py`。

## 1. 范围

这份 md 基于本地两个代码库做静态调研：

- Codex 源码：`D:\git_codes\codex`
- GenericAgent 源码：`D:\git_codes\GenericAgent`

目标不是复刻 Codex 的 Rust 线程架构，而是识别 Codex 在 subagent 生命周期、状态观测、父子通信、上下文继承和清理语义上的工程做法，并判断哪些可以迁移到 GA 当前的 Python 文件 IO subagent 机制中。

本次分析也参考了今天 GA 运行“法国/巴西世界杯战绩”两个 subagent 时暴露的问题：巴西 worker 未收尾后由主进程接管；法国 worker 后续写出 `[ROUND END]`，但父会话没有重新按 worker 粒度收集其完成结果，最终统一终止两个 PID。

## 2. 当前 GA Subagent 机制概况

GA 当前 subagent 是轻量级文件 IO 协议，核心实现位于 `agentmain.py`：

- `python agentmain.py --task {name} --input ...` 会先启动后台进程，并把 PID 打印给父进程。
- 实际 worker 加 `--nobg` 后运行，工作目录仍是项目根/`temp` 体系。
- 输入写入 `temp/{task}/input.txt`。
- worker 每轮输出写入 `temp/{task}/output{nround}.txt`。
- 轮次完成时写入最终文本并追加 `[ROUND END]`。
- 完成一轮后等待 `reply.txt`，最多 10 分钟；如果没有 reply，进程退出。
- `_stop`、`_intervene`、`_keyinfo` 用作停止、干预和工作记忆注入。

当前 SOP 记录在 `memory/subagent.md`，它把 `[ROUND END]` 定义为“轮完成”标记，并说明主 agent 应轮询 output、必要时干预、最后收集结果。

需要注意一个实现细节：SOP 说 output 是 append，但 `agentmain.py` 当前对 `output{nround}.txt` 是覆盖写入。也就是说，读方应把 output 文件视为“当前快照”，不能假设它是严格事件日志。

## 3. Codex Subagent 机制概况

Codex 的 subagent 不靠独立文件协议完成生命周期管理，而是作为“线程级受控实体”挂在同一个 AgentControl 控制面下。关键代码集中在：

- `codex-rs/core/src/tools/handlers/multi_agents_v2/`
- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/status.rs`
- `codex-rs/protocol/src/protocol.rs`

### 3.1 AgentControl 是核心控制面

`AgentControl` 是 Codex 多 agent 的中心控制对象。它由 root thread 创建，并共享给该 root 派生出的所有 subagent。它负责：

- 创建新 agent thread；
- 保存 agent registry；
- 维护父子 thread spawn edge；
- 发送 inter-agent message；
- 查询/订阅 agent status；
- 关闭 agent 及其 descendants；
- 给父 thread 注入子 agent 完成通知。

这和 GA 当前“父进程自己记 PID、自己轮询 output、自己写 `_intervene`”的模式有本质区别：Codex 把 subagent 当成一个有 ID、路径、状态、父子关系和事件流的受控对象，而不是一个只暴露 output 文件的后台进程。

### 3.2 Spawn 使用结构化 task name 和路径树

Codex MultiAgentV2 的 `spawn_agent` 接口要求：

- `task_name`：新 agent 的规范任务名，只允许小写字母、数字和下划线；
- `message`：初始任务；
- `fork_turns`：`none`、`all` 或正整数，控制上下文继承范围；
- 可选 `agent_type`、`model`、`reasoning_effort`、`service_tier`。

spawn 后生成 canonical task path，例如 `/root/task_a/subtask_b`。父/子/兄弟 agent 之间可以用相对路径或 canonical path 定位目标。Codex 在 registry 中维护 path 到 thread id 的映射，并用 `list_agents` 暴露当前 live agent 列表。

GA 当前只有 `temp/{task_name}` 目录名和 PID，没有一层正式的 agent path / registry。因此父进程很容易把“法国 PID 仍活着”误判为“法国任务未完成”，也没有一个统一 API 能表达“这个 worker 的 turn 已完成，但进程正在等待 reply”。

### 3.3 状态不是文本标记，而是事件派生的 AgentStatus

Codex 的 `AgentStatus` 是协议枚举：

- `pending_init`
- `running`
- `interrupted`
- `completed(final_message)`
- `errored(error)`
- `shutdown`
- `not_found`

状态由事件派生：

- `TurnStarted` -> `Running`
- `TurnComplete` -> `Completed(last_agent_message)`
- `TurnAborted` -> `Interrupted` 或 `Errored`
- `Error` -> `Errored`
- `ShutdownComplete` -> `Shutdown`

`is_final()` 明确把 `PendingInit`、`Running`、`Interrupted` 视为非终态，其它状态视为终态。

GA 当前只有 `[ROUND END]` 和进程是否存活这两个松散信号。二者语义不同：

- `[ROUND END]` 表示一轮输出完成；
- PID alive 可能只是 worker 等待 `reply.txt`；
- PID dead 也可能是正常退出、被 kill、崩溃或启动失败。

因此 GA 需要补一层结构化状态，否则父进程只能靠文本和进程状态猜。

### 3.4 Wait 不是轮询文件，而是订阅状态或邮箱变化

Codex V1 的 `wait_agent` 订阅目标 agent status，直到任一目标进入 final status 或超时。V2 的 `wait_agent` 更进一步：等待当前线程邮箱中出现来自任意 live agent 的更新，包括子 agent 消息和完成通知。

V2 的设计刻意不直接把 final content 放进 `wait_agent` 返回值。`wait_agent` 只负责唤醒父 agent；具体内容作为 inter-agent mailbox message 进入父 agent 的上下文。这降低了工具返回过大、内容重复和上下文污染的风险。

GA 当前做法是父进程主动轮询每个 `output.txt`，并在输出里找 `[ROUND END]`。这个机制能跑起来，但容易出现：

- 轮询窗口错过最后状态；
- 清理前没有按 agent 粒度复查；
- partial output 与 final output 混淆；
- “进程还活着”与“任务还没完成”混淆；
- 没有统一的 mailbox 通知父进程哪个 worker 已有新状态。

### 3.5 父子通信用 InterAgentCommunication，不靠 reply.txt 单槽文件

Codex 定义了 `InterAgentCommunication`：

- `author`
- `recipient`
- `other_recipients`
- `content`
- `trigger_turn`

`send_message` 是 queue-only，不触发新 turn；`followup_task` 会触发目标 agent 的下一轮。消息进入目标 session 的 input queue/mailbox，由统一调度器决定是否立即启动 turn。

GA 当前用 `reply.txt` 作为下一轮输入，用 `_intervene` 作为干预文件。这两个文件都是单槽机制，缺少消息 ID、作者、目标、是否触发 turn、是否已消费、消费时间等元数据。短期可以继续用文件，但建议升级成 mailbox JSONL 或 stateful queue。

### 3.6 Close 是有语义的，不是直接 kill PID

Codex 的 `close_agent` 做几件事：

- 解析 target；
- 禁止 close root；
- 读取关闭前 status，并把 previous_status 返回给调用者；
- 持久化 spawn edge 为 closed；
- shutdown 目标 agent 及 live descendants；
- flush rollout/materialized history；
- 从 registry 移除 agent。

这对 GA 很有借鉴价值。今天法国/巴西案例中，主进程 cleanup 直接按 PID 终止两个进程，没有先做“逐 agent 最终状态复查”和“完成结果收集”。如果 GA 有 `close_agent(task)`，它应该返回 previous_status，并在关闭前把 final output 和 state 持久化。

## 4. 对 GA 的可借鉴优化点

### 4.1 增加结构化 state.json

建议每个 `temp/{task_name}/` 下新增 `state.json`，作为文件协议的第二层，而不是继续只依赖 output 文本。

建议字段：

```json
{
  "task_name": "wc_france_history",
  "pid": 22596,
  "parent_session_id": "session_xxx",
  "status": "running",
  "turn_status": "running",
  "process_status": "alive",
  "round": 0,
  "input_path": "temp/wc_france_history/input.txt",
  "output_path": "temp/wc_france_history/output.txt",
  "started_at": "2026-07-13T10:20:00+08:00",
  "updated_at": "2026-07-13T10:24:49+08:00",
  "last_round_end_at": null,
  "last_message": "准备检索权威战绩表",
  "final_output_sha256": null,
  "error": null,
  "close_reason": null
}
```

建议把状态拆成两层：

- `turn_status`: `pending`、`running`、`completed`、`errored`、`interrupted`
- `process_status`: `starting`、`alive`、`waiting_reply`、`exited`、`shutdown`、`killed`

这样可以准确表达“法国已经 completed，但进程仍 alive/waiting_reply”。父进程不会再把 alive 误判为未完成。

### 4.2 新增 subagent_manager.py

建议把启动、轮询、收集、关闭逻辑从临时脚本和 SOP 中下沉成代码 API。

最小接口：

- `spawn_agent(task_name, input_text, *, verbose=False, fork_history=None) -> AgentHandle`
- `list_agents(prefix=None) -> list[AgentState]`
- `read_agent(task_name) -> AgentState`
- `wait_agents(task_names=None, timeout_s=30) -> WaitResult`
- `send_message(task_name, message, trigger_turn=False)`
- `close_agent(task_name, reason="") -> CloseResult`

其中 `close_agent` 应明确：

1. 先读取 `state.json` 和 output；
2. 如果 `turn_status == completed`，先保存 final output 摘要/路径；
3. 如果进程仍在 waiting_reply，可优先写 `_stop` 或发送 graceful shutdown；
4. 超过 grace period 才 terminate；
5. 返回 previous_status。

这基本对应 Codex `AgentControl` 的 Python 轻量版。

### 4.3 引入 events.jsonl，替代把 output 当事件流

保留 `output.txt` 给人读，但新增 `events.jsonl` 给程序读。

事件示例：

```json
{"type":"agent_started","task_name":"wc_brazil_history","pid":25096,"ts":"..."}
{"type":"turn_started","round":0,"ts":"..."}
{"type":"delta","round":0,"bytes":1588,"ts":"..."}
{"type":"turn_completed","round":0,"output_path":"output.txt","ts":"..."}
{"type":"agent_waiting_reply","round":0,"ts":"..."}
{"type":"agent_closed","previous_status":"completed","reason":"parent_cleanup","ts":"..."}
```

有了 events，父进程可以等待“事件更新”，而不是每次重新读全文并猜测。UI 也可以稳定显示状态、进度和最终结果来源。

### 4.4 明确 send_message 与 followup_task 的差异

GA 当前 `reply.txt` 既像普通消息，也像触发下一轮任务。建议借鉴 Codex：

- `send_message`: 只入队，不触发 turn；
- `followup_task`: 入队并触发下一轮；
- `_intervene`: 仍可保留为高优先级控制消息，但也应进入 mailbox，有作者、时间、是否消费。

文件实现上可以从 `mailbox.jsonl` 开始，而不是立即引入复杂 IPC。

### 4.5 引入 agent registry 和路径命名

建议新增 root 级 `temp/subagents/registry.json` 或每个 session 下的 registry：

```json
{
  "root_session_id": "session_xxx",
  "agents": {
    "/root/wc_france_history": {
      "task_name": "wc_france_history",
      "pid": 22596,
      "status_path": "temp/wc_france_history/state.json",
      "last_task_message": "调研法国国家队..."
    }
  }
}
```

这样后续可以支持：

- `list_agents`
- 相对 target；
- 父子层级；
- max agents / max depth；
- 按 session 清理；
- UI 统一展示 live agents。

### 4.6 上下文继承从“可选手写 history”升级为正式 fork_turns

GA SOP 已有 `_history.json` 可选继承，但它不是正式接口。Codex 的 `fork_turns` 更清晰：

- `none`: 不继承上下文；
- `all`: 继承完整历史；
- `N`: 只继承最近 N 轮。

建议 GA 的 spawn API 也采用这个三值接口。初期可只实现 `none` 和 `all`，后续再实现最近 N 轮裁剪。

## 5. 针对法国/巴西案例的改进闭环

如果 GA 有上述 state/manager，这次运行应该表现为：

1. 父进程启动两个 agent，registry 记录两个 task。
2. 法国 worker 写 `[ROUND END]` 时同步写 `state.turn_status=completed`、`process_status=waiting_reply`。
3. 巴西 worker 卡在 MCP 检索后持续 `running`，heartbeat 更新时间不再变化或错误字段记录超时。
4. `wait_agents` 返回“法国 completed，巴西 running/timeout”。
5. 父进程可以只接管巴西，不再把法国当作未完成。
6. cleanup 调用 `close_agent(wc_france_history)`，返回 previous_status=completed；调用 `close_agent(wc_brazil_history)`，返回 previous_status=running 或 interrupted。

这比“统一 terminate 两个 PID”更符合工程语义，也更容易测试。

## 6. 分阶段落地建议

### Phase 1：不改变交互，只补状态层

目标：解决“完成标记、进程存活、父进程收集”混淆。

- 在 `agentmain.py --task` 中写 `state.json`。
- 完成轮次时写 `turn_status=completed`。
- 等待 `reply.txt` 时写 `process_status=waiting_reply`。
- 异常退出时写 `errored`。
- 用原子写避免半截 JSON。
- 增加单元测试：`[ROUND END]` 后进程仍 alive 时，manager 判定为 completed_waiting_reply。

### Phase 2：新增 manager API，替换手写轮询

目标：让父 agent 不再直接拼 `psutil + open(output.txt)`。

- 新增 `subagent_manager.py`。
- 让常用 SOP 调用 manager。
- 实现 `wait_agents`、`list_agents`、`close_agent`。
- cleanup 前必须逐 agent 刷新 state。
- 增加回归测试：一个 completed、一个 running 时，只接管 running。

### Phase 3：加入 mailbox 和完成通知

目标：减少父进程轮询输出全文。

- 新增 `mailbox.jsonl`。
- 支持 `send_message` 与 `followup_task`。
- worker 完成时向 parent mailbox 写 `agent_completed`。
- 父进程 `wait_agents` 等待 mailbox/event 更新。

### Phase 4：上下文 fork 和路径树

目标：支持更稳定的多层 subagent。

- 引入 canonical path，如 `/root/research/france`。
- 实现 `fork_turns=none|all|N`。
- 加 max_agents、max_depth。
- UI 展示 subagent tree、last_task_message、status。

## 7. 不建议直接照搬的部分

Codex 的完整实现依赖 Rust async runtime、ThreadManager、rollout store、app-server protocol、telemetry 和多客户端事件流。GA 当前是紧凑 Python 项目，直接照搬会带来过高改造成本。

更合适的路线是：

- 保留 GA 简单进程模型；
- 在文件协议上补结构化 state/events/mailbox；
- 把父进程 ad hoc 操作沉淀成 manager；
- 后续再逐步把子任务纳入 workflow runtime 或统一调度器。

## 8. 建议的验收标准

优化后的 GA subagent 至少应通过以下场景：

1. worker 写出 `[ROUND END]` 后仍等待 reply，manager 返回 `turn_status=completed`，不误判为 stuck。
2. 两个 worker 中一个 completed、一个 running timeout，父进程只接管 timeout 的那个。
3. `close_agent` 在关闭前返回 previous_status，并保留 final output 路径。
4. `list_agents` 能显示 task_name、PID、turn_status、process_status、last_task_message。
5. `wait_agents` 能在 worker 完成或 mailbox 更新时返回，而不是固定 sleep。
6. output partial snapshot 不会被当作 final answer；只有 `turn_completed` event 或 `[ROUND END]` 才算 final。
7. 被 kill、异常退出、正常退出三种情况在 state 中可区分。

## 9. 参考资料

[1] Codex spawn v2：`D:\git_codes\codex\codex-rs\core\src\tools\handlers\multi_agents_v2\spawn.rs`

[2] Codex wait v2：`D:\git_codes\codex\codex-rs\core\src\tools\handlers\multi_agents_v2\wait.rs`

[3] Codex message tools：`D:\git_codes\codex\codex-rs\core\src\tools\handlers\multi_agents_v2\message_tool.rs`

[4] Codex AgentControl：`D:\git_codes\codex\codex-rs\core\src\agent\control.rs`

[5] Codex AgentRegistry：`D:\git_codes\codex\codex-rs\core\src\agent\registry.rs`

[6] Codex AgentStatus：`D:\git_codes\codex\codex-rs\core\src\agent\status.rs`，`D:\git_codes\codex\codex-rs\protocol\src\protocol.rs`

[7] GA task mode：`D:\git_codes\GenericAgent\agentmain.py`

[8] GA subagent SOP：`D:\git_codes\GenericAgent\memory\subagent.md`

[9] GA supervisor SOP：`D:\git_codes\GenericAgent\memory\supervisor_sop.md`
