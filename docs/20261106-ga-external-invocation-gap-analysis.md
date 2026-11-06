# GA 被外部调用能力差距与改造方案

> 目标：回答“GenericAgent（GA）离‘被其他 agent / 应用直接调用’还差什么、怎么改”。
> 对照基准是 Claude Code 2.1.303 的原生调用面，见
> `D:/git_codes/claudecode283_hack/docs/20261106-CC被外部调用面调研.md`。
> 本文所有 GA 结论均带 `path:line`；【可见】=代码直读，【推断】=基于结构判断。

---

## 0. 结论速览

1. **GA 不是“没有外部调用能力”，而是“有多个半成品入口，缺一个产品化的宿主协议”。**
   - `GenericAgent` 是可 import 的运行时（`genericagent/core/app.py:681`）；
   - `frontends/ink/bridge.py` 已是一套 JSONL stdio 协议（约 40 条命令、约 40 类事件）；
   - `frontends/acp/genericagent_acp_bridge.py` 已实现 ACP 的 initialize/new/prompt/cancel。
2. **这些入口都停在“UI 适配器”层级，不是公共 API。**
   Ink 协议没有 request id、没有版本协商、没有终态 result，事件形状服务于 UI 状态机；
   ACP 桥只做纯文本流，不支持工具事件、权限、cwd、loadSession。
3. **最关键的六个硬缺口**：
   - 外部不能指定 `session_id` 启动，也不能按 id 恢复（只能按绝对路径）；
   - 主循环没有结构化 tool call/result 事件，工具调用被渲染成 Markdown 文本混在 assistant 流里；
   - 没有统一的 `turn/result` 终态（只有 `status idle` + `assistant_done`）；
   - ACP 桥没有接 `PermissionRuntime`，ask 档在 ACP 下会静默变成 deny；
   - 单进程单会话单 turn，运行中再提交直接 `busy`，没有排队/合并；
   - **主会话没有工作区边界且默认 `full_access`**——不先补这个，外部入口等于任意代码执行。
4. **改造方向不是重写 GA，而是“抽取协议层 + 保留 UI 适配器”**：
   以 `GeneraticAgent` + `PermissionRuntime` + `transcripts` 为内核，
   新增 `genericagent/core/protocol/`（SessionHost + NDJSON codec + server），
   让 Ink、ACP、未来的 Web/HTTP 都变成这个协议层的适配器。
5. **优先级**：P0 做 `ga serve` 的 NDJSON v1（握手、prompt ack、权限、取消、session id、
   turn result、workspace 边界、shutdown、单写者事件管线）；
   P1 做 resume/load、rewind 抽取、ACP 升级、控制面接入；
   P2 做 WebSocket/HTTP、MCP server、多会话进程池。

---

## 0.1 二次审核修订摘要（2026-11-06）

首版方案按“差距→协议→分期”写完后，又按“能不能真的落地”做了一轮代码复核。
以下判断发生了变化，正文已同步修订：

1. **边界要分清“审批”和“文件范围”，不要误伤 full_access 的本意。**
   主会话当前没有 `workspace_root`（`app.py:598-611` 有意不传），默认 `full_access`
   （`policy.py:30-31`）。这是 GA 的既定产品选择，**保留**。
   外部入口要补的是：① 把 `permissionMode` 与 `workspacePolicy` 两个维度显式声明给调用方；
   ② 网络传输默认安全（`ask + strict`），本地 stdio 默认保持 `full_access + unrestricted`。
   不能因为“外部调用”就把本地用户的 full_access 强行降级。
2. **`run()` 没有关闭信号。** `while True: task_queue.get()`（`app.py:1751-1753`）在 stdin EOF 后
   不会退出，只有 daemon 线程被进程强杀；MCP 子进程、workflow store、transcript 都没有收尾。
   P0 必须加显式 shutdown sentinel。
3. **`session.prompt` 不能同步等到 turn 结束。** 否则同一连接上的 `permission.respond`、
   `session.cancel` 永远读不到。修订为：prompt 立即 ack，turn 结束用 `turn/result` 事件通知。
4. **`session/ready` 不能在 `session.new` 时给出完整工具表。**
   handler 是每轮构建的（`app.py:1821`），MCP 工具是后台发现后原地追加的（`app.py:488-512`）。
   修订为：ready 给静态工具 + 当前缓存，后续用 `tools/changed` 事件增量更新。
5. **`assistant/delta` 里混着工具渲染文本。**
   `loop.py:73-138` 把工具头、代码围栏直接 yield 进同一条文本流。
   修订为：事件增加 `channel: "display"|"model"`，并单独给模型原文 `assistant/message`。
6. **rewind 的现场不在 core。** 快照在 `GenericAgentBridge._rewind_snapshots`（`bridge.py:249/547/2808`），
   新协议层直接拿不到。修订：把快照/恢复逻辑抽到 `core/rewind.py`，Ink 与 serve 共用。
7. **多线程事件没有统一顺序。** 并行工具（`loop.py:141-192`）、权限 runtime、MCP 后台发现
   都会从不同线程发事件。修订：单写者 EventWriter + 全局 seq + 有界队列/丢弃策略。
8. **工期被低估。** 补上 shutdown、边界、事件管线、rewind 抽取后，P0 从 1.5-3 周调整为
   **3-5 周**；P1 从 2-4 周调整为 **3-5 周**。

---

## 1. 评估基准与方法

### 1.1 对照基准：CC 的 8 条调用面

| # | CC 方式 | 外部可见入口 |
| --- | --- | --- |
| A | headless 单发 | `claude -p --output-format text/json/stream-json` |
| B | 双向 SDK streaming | `--print --input-format stream-json --output-format stream-json --verbose` |
| C | 远程 I/O | `--sdk-url <ws-url>` |
| D | 后台会话 | `--bg` + `attach/logs/stop/rm` |
| E | 预热池 | `--await-claim` + `claim_session` |
| F | MCP 反向暴露 | `claude mcp serve` |
| G | 内置 SDK | `query()` |
| H | hooks / 权限代答 | `hook_callback`、`--permission-prompts` |

GA 不需要逐条复刻，但 B 的**协议要素**是公共底座：
握手、会话、消息、工具事件、权限请求、取消、终态、恢复。

### 1.2 GA 侧证据来源

- 入口：`ga_cli/cli.py`、`pyproject.toml`、`genericagent/core/app.py:2964-3060`
- 桥接：`frontends/ink/bridge.py`、`frontends/ink/ui/src/protocol.ts`
- ACP：`frontends/acp/genericagent_acp_bridge.py`
- 主循环：`genericagent/core/loop.py`、`handler.py`、`app.py`
- 权限：`genericagent/core/permissions/`
- 持久化：`genericagent/core/transcripts.py`、`paths.py`
- 控制面：`genericagent/core/control/`
- 多代理/workflow：`genericagent/agents/`、`genericagent/workflow/`

### 1.3 证据等级

- **[可见]** 代码直接可读；
- **[推断]** 由命名/结构判断，需运行时确认；
- **[设计]** 本文提出的改造方案，不是现状。

---

## 2. GA 现有资产盘点

### 2.1 可 import 的运行时

`genericagent/core/app.py:681` 的 `GenericAgent`（别名 `GeneraticAgent`，`:2081`）是真正可嵌入的内核：

| API | 位置 | 说明 |
| --- | --- | --- |
| `__init__()` | `app.py:682` | 无参数；会话 id、cwd、模型都从环境/配置推导 |
| `run()` | `app.py:1751` | 单线程阻塞消费 `task_queue` |
| `put_task(query, source, images, model_prefix)` | `app.py:1067` | 返回 per-task `queue.Queue` |
| `abort()` | `app.py:1043` | 整轮取消 |
| `set_permission_mode(mode)` | `app.py:993` | `read_only/ask/full_access` |
| `select_reasoning_effort(effort)` | `app.py:952` | 运行期切换 |
| `next_llm(n)` / `list_llms()` | `app.py:826` / `:981` | 选择后端 |

队列元素只有三种键【可见】：`next`（增量文本，`app.py:1683`）、`done`（终态文本，`:1988`）、
`system`（中途系统提示，`:1541`）。**没有工具调用、没有结果对象、没有 usage 帧。**

### 2.2 Ink JSONL 协议：事实上的第一套宿主协议

- 入口：`frontends/ink/bridge.py:3262 run_jsonl_loop`，可直接 `python -m` 或由 Node 启动。
- 成帧：一行一个 JSON，紧凑序列化 + `\n`（`bridge.py:137-138`）。
- 启动帧：`{"type":"ready","version":1}`（`bridge.py:3268`）。
- 命令约 40 条（`frontends/ink/ui/src/protocol.ts:33-69`），覆盖：
  `submit/stop/new_session/list_resume_sessions/resume_session/resume_session_index/rewind/
   mcp_*/model_switch/set_permission_mode/permission_response/skill_*/compact/
   workflow_*/team_list/hooks_status/shutdown`。
- 事件约 40 类（`protocol.ts:399-468`），包括：
  `assistant_delta/assistant_done/status/token_usage/permission_request/permission_request_settled/
   history_replace/rewind_done/agent_snapshot/agent_event/workflow_*`。
- 权限桥：`bridge.py:428` 把 `emit` 注入 `PermissionRuntime`；应答在 `bridge.py:863-886`。
- 限制【可见】：**没有 request id**，命令与响应靠 `taskId`、`permission requestId`、`workflow runId` 间接关联；
  运行中再提交返回 `busy`（`bridge.py:483-485`）。

结论：它已经能驱动 GA 做很完整的事，但形状是“UI 状态推送”，不是“请求-响应 SDK”。

### 2.3 ACP 桥：已有 0.1 雏形，但被标记 deprecated

`frontends/acp/genericagent_acp_bridge.py`（377 行）：

| ACP 方法 | 现状 | 位置 |
| --- | --- | --- |
| `initialize` | 声明 `loadSession:false`、`mcpCapabilities:{http:false,sse:false}` | `:168-190` |
| `session/new` | 创建 `GeneraticAgent` + 线程；**`cwd` 只存进 `SessionState`，没有应用到 agent** | `:192-212` |
| `session/prompt` | `put_task` → 排空队列 → 仅发 `agent_message_chunk` 文本 | `:214-311` |
| `session/cancel` | 调 `agent.abort()` | `:313-319` |
| `session/load/list` | 返回 `-32601` 不支持 | `:335-338` |
| `session/close` | 直接回 `{}`，不停止 agent | `:339-340` |
| 权限 | **完全没接**：`PermissionRuntime` 无 emit，ask 档 fail-closed 成 deny | 推断自 `runtime.py:72-73` |

会话 id 是 `ga_<uuid4.hex>`（`:199`），与 GA transcript 的 `session_<uuid32>` **没有映射关系**。

### 2.4 权限运行时：现成的最佳插入点

`genericagent/core/permissions/runtime.py`：

- `PermissionRuntime.set_emit(emit)`（`:55`）：注入宿主事件出口；`None` = headless fail-closed。
- `wait_for_decision(tool_name, args, reason)`（`:62`）：发 `permission_request`，Future 挂起等应答。
- `resolve(request_id, decision)`（`:112`）：宿主回写。
- `cancel_all()`（`:126`）：`abort()` 时把所有 pending 按 deny 收尾。
- 事件载荷（`:82-91`）：`type/requestId/toolName/argsPreview/reason/mode`。

主会话的挂载点在 `handler.py:682-715`；workflow/subagent 的 ask 不请求宿主，
直接返回 `approval_required`（`handler.py:672-681`）。

### 2.5 Transcript：接近 CC 的 v2 JSONL

- 位置：`<data_root>/projects/<project_key>/session_<uuid32>.jsonl`（`genericagent/paths.py:411-427`）。
- 事件类型（`core/transcripts.py:143-151`）：
  `session_start`、`message`（含 uuid/parent_uuid/turn_id/blocks/origin）、`turn_end`、
  `working_state`、`compact_boundary`、`summary`、`rewind`、`resumed_from`。
- 能力：`create_session(session_id=...)`（`transcripts.py:154`）、
  `append_message`（`:238`）、`load_v2`（`:973`）、`load_session`（`:1201`）、
  `restore_agent_session`（`:1357`）、`list_sessions`（`:1304`）。
- 恢复时用 projection 重建 backend history（`core/projection.py:250-272`），工具结果只重放不重跑。

限制【可见】：主会话**只在整轮结束时**写 `record_agent_turn`（`app.py:2002-2008`）；
流式中的文本与工具进度不落盘；seq 是进程内计数（`transcripts.py:20-25`），跨进程会重计。

### 2.6 控制面：已有，但只服务子代理/workflow

`genericagent/core/control/control.py:39` 定义 `AgentControlAdapter`，
`:63` 的 `UnifiedAgentControl` 提供：

```text
list_records / get_record / events_since / read_result / control(action)
```

事件模型 `AgentEvent`（`core/models.py:236`）已带 `event_id/sequence/execution_id/
parent_execution_id/source_cursor`，`AgentEventBatch` 还带游标（`:378`）。

现状【可见】：只在 `frontends/ink/bridge.py:355/378/1889` 使用；
适配器只有 `ProcessSubagentAdapter`（子代理）与 `WorkflowChildAdapter`（workflow），
**没有主会话适配器**。

### 2.7 其余可复用能力

| 能力 | 现状 | 坐标 |
| --- | --- | --- |
| Hooks | 33 个 CC 兼容事件名，20 个已集成；command/http/mcp_tool 可由外部进程提供 | `hooks/models.py:14-62`、`hooks/runtime.py:303-340`、`hooks/runner.py:125-435` |
| MCP | 纯 client（fastmcp），stdio/http/sse，工具名 `mcp__<server>__<tool>` | `mcp/runtime.py:661-674`、`:1208` |
| 子代理 | 真实子进程 + 文件控制面 + 实时 IPC | `agents/manager.py:817/1057`、`agents/registry.py:175-235`、`agents/realtime_ipc.py:213` |
| Workflow | Controller + Store + journal/state 文件契约 | `workflow/controller.py:11-74`、`workflow/runtime.py:193`、`workflow/store.py:154-172` |

---

## 3. 差距矩阵

### 3.1 入口 / CLI 层

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 无头协议入口 | 只有 `python -m frontends.ink.bridge` 与 `python -m frontends.acp...` | 没有 `ga serve`；没有 `-p` 一次性输出 | `ga_cli/cli.py:324-400`、`bridge.py:3439-3442` |
| 启动参数 | `ga cli` 的子进程参数有 `--task/--input/--fork_*/--permission_*` | 无 `--session-id`、`--resume`、`--cwd`、`--permission-mode`、`--mcp-config`、`--max-turns` | `app.py:2964-2987` |
| 入口地位 | ACP 被文档标为 deprecated，`ga acp` 未注册 | 外部用户没有“官方支持”的调用命令 | `docs/20261022-GA入口收敛与旧前端退役说明.md:24` |
| 输出格式 | Ink 只有 JSONL；CLI 是 REPL | 无 `text/json/stream-json` 三档 | `bridge.py:137`、`app.py:2968+` |

**影响**：其他 agent 要接 GA，必须先知道“去跑哪个隐藏模块”，且没有任何稳定契约。

### 3.2 握手与会话标识

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 握手 | Ink 只发 `{"type":"ready","version":1}`；ACP 有 `initialize` 但无能力协商 | 没有 `system/init` 等价帧（session_id、tools、model、permissionMode、mcp servers） | `bridge.py:3268`、`acp_bridge.py:168-190` |
| session id | `session_<uuid32>` 自动生成；`create_session(session_id=...)` 支持但无外部入口 | 外部不能指定 id；ACP 的 `ga_<hex>` 与 transcript 无关 | `transcripts.py:121/154`、`app.py:759`、`acp_bridge.py:199` |
| cwd | `tool_cwd` 由环境/安装目录决定；`session/new` 的 cwd 被忽略 | 外部无法按会话切换工作目录 | `app.py:741-755`、`acp_bridge.py:192-202` |
| 协议版本 | Ink 无协商；ACP 固定 `ACP_PROTOCOL_VERSION=1` | 无 `protocolVersion` 双向确认、无能力声明 | `bridge.py:3268`、`acp_bridge.py:56/169-170` |

**影响**：宿主无法把 GA 会话纳入自己的会话表；断线后无法按 id 找回；多项目并行会互相踩目录。

### 3.3 消息模型

| CC 帧 | GA 对应 | 差距 |
| --- | --- | --- |
| `system/init` | 无 | 需要新造：会话元数据 + 工具清单 + 模型 + 权限模式 |
| `user`（content blocks） | Ink `submit {text, images[]}`；`put_task` 只收字符串 | 无 text/image/tool_result 内容块数组；图片是路径列表 |
| `assistant`（content blocks） | `assistant_delta/assistant_done` 纯文本 | 工具调用被渲染成 Markdown 文本（`loop.py:73-78/106/119-138`） |
| `stream_event` | 无 | 只有文本增量，没有 message_start/content_block_delta |
| `result` | 无 | 只有 `status idle` + `assistant_done`；缺少 is_error/usage/duration/num_turns |
| `control_request/response` | 无通用通道 | 权限/工作流各有专用消息，无 request id |

**影响**：宿主无法可靠地区分“模型说的话”和“工具产生的文本”，也无法做结构化渲染或审计。

### 3.4 工具事件

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 工具调用事件 | 无结构化对象；`BaseHandler.dispatch` yield 文本 | 需要 `tool/call`：tool_use_id、name、input、agent_id | `loop.py:20-31`、`loop.py:73-138` |
| 工具结果事件 | `StepOutcome.data` 只回填下一条消息 | 需要 `tool/result`：status、content、duration | `loop.py:257-272`、`handler.py:569-571` |
| 工具状态 | `success/error/approval_required/timeout/cancelled` | 需要稳定的状态枚举与错误码 | `handler.py:569-571` |
| 子代理工具 | `agent_event` 从控制面轮询 | 需要统一进主流或明确旁路 | `bridge.py:1889`、`control/process.py:65` |

**影响**：外部宿主看不到“正在跑什么工具”，只能看到文本里偶然出现的代码围栏；做不了权限 UI、进度条、审计。

### 3.5 权限

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 请求通道 | `PermissionRuntime` + `permission_request/response` | Ink 已接；**ACP 未接** | `runtime.py:55-100/112`、`bridge.py:428` |
| 载荷 | `toolName/argsPreview/reason/mode` | 缺 `toolUseId`、完整 `input`、`suggestions`、`agentId`、`timeoutMs` | `runtime.py:82-91` |
| 应答 | 仅 `accept/deny` | 缺 `updatedInput`、`message`、session/always | `runtime.py:19-21` |
| 持久化 | 三档 mode 仅内存，session-only | 外部不能存策略；重启丢档 | `bridge.py:839-855` |
| 主会话边界 | 默认 `full_access`，无路径 ACL | 外部调用方无法约束写入范围 | `policy.py:30-31`、`handler.py:789-790` |
| 子代理 ask | 直接 `approval_required`，不请求宿主 | 宿主无法统一审批 | `handler.py:672-681` |

**影响**：把 GA 接到编辑器/其他 agent 时，最需要的“每次写文件前问我”在 ACP/headless 下不可用。

### 3.6 取消、超时与并发

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 取消粒度 | `abort()` 整轮 | 无单工具取消、无 turn_id 定位 | `app.py:1043` |
| 工具响应取消 | 只有 `code_run` 轮询 `stop_sig` | `web_scan/file_read/spawn_agent` 等不检查 | `handler.py:148-153` |
| 并行工具 | 收集循环无取消检查，线程 `join(timeout=1.0)` 后丢弃 | 在途工具会继续跑；生成器未 `close()` | `loop.py:180-194`、`app.py:1661-1688` |
| 超时 | `code_run` 60s、MCP 60s、`wait_agent` 有 | dispatch 层与多数工具无超时 | `handler.py:875`、`mcp/runtime.py:457` |
| 并发 | `run()` 单线程串行；运行中提交 `busy` | 无排队/fold、无多会话 | `app.py:1751`、`bridge.py:483-485` |

**影响**：外部宿主无法实现“打断当前工具、保留本轮”“并发两个任务”“超时兜底”。

### 3.7 持久化与恢复

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 按 id 恢复 | 只有绝对路径恢复；`load_session(path)` | 无 `load_session_by_id(id, root)` | `transcripts.py:1201/1357` |
| 增量读取 | 无 | 无 `events_since(seq)`；重连只能全量 load | `transcripts.py:61-62` |
| 轮次写入 | 整轮结束才写 `record_agent_turn` | 流式中的文本/工具进度不落盘 | `app.py:2002-2008` |
| 跨进程安全 | 进程内 RLock；seq 从 1 重计 | 无文件锁/租约；双写会交错 | `transcripts.py:20-25/133-140` |
| fork | 仅 TUI `/branch` 深拷贝内存，不复制 transcript | 无血缘；分支续聊可能丢上下文 | `tuiapp.py:458-476`、`transcripts.py:1588-1602` |
| ACP load | `session/load` 不支持 | 编辑器无法恢复上一会话 | `acp_bridge.py:335-338` |

**影响**：外部宿主断线重连 = 会话丢失；这是“可被调用”与“可被长期托管”的分水岭。

### 3.8 传输与部署

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 传输 | stdio（Ink/ACP） | 无 WebSocket/HTTP/Unix socket 服务端 | `bridge.py:137`、`acp_bridge.py:354-364` |
| 鉴权 | 无 | 无 token/来源校验 | 同处 |
| 多路复用 | 单进程单会话 | 无 session registry / 连接会话映射 | `app.py:1751` |
| stdout 卫生 | 核心多处 `print`（`handler.py:93/107/151`、`app.py` 多处） | 协议模式下必须重定向；ACP 已用 fd dup 处理 | `acp_bridge.py:16-42` |

**影响**：只能在同机同进程族内用；无法做“一个 GA 服务端，多个客户端”。

### 3.9 多代理与 workflow 的可观察性

| 项 | GA 现状 | 缺口 | 证据 |
| --- | --- | --- | --- |
| 统一控制面 | `UnifiedAgentControl` 已合并子代理/workflow 的 records/events/result | 只在 Ink bridge 内使用，无外部入口；无主会话适配器 | `control/control.py:63`、`bridge.py:355/378` |
| 事件游标 | `AgentEventBatch.next_cursors` | 未暴露为协议方法 | `models.py:378` |
| 子代理事件 | 文件/IPC 双通道 | 外部需要 replay + 实时订阅的一致语义 | `agents/event_bus.py:74`、`realtime_ipc.py:213` |
| workflow 观察 | journal/state/live 文件 | 未进协议；ACP 完全不支持 | `workflow/store.py:154-172`、`acp_bridge.py:321-340` |

**影响**：外部宿主能看到“主 agent 说了什么”，但看不到“它派了哪些子代理、workflow 跑到哪”。

### 3.10 安全与隔离

| 项 | GA 现状 | 缺口 |
| --- | --- | --- |
| 主会话路径边界 | 默认 `workspace_root=None`，无 PathACL | 外部调用方无法声明工作区边界（`handler.py:789-790`） |
| 工具注入 | 只有 MCP；无运行时注册 | 宿主不能提供自己的工具（`handler.py:746-752`） |
| 审批 | 有 runtime，但 ACP/headless 默认 deny | 外部必须在协议里回答，否则工具不可用 |
| 密钥 | 环境/`llm.yaml` | 协议层无凭据隔离概念 |

**影响**：把 GA 暴露给外部应用前，必须先补“工作区 + 审批 + 工具来源”三件事，否则是裸奔。

### 3.11 首版方案自身的漏洞（二次审核清单）

| # | 严重度 | 漏洞 | 证据 | 修订 |
| --- | --- | --- | --- | --- |
| H1 | 高（分传输） | 外部入口会隐式继承 `full_access + 无边界`：本地可信调用没问题，网络/第三方调用危险（§12 已实测；**用户确认为产品本意，保留不修**） | `app.py:598-611`、`policy.py:30-31` | 拆成 `permissionMode` + `workspacePolicy` 两个独立维度；本地 stdio 保持现状，网络默认 `ask + strict`，`ready` 显式声明 |
| H2 | 致命 | `run()` 无 shutdown sentinel，EOF 后无法收尾（§12 已实测，**已修复**） | `app.py:1751-1753` | 新增 `request_shutdown()` + 哨兵任务 + 清理顺序 |
| H3 | 高 | `session.prompt` 若同步等待，权限应答/取消读不到 | 协议设计缺陷 | prompt 立即 ack，终态走 `turn/result` |
| H4 | 高 | `session/ready` 在 new 时工具表不完整 | `app.py:1821`、`:488-512` | ready 给静态+缓存；新增 `tools/changed` |
| H5 | 高 | `assistant/delta` 混入工具渲染文本，宿主会重复渲染 | `loop.py:73-138` | 增加 `channel` 字段与模型原文 message |
| H6 | 高 | rewind 快照在 Ink bridge，serve 无法复用 | `bridge.py:249/547/2808` | 抽 `core/rewind.py`，两处共用 |
| H7 | 高 | 主会话 `tool_use_id` 可能为空（`no_tool`/部分 provider）（**已修复：合成 `tool_<turn>_<index>`**） | `loop.py:228/237` | 事件层合成稳定 id，并保持 call/result 一致 |
| H8 | 高 | 按 id 加载需要 project root；全局扫描不可行 | `transcripts.py:125/1297` | `session.load` 增加 `cwd/workspaceRoot` 参数，限定目录 |
| H9 | 高 | 事件来自多线程，seq/顺序无保证 | `loop.py:141-192`、`runtime.py:82` | 单写者 EventWriter + 锁 + 全局 seq |
| H10 | 高 | transcript 单写者只是口头约定 | `transcripts.py:20-25/133-140` | 会话租约/文件锁；第二写者拒绝或只读 |
| H11 | 高 | 权限载荷可能带完整文件内容/密钥 | `runtime.py:82-91` | 事件脱敏 + 大小上限 + `argsPreview` 双轨 |
| H12 | 中 | 子代理/workflow 的 ask 不走宿主，语义不一致 | `handler.py:672-681` | P1 明确“策略档案”边界；P2 再代理 |
| H13 | 中 | 慢客户端会阻塞 agent 线程（同步写 stdout） | 协议设计缺陷 | 有界队列 + writer 线程 + 丢弃策略 |
| H14 | 中 | `session.new` 的 `model` 字符串未定义映射 | `app.py:856/826` | 明确用 `select_llm(selector)`，返回实际模型 |
| H15 | 中 | `session.load` 历史回放格式未定义 | `projection.py:250-272` | 返回 `messages[]` + 分页上限 |
| H16 | 中 | ACP `tool_call` 的 kind/locations 未映射（**已实现 GA 工具 → ACP kind 映射**） | ACP 规范 | 增加 GA 工具 → ACP kind 映射表 |
| H17 | 中 | serve 模式下 hooks 输出去向未定义 | `bridge.py:403`、`hooks/runtime.py:367-423` | hook 事件走协议或禁用 `hook_emit` |
| H18 | 中 | MCP 子进程可能继承协议 stdout | `acp_bridge.py:16-42` 的对策 | serve 启动即 dup2 + 标记 fd 不可继承 |
| H19 | 中 | 进程模型未写死（一个 workspace 一个进程） | `identity_from_environment`、`paths` | v1 明确单会话单 workspace；多会话走进程池 |
| H20 | 低 | 实施时工作区已有未提交改动 | `git status`（app.py/ga_cli 等） | 先定基线/分支，避免覆盖他人改动 |

---

## 4. 改造方案

### 4.1 目标分层

```text
外部客户端（其他 agent / 编辑器 / Web / CLI / ACP client）
        │  目标协议：NDJSON v1（首选）或 ACP（外壳）
        ▼
genericagent/core/protocol/           ← 新增：协议层（可测试、无 UI 依赖）
  SessionHost  ── codec ── server
        │
        ├── GeneraticAgent（core/app.py）
        ├── PermissionRuntime（core/permissions/runtime.py）
        ├── transcripts（core/transcripts.py）
        ├── UnifiedAgentControl（core/control/）
        └── hooks / MCP / workflow / agents
        ▲
        └── 适配器：frontends/ink（现有）、frontends/acp（升级）、未来 web/http
```

原则：

1. **内核不感知客户端**：协议层只消费 `GeneraticAgent` 的队列与 emit。
2. **协议层无 UI 依赖**：不 import Ink、不读 `protocol.ts` 语义。
3. **UI 与协议并行**：保留现有 Markdown 渲染；新增结构化事件，不替换文本。
4. **会话主键 = transcript session_id**：外部指定、可恢复、可审计。
5. **失败可见**：所有错误都要有结构化 code，不能只写 stderr。

### 4.2 新增 `genericagent/core/protocol/`

| 文件 | 职责 | 关键接口 |
| --- | --- | --- |
| `events.py` | 规范事件模型 + 版本 | `HOST_PROTOCOL_VERSION = 1`；`HostEvent(type, seq, session_id, turn_id, payload, uuid)` |
| `writer.py` | 单写者事件出口 | `EventWriter.emit/critical`；锁 + 全局 seq + 有界队列 + 丢弃策略 |
| `session_host.py` | 一个生成器/会话的生命周期 | `start_new(session_id, workspace_root, permission_mode)`, `resume(session_id, workspace_root)`, `submit(content)`, `interrupt()`, `close()` |
| `ndjson.py` | 成帧与解析 | `encode(frame)->bytes`, `decode(line)->frame`；单行上限（建议 8 MiB） |
| `methods.py` | 方法分发 | `initialize/session.*/permission.respond` 表 |
| `server.py` | stdio 主循环 + 并发保护 | `serve(stdin, stdout)`；读线程与 agent 线程分离 |
| `acp_adapter.py` | ACP 映射（P1） | `session/update`、`session/request_permission` |
| `ink_adapter.py` | 兼容层（P2，可选） | 把 HostEvent 转成现有 Ink 事件 |

`SessionHost` 的内部职责：

1. 创建/恢复 `GeneraticAgent`，设置 session_id 与 workspace_root；
2. 调用 `put_task` 并消费 display_queue，转成 `assistant/delta` 与 `assistant/message`；
3. 把 `PermissionRuntime.set_emit` 接到协议事件；
4. 在 turn 结束写 transcript，并发出 `turn/result`；
5. 管理 `abort()`、排队与超时；
6. 向 `UnifiedAgentControl` 注册主会话适配器（P1）。

### 4.3 协议 v1 规格（NDJSON）

#### 4.3.1 帧与并发模型

```text
请求:  {"id":"<client-id>","method":"session.prompt","params":{...}}
响应:  {"id":"<client-id>","result":{...}} | {"id":"...","error":{"code":"...","message":"..."}}
事件:  {"type":"event","event":{"type":"turn/result","seq":12,...}}
```

硬性规则（首版方案漏掉的）：

1. **读线程与 agent 线程分离**：读循环只做解析、校验、分发，绝不等 turn 结束。
2. **`session.prompt` 立即回 ack**：`{"turnId": "...", "accepted": true}`；
   完成通过 `turn/result` 事件通知。否则同一连接上的 `permission.respond` / `session.cancel` 无法送达。
3. **单写者**：所有事件经一个 `EventWriter`（锁 + 单调 `seq`）写出；
   并行工具线程、权限 runtime、MCP 后台发现都只能投递，不能直接写 stdout。
4. **有界队列**：建议 10,000 条；满时先丢 `assistant/delta`，永不丢
   `turn/result`、`permission/request`、`error`。
5. **写线程独立**：慢客户端只阻塞写线程，不阻塞 agent；超时后断开连接。

#### 4.3.2 方法

| 方法 | 参数 | 结果 | 说明 |
| --- | --- | --- | --- |
| `initialize` | `protocolVersion`, `clientInfo`, `capabilities` | `protocolVersion`, `serverInfo`, `capabilities` | 版本取交集；不支持则 `unsupported_version` |
| `session.new` | `sessionId?`, `workspaceRoot?`, `workspacePolicy?`, `permissionMode?`, `model?`, `dataDir?` | `sessionId`, `model`, `permissionMode`, `workspacePolicy` | 本地可省略 workspaceRoot（用 serve 的默认）；`strict` 下必填 |
| `session.load` | `sessionId`, `workspaceRoot` | `messages[]`, `nextCursor?` | 限定该 workspace 的 session 目录（H8） |
| `session.list` | `workspaceRoot`, `limit?`, `cursor?` | 摘要列表 | 只扫该 workspace |
| `session.prompt` | `sessionId`, `content[]`, `turnId?` | `turnId`, `accepted` | **立即 ack**（H3） |
| `session.cancel` | `sessionId`, `turnId?` | `cancelled`, `turns[]` | 运行中也可达 |
| `session.rewind` | `sessionId`, `turnId`, `dryRun?` | `filesChanged`, `insertions`, `deletions` | 依赖抽出的 `core/rewind.py`（H6） |
| `session.set_mode` | `sessionId`, `mode` | 当前模式 | 运行中允许，作用于下一次 dispatch |
| `session.set_model` | `sessionId`, `model` | 实际模型 | 运行中返回 `busy` |
| `session.close` | `sessionId` | `{}` | 只关会话，不杀进程 |
| `permission.respond` | `requestId`, `decision`, `updatedInput?` | `{}` | 未知/过期 id 返回 `unknown_request`（幂等） |
| `shutdown` | — | `{}` | 停 agent、MCP、workflow，进程退出 |

#### 4.3.3 事件

| 事件 | 关键字段 | 对应 CC | 备注 |
| --- | --- | --- | --- |
| `session/ready` | sessionId, workspaceRoot, workspacePolicy, model, permissionMode, tools[], mcpServers[], commands[], toolsPending | `system/init` | 工具表可能不完整（H4）；两个安全字段必须回显 |
| `tools/changed` | added[], removed[], reason | — | MCP 后台发现完成时发 |
| `turn/queued` | turnId, position | — | 仅在启用排队时 |
| `turn/started` | sessionId, turnId | — | |
| `assistant/delta` | turnId, text, channel | `stream_event` | `channel:"display"|"model"`（H5） |
| `assistant/message` | turnId, content[], text | `assistant` | 模型原文，不含工具渲染 |
| `tool/call` | turnId, toolUseId, name, input, agentId? | `assistant.tool_use` | toolUseId 为空时合成（H7） |
| `tool/result` | turnId, toolUseId, status, content, truncated, artifactRef?, durationMs | `user.tool_result` | 大结果给引用，不内联 |
| `permission/request` | requestId, turnId, toolUseId, toolName, argsPreview, inputHash, reason, options[] | `can_use_tool` | 不默认内联完整敏感内容（H11） |
| `permission/resolved` | requestId, decision, reason | `control_response` | |
| `usage` | inputTokens, outputTokens, cache*, costUsd? | `result.usage` | |
| `turn/result` | turnId, stopReason, isError, text, durationMs, numTurns, usage | `result` | 终态；错误也走这里 |
| `session/state` | status: idle/running/stopping | — | |
| `error` | code, message, retryable, requestId? | — | 传输/协议错误，不表示 turn 结束 |

#### 4.3.4 顺序与投递

- `seq` 在**写出时**分配，保证单连接全序；
- 同一 turn 内顺序契约：`turn/started` → (`assistant/delta` | `tool/call` | `tool/result` | `permission/*`)* → `assistant/message` → `turn/result`；
- `assistant/message` 可能早于最后一个 `tool/result` 发出（模型原文在响应到达时就有），宿主不能假设它是最后一条；
- 断线重连只保证 **message 级**持久事件（transcript），不保证 delta 级（P1 的 `events_since`）。

#### 4.3.5 版本与能力

`initialize` 必须带能力声明，宿主据此降级：

```json
{"protocolVersion":1,
 "capabilities":{"toolEvents":true,"permissions":true,"resume":false,
                 "rewind":false,"workflow":false,"subagents":false,
                 "images":true,"mcp":true}}
```

- 版本策略：客户端发 `minVersion/maxVersion`，服务端取交集；无交集返回 `unsupported_version`。
- 新增能力只能加字段/事件，不能改变已有字段语义；未知事件宿主必须忽略。

#### 4.3.6 错误码

```text
bad_request / invalid_params / unknown_method / unsupported_version
unknown_session / session_locked / workspace_invalid / busy / not_running
permission_denied / permission_timeout / unknown_request
cancelled / timeout / model_error / tool_error / transcript_error
agent_exited / internal_error
```

规则：**一个 turn 的失败用 `turn/result{isError:true, stopReason:"error"}` 表达**；
`error` 事件只用于协议/传输/请求级失败。

#### 4.3.7 边界语义（必须写死）

| 场景 | 语义 |
| --- | --- |
| `session.new` 第二次 | 同 workspace 返回 `session_exists`；不同 workspace 返回 `workspace_mismatch`；都不隐式切换 |
| `workspaceRoot` 省略 | 用 serve 启动参数或进程 cwd；`strict` 下必须显式，否则 `invalid_params` |
| `dataDir` | 只在进程内第一次构造 agent 前生效；之后传返回 `invalid_params` |
| 重复 `turnId` | 幂等：返回同一 turn 的 ack，不重复入队 |
| `session.close` 时 turn 正在跑 | 先 abort，pending 权限全部 deny，保留 transcript，再返回 |
| `shutdown` 时仍有请求 | 先回 `error{cancelled}`，再按 §4.7 收尾 |
| `session.prompt` 内容块 | P0 只支持 `text`；未知块返回 `invalid_params`，不要静默忽略 |
| 以 `/` 开头的 prompt | 默认按 GA slash 命令处理；宿主可传 `slashCommands:false` 让文本原样进模型 |
| `session.rewind` 时 turn 正在跑 | 返回 `busy`；先 cancel 再 rewind |


### 4.4 核心改造点（按文件）

#### `genericagent/core/app.py`

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| 构造参数 | `:682` | `GenericAgent(session_id=None, workspace_root=None, permission_mode=None)`；不传则保持现状 |
| 会话 id 注入 | `:759` → `transcripts.ensure_agent_session` | 允许传入外部 id；返回实际 id |
| cwd 注入 | `:741-755` | 改为 `set_workspace(path)`：经 `GA_PROJECT_ROOT` 语义重算 `project_root/data_root/项目身份`，并设置 `tool_cwd`；不要只改 `tool_cwd` |
| hosted handler | `:598-611`、`:1811-1821` | 仅在 `workspacePolicy=strict` 时用 `build_hosted_handler(..., workspace_root)`；`unrestricted` 保持 `build_main_handler` |
| `put_task` 返回句柄 | `:1067` | 返回 `{queue, turnId, sessionId}`，而不是裸 Queue；保留旧接口兼容 |
| 关闭信号 | `:1751-1753`、`:2070-2079` | `request_shutdown()` + 哨兵任务；`run()` 收到后 break 并执行清理 |
| turn 上下文 | `:1817` 前后 | 在进入 `agent_runner_loop` 前设置 `current_turn_id`（contextvar/属性），供权限事件取用 |
| 结果对象 | `:1981-2008` | 汇总 `exit_reason`、`_classify_turn_outcome`、usage、整轮文本为 `TurnResult` |
| 初始化帧 | `:1821` 之后 | 发 `session/ready`（静态工具 + 当前 MCP 缓存）；不等待后台发现 |

#### `genericagent/core/loop.py`

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| 事件出口 | `:201` | `agent_runner_loop(..., event_sink=None)`；不传则行为不变 |
| 工具调用事件 | `:237-272` | 在 dispatch 前发 `tool/call`，后发 `tool/result`；文本渲染保持原样 |
| 工具 id 合成 | `:228`、`:237` | `tid` 可能为空（`no_tool`/无 id 的 provider）；为空时合成 `tool_<turn>_<index>`，并保证 call/result 一致 |
| 文本通道标记 | `:73-138` | 文本帧带 `channel:"display"|"model"`；模型原文另走 `assistant/message` |
| 取消检查 | `:180-194` | 收集循环加入 `stop_sig` 检查；`join` 后显式清理 |
| 生成器收尾 | `app.py:1661-1688` | break 时 `gen.close()`，避免在途工具继续跑 |

#### `genericagent/core/handler.py`

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| dispatch 边界 | `:554/591` | 把 tool_use_id、参数、结果 status 交给事件汇 |
| 权限请求 | `:682-715` | 保持 `PermissionRuntime` 调用；补充 `tool_use_id`、完整 input、agent_id |
| stdout | `:93/107/151` 等 | 引入 `stdout_sink`（默认 `sys.stdout`）；协议模式指向 stderr |

#### `genericagent/core/permissions/runtime.py`

- `wait_for_decision` 增加 `tool_use_id`、`input`、`suggestions`、`timeout_ms`；
- `permission_request` 增加 `toolUseId`、`agentId`、`options:[accept,deny]`；
- `resolve` 支持 `updatedInput`（与 CC 的 `behavior=allow + updatedInput` 对齐）；
- 超时默认 deny，并记 `permission_request_settled(reason="timeout")`。

#### `genericagent/core/transcripts.py`

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| 按 id 加载 | `:1201` 附近 | `load_session_by_id(session_id, workspace_root)`；只在 workspace 的 session 目录内解析 |
| 增量读取 | `:61-62` | `iter_events_since(path, seq)`，返回 `(events, next_seq)` |
| 轮次内增量 | `:177` | 可选：`append_message` 每次 assistant/tool 事件即写；至少保证 `turn_end` 前可恢复 |
| 跨进程锁 | `:133-140` | `portalocker`/msvcrt 锁；或声明“单写者”并在启动时检查 |
| fork | `:1588-1602` | `fork_session(src_id, new_id)`：复制事件 + 写 `forked_from` 血缘 |

#### `genericagent/core/rewind.py`（新增，抽取自 Ink bridge）

`GenericAgentBridge._rewind_snapshots`（`bridge.py:249/547`）、`_snapshot_agent_state`
（`:2830-2870`）、`_restore_agent_state`（`:2872+`）目前只服务 Ink。
`ga serve` 要支持 `session.rewind`，必须把它们抽成无 UI 依赖的模块：

```python
class RewindStore:
    def snapshot(agent) -> dict
    def restore(agent, snapshot) -> bool
    def prune(after_turn_id: int) -> None
```

Ink bridge 与 `SessionHost` 共用同一实现；否则协议层的 rewind 只能是空壳。

#### `genericagent/core/protocol/writer.py`（新增，单写者）

```python
class EventWriter:
    def emit(self, event_type: str, payload: dict) -> None  # 分配 seq、加锁、入有界队列
    def critical(self, event_type: str, payload: dict) -> None  # 永不丢弃
```

所有线程（turn、并行工具、权限 runtime、MCP 发现、hooks）只调用 `EventWriter`，
由独立写线程落到 stdout；慢客户端不阻塞 agent。

#### `genericagent/core/control/`

新增 `session.py`：

```python
class MainSessionAdapter:
    engine = "session"
    def list_records(...) -> list[AgentRecord]   # 主会话/轮次
    def get_record(execution_id) -> AgentRecord | None
    def events_since(cursors) -> AgentEventBatch
    def read_result(execution_id) -> AgentResultRecord
    def control(execution_id, request) -> ControlResult  # interrupt/close
```

这样外部宿主用同一套 `list/get/events/result/control` 观察主会话、子代理、workflow。

#### `ga_cli/cli.py`

在 `:324 main` 增加 `serve` 命令：

```bash
ga serve --protocol ndjson [--session-id ID] [--workspace-root DIR] [--data-dir DIR] [--permission-mode ask]
ga serve --protocol acp
ga run -p "prompt" --output-format text|json|stream-json   # 可选的一次性模式
```

`serve` 不应走 `subprocess.Popen`；直接在进程内构造 `SessionHost`。

#### `frontends/ink/bridge.py`

P0 **不改**。P1 再决定：要么把 `emit` 适配到 `HostEvent`（双向兼容），
要么让 Ink 继续用现有事件，协议服务器另起一条 emitter。避免一次性重构 UI。

### 4.5 ACP 适配器升级（P1）

以 `SessionHost` 为底座重写 `frontends/acp/genericagent_acp_bridge.py`：

| ACP 能力 | 改造 |
| --- | --- |
| `session/new` | 应用 `cwd`；`sessionId` 映射到 GA transcript id；返回 `modes` |
| `session/load` | 调 `load_session_by_id`；`agentCapabilities.loadSession=true` |
| `session/prompt` | `SessionHost.submit`；流式发 `agent_message_chunk` + `tool_call`/`tool_call_update` |
| `session/request_permission` | 把 `permission/request` 映射成 ACP 权限请求；支持 allow/deny |
| `session/cancel` | `SessionHost.interrupt()` |
| `session/close` | 真正停止并释放 agent |
| MCP | `initialize.mcpCapabilities` 按实际支持声明；可选把 ACP `mcpServers` 转成 GA MCP 配置 |
| 能力声明 | 不再固定 `loadSession:false`；按实现逐步打开 |

GA 工具 → ACP `tool_call.kind` 映射（否则编辑器只能显示 “other”）：

| GA 工具 | ACP kind |
| --- | --- |
| `file_read` | `read` |
| `file_write` / `file_patch` | `edit` |
| `code_run` | `execute` |
| `web_scan` / `web_execute_js` / `web_fetch` | `fetch` |
| `spawn_agent` / `workflow` | `other` |
| `mcp__<server>__<tool>` | `other`，`rawInput` 保留原名 |

ACP 权限应答是 `outcome: {outcome:"selected", optionId:"allow_once"|"reject_once"}`
或 `{outcome:"cancelled"}`；GA 的 `updatedInput` 不属于 ACP 基线，不要在 ACP 层承诺。

**注意**：ACP 的 `fs/*` / `terminal/*` 是“客户端提供工具”的模型，与 GA“工具在 agent 进程内”冲突。
GA 应像 CC 的 adapter 一样**降级/忽略**这些客户端能力，不要把工具搬到客户端执行。

### 4.6 一次性模式（可选）

如果只想要“CI 跑一次”，可在 P0 顺手加：

```bash
ga run -p "分析这个仓库" --output-format json
```

输出 `turn/result` 等价的单个 JSON。实现成本低，但它不能替代 `serve`。

### 4.7 生命周期与关闭（首版漏项）

`GeneraticAgent.run()` 是 `while True: task_queue.get()`（`app.py:1751-1753`），没有退出条件。
`ga serve` 必须显式补一条关闭链路：

```text
stdin EOF / shutdown 请求
  → 停止接收新请求
  → permission_runtime.cancel_all()        # pending 全部按 deny
  → abort()                                # 停当前 turn
  → 队列中未开始的 turn 逐个回 turn/result{cancelled}
  → 向 task_queue 投递 shutdown 哨兵
  → join agent 线程（建议 10s，超时记日志并返回非零）
  → close MCP clients / workflow store / hooks runtime
  → flush + close transcript
  → flush stdout，进程退出
```

实现要点：

- `app.py` 增加 `request_shutdown()`：置标志 + 投递哨兵；`run()` 收到哨兵后 `task_done()` 并 break。
- `run()` 的 finally 目前只重置状态（`app.py:2070-2079`），要补“退出清理”分支。
- `session.close` 只关会话：abort + 释放会话资源，不退出进程（v1 只能有一个活动会话）。
- 异常退出时 transcript 可能留下未 `turn_end` 的轮次；`session.load` 要能识别并标记 `interrupted`。

### 4.8 边界策略：两个维度，不要混成一个开关

GA 今天的主会话行为 = **`full_access` + `unrestricted`**：

- 不弹审批（`policy.py:30-31`）；
- 不启用 PathACL（`app.py:598-611` 有意不传 `workspace_root`）；
- `tool_cwd` 只做相对路径锚点，绝对路径可以指向任何 OS 允许的位置。

这是 GA 的产品选择，不是 bug。外部调用要补的是“显式声明 + 按传输给默认”，
**不是强制把本地用户的 full_access 降级**。

| 维度 | 取值 | 本地 stdio 默认 | ws/http 默认 |
| --- | --- | --- | --- |
| `permissionMode` | `read_only` / `ask` / `full_access` | **`full_access`（保持现状）** | `ask` |
| `workspacePolicy` | `unrestricted` / `strict` | **`unrestricted`（保持现状）** | `strict` |

- `unrestricted`：`workspace_root=None`，绝对路径任意；相对路径锚定 `workspaceRoot`。**等价今天**。
- `strict`：`workspace_root=<workspaceRoot>`，启用 PathACL；越界拒绝。给不可信调用方/网络。

`ga serve` 规则：

1. **本地 stdio 不传参数 → `full_access + unrestricted`**，与今天逐字节一致；
   启动时在 stderr 打一行“full access, unrestricted workspace”警告，
   `session/ready` 明确回显两个字段，让调用方看得见。
2. 传 `--permission-mode` / `--workspace-policy` 按传的来；两者**独立**，
   `ask` 不等于 `strict`，`full_access` 也不等于 `unrestricted`。
3. `--transport ws|http` 时，未显式指定则默认 `ask + strict`；
   要 full access 必须同时显式 `--permission-mode full_access --workspace-policy unrestricted`。
4. `strict` 下：`workspaceRoot` 必填、路径规范化 + symlink 解析、越界拒绝；
   MCP 只允许 allowlist，或直接禁用。
5. **事件脱敏与大小上限**（两种策略都适用）：
   - `tool/call.input`、`permission/request` 走 `redact_sensitive_text`（`core/redaction.py`）；
   - 默认只给 `argsPreview`（建议 1-4 KB）+ `inputHash`；完整 input 需宿主能力位 `fullToolInput`；
   - `tool/result` 超过阈值时给 `artifactRef`/`truncated`，不内联。
6. **stdout 卫生**（两种策略都适用）：
   - 启动最早期 dup stdout fd 到 stderr，协议输出用独立二进制 fd；
   - Windows 需 `msvcrt.setmode(..., os.O_BINARY)`，并标记 fd 不可继承（`acp_bridge.py:16-42` 已示范）；
   - 所有 `print`（`handler.py:93/107/151`、`app.py` 多处）走 sink 或重定向。
7. **网络传输**：stdio 继承进程信任；ws/http 必须 token + 来源校验 + strict 默认，
   不要把 `full_access` 作为网络会话默认。

### 4.9 进程模型与兼容矩阵

**v1 明确：一个进程 = 一个 workspace = 一个活动会话。**

原因【可见】：

- `GA_PROJECT_ROOT` / `GA_DATA_DIR` 决定 `project_root/tool_cwd/data_root`，在
  `identity_from_environment` 与 `ensure_agent_session` 里是进程级（`transcripts.py:1482-1545`）；
- `TOOLS_SCHEMA` 是模块级全局，MCP 发现原地追加（`app.py:470-512`）；
- MCP warmup、hooks runtime、workflow store 都有进程级单例倾向。

因此：

| 需求 | 方案 |
| --- | --- |
| 一个客户端一个会话 | `ga serve` 单进程，直接支持 |
| 多会话/多项目 | 进程池/ supervisor：每会话一个 `ga serve`，外部路由 |
| 同一进程多会话 | 只有把全局状态全部实例化后才可行，放进 P2，不要 P0 硬做 |

兼容矩阵（改动必须满足）：

| 宿主 | 影响 | 要求 |
| --- | --- | --- |
| Ink UI | 现有 JSONL 协议不能变 | 协议层只新增，不改 `bridge.py` 事件形状 |
| `ga cli` | 复用 `put_task/run` | 新参数默认关闭，行为不变 |
| 子代理 | `agent_runner_loop` 新增 `event_sink=None` | 不传时零变化 |
| 测试 | `tests/test_ink_bridge.py` | 全绿；新增协议测试独立目录 |

---

## 5. 分阶段实施计划

### P0：最小可用宿主协议（修订后建议 3-5 周）

交付：

1. `genericagent/core/protocol/`（events/session_host/ndjson/methods/server）。
2. `ga serve --protocol ndjson`。
3. `--session-id`、`--workspace-root`、`--data-dir`、`--permission-mode`、`--workspace-policy` 五个启动参数。
4. `session.new/prompt/cancel/close` + `permission.respond` + `shutdown`。
5. `session/ready`、`tools/changed`、`assistant/delta`、`assistant/message`、
   `tool/call`、`tool/result`、`permission/request`、`turn/result`、`error`。
6. `PermissionRuntime` 接到协议；ask 档可被外部回答。
7. `turn/result` 汇总：stopReason、isError、durationMs、numTurns、usage、text。
8. **两个独立的安全维度**：`permissionMode`（审批）与 `workspacePolicy`（文件范围）；
   本地 stdio 默认 `full_access + unrestricted`（**保持今天行为**），网络默认 `ask + strict`。
9. **显式声明**：`session/ready` 回显两个字段；启动 stderr 打一行模式警告。
10. **strict 处理器**：仅在 `workspacePolicy=strict` 时用 `build_hosted_handler(..., workspace_root)`；
    `unrestricted` 仍走现有 `build_main_handler`（H1）。
11. **shutdown 链**：哨兵任务、线程 join、MCP/workflow 收尾（H2）。
12. **单写者 EventWriter**：全局 seq、有界队列、丢弃策略（H9/H13）。
13. **stdout 卫生**：fd dup + 二进制模式 + 不可继承（H18）。

验收（必须逐条跑通）：

- [ ] 宿主只发 `initialize` + `session.new` 就能拿到 `session/ready`（tools/model/permissionMode 非空）
- [ ] `session.prompt` **立即**返回 `{turnId, accepted:true}`，不阻塞后续请求
- [ ] `session.prompt` 能收到文本增量、工具调用、工具结果、最终 `turn/result`
- [ ] ask 档下 `tool/call` 之后收到 `permission/request`，accept 后工具执行，deny 后模型收到拒绝
- [ ] 无 responder 时 ask = deny（fail-closed 不变）
- [ ] `session.cancel` 在 turn 运行中可达，收到 `turn/result{stopReason:"cancelled"}`，进程不挂
- [ ] 本地 stdio 默认 `full_access + unrestricted`：写/执行不弹审批，绝对路径可越出 workspace（与今天一致）
- [ ] `session/ready` 同时回显 `permissionMode` 与 `workspacePolicy`
- [ ] `--workspace-policy strict`：workspace 外路径被 PathACL 拒绝
- [ ] `--permission-mode ask`：写/执行前收到 `permission/request`，accept/deny 生效
- [ ] 网络传输未显式指定时默认 `ask + strict`（ws/http 落地时生效）
- [ ] 指定 `--session-id` 后落盘文件名与 ready 帧一致
- [ ] `--workspace-root` 生效：相对路径按新目录解析，项目身份/数据目录随之切换
- [ ] MCP 后台发现完成后收到 `tools/changed`
- [ ] 协议模式 stdout 只有 JSONL；stderr 可读；无 stray print
- [ ] stdin EOF 后：pending 权限 deny、turn 收尾、MCP 关闭、transcript flush、进程 10s 内退出
- [ ] 第二个 `session.prompt` 在运行中按设计返回 `busy`（P0 不排队）

### P1：可恢复、可观察、可接编辑器（修订后建议 3-5 周）

- `session.load/list` + `load_session_by_id` + `events_since`
- `session.rewind`：先把 Ink bridge 的快照/恢复抽到 `core/rewind.py`，再暴露协议
- `session.set_mode/set_model`
- ACP 适配器升级：loadSession、权限、tool_call 更新、cwd、close
- `MainSessionAdapter` 接入 `UnifiedAgentControl`
- workflow/subagent 事件通过协议暴露（`agent/*`、`workflow/*`）
- 超时与取消分层：turn cancel、tool cancel、permission timeout
- `updatedInput` 与权限建议（suggestions）

验收：

- [ ] 断线后按 sessionId 恢复，历史与工具结果完整
- [ ] ACP 客户端（Zed 或自建）能完成 new/prompt/permission/cancel/load
- [ ] 外部能看到子代理与 workflow 的 records/events/result
- [ ] 超时的工具返回结构化 `tool/result{status:"timeout"}`

### P2：服务化与生态（按需）

- WebSocket/HTTP 传输 + token 鉴权 + 多会话 registry（进程池路由）
- **进程池**：每会话一个 `ga serve` 进程，明确并发上限与回收；不要在同进程起多个 agent
- `ga mcp serve`（用 `fastmcp.FastMCP` 把 GA 工具暴露为 MCP server）
- 协议级 hooks 回调（类似 CC 的 `hook_callback`，宿主进程内实现策略）
- fork/血缘与跨进程 transcript 锁

---

## 6. 关键设计决策与反模式

### 6.1 为什么不直接把 Ink 协议当公共协议

| 原因 | 证据 |
| --- | --- |
| 命令没有 request id，无法做请求-响应关联 | `bridge.py:3280-3435` |
| 事件是 UI 状态模型（`history_replace`、panel、中文文案） | `protocol.ts:399-468`、`bridge.py:2782` |
| 没有能力协商与版本兼容策略（只有 `ready.version=1`） | `bridge.py:3268` |
| 运行中提交直接 `busy`，没有排队语义 | `bridge.py:483-485` |
| 没有终态 result，宿主只能猜 `assistant_done + idle` | `bridge.py:3244-3248` |

**做法**：不复制它的消息，而是复用它背后的 `emit`/队列/权限 runtime；
把 UI 事件和协议事件都从同一处发，保持双轨。

### 6.2 文本与结构事件双轨

GA 现有工具输出是 Markdown 文本（代码围栏），Ink 依赖它。
协议层需要结构化 `tool/call|result`，但**不要删掉文本渲染**，否则破坏 UI 与历史回放。
正确做法：在 `loop.py` 分发点同时产出“文本 chunk”和“结构事件”。

### 6.3 session id 就是 transcript id

不要让 ACP 的 `ga_<hex>` 另起一套。外部宿主用 `session_<uuid32>` 作为主键：
- `session.new` 可传入；
- `session.load` 直接定位文件；
- `UnifiedAgentControl.execution_id` 用它；
- 日志、审计、恢复都用同一个 id。

### 6.4 保留 headless fail-closed

现有 `PermissionRuntime` 在无 emit 时 deny 是正确安全默认（`runtime.py:72-73`）。
协议服务器必须显式注入 emit；没有客户端的进程不能“默认放行”。

### 6.5 单写者 / 会话租约

多进程同写一个 transcript 会产生 seq 冲突与交错（`transcripts.py:20-25`）。
P0 先声明“一个 session 同时只有一个 serve 进程”，用文件锁或 owner 文件拒绝第二写者；
P2 再考虑真正的多写者。

### 6.6 反模式清单

| 反模式 | 后果 |
| --- | --- |
| 把 Ink 事件改个名就宣布是 SDK | 没有 request id/终态，宿主永远对不齐 |
| 在 core 里直接读 stdin | 与 Ink/CLI/子代理三种宿主冲突 |
| 用 `print` 输出协议帧 | 核心其他 print 会污染流；必须 fd dup 或 sink |
| 多进程共写 transcript | seq 错乱、恢复出重复/丢失轮次 |
| 为 ACP 把工具搬到客户端 | 与 GA 工具在环模型冲突，权限/工作区全失效 |
| 一次性重构 Ink bridge | UI 回归面过大；应先新增协议层，再逐步适配 |

---

## 7. 测试清单

### 7.1 协议级

- [ ] 一行一个 JSON；粘包/拆包正确；超长行有明确错误
- [ ] 坏 JSON 只丢该帧，后续帧继续处理
- [ ] 未知方法返回 `unknown_method`，不崩
- [ ] 响应 `id` 与请求一致；事件 `seq` 严格单调
- [ ] `session.prompt` 立即 ack，后续 `permission.respond` 仍可送达
- [ ] 慢客户端/满队列：不阻塞 agent；关键事件不丢
- [ ] stdout 只有协议帧（无 stray print）；stderr 可读
- [ ] stdin EOF → 优雅退出（agent 线程 join、transcript flush、MCP 关闭）

### 7.2 会话

- [ ] `session.new` 不带 id → 自动生成并返回
- [ ] 带 id → transcript 文件名一致
- [ ] 本地默认 `full_access + unrestricted`：写/执行不弹审批，绝对路径可越出 workspace
- [ ] `session/ready` 同时回显 `permissionMode` 与 `workspacePolicy`
- [ ] `--workspace-policy strict` 下：workspace 外相对/绝对路径被 PathACL 拒绝
- [ ] `--permission-mode ask` 下：写/执行前收到 `permission/request`
- [ ] `--workspace-root` 生效：项目身份、数据目录、相对路径一起切换
- [ ] `session/ready` 的 tools/model/permissionMode 与实际一致
- [ ] MCP 后台发现后收到 `tools/changed`
- [ ] 同一 session 第二个 prompt：P0 返回 `busy`，且不影响正在跑的 turn

### 7.3 turn 与工具

- [ ] 文本增量顺序正确，最终 `assistant/message` 与增量拼接一致
- [ ] `tool/call` → `tool/result` 顺序与 tool_use_id 对应
- [ ] 工具异常 → `tool/result{status:"error"}`，模型能继续
- [ ] `turn/result` 的 stopReason 与 `_classify_turn_outcome` 一致
- [ ] usage/duration/numTurns 非空

### 7.4 权限

- [ ] `ask` 档：收到 `permission/request`，accept 执行
- [ ] deny：工具不执行，拒绝原因进模型上下文
- [ ] 无 responder：deny（fail-closed）
- [ ] 权限 pending 时 `session.cancel`：按 deny 收尾，进程不挂
- [ ] 超时：按 deny 收尾并记 settled 原因（P1）
- [ ] P1：`updatedInput` 能改写工具参数

### 7.5 恢复

- [ ] `session.load` 后历史、工具结果、working_state 正确
- [ ] `events_since(seq)` 只返回新事件
- [ ] `rewind` 后文件与历史一致（dry-run 先测）
- [ ] 进程崩溃后按 id 恢复，未完成 turn 有明确标记

### 7.6 回归

- [ ] `python -m unittest discover -s tests` 全绿
- [ ] `tests/test_ink_bridge.py` 全绿（Ink 协议未退化）
- [ ] `frontends/ink/ui` 的 `npm test` / `npm run typecheck` 全绿
- [ ] ACP：initialize/new/prompt/cancel/load 手工跑通

---

## 8. 风险与边界

1. **核心 stdout 污染**：`handler.py:93/107/151` 等直接 print。协议模式必须像 ACP 桥那样
   dup fd（`acp_bridge.py:16-42`）或注入 sink；否则第一帧就可能被污染。
2. **主循环是同步单线程**：`run()` 串行消费（`app.py:1751`）。加协议层不能假设并发；
   多会话必须一进程一 agent 或一会话一线程，且要限制资源。
3. **工具事件改造会碰 UI 文本**：`loop.py:73-138` 的 Markdown 渲染与 lnk 展示强绑定；
   必须“只加事件、不改文本”，否则 UI 回归。
4. **transcript 写放大**：如果每个 delta 都落盘，JSONL 会暴涨；建议按 tool/message 粒度，
   而不是 token 粒度。
5. **ACP 规范细节**：`session/request_permission` 的 option 结构与 stopReason 枚举要按版本核对，
   不要凭记忆实现。
6. **安全**：stdio 继承进程信任；一旦开 WebSocket/HTTP，必须加 token、来源校验、
   工作区白名单，否则等于把本机工具执行暴露给网络。
7. **兼容性**：`ga cli` / Ink / 子代理三种宿主都在用 `put_task` 与 emit；
   所有改动必须保留旧签名或提供默认参数。

---

## 9. 证据索引

| # | 坐标 | 结论 |
| --- | --- | --- |
| 1 | `genericagent/core/app.py:681/2081` | `GenericAgent` / `GeneraticAgent` 可 import 运行时 |
| 2 | `app.py:1067` | `put_task` 返回裸 `queue.Queue` |
| 3 | `app.py:1043` | `abort()` 整轮取消 |
| 4 | `app.py:993` | `set_permission_mode` |
| 5 | `app.py:1751` | 单线程串行消费 task_queue |
| 6 | `app.py:1631-1659` | turn outcome 分类 |
| 7 | `app.py:1661-1688` | 流式消费；break 未 close 生成器 |
| 8 | `app.py:2002-2008` | 整轮结束才写 transcript |
| 9 | `app.py:741-755` | `tool_cwd` 来源 |
| 10 | `app.py:2964-2987` | 子进程 CLI 参数；无 session/cwd/resume |
| 11 | `frontends/ink/bridge.py:3262` | JSONL 主循环 |
| 12 | `bridge.py:3268` | 仅 `ready.version=1` |
| 13 | `bridge.py:3280-3435` | 命令分发，无 request id |
| 14 | `bridge.py:483-485` | 运行中提交 busy |
| 15 | `bridge.py:428` | 权限 emit 注入 |
| 16 | `bridge.py:863-886` | permission_response |
| 17 | `bridge.py:3244-3248` | assistant_delta/done 事件 |
| 18 | `frontends/ink/ui/src/protocol.ts:33-69/399-468` | 命令/事件全量 |
| 19 | `frontends/acp/genericagent_acp_bridge.py:168-190` | ACP initialize 能力声明 |
| 20 | `acp_bridge.py:192-212` | session/new；cwd 未应用 |
| 21 | `acp_bridge.py:214-311` | 仅文本 chunk |
| 22 | `acp_bridge.py:335-338` | session/load 不支持 |
| 23 | `genericagent/core/permissions/runtime.py:55-100` | PermissionRuntime 挂起/emit |
| 24 | `runtime.py:82-91` | permission_request 载荷 |
| 25 | `genericagent/core/handler.py:682-715` | 主会话 ask 挂载点 |
| 26 | `handler.py:672-681` | 子代理 ask 不请求宿主 |
| 27 | `handler.py:789-790` | 主会话无 PathACL |
| 28 | `genericagent/core/loop.py:20-31` | dispatch 生成器 |
| 29 | `loop.py:73-138` | 工具调用被渲染成 Markdown |
| 30 | `loop.py:180-194` | 并行工具收集无取消检查 |
| 31 | `genericagent/core/transcripts.py:121/154` | session id 生成/可指定 |
| 32 | `transcripts.py:143-151` | v2 事件类型 |
| 33 | `transcripts.py:1201/1357` | 按路径加载/恢复 |
| 34 | `transcripts.py:20-25/133-140` | seq 进程内计数、无跨进程锁 |
| 35 | `genericagent/core/control/control.py:39/63` | 统一控制面 |
| 36 | `frontends/ink/bridge.py:355/378` | 控制面仅 Ink 使用 |
| 37 | `genericagent/hooks/models.py:14-62` | 33 个 hook 事件 |
| 38 | `genericagent/mcp/runtime.py:661-674` | MCP client |
| 39 | `genericagent/agents/manager.py:817/1057` | 子代理 spawn |
| 40 | `genericagent/workflow/controller.py:11-74` | workflow 生命周期 API |
| 41 | `genericagent/core/app.py:1751-1753` | `run()` 无 shutdown 哨兵，阻塞在 `task_queue.get()` |
| 42 | `genericagent/core/app.py:598-611` | 主场景 handler 有意不传 `workspace_root`，无 PathACL |
| 43 | `genericagent/core/permissions/policy.py:30-31` | 主会话默认 `full_access` |
| 44 | `frontends/ink/bridge.py:249/547/2808` | rewind 快照只存在于 Ink bridge |
| 45 | `genericagent/core/app.py:488-512` | MCP 工具后台发现、原地追加，无变更事件 |

---

## 10. 一句话结论

> **GA 已经具备“被调用”的骨架：可 import 的运行时、JSONL 桥、权限 Future、v2 transcript、
> 多代理控制面；缺的是一层把它变成稳定契约的协议层，以及先于协议的边界与关闭链路。**
> 先做九件事：`session/ready`、prompt ack、`tool/call|result`、`permission/request`、
> `turn/result`、`session.cancel`、边界策略（审批与文件范围分开）、shutdown 链、stdout/单写者事件管线；
> 再接 `session.load` 与 ACP，GA 才能安全地像 CC 一样被其他 agent / 编辑器直接驱动。

---

## 11. 直观示例：另一个 agent 怎么调用 GA

### 11.1 一句话心智模型

> 把 GA 当成一个**会干活的子进程**：调用方 spawn 它，然后双方用“一行一个 JSON”对话。
> 不是 HTTP 服务、不是 import 一个库、也不是让 GA 去调别人。

```text
调用方 agent（Claude Code / Codex / 自研编排器）
      │  spawn: ga serve --workspace-root D:/proj/demo
      │  stdin  ── JSON 请求 ──▶  GA 进程
      │  stdout ◀── JSON 事件 ──  GA 进程
      ▼
  拿到 turn/result，当作一次工具调用的结果
```

### 11.2 今天就能跑的样子（Ink JSONL 桥）

不需要等新协议，现有代码就能被外部驱动，只是消息形状偏 UI：

```bash
python -m frontends.ink.bridge
```

调用方写入（stdin）：

```json
{"type":"submit","text":"把 src/app.py 里的 TODO 实现掉，并跑测试"}
```

GA 输出（stdout，节选）：

```json
{"type":"ready","version":1}
{"type":"user","taskId":1,"text":"把 src/app.py 里的 TODO 实现掉，并跑测试"}
{"type":"status","status":"running","taskId":1}
{"type":"assistant_delta","taskId":1,"text":"我先看一下文件"}
{"type":"permission_request","requestId":"a1b2","toolName":"file_patch","argsPreview":"{\"path\":\"src/app.py\",...}","reason":"写入文件","mode":"ask"}
```

调用方应答：

```json
{"type":"permission_response","requestId":"a1b2","decision":"accept"}
```

结束：

```json
{"type":"assistant_done","taskId":1,"text":"已实现并测试通过"}
{"type":"status","status":"idle","taskId":1}
```

**局限**：没有 request id、没有 `turn/result`、工具调用只能从 `agent_event` 旁路看，
运行中再 `submit` 会被 `busy` 拒绝。适合“能用”，不适合“做成产品”。

### 11.3 方案落地后的样子（NDJSON v1）

启动：

```bash
ga serve --protocol ndjson \
  --workspace-root D:/proj/demo \
  --permission-mode ask
```

如果你要的就是今天的行为（full access、无审批、绝对路径任意）：

```bash
ga serve --protocol ndjson \
  --workspace-root D:/proj/demo \
  --permission-mode full_access \
  --workspace-policy unrestricted
```

两者只差“有没有审批/PathACL”，模型、工具、会话完全一样；
`session/ready` 会明确回显 `"permissionMode":"full_access","workspacePolicy":"unrestricted"`。
上面的示例用 `ask` 是为了演示 `permission/request`；`full_access` 下不会出现该事件，工具直接执行。

#### 第 1 步：握手

调用方 → GA：

```json
{"id":"1","method":"initialize","params":{
  "protocolVersion":1,
  "clientInfo":{"name":"claude-code","version":"2.1.303"},
  "capabilities":{"permissions":true,"toolEvents":true}}}
```

GA → 调用方：

```json
{"id":"1","result":{
  "protocolVersion":1,
  "serverInfo":{"name":"genericagent","version":"0.2.0"},
  "capabilities":{"permissions":true,"toolEvents":true,"resume":false}}}
```

#### 第 2 步：开会话

```json
{"id":"2","method":"session.new","params":{
  "sessionId":"session_7f3a9c2e4b1d4a6f8c0e2d4b6a8f0c1e",
  "workspaceRoot":"D:/proj/demo",
  "permissionMode":"ask"}}
```

```json
{"id":"2","result":{"sessionId":"session_7f3a9c2e4b1d4a6f8c0e2d4b6a8f0c1e",
  "model":"deepseek-v4.1-flash","permissionMode":"ask"}}
{"type":"event","event":{"type":"session/ready","seq":1,
  "sessionId":"session_7f3a9c2e4b1d4a6f8c0e2d4b6a8f0c1e",
  "workspaceRoot":"D:/proj/demo",
  "tools":["file_read","file_write","file_patch","code_run","web_scan"],
  "mcpServers":[],"commands":["/model","/compact"],"toolsPending":true}}
```

#### 第 3 步：发任务（立即 ack，不阻塞）

```json
{"id":"3","method":"session.prompt","params":{
  "sessionId":"session_7f3a9c2e4b1d4a6f8c0e2d4b6a8f0c1e",
  "content":[{"type":"text","text":"把 src/app.py 里的 TODO 实现掉，并跑测试"}]}}
```

```json
{"id":"3","result":{"turnId":"turn_1","accepted":true}}
```

#### 第 4 步：事件流

```json
{"type":"event","event":{"type":"turn/started","seq":2,"turnId":"turn_1"}}

{"type":"event","event":{"type":"assistant/delta","seq":3,"turnId":"turn_1",
  "channel":"display","text":"我先看一下文件。"}}

{"type":"event","event":{"type":"tool/call","seq":4,"turnId":"turn_1",
  "toolUseId":"call_abc","name":"file_read","input":{"path":"src/app.py"}}}

{"type":"event","event":{"type":"tool/result","seq":5,"turnId":"turn_1",
  "toolUseId":"call_abc","status":"success","content":"...# TODO...","truncated":false}}

{"type":"event","event":{"type":"tool/call","seq":6,"turnId":"turn_1",
  "toolUseId":"call_def","name":"file_patch",
  "input":{"path":"src/app.py","old_content":"# TODO","new_content":"..."}}}

{"type":"event","event":{"type":"permission/request","seq":7,"turnId":"turn_1",
  "requestId":"perm_9","toolUseId":"call_def","toolName":"file_patch",
  "argsPreview":"{\"path\":\"src/app.py\",\"old_content\":\"# TODO\",...}",
  "reason":"写入文件","options":["accept","deny"]}}
```

调用方**在同一条连接上**应答（这就是 prompt 必须立即 ack 的原因）：

```json
{"id":"4","method":"permission.respond","params":{
  "requestId":"perm_9","decision":"accept"}}
```

```json
{"id":"4","result":{}}
{"type":"event","event":{"type":"permission/resolved","seq":8,
  "requestId":"perm_9","decision":"accept"}}
{"type":"event","event":{"type":"tool/result","seq":9,"turnId":"turn_1",
  "toolUseId":"call_def","status":"success","content":"patched"}}
```

最终：

```json
{"type":"event","event":{"type":"turn/result","seq":12,"turnId":"turn_1",
  "stopReason":"completed","isError":false,
  "text":"已实现 TODO，并运行 tests/test_app.py，4 passed。",
  "durationMs":8342,"numTurns":4,
  "usage":{"inputTokens":8123,"outputTokens":1204}}}
```

调用方拿到这一帧，就等于拿到一次“工具调用”的结果，可以继续自己的循环。

#### 第 5 步：中断

```json
{"id":"5","method":"session.cancel","params":{
  "sessionId":"session_7f3a...","turnId":"turn_1"}}
```

```json
{"id":"5","result":{"cancelled":true,"turns":["turn_1"]}}
{"type":"event","event":{"type":"turn/result","seq":13,"turnId":"turn_1",
  "stopReason":"cancelled","isError":false,"text":"..."}}
```

#### 第 6 步：收尾

```json
{"id":"6","method":"shutdown"}
```

GA 按 §4.7 收尾：pending 权限 deny、abort 当前 turn、停 MCP、flush transcript，然后退出。

### 11.4 调用方伪代码（Python）

```python
import json, subprocess

proc = subprocess.Popen(
    ["ga", "serve", "--protocol", "ndjson",
     "--workspace-root", "D:/proj/demo", "--permission-mode", "ask"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
)

def send(obj):
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()

send({"id": "1", "method": "initialize",
      "params": {"protocolVersion": 1, "capabilities": {"permissions": True}}})
send({"id": "2", "method": "session.new",
      "params": {"workspaceRoot": "D:/proj/demo", "permissionMode": "ask"}})
send({"id": "3", "method": "session.prompt",
      "params": {"sessionId": SID,
                 "content": [{"type": "text", "text": "实现 TODO 并跑测试"}]}})

for line in proc.stdout:
    msg = json.loads(line)
    if msg.get("type") != "event":
        continue
    ev = msg["event"]
    if ev["type"] == "permission/request":
        # 这里就是“另一个 agent 决定要不要放行”
        decision = "accept" if policy_allows(ev) else "deny"
        send({"id": "p1", "method": "permission.respond",
              "params": {"requestId": ev["requestId"], "decision": decision}})
    elif ev["type"] == "assistant/delta":
        print(ev["text"], end="", flush=True)
    elif ev["type"] == "turn/result":
        print("\nDONE:", ev["stopReason"], ev["text"])
        break

send({"id": "9", "method": "shutdown"})
proc.wait(timeout=10)
```

### 11.5 三种典型调用姿势

| 姿势 | 怎么用 | 适合 |
| --- | --- | --- |
| 一次性工具 | spawn → prompt → 等 `turn/result` → shutdown | 编排器把 GA 当一个“会写代码的工具” |
| 长会话 | spawn 一次，多轮 `session.prompt`，中间可 `cancel` | 编辑器 / 聊天宿主 |
| 多任务并行 | 每个任务 spawn 一个 `ga serve`，各自独立 workspace | 多项目、多分支 |

### 11.6 三个常见误解

1. **“是不是要起 HTTP 服务？”** 不是。P0 就是子进程 + stdio，跟 MCP stdio server 同一种形态。
2. **“是不是要 import GenericAgent？”** 不必。import 是同进程嵌入的写法；
   跨 agent 调用走进程边界更安全，也能隔离崩溃与依赖。
3. **“另一个 agent 怎么知道 GA 在等权限？”** 靠 `permission/request` 事件；
   它必须能在 prompt 未结束时继续写 stdin——这就是为什么 `session.prompt` 只能 ack，不能同步阻塞。

---

## 12. 实机验证记录（2026-11-06）

环境：Windows，GA 源码检出，`llm.yaml` 新增并激活 `zhanlu-ds`
（`moma/deepseek-latest-flash`，`openai_chat`，context 272000，`thinking: low`，`supports: [image_input]`）。

### 12.1 配置与连通性

- `active_profile: zhanlu-ds`，运行时输出：
  `19 profile(s), active=#17 zhanlu-ds/moma/deepseek-latest-flash`
- 直连 `/chat/completions`：HTTP 200，`finish_reason=stop`，
  `completion_tokens_details.reasoning_tokens=32`（`reasoning_effort: low` 已生效）。
- 图片能力通过 `supports: [image_input]` 打开（`config.py:433`）。

### 12.2 GA 源码端到端

`python -m frontends.ink.bridge` → `submit` → 收到 `assistant_delta` / `assistant_done`
→ `status idle` → `shutdown`，退出码 0。简单问答可跑通。

### 12.3 漏洞实测

| 漏洞 | 实测方法 | 结果 | 结论 |
| --- | --- | --- | --- |
| H1a 默认 full_access | 桥上发 `permission_status` | `mode=full_access, default=full_access` | 确认；**产品本意，保留** |
| H1b 主会话无工作区边界 | 让 agent 用 `code_run` 写项目外临时文件 | 0 次 `permission_request`；文件创建成功，内容 `GA_OUTSIDE_OK` | 确认；**产品本意，保留** |
| H2 无 shutdown 哨兵 | `GenericAgent()` 起 `run` 线程，`abort()` 后 join 2s | `alive_after_abort=True`；无 `request_shutdown`/`shutdown`；队列为空 | 确认；**已修复**（§13） |
| ACP `cwd` 被忽略 | ACP `session/new` 传临时目录，prompt 让 `code_run` 打印 `os.getcwd()` | 实际输出 `D:\git_codes\GenericAgent`，不是请求目录 | 确认；**已修复**（§13） |
| ACP 只有文本、无结构化工具事件 | 观察 ACP `session/update` | 只有 `agent_message_chunk`；工具参数、执行 banner、`[Status]` 都混在文本里 | 确认；**已修复**：新增 `tool_call`/`tool_call_update`，文本保留 |
| headless ask fail-closed | `PermissionRuntime()` 无 emit 调 `wait_for_decision` | 返回 `deny` | 确认；行为正确，未改 |

> H1 的实测只证明“外部调用方会继承 full_access + 无边界”，
> **不表示本地交互使用有问题**；本地 full_access 是既定产品行为（见 §4.8）。

### 12.4 未实测、仅代码可见的项

H4（ready 工具表不完整）、H6（rewind 快照只在 Ink bridge）、H9/H13（多线程事件顺序与背压）、
H10（transcript 跨进程写）、H16（ACP tool kind 映射）、H17（serve 模式 hooks 去向）、
H18（MCP 子进程 fd 继承）尚未做运行时验证；实施 P0 时应按 §7 补测。

### 12.5 一个配置陷阱（实测踩到）

从 `temp/` 下直接 `python temp/xxx.py` 时，`sys.path[0]` 是 `temp/`，
可能导入**已安装的 genericagent** 而不是源码检出，于是读到
`%APPDATA%/GenericAgent/llm.yaml`，表现为“源码目录改了配置但没生效”。

验证方式：

```bash
python -c "from genericagent import paths; print(paths.LLM_CONFIG_PATH)"
```

源码场景应输出 `<repo>/llm.yaml`。必要时显式设置：

```bash
export GA_LLM_CONFIG=D:/git_codes/GenericAgent/llm.yaml
# 或：PYTHONPATH=D:/git_codes/GenericAgent python ...
```

---

## 13. 修复记录（2026-11-06）

### 13.1 用户决策

- **H1（默认 `full_access` + 主会话无工作区边界）是产品本意，保留不修。**
  本文只记录事实与风险，不再建议收紧本地默认。
- 其余可落地的缺陷本次修复；协议层（`ga serve`）仍属于后续 P0 工程，不在本次范围。

### 13.2 已修复

| # | 修复 | 落点 |
| --- | --- | --- |
| 1 | **关闭哨兵**：`request_shutdown()` 入队 `__shutdown__`，`run()` 收到后 break | `genericagent/core/app.py`（`run` / `request_shutdown`） |
| 2 | **workspace 注入**：`GenericAgent(workspace_root=...)` 重算 project/data 身份与 `tool_cwd`；非法目录抛 `workspace_invalid` | `genericagent/core/app.py`（`__init__`） |
| 3 | **ACP cwd 生效**：`session/new` 用 `GeneraticAgent(workspace_root=cwd)`；非法 cwd 返回错误 | `frontends/acp/genericagent_acp_bridge.py`（`handle_session_new`） |
| 4 | **ACP 权限桥**：`PermissionRuntime.set_emit` → `session/request_permission`；客户端 `allow_once`/`reject_once` → `resolve`；`session/cancel` 清理 pending | 同上（`_emit_permission` / `_handle_permission_response`） |
| 5 | **ACP 模式切换**：`session/set_mode` 映射 `read_only/ask/full_access` | 同上（`handle_message`） |
| 6 | **ACP 结构化工具事件**：handler 在 dispatch 前后发 `start/end`；ACP 映射为 `tool_call` / `tool_call_update`；`no_tool` 不上报；空 `tool_use_id` 合成 `tool_<turn>_<index>` | `genericagent/core/handler.py`（`_emit_tool_event` / `dispatch`）、ACP（`_on_tool_event`） |
| 7 | **ACP 关闭**：`session/close` 调 `request_shutdown` 并移除会话；EOF 关闭所有会话 | 同上（`handle_message` / `serve`） |
| 8 | **Ink 桥关闭**：`shutdown` 命令与 stdin EOF 都调 `request_shutdown` | `frontends/ink/bridge.py`（`run_jsonl_loop`） |
| 9 | **ACP 默认模型**：`--llm-no` 默认改为 `None`，不传时使用 `active_profile`（即 `zhanlu-ds`） | 同上（`__init__` / `main`） |

H7（空 `tool_use_id`）随第 6 项一并修复；H16（ACP kind 映射）已实现
`file_read→read`、`file_write/file_patch→edit`、`code_run→execute`、
`web_*→fetch`，其余 `other`。

### 13.3 测试

新增：

- `tests/test_shutdown_sentinel.py`：哨兵入队、`run()` 退出（2 条）
- `tests/test_acp_bridge.py`：workspace、权限往返、工具事件、set_mode、close（5 条）

回归：

- `tests.test_ink_bridge`：**149/149 OK**（同步修正了 FakeAgent 的 `model_prefix` 夹具与断言）
- `python -m unittest discover -s tests`：**2176 条，1 条失败**
  - 失败项 `test_skill_forbids_hardcoding_repo_root_in_prompts` 是既有的 skill 文案断言
    （期望 `Never hardcode the repository`，实际文案已改为 `Do not hardcode ...`），
    与本次改动无关。
- 真实 ACP e2e（默认 `zhanlu-ds`，未传 `--llm-no`）：
  - `session/new` 请求临时 cwd → `code_run` 打印的 `os.getcwd()` 就是该目录；
  - `set_mode ask` → 收到 1 次 `session/request_permission(code_run)` → `allow_once` → 工具执行；
  - 结构化事件：1 次 `tool_call` + 1 次 `tool_call_update`（`completed`）；
  - 文件 `out.txt` 写入 `OK`，`session/close` 后进程退出码 0。

### 13.4 仍未开放（属于后续工程）

H3/H4/H5（完整协议握手与 tool/result 帧）、H6（rewind 抽到 core）、
H8/H15（按 workspace 的 session load）、H9/H13（单写者与背压）、
H10（transcript 跨进程锁）、H11（事件脱敏全链路）、H12（子代理 ask 代理）、
H14（模型 selector 映射）、H17（serve 模式 hooks 去向）、H18（MCP 子进程 fd）、
H19（进程池）。这些依赖 `ga serve` 协议层，不在本次“修现有入口”的范围。

---

## 14. ACP 补充测试环节（2026-11-06）

> 目的：在 §13 的修复之后，补一套**可重复执行**的 ACP 验收步骤，回答两个问题：
> ① 安装后的 GA（0.3.0 wheel）能不能被其他 agent 通过 ACP 真正驱动；
> ② “其他 agent”这一侧需要具备什么能力，GA 侧还缺什么。
>
> **本次实际执行了用例 A（CC 会话内拉起 GA）；用例 B（Zed 原生宿主）只落操作步骤，未安装、未执行。**

### 14.1 结论

1. **GA 作为 ACP server 是可用的。** 安装版 0.3.0 从 `site-packages` 启动，
   `initialize → session/new → session/set_mode(ask) → session/prompt`
   能完成一次带工具调用、带权限审批的真实会话，最终 `stopReason=end_turn`。
2. **Claude Code 本身不是 ACP client。** CC 2.1.303 的 `--help` 没有 ACP 选项，
   `claudecode303/modules` 里也搜不到 `agent-client-protocol` / `session/request_permission`。
   CC 只能作为“进程启动器”，通过 Bash 工具拉起一个 ACP 客户端 shim。
3. **Zed 是当前 Windows 上最直接的 GA 宿主。** Zed Windows 稳定版已正式支持，
   且官方明确支持 ACP-powered agents；只需在 `settings.json` 的 `agent_servers` 里登记 GA。
4. **Stably Orca 不是通用 ACP client。** 它的 ACP 结构化聊天目前主要接 Grok，
   不支持通过配置挂任意 ACP agent；另一个同名项目 `orca-agent` 本身是 ACP agent，和 GA 同侧。
5. ACP 能力边界仍是 §13.4 的状态：文本 prompt、无 `session/load`、无 `session/list`、
   image/audio/embeddedContext 均为 `false`；工具事件和权限审批已通。

### 14.2 被测环境

```text
OS            Windows 11
Python        C:\anaconda3\python.exe（3.12.4）
GA            0.3.0（wheel 安装，非源码 checkout）
GA 安装位置   C:\anaconda3\Lib\site-packages
LLM 配置      C:\Users\<user>\AppData\Roaming\GenericAgent\llm.yaml
默认 profile  zhanlu-ds
默认模型      moma/deepseek-latest-flash（context_window=272000）
```

先确认跑的是安装版而不是源码：

```powershell
python -c "import genericagent, frontends.acp.genericagent_acp_bridge as b; print(genericagent.__file__); print(b.__file__)"
```

期望两者都在 `site-packages`。如果输出仓库路径，说明当前目录或 `PYTHONPATH` 让源码抢占了导入。

确认配置解析：

```powershell
python -c "from genericagent.llm.config import find_llm_config; print(find_llm_config())"
```

安装版应输出 `%APPDATA%\GenericAgent\llm.yaml`，不是仓库根目录的 `llm.yaml`。
两个文件是独立的；只改仓库配置不会影响已安装版。

### 14.3 用例 A：最小 ACP 客户端（已执行）

#### A.1 测试文件

```text
scripts/acp_client_demo.py
```

它是一个独立 ACP 客户端，行为是：

1. 用 `subprocess` 启动 `python -m frontends.acp.genericagent_acp_bridge`；
2. 发 `initialize`；
3. 发 `session/new { cwd }`，cwd 使用新建的临时目录；
4. 发 `session/set_mode { modeId: "ask" }`；
5. 发 `session/prompt`，要求 GA 用 `file_write` 在 cwd 创建 `acp_demo.txt`，内容为 `ACP_OK_030`；
6. 处理服务端反向请求 `session/request_permission`，按 `--permission allow|deny` 回
   `allow_once` 或 `reject_once`；
7. 收集 `session/update` 的 `agent_message_chunk` / `tool_call` / `tool_call_update`；
8. 校验文件真实落盘，写 `demo_result.json`；
9. 发 `session/close`，关闭 stdin，等待进程退出。

#### A.2 执行命令

```powershell
# 自动放行写文件
python D:/git_codes/GenericAgent/scripts/acp_client_demo.py

# 拒绝审批，观察 deny 链路
python D:/git_codes/GenericAgent/scripts/acp_client_demo.py --deny

# 打开 bridge 原始 JSON-RPC 日志
python D:/git_codes/GenericAgent/scripts/acp_client_demo.py --debug
```

建议从仓库外运行（例如 `C:\Users\<user>`），这样能确保导入的是安装版。
如果确实要从源码 checkout 测，加 `--allow-source`。

#### A.3 实测结果（allow）

```text
[install] bridge module: C:\anaconda3\Lib\site-packages\frontends\acp\genericagent_acp_bridge.py
[init] protocol=1 agent={"name": "genericagent-acp", "title": "GenericAgent", "version": "0.1.0"}
[session] modes={"currentModeId": "full_access", "availableModes": [read_only, ask, full_access]}
[mode] applied=ask
[tool] file_write kind=edit status=in_progress
[permission] 服务端请求审批: file_write -> 客户端选择 allow_once
[tool] call-c44d... -> completed
[done] stopReason=end_turn
[stream] assistant_chunks=11 tool_calls=1 permissions=1
[verify] file_exists=True marker_present=True
[verify] content='ACP_OK_030'
[result] ok=True
```

#### A.4 实测结果（deny）

```text
[tool] file_write kind=edit status=in_progress
[permission] 服务端请求审批: file_write -> 客户端选择 reject_once
[tool] call-1b5b... -> failed
[tool] code_run kind=execute status=in_progress
[permission] 服务端请求审批: code_run -> 客户端选择 reject_once
[tool] call-e02f... -> failed
[done] stopReason=end_turn
[stream] tool_calls=2 permissions=2
[verify] file_exists=False marker_present=False
[result] ok=True
```

deny 用例里模型被拒后会改试 `code_run`，同样被拒，最终没有写文件。
这说明权限链路不是只“发个通知”，而是真的会让工具执行失败。

#### A.5 判定标准

| 检查项 | allow 期望 | deny 期望 |
| --- | --- | --- |
| `initialize.agentInfo.name` | `genericagent-acp` | 同左 |
| `session/new` 返回 `sessionId` | 非空 | 非空 |
| `session/set_mode` | `modeId=ask` | 同左 |
| `session/request_permission` | ≥1 次 | ≥1 次 |
| `tool_call` / `tool_call_update` | 各 ≥1 次 | 各 ≥1 次 |
| `session/prompt` 终态 | `end_turn` | `end_turn` |
| 文件 `acp_demo.txt` | 存在且含 `ACP_OK_030` | 不存在 |
| bridge 进程退出码 | 0 | 0 |

### 14.4 用例 B：Zed Windows 原生宿主（未执行，待补测）

> 本节只记录操作方式。**本次没有安装 Zed，也没有在 Zed 里实跑 GA**；
> 下面的步骤按 Zed 官方文档和本机 `winget show` 结果整理，执行后应把结果回填到 B.5。

#### B.1 为什么选 Zed

- Zed 的 Windows 稳定版已正式发布（2025-10-15），不是 WSL 或第三方移植；
- 官方明确支持 ACP-powered agents，并支持 Windows 原生、WSL、SSH remoting；
- 自定义 ACP agent 的入口是 `settings.json` 的 `agent_servers`，不需要改 Zed 源码。

本机 `winget show` 实测：

```text
Zed [ZedIndustries.Zed]
版本: 1.27.0
发布者: Zed Industries
```

#### B.2 安装

```powershell
winget install -e --id ZedIndustries.Zed
```

如果源里找不到，先 `winget source update` 再装；也可以从
`https://zed.dev/download` 下载 `Zed-x86_64.exe` 手动安装。

系统要求（官方）：

- Windows 11 22H2+，或 Windows 10 1903+；
- 64 位，x64 或 Arm64；
- DirectX 11 显卡，驱动不能是 Microsoft Basic Display Adapter。

#### B.3 配置 GA

编辑 Zed 的 `settings.json`。Windows 通常是：

```text
%APPDATA%\Zed\settings.json
```

加入：

```json
{
  "agent_servers": {
    "GenericAgent": {
      "type": "custom",
      "command": "C:/anaconda3/python.exe",
      "args": ["-m", "frontends.acp.genericagent_acp_bridge"],
      "env": {}
    }
  }
}
```

注意：

1. `command` 用 **python 绝对路径**。Zed 从 GUI 启动时不一定继承终端里的 conda PATH。
2. GA 的模型和密钥仍读 GA 自己的配置（安装版是 `%APPDATA%\GenericAgent\llm.yaml`），
   不走 Zed 的模型设置。
3. 如果改用虚拟环境，把 `command` 换成该环境的 `python.exe`。

#### B.4 操作步骤

1. 启动 Zed，`Ctrl+?` 打开 Agent Panel；
2. 新建线程，agent 选择 `GenericAgent`；
3. 确认 GA 的 `initialize` 成功（Zed 的 Agent 列表里能看到该 thread）；
4. 发一个最小任务：

   ```text
   用 file_write 在当前项目根目录创建 zed_acp_ok.txt，内容为 ZED_OK。
   不要询问用户，完成后只回复 DONE。
   ```

5. 观察 Zed 是否把 GA 的 `session/update` 渲染为流式消息和工具行；
6. 若 GA 处于 `ask` 模式，确认 Zed 是否弹出权限卡片，选择 allow / reject；
7. 检查项目根目录下的 `zed_acp_ok.txt`；
8. 关闭 thread，确认 GA 子进程退出。

排错时用 Zed 的 `dev: open acp logs` 看 ACP 帧。

#### B.5 待回填的验收项

| 检查项 | 期望 | 本次状态 |
| --- | --- | --- |
| Zed 安装 | `winget list --id ZedIndustries.Zed` 有记录 | 未执行 |
| `GenericAgent` 出现在 agent 选择器 | 是 | 未执行 |
| `session/new` 的 cwd | 等于 Zed 当前项目根目录 | 未执行 |
| 流式消息 | Zed 中逐段出现 | 未执行 |
| 工具行 | 出现 `file_write`，状态为完成 | 未执行 |
| 权限卡片 | ask 模式下出现，可选择 | 未执行 |
| 文件落盘 | `zed_acp_ok.txt` 存在且内容为 `ZED_OK` | 未执行 |
| 关闭后子进程 | 退出，无残留 python | 未执行 |

### 14.5 其他宿主排查结论

| 宿主 | Windows | 能否直接托管 GA 的 ACP | 证据 / 说明 |
| --- | --- | --- | --- |
| **Zed** | ✅ 稳定版 | ✅ 配置 `agent_servers` 即可 | 官方文档明确支持自定义 ACP agent |
| **Claude Code** | ✅ | ❌ 不是 ACP client | `claude --help` 无 ACP；303 模块 0 命中；只能用 Bash 拉起客户端 shim |
| **Stably Orca** | ✅ 有 Windows 安装包 | ❌ 目前不是通用 ACP client | 公开 PR 显示 ACP 结构化聊天先接 Grok；不支持配置任意 ACP agent |
| **Orca Agent（echoVic/orca-agent）** | ✅ | ❌ 它自己是 ACP agent | `orca acp` 是“连接 ACP client”，与 GA 同侧 |
| **VS Code + ACP Client 扩展** | ✅ | ⚠️ 第三方扩展，未实测 | 扩展宣称可连任意 ACP-compatible agent |
| **JetBrains 新版** | ✅ | ⚠️ 官方文档称 ACP agent 暂不支持 WSL | 原生 Windows 未实测 |

> Orca 的结论随版本变化很快，接入前应以当时的官方文档 / 源码为准；
> 本文只记录 2026-11-06 可查到的状态。

### 14.6 排错清单

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 明明改了仓库 `llm.yaml`，安装版不生效 | 安装版读 `%APPDATA%\GenericAgent\llm.yaml` | 改 AppData 配置，或设 `GA_LLM_CONFIG=<repo>/llm.yaml` |
| Zed 里 agent 起不来，日志说找不到 python | GUI 没继承终端 PATH | `command` 写 python 绝对路径 |
| 导入 bridge 后宿主程序 stdout 消失 | bridge 模块在导入期把 stdout 重定向到 stderr | 不要 import；用 `subprocess` 启动，探测路径用 `importlib.util.find_spec` |
| `session/new` 报 `cwd is not a directory` | cwd 不存在或不是绝对路径 | 传已存在的绝对目录 |
| 看不到权限请求 | GA 当前是 `full_access` | 先 `session/set_mode` 切 `ask`，再发 mutating 工具任务 |
| `session/load` / `session/list` 报 not supported | 当前 bridge 未实现 | 不要依赖恢复/列表；每次新建会话 |
| 图片 prompt 不生效 | `promptCapabilities.image=false` | 当前只支持文本 prompt |
| prompt 长时间无响应 | 模型调用慢或任务过大 | demo 默认 300s 超时；可调 `--timeout`，并看 bridge stderr |
| 会话结束后 python 残留 | 没有发 `session/close` 或关闭 stdin | 客户端退出前调用 `session/close`，或关闭 bridge stdin |

补充说明：

- `--llm-no` 不传时使用 `active_profile`。安装版当前 `active_profile=zhanlu-ds`，
  所以 demo 不需要额外指定模型。
- 本次为了让安装版默认用 `zhanlu-ds`，把 provider/model/profile 合并进了
  `%APPDATA%\GenericAgent\llm.yaml`，合并前备份为
  `llm.yaml.bak.20261106-0.3.0`。这是本机操作，不属于仓库产物；
  其他机器需要按同样的三段结构自行合并，且不要把 API key 写进文档或提交。
- demo 的工作目录默认建在系统临时目录下，运行结束**不自动删除**，便于检查
  `acp_demo.txt` 和 `demo_result.json`。清理时由使用者手动处理。

### 14.7 最小复现命令

```powershell
# 前置：确认安装版
python -c "import genericagent, frontends.acp.genericagent_acp_bridge as b; print(genericagent.__file__, b.__file__)"

# allow：完整跑通工具 + 权限 + 文件落盘
python D:/git_codes/GenericAgent/scripts/acp_client_demo.py --permission allow

# deny：验证拒绝后工具失败、文件不落盘
python D:/git_codes/GenericAgent/scripts/acp_client_demo.py --deny

# 原始帧：观察 initialize/session/update/request_permission
python D:/git_codes/GenericAgent/scripts/acp_client_demo.py --debug
```

判定：三条命令退出码均为 0，且最后一次输出 `[result] ok=True`。

### 14.8 后续待办

1. **执行用例 B**：在 Windows 上安装 Zed，按 §14.4 配置 GA，回填 B.5 的验收表。
2. **固化测试资产**：`scripts/acp_client_demo.py` 目前是新文件，若要把 §14 作为
   长期回归环节，应随文档一起提交，并考虑加一条 CI 可选的真实 e2e（默认跳过，
   需要真实 LLM 凭据时显式开启）。
3. **补 ACP 协议面**：按 §13.4 的清单，优先做 `session/load`、`session/list`、
   图片 prompt 能力声明，以及 `agentInfo.version` 与 GA 版本对齐。
4. **CC 侧桥（可选）**：如果确实要让 Claude Code 直接消费 GA 的 ACP，
   需要单独实现 ACP ↔ CC stream-json 双向桥，不属于 GA 仓库的范围。
