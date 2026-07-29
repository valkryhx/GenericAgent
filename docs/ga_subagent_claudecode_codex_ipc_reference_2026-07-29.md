# Claude Code / Codex subagent 消息通道与实时机制源码调研

日期：2026-07-29

调研对象（本地源码，非二手笔记）：

- Claude Code：`D:\git_codes\claude-reviews-claude\claude-code-fork\src`
- Codex：`D:\git_codes\codex`（主要 `codex-rs`）

调研问题：Claude Code 和 Codex 的 subagent 父子消息传递，是否使用 GA 现在这套
"durable mailbox（`mailbox.jsonl` 为权威源）+ 可选 realtime IPC 只做通知" 的架构？

## 0. 结论速览

**两者都不是。** GA 现在的设计是"可靠性优先"的独特混合体，两个参考实现都是"延迟优先"：

| 系统 | durable 层 | live 实时层 | live 消息权威源 | 崩溃时未消费消息 |
| --- | --- | --- | --- | --- |
| Claude Code | 每个子 agent 一份 sidechain JSONL transcript | 同进程 async generator + AppState 内存数组 + 模块级内存队列 | **内存状态 + query loop** | 丢失 |
| Codex | rollout JSONL + SQLite metadata | `async_channel` / `mpsc` / `watch` / `broadcast` / app-server transport | **内存 channel + thread manager** | 丢失 |
| GA 当前设计 | `mailbox.jsonl` / `events.jsonl` / `state.json` | 可选 realtime IPC（named pipe / socket） | **durable mailbox 仍为权威源** | 可重放 |

即：两者的实时性来自"同进程 async 流 / channel / 事件队列 / Tokio task / SSE-WebSocket
传输"，JSONL 的角色是 transcript、history、replay、resume、审计，**不是 live 消息投递通道**。

**第二轮复核补充（2026-07-29）：两者都没有父子 agent 之间的 IPC。** Claude Code 子 agent 是
同进程隔离 context（`forkedAgent.ts:345-462`），Codex 子 agent 是同进程 Tokio task
（`session/mod.rs:662`，`core/src/agent/` 下 `Command::new` 调用点为 0）。它们存在的所有
IPC（Claude Code 的 UDS/bridge、Codex 的 app-server stdio/UDS/WebSocket、
`\\.\pipe\codex-ipc`）都是 **client↔core / 跨 session / IDE 上下文 / 远端 exec 宿主**，
从不用于 parent-agent↔child-agent。**GA 是三者中唯一把子 agent 放进独立 OS 进程的**，
所以 GA 需要 IPC 不是设计缺陷，是必然结果；而"改走 Codex 路线"在传输层等价于
"取消 IPC、改同进程"，与 Claude Code 路线是同一笔代价（详见 2.9 / 4.2.1）。

## 1. Claude Code：durable sidechain 与内存 pendingMessages 的分工

### 1.1 durable 层：sidechain JSONL transcript（transcript / resume 用，不是 mailbox）

`src\utils\sessionStorage.ts`：

- `getTranscriptPath()` → `<projectDir>/<sessionId>.jsonl`（L202-205）。
- `getAgentTranscriptPath(agentId)` → `<projectDir>/<sessionId>/subagents/agent-<agentId>.jsonl`（L247-258）。
- `getAgentMetadataPath()` 把 `.jsonl` 换成 `.meta.json`，`writeAgentMetadata()` 写 agentType / worktreePath / description（L260-302）——与 GA 的 `state.json` 元数据侧车思路相近。
- **写入是异步批量**：`enqueueWrite(filePath, entry)` + `scheduleDrain()`，默认 100ms drain（L606-631），批量 append `jsonStringify(entry) + '\n'`（L645-678）。
- `insertMessageChain(messages, isSidechain, agentId, ...)` 构造带 `parentUuid`、`isSidechain`、`agentId`、`sessionId`、`cwd`、`version` 的 `TranscriptMessage`（L993-1068）。
- 路由：`entry.isSidechain && entry.agentId !== undefined` → 写子 agent 文件（L1217-1245）；`recordSidechainTranscript()`（L1451-1462）。
- 读取：`getAgentTranscript()` 过滤 `msg.agentId === agentId && msg.isSidechain` 重建链（L4183-4232）。

### 1.2 决定性证据：注入的 attachment 消息**故意不写 transcript**

`src\tools\AgentTool\runAgent.ts`：

- 启动前 fire-and-forget 记录初始消息（为 resume 时的 agentType 路由）（L732-742）。
- 消费 `query(...)` async generator（L747-757）。
- 正常消息：`recordSidechainTranscript([message], agentId, lastRecordedUuid)` 后更新 `lastRecordedUuid`（L792-804）。
- **L770-789：attachment 消息被 yield 出去，注释明确写 "without recording them"。**

这一条直接否定"JSONL 是 live 消息权威源"的可能：运行期注入给子 agent 的消息根本不落盘。

同文件 L412-479 还有 subagent 权限模式改写（frontmatter override、async agent 的
`shouldAvoidPermissionPrompts`、bubble mode、allowedTools 变成 scoped session rules），
对 GA 的 `inherit-current-permissions` 有参考价值。

### 1.3 live 层：AppState 上的内存 `pendingMessages`

`src\tasks\LocalAgentTask\LocalAgentTask.tsx`：

- `LocalAgentTaskState` 含 `messages?: Message[]`、`pendingMessages: string[]`、`retain`、`diskLoaded`、`abortController`（L115-147）。
- `queuePendingMessage(taskId, msg, setAppState)`（L162-166）。
- `drainPendingMessages()` 读完即清空（L181-190）——纯内存，无持久化、无 ack、无重放。
- `enqueueAgentNotification()` 拼装 task-notification（task-id、output-file、status、summary、result、usage、worktree info），再 `enqueuePendingNotification({ value: message, mode: 'task-notification' })`（L196-261）。
- **task 输出文件是 symlink**：`initTaskOutputAsSymlink(agentId, getAgentTranscriptPath(...))`，async agent L483（块 L466-515）、foreground agent L547（块 L526-614）。

### 1.4 SendMessageTool 的投递优先级

`src\tools\SendMessageTool\SendMessageTool.ts`：

- registry / 裸 agentId 查找（L800-808）。
- **正在运行的本地子 agent → `queuePendingMessage(...)`**，返回 "Message queued for delivery ... at its next tool round."（L809-821）。
- 已停止 → `resumeAgentBackground(...)`（L822-844）；AppState 已驱逐 → 同一恢复路径（L845-871）。
- `uds:<socket-path>` / `bridge:<session-id>` schema 在 `UDS_INBOX` feature 下（L67-86）；bridge 权限文案提到 "Remote Control session ... via Anthropic's servers"（L585-601）；`postInterClaudeMessage(...)`（L741-774）、`sendToUdsSocket(...)`（L775-797）。

即：**UDS / bridge 是跨 session / 远程场景，本地父子通道走内存队列。**

### 1.5 消息如何进入子 agent 的下一轮

`src\utils\attachments.ts`：

- `getAgentPendingMessageAttachments()` 调 `drainPendingMessages(...)`，产出 `type: 'queued_command'`（L1085-1100）；producer 注册进 thread-safe attachments（L916-918）。
- `getQueuedCommandAttachments()` → `queued_command`（`prompt`、`commandMode`、`origin`）（L1044-1081）。
- `getAttachmentMessages()` 包成 `AttachmentMessage` 并 yield（L2937-2969）。

`src\query.ts`：

- tool results 之后处理 queued command，注释说明 `later` 优先级、Sleep flush override、agent 作用域（主线程 drain `agentId===undefined`，子 agent drain 自己的 `agentId`）（L1547-1578）。
- `getCommandsByMaxPriority(...)` → `getAttachmentMessages(...)` → yield → push 进 `toolResults` → `removeFromQueue(consumedCommands)`（L1570-1589、L1630-1643）。
- 模型流：`for await (const message of deps.callModel(...))`（L659-708），`yield yieldMessage`（L823-825）。

### 1.6 模块级队列与 SDK 事件队列

`src\utils\messageQueueManager.ts`：

- 模块级队列，注释 "Unified command queue (module-level, independent of React state)"，React 通过 `useSyncExternalStore` 订阅（L40-50、L67-80）。
- `enqueue()` / `enqueuePendingNotification()`（默认优先级 `later`）（L123-149）；`dequeue()` 按优先级 FIFO（L167-192）。
- `logOperation()` → `recordQueueOperation(queueOp)`（L28-38）——**只是审计日志，不是队列权威源**。

`src\utils\sdkEventQueue.ts`：

- 事件类型 `task_started`、`task_progress`、`task_notification`、`session_state_changed`（L6-72）。
- 模块级 `const queue: SdkEvent[] = []`、`enqueueSdkEvent()`、`drainSdkEvents()`（L74-101）。
- L36-40 注释：task_notification "Drained by drainSdkEvents() directly into the output stream"、"does NOT go through print.ts XML task_notification parser"、"does NOT trigger the LLM loop"。

### 1.7 async 生命周期、resume、任务输出

- `src\tools\AgentTool\agentToolUtils.ts`：`for await (const message of makeStream(...))` 推入 `agentMessages`、retain 时 append 到 AppState `task.messages`、更新进度、发 SDK progress（L554-593）；`completeAsyncAgent(...)` 后 `enqueueAgentNotification(...)`（L597-637）。
- `src\tools\AgentTool\AgentTool.tsx`：async subagent 由 `void runWithAgentContext(... runAsyncAgentLifecycle(...))` 拉起，立即返回 `status: 'async_launched'`、`agentId`、`outputFile`（L686-764）。
- `src\tools\AgentTool\resumeAgent.ts`：并行 `getAgentTranscript(...)` + `readAgentMetadata(...)`（L63-66）；无 transcript 报错（L67-69）；append 新 user prompt（L166-195）；重新注册 async agent + `runAsyncAgentLifecycle`（L197-205、L230-258）。
- `src\utils\task\diskOutput.ts`：输出路径 `<project temp>/<sessionId>/tasks/<taskId>.output`（L49-55、L72-74）；`initTaskOutputAsSymlink()`（L423-450）；`readFileRange(outputPath, fromOffset, maxBytes)`（L300-329）；`tailFile(...)`（L332-357）。
- `src\utils\task\framework.ts`：注册时发 `task_started` SDK 事件（L77-117）；`generateTaskAttachments()` 读 `getTaskOutputDelta(taskState.id, taskState.outputOffset)` 并推进 `outputOffset`（L154-205）——**与 GA `attach_agent(since_offset)` 的增量切片同构**。
- `src\tools\TaskOutputTool\TaskOutputTool.tsx`：注释说明 local_agent 的 disk output 是完整 transcript 的 symlink（每条消息/工具调用，不只是答案）（L91-105）；deprecation 建议直接 Read 输出文件（L172-181）；`waitForTaskCompletion()` 每 100ms 轮询 `getAppState().tasks[taskId].status`（L117-143）。
- `src\Task.ts`：`TaskType` 含 `local_agent`/`remote_agent`（L5-12）；`TaskStatus`（L14-20）；`TaskStateBase.outputFile/outputOffset/notified`（L44-56）；`createTaskStateBase()` 初始化 `outputFile: getTaskOutputPath(id)`、`outputOffset: 0`（L107-124）。

### 1.8 唯一真正的文件 mailbox：teammateMailbox（但不在本地 subagent 路径上）

`src\utils\teammateMailbox.ts`：

- 文件头："File-based messaging system for agent swarms"，inbox 路径 `.claude/teams/{team_name}/inboxes/{agent_name}.json`（L1-7）。
- 消息结构 `from`、`text`、`timestamp`、`read`、`color`、`summary`（L42-49），路径构造（L55-65）。
- 读 = 整文件 `jsonParse(content) as TeammateMessage[]`，ENOENT → `[]`（L83-107）；写 = lockfile + 整文件重写（L133-190）。

对比 GA（2026-07-29 复核修正）：GA `subagent_mailbox.py` 虽然**文件格式**是 JSONL，但
`_write_rows()` 用 `open(self.path, "w")` **整文件截断重写**，且 `enqueue()` /
`consume_trigger_turn()` 外面**没有任何跨进程锁**（对比 `subagent_event_bus.py:118` 的
`_locked()`）。也就是说 GA 当前实现在并发语义上**比 Claude Code 的 `teammateMailbox` 更弱**：
Claude Code 至少有 lockfile（L133-190），GA 连锁都没有。父进程 `enqueue` 与子进程
`consume_trigger_turn` 并发时会互相覆盖丢消息，这直接动摇了 "durable mailbox 是权威源" 的前提。
修复方向：改成真正的 append-only（consume 只追加 `message_consumed` 标记行，不重写历史行），
或至少套用 event bus 现成的跨进程锁。

### 1.9 subagent 隔离不是独立进程

`src\utils\forkedAgent.ts`：`createSubagentContext()` 创建同进程隔离 `ToolUseContext`
（克隆 `readFileState`、子 abort controller、全新集合、no-op mutation callbacks，但
`setAppStateForTasks` 仍指向 root store，使 async agent 的后台任务保持可见/可 kill）（L345-462）。

**这是 Claude Code 不需要 IPC 的根因：子 agent 就在同一个 Node 进程里。**

## 2. Codex：SQ/EQ 队列对 + 内存 thread manager，rollout 只做历史

### 2.1 协议层就是队列对

`codex-rs\protocol\src\protocol.rs`：

- 文件头明确写 SQ/EQ 模式："Uses a SQ (Submission Queue) / EQ (Event Queue) pattern to asynchronously communicate between user and agent."
- `Submission { id, op, trace }`（L123-133）；`Op` enum 从 L474 开始。
- `Op::InterAgentCommunication`（L532-536）：走普通 thread submission 生命周期，同时记进 assistant history。
- **字段定义（L663-670）**：

  ```rust
  InterAgentCommunication {
      author: AgentPath,
      recipient: AgentPath,
      other_recipients: Vec<AgentPath>,
      content: String,
      trigger_turn: bool,
  }
  ```

  与 GA mailbox 消息（`author` / `recipient` / `content` / QueueOnly-vs-TriggerTurn）**字段语义高度重合**，
  甚至 `trigger_turn` 命名一致；差别只在 GA 落文件、Codex 进内存 op 队列。

### 2.2 core session 的 channel 布线

`codex-rs\core\src\session\mod.rs`：

- `Codex` 结构持有 `tx_sub: Sender<Submission>`、`rx_event: Receiver<Event>`、`agent_status: watch::Receiver<AgentStatus>`（L367-378）。
- `SUBMISSION_CHANNEL_CAPACITY = 512`（L422）；`async_channel::bounded(...)` / `async_channel::unbounded()`（L480-481）。
- `watch::channel(AgentStatus::PendingInit)`（L629）；`tokio::spawn` submission_loop（L660-665）。
- `submit_with_trace`/`submit_with_id` → `self.tx_sub.send(sub).await`（L683-709）；`next_event` → `self.rx_event.recv().await`（L733-739）。
- `flush_rollout` / `ensure_rollout_materialized`（L1048-1068）；`InitialHistory::Resumed`/`Forked`（L1184-1245）；`apply_rollout_reconstruction`（L1250-1281）；`last_token_info_from_rollout`（L1299-1303）。

### 2.3 多 agent 控制面：AgentControl / AgentRegistry / ThreadManagerState

`codex-rs\core\src\agent\control.rs`：

- `AgentControl` 是 root 与 children 共享的多 agent 控制面 handle，含 `session_id`、`manager: Weak<ThreadManagerState>`、`state: Arc<AgentRegistry>`（L147-163）。
- spawn 路径（L202-338），含 `notify_thread_created`（L325-328）与 spawn-edge 持久化（L330-338）。
- fork 路径在快照前先调父的 `ensure_rollout_materialized` + `flush_rollout`（L390-395）——因为 record 写入是异步排队的。**这正是 GA 需要注意的"异步落盘导致快照落后"陷阱。**
- `send_input` → `state.send_op(agent_id, ...)`（L681-700）；`send_inter_agent_communication` 构造 `Op::InterAgentCommunication` 再 `state.send_op(...)`（L719-739）。
- `subscribe_status` 返回 `watch::Receiver<AgentStatus>`（L880-887）。
- detached 完成观察者（`tokio::spawn`）等子 agent 终态，把完成转成 `InterAgentCommunication`（MultiAgentV2）或 `inject_user_message_without_turn`（L987-1064）——对应 GA 的父 inbox 通知注入。
- `persist_thread_spawn_edge_for_source` → `upsert_thread_spawn_edge(parent, child, Open)`（L1197-1219）；close 路径置 `Closed`（L783-795）。

`registry.rs`：`AgentRegistry` 按 user session 共享，限制多 agent 能力，含
`agent_tree: HashMap<String, AgentMetadata>`（L15-24）——**内存树，不是文件 registry**。

`thread_manager.rs`：

- `ThreadManagerState { threads: Arc<RwLock<HashMap<ThreadId, Arc<CodexThread>>>>, thread_created_tx: broadcast::Sender<ThreadId> }`（L198-216）。
- `THREAD_CREATED_CHANNEL_CAPACITY = 1024`（L78），broadcast 创建（L258），`subscribe_thread_created`（L453-455）。
- **`send_op` → `thread.submit(op).await`（L986-995）** —— 父子投递的落点，纯内存。
- `notify_thread_created`（L1303-1304）。

### 2.4 TUI 事件总线

- `tui\src\app.rs`：`let (app_event_tx, mut app_event_rx) = unbounded_channel();`（L707-710）；`tui.event_stream()` + `tokio::pin!`（L1057-1058）；顶层 `tokio::select!` 覆盖 app events / active thread channel / 终端输入 / `app_server.next_event()`（L1105-1153）。
- `app_event.rs`：`AppEvent` 内部消息总线（头 L0-5），enum 从 L137，`CodexOp`（L229-231）。
- `app_event_sender.rs`：`AppEventSender` 持 `UnboundedSender<AppEvent>`（L23-25），`send`（L35-43）。
- `app/thread_events.rs`：`ThreadEventStore { buffer: VecDeque<ThreadBufferedEvent>, capacity, active }`（L33-43）；`push_notification`（L99-125）；`snapshot`（L200-220）；`ThreadEventChannel` 用 `mpsc::channel(capacity)`（L281-313）。
- `app/thread_routing.rs`：`ensure_thread_channel`（L38-42）、`activate_thread_channel`（L51-63）、`activate_thread_for_replay`（L82-91）、notification `try_send`（L849/866）、request `try_send`（L972/984）。
- `app_server_session.rs`：`next_event` → `self.client.next_event().await`（L360-361）。

### 2.5 app-server：JSON-RPC over stdio / UnixSocket / WebSocket

- `app-server-client\src\lib.rs`：bounded mpsc + backpressure（头 L0-15）；`AppServerEvent { Lagged, ServerNotification, ServerRequest, Disconnected }`（L130-136）；lossless vs best-effort（L150-184）；`forward_in_process_event`（L196-272）；`InProcessAppServerClient` worker bridge（L452-467）；`start` 建 `command_tx`/`event_tx` 与 `tokio::select!` worker（L491-601）。
- `app-server\src\in_process.rs`：头 L0-15；`InProcessServerEvent`（L150-162）；`InProcessClientMessage`（L169-187）；`try_send_client_message` 返回 `WouldBlock`（L237-249）；`next_event`（L306-312）。
- `app-server\src\request_processors\thread_lifecycle.rs`：`ensure_conversation_listener`（L136-185）；`ensure_listener_task_running`（L211-384），含 `oneshot` cancel、`tokio::spawn`（L265）、`tokio::select!` 覆盖 cancel / listener command / `conversation.next_event()` / unload watcher（L267-375）、`apply_bespoke_event_handling(...)`（L290-340）。
- `app-server\src\bespoke_event_handling.rs`：`apply_bespoke_event_handling`（L135-145）；TurnStarted→`ServerNotification::TurnStarted`（L151-179）；TurnComplete（L181-197）；McpStartupUpdate（L198-220）；warnings（L222-238）；realtime conversation events → `ThreadRealtime*`（L340-496）；patch approval 用 `outgoing.send_request(...)` + `tokio::spawn`（L498-525）。
- `app-server\src\outgoing_message.rs`：`OutgoingMessageSender` 持 `mpsc::Sender<OutgoingEnvelope>`（L94-104）；`ThreadScopedOutgoingMessageSender::send_server_notification`（L159-168）；oneshot request callbacks（L113-117）。
- `app-server\src\transport.rs`：`OutboundConnectionState.writer: mpsc::Sender<QueuedOutgoingMessage>`；`send_message_to_connection`（L129-166，droppable 连接满则 `try_send` 失败即断开，stdio/in-process 则 await）；`route_outgoing_envelope`（L193-231）。
- `app-server-transport\src\transport\mod.rs`：`CHANNEL_CAPACITY = 128`（L20-23）；`AppServerTransport { Stdio, UnixSocket, WebSocket, Off }`（L65-70）；`from_listen_url`（L104-151）；`TransportEvent { ConnectionOpened, ConnectionClosed, IncomingMessage }`（L162-177）；`forward_incoming_message`/`enqueue_incoming_message`（L193-249）。
- `stdio.rs`：`start_stdio_connection`（L23-39）、stdin reader `lines.next_line().await`（L42-79）、stdout writer 从 `writer_rx.recv().await` 写 JSON+换行（L81-97）——**与 GA `frontends/ink_bridge.py` 的 JSONL stdio 协议同构**。
- `unix_socket.rs`：`start_control_socket_acceptor`（L23-31）、accept loop + `tokio::select!`（L53-78）、每连接 WebSocket handshake upgrade。
- `websocket.rs`：`start_websocket_acceptor`（axum，L128-168）；`run_websocket_connection`（L171-228）。

### 2.6 模型流

- `core\src\client_common.rs`：`ResponseStream` 持 `rx_event: mpsc::Receiver<Result<ResponseEvent>>`，用 `poll_recv` 实现 `Stream`（L68-80）；Drop 时 cancel token（L83-86）。
- `core\src\client.rs`：`stream_responses_api`（L1207-1285）；`stream_responses_websocket`（从 L1322）；`map_response_stream`/`map_response_events`（L1738-1904），`RESPONSE_STREAM_CHANNEL_CAPACITY = 1600`、`mpsc::channel::<Result<ResponseEvent>>`、`tokio::spawn` mapper、`tokio::select!` 覆盖 cancellation 与 `api_stream.next()`。
- `codex-api\src\sse\responses.rs`：`eventsource_stream::Eventsource`，"idle timeout waiting for SSE"、"SSE event" 日志。
- `codex-api\src\endpoint\responses.rs`：`stream_request`、`text/event-stream`。
- `codex-api\src\endpoint\responses_websocket.rs`：`tokio_tungstenite`、`stream_request` 返回 `ResponseStream`。

### 2.7 rollout / SQLite：历史与恢复，不是投递

- `rollout\src\recorder.rs`：头 L1 "Persist Codex session rollouts (.jsonl) so sessions can be replayed or inspected later."；JSONL 便于 jq/fx 的说明（L63-70）；`RolloutRecorder { tx: Sender<RolloutCmd>, writer_task, rollout_path }`（L72-76）；`RolloutCmd { AddItems, Persist, Flush, Shutdown }`（L93-104）；`record_canonical_items`（L756）、`persist`（L774）、`flush`（L795）、`load_rollout_items`（L812）、`get_rollout_history`（L881）；writer `write_all(json.as_bytes()).await` + `file.flush().await`（L1651-1652）。
- `thread-store\src\live_thread.rs`：本地 store 内部可用 rollout 文件、远端 store 可用服务（L26-31）；`append_items`（L138-148）；`persist`/`flush`（L170-179）；`local_rollout_path` 对远端返回 `Ok(None)`（L251-266）。
- `thread-store\src\local\live_writer.rs`：`append_items` → `record_canonical_items` 后 `recorder.flush().await`，**保证 SQLite 永不领先 JSONL**（L75-87）；`persist_thread`/`flush_thread`（L89-113）；`sync_materialized_rollout_path`（L153-194）。
- `thread-store\src\local\read_thread.rs`：`read_thread` 优先 SQLite metadata，再校验/读 rollout 路径取 history（L27-79）；`attach_history_if_requested`（L145-159）；`load_history_items` → `RolloutRecorder::load_rollout_items(path)`（L248-255）。
- `rollout\src\state_db.rs`：L26 "Core-facing handle to the SQLite-backed state runtime."；init + backfills（L37-41）。

### 2.8 Codex 确实有 Windows named pipe，但只给 /ide 用

- `tui\src\ide_context\ipc.rs`：头 "Private transport for fetching IDE context for TUI /ide support."；Windows 默认路径 `\\.\pipe\codex-ipc`（L162-165）；`fetch_ide_context`（L141-180）。
- `tui\src\ide_context\windows_pipe.rs`：头 "Windows named-pipe transport for the IDE context IPC client."；`WindowsPipeStream::connect` 用 `CreateFileW` + `validate_pipe_server_owner`（L56-82）；overlapped `ReadFile`（L89-108）、overlapped `WriteFile`（L111-134）。

**注意这里的安全细节：`validate_pipe_server_owner` 校验 pipe server 所有者。GA 的
`SubagentRealtimeChannel` 若继续走 named pipe，应参考这一点做 owner/ACL 校验。**

### 2.9 关键补充复核（2026-07-29 第二轮）：Codex 根本没有父子 agent IPC

第二轮针对性复核（问题：Codex 是否曾把子 agent 放进独立 OS 进程并用 IPC 通信）结论是
**完全没有**。所有父子 agent 关系都是同进程 Tokio task：

- `core/src/agent/`、`core/src/thread_manager.rs`、`core/src/session/mod.rs` 下
  `Command::new` / `std::process` / `tokio::process` 的调用点数量为 **0**。
- `control.rs:202` `spawn_agent_with_metadata` → `:221` `reserve_spawn_slot`（内存计数器）
  → `:265` `state.spawn_new_thread_with_source(...)`，不创建进程。
- `thread_manager.rs:1220` `Codex::spawn(CodexSpawnArgs { ... })`：子 agent **直接继承父的
  `Arc`**（`mcp_manager`、`skills_manager`、`environment_manager`、`thread_store`）——
  这只有同进程才可能。
- `session/mod.rs:662` `tokio::spawn(async move { submission_loop(...) })`，注释
  "This task will run until Op::Shutdown is received."：子 agent 的"运行时"就是一个 Tokio task。
- `registry.rs:23-26` `AgentRegistry { active_agents: Mutex<ActiveAgents>, total_count: AtomicUsize }`：
  agent 树 / 深度 / 数量上限用 `std::sync::Mutex` + 原子量守护，即单地址空间共享内存。
- `control.rs:149` 的 doc 把 `AgentControl` 称作 "the inter-agent communication layer"，
  但它只持 `Weak<ThreadManagerState>` + `Arc<AgentRegistry>`（`:153`）——是**内存 handle，不是传输层**。

Codex 确实大量创建子进程，但每一个都属于三类非 agent 用途：

1. **per-command 工具子进程**：`core/src/spawn.rs:67` `Command::new(&program)`，doc（`:71-73`）
   明说是 "any child processes that were spawned as part of a shell tool call"；
   macOS seatbelt（`sandboxing/src/manager.rs:211`）、Linux landlock 自 re-exec
   （`core/src/landlock.rs:59-62` + `arg0/src/lib.rs:88-90`）、bubblewrap 都只是**包一条命令**。
   PTY 长驻会话（`exec-server/src/local_process.rs:167`）是长命 **shell**，线上跑的是裸
   stdin/stdout 字节，没有 Codex 协议。
2. **MCP stdio server**（Codex 是 client）：`rmcp-client/src/stdio_server_launcher.rs:253`。
3. **client↔core / core↔exec-host 宿主进程**：`app-server-daemon/src/backend/pid.rs:154`
   起 `codex app-server --remote-control --listen unix://`（`:402`）；
   `exec-server/src/client_transport.rs:134` 起远端 `codex exec-server`
   （配置示例 `environment_toml.rs:359-362`：`ssh dev "codex exec-server --listen stdio"`）。
   **注意后者：远端跑的是命令/文件系统执行服务，agent 仍留在本地进程，只有 tool call 过网。**

其它容易误认的东西：

- `codex mcp-server`（`cli/src/main.rs:902`）里 agent 是该 server 进程内的 thread，
  驱动方是**外部 MCP client 进程**；`core/` 从不去连 `codex mcp-server` 来创建子 agent。
- `app-server` 的 Stdio/UnixSocket/WebSocket 全是 **client↔core**（TUI 作为 client：
  `tui/src/lib.rs:451`）；默认路径甚至用 `in_process.rs:1-6` 的内存 channel
  "avoiding a process boundary"。`app-server-protocol` 里没有任何
  parent-agent→child-agent RPC；`ThreadSourceKind::SubAgentThreadSpawn`
  （`v2/thread.rs:1024`）只是给 UI 画 agent 树的只读标签。
- 没有任何内置工具 shell out 到 `codex exec`：`core/src` 里 grep `codex exec` 只命中
  文档注释（`config/mod.rs:1465`）与测试。
- `external-agent-migration/src/lib.rs:15` `SOURCE_EXTERNAL_AGENT_NAME: &str = "claude"`
  只是从磁盘读别的 CLI 的 rollout 历史文件，不运行也不与之通信。
- TypeScript SDK 确实 spawn Codex 子进程（`sdk/typescript/src/exec.ts:181`），但那是外部
  embedder 作为 client，Node→Codex，不是 agent→agent。
- `\\.\pipe\codex-ipc` 再次确认只服务 `/ide`（`tui/src/ide_context/ipc.rs:1,142,165`）。

**直接回答"codex 有父子 agent 之间的 IPC 吗"：没有。所以"改走 Codex 路线"在传输层上
等价于"取消 IPC、改同进程"——这一点与 Claude Code 路线的代价是同一笔。**

### 2.10 GA 的工具面已经是 Codex 的工具面

复核 `assets/tools_schema.json` 后发现，GA 的多 agent 工具名与 Codex
`multi_agents_v2` **6/6 完全一致**：

| Codex | 源文件 | GA |
| --- | --- | --- |
| `spawn_agent` | `core/src/tools/handlers/multi_agents_v2/spawn.rs:30` | `spawn_agent` |
| `wait_agent` | `.../wait.rs:25` | `wait_agent` |
| `close_agent` | `.../close_agent.rs:11` | `close_agent` |
| `send_message` | `.../send_message.rs:13` | `send_message` |
| `followup_task` | `.../followup_task.rs:13` | `followup_task` |
| `list_agents` | `.../list_agents.rs:11` | `list_agents` |

即 **GA 在协议/工具语义层已经在 Codex 路线上了**（加上 `Op::InterAgentCommunication` 的
`author/recipient/other_recipients/content/trigger_turn` 与 GA mailbox 消息同构）。
"改走 Codex 路线" 剩下的差异**只在传输层与运行时**：Codex 用同进程 Tokio + 内存 channel，
GA 用独立进程 + 文件。

Codex 还有 GA 目前缺的两个工具面能力，可低成本借鉴（与路线无关）：
`spawn_agents_on_csv` / `report_agent_job_result` 批量 fan-out
（`core/src/tools/handlers/agent_jobs/spawn_agents_on_csv.rs:20`、`report_agent_job_result.rs:19`，
worker loop `agent_jobs.rs:207`），以及 `registry.rs:23-26` 的
**agent 数量/深度上限守护**——GA 现在 `subagent_registry.py` / `subagent_manager.py` 里
grep 不到任何 depth / max_agents 限制，存在子 agent 递归 spawn 失控的风险。

### 2.11 Codex 自己也把"内容存储"与"变更通知"分开——这正是 GA 4.3 的设计

一个对 GA 决策极关键的发现：Codex 的 `wait_agent` 不是轮询内容，而是**阻塞在一个独立的
"mailbox 变更" watch 信号**上：

- `core/src/tools/handlers/multi_agents_v2/wait.rs:151`
  `wait_for_mailbox_change(mailbox_rx: &mut tokio::sync::watch::Receiver<()>, ...)`
- 信号源：`core/src/session/input_queue.rs:26` `mailbox_tx: watch::Sender<()>`

注意 `watch::Sender<()>` 的负载是**空元组**——它只说"变了，去读"，不携带内容。这与 GA 设想的
"durable mailbox 存内容 + realtime channel 只推 trigger 信号，收到后回去读权威源"
在架构上**完全同构**。

换句话说：**GA 4.3 的折中方案不是妥协，它就是 Codex 的通知模型，只是把 Codex 的进程内
`watch` 通道换成了跨进程 named pipe / socket。** 这大幅提高了 4.3 路线的置信度。


## 3. 三方对比与权衡

### 3.1 分层对比

| 维度 | Claude Code | Codex | GA 当前 |
| --- | --- | --- | --- |
| 子 agent 隔离 | 同进程 `createSubagentContext()` | 同进程 `CodexThread`（Tokio task） | **独立 OS 进程**（`agentmain.py`） |
| 父→子投递 | AppState `pendingMessages`（内存数组） | `ThreadManagerState::send_op` → `CodexThread::submit`（内存 channel） | `mailbox.jsonl` append |
| 消息生效时机 | 下一个 tool round 的 `queued_command` attachment | 下一次 submission 处理；`trigger_turn` 决定是否起轮 | `consume_trigger_turn()`；QueueOnly / TriggerTurn |
| 子→父通知 | `enqueueAgentNotification` → `messageQueueManager`（`later`）/ `sdkEventQueue` | detached watcher → `InterAgentCommunication` / `inject_user_message_without_turn` | `events.jsonl` + 父 inbox + `GA_SUBAGENT_NOTIFICATIONS` |
| 输出增量读取 | `outputOffset` + `getTaskOutputDelta` / symlink 到 transcript | `ThreadEventStore` ring buffer + snapshot | `attach_agent(since_offset)` 读 `output*.txt` |
| 历史/恢复 | sidechain JSONL + `.meta.json` | rollout JSONL + SQLite metadata + spawn edges | sidechain transcript JSONL + `state.json` + registry |
| 崩溃后未消费消息 | 丢失（内存） | 丢失（channel） | **可重放（durable mailbox）** |
| 延迟 | 极低（同进程） | 极低（channel） | 文件轮询 + 可选 realtime IPC |
| 跨进程/跨机器 | UDS / bridge / SSE / WebSocket（跨 session、远程场景） | app-server（stdio / UnixSocket / WebSocket） | 文件 + 可选 named pipe / socket |

### 3.2 关键洞察

1. **两者的低延迟是用"非持久 live 队列"换来的。** Claude Code 的 `pendingMessages`
   或 Codex 的 channel 里正在飞的消息，进程崩溃即丢。GA durable mailbox 能重放未消费消息。
2. **两者的 JSONL 都是"事后事实"，不是"当下真相"。** Claude Code 的 attachment 消息压根不写；
   Codex fork 前必须先 `flush_rollout` 才敢快照，正说明 JSONL 落后于内存状态。
3. **文件形态上 GA 目前反而更弱（复核修正）。** `subagent_mailbox.py:109-113` 的
   `_write_rows()` 是 `open(..., "w")` 整文件截断重写，且 enqueue/consume 路径**无跨进程锁**
   （对比 `subagent_event_bus.py:118` 已有的 `_locked()`）；Claude Code `teammateMailbox`
   至少有 lockfile。GA 需要先补锁或改真 append-only，否则 "durable mailbox 是权威源" 只是名义上的。
4. **命名收敛不是巧合。** Codex `Op::InterAgentCommunication` 的
   `author/recipient/other_recipients/content/trigger_turn` 与 GA mailbox 消息几乎同构，
   说明 GA 的**协议语义**是对的；分歧只在**传输载体**。
5. **两者都有"输出偏移增量读取"这一模式**（Claude Code `outputOffset`、Codex
   `ThreadEventStore` snapshot），GA `attach_agent(since_offset)` 方向正确。
6. **GA 独立进程模型是它必须做 IPC 的根因**，也是它唯一能提供"子 agent 崩溃不带走父进程、
   跨进程自动恢复"的原因。这不是缺陷，是不同的取舍。

### 3.3 GA 当前实现的三个真实缺陷（2026-07-29 复核代码得出）

对比之后回头读 GA 代码，发现"GA 的 IPC 不合适"这个判断是对的，但**痛点不在文件 vs IPC 的路线选择上**，
而在三个具体实现缺陷：

1. **父→子延迟是 2 秒轮询，不是 IPC 慢。**
   `agentmain.py:827` 默认 `reply_wait_iterations=300, reply_sleep_s=2`，子 agent 在
   `agentmain.py:960-961` 里 `sleep_fn(reply_sleep_s)` 后才去 `consume_trigger_turn()`。
   父进程 `send_message` 落盘后，子 agent 平均 1 秒、最差 2 秒才看到。同时
   `300 * 2s = 600s` 硬编码成了子 agent 的空转寿命上限。
   Claude Code 对应位置是"下一个 tool round 立即注入 attachment"，延迟为 0。

2. **realtime channel 建了但没人订阅——等于没接。**
   `subagent_realtime_ipc.py:21` 的 `connect_realtime_channel()` 在**整个非测试代码里没有任何调用者**
   （`grep` 仅命中定义处本身）。`SubagentManager` 只在父侧 `start()` 了 Listener 并把地址写进
   `ipc_endpoint`，子进程 `agentmain.py` 从未连接、从未订阅。所以 `GA_SUBAGENT_REALTIME_IPC=1`
   目前只做到"父侧监听 + 事件 fan-out 给（不存在的）订阅者"，子 agent 侧仍是纯 2 秒轮询。
   **这是"realtime IPC 感觉没用"的直接原因，也是最高性价比的修复点。**

3. **mailbox 无锁 + 整文件重写，权威源名不副实。**
   见 3.2 第 3 条：`subagent_mailbox.py:109-113` 截断重写、enqueue/consume 无跨进程锁。
   父子并发时可互相覆盖丢消息。

4. **（第二轮补充）没有 agent 深度/数量上限。** Codex 用
   `registry.rs:23-26` `AgentRegistry { active_agents: Mutex<ActiveAgents>, total_count: AtomicUsize }`
   守护 agent 树深度与总数；GA 在 `subagent_registry.py` / `subagent_manager.py` /
   `subagent_agent_path.py` 里 grep 不到任何 depth / max_agents / MAX_ 限制，
   子 agent 递归 spawn 可以失控。这一条与路线选择无关，应尽早补。

前三条都**不需要改路线**就能修，且修完之后 GA 的延迟会接近 Claude Code，同时保住 durable 语义。
反之，如果直接切 Claude Code 同进程路线，还要先付 4.1 里列出的 cwd / TOOLS_SCHEMA 全局状态重构成本。

### 3.4 mailbox 缺陷的实测确认（2026-07-29 第三轮，一次性探针实测）

3.3 第 3 条原本是读代码得出的推断。本轮用一次性探针脚本（`temp/_probe_mailbox_race*.py`，
跑完即删）对 `SubagentMailbox` 做了并发实测，**缺陷全部复现，且比预估严重**。

环境：Python 3.12.4 / Windows。所有探针用 `threading.Barrier` 对齐起跑点放大竞态窗口；
线程级竞态即证明跨进程竞态（跨进程无 GIL 保护，只会更差）。

| # | 探针 | 条件 | 实测结果 |
| --- | --- | --- | --- |
| 1 | 并发 `enqueue` 丢消息 | 4 writer × 200 轮 = 800 条 | **丢失 600 条（75%）**，每轮平均只有 1 条存活 |
| 2 | `enqueue` 与 `consume_trigger_turn` 并发 | 2 线程 × 200 轮 | **93/200 轮（46.5%）新消息既不在盘上、也未被消费，即彻底丢失** |
| 3 | consume 后自动 `message_id` | 顺序调用 | `msg_000001` → consume → `msg_000002`（此路径本身正常） |
| 4 | 并发 `enqueue` 自动 `message_id` 碰撞 | 4 writer × 200 轮 | **200/200 轮全部碰撞** |
| 5 | 套用 event bus 现成跨进程锁后重跑探针 1 | 4 writer × 200 轮 | **丢失 0 条** |
| 6 | `_write_rows` 截断窗口 | 500 行 mailbox，写入期间并发读 | **86/259 次读（33%）观测到不完整或空 mailbox** |

逐条归因：

- **探针 1（75% 丢失）**：`enqueue()` 是典型 read-modify-write——
  `subagent_mailbox.py:30` `rows = self._read_rows()` → `:50` `rows.append(row)` →
  `:51` `self._write_rows(rows)`，全程无锁。N 个并发写入者各自读到旧快照，
  最后一个覆盖前面所有人。丢失量 = (writers-1) × rounds = 3×200 = 600，与实测完全一致，
  说明这不是偶发竞态，而是**结构性必然**。
- **探针 2（46.5% 丢失）**：父进程 `enqueue` 与子进程 `consume_trigger_turn` 并发时，
  `consume_trigger_turn`（`:55` 读 → `:75` 写回全量）会用它读到的旧快照覆盖掉父刚写入的新消息。
  **这是生产路径上的真实场景**：父 `send_message` / `followup_task` 与子 agent 轮询消费天然并发。
  近一半概率静默丢消息——"durable mailbox 是权威源"因此不成立。
- **探针 4（100% 碰撞）**：`:33` `message_id = message_id or f"msg_{len(rows) + 1:06d}"`
  用行数派生 ID。并发写入者读到同样的行数，生成同一个 ID。更糟的是 `:31-32` 的去重逻辑
  会把碰撞消息当成同一条消息返回，**丢失被伪装成幂等命中**。
  注意工具层是否总传显式 `message_id` 决定这条的实际暴露面，但库本身不该有这个坑。
- **探针 5（丢失归零）**：直接复用 `subagent_event_bus.py:117-142` 的 `_locked()`
  （Windows `msvcrt.locking` / POSIX `fcntl.flock`）包住 enqueue，丢失即为 0。
  **修复方案已被实测验证有效，不需要新造锁机制。**
- **探针 6（33% 脏读）**：`:111` `open(self.path, "w")` 先截断再逐行写。
  截断到写完之间，任何读者（含另一个进程的 `consume_trigger_turn`）看到的是残缺文件；
  此刻进程崩溃会**留下被截断的 mailbox**。对比 `subagent_state.py:102-106` 的
  `consume_mailbox_trigger` 用的是 tmp 文件 + `_replace_file()` 原子替换——
  同一仓库里已有正确写法，mailbox 没用上。

**额外发现：仓库里存在两套分叉的 mailbox 消费实现。**
`subagent_state.py:77` `consume_mailbox_trigger(path)` 是第二套实现，被
`agentmain.py:27` import 但**从未调用**（grep 全仓仅命中定义与该 import）。两套语义还不一致：

| | `SubagentMailbox.consume_trigger_turn()` | `consume_mailbox_trigger()` |
| --- | --- | --- |
| 位置 | `subagent_mailbox.py:54` | `subagent_state.py:77` |
| 落盘方式 | `open(...,"w")` 就地截断（**不安全**） | tmp + `os.replace` 原子替换（安全） |
| 消费范围 | trigger 消息**及其之前所有 queue_only**（`:64`） | 只消费第一条 trigger 行（`:96-99`） |
| 返回 | dict（content/messages/consumed_at） | 仅 content 字符串 |
| 是否被调用 | 是（`agentmain.py:969`） | **否（死代码）** |

即：**被调用的那套用的是不安全写法，没被调用的那套用的是安全写法。** 应统一为一套：
保留 `SubagentMailbox` 的消费语义（范围正确），换用 `consume_mailbox_trigger` 的原子落盘方式，
并删除死代码，避免后来者改错文件。

### 3.5 realtime IPC 的安全缺口（第三轮新发现）

在规划把子 agent 接上 realtime channel 之前，必须先记录一个**必须同批修掉**的安全问题：

- `subagent_realtime_ipc.py:39` `SubagentRealtimeChannel.__init__(..., authkey=None)`，
  `:52` `Listener(self.address, authkey=self.authkey, ...)` —— 默认 **authkey 为 None，
  即不做任何认证**。
- 地址是可预测的：`subagent_registry.py:76` `run_id = f"run_{run_no:06d}"` 是顺序编号，
  `subagent_realtime_ipc.py:15` 拼成 `\\.\pipe\ga_subagent_run_000001`。
- 结论：本机任何进程都能猜到管道名并 `Client()` 连上，然后**接收该子 agent 的全部事件流**
  （events 里含 task 内容、工具调用、权限决策）。Windows named pipe 默认 ACL 允许
  同一 session 的其它进程连接。
- 对比 Codex：`tui/src/ide_context/windows_pipe.rs:56-82` 的 `WindowsPipeStream::connect`
  显式调用 `validate_pipe_server_owner` 校验管道服务端所有者。

修复方向：生成 per-run 随机 authkey（写进 `state.json`，仅子进程可读的 task dir 内），
`Listener`/`Client` 双向使用；POSIX socket 侧把父目录权限收到 `0o700`；
Windows 侧补 server owner 校验。**这一条比延迟优化优先级更高——延迟是体验问题，
未认证的事件流是信息泄露。**


## 4. 如果后续改走参考实现路线：实现指引

### 4.1 如果改走 Claude Code 路线（同进程 + 内存队列 + JSONL 只做 transcript）

需要改：

- 子 agent 从 `subprocess` 启 `agentmain.py` 改为**同进程隔离上下文**（Python 层可用
  线程 + 隔离 handler/registry 对象，参考 `forkedAgent.ts:345-462` 的克隆策略：
  克隆只读状态、独立 cancel token、mutation callback 置空、但任务表仍指向 root 以便可见/可 kill）。
- `mailbox.jsonl` 降级为审计日志，live 投递改为进程内 `pending_messages` 列表 +
  "下一个 tool round 注入 attachment"（对应 `attachments.ts:1085-1100`、`query.ts:1547-1589`）。
- 通知改为模块级优先级队列（`messageQueueManager.ts:123-192` 的 `later` 优先级语义）。
- transcript 仍写 JSONL，但接受"运行期注入消息不落盘"（`runAgent.ts:770-789`）。

收益：延迟趋零、无 IPC、无文件锁竞争、实现显著简化。

代价（必须向上明确）：

- 失去未消费消息的崩溃重放能力。
- 失去跨进程恢复：父进程挂了，子 agent 也没了。
- 子 agent 的 Python 异常/内存问题会污染父进程。
- Windows 上失去"进程级取消"这一最可靠的 kill 手段。

**GA 特有的阻塞点（2026-07-29 复核，这是照搬 Claude Code 的最大障碍）：**

Claude Code 能做同进程隔离，是因为它的 `createSubagentContext()` 能把所有相关状态
显式克隆进一个 `ToolUseContext` 对象。GA 目前做不到，因为工具实现依赖**进程级全局状态**：

- `ga.py:435-442`：`inline_eval` 路径直接 `os.chdir(cwd)` 改进程 cwd，再 `finally` 改回。
  两个子 agent 在同进程并发时会互相踩 cwd。
- `agentmain.py:268-281`：`load_tool_schema()` 写模块全局 `TOOLS_SCHEMA`，且按模型名
  在中英文 schema 之间切换。同进程里跑不同 model 的父子 agent 会互相覆盖 schema。
- `code_run()`（`ga.py:26`）起子进程仍靠 `cwd=` 参数，尚可；但 `inline_eval` 与 schema
  全局是硬阻塞。

结论：走 Claude Code 路线的**前置重构成本远大于 IPC 本身**——必须先把 cwd 与
tool schema 从进程全局收进 per-agent context。在没做这层重构前，同进程子 agent
会引入比现在更难查的串扰 bug。

### 4.2 如果改走 Codex 路线（SQ/EQ 队列对 + 内存 thread map + rollout 只做历史）

需要改：

- 引入显式 **SQ/EQ**：父子都通过 `Submission { id, op }` 提交、通过 event 流消费
  （`protocol.rs:123-133`），GA 的 `spawn/send/close/interrupt` 全部收敛成 `Op`。
- 引入 **thread manager**：`{agent_id -> handle}` 内存表 + `send_op`（`thread_manager.rs:198,986`），
  registry 文件退化为持久化镜像而非查询入口。
- `mailbox` 语义映射到 `Op::InterAgentCommunication`（`protocol.rs:663-670`），
  `trigger_turn` 语义可**直接沿用 GA 现有定义**。
- 状态订阅改为"watch 型最新值"语义（`control.rs:880-887`），替代轮询 `state.json`。
- `events.jsonl` 定位为 rollout（replay/resume/fork/审计），并遵守
  "**元数据永不领先 JSONL**"的顺序约束（`live_writer.rs:75-87`）；
  fork/snapshot 前必须先 flush（`control.rs:390-395`）。
- 若要跨进程，走一个统一 transport 层（stdio / UnixSocket / WebSocket，
  `transport/mod.rs:65-70`），而不是每个能力各写一套 IPC。GA 已有的
  `frontends/ink_bridge.py` JSONL stdio 与 Codex `stdio.rs:81-97` 同构，可复用为基座。
- 背压要显式：bounded channel + `Lagged` 语义（`app-server-client\src\lib.rs:130-136`），
  而不是无限增长的文件队列。

收益：清晰的控制面模型、显式背压、低延迟、可 fork/replay。

代价：需要 Python 侧的 async 运行时（asyncio）重构；GA 现有同步文件轮询代码面较大。

#### 4.2.1 代价明细（2026-07-29 第二轮复核后细化）

先纠正一个可能的误解：**Codex 没有父子 agent IPC**（见 2.9）。因此"改走 Codex 路线"
在传输层上并不是"换一种更好的 IPC"，而是**取消 IPC、把子 agent 变成同进程 Tokio task**。
这笔代价与 4.1 Claude Code 路线**是同一笔**，不是更便宜的替代。

把改造拆成三层看，代价差别极大：

| 层 | GA 现状 | Codex 做法 | 改造代价 |
| --- | --- | --- | --- |
| 工具/协议语义 | `spawn/wait/close/send_message/followup_task/list_agents` + `trigger_turn` | 6/6 同名（见 2.10） | **零。已经在 Codex 路线上。** |
| 通知模型 | durable mailbox + realtime channel 推 trigger | `watch::Sender<()>` 空信号 + 回读（见 2.11） | **低。就是 4.3 那三件事。** |
| 运行时/传输 | 独立进程 + 文件轮询 | 同进程 Tokio task + 内存 channel | **高。见下。** |

第三层的具体代价：

1. **Python 没有 Tokio 等价物。** Codex 的低延迟建立在 `tokio::select!` + bounded
   `async_channel` + `watch` + `broadcast` 之上（`session/mod.rs:480-481,629`）。
   Python 侧要么上 asyncio（GA 现有 `agent_loop.py` / `ga.py` / `subagent_manager.py`
   是同步阻塞风格，全链路 async 化是伤筋动骨的重构），要么用 threading + `queue.Queue`
   模拟——后者能拿到低延迟，但拿不到 `tokio::select!` 那种统一取消/超时语义。
2. **同进程要先付掉与 4.1 完全相同的全局状态债**：`ga.py:435-442` 的 `os.chdir(cwd)`、
   `agentmain.py:268-281` 的模块全局 `TOOLS_SCHEMA`。Codex 能同进程跑 N 个 agent，
   是因为 Rust 侧每个 `CodexThread` 的配置都在自己的结构体里，没有进程级可变全局。
3. **子 agent 隔离性归零。** Codex 靠 Rust 的类型系统与无 GIL 并发扛住同进程多 agent；
   Python 同进程多 agent 会共享 GIL、共享异常传播面，一个子 agent 的
   `MemoryError` / 段错误级扩展崩溃会带走父进程。GA 现在"子 agent 崩溃不影响父"是免费得到的。
4. **失去 Windows 上最可靠的取消手段。** 现在 `interrupt_agent` / `close_agent` 最终
   可以落到进程终止；同进程后只能靠协作式 cancel token，而 GA 工具里有
   `code_run()` 这类阻塞调用，协作式取消并不总能及时生效。
5. **失去跨进程恢复。** Codex 用 rollout JSONL + SQLite 做 resume，但那是"重启进程后
   从磁盘重建"，不是"父进程挂了子 agent 还活着"。GA 现在的独立进程模型能做到后者，
   切过去就没了。
6. **SQ/EQ 本身的改造面**：把 `spawn/send/close/interrupt` 全部收敛成 `Op`、引入
   `{agent_id -> handle}` 内存表、状态订阅从轮询 `state.json` 改成 watch 语义——
   这部分是纯收益（模型更清晰），但需要同时改 `subagent_manager.py`（1544 行）、
   `agentmain.py` worker loop、以及 13 个工具的 handler。

**结论：Codex 路线值得抄的是它的「协议 + 通知模型」，而这两层 GA 已经抄到了（2.10）
或只差 4.3 那三件事（2.11）。它的「同进程运行时」不值得抄，因为 GA 是 Python、
而且会把 Codex 用 Rust 换来的隔离性代价原样承担下来。**

如果一定要给一个渐进路径（低风险到高风险）：

- 阶段 A（推荐，等于 4.3）：realtime channel 承载 trigger 信号 + mailbox 加锁 +
  轮询/寿命参数解耦。等价于把 Codex 的 `watch::Sender<()>` 做成跨进程版。
- 阶段 B（可选，纯收益）：把 `spawn/send/close/interrupt` 收敛成显式 `Op` 结构，
  加 `registry.rs:23-26` 式的 agent 深度/数量上限（GA 目前**完全没有**这个守护，
  grep 不到 depth / max_agents），realtime channel 补 bounded + `Lagged` 背压语义。
  这一步不动进程模型，就能拿到 Codex 控制面的清晰度。
- 阶段 C（不建议，除非出现 4.4 的信号）：同进程运行时 + asyncio 化。

### 4.3 建议的折中（若不想全盘切换）——**推荐路线**

结论先说：**建议不切路线，先修 3.3 的三个缺陷。** 理由是 3.3 里三个痛点都不是"文件 vs IPC"
造成的，而是"realtime 通道没接通 + 轮询间隔太长 + mailbox 没锁"；而切 Claude Code 路线
要先付掉 4.1 里 cwd / `TOOLS_SCHEMA` 进程全局状态的重构成本，风险高于收益。

**第二轮复核后这个结论更强了**：Codex 的 `wait_agent` 本身就是
"空负载 watch 信号 + 回读权威源"（2.11，`wait.rs:151` + `input_queue.rs:26`），
所以 4.3 不是"退而求其次"，而是**跨进程版的 Codex 通知模型**。

保留 GA 的 durable-first 权威源，只把**触发信号**实时化：

- P0 **把子 agent 接上 realtime channel**：`agentmain.py` 的 reply 等待循环改为
  "订阅 `ipc_endpoint` → 阻塞等 `conn.poll(timeout)` → 收到 mailbox trigger 事件立即
  `consume_trigger_turn()`"，连接失败则退回轮询。这一步把父→子延迟从 ~1-2s 压到 ~ms，
  且不动权威源语义（收到通知后仍去读 durable mailbox）。
- P0 **mailbox 补跨进程锁**：直接复用 `subagent_event_bus.py:118` 的 `_locked()` 实现，
  或把 consume 改成 append-only 标记行。
- P1 **轮询间隔与寿命解耦**：现在 `reply_wait_iterations * reply_sleep_s` 同时决定
  "轮询粒度"和"空转寿命"两件事，应拆成独立的 `idle_timeout_s` 与 `poll_interval_s`；
  realtime 接通后 poll interval 可以放大而不缩短寿命。
- P1 named pipe 侧补 owner/ACL 校验（参考 `windows_pipe.rs:56-82` 的 `validate_pipe_server_owner`）。
- P2 realtime channel 补背压语义（参考 Codex `AppServerEvent::Lagged`，
  `app-server-client\src\lib.rs:130-136`）：现在 `publish()` 是 best-effort 全量 fan-out，
  慢订阅者会拖住 publish 线程。

这样延迟接近参考实现，但崩溃语义仍保持"未消费消息可重放"，也不需要重构工具层全局状态。

这正是 `docs/ga_subagent_v2_optimization_design_2026-07-27.md` 剩余增强项里
"realtime IPC 承载 mailbox/followup 实时通知，但 durable mailbox 仍为权威源"的技术依据。

### 4.4 什么情况下才真的应该切路线

上面的折中修完之后，如果仍出现下列信号，才值得付重构成本：

- 需要子 agent 与父共享内存态（例如共享 `readFileState` 缓存、共享 MCP 连接池），
  文件/IPC 序列化成本变成瓶颈 → 走 4.1 Claude Code 同进程路线。
- 需要同时管理数十个 agent、需要显式背压与 fork/replay 一等公民 → 走 4.2 Codex SQ/EQ 路线。
- 需要把 attach/detach 做成真正接管子 agent stdin + live tool permission 的交互 runtime
  → 4.1 或 4.2 都比现在的文件协议更顺，因为权限询问是**请求-应答**语义，文件轮询天然不适合。

最后一条值得单独强调：**live tool permission request 是文件协议的真实边界。**
它需要低延迟双向应答，用 `mailbox.jsonl` 轮询实现会很别扭。如果这个能力被列为必须，
那时切路线的理由才是充分的。

## 5. 源码路径索引（便于复查）

Claude Code（`D:\git_codes\claude-reviews-claude\claude-code-fork\src`）：
`utils\sessionStorage.ts`、`tools\AgentTool\runAgent.ts`、`tools\AgentTool\agentToolUtils.ts`、
`tools\AgentTool\AgentTool.tsx`、`tools\AgentTool\resumeAgent.ts`、
`tasks\LocalAgentTask\LocalAgentTask.tsx`、`tools\SendMessageTool\SendMessageTool.ts`、
`utils\attachments.ts`、`utils\messageQueueManager.ts`、`utils\sdkEventQueue.ts`、
`utils\teammateMailbox.ts`、`utils\forkedAgent.ts`、`utils\task\diskOutput.ts`、
`utils\task\framework.ts`、`tools\TaskOutputTool\TaskOutputTool.tsx`、`Task.ts`、`query.ts`

Codex（`D:\git_codes\codex\codex-rs`）：
`protocol\src\protocol.rs`、`core\src\session\mod.rs`、`core\src\agent\control.rs`、
`core\src\agent\registry.rs`、`core\src\agent\thread_manager.rs`、`core\src\client.rs`、
`core\src\client_common.rs`、`tui\src\app.rs`、`tui\src\app_event.rs`、
`tui\src\app_event_sender.rs`、`tui\src\app\thread_events.rs`、`tui\src\app\thread_routing.rs`、
`tui\src\app_server_session.rs`、`tui\src\ide_context\ipc.rs`、`tui\src\ide_context\windows_pipe.rs`、
`app-server-client\src\lib.rs`、`app-server\src\in_process.rs`、
`app-server\src\request_processors\thread_lifecycle.rs`、`app-server\src\bespoke_event_handling.rs`、
`app-server\src\outgoing_message.rs`、`app-server\src\transport.rs`、
`app-server-transport\src\transport\{mod,stdio,unix_socket,websocket}.rs`、
`rollout\src\recorder.rs`、`rollout\src\state_db.rs`、
`thread-store\src\live_thread.rs`、`thread-store\src\local\{live_writer,read_thread}.rs`、
`codex-api\src\sse\responses.rs`、`codex-api\src\endpoint\{responses,responses_websocket}.rs`

## 6. 相关文档

- `docs/ga_subagent_ipc_implementation_plan_2026-07-29.md`——**本调研的落地规划**：
  路线判定、14 项任务优先级总表、每项的 TDD 要求与切片顺序
- `docs/ga_subagent_v2_optimization_design_2026-07-27.md`（本 feat 主设计与进度）
- `docs/ga_subagent_mechanism_research_2026-07-27.md`
- `docs/ga_subagent_codex_reference_2026-07-13.md`
- `docs/ga_codex_vs_ink_stdout_ownership_2026-07-15.md`

