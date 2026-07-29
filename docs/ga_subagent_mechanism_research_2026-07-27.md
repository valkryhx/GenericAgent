# GA subagent 机制调研：Claude Code / Codex 对比与增强建议

日期：2026-07-27

## 1. 一句话结论

你的直觉是对的：**GA 目前的文件/进程型 subagent 机制已经不再是最早期的“简单开个后台进程”，但相对 Claude Code 和 Codex 仍然偏简陋，主要缺的是统一 task control plane、正式 registry/canonical path、强权限隔离、sidechain transcript/resume、事件化 final artifact、以及 foreground/background 生命周期管理。**

不过 GA 并非没有基础。当前 GA 已经有：

- `SubagentManager`
- `state.json`
- `events.jsonl`
- `mailbox.jsonl`
- 父 inbox
- `wait_agent` / `read_agent_result` 分离
- context fork
- `interrupt_agent`
- workflow child agent 的权限、transcript、MCP/Skill 能力快照和取消机制

因此最合理路线不是推倒重写，而是：

```text
保留现有文件/进程型 subagent 协议
  + 引入 registry / canonical path / run id
  + 复用 workflow_permissions.py
  + 升级 mailbox / close / final artifact
  + 统一 workflow child 与 process subagent 的状态模型
  + 再逐步做 resume / foreground→background / fork 优化
```

## 2. 调研范围

### Claude Code

本地源码：

```text
D:\git_codes\claude-reviews-claude\claude-code-fork\src
```

重点文件：

- `tools/AgentTool/AgentTool.tsx`
- `tools/AgentTool/runAgent.ts`
- `tools/AgentTool/agentToolUtils.ts`
- `tools/AgentTool/forkSubagent.ts`
- `tools/AgentTool/resumeAgent.ts`
- `tools/AgentTool/loadAgentsDir.ts`
- `tasks/LocalAgentTask/LocalAgentTask.tsx`
- `tools/SendMessageTool/SendMessageTool.ts`
- `tools/TaskOutputTool/TaskOutputTool.tsx`
- `tools/TaskStopTool/TaskStopTool.ts`
- `utils/forkedAgent.ts`
- `utils/sessionStorage.ts`

### Codex

本地源码：

```text
D:\git_codes\codex\codex-rs
```

重点文件：

- `core/src/tools/handlers/multi_agents.rs`
- `core/src/tools/handlers/multi_agents/spawn.rs`
- `core/src/tools/handlers/multi_agents/wait.rs`
- `core/src/tools/handlers/multi_agents/send_input.rs`
- `core/src/tools/handlers/multi_agents/close_agent.rs`
- `core/src/tools/handlers/multi_agents_v2.rs`
- `core/src/tools/handlers/multi_agents_v2/spawn.rs`
- `core/src/tools/handlers/multi_agents_v2/send_message.rs`
- `core/src/tools/handlers/multi_agents_v2/followup_task.rs`
- `core/src/tools/handlers/multi_agents_v2/close_agent.rs`
- `core/src/tools/handlers/multi_agents_v2/list_agents.rs`
- `core/src/tools/handlers/multi_agents_common.rs`
- `protocol/src/agent_path.rs`
- `core/src/context/subagent_notification.rs`
- `core/src/session/multi_agents.rs`
- `core/src/tools/handlers/agent_jobs.rs`

### GA

当前仓库重点文件：

- `subagent_manager.py`
- `subagent_state.py`
- `agentmain.py`
- `ga.py`
- `subagent_prompts.py`
- `assets/tools_schema.json`
- `workflow_child_agent.py`
- `workflow_runtime.py`
- `workflow_scheduler.py`
- `workflow_models.py`
- `workflow_permissions.py`
- `tests/test_ga_subagent_tools.py`
- `tests/test_workflow_child_agent.py`
- `docs/ga_subagent_codex_reference_2026-07-13.md`

## 3. Claude Code subagent 机制

### 3.1 整体结构

Claude Code 表面工具名现在是 `Agent`，旧名 `Task` 仍作为 alias 兼容。

关键文件：

```text
src/tools/AgentTool/constants.ts
```

关键常量：

```ts
AGENT_TOOL_NAME = 'Agent'
LEGACY_AGENT_TOOL_NAME = 'Task'
```

Claude Code 的 subagent 不是单个“函数调用里临时多跑一个模型”，而是一整套 task control plane：

```text
AgentTool
  ↓
agent definition registry
  ↓
工具/权限/model/effort/skills/MCP/hooks 解析
  ↓
runAgent(...)
  ↓
createSubagentContext(...)
  ↓
query(...) 主循环
  ↓
LocalAgentTask / RemoteAgentTask registry
  ↓
notification / TaskOutput / TaskStop / SendMessage / resume
```

### 3.2 AgentTool 入口

关键文件：

```text
src/tools/AgentTool/AgentTool.tsx
```

它负责：

- 解析输入参数：`description`、`prompt`、`subagent_type`、`model`、`run_in_background`、`name`、`isolation` 等。
- 找到 agent definition。
- 检查权限和 MCP server。
- 判断 sync / async。
- 组装 worker tool pool。
- 处理 worktree / remote isolation。
- 注册 foreground 或 background task。
- 返回 completed 结果或 async handle。

输入 schema 支持：

```text
description
prompt
subagent_type
model: sonnet | opus | haiku
run_in_background
name
mode
isolation
cwd
```

输出分两种：

```text
sync completed：直接返回结果和 metadata
async launched：返回 agentId / outputFile / canReadOutputFile
```

### 3.3 Agent definition registry

关键文件：

```text
src/tools/AgentTool/loadAgentsDir.ts
```

Agent definition 支持字段非常丰富：

- `agentType`
- `whenToUse`
- `tools`
- `disallowedTools`
- `skills`
- `mcpServers`
- `hooks`
- `color`
- `model`
- `effort`
- `permissionMode`
- `maxTurns`
- `background`
- `initialPrompt`
- `memory`
- `isolation`
- `omitClaudeMd`

来源覆盖顺序包括：

- built-in
- plugin
- user settings
- project settings
- flag settings
- policy settings

这让 Claude Code 的 subagent 不只是“开一个子任务”，而是可配置的专业 agent 类型系统。

### 3.4 工具权限与 capability isolation

关键文件：

```text
src/tools/AgentTool/agentToolUtils.ts
src/tools/AgentTool/runAgent.ts
```

Claude Code 的工具权限不是简单继承父 agent 工具：

1. worker 重新 assemble tool pool。
2. agent definition 的 `tools` / `disallowedTools` 再过滤。
3. async agent 还有 async-safe 限制。
4. MCP tools 有特殊处理。
5. `allowedTools` 是 session-scoped 限制。
6. `permissionMode` 可由 agent definition override。
7. background agent 无 UI prompt 时自动避免交互式权限弹窗。

这是 GA 目前最缺的一层：GA 文件/进程型 subagent 基本继承 GenericAgent 的工具面，没有 per-subagent allowed/denied tools 或 permission profile。

### 3.5 上下文隔离

关键文件：

```text
src/utils/forkedAgent.ts
src/tools/AgentTool/runAgent.ts
```

`createSubagentContext(...)` 会创建隔离的 `ToolUseContext`：

- clone read file state。
- nested memory / loaded memory / skill discovery / tool decisions 新建集合。
- content replacement state clone。
- abort controller 根据 sync/async 分配。
- async subagent 有 local denial tracking。
- UI callbacks 清空。
- 新 agentId 和 query tracking chain。

关键语义：

```text
sync agent 可共享 parent abort / app state
async agent 默认独立 abort controller
background agent 不应因为 parent turn 结束而消失
```

### 3.6 fork subagent

关键文件：

```text
src/tools/AgentTool/forkSubagent.ts
```

Claude Code 的 fork subagent 是高级模式：

- 继承 parent system prompt。
- 继承 parent messages。
- 继承 exact tools。
- `model: inherit`。
- `permissionMode: bubble`。
- 用 placeholder tool results 构造 byte-identical prefix，以最大化 prompt cache 命中。
- 防递归 fork。

GA 当前有 `fork_turns`，但只是把父 backend history 写入 `_history.json`，没有 Claude Code 这种 cache-safe fork prefix、exact tool inheritance、fork-specific permissionMode 和递归防护。

### 3.7 task registry / foreground-background / notification

关键文件：

```text
src/tasks/LocalAgentTask/LocalAgentTask.tsx
src/tools/TaskOutputTool/TaskOutputTool.tsx
src/tools/TaskStopTool/TaskStopTool.ts
src/tools/SendMessageTool/SendMessageTool.ts
```

Claude Code 把 agent 执行和 task 管理分离：

- `AgentTool`：启动/路由。
- `runAgent`：执行 agent loop。
- `LocalAgentTask`：状态、progress、result、pending messages、cleanup。
- `TaskOutput`：读取或等待结果。
- `TaskStop`：取消。
- `SendMessage`：给 running/stopped/evicted agent 投递消息或恢复。

生命周期能力：

- async-from-start。
- sync foreground agent。
- foreground 可转 background。
- 后台完成发 task notification。
- completed agent 可 SendMessage 恢复。
- 内存 task 被清理后可从 sidechain transcript 恢复。

GA 当前有 `wait_agent`、`read_agent_result`、`send_message`、`followup_task`、`interrupt_agent`，但还没有统一 task registry、foreground→background、sidechain resume 这一整套控制面。

### 3.8 sidechain transcript

关键文件：

```text
src/utils/sessionStorage.ts
src/tools/AgentTool/resumeAgent.ts
```

Claude Code 保存 subagent sidechain transcript：

- agent messages
- tool uses/results
- compact boundaries
- metadata sidecar
- worktree path
- agentType
- description

这是它能 resume/continue subagent 的基础。

GA 当前文件/进程型 subagent 主要保存 output/state/events/mailbox，而不是完整 LLM sidechain transcript。workflow child agent 反而有更接近 transcript 的 `transcript_events`。

## 4. Codex subagent 机制

### 4.1 Codex 明确有 multi-agent tool surface

关键文件：

```text
D:\git_codes\codex\codex-rs\core\src\tools\handlers\multi_agents.rs
```

文件开头说明：

```rust
//! Implements the collaboration tool surface for spawning and managing sub-agents.
//!
//! This handler translates model tool calls into `AgentControl` operations and keeps spawned
//! agents aligned with the live turn that created them. Sub-agents start from the turn's effective
//! config, inherit runtime-only state such as provider, approval policy, sandbox, and cwd, and
//! then optionally layer role-specific config on top.
```

这段很关键：Codex 的 subagent 启动不是随便 clone 配置，而是从 live turn 的 effective config 开始，继承 provider、approval policy、sandbox、cwd 等 runtime state，再叠加 role-specific config。

### 4.2 V1 multi_agents 工具

`multi_agents.rs` 暴露：

```rust
CloseAgentHandler
ResumeAgentHandler
SendInputHandler
SpawnAgentHandler
WaitAgentHandler
```

对应模块：

```text
multi_agents/spawn.rs
multi_agents/wait.rs
multi_agents/send_input.rs
multi_agents/close_agent.rs
multi_agents/resume_agent.rs
```

V1 使用 thread id 作为 target，工具名在 namespace 下，例如：

```text
multi_agent.spawn_agent
multi_agent.wait_agent
multi_agent.send_input
multi_agent.close_agent
```

### 4.3 V2 multi_agents 工具

关键文件：

```text
core/src/tools/handlers/multi_agents_v2.rs
```

V2 暴露 plain tool：

```rust
close_agent
followup_task
list_agents
send_message
spawn_agent
wait_agent
```

这与 GA 当前工具面非常接近：

```text
spawn_agent
list_agents
wait_agent
read_agent_result
send_message
followup_task
interrupt_agent
```

但 Codex V2 的关键增强是：**canonical agent path + list_agents + close_agent + send_message/followup_task 分离**。

### 4.4 AgentPath：canonical path/tree

关键文件：

```text
protocol/src/agent_path.rs
```

核心结构：

```rust
pub struct AgentPath(String);
```

关键语义：

```rust
pub const ROOT: &str = "/root";
pub const MORPHEUS: &str = "/morpheus";

pub fn join(&self, agent_name: &str) -> Result<Self, String> {
    validate_agent_name(agent_name)?;
    Self::from_string(format!("{self}/{agent_name}"))
}

pub fn resolve(&self, reference: &str) -> Result<Self, String> { ... }
```

验证规则：

- absolute path 必须从 `/root` 开始，或是 `/morpheus`。
- agent name 只能是小写字母、数字、下划线。
- 不能使用 `root`、`.`、`..`。
- 不能包含 `/`。

这比 GA 当前的 `normalize_task_name()` 强很多。GA 目前 path-like target 最终基本取最后一段，不能稳定表示 `/root/researcher/worker` 这种树。

### 4.5 Codex spawn_agent V2

关键文件：

```text
core/src/tools/handlers/multi_agents_v2/spawn.rs
```

输入：

```rust
struct SpawnAgentArgs {
    message: String,
    task_name: String,
    agent_type: Option<String>,
    model: Option<String>,
    reasoning_effort: Option<ReasoningEffort>,
    service_tier: Option<String>,
    fork_turns: Option<String>,
    fork_context: Option<bool>,
}
```

重要语义：

- `task_name` 进入 `thread_spawn_source(...)`，生成 canonical agent path。
- `fork_turns` 支持：
  - `none`
  - `all`
  - positive integer string
- `fork_context` 在 V2 中被拒绝，提示使用 `fork_turns`。
- `FullHistory` fork 禁止同时指定 `agent_type` / `model` / `reasoning_effort`，因为 full-history fork 继承父 agent 类型、模型和 reasoning effort。

源码中明确：

```rust
if matches!(fork_mode, Some(SpawnAgentForkMode::FullHistory)) {
    reject_full_fork_spawn_overrides(role_name, args.model.as_deref(), args.reasoning_effort)?;
} else {
    apply_requested_spawn_agent_model_overrides(...);
    apply_role_to_config(...);
}
```

这点对 GA 很有启发：GA 现在允许 fork all 又可以隐含继承 llm_no，但没有清晰说明“full fork 与 agent_type/model override 是否互斥”。

### 4.6 Codex runtime config inheritance

关键文件：

```text
core/src/tools/handlers/multi_agents_common.rs
```

核心函数：

```rust
build_agent_spawn_config(base_instructions, turn)
build_agent_shared_config(turn)
apply_spawn_agent_runtime_overrides(config, turn)
```

注释非常明确：

```rust
The returned config starts from the parent's effective config and then refreshes the
runtime-owned fields carried on `turn`, including model selection, reasoning settings,
approval policy, sandbox, and cwd.
```

实际复制/刷新内容包括：

- model
- provider
- reasoning effort
- developer instructions
- compact prompt
- approval policy
- shell environment policy
- sandbox exe
- cwd
- permission profile

GA 当前文件/进程型 subagent 只显式传了 `llm_no` 和 `_history.json`，运行时 permission/sandbox/cwd/model provider 的继承没有这么结构化。

### 4.7 Codex wait_agent

关键文件：

```text
core/src/tools/handlers/multi_agents/wait.rs
```

核心机制：

- target 解析为 ThreadId。
- 订阅每个 agent 的 status watch channel。
- 如果已有 final status，立即返回。
- 否则等任一 agent final 或 timeout。
- 通过 `CollabWaitingBeginEvent` / `CollabWaitingEndEvent` 发送 UI/protocol 事件。

这比纯轮询文件更强。GA 当前 `wait_agent` 已经避免读取 output，而是等 state/events/process；方向正确，但底层不是 watch channel，而是文件事件/状态轮询。

### 4.8 Codex send_message / followup_task

关键文件：

```text
core/src/tools/handlers/multi_agents_v2/send_message.rs
core/src/tools/handlers/multi_agents_v2/followup_task.rs
```

两者共用 message tool，只是 delivery mode 不同：

```rust
send_message -> MessageDeliveryMode::QueueOnly
followup_task -> MessageDeliveryMode::TriggerTurn
```

这与 GA 当前设计非常相似：

```text
send_message: 写 mailbox，但不触发 turn
followup_task: 写 mailbox + reply.txt，触发 turn
```

差异是 Codex 把它抽象成同一个 message tool + delivery mode，而 GA 仍有新 mailbox 和旧 `reply.txt` 混用。

### 4.9 Codex close_agent

关键文件：

```text
core/src/tools/handlers/multi_agents_v2/close_agent.rs
```

关键行为：

- 使用 `resolve_agent_target(...)` 解析 target。
- root 不能 close：

```rust
if receiver_agent.agent_path.as_ref().is_some_and(AgentPath::is_root) {
    return Err("root is not a spawned agent")
}
```

- 订阅当前 status，保存 previous status。
- 调 `agent_control.close_agent(agent_id)`。
- 返回：

```rust
CloseAgentResult { previous_status }
```

GA 的 `SubagentManager` 里已有 `close_agent()`，但工具面尚未完整产品化，当前主要暴露 `interrupt_agent`。这正是最值得从 Codex 借鉴的短板之一。

### 4.10 Codex subagent notification

关键文件：

```text
core/src/context/subagent_notification.rs
```

Codex 会把 subagent 状态通知作为 contextual user fragment 注入：

```rust
markers: <subagent_notification> ... </subagent_notification>
body:
{
  "agent_path": ...,
  "status": ...
}
```

GA 当前已有父 inbox 和 events，但还没有标准化地把 subagent completion/status 作为主 agent 下一轮上下文片段注入。现在更多是靠工具调用 `wait_agent` / `read_agent_result`。

### 4.11 Codex agent_jobs

关键文件：

```text
core/src/tools/handlers/agent_jobs.rs
```

这是批量 agent job 系统，例如 CSV 多 worker：

- 默认并发 16。
- 最大并发 64。
- 每个 item spawn 一个 worker subagent。
- state db 记录 job/item status。
- 支持 recovery running items。
- 每个 item 有 timeout。
- item completion 通过工具报告。

这说明 Codex 不只支持手动 spawn subagent，还支持更高层的批量 agent job control plane。

GA 的 dynamic workflow 已经有类似方向：`workflow_scheduler.py` + `NativeGPTChildAgentRunner`。但它和 `SubagentManager` 是两套系统，没有统一 agent job/subagent abstraction。

## 5. GA 当前 subagent 机制

### 5.1 GA 现在有两套子 agent 体系

#### A. 文件/进程型 subagent

核心文件：

```text
subagent_manager.py
subagent_state.py
agentmain.py
ga.py
assets/tools_schema.json
subagent_prompts.py
```

工具面：

- `spawn_agent`
- `list_agents`
- `wait_agent`
- `read_agent_result`
- `send_message`
- `followup_task`
- `interrupt_agent`

运行模型：

1. 父 agent 调用 `spawn_agent`。
2. `ga.py` 默认 `fork_turns="all"`，复制父 backend history。
3. `SubagentManager.spawn_agent()` 创建 `temp/<task_name>`。
4. 写入：
   - `input.txt`
   - `_history.json`
   - `state.json`
   - `events.jsonl`
5. 启动子进程：

```text
python agentmain.py --task <task_name> --nobg --task_root <root_dir> --llm_no <llm_no>
```

6. 子进程 `run_task_worker_loop(...)` 执行任务。
7. 输出写 `output<N>.txt`，末尾加 `[ROUND END]`。
8. 状态变为 completed / waiting_reply。
9. 父 agent 通过 `wait_agent` 等待状态，再用 `read_agent_result` 读最终输出。
10. `followup_task` 可触发下一轮。
11. `interrupt_agent` 写 `_stop`。

#### B. workflow child agent

核心文件：

```text
workflow_child_agent.py
workflow_runtime.py
workflow_scheduler.py
workflow_models.py
workflow_permissions.py
```

特点：

- workflow JS worker 调 `agent(...)` RPC。
- Python `WorkflowRuntime` 注册 `WorkflowJob`。
- `AgentScheduler` 控制并发、缓存、状态。
- `NativeGPTChildAgentRunner` 每个 job 一个线程 / 独立 session。
- 支持 MCP / Skill prompt / tool schema / permission policy。
- 有 transcript events。
- 有 schema validation。
- 有 cancellation。
- 有 capability snapshot。

这套比文件/进程型 subagent 更像“可控 child agent runner”。

### 5.2 GA 已有优点

GA 的文件/进程型 subagent 已经实现了一些重要改进：

- prompt 不泄漏到命令行参数，而是写 `input.txt`。
- 有 `_history.json` 做 context fork。
- 有 `state.json` 结构化状态。
- 有 `events.jsonl` 事件。
- 有父 inbox。
- `wait_agent` 不直接读 final output。
- `read_agent_result` 是最终输出权威入口。
- `send_message` 与 `followup_task` 区分 queue-only 和 trigger-turn。
- 有 `interrupt_agent`。
- 有 root/subagent usage hint。
- 有测试覆盖工具行为。

这些说明 GA 并不是完全简陋的玩具实现，而是已经有了可演进的协议雏形。

### 5.3 GA 主要短板

#### 5.3.1 没有正式 registry / canonical path / tree

GA 当前核心索引还是 `task_name` 和 `temp/<task_name>`。即使工具里有 `/root/<task>` 这类 path-like 表示，内部 normalize 后主要取最后一段。

问题：

- 同名任务容易覆盖旧 artifact。
- 不能表达 `/root/researcher/worker` 树。
- 不能 close descendants。
- 不能限制 max depth。
- 不能以 session 为单位管理 subagent registry。
- `list_agents` 难以做层级展示。

Codex 的 `AgentPath` 正好解决这个问题。

#### 5.3.2 权限隔离不够

文件/进程型 subagent 当前基本继承 GenericAgent 工具能力，没有：

- per-subagent permission profile
- allowed tools
- denied tools
- allowed MCP servers/tools
- denied MCP servers/tools
- async/background 不可交互权限策略

workflow child agent 已经有 `workflow_permissions.py`，但文件/进程型 subagent 没复用。

#### 5.3.3 context fork 默认 all，成本和泄漏风险高

默认：

```text
fork_turns = "all"
```

优点：子 agent 不缺上下文。

风险：

- token 成本高。
- 子任务可能拿到不必要的敏感上下文。
- 长会话性能差。
- 没有 fork token estimate / redaction / explicit source summary。

Codex V2 把 `fork_turns` 明确成 `none | all | positive integer`，并且 full-history fork 禁止 agent_type/model/reasoning overrides，这比 GA 当前语义更清楚。

#### 5.3.4 mailbox 仍与 reply.txt 并存

GA 当前：

- `send_message` 写 mailbox，不触发 turn。
- `followup_task` 写 mailbox + `reply.txt`，触发 turn。

这和 Codex 的 QueueOnly / TriggerTurn 设计方向一致，但 GA 仍有旧 `reply.txt`，导致协议不纯。

建议把 mailbox 升级为唯一消息总线。

#### 5.3.5 interrupt 不是强取消

`interrupt_agent` 写 `_stop`，需要 worker 回到检查点才生效。

它不能立即取消：

- 正在进行的 LLM 请求。
- 长时间工具调用。
- MCP 调用。

workflow child runner 反而有 `cancel_current_request()` / handler cancel，比文件/进程型 subagent 强。

#### 5.3.6 final output 仍依赖 `[ROUND END]` 文本 marker

GA 已有 `state.json` 和 final output path，但 `[ROUND END]` 仍是重要 marker。

问题：

- 文本 marker 可能被普通输出污染。
- snapshot/final 边界不够结构化。
- 不如 event/artifact manifest 稳定。

#### 5.3.7 缺 sidechain transcript / resume

GA 文件/进程型 subagent 保存 output/state/events/mailbox，但不保存完整 LLM turn sidechain。

因此很难做到 Claude Code 那种：

- stopped/completed agent SendMessage 继续。
- 内存状态被清理后从 transcript 恢复。
- 精确恢复 tool use/results。

workflow child 有 transcript events，可以作为 GA 统一 transcript schema 的参考。

#### 5.3.8 文件/进程型 subagent 与 workflow child agent 两套系统割裂

两套系统分别有：

- runner
- status
- result
- transcript/artifact
- permission/cancel
- capability snapshot

这会导致能力重复和语义分裂。长期应统一抽象。

### 5.4 GA 文件/进程型 subagent 不足：按严重程度排序

下面按“对安全性、正确性、可恢复性、可演进性、用户体验”的综合影响，从严重到轻微排序。这里专门只评价 GA 当前文件/进程型 subagent，即 `SubagentManager` + 子进程 + 文件协议这一套，不包含 workflow child agent。

#### S1. 缺正式 registry / canonical path / run id（最严重）

当前 GA 主要以 `task_name` 和 `temp/<task_name>` 作为 subagent 身份，path-like target 最终也主要落到最后一段 task name。这是最基础的 control plane 缺口。

影响：

- 同名 task 可能覆盖旧 artifact。
- 无法稳定表达 `/root/researcher/worker` 这种父子树。
- `list_agents` 难以按 path prefix / subtree 展示。
- `close_agent` / cascade close / max depth / descendants cleanup 都缺少身份基础。
- 运行记录无法自然关联到某次 run，resume/debug/audit 都不稳定。

对比：

- Codex 有 `AgentPath`，以 `/root/...` 作为 canonical tree。
- Claude Code 有 task registry / agentId / sidechain metadata。

优先级判断：**必须第一阶段解决。** 没有身份和 registry，后续权限、关闭、消息、resume 都会继续打补丁。

#### S2. 权限隔离不够，文件/进程型 subagent 默认工具面过宽

当前文件/进程型 subagent 基本继承 GenericAgent 的工具能力，缺少 per-subagent 的 permission profile / allowed tools / denied tools / MCP 白名单黑名单。

影响：

- 父 agent 想委托只读调研时，很难硬性限制子 agent 不写文件、不跑代码。
- background/headless 子进程遇到需要审批的操作时缺少清晰策略。
- 工具能力边界主要靠 prompt discipline，不够可靠。
- 安全模型和 workflow child agent 已有的 `workflow_permissions.py` 割裂。

对比：

- Claude Code 会重新 assemble worker tool pool，再叠加 agent definition 的 allow/deny、permissionMode、async-safe 限制。
- Codex 子 agent 会继承 live turn 的 permission profile、approval policy、sandbox/cwd 等 runtime state。
- GA workflow child agent 已经有 read_only / restricted_mcp / explicit_approval，但文件/进程型 subagent 没复用。

优先级判断：**和 registry 同级或紧随其后。** 这是安全边界问题，不只是工程整洁问题。

#### S3. 生命周期控制不完整：close_agent 未产品化，interrupt 只是协作式停止

GA `SubagentManager` 内部已有 `close_agent()` 雏形，但工具面主要暴露的是 `interrupt_agent`。`interrupt_agent` 通过写 `_stop` 文件工作，需要子进程回到检查点才生效。

影响：

- 不能立即取消正在进行的 LLM 请求、长工具调用或 MCP 调用。
- 缺少正式 `close_agent` 返回 previous_status / final artifact / graceful 标记。
- 无法关闭 descendants。
- 进程退出、等待回复、关闭、kill、interrupt 等状态语义还不够产品化。
- 父 agent 很难稳定清理不再需要的后台 worker。

对比：

- Codex V2 有 `close_agent`，禁止 close root，并返回 `previous_status`。
- Claude Code 有 TaskStop/kill、foreground/background task state、abort controller 和 cleanup handler。

优先级判断：**高。** 这是后台 agent 不泄漏、不失控的核心能力。

#### S4. 缺 sidechain transcript / resume，无法做到真正可继续、可审计

文件/进程型 subagent 当前保存 output/state/events/mailbox，但没有保存完整 LLM sidechain transcript。

影响：

- completed/stopped agent 无法像 Claude Code 那样用 `SendMessage` 恢复原上下文。
- 无法精确恢复 tool call/tool result。
- 调试时只能看 output/stdout/stderr/events，缺少完整 agent reasoning turn 边界和工具交互链。
- compact / resume / continuation 很难做成可靠能力。

对比：

- Claude Code 保存 sidechain transcript 和 metadata，task evicted 后仍可从 transcript 恢复。
- Codex 的 thread/session/rollout 体系比纯 output 文件更结构化。
- GA workflow child agent 已经有 `transcript_events`，可以反向作为统一 schema 参考。

优先级判断：**高。** 这决定 subagent 是“一次性后台命令”还是“可长期协作的 agent”。

#### S5. 默认 `fork_turns="all"` 成本和信息泄漏风险高

GA 文件/进程型 subagent 默认 fork 全部父 backend history。这个默认值降低了子 agent 缺上下文的概率，但成本和风险都偏高。

影响：

- 长会话下 token 成本明显增加。
- 子任务可能拿到不必要甚至敏感的上下文。
- 子 agent 更容易被历史噪音影响。
- 没有 token estimate、redaction、source summary 或 fork policy 记录。

对比：

- Codex V2 把 `fork_turns` 明确为 `none | all | positive integer`。
- Codex full-history fork 禁止同时覆盖 agent_type/model/reasoning effort，语义更清楚。
- Claude Code fork 有 cache-safe prefix、exact tools、递归防护和 permissionMode 设计。

优先级判断：**高-中。** 不一定先于 registry/permission/close，但应在 subagent 广泛使用前收敛。

#### S6. mailbox 协议与 `reply.txt` 并存，父子通信语义不够纯

GA 已经区分：

```text
send_message  = queue-only
followup_task = trigger-turn
```

但 `followup_task` 仍同时写 `mailbox.jsonl` 和旧式 `reply.txt`。

影响：

- 新旧协议并存，优先级和一致性容易出问题。
- queue-only 消息如果没有后续 trigger turn，可能长期不被模型看见。
- 消息缺少 message_id / ack / reply_to / consumed_at 之外的完整投递语义。
- 很难扩展 other recipients、优先级、批量消息、结构化附件。

对比：

- Codex V2 把 `send_message` / `followup_task` 收敛成同一 message tool，只是 delivery mode 分别为 QueueOnly / TriggerTurn。
- Claude Code 对 running agent 会 queue pending message，并在安全边界注入下一轮。

优先级判断：**中高。** 它影响协作可靠性，但可以在 registry 和 close 后逐步收敛。

#### S7. final output 仍依赖 `[ROUND END]` 文本 marker，artifact 协议偏脆弱

GA 已有 `state.json.final_output_path` 和 sha256，但 `[ROUND END]` 仍是 final output 判定的重要文本 marker。

影响：

- 普通输出中如果出现 marker，解析可能混淆。
- snapshot、partial output、final output 的边界不是纯结构化事件。
- 大输出分页、artifact 引用、resume 校验不够自然。

对比：

- Claude Code 的 task result / output file / transcript metadata 更结构化。
- Codex wait/status 关注 agent status，不靠文本 marker 判断完成。

优先级判断：**中。** 当前可用，但会成为长任务、流式输出和 resume 的隐患。

#### S8. 文件/进程型 subagent 与 workflow child agent 割裂，能力重复且不均衡

GA 现在两套子 agent 系统各自发展：

- 文件/进程型 subagent 有 mailbox、wait/read、父子进程协议。
- workflow child agent 有 permission、transcript_events、capability_snapshot、scheduler、schema validation、cancel_current_request。

影响：

- 同样是“子 agent”，用户/模型看到的能力却不一致。
- 权限、transcript、result、status、cancel 语义重复实现。
- workflow child 的强能力无法自然回填到通用 `spawn_agent`。
- 后续维护成本升高。

对比：

- Claude Code 的 subagent 最终进入统一 `query()` / task registry / sidechain 体系。
- Codex 的 manual spawn 和 agent_jobs 都走 AgentControl/thread/status 基础设施。

优先级判断：**中。** 不一定第一步合并，但必须在 v2 稳定后统一抽象。

#### S9. 缺 agent definition / role registry，子 agent 专业化主要靠 prompt

文件/进程型 subagent 目前没有类似 Claude Code agent definition registry，也没有 Codex role config layer 的稳定配置入口。

影响：

- `agent_type`、工具范围、模型、effort、permission、skills 不能作为稳定 agent profile 管理。
- 每次 spawn 都要在 message 里手写约束。
- 模型不容易可靠选择“什么任务该派什么 agent”。

对比：

- Claude Code 有 built-in/custom/plugin/policy agent definition。
- Codex spawn 支持 `agent_type` 并可 apply role config。

优先级判断：**中低。** 它能提升易用性和专业化，但应建立在 registry/permission/lifecycle 之后。

#### S10. 缺标准化 subagent notification 注入，仍偏主动 wait/读结果

GA 有父 inbox 和 events，但还没有像 Codex `<subagent_notification>` 或 Claude Code `<task-notification>` 那样，将后台 agent 完成/失败/关闭作为标准上下文片段推给父 agent。

影响：

- 父 agent 更容易主动轮询。
- UI 和模型上下文中的任务状态不够统一。
- 后台完成事件不一定自然触发下一步 synthesis。

对比：

- Claude Code 后台 agent 完成会产生 task notification。
- Codex 有 `SubagentNotification` contextual user fragment。

优先级判断：**中低。** 对体验很重要，但依赖 registry/status/final artifact 稳定后再做更稳。

#### S11. 不支持 foreground → background、worktree isolation、remote isolation 等高级能力

GA 文件/进程型 subagent 当前天然是子进程后台任务，没有 Claude Code 的 foreground agent 转后台，也没有 worktree/remote isolation。

影响：

- 无法先同步观察 agent，必要时转后台。
- 多个写代码 subagent 并行时缺少 worktree 隔离。
- 远程/沙箱隔离能力弱。

对比：

- Claude Code 支持 foreground/background 转换、worktree isolation、remote isolation。
- Codex 更强调 sandbox/cwd/policy 继承。

优先级判断：**较低。** 这些是高级体验和隔离能力，不应早于 registry、权限、close、transcript。

#### S12. 测试仍偏工具行为，复杂并发/异常/恢复路径覆盖不足

GA 已有不少 subagent 测试，但主要覆盖工具基本行为。更复杂的并发和异常路径还应加强。

建议补充：

- 多 subagent 并发 wait。
- duplicate task name / run id。
- mailbox 多消息顺序与 ack。
- close descendants。
- interrupt during running LLM/tool call。
- final output marker 被普通内容污染。
- malformed `_history.json` / `state.json` / `mailbox.jsonl`。
- Windows 文件锁和 atomic write 竞争。

优先级判断：**贯穿所有阶段。** 单独看严重度较低，但每个增强阶段都必须先写测试，否则 control plane 会越来越难维护。

### 5.5 严重度排序对应的修复优先级

按上述严重度，建议修复顺序不是完全线性的，但第一批必须覆盖 S1-S4：

```text
P0：S1 registry/canonical path/run id
P0：S2 permission isolation
P0：S3 close/cancel lifecycle
P1：S4 sidechain transcript/resume 基础
P1：S5 fork policy 收敛
P1：S6 mailbox 单一协议
P2：S7 final artifact event 化
P2：S8 workflow child/process subagent 抽象统一
P2：S10 notification 注入
P3：S9 agent definition/role registry
P3：S11 foreground-background/worktree/remote
持续：S12 测试覆盖
```

如果只能先做一个最小闭环，建议是：

```text
AgentPath + registry + run_id
  → list_agents(path_prefix)
  → close_agent(previous_status)
  → permission_profile=read_only
  → mailbox TriggerTurn/QueueOnly 单一协议
```

这组改动能最快把 GA 文件/进程型 subagent 从“轻量后台进程”推进到“有正式控制面的 subagent 系统”。

## 6. 能力对比表

| 能力 | Claude Code | Codex | GA 当前 |
|---|---|---|---|
| agent 类型 registry | 强，支持 built-in/custom/plugin/policy | 有 role/config layer | 弱，文件进程型无 agent type registry；workflow 有 job label/options |
| subagent 启动 | AgentTool → runAgent → query | spawn_agent → AgentControl | spawn_agent → SubagentManager → 子进程；workflow RPC → child runner |
| canonical path | agentId/name + task registry | 强，`/root/...` AgentPath | 弱，主要 task_name |
| list agents | task registry / UI | V2 `list_agents(path_prefix)` | 有 list_agents，但树/registry 弱 |
| wait | TaskOutput/block + notification | watch status channel | 文件 state/events 轮询 |
| send message | pending queue + resume | QueueOnly | mailbox queue-only，但语义较弱 |
| followup | SendMessage/resume | TriggerTurn | mailbox + reply.txt 触发 |
| close/stop | TaskStop/kill | close_agent returns previous_status | manager 有 close，工具面主要 interrupt |
| permission isolation | 强，多层 tools/allowed/permissionMode | 继承 live turn permission profile | 文件进程型弱；workflow child 较强 |
| context fork | 普通 / fork cache-safe | fork_turns none/all/N | fork_turns 写 `_history.json` |
| background notification | 强 task-notification | subagent_notification/context events | 父 inbox/events，但未标准注入主上下文 |
| sidechain transcript | 强，可 resume | rollout/thread/session 管理 | 文件进程型弱；workflow child 有 transcript_events |
| foreground→background | 支持 | 更偏 thread/control plane | 不支持 |
| worktree/remote isolation | 支持 | sandbox/cwd/policy inherit | 不支持 worktree，只有 task dir |
| agent jobs/batch | workflow + Agent | agent_jobs CSV workers | GA dynamic workflow 有类似但割裂 |

## 6.1 事件通知机制专项决策：应按 Codex 方向重做，而不是继续扩大文件轮询

GA 当前文件/进程型 subagent 的消息和状态传递，本质是文件协议 + 轮询：

```text
父进程 wait_agent：轮询 state.json / events.jsonl / parent inbox / process liveness
子进程 waiting_reply：轮询 _stop / mailbox.jsonl / reply.txt
```

这套机制简单、跨进程容易、Windows 下可用，但继续往上堆功能会遇到明显瓶颈：

- latency 取决于 poll interval。
- 父 agent 容易主动轮询，缺少自然完成通知。
- 子 agent 完成/失败/关闭没有标准上下文片段进入父 agent。
- 取消、关闭、followup、final artifact 都要靠更多文件 marker 互相补丁。
- 复杂并发时，事件顺序、去重、ack、cursor 都会越来越难维护。

因此事件通知机制建议**更明确地按 Codex 方向实现**，不要只是在现有文件轮询上继续微调。

### Codex 方向的关键点

Codex 值得借鉴的不是 Rust/Tokio 细节，而是控制面语义：

```text
AgentControl
  + status subscription / watch
  + Collab* Begin/End protocol events
  + SubagentNotification contextual fragment
  + QueueOnly / TriggerTurn message delivery mode
  + close_agent(previous_status)
  + canonical AgentPath
```

映射到 GA，可以设计为：

```text
SubagentEventBus
  + SubagentRegistry
  + AgentPath
  + AgentStatus subscription cursor
  + parent notification queue
  + wait_agent 等事件而不是扫 output
  + subagent_notification 注入父上下文/UI
```

### GA 可采用的 Python 落地方式

由于 GA 文件/进程型 subagent 是独立 Python 子进程，不能简单照搬 Codex 的进程内 watch channel。建议分两层落地：

#### 第一层：事件日志仍可落盘，但必须有 event cursor / notification 语义

保留：

```text
events.jsonl
parent inbox
state.json
```

但新增明确语义：

```text
event_seq
status_version
last_seen_event_seq
notification_id
agent_path
run_id
```

`wait_agent` 不再把重点放在扫描 output / process，而是等待：

```text
某个 target 的 status_version 增长
或 parent notification queue 出现 matching event
或 timeout
```

这仍可能底层短暂 poll 文件，但抽象上变成“事件 cursor 等待”，为之后替换成真正 IPC 留接口。

#### 第二层：引入进程间通知通道

后续可以增加一种真正事件通知通道：

```text
local socket / pipe / multiprocessing connection / lightweight event server
```

子 agent 完成、失败、等待回复、关闭、消费消息时主动发事件给 parent runtime。文件仍作为 durable fallback，不作为唯一实时通道。

### subagent_notification 应成为父 agent 上下文输入

借鉴 Codex 的 `<subagent_notification>`，GA 可定义：

```text
<ga_subagent_notification>
{
  "agent_path": "/root/researcher",
  "run_id": "agent_...",
  "status": "completed",
  "event_seq": 42,
  "summary": "...",
  "final_output_ref": "..."
}
</ga_subagent_notification>
```

用途：

- 后台子 agent 完成后，父 agent 下一轮自然知道状态。
- 减少模型主动 `wait_agent` 轮询。
- UI 可以统一显示通知。
- `read_agent_result` 仍保留为读取完整结果的权威入口。

### 与 workflow 机制的关系：可以借鉴，不要直接改动已有正确 workflow 代码

GA 已有 workflow 机制中有不少可复用思想：

- `workflow_scheduler.py` 的 job status / concurrency / result model。
- `workflow_models.py` 的 run/job/event 状态枚举。
- `workflow_child_agent.py` 的 `transcript_events`、capability snapshot、cancellation。
- `workflow_permissions.py` 的 permission profile。
- `workflow_runtime.py` 的 event/progress/artifact 组织方式。

但这次增强文件/进程型 subagent 是**另一个 feat**，不应为了 subagent v2 去修改已有 workflow 正确代码。正确做法是：

```text
只读参考 workflow 的模型和测试经验；
必要时抽出新的共享小模块，但必须先有测试证明不改变 workflow 行为；
不要在 subagent v2 初期重构 workflow_runtime / workflow_scheduler；
不要为了统一抽象破坏现有 dynamic workflow 已验证路径。
```

建议边界：

```text
可以借鉴：状态枚举、permission profile、transcript_events schema、capability snapshot 思路。
可以新增：subagent_event_bus.py、subagent_registry.py、subagent_agent_path.py。
谨慎抽取：shared agent event/result dataclass。
暂不改动：workflow_runtime.py、workflow_scheduler.py、workflow_child_agent.py 的正确主流程。
```

这样既能把 subagent 事件通知机制往 Codex 方向推进，又不会把已稳定的 workflow feat 牵连进来。

## 7. 推荐增强路线

### Phase 1：补正式 registry / canonical path / run id

新增或增强：

```text
subagent_registry.py
```

建议 registry 文件：

```text
temp/subagents/registry.json
```

每个 subagent 记录：

```json
{
  "agent_path": "/root/researcher",
  "task_name": "researcher",
  "run_id": "agent_...",
  "parent_path": "/root",
  "children": [],
  "pid": 1234,
  "task_dir": "temp/researcher",
  "state_path": "temp/researcher/state.json",
  "created_at": "...",
  "closed_at": null,
  "status": "running",
  "permission_profile": "inherit-current-permissions",
  "fork_turns": "all"
}
```

关键规则：

- 引入 Codex 式 `AgentPath`：`/root/<name>/<child>`。
- task name 仍可作为目录名，但 path 是权威 ID。
- 同名 task 不覆盖旧 artifact：目录可加 run id。
- `list_agents` 支持树形和 `path_prefix`。

优先测试：

- valid/invalid agent path。
- duplicate task name 不覆盖。
- parent/child path join。
- list by prefix。

### Phase 2：把 mailbox 升级为唯一消息总线

目标：逐步淘汰 `reply.txt`。

消息结构：

```json
{
  "message_id": "msg_...",
  "author": "/root",
  "recipient": "/root/researcher",
  "content": "...",
  "trigger_turn": true,
  "priority": "normal",
  "created_at": "...",
  "consumed_at": null,
  "acknowledged_at": null,
  "reply_to": null,
  "source_tool": "followup_task"
}
```

语义：

- `send_message`：QueueOnly。
- `followup_task`：TriggerTurn。
- TriggerTurn 时，把之前未消费的 QueueOnly 消息一起纳入下一轮 prompt。
- worker 不再依赖 `reply.txt`。

测试：

- 多消息顺序。
- queue-only 不触发 turn。
- followup 触发并包含未消费消息。
- consumed_at / ack。
- mailbox 损坏恢复。

### Phase 3：产品化 close_agent

GA manager 已有 `close_agent()`，建议正式暴露 tool：

```text
close_agent(target, cascade=false, timeout_seconds=...)
```

返回：

```json
{
  "target": "/root/researcher",
  "previous_status": {...},
  "status": "closed",
  "closed_descendants": [],
  "final_output_path": "...",
  "final_output_sha256": "...",
  "graceful": true
}
```

规则：

- root 不能 close。
- 支持 close descendants。
- close 前 flush final output/state/events。
- close 后 registry 标记 closed，不直接删除 artifact。

借鉴 Codex：返回 `previous_status`，并明确 root 不是 spawned agent。

### Phase 4：复用 workflow_permissions.py 到文件/进程型 subagent

在 `spawn_agent` 增加：

```text
permission_profile
allowed_tools
denied_tools
allowed_mcp_servers
denied_mcp_servers
allowed_mcp_tools
denied_mcp_tools
```

复用：

```text
workflow_permissions.py
```

行为：

- 子 agent 初始化时记录 permission profile。
- `GenericAgentHandler` 在子进程中读取 task state/metadata，应用 permission policy。
- read_only 拒绝 file_write/file_patch/code_run/非只读 MCP。
- explicit_approval 在 headless 子进程中返回 approval_required，不阻塞 UI。

测试：

- read_only 子 agent 能 file_read，不能 file_write/code_run。
- restricted_mcp 白名单/黑名单。
- permission profile 写入 state/events。

### Phase 5：升级 final output 为 artifact/event，不再依赖 `[ROUND END]`

新增 final event：

```json
{
  "type": "turn_completed",
  "final_output_ref": "output1.txt",
  "final_output_sha256": "...",
  "round": 1
}
```

`read_agent_result` 只信任：

1. `state.json.final_output_path`
2. `turn_completed.final_output_ref`
3. artifact manifest

`[ROUND END]` 只作为兼容 fallback。

### Phase 6：sidechain transcript / resume

目标：让 subagent 可以真正继续，而不是只 followup 一段新 prompt。

建议路径：

```text
temp/sessions/<session_id>/subagents/<agent_path_or_id>.jsonl
temp/sessions/<session_id>/subagents/<agent_path_or_id>.meta.json
```

记录：

- system prompt hash
- model/profile
- parent session id
- agent path
- input messages
- assistant outputs
- tool calls/results
- compact events
- permission decisions
- final result events

先复用 workflow child 的 transcript_events schema，再逐步统一。

### Phase 7：统一 workflow child 与 process subagent 抽象

建议抽象：

```text
AgentHandle
AgentState
AgentEvent
AgentResult
AgentRunner
AgentRegistry
PermissionProfile
TranscriptRef
ArtifactRef
```

让两套系统共享：

- status enum
- permission policy
- transcript schema
- capability snapshot
- result artifact
- cancellation semantics

短期不用强行合并运行方式，但要统一概念和协议。

### Phase 8：高级能力：foreground→background、worktree、fork cache

最后再做：

- foreground subagent 可转后台。
- task notification 自动注入主上下文。
- worktree isolation。
- full fork cache-safe prefix。
- agent definition registry。
- hooks。

这些价值高，但实现复杂，不应排在 registry/permission/final artifact 前面。

## 8. 推荐近期实施顺序

如果要从现在开始增强 GA subagent，我建议按这个顺序：

```text
1. subagent_registry.py + AgentPath + run_id
2. list_agents/path_prefix/duplicate task 测试
3. close_agent tool 产品化
4. mailbox 去 reply.txt 化
5. spawn_agent permission_profile 复用 workflow_permissions.py
6. final_output_ref event/artifact 化
7. subagent transcript_events schema
8. workflow child/process subagent 状态模型统一
```

原因：

- registry 解决“简陋感”的根：现在没有正式 control plane。
- close_agent 解决 lifecycle 不完整。
- mailbox 解决父子通信协议不纯。
- permission_profile 解决安全和能力边界。
- final artifact 解决 output 文本 marker 脆弱。
- transcript/resume 是后续高级能力基础。

## 9. 对现有 GA 实现的评价

GA 当前实现不是完全不可用，也不是只能重写。更准确评价是：

```text
GA 文件/进程型 subagent：
  已有可用的轻量分布式 worker 雏形，适合 bounded sidecar work；
  但 control plane 还不够正式，权限/registry/resume/lifecycle 仍偏简陋。

GA workflow child agent：
  设计更现代，权限、transcript、capability snapshot、scheduler 比文件/进程型 subagent 更完整；
  但它主要服务 dynamic workflow，与通用 spawn_agent 工具体系割裂。
```

所以最值得做的不是“照抄 Claude Code”，而是把 GA 已有两套能力合并到一个渐进式 control plane：

```text
SubagentManager 的进程隔离 / mailbox / wait/read
  + workflow child 的 permission / transcript / scheduler / capability snapshot
  + Codex 的 AgentPath / close_agent / QueueOnly vs TriggerTurn / live turn config inheritance
  + Claude Code 的 task registry / notification / SendMessage resume / sidechain transcript
```

## 10. 建议新增测试清单

### `tests/test_subagent_agent_path.py`

- `/root` 合法。
- `/root/a_b1` 合法。
- `/morpheus` 如不需要可不实现。
- `root`、`/foo`、`/root/..`、`/root/A`、`/root/a-b` 非法。
- join / resolve。

### `tests/test_subagent_registry.py`

- spawn 注册 root child。
- duplicate task name 生成不同 run id 或拒绝覆盖。
- list by path prefix。
- close 标记状态，不删除 artifact。
- cascade close descendants。

### `tests/test_subagent_mailbox.py`

- send_message queue-only 不触发 turn。
- followup_task trigger-turn。
- followup 带上之前未消费 queue-only 消息。
- consumed_at / ack。

### `tests/test_ga_subagent_permissions.py`

- read_only 子 agent 拒绝 write/code_run。
- restricted_mcp 只允许白名单 MCP。
- explicit_approval 不阻塞 headless 子进程。

### `tests/test_agentmain_subagent_lifecycle.py`

- `_stop` during waiting。
- `_stop` during running。
- child process crash。
- final_output_ref event。
- no `[ROUND END]` fallback path。

### `tests/test_subagent_transcript.py`

- sidechain transcript 写入。
- followup 第二轮追加。
- resume 可读取 meta。
- tool call/tool result 脱敏。

## 11. 实现取向决策：主线按 Codex，局部吸收 Claude Code

如果要在 GA 里真正开始实现 subagent v2，建议不要把问题理解成“二选一复制 Claude Code 或 Codex”。更稳的决策是：

```text
GA subagent v2 主体：按 Codex 路线做
GA subagent v3+ 高级能力：选择性吸收 Claude Code
```

也就是：

```text
Codex = GA 近期实现蓝图
Claude Code = GA 中长期增强参考
```

### 11.1 为什么主线更适合 Codex

Codex 的 multi-agent 机制和 GA 当前结构天然接近。GA 现在已经有：

- `spawn_agent`
- `list_agents`
- `wait_agent`
- `send_message`
- `followup_task`
- `interrupt_agent`
- `SubagentManager`
- `state.json`
- `events.jsonl`
- `mailbox.jsonl`
- context fork

而 Codex V2 也正是围绕这些概念组织：

- `spawn_agent`
- `list_agents`
- `wait_agent`
- `send_message`
- `followup_task`
- `close_agent`
- `AgentPath`
- QueueOnly vs TriggerTurn
- canonical `/root/...` path
- previous_status
- live turn config inheritance

因此 Codex 对 GA 是“增量增强”，不是把 GA 改造成另一个产品。

近期最值得直接借鉴 Codex 的是：

1. **AgentPath / canonical tree**
   - 用 `/root/<agent>/<child>` 替代单纯 `task_name`。
   - 解决同名覆盖、路径不稳定、无法表达父子树、无法按 prefix list/close 的问题。

2. **close_agent lifecycle**
   - 把 `SubagentManager.close_agent()` 产品化为正式工具。
   - 返回 `previous_status`。
   - root 不能 close。
   - 后续支持 cascade descendants。

3. **send_message / followup_task delivery mode**
   - `send_message` = QueueOnly。
   - `followup_task` = TriggerTurn。
   - GA 当前已有雏形，但还混用 `reply.txt`；应统一到 mailbox 协议。

4. **live turn config inheritance**
   - 子 agent 不应只继承 history / `llm_no`。
   - 还应结构化继承当前 provider/model、cwd、permission profile、sandbox/环境策略等 runtime state。

5. **subagent notification**
   - 子 agent 完成/失败/关闭后，以结构化通知进入父 agent 上下文或 UI 事件流。
   - 减少父 agent 主动轮询。

### 11.2 为什么不建议直接按 Claude Code 全量实现

Claude Code 的机制非常成熟，但它不是一个单点 subagent 方案，而是一整套产品级 task runtime：

- Agent definition registry
- built-in/custom/plugin/policy agent
- hooks
- MCP server 级 agent 配置
- skills preload
- foreground → background
- sidechain transcript
- `SendMessage` resume
- worktree isolation
- remote isolation
- prompt-cache-safe fork
- task notification
- `TaskOutput` / `TaskStop` 统一后台任务体系
- Claude Code UI / SDK 事件联动

这些能力值得学习，但如果 GA 现在直接照搬，会带来几个问题：

```text
实现跨度过大
需要重构大量 runtime
短期难以验证
容易把 GA 简洁结构复杂化
```

尤其是 Claude Code 的 fork subagent、sidechain resume、foreground/background 切换和 task registry，非常适合放到 GA 的中长期路线，而不适合作为第一阶段实现目标。

### 11.3 推荐分阶段取舍

#### 第一阶段：Codex-style control plane

优先实现：

```text
subagent_agent_path.py
subagent_registry.py
close_agent tool
mailbox 统一
list_agents path_prefix
subagent_notification
```

目标是把 GA 文件/进程型 subagent 从“能用的后台任务”升级为“有正式控制面的 subagent 系统”。

#### 第二阶段：融合 GA workflow child 的已有强项

GA 自己已经有一些比文件/进程型 subagent 更现代的能力：

- `workflow_permissions.py`
- `transcript_events`
- `capability_snapshot`
- scheduler/result schema
- cancellation hooks

应把这些能力回填到通用 `spawn_agent` 体系：

```text
spawn_agent(permission_profile="read_only")
spawn_agent(allowed_tools=[...])
spawn_agent(denied_tools=[...])
subagent transcript_events
final_output_ref
permission events
```

#### 第三阶段：吸收 Claude Code 高级能力

等 control plane 稳定后，再逐步吸收 Claude Code 的能力：

```text
sidechain transcript / resume
SendMessage 恢复 stopped agent
foreground -> background
TaskOutput / TaskStop 统一后台任务体系
worktree isolation
prompt-cache-safe fork
agent definition registry
hooks
```

这时 Claude Code 更像高级能力参考，而不是初始蓝图。

### 11.4 最终决策

如果必须在 Claude Code 和 Codex 之间选择一个作为 GA subagent v2 的主参照：

```text
选择 Codex。
```

完整表达是：

```text
短期架构照 Codex：
  AgentPath + registry + close/list/wait/send/followup + notification

中期融合 GA workflow child：
  permission + transcript + capability snapshot + schema validation

长期再吸收 Claude Code：
  sidechain resume + foreground/background + worktree/fork/agent registry
```

这条路线最符合 GA 当前代码基础，也最容易通过 TDD 分阶段落地。

## 12. 最终建议

短期目标不要定成“完全复制 Claude Code subagent”。Claude Code 的机制很强，但复杂度也很高，包含 Agent SDK/CLI/UI/hooks/MCP/worktree/task notification/sidechain resume 等全套产品能力。

GA 更现实的目标是：

```text
用 Codex 的 AgentPath + close/list/send/followup 语义，
补齐 SubagentManager 的 control plane；
再用 GA workflow child agent 已有的 permission/transcript 能力回填文件/进程型 subagent；
最后再吸收 Claude Code 的 sidechain resume、foreground→background、task notification。
```

这样既能明显增强 GA subagent，又不会一次引入过大复杂度。
