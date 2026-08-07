# P2-1 Agent Control Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 process subagent 与 dynamic workflow 正确执行路径的前提下，建立统一的 agent identity、状态投影、事件、结果/artifact 引用、workspace 元数据和能力声明控制面。

**Architecture:** 保留 `SubagentManager`/`agentmain.py` 的进程执行引擎和 `WorkflowRuntime`/`AgentScheduler`/`NativeGPTChildAgentRunner` 的 workflow 执行引擎。以“源边界加固与只读公共控制面 → 能力感知生命周期控制 → additive UI 投影 → 真实 API 验收”四个可回滚阶段交付；每一阶段都能独立测试。新增一个窄而深的 `UnifiedAgentControl` facade，由 `ProcessSubagentAdapter` 与 `WorkflowChildAdapter` 将两套持久化控制面投影到同一 read model；控制动作只有在 adapter 明确支持时才路由，不能用统一接口伪造 workflow child 的 mailbox、独立 cancel 或独立 resume。

**Tech Stack:** Python 3.10–3.13、标准库 `dataclasses`/`typing.Protocol`/`unittest`、现有 JSON/JSONL artifact store、Ink + TypeScript + Node test runner、opt-in 的 `gpt-5.6-luna` 真实 API E2E。

---

## 1. 已确认的架构事实

### 1.1 两个执行引擎不是同一个对象的两种名字

| 维度 | process subagent | workflow child agent |
| --- | --- | --- |
| 启动 | `SubagentManager.spawn_agent()` 启动独立 `agentmain.py --task` OS 进程 | `WorkflowRuntime` 启动 Node `workflow_js_worker.js`；`NativeGPTChildAgentRunner.start()` 为每个 job 启动 Python 线程 |
| 原始身份 | `AgentPath`、`task_name`、`run_id`、pid、registry entry | `WorkflowRun.run_id`、`WorkflowJob.job_id`、call index、job metadata |
| 状态真相 | `state.json` + `SubagentRegistry` + `events.jsonl`/event bus | `run.json`/`state.json` + `journal.jsonl` + `WorkflowJob.status` |
| 事件等待 | `SubagentEventBus.read_events_since()`、parent inbox、realtime IPC；`wait_agents()` 已避免轮询写放大 | runtime 在 scheduler tick 中 poll runner，workflow journal 再由 Ink bridge replay |
| 结果 | `SubagentArtifactStore` 的 `artifacts.json`、`final_output_ref`、output 文件、sidechain transcript | `agents/<job_id>/result.json`、`transcript.jsonl`、`final-result.json`、`workflow-progress.json` |
| workspace | `task_dir`、可选 worktree、`worktree_path` | `args.workspacePath` 经 `normalize_workflow_workspace()` 写入 job metadata，并由 child cwd 使用 |
| 权限/能力 | process state 中的 permission metadata、tools 运行时和 IPC 配置 | `ToolPermissionPolicy`、permission events、`capability_snapshot` |
| 取消/关闭 | `interrupt_agent`、`close_agent`、级联关闭、`_stop`、独立进程终止 | `runner.cancel(job)`、scheduler cancel、`WorkflowController.stop(run_id)`；公开路径主要是 run 级停止 |
| 恢复/消息 | sidechain transcript resume、durable mailbox、follow-up、attach/detach | workflow resume 是新 run 的 completed-prefix cache；job 没有等价的独立 resume/mailbox |
| UI | `ga.py` subagent tools 返回 state/event/result；父上下文可消费 inbox 通知 | `frontends/ink_bridge.py` 发 `workflow_run`/`workflow_event`/`workflow_progress`/`workflow_final`，Ink state 只维护 workflow 结构 |

因此 P2-1 的正确对象是控制面和 read model，不是一次性替换任一执行器。

### 1.2 直接参考实现的结论

直接读取 `D:\git_codes\codex\codex-rs` 得到以下可复用原则：

- `core/src/agent/control.rs` 的 `AgentControl` 由 root session 创建并共享给子 agent；它集中 spawn、send、inter-agent communication、interrupt、close、status 和 status subscription，但执行仍由 `ThreadManagerState` 完成。
- `core/src/agent/registry.rs` 的 `AgentRegistry` 维护 `AgentPath`、父子树、spawn reservation 和 metadata；它不是 LLM 执行器。
- `core/src/agent/status.rs` 从 `TurnStarted`、`TurnComplete`、`TurnAborted`、`Error`、`ShutdownComplete` 事件派生 `AgentStatus`，调用方订阅状态变化，不通过最终文本 marker 猜测完成。
- `core/src/agent/control.rs:987-1064` 的 completion watcher 把子 agent 的 terminal status 转成父侧通知；通知是控制面事件，不要求父侧重新读取全部 transcript。
- `core/src/thread_manager.rs:987` 的 `send_op` 是控制面到具体 thread 执行器的 seam。GA 应仿照这一层次，而不是让 workflow adapter 直接伪装成 process。

GA 已有的 `agent_runtime_models.py` 是一个真实但尚未完成的 seam：目前只有 `AgentStatus`、`ArtifactRef` 和 process event 转换，后续应增量扩展，不要再创建第三套状态 dataclass。

### 1.3 Claude Code 实例目录审计（2026-08-06）

本节基于对用户提供的 Claude Code 当前运行目录的只读审计：

```text
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\9a23b4a4-0438-41f4-9df7-ffff5291f943
```

目录职责不是“一个 agent 一个目录”的平面 registry，而是三层持久化结构：

```text
workflows/
  scripts/*.js                 # workflow 编排源码
  wf_<run-id>.json              # workflow run snapshot
subagents/workflows/
  wf_<run-id>/
    journal.jsonl               # logical invocation / attempt 事件
    agent-<agent-id>.jsonl      # 物理 agent attempt transcript
    agent-<agent-id>.meta.json  # 极窄的 attempt metadata
tool-results/                   # 工具结果引用/附件 namespace
```

审计统计如下，数字是该实例目录的观测值，不是新的运行时常量：

| 观测对象 | 数量/结果 | 设计含义 |
| --- | --- | --- |
| `workflows/wf_*.json` | 15 | 15 个顶层 workflow run snapshot |
| `workflows/scripts/*.js` | 15 | 每个观测 run 都有对应编排脚本；脚本源码还内嵌在 snapshot 的 `script` 字段中，和 `scriptPath` 对应 |
| `subagents/workflows/wf_*` | 15 | 与顶层 run 一一对应的 workflow transcript namespace |
| `journal.jsonl` `started` rows | 85 | 85 个物理启动/attempt 记录 |
| `journal.jsonl` `result` rows | 39 | 只有部分物理 attempt 产生独立 result 事件，不能用 transcript 文件数推导成功数 |
| `agent-*.jsonl` + `.meta.json` | 85 对 | 物理 attempt transcript/meta 对 |
| meta `agentType` | 67 个 `workflow-subagent`、18 个 `Explore` | namespace 还包含 workflow child 内部再 spawn 的 Explore subagent；85 不是 85 个顶层 workflow child |
| 当前 `workflow_agent` progress rows | 65 | logical child 的当前 read-model 投影；retry 后仍应只保留一个 logical record |
| logical journal key 出现多次 `started` | 11 个 key | 同一 logical invocation 可以产生多个 physical attempt |
| run snapshot status | 12 个 `completed`、3 个 `killed` | run-level 状态不能代表每个 child 的业务结果 |
| `completed` run 中包含 error child | 4 个 | raw run `completed` 不能直接投影为 common `succeeded` |
| `cached: true` progress row | 2 个 | cache 是来源状态，不能折叠成 fresh success |

代表性 `workflows/scripts/*.js` 还揭示了 Claude workflow 的编排 DSL，而不只是静态配置：

```javascript
export const meta = { name, description, phases: [...] }

phase("Inspect")
const result = await agent(prompt, {
  label,
  phase,
  schema,
  agentType,
  isolation,
})

const reviews = await parallel([
  () => agent(reviewPromptA, optionsA),
  () => agent(reviewPromptB, optionsB),
])

const outputs = await pipeline(tasks, task => agent(task.prompt, task.options))
return { result, reviews, outputs }
```

从这些脚本和 snapshot 的 `script` 字段可以反推出：`phase()` 只推进 workflow phase 视图；`agent()` 创建一个 workflow child invocation；`parallel()` 并发创建多个 child；`pipeline()` 按任务数组动态创建 child；`schema` 触发结构化输出，最终结果通常以 `StructuredOutput` 形式消费；后续 child 通过 `JSON.stringify(previousResult)` 消费前序结果。只有 1 个观测脚本显式使用 `isolation: "worktree"`。未发现通用 `resume`、`cancel` 或 `stop` DSL；这些控制语义若存在，属于 workflow/UI 的专用控制层，而不是脚本内置的 child 执行原语。

`workflows/wf_<run-id>.json` 是完整 run snapshot，至少包含 `runId`、`taskId`、`workflowName`、`status`、`startTime`、`durationMs`、`agentCount`、`defaultModel`、`phases`、`workflowProgress`、`result`、`logs`、`script`、`scriptPath`、`args`、`totalTokens`、`totalToolCalls` 和 `summary`。其中 `workflowProgress` 同时保存 phase 行和 `workflow_agent` logical 行；logical 行可见 `agentId`、`model`、`state`、`cached`、`resultPreview`、`promptPreview`，retry 场景还可见 `attempt`、`lastToolName`、`lastProgressReason` 等进度字段。`promptPreview` 只能作为来源事实，不能进入公共 snapshot 或日志。

`journal.jsonl` 的 `started` 行含 `type`、`key`、`agentId`，`result` 行在此基础上含 `result`。同一个 `key` 可能有多个 `started` 行，但 logical progress 最终只保留一个当前投影；每个 `agent-<agent-id>.jsonl` 则是一个物理 attempt。由此得到稳定的三层关系：

```text
sessionId
└── workflow run/container
    └── logical invocation key
        ├── physical attempt / agentId / transcript
        ├── physical attempt / agentId / transcript
        └── current attempt + logical progress projection
```

因此公共模型必须分离 `execution_id`（logical record）、`logical_key`（来源 logical invocation identity）和 `attempt_id`（当前物理 transcript identity）。重试不能创建第二个 logical child record，也不能覆盖已经存在的历史 transcript；它只更新 current attempt/read model，并保留 `attempt_index`/`attempt_count` 等可审计信息。`cached` 同样是 logical record 的来源状态，不是一次新的 provider execution。

每个物理 transcript 的事件类型主要是 `user`、`assistant`、`attachment`、`system`，公共 envelope 可见 `uuid`、`parentUuid`、`agentId`、`sessionId`、`isSidechain`、`cwd`、`timestamp`、`sourceToolUseID`、`attributionAgent` 和 `attributionSkill`。所有 workflow child transcript 都以 sidechain 形式保存；但 `.meta.json` 很窄，通常只有 `agentType`，只有 1 个 meta 额外出现 `worktreePath`。没有发现统一持久化的 parent execution id、permission profile、capability snapshot 或 normalized workspace。因此 GA 的 workspace/permission 字段必须保持 adapter 可选来源，不能声称 Claude transcript metadata 已经提供完整权限控制面。

这也解释了目录布局上的关键区别：`workflows/scripts` 是编排入口，`subagents/workflows` 是由 workflow 使用的 transcript namespace；后者没有独立的通用编排 `scripts` 目录，不能被当作第二套 workflow scheduler 或统一 durable control database。

### 1.4 Claude Code 源码对控制面的交叉验证

对 Claude Code 当前源码的交叉检查进一步确认：

- `Task.ts` 把 `local_agent`、`local_workflow`、`remote_agent` 等放入统一 `TaskState`/background-task UI，并共享 `pending`、`running`、`completed`、`failed`、`killed` 基础状态；这统一的是 Task/UI/SDK 控制面，不是执行器。`local_workflow` 没有因此变成 `local_agent` 的另一种 runtime。
- `BackgroundTasksDialog.tsx` 同时暴露通用的 `killWorkflowTask` 和 workflow-specific 的 `skipWorkflowAgent`、`retryWorkflowAgent`。因此 `skip`/`retry` 不能被简化成通用 `cancel`/`resume`，也不能把 child 动作映射成 run-level stop。当前参考分支的 `LocalWorkflowTask.ts`/`WorkflowDetailDialog.ts` 存在 stub，所以这里只确认控制动作的分层，不假定其完整执行语义。
- `utils/task/sdkProgress.ts` 让 agent/workflow 共享 `emitTaskProgress()` 事件出口；`workflow_progress` 是按 `${type}:${index}` upsert 的 delta batch，消费方重建 phase tree。它是 UI/SDK read-model projection，不是 durable transcript，也不能使用一个跨 run 的全局 workflow cursor。
- `runAgent.ts` 与 `sessionStorage.ts` 通过 `transcriptSubdir` 把 workflow child transcript 写入 `subagents/workflows/<runId>/agent-<agentId>.jsonl`。这证实该路径是 namespace grouping，而不是统一 agent registry。

P2-1 因而采用“通用基础动作 + engine/workflow-specific capability”的模型：GA common facade 只统一可证明的 `read`/`events`/`result`/artifact 引用和明确支持的 lifecycle action；`skip`、`retry` 如果未来加入，必须是显式 workflow capability，并单独定义 scope、状态迁移和审计事件。

### 1.5 缺陷影响分解

| 不一致 | 当前事实 | 直接风险 | P2-1 的收敛目标 |
| --- | --- | --- | --- |
| identity | process 用 `agent_path/run_id/pid`；workflow 用 `run_id/job_id`，不同 run 可重复 `agent_1` | UI、日志和控制调用方可能把两个 job 误认为同一个 agent | opaque `execution_id` + 明确 container/child relation |
| status | process 同时有 turn 和 process lifetime；workflow 有 run/job/cache/stale | `completed`、`waiting_reply`、`cached` 被错误折叠，导致错误的停止/恢复 | `status` 投影 + `source_status`/引擎细节并存 |
| events | process event bus 的游标全局递增；workflow journal 的 sequence 按 run 重置 | 单一 cursor 会漏掉第二个 workflow run 的事件，或重复投递旧事件 | source-scoped cursor + stable event id + 去重 |
| result/artifact | process 有 manifest、output path、sidechain transcript；workflow 有 `resultRef`、transcript JSONL、run artifacts | 调用方为读取结果而复制全文、读错目录或绕过 redaction | 只提供 bounded common result 和引用，原 store 仍为真相 |
| workspace/permission | process 的 task/worktree 和 permission metadata；workflow 的 args workspace、job metadata、policy snapshot | 能执行不等于能被同一套观测/审计正确展示 | 明确来源字段，按 engine 报告，不宣称路径相同 |
| lifecycle | process 有 interrupt/close/mailbox/resume/attach；workflow public contract 主要是 run stop/cancel/resume 新 run | child cancel 被误映射为整个 workflow stop，或 UI 显示虚假 resume | capability matrix；unsupported 必须无副作用 |
| execution | process 是独立 `agentmain.py` OS process；workflow child 是 scheduler 驱动的线程，workflow 另有 Node worker | 为了“统一 pid”而改执行器会破坏隔离、取消和真实 E2E | adapter/facade 统一控制面，不合并执行器 |

这解释了为什么 P2-1 当前没有一个单一的 workflow 主路径复现：它是跨 subsystem contract drift，而不是某个 child prompt 立即失败。风险会在以下交叉操作放大：同名 job 跨 run 展示、同时读取两类结果、UI 增量事件恢复、只取消一个 child、以及 workflow run 中途被 stop 后重新读取状态。故第一阶段必须先做只读投影和 cursor contract，再开放任何 mutation。

### 1.6 当前 GA 实施裁决：subagent 保持执行器，workflow 做边界改造

对 GA 当前代码再次逐项核对后，P2-1 不应平均改造两套系统。process subagent 的执行和生命周期控制已经完整，workflow 的执行器也应保留；真正需要修改的是观察 seam、workflow 终态汇总和 journal 并发写边界。

| 子系统/边界 | 当前代码证据 | 裁决 | P2-1 必需改动 |
| --- | --- | --- | --- |
| process subagent 执行器 | `subagent_manager.py` 已有 `AgentPath`、run/registry、OS process 生命周期、turn/process 双状态、event bus、mailbox、resume、attach/detach、interrupt/close、artifact/transcript、workspace/worktree 和 IPC | 核心执行器与生命周期不重构 | 不把 `SubagentManager` 变成 workflow scheduler，不改变 `agentmain.py` 启动路径 |
| process 只读枚举 | `SubagentManager.list_agents()` 在 `subagent_manager.py:695-720` 调用会写 `state.json`/registry 的 `read_agent()`；同文件 `probe_agent()` 已提供无写盘状态刷新 | adapter 不能调用现有 `list_agents()` 做 snapshot | 新增 additive `list_agent_snapshots(path_prefix=None, include_closed=False)`，使用 registry enumeration + `probe_agent()`；保留原 `list_agents()` 行为兼容 |
| workflow 执行器 | `WorkflowRuntime -> workflow_js_worker.js -> AgentScheduler -> NativeGPTChildAgentRunner` 已承载 DSL、并发、工具、权限、workspace、cache/resume 和 Test Gate | 保留现有 Node worker + Python scheduler/thread runner | 不强制 workflow child 启动 `agentmain.py`，不与 process subagent 合并执行器 |
| workflow 完成语义 | runtime 以 `manage_run_completion=False` 构造 scheduler，收到 worker `done` 后直接把 raw run 写成 `succeeded`；因此 scheduler 的 child aggregate 不参与 runtime finalization | raw status 继续表达 script/runtime 生命周期，但不能再被公共层无条件解释成业务成功 | 增加统一 finalization seam，持久化 bounded `childSummary` 和 `executionOutcome`; raw `succeeded` 且存在 handled child failure 时，common status 投影为 `partial` |
| workflow journal | controller、scheduler、runtime 都先 replay 再自行计算 `max(sequence)+1`，而 `WorkflowStore.append_event()` 直接 append | 并发 stop/runtime/scheduler writer 可产生重复 sequence，进而破坏 stable event id/cursor | 在 `WorkflowStore.append_event()` 内以每 run 的 cross-process journal lock 串行校验/分配 sequence，并返回最终写入的 event |
| workflow child identity | 当前 scheduler 使用 `agent_<call-index>`；GA 没有同一 run 内自动 physical retry，resume 创建新 run 并把完成前缀注册为 `cached` | 当前 `workflow-child:<run-id>:<job-id>` 已足够标识 logical child | 不伪造 Claude 的 `v2:<hash>`、`attempt_id` 或 attempt counters；cached child 保留 `cachedFromRunId`/`cachedFromJobId`，attempt 字段仅在未来来源真实持久化 retry 时填写 |

因此，GA subagent 只需要一个很小、无副作用的 read seam；workflow 则必须修改 source-side finalization、journal sequence 分配、adapter/facade 和 UI 状态投影。`SubagentManager`、`NativeGPTChildAgentRunner`、Node worker、原 artifact 布局和原生命周期协议都不属于本轮重写范围。

## 2. 目标控制面与不变量

### 2.0 交付分期和硬边界

P2-1 不把两个执行器改造成同一执行器，而是把“调用方观察和控制它们所需的最小事实”统一。实施顺序固定为：

1. **Phase A — 公共模型、源边界和两个 read-only adapter。** process 增加无副作用 `list_agent_snapshots()`；workflow 增加 `list_runs()`、bounded transcript reader、child aggregate/finalization 和 journal sequence serialization；随后由两个 adapter 建立公共投影。上述修改不改变任何 spawn、child runner 或 LLM 调用路径。
2. **Phase B — `UnifiedAgentControl` 和 capability-aware controls。** facade 先合并两个 adapter 的 read/events/result，再路由 process 的真实 interrupt/close/message/followup/resume/attach/detach 和 workflow **run 级** cancel/stop；workflow child 的不支持动作返回结构化结果。
3. **Phase C — Ink additive read model。** 新增 `agent_snapshot`/`agent_event`，保留所有 `workflow_*` 事件和旧 reducer 字段；common snapshot 不携带 prompt、transcript body 或完整 result。
4. **Phase D — opt-in real E2E and defect record.** 只使用 `llm.yaml` 中 profile `luna` 对应的 model `gpt-5.6-luna`，确定性 suite 和真实 provider suite 分开执行。

Phase A 是 P2-1 的最小可用切片；Phase B/C 任何一项失败都不能回头修改两个原始执行器来“凑统一”。

本计划只覆盖缺陷文档中的 P2-1。面向真实 workflow 的完整 TDD 行为链（先红、实现、绿、review、repair/retest、最终失败/成功）属于独立的 P2-2 计划；本计划的真实 API E2E 只验证两个执行引擎的身份、状态、事件和结果引用契约。

### 2.1 公共身份

公共 `execution_id` 是 opaque string，绝不假设 `run_id == job_id`，也不允许调用方拆字符串推断来源。第一期由 adapter 使用带 URL 编码的稳定构造函数生成；调用方只能把它原样回传给 facade：

```text
process agent:  process-agent:<encoded-run-or-session>:<encoded-agent-path>
workflow run:   workflow-run:<encoded-run-id>
workflow child: workflow-child:<encoded-run-id>:<encoded-job-id>
```

实际代码通过 `make_process_execution_id()`、`make_workflow_run_execution_id()`、`make_workflow_child_execution_id()` 生成；`UnifiedAgentControl` 维护 `execution_id -> adapter` 路由表。两个 workflow run 都可以有 `agent_1`，它们的 execution id 必须不同。`record_kind` 使用三个值：`process_agent`、`workflow_run`、`workflow_child`，不能用一个泛化的 `agent` 丢掉 container/child 关系。

`execution_id` 只标识一个可被 facade 查询和控制的 logical execution/container，不标识某个物理 transcript 文件。GA 当前没有同一 workflow run 内的自动 physical retry：scheduler 以 `agent_<call-index>` 创建 job，resume 则创建新 run，并把可复用前缀登记为 `cached`。因此当前 workflow adapter 必须令 `logical_key`、`attempt_id`、`attempt_index`、`attempt_count` 保持 `None`，除非 GA 来源文件未来真实持久化了这些字段；不得从 `job_id`、transcript 文件名或 Claude 的 `v2:<hash>` 规则猜造。cached child 通过 `metadata.cachedFromRunId`/`cachedFromJobId` 保留来源。公共模型仍保留可选 attempt 字段，以便未来实现同一 run retry 时不破坏 schema；届时每次 attempt 必须使用独立 transcript 路径，不能覆盖当前 `agents/<job-id>/transcript.jsonl`。process adapter 同样不为了填满公共 schema 复制 `execution_id`。

### 2.2 公共 record

`agent_runtime_models.py` 增加以下只读模型；已有 `AgentStatus`、`ArtifactRef` 和 `AgentEvent.from_subagent_event()` 的旧字段与行为保持兼容。

```python
@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str = "final_output"
    ref: str | None = None
    engine: str | None = None


@dataclass(frozen=True)
class AgentCapabilities:
    actions: frozenset[str] = frozenset()
    features: frozenset[str] = frozenset()

    def supports(self, name: str) -> bool:
        return name in self.actions or name in self.features

    def supports_action(self, name: str) -> bool:
        return name in self.actions


@dataclass(frozen=True)
class AgentRecord:
    execution_id: str
    engine: str                         # "process" | "workflow"
    record_kind: str                    # process_agent | workflow_run | workflow_child
    status: str                         # common projected status
    source_status: str | None = None    # original engine status, e.g. waiting_reply/cached
    agent_path: str | None = None
    parent_execution_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    logical_key: str | None = None
    attempt_id: str | None = None
    attempt_index: int | None = None
    attempt_count: int | None = None
    cached: bool = False
    task_name: str | None = None
    turn_status: str | None = None
    process_status: str | None = None
    workspace: str | None = None
    permission_profile: str | None = None
    permission_policy_version: str | None = None
    capability_snapshot: dict = field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    transcript_ref: str | None = None
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResultRecord:
    execution_id: str
    status: str
    payload: dict = field(default_factory=dict)
    final_text_ref: ArtifactRef | None = None
    transcript_ref: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class AgentEventBatch:
    events: tuple[AgentEvent, ...] = ()
    next_cursors: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
```

`AgentEvent` 增加 `event_id`、`engine`、`execution_id`、`record_kind`、`parent_execution_id`、`job_id`、`logical_key`、`attempt_id`、`attempt_index`、`source_sequence`、`source_cursor` 和 `occurred_at`；现有 `sequence` 字段保持兼容并继续等于原始 sequence。`event_id` 是来源事件的稳定 identity，不能用 `attempt_id` 覆盖 logical event identity；如果来源 sequence 可能在不同 physical attempts 间重复，adapter 必须在 fallback event id 中加入经过编码的 `attempt_id`，并在测试中证明两个 attempts 的 event id 不冲突。process event 的 `event_seq` 是一个 event bus 全局游标，source cursor 固定为 `process`；workflow journal 的 `sequence` 是**每个 run 独立**的游标，source cursor 必须由 `make_workflow_source_cursor(run_id)` 生成。ID、cursor 和 event id 对 run id 做同样的 URL 编码。因此 `AgentEventBatch.next_cursors` 的类型是 `dict[str, int]`，不能简化为单个 workflow 整数。跨 source 不伪造全局顺序，使用 source cursor 加 `event_id` 去重。

事件 ID 规则固定为：process 为 `process:<event_id-from-event-bus>`，workflow 为 `workflow:<encoded-run-id>:<sequence>`；缺失 process event id 时使用 `evt_<event_seq>` 作为 fallback。`make_workflow_source_cursor(run_id)` 返回 `workflow:<encoded-run-id>`。同一个 workflow journal event 只能投影成一个 common event；如果按 child 过滤，只改变查询结果，不重新生成另一个 ID。

`AgentResultRecord.payload` 只能是 `sanitize()` 后的 bounded summary；默认 `read_result()` 返回 `{}`，调用方显式请求 preview 时最多返回 240 个字符的摘要。完整 output/transcript 仍由原有 artifact store 通过引用读取。当前 GA workflow 没有同一 run physical retry，因此结果只引用当前 job artifact；未来若来源真实持久化 retry，才返回当前 logical execution 的 `attempt_id` metadata，并由原始 store 保留历史 attempt，不得把多个完整结果拼进 common record。

### 2.3 公共状态语义

`status` 表示当前 agent execution/turn 的公共状态；`turn_status` 和 `process_status` 保留引擎细节：

```text
pending | queued | running | succeeded | failed | cancelled |
partial | interrupted | killed | closed | cached | skipped | stale | unknown
```

process agent 在 `turn_status=completed, process_status=waiting_reply` 时映射为 `status=succeeded`，因为当前 turn 已完成但持久 worker 仍存活；workflow child 的 `cached` 保持 `cached`，不能折叠成 fresh `succeeded` 后丢失 resume/cache 语义。当前 resume 通过新 run + cached prefix 表达，不产生 attempt metadata；未来若增加同一 run retry，才允许在同一个 `execution_id` 下更新 current `attempt_id`/attempt counters，并必须保留历史 transcript。

workflow run 的 raw `status=succeeded` 只表示 JS script 正常返回、runtime/Test Gate 没有失败，不是充分的全量 child 成功判据。source finalizer 和 adapter 使用同一份 child 汇总规则，至少持久化 `total`、`succeeded`、`failed`、`cached`、`stale`、`cancelled`、`running`、`terminal`：raw `failed` 映射为 common `failed`；raw `succeeded` 且存在被脚本显式处理后留下的 failed/cancelled/killed/stale/non-terminal child 时映射为 common `partial`，同时保留 `source_status=succeeded` 和 `metadata.childSummary`；只有 raw `succeeded` 且所有 child 都是 success-like terminal state 时才映射为 `succeeded`。`executionOutcome` 使用 `succeeded | partial | failed` 表达这一聚合结果，但不覆盖 raw lifecycle status。`cached` child 可作为已完成 prefix 参与 aggregate，child record 仍暴露 `status=cached`、`source_status=cached`、`cached=true` 和 cache source，不能伪装成 fresh success。`killed`、`cancelled`、`interrupted` run 原样保留。

这里选择 `partial` 而不是无条件把 raw run 改成 `failed`，因为当前 GA DSL 没有 `requiredChild`/`optionalChild` contract，script 可以显式捕获某个 child error 并返回仍有价值的部分结果。未捕获的 child error、provider failure、Test Gate failure 和 verification failure 仍由现有 P0 路径把 raw run 置为 `failed`；P2-1 不放宽这些失败条件。未来若引入 required-child 标记，应在 workflow source contract 中让 required child failure 直接导致 raw `failed`，而不是继续依赖公共投影猜测。

### 2.4 能力必须显式而不等价伪装

| 公共能力 | process adapter | workflow run record | workflow child record |
| --- | --- | --- | --- |
| `read` / `events` / `result` / `artifacts` | 支持 | 支持 | 支持 |
| `workspace` / `permission` / `capability_snapshot` | 有 metadata，按实际存在报告 | run policy 可读 | job policy 与 snapshot 可读 |
| `interrupt` | `SubagentManager.interrupt_agent` | 不适用 | 第一阶段不声明 |
| `cancel` | 不声明；调用方明确选择 `interrupt` 或 `close` | `WorkflowController.cancel`（只限 run record） | 不支持；绝不偷换成整 run stop |
| `stop` | 不声明；process 用 `interrupt`/`close` | `WorkflowController.stop`（只限 run record） | 不支持；绝不偷换成整 run stop |
| `close` | `close_agent`，支持 cascade | run close 不是现有 contract | 不支持 |
| `message` / `followup` | mailbox 支持 | 不适用 | 不支持 |
| `resume` | sidechain transcript resume | 不在 common control 中声明；现有 bridge 的 resume 需要新 run、script、args 和 runtime | child 独立 resume 不支持 |
| `attach` / `detach` | 支持 | 不适用 | 不支持 |
| `cached` / `stale` | 不适用 | 支持 | 支持 |
| workflow-specific `skip` / `retry` | 不适用 | 不是通用 run action | P2-1 不声明；未来若实现必须是显式 child capability，不能映射为 `cancel`、`stop` 或 process `resume` |

统一 facade 遇到不支持动作返回结构化 `unsupported_capability`，不抛出模糊异常，也不跨 sibling job 扩大作用域。Claude 的 `skipWorkflowAgent`/`retryWorkflowAgent` 证实 workflow-specific action 应与通用 kill 分层，但 GA P2-1 不假定它们已经存在。workflow run 的 `stop`/`cancel` 只写入现有 controller contract；运行中的 `WorkflowRuntime` 通过已有 external-kill guard 观察 `killed`，不能把这个动作描述成 child-level cancellation。

## 3. 文件职责与迁移边界

### 3.1 新增/修改文件

| 文件 | 职责 |
| --- | --- |
| `agent_runtime_models.py` | 公共 immutable record、event、result、capability 和 engine-scoped cursor；保留现有 process converter |
| `agent_control.py` | `AgentControlAdapter` Protocol、`ControlRequest`/`ControlResult`、adapter 路由、`UnifiedAgentControl` facade |
| `subagent_manager.py` | 新增无副作用的 `list_agent_snapshots()`；保留会持久化刷新结果的 `list_agents()` |
| `agent_control_process.py` | 只把 `SubagentManager`/`SubagentEventBus`/`SubagentArtifactStore` 投影到公共模型；process 原始协议仍在原文件 |
| `agent_control_workflow.py` | 只把 `WorkflowStore`/`WorkflowController`/`WorkflowRun`/`WorkflowJob` 投影到公共模型；不接管 Node RPC |
| `workflow_models.py` | 定义共享的 bounded child summary 与 execution outcome 投影函数，不新增 raw run/job status |
| `workflow_store.py` | `list_runs()`、bounded transcript reader，以及 journal sequence 的 per-run cross-process serialization |
| `workflow_runtime.py` | worker `done`/failure finalization 时持久化 child summary 和 execution outcome；raw lifecycle status 保持兼容 |
| `workflow_scheduler.py` | 使用 store 分配的 journal sequence，并复用 child summary 语义；不接管 runtime completion |
| `workflow_controller.py` | stop/cancel event 改由 store 原子分配 sequence；原 action precondition 不变 |
| `tests/test_agent_runtime_models.py` | 公共模型序列化、状态和 cursor contract |
| `tests/test_subagent_manager.py` | `list_agent_snapshots()` 无写盘回归 |
| `tests/test_agent_control_process.py` | process adapter 映射与能力声明 |
| `tests/test_agent_control_workflow.py` | workflow run/job 映射、artifact/transcript/workspace |
| `tests/test_workflow_store.py` | `list_runs()`、bounded transcript reader 和 concurrent journal writer 的持久化边界 |
| `tests/test_workflow_runtime.py` | handled child failure 的 raw success/common partial finalization 回归 |
| `tests/test_workflow_scheduler.py` | source child summary 与 store-assigned sequence 回归 |
| `tests/test_agent_control.py` | facade 合并、路由、去重、unsupported 与控制结果 |
| `frontends/ink_bridge.py` | 增量发 common `agent_snapshot`/`agent_event`；原 workflow event API 保留 |
| `frontends/ink-ui/src/protocol.ts` | common agent record/event 的 bridge 类型 |
| `frontends/ink-ui/src/state.ts` | common agent read model state；workflow detail/progress state 保留 |
| `frontends/ink-ui/src/state.test.ts` | process/workflow common event reducer 回归 |
| `frontends/ink-ui/src/workflowStatusBar.ts` | 用 common record 做全局状态摘要，细节仍从 workflow progress 读取 |
| `tests/real_p2_1_agent_control_e2e.py` | opt-in、固定 `gpt-5.6-luna` 的跨引擎真实 API 验收 |
| `docs/workflow_defect_2026_08_05.md` | 实施后补充 P2-1 的阶段进度、验证结果和未支持能力 |

### 3.2 明确不迁移的文件/能力

- 不把 `NativeGPTChildAgentRunner` 改成启动 `agentmain.py`。
- 不把 `SubagentManager` 改成 `WorkflowJob` scheduler。
- 不移动已有 `state.json`、`events.jsonl`、`mailbox.jsonl`、`journal.jsonl`、`result.json`、`transcript.jsonl` 的物理位置。
- 不重做 P0 Test Gate、P1 planner topology、P1 预检删除、P1 workspace 传递、P1 P8 scanner；这些只作为回归基线。
- 不给 workflow child 虚构 mailbox、attach/detach 或独立 child resume；能力矩阵必须让 UI/调用方看见 unsupported。
- 不在本 P2-1 中改变 `workflow_permissions.py` 的产品决策、主会话 approval UI、worktree isolation 或真实跨进程 IPC。

## 4. TDD 实施任务

### Task 1: 扩展公共 runtime model

**Files:**
- Modify: `agent_runtime_models.py`
- Test: `tests/test_agent_runtime_models.py`

- [x] **Step 1: Write the failing tests**

在 `AgentRuntimeModelsTest` 中增加以下确定性测试：

```python
def test_execution_ids_are_engine_scoped_and_do_not_equal_job_id(self):
    from agent_runtime_models import make_workflow_child_execution_id, make_workflow_run_execution_id, make_workflow_source_cursor

    first = make_workflow_child_execution_id("wf_a", "agent_1")
    second = make_workflow_child_execution_id("wf_b", "agent_1")
    self.assertNotEqual(first, second)
    self.assertNotEqual(first, "agent_1")
    self.assertNotEqual(first, make_workflow_run_execution_id("wf_a"))

def test_logical_identity_is_stable_across_physical_attempts(self):
    from agent_runtime_models import AgentRecord, make_workflow_child_execution_id

    execution_id = make_workflow_child_execution_id("wf_retry", "agent_1")
    first = AgentRecord(
        execution_id=execution_id,
        engine="workflow",
        record_kind="workflow_child",
        status="running",
        run_id="wf_retry",
        job_id="agent_1",
        logical_key="v2:logical-hash",
        attempt_id="physical-old",
        attempt_index=1,
        attempt_count=2,
    )
    current = AgentRecord(
        execution_id=execution_id,
        engine="workflow",
        record_kind="workflow_child",
        status="succeeded",
        run_id="wf_retry",
        job_id="agent_1",
        logical_key="v2:logical-hash",
        attempt_id="physical-new",
        attempt_index=2,
        attempt_count=2,
    )
    self.assertEqual(first.execution_id, current.execution_id)
    self.assertEqual(first.logical_key, current.logical_key)
    self.assertNotEqual(first.attempt_id, current.attempt_id)
    self.assertNotEqual(current.attempt_id, current.execution_id)

def test_cached_status_is_not_serialized_as_fresh_success(self):
    from agent_runtime_models import AgentRecord

    record = AgentRecord(
        execution_id="workflow-child:wf_cache:agent_1",
        engine="workflow",
        record_kind="workflow_child",
        status="cached",
        source_status="cached",
        cached=True,
    )
    restored = AgentRecord.from_dict(record.to_dict())
    self.assertEqual("cached", restored.status)
    self.assertEqual("cached", restored.source_status)
    self.assertTrue(restored.cached)

def test_partial_status_round_trips_without_rewriting_source_status(self):
    from agent_runtime_models import AgentRecord

    record = AgentRecord(
        execution_id="workflow-run:wf_partial",
        engine="workflow",
        record_kind="workflow_run",
        status="partial",
        source_status="succeeded",
        metadata={"childSummary": {"total": 2, "succeeded": 1, "failed": 1}},
    )
    restored = AgentRecord.from_dict(record.to_dict())
    self.assertEqual("partial", restored.status)
    self.assertEqual("succeeded", restored.source_status)
    self.assertEqual(1, restored.metadata["childSummary"]["failed"])

def test_capabilities_report_unsupported_actions_explicitly(self):
    from agent_runtime_models import AgentCapabilities

    capabilities = AgentCapabilities(actions=frozenset({"read", "result"}))
    self.assertTrue(capabilities.supports("result"))
    self.assertFalse(capabilities.supports("resume"))
    self.assertFalse(capabilities.supports_action("resume"))

def test_workflow_event_preserves_source_cursor_and_identity(self):
    from agent_runtime_models import AgentEvent

    execution_id = make_workflow_child_execution_id("wf_a", "agent_1")
    parent_id = make_workflow_run_execution_id("wf_a")
    event = AgentEvent.from_workflow_event(
        {"type": "agent_completed", "runId": "wf_a", "jobId": "agent_1", "sequence": 9, "payload": {"resultRef": "agents/agent_1/result.json"}},
        execution_id=execution_id,
        parent_execution_id=parent_id,
    )
    self.assertEqual("workflow", event.engine)
    self.assertEqual(9, event.source_sequence)
    self.assertEqual(execution_id, event.execution_id)
    self.assertEqual(make_workflow_source_cursor("wf_a"), event.source_cursor)
    self.assertEqual("workflow:wf_a:9", event.event_id)

def test_process_conversion_adds_scoped_identity_without_changing_legacy_fields(self):
    from agent_runtime_models import make_process_execution_id

    execution_id = make_process_execution_id("run_1", "/root/demo")
    event = AgentEvent.from_subagent_event(
        {
            "event_seq": 7,
            "event_id": "evt_000007",
            "type": "turn_completed",
            "agent_path": "/root/demo",
            "run_id": "run_1",
            "created_at": "2026-08-06T00:00:00Z",
            "status": {"turn_status": "completed", "process_status": "waiting_reply"},
            "payload": {"final_output_ref": "final_output_round_0"},
        },
        execution_id=execution_id,
    )
    self.assertEqual(7, event.sequence)
    self.assertEqual("process", event.engine)
    self.assertEqual(execution_id, event.execution_id)
    self.assertEqual("process", event.source_cursor)
    self.assertEqual("process:evt_000007", event.event_id)
```

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_agent_runtime_models -v
```

Expected: the legacy process conversion test passes, while the new identity, optional attempt, cache, partial status, workflow converter and capability tests fail because the additive common model surface does not yet exist.

- [x] **Step 3: Implement the smallest model surface**

Add immutable dataclasses and the three engine-scoped ID factories described in §2.1, plus `make_workflow_source_cursor()`. Use `urllib.parse.quote(part, safe="")` for each identity component, so a slash in an agent path cannot collide with a delimiter. Add optional `logical_key`, `attempt_id`, `attempt_index`, `attempt_count` and `cached` fields to `AgentRecord`, and allow `partial` as a common projected status; `execution_id` must remain unchanged when only source-provided physical attempt metadata changes. These optional fields establish schema capacity only: Task 3 must prove the current GA workflow adapter leaves them unset when no persisted retry evidence exists. Implement `AgentEvent.from_workflow_event()` by copying only `sanitize(raw.get("payload") or {})`, assigning the logical/attempt fields supplied by the adapter, and using `event_id=f"workflow:{quote(run_id, safe='')}:{sequence}"` when the source sequence is unique. If a future source lacks a unique sequence, require a source event id or append the encoded `attempt_id` only to the fallback event id; never silently let two physical attempts share one event id. `source_cursor` is `make_workflow_source_cursor(run_id)`. Extend `AgentEvent.from_subagent_event()` to set `engine="process"`, `source_sequence=event_seq`, `source_cursor="process"`, `event_id=f"process:{raw_event_id or f'evt_{event_seq}'}"`, and `execution_id` from the supplied process identity without changing its existing `sequence`, `event_type`, `agent_path`, `run_id`, `status`, `payload`, or `artifact_ref` behavior. Add `to_dict()`/`from_dict()` round trips that accept the existing snake_case process names and emit bridge-compatible camelCase names. `AgentEventBatch` must serialize `next_cursors` as a mapping; it must never flatten per-run workflow cursors.

`AgentCapabilities.supports()` must return `False` for absent actions; it must not infer `resume` from `transcript_ref` or infer `cancel` from a terminal status.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_agent_runtime_models -v
```

Expected: all existing and new model tests pass.

- [x] **Step 5: Refactor for compatibility**

Round-trip the new common records through `to_dict()`/`from_dict()` using camelCase keys where the bridge already uses camelCase. Keep old snake_case process fields accepted by the converter. Do not modify `workflow_models.AgentResult` or `subagent_manager.AgentState` in this task.

- [x] **Step 6: Commit the model seam**

```powershell
git add agent_runtime_models.py tests/test_agent_runtime_models.py
git commit -m "feat(agent-control): define common agent runtime records"
```

### Task 2: Add the process adapter

**Files:**
- Modify: `subagent_manager.py`
- Create: `agent_control_process.py`
- Test: `tests/test_subagent_manager.py`
- Test: `tests/test_agent_control_process.py`
- Reference without changing: `subagent_event_bus.py`, `subagent_artifacts.py`, `subagent_transcript.py`

- [x] **Step 1: Write the failing no-write snapshot test**

Add this regression beside the existing `probe_agent` tests in `tests/test_subagent_manager.py`, reusing `_running_agent()` and `_count_writes()`:

```python
def test_list_agent_snapshots_uses_probe_without_writing(self):
    with tempfile.TemporaryDirectory() as td:
        task_dir = self._running_agent(td, "snapshot", 450)
        manager = SubagentManager(root_dir=td, process_exists=lambda _pid: False, sleep=lambda _: None)
        before = (task_dir / "state.json").read_text(encoding="utf-8")
        writes = self._count_writes()

        snapshots = manager.list_agent_snapshots(include_closed=True)

        self.assertEqual(["snapshot"], [state.task_name for state in snapshots])
        self.assertEqual("exited", snapshots[0].process_status)
        self.assertEqual([], writes)
        self.assertEqual(before, (task_dir / "state.json").read_text(encoding="utf-8"))
```

- [x] **Step 2: Write the failing adapter tests**

Add `ProcessSubagentAdapterTest` with these exact tests. The fake manager must expose `list_agent_snapshots()`, `probe_agent()`, `event_bus`, `temp_dir`, and the lifecycle methods used by the control test; no test starts a subprocess. Make `FakeManager.list_agents()` and `FakeManager.read_agent()` raise `AssertionError`, so a regression cannot silently restore either write-producing path.

```python
def test_completed_process_turn_maps_to_succeeded_but_keeps_waiting_process_status(self):
    state = make_state(turn_status="completed", process_status="waiting_reply")
    adapter = ProcessSubagentAdapter(FakeManager([state]))
    record = adapter.list_records(include_terminal=True)[0]
    self.assertEqual("succeeded", record.status)
    self.assertEqual("completed", record.turn_status)
    self.assertEqual("waiting_reply", record.process_status)

def test_process_record_keeps_identity_workspace_and_permission_metadata(self):
    state = make_state(
        run_id="run_0007",
        task_dir="C:/ga/temp/demo",
        permission_profile="read_only",
        worktree_path="C:/ga/worktrees/run_0007",
    )
    adapter = ProcessSubagentAdapter(FakeManager([state]))
    record = adapter.list_records(include_terminal=True)[0]
    self.assertEqual("run_0007", record.run_id)
    self.assertEqual("/root/demo", record.agent_path)
    self.assertEqual("C:/ga/temp/demo", record.workspace)
    self.assertEqual("read_only", record.permission_profile)
    self.assertEqual("C:/ga/worktrees/run_0007", record.metadata["worktreePath"])

def test_process_capabilities_include_only_real_process_actions(self):
    state = make_state()
    record = ProcessSubagentAdapter(FakeManager([state])).list_records(include_terminal=True)[0]
    for action in ("read", "events", "result", "artifacts", "interrupt", "close", "message", "followup", "resume", "attach", "detach"):
        self.assertTrue(record.capabilities.supports_action(action), action)
    self.assertFalse(record.capabilities.supports_action("cancel"))

def test_process_event_uses_event_bus_sequence_and_artifact_reference(self):
    state = make_state(run_id="run_0008", final_output_path="C:/ga/temp/demo/output.txt")
    raw = {
        "event_seq": 12,
        "event_id": "evt_000012",
        "type": "turn_completed",
        "agent_path": "/root/demo",
        "run_id": "run_0008",
        "created_at": "2026-08-06T00:00:00Z",
        "status": {"turn_status": "completed", "process_status": "waiting_reply"},
        "payload": {"final_output_ref": "final_output_round_0"},
    }
    manager = FakeManager([state], events=[raw])
    events = ProcessSubagentAdapter(manager).events_since({"process": 11})
    self.assertEqual([12], [event.source_sequence for event in events.events])
    self.assertEqual("process:evt_000012", events.events[0].event_id)
    self.assertEqual(12, events.next_cursors["process"])

def test_process_result_is_reference_only_by_default(self):
    state = make_state(run_id="run_0009", final_output_path="C:/ga/temp/demo/output.txt")
    adapter = ProcessSubagentAdapter(FakeManager([state]))
    result = adapter.read_result(make_process_execution_id(state.run_id, state.agent_path))
    self.assertEqual({}, result.payload)
    self.assertIsNotNone(result.final_text_ref)
    self.assertIsNotNone(result.transcript_ref)
```

Define `make_state()` with the real `AgentState` constructor and create a temporary `artifacts.json` containing one `final_output_round_0` entry whose `path` points to a temporary output file. `FakeManager.event_bus.read_events_since()` must filter by the passed integer and return the rows above. The adapter test must assert that the default result contains no full output text; a separate `include_preview=True` test may assert the preview is capped at 240 characters.

- [x] **Step 3: Run the snapshot and adapter tests and verify RED**

```powershell
python -m unittest tests.test_subagent_manager tests.test_agent_control_process -v
```

Expected: `list_agent_snapshots` is missing and the adapter import/method assertions fail; existing unrelated subagent manager tests remain green.

- [x] **Step 4: Implement `list_agent_snapshots()` and `ProcessSubagentAdapter`**

Use this interface:

```python
class ProcessSubagentAdapter:
    engine = "process"
    source_cursor = "process"

    def list_records(self, *, include_terminal=False, path_prefix=None):
        raise NotImplementedError
    def get_record(self, execution_id):
        raise NotImplementedError
    def events_since(self, cursors=None, *, execution_id=None):
        raise NotImplementedError
    def read_result(self, execution_id, *, include_preview=False):
        raise NotImplementedError
    def control(self, execution_id, request):
        raise NotImplementedError
```

Implement `SubagentManager.list_agent_snapshots(path_prefix=None, include_closed=False)` with the same registry-first/fallback-directory enumeration and filters as `list_agents()`, but call `probe_agent()` for every row and never call `read_agent()`, `_write_registry_entry()` or `atomic_write_json()`. Keep `list_agents()` byte-for-byte compatible at its public behavior; a small private enumeration helper is acceptable only if both methods still make their read-vs-write distinction explicit.

`ProcessSubagentAdapter.list_records()` reads `SubagentManager.list_agent_snapshots(include_closed=include_terminal)` and maps `AgentState`; `get_record()` and direct `read_result()` lookup use `probe_agent()` or a freshly built snapshot index. No common read path may call `read_agent()`. Use `AgentPath.parse(state.agent_path).parent` only to populate `metadata.parentAgentPath`; do not invent a root execution record. Preserve `task_dir` as `workspace`, `worktree_path`, artifact directory, permission profile, `parent_permission_mode`, and both process statuses in the record or bounded metadata. `events_since()` reads `cursors.get("process", 0)`, delegates to the durable event bus, converts each row with the common process converter, filters by execution identity after conversion, and returns `AgentEventBatch` with the maximum event sequence consumed. `read_result()` reads the artifact manifest and transcript metadata, returns `ArtifactRef`/`transcript_ref`, and includes a bounded preview only when `include_preview=True`; it never loads the full output into a common event.

The control mapping is exact: `interrupt -> interrupt_agent`, `close -> close_agent`, `message -> send_message`, `followup -> followup_task`, `resume -> resume_agent`, `attach -> attach_agent`, `detach -> detach_agent`. `close` accepts only the explicit boolean `payload["cascade"]`; `message`, `followup`, and `resume` require a non-empty `payload["message"]`; attach/detach pass only bounded numeric offsets and `max_chars`. Unknown or missing execution ids return `ControlResult(ok=False, code="not_found")`. `cancel` is not silently mapped to `interrupt` or `close`.

- [x] **Step 5: Run process adapter and existing process suites**

```powershell
python -m unittest tests.test_agent_control_process tests.test_agent_runtime_models tests.test_subagent_manager tests.test_ga_subagent_tools -v
```

Expected: all pass, with no additional process spawned by the adapter read tests.

- [x] **Step 6: Refactor and commit**

Keep all filesystem access in the adapter or existing stores. Do not add adapter-specific reads to `ga.py` or alter the subagent tool JSON shape.

```powershell
git add subagent_manager.py agent_control_process.py tests/test_subagent_manager.py tests/test_agent_control_process.py
git commit -m "feat(agent-control): project process subagents"
```

### Task 3: Harden workflow source contracts and add the workflow adapter

**Files:**
- Create: `agent_control_workflow.py`
- Modify: `workflow_models.py`
- Modify: `workflow_store.py`
- Modify: `workflow_runtime.py`
- Modify: `workflow_scheduler.py`
- Modify: `workflow_controller.py`
- Test: `tests/test_agent_control_workflow.py`
- Test: `tests/test_workflow_models.py`
- Test: `tests/test_workflow_store.py`
- Test: `tests/test_workflow_runtime.py`
- Test: `tests/test_workflow_scheduler.py`

- [x] **Step 1: Write the failing source finalization and journal tests**

Extend the `workflow_models` import in `tests/test_workflow_models.py` with `summarize_workflow_jobs` and `project_workflow_execution_outcome`, then add the shared aggregation contract:

```python
def test_workflow_child_summary_projects_handled_failure_to_partial(self):
    jobs = [
        WorkflowJob(job_id="agent_1", status="succeeded"),
        WorkflowJob(job_id="agent_2", status="failed"),
        WorkflowJob(
            job_id="agent_3",
            status="cached",
            metadata={"cachedFromRunId": "wf_source", "cachedFromJobId": "agent_3"},
        ),
    ]

    summary = summarize_workflow_jobs(jobs)

    self.assertEqual(3, summary["total"])
    self.assertEqual(1, summary["succeeded"])
    self.assertEqual(1, summary["failed"])
    self.assertEqual(1, summary["cached"])
    self.assertEqual(3, summary["terminal"])
    self.assertEqual("partial", project_workflow_execution_outcome("succeeded", summary))
    self.assertEqual("failed", project_workflow_execution_outcome("failed", summary))
```

Add this runtime regression to `tests/test_workflow_runtime.py`, reusing the existing `SucceedsThenFailsRunner`:

```python
def test_handled_child_failure_persists_partial_execution_outcome(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkflowStore(root=tmp)
        script = """
const first = await agent('succeeds first')
let handled = false
try {
  await agent('fails second')
} catch (error) {
  handled = true
}
return {handled, first}
"""
        run = store.create_run(
            WorkflowRun(run_id="wf_partial", session_id="session_test", script=script, status="running")
        )

        outcome = WorkflowRuntime(
            store=store,
            runner=SucceedsThenFailsRunner(),
            timeout_seconds=2.0,
        ).run(run)

        self.assertTrue(outcome.result["handled"])
        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.status)
        self.assertEqual("partial", loaded.metadata["executionOutcome"])
        self.assertEqual(1, loaded.metadata["childSummary"]["failed"])
        final_result = json.loads((Path(loaded.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
        progress = json.loads((Path(loaded.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
        self.assertEqual("partial", final_result["executionOutcome"])
        self.assertEqual(loaded.metadata["childSummary"], final_result["childSummary"])
        self.assertEqual(loaded.metadata["childSummary"], progress["childSummary"])
```

Add a timing-independent concurrent writer regression to `tests/test_workflow_store.py`:

```python
def test_append_event_allocates_unique_sequence_under_concurrent_writers(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkflowStore(root=tmp)
        run = store.create_run(WorkflowRun(run_id="wf_journal", session_id="session_test", script=""))
        barrier = threading.Barrier(3)
        result_lock = threading.Lock()
        assigned = []
        errors = []

        def write_event(event_type):
            try:
                local_store = WorkflowStore(root=tmp)
                barrier.wait()
                stored = local_store.append_event(
                    run.run_id,
                    WorkflowEvent(run_id=run.run_id, event_type=event_type, sequence=1),
                )
                with result_lock:
                    assigned.append(stored.sequence)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=write_event, args=(name,)) for name in ("writer_a", "writer_b")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual([1, 2], sorted(assigned))
        events = store.replay_events(run.run_id)
        self.assertEqual([1, 2], [event.sequence for event in events])
        self.assertEqual(2, len({event.sequence for event in events}))

def test_list_runs_and_bounded_transcript_reader_are_public_and_safe(self):
    with tempfile.TemporaryDirectory() as tmp:
        store = WorkflowStore(root=tmp)
        run_b = store.create_run(
            WorkflowRun(
                run_id="wf_b",
                session_id="session_b",
                script="",
                jobs=[WorkflowJob(job_id="agent_1")],
            )
        )
        store.create_run(WorkflowRun(run_id="wf_a", session_id="session_a", script=""))
        transcript_ref = store.write_agent_transcript(
            run_b,
            run_b.jobs[0],
            [{"type": "metadata", "index": index} for index in range(20)],
        )

        self.assertEqual(["wf_a", "wf_b"], [run.run_id for run in store.list_runs()])
        events = store.read_agent_transcript_events(run_b, transcript_ref, max_events=5)
        self.assertEqual([0, 1, 2, 3, 4], [event["index"] for event in events])
        with self.assertRaises(ValueError):
            store.read_agent_transcript_events(run_b, "../state.json")
```

- [x] **Step 2: Run source contract tests and verify RED**

```powershell
python -m unittest tests.test_workflow_models tests.test_workflow_store tests.test_workflow_runtime -v
```

Expected: the summary helpers and persisted `executionOutcome` are missing, and the concurrent writers can both retain sequence `1`.

- [x] **Step 3: Implement source finalization, bounded reads and atomic journal sequence allocation**

Add pure `summarize_workflow_jobs(jobs)` and `project_workflow_execution_outcome(raw_status, summary)` functions to `workflow_models.py`. The summary contains at least `total`, `succeeded`, `failed`, `cached`, `stale`, `cancelled`, `killed`, `skipped`, `running`, and `terminal`; `registered`/`queued`/`running` contribute to `running`, all other known job statuses contribute to `terminal`. For a terminal raw status, use this exact projection:

```python
def project_workflow_execution_outcome(raw_status, summary):
    if raw_status == "failed":
        return "failed"
    if raw_status in {"succeeded", "completed"}:
        success_like = summary["succeeded"] + summary["cached"]
        return "succeeded" if success_like == summary["total"] else "partial"
    return None
```

Add one runtime finalization helper that refreshes `run.metadata["childSummary"]` and `run.metadata["executionOutcome"]` before every terminal `save_run()`, `write_workflow_progress()` and `write_final_result()` call. If the projection returns `None` for `killed`/`cancelled`/`interrupted`, remove any stale `executionOutcome` key rather than carrying a previous value forward. Include both fields, when present, in `_final_payload()` and top-level `workflow-progress.json`. The worker `done` path keeps raw `run.status="succeeded"`; a script/runtime/Test Gate failure keeps raw `failed`; handled child failure is represented by `executionOutcome="partial"` and later by the common adapter. `AgentScheduler._update_run_completion_state()` uses the same helpers when `manage_run_completion=True`, but `WorkflowRuntime` continues constructing it with `False`.

Add `WorkflowStore.list_runs()` using the existing validated `root.glob("*/workflows/*/state.json")` layout, loading each row through `load_run()` and sorting by `run_id`. Add public `read_agent_transcript_events(run, transcript_ref, *, max_bytes=64_000, max_events=256)` that rejects absolute/escaping refs, reads no more than the byte/event limits, skips malformed JSONL rows, and returns sanitized dictionaries; change internal progress extraction to use this bounded method.

Move journal sequence authority into `WorkflowStore.append_event()`. Use `cross_process_lock(artifact_dir / ".journal.lock")`; while holding it, read the current journal, compute `current_max`, and replace `event.sequence` with `current_max + 1` whenever the caller supplied `sequence <= current_max`. An explicit forward sequence such as `9` in an empty journal remains `9`. Append and return the final event while still holding the lock. `WorkflowRuntime`, `AgentScheduler`, `WorkflowController`, `append_permission_event()` and `project_resume_state()` must pass `sequence=0` instead of replaying and preallocating `max+1`; remove their private `_next_sequence` helpers. Do not create another durable journal.

- [x] **Step 4: Run source suites and commit the source contract**

```powershell
python -m unittest tests.test_workflow_models tests.test_workflow_store tests.test_workflow_scheduler tests.test_workflow_controller tests.test_workflow_runtime -v
```

Expected: all pass; journal sequences are unique across two `WorkflowStore` instances, raw runtime success remains compatible, and handled child failure persists `executionOutcome=partial`.

```powershell
git add workflow_models.py workflow_store.py workflow_runtime.py workflow_scheduler.py workflow_controller.py tests/test_workflow_models.py tests/test_workflow_store.py tests/test_workflow_scheduler.py tests/test_workflow_runtime.py
git commit -m "fix(workflow): finalize child outcomes and serialize journal"
```

- [x] **Step 5: Write the failing workflow projection tests**

Use `TemporaryDirectory`, `WorkflowStore`, `WorkflowRun`, `WorkflowJob`, and `AgentResult` to create two runs with the same `agent_1` job id. Add these tests with concrete assertions:

```python
class WorkflowAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkflowStore(Path(self.tmp.name) / "sessions")
        self.controller = WorkflowController(store=self.store)

    def make_run(self, run_id, jobs, status="running"):
        run = WorkflowRun(run_id=run_id, session_id="session_p2_1", script="return {}", status=status, jobs=jobs)
        return self.store.create_run(run)

    def test_workflow_adapter_exposes_run_container_and_child_records(self):
        self.make_run("wf_a", [WorkflowJob(job_id="agent_1", status="running")])
        records = WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
        self.assertEqual({"workflow_run", "workflow_child"}, {record.record_kind for record in records})
        child = next(record for record in records if record.record_kind == "workflow_child")
        self.assertEqual(make_workflow_run_execution_id("wf_a"), child.parent_execution_id)

    def test_same_job_id_in_two_runs_has_two_execution_ids(self):
        for run_id in ("wf_a", "wf_b"):
            self.make_run(run_id, [WorkflowJob(job_id="agent_1", status="queued")])
        children = [record for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True) if record.record_kind == "workflow_child"]
        expected = {make_workflow_child_execution_id("wf_a", "agent_1"), make_workflow_child_execution_id("wf_b", "agent_1")}
        self.assertEqual(expected, {record.execution_id for record in children})

    def test_cached_child_keeps_cache_source_without_fake_attempt_id(self):
        job = WorkflowJob(
            job_id="agent_1",
            status="cached",
            metadata={
                "cachedFromRunId": "wf_source",
                "cachedFromJobId": "agent_1",
            },
        )
        self.make_run("wf_resumed", [job], status="succeeded")
        children = [record for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True) if record.record_kind == "workflow_child"]
        self.assertEqual(1, len(children))
        child = children[0]
        self.assertEqual(make_workflow_child_execution_id("wf_resumed", "agent_1"), child.execution_id)
        self.assertEqual("cached", child.status)
        self.assertTrue(child.cached)
        self.assertEqual("wf_source", child.metadata["cachedFromRunId"])
        self.assertEqual("agent_1", child.metadata["cachedFromJobId"])
        self.assertIsNone(child.logical_key)
        self.assertIsNone(child.attempt_id)
        self.assertIsNone(child.attempt_index)
        self.assertIsNone(child.attempt_count)

    def test_succeeded_run_with_failed_child_projects_to_partial(self):
        self.make_run(
            "wf_partial",
            [WorkflowJob(job_id="agent_1", status="succeeded"), WorkflowJob(job_id="agent_2", status="failed")],
            status="succeeded",
        )
        records = WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
        run_record = next(record for record in records if record.record_kind == "workflow_run")
        self.assertEqual("succeeded", run_record.source_status)
        self.assertEqual("partial", run_record.status)
        self.assertEqual(1, run_record.metadata["childSummary"]["failed"])
        self.assertEqual("partial", run_record.metadata["executionOutcome"])

    def test_workflow_job_preserves_cached_stale_failed_and_cancelled(self):
        jobs = [WorkflowJob(job_id=f"agent_{index}", status=status) for index, status in enumerate(("cached", "stale", "failed", "cancelled"), start=1)]
        self.make_run("wf_status", jobs)
        children = [record for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True) if record.record_kind == "workflow_child"]
        expected = {"cached", "stale", "failed", "cancelled"}
        self.assertEqual(expected, {record.status for record in children})
        self.assertEqual(expected, {record.source_status for record in children})

    def test_workflow_child_record_carries_workspace_permission_transcript_and_result_ref(self):
        workspace = str(Path(self.tmp.name) / "workspace")
        Path(workspace).mkdir()
        job = WorkflowJob(job_id="agent_1", status="succeeded", result_ref="agents/agent_1/result.json", metadata={"workspacePath": workspace, "permissionProfile": "read_only", "permissionPolicyVersion": "read-only-v1", "transcriptRef": "agents/agent_1/transcript.jsonl"})
        run = self.make_run("wf_meta", [job])
        self.store.write_agent_result(run, job, AgentResult(job_id="agent_1", payload={"summary": "done"}, transcript_ref="agents/agent_1/transcript.jsonl"))
        self.store.write_agent_transcript(run, job, [{"type": "capability_snapshot", "capabilities": {"fileReadAvailable": True}}])
        child = next(record for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True) if record.record_kind == "workflow_child")
        self.assertEqual(workspace, child.workspace)
        self.assertEqual("read_only", child.permission_profile)
        self.assertEqual("read-only-v1", child.permission_policy_version)
        self.assertEqual("agents/agent_1/transcript.jsonl", child.transcript_ref)
        self.assertIn("agents/agent_1/result.json", {ref.ref for ref in child.artifact_refs})

    def test_workflow_journal_events_use_per_run_source_cursor_and_stable_id(self):
        run = self.make_run("wf_events", [WorkflowJob(job_id="agent_1", status="succeeded")])
        self.store.append_event(run, WorkflowEvent(run_id=run.run_id, event_type="agent_completed", sequence=9, job_id="agent_1", payload={"resultRef": "agents/agent_1/result.json"}))
        events = WorkflowChildAdapter(self.store, self.controller).events_since({"workflow:wf_events": 8})
        self.assertEqual(["workflow:wf_events:9"], [event.event_id for event in events.events])
        self.assertEqual("workflow:wf_events", events.events[0].source_cursor)
        self.assertEqual(9, events.next_cursors["workflow:wf_events"])

    def test_workflow_child_does_not_advertise_process_only_actions(self):
        self.make_run("wf_caps", [WorkflowJob(job_id="agent_1", status="running")])
        child = next(record for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True) if record.record_kind == "workflow_child")
        for action in ("read", "events", "result", "artifacts"):
            self.assertTrue(child.capabilities.supports_action(action), action)
        for action in ("cancel", "close", "message", "followup", "resume", "attach", "detach"):
            self.assertFalse(child.capabilities.supports_action(action), action)
```

- [x] **Step 6: Run the workflow adapter tests and verify RED**

```powershell
python -m unittest tests.test_agent_control_workflow -v
```

Expected: failures for the missing adapter and common projection methods. The source contract tests from Step 4 remain green.

- [x] **Step 7: Implement `WorkflowChildAdapter`**

Expose a `workflow-run:<encoded-run-id>` container record and `workflow-child:<encoded-run-id>:<encoded-job-id>` child records through the factories from §2.1. The run record carries run status, artifact directory, workspace metadata, permission profile and actions `read`, `events`, `result`, `artifacts`, plus `stop` or `cancel` only when the current `WorkflowController` status precondition allows that action. It does not advertise common `resume`; the existing bridge resume needs a new run and runtime arguments. A child record carries `job.status`, `result_ref`, `transcript_ref`, `workspacePath`, permission metadata and the bounded capability snapshot extracted from its persisted transcript.

Convert `WorkflowEvent.to_dict()` rows with `AgentEvent.from_workflow_event()`. A journal event with `jobId` maps to that child; a run-level event without `jobId` maps to the run container. Copy `logicalKey`, `attemptId`, `attemptIndex` and `attemptCount` only when those exact concepts are explicitly persisted by the GA source; never derive them from `job_id`, `agentId`, call index or transcript path. Under the current runtime these fields remain `None`. A cached child keeps `status/source_status=cached`, `cached=True`, `cachedFromRunId` and `cachedFromJobId`; it is not assigned a fake attempt. `stale`, `failed`, `cancelled` and `killed` also remain distinct. Do not use `NativeGPTChildAgentRunner._states` as durable truth. Read `capability_snapshot` only from the bounded persisted transcript reader; if no snapshot exists, return `{}` rather than infer tools from the common action set.

Before projecting a workflow run, recompute current child state with `summarize_workflow_jobs()` and `project_workflow_execution_outcome()` rather than blindly trusting a stale persisted summary. Put both values in bounded record metadata. A raw GA `succeeded` run with any failed/cancelled/killed/stale/non-terminal child projects to common `partial`; raw `failed` remains `failed`; only raw `succeeded` with all children in `succeeded`/`cached` projects to common `succeeded`. Preserve raw status in `source_status`. `killed`, `cancelled` and `interrupted` retain their raw common status regardless of already-completed siblings.

For child control, return `unsupported_capability` for `message`, `followup`, `attach`, `detach`, `resume`, `close`, `cancel`, and `stop`. Only the workflow run record may route `stop` to `WorkflowController.stop()` and `cancel` to `WorkflowController.cancel()` when the source status allows it. The adapter must never stop a whole run in response to a child action.

- [x] **Step 8: Run workflow and adapter suites**

```powershell
python -m unittest tests.test_agent_control_workflow tests.test_workflow_models tests.test_workflow_store tests.test_workflow_scheduler tests.test_workflow_runtime -v
```

Expected: all pass; existing `WorkflowRun`/`WorkflowJob` JSON keys remain compatible, `partial` exists only in the common projection/`executionOutcome`, and no GA child receives fabricated retry identity.

- [x] **Step 9: Refactor and commit the adapter**

Keep `WorkflowStore` as workflow artifact source of truth. The adapter returns references and bounded previews, never inlines a complete transcript into a common event.

```powershell
git add agent_control_workflow.py tests/test_agent_control_workflow.py
git commit -m "feat(agent-control): project workflow runs and children"
```

### Task 4: Introduce `UnifiedAgentControl`

**Files:**
- Create: `agent_control.py`
- Test: `tests/test_agent_control.py`

- [x] **Step 1: Write the failing facade tests**

Add `UnifiedAgentControlTest` with a fake adapter whose `control()` increments `control_calls` and whose `list_records()` can raise a provider-independent `RuntimeError`. The tests must assert routing and cursor behavior without touching disk:

```python
def test_list_records_merges_engines_and_sorts_by_execution_id(self):
    process = FakeAdapter("process", [record("process-agent:one")])
    workflow = FakeAdapter("workflow", [record("workflow-child:wf_a:agent_1")])
    control = UnifiedAgentControl([workflow, process])
    self.assertEqual(["process-agent:one", "workflow-child:wf_a:agent_1"], [item.execution_id for item in control.list_records(include_terminal=True)])

def test_get_routes_by_opaque_execution_id_without_assuming_run_equals_job(self):
    expected = record("workflow-child:wf_a:agent_1", record_kind="workflow_child", run_id="wf_a", job_id="agent_1")
    workflow = FakeAdapter("workflow", [expected])
    control = UnifiedAgentControl([workflow])
    self.assertIs(expected, control.get_record(expected.execution_id))
    self.assertIsNone(control.get_record("agent_1"))

def test_events_since_uses_per_source_cursors_and_deduplicates_event_id(self):
    process_event = event("process:evt_1", "process", "process-agent:one", "process", 4)
    workflow_a_event = event("workflow:wf_a:4", "workflow", "workflow-child:wf_a:agent_1", "workflow:wf_a", 4)
    workflow_b_event = event("workflow:wf_b:4", "workflow", "workflow-child:wf_b:agent_1", "workflow:wf_b", 4)
    duplicate = event("workflow:wf_a:4", "workflow", "workflow-run:wf_b", "workflow:wf_b", 8)
    control = UnifiedAgentControl([
        FakeAdapter("process", [], AgentEventBatch((process_event,), {"process": 4})),
        FakeAdapter("workflow", [], AgentEventBatch((workflow_a_event, workflow_b_event, duplicate), {"workflow:wf_a": 4, "workflow:wf_b": 8})),
    ])
    batch = control.events_since({"process": 3, "workflow:wf_a": 3, "workflow:wf_b": 3})
    self.assertEqual(["process:evt_1", "workflow:wf_a:4", "workflow:wf_b:4"], [item.event_id for item in batch.events])
    self.assertEqual(4, batch.next_cursors["process"])
    self.assertEqual(8, batch.next_cursors["workflow:wf_b"])

def test_control_routes_only_to_the_owning_adapter(self):
    process = FakeAdapter("process", [record("process-agent:one")])
    workflow = FakeAdapter("workflow", [record("workflow-run:wf_a", record_kind="workflow_run", run_id="wf_a")])
    control = UnifiedAgentControl([process, workflow])
    result = control.control("process-agent:one", ControlRequest(action="interrupt", reason="test"))
    self.assertTrue(result.ok)
    self.assertEqual(1, process.control_calls)
    self.assertEqual(0, workflow.control_calls)

def test_unsupported_workflow_child_action_is_structured_and_side_effect_free(self):
    child = record("workflow-child:wf_a:agent_1", record_kind="workflow_child", run_id="wf_a", job_id="agent_1")
    workflow = FakeAdapter("workflow", [child])
    control = UnifiedAgentControl([workflow])
    result = control.control(child.execution_id, ControlRequest(action="cancel"))
    self.assertFalse(result.ok)
    self.assertEqual("unsupported_capability", result.code)
    self.assertEqual(0, workflow.control_calls)

def test_adapter_failure_is_redacted_and_does_not_hide_other_engine_records(self):
    good = FakeAdapter("process", [record("process-agent:one")])
    bad = FakeAdapter("workflow", [], list_error=RuntimeError("Bearer workflow-secret-should-not-leak"))
    control = UnifiedAgentControl([good, bad])
    self.assertEqual(["process-agent:one"], [item.execution_id for item in control.list_records(include_terminal=True)])
    self.assertNotIn("workflow-secret", control.last_errors["workflow"])
```

The test module defines `record()` with a common `read/result/events` capability set, `event()` with a concrete `AgentEvent`, and `FakeAdapter` implementing every protocol method by returning its configured value. This keeps the RED phase deterministic and makes an adapter call observable.

- [x] **Step 2: Run the facade tests and verify RED**

```powershell
python -m unittest tests.test_agent_control -v
```

Expected: missing facade/protocol failures.

- [x] **Step 3: Implement the protocol and facade**

Use this minimal caller-facing surface:

```python
class AgentControlAdapter(Protocol):
    engine: str
    def list_records(self, *, include_terminal=False, path_prefix=None) -> list[AgentRecord]:
        raise NotImplementedError
    def get_record(self, execution_id: str) -> AgentRecord | None:
        raise NotImplementedError
    def events_since(self, cursors: dict[str, int] | None = None, *, execution_id: str | None = None) -> AgentEventBatch:
        raise NotImplementedError
    def read_result(self, execution_id: str, *, include_preview=False) -> AgentResultRecord:
        raise NotImplementedError
    def control(self, execution_id: str, request: ControlRequest) -> ControlResult:
        raise NotImplementedError

class UnifiedAgentControl:
    def list_records(self, *, include_terminal=False, engine=None) -> list[AgentRecord]:
        raise NotImplementedError
    def get_record(self, execution_id: str) -> AgentRecord | None:
        raise NotImplementedError
    def events_since(self, cursors: dict[str, int] | None = None, *, execution_id: str | None = None) -> AgentEventBatch:
        raise NotImplementedError
    def read_result(self, execution_id: str, *, include_preview=False) -> AgentResultRecord:
        raise NotImplementedError
    def control(self, execution_id: str, request: ControlRequest) -> ControlResult:
        raise NotImplementedError
```

The facade builds a fresh route index on `list_records`, then uses the opaque id for all subsequent calls. `events_since()` accepts mappings such as `{ "process": 4, "workflow:wf_a": 9, "workflow:wf_b": 3 }`, discovers workflow source keys from the adapter's records, returns the next source cursors, and de-duplicates event IDs. A child filter still advances its containing workflow run cursor because the journal is run-scoped. It does not create a second durable journal in this phase; existing JSONL/journal files remain authoritative. `last_errors` contains redacted adapter read errors from the most recent call and is never allowed to abort healthy engine records.

- [x] **Step 4: Run the facade and all focused Python suites**

```powershell
python -m unittest tests.test_agent_control tests.test_agent_control_process tests.test_agent_control_workflow tests.test_agent_runtime_models -v
```

Expected: all pass; the facade must not change any existing raw process/workflow file.

- [x] **Step 5: Commit the read-model facade**

```powershell
git add agent_control.py tests/test_agent_control.py
git commit -m "feat(agent-control): unify process and workflow read models"
```

### Task 5: Publish a common bridge read model without removing legacy events

**Files:**
- Modify: `frontends/ink_bridge.py`
- Modify: `frontends/ink-ui/src/protocol.ts`
- Modify: `frontends/ink-ui/src/state.ts`
- Modify: `frontends/ink-ui/src/workflowStatusBar.ts`
- Test: `tests/test_ink_bridge.py`
- Test: `frontends/ink-ui/src/state.test.ts`
- Test: `frontends/ink-ui/src/workflowStatusBar.test.ts`

- [x] **Step 1: Write the failing bridge/reducer tests**

Add these TypeScript types before writing reducer tests:

```typescript
export type AgentCapabilities = { actions: string[]; features: string[] }
export type AgentRecord = {
  executionId: string
  engine: 'process' | 'workflow' | string
  recordKind: 'process_agent' | 'workflow_run' | 'workflow_child' | string
  status: string
  sourceStatus?: string | null
  runId?: string | null
  jobId?: string | null
  logicalKey?: string | null
  attemptId?: string | null
  attemptIndex?: number | null
  attemptCount?: number | null
  cached?: boolean
  parentExecutionId?: string | null
  agentPath?: string | null
  workspace?: string | null
  permissionProfile?: string | null
  artifactRefs?: Array<Record<string, unknown>>
  transcriptRef?: string | null
  capabilities: AgentCapabilities
  metadata?: Record<string, unknown>
}
export type AgentEvent = { eventId: string; engine: string; executionId: string; sourceCursor: string; sourceSequence: number; logicalKey?: string | null; attemptId?: string | null; attemptIndex?: number | null; type: string; payload?: Record<string, unknown> }
export type AgentSnapshot = { records: AgentRecord[]; cursors: Record<string, number>; errors: Record<string, string> }
// Add to BridgeEvent without removing any existing union member:
// | { type: 'agent_snapshot'; snapshot: AgentSnapshot }
// | { type: 'agent_event'; event: AgentEvent }
```

Python tests must assert that a bridge can emit a common process record and a common workflow run/job record in the same event stream, while still emitting the existing `workflow_event` payload. TypeScript tests must assert:

```text
agent_snapshot(process + workflow) -> state.agents contains both engines
agent_event(workflow child completed) -> state.agentEvents appends once by eventId
duplicate agent_event -> no duplicate state entry
workflow_detail/progress remains available after common event processing
workflow status bar uses common status when a record exists and old workflow progress as fallback
raw workflow succeeded + common partial -> status bar displays partial without rewriting legacy workflow state
```

- [x] **Step 2: Run the focused tests and verify RED**

```powershell
python -m unittest tests.test_ink_bridge.GenericAgentBridgeTest -v
npm test -- --test-name-pattern="agent|workflow"
```

Run the second command in `frontends/ink-ui`. Expected: new common-event tests fail while existing workflow tests pass.

- [x] **Step 3: Implement additive bridge events**

Inject `UnifiedAgentControl` into `GenericAgentBridge` as an optional constructor dependency **after** `workflow_store` is constructed. The default construction creates `SubagentManager(root_dir=PROJECT_DIR, python_executable=sys.executable)` only for reads and combines it with `WorkflowChildAdapter(self.workflow_store, self.workflow_controller)`; it must not spawn, close, or attach an agent during bridge construction. Tests inject a fake facade. Add `_emit_agent_snapshot()` and `_emit_agent_events()` with `agent_cursors: dict[str, int]`, preserving separate keys for `process` and each `make_workflow_source_cursor(run_id)`. Call them at workflow start/progress/final and at `workflow_list`, `workflow_detail`, and `workflow_progress` refresh points.

Add `agent_snapshot` and `agent_event` to `BridgeEvent`. `agent_snapshot` contains `{records, cursors, errors}`; `agent_event` contains one serialized common event. Every payload passes the existing `sanitize()` emitter and common records omit prompt/task text, transcript bodies, full result payloads and raw API metadata. Keep `workflow_run`, `workflow_event`, `workflow_progress`, and `workflow_final` unchanged so older clients can roll back by ignoring common event types. Store `agents`, `agentEvents`, and `agentCursors` in `AppState`; de-duplicate common events by `eventId` and retain at most the newest 1,000 events. Keep workflow-specific detail/progress because it contains phase, tool, skill and test-gate information that the common model intentionally does not flatten.

`workflowStatusBarFromState()` first finds the common `workflow_run` record by `runId` and uses its projected status, including `partial`; when no common record exists it uses the old `WorkflowRun.status`. It still reads child labels, token usage, phase and test-gate details from the existing workflow progress state. No new bridge command or UI mutation is introduced in Phase C.

- [x] **Step 4: Run Python and TypeScript UI tests**

```powershell
python -m unittest tests.test_ink_bridge -v
npm test
npm run typecheck
```

Expected: all existing and new tests pass. No test should require a live LLM or an interactive terminal.

- [x] **Step 5: Commit additive UI projection**

```powershell
git add frontends/ink_bridge.py frontends/ink-ui/src/protocol.ts frontends/ink-ui/src/state.ts frontends/ink-ui/src/workflowStatusBar.ts tests/test_ink_bridge.py frontends/ink-ui/src/state.test.ts frontends/ink-ui/src/workflowStatusBar.test.ts
git commit -m "feat(ink): expose unified agent read model"
```

### Task 6: Add capability-aware control actions

**Files:**
- Modify: `agent_control.py`
- Modify: `agent_control_process.py`
- Modify: `agent_control_workflow.py`
- Modify: `frontends/ink_bridge.py`
- Test: `tests/test_agent_control.py`
- Test: `tests/test_ink_bridge.py`

- [x] **Step 1: Write failing action tests**

Add these focused tests to `tests/test_agent_control.py`; use recording fakes for process manager and workflow controller, and assert the original objects after each unsupported child call:

```python
def test_process_interrupt_is_forwarded_with_reason(self):
    manager = RecordingProcessManager([make_process_record("process-agent:one")])
    control = UnifiedAgentControl([ProcessSubagentAdapter(manager)])
    result = control.control("process-agent:one", ControlRequest(action="interrupt", reason="user stop"))
    self.assertTrue(result.ok)
    self.assertEqual(("/root/one", "user stop"), manager.interrupt_calls[0])
    self.assertEqual("execution", result.scope)

def test_process_close_cascade_returns_closed_descendants(self):
    manager = RecordingProcessManager([make_process_record("process-agent:parent")])
    control = UnifiedAgentControl([ProcessSubagentAdapter(manager)])
    result = control.control("process-agent:parent", ControlRequest(action="close", payload={"cascade": True}))
    self.assertTrue(result.ok)
    self.assertTrue(manager.close_calls[0][1])
    self.assertEqual("agent_tree", result.scope)
    self.assertEqual(["process-agent:child"], result.data["closedDescendantExecutionIds"])

def test_workflow_run_stop_is_scoped_to_the_run_record(self):
    store, controller, run = recording_running_workflow("wf_a", child_count=2)
    control = UnifiedAgentControl([WorkflowChildAdapter(store, controller)])
    result = control.control(make_workflow_run_execution_id(run.run_id), ControlRequest(action="stop", reason="test stop"))
    self.assertTrue(result.ok)
    self.assertEqual([run.run_id], controller.stop_calls)
    self.assertEqual("workflow_run", result.scope)
    self.assertEqual("killed", store.load_run(run.run_id).status)

def test_workflow_child_cancel_returns_unsupported_without_cancelling_siblings(self):
    store, controller, run = recording_running_workflow("wf_b", child_count=2)
    control = UnifiedAgentControl([WorkflowChildAdapter(store, controller)])
    child_id = make_workflow_child_execution_id(run.run_id, "agent_1")
    result = control.control(child_id, ControlRequest(action="cancel"))
    self.assertFalse(result.ok)
    self.assertEqual("unsupported_capability", result.code)
    self.assertEqual("running", store.load_run(run.run_id).status)
    self.assertEqual(["running", "running"], [job.status for job in store.load_run(run.run_id).jobs])
    self.assertEqual([], controller.stop_calls)

def test_workflow_child_resume_returns_unsupported_not_cached_resume(self):
    store, controller, run = recording_running_workflow("wf_c", child_count=1)
    control = UnifiedAgentControl([WorkflowChildAdapter(store, controller)])
    result = control.control(make_workflow_child_execution_id(run.run_id, "agent_1"), ControlRequest(action="resume"))
    self.assertFalse(result.ok)
    self.assertEqual("unsupported_capability", result.code)
    self.assertEqual("running", store.load_run(run.run_id).status)

def test_control_result_contains_capability_and_scope_for_ui(self):
    record = make_process_record("process-agent:one")
    result = ControlResult(ok=False, code="unsupported_capability", execution_id=record.execution_id, scope="execution", status=record.status, data={"requestedAction": "cancel", "capabilities": sorted(record.capabilities.actions)})
    self.assertEqual("execution", result.scope)
    self.assertIn("interrupt", result.data["capabilities"])
```

The test helpers create an in-memory recording manager/controller backed by a temporary `WorkflowStore`; `RecordingProcessManager.close_agent()` returns one closed descendant whose execution id is `process-agent:child`. No helper may call an LLM or a real process.

- [x] **Step 2: Run the action tests and verify RED**

```powershell
python -m unittest tests.test_agent_control tests.test_ink_bridge -v
```

Expected: action routing or capability assertions fail; raw `SubagentManager` and `WorkflowController` tests remain green.

- [x] **Step 3: Implement explicit action routing**

Define these immutable request/result models:

```python
@dataclass(frozen=True)
class ControlRequest:
    action: str
    reason: str = ""
    payload: dict = field(default_factory=dict)

@dataclass(frozen=True)
class ControlResult:
    ok: bool
    code: str
    execution_id: str
    scope: str = "execution"
    status: str | None = None
    message: str = ""
    data: dict = field(default_factory=dict)
```

The facade first checks `record.capabilities.supports_action(request.action)`; if absent, return `code="unsupported_capability"` before calling an adapter. The result includes `requestedAction` and the sorted supported action names so UI can explain the boundary. For process actions call the existing manager methods. For a workflow run call `WorkflowController.stop()` or `cancel()` only when the current source status satisfies the existing controller precondition. For a workflow child reject `cancel`, `stop`, `close`, `resume`, and messaging rather than widening scope. Adapter exceptions become `code="control_error"` with `redact_sensitive_text(str(exc))` in `message`; no raw traceback is emitted.

Expose only these actions to UI command handlers. Keep `ga.py`'s existing process subagent tools unchanged in this task; adding a new facade must not silently change their target names or mailbox semantics.

- [x] **Step 4: Run focused control and regression suites**

```powershell
python -m unittest tests.test_agent_control tests.test_agent_control_process tests.test_agent_control_workflow tests.test_ga_subagent_tools tests.test_workflow_controller tests.test_workflow_runtime -v
```

Expected: all pass; a denied/unsupported child action leaves every sibling job and the workflow run unchanged.

- [x] **Step 5: Commit capability-aware controls**

```powershell
git add agent_control.py agent_control_process.py agent_control_workflow.py frontends/ink_bridge.py tests/test_agent_control.py tests/test_ink_bridge.py
git commit -m "feat(agent-control): make lifecycle actions capability-aware"
```

### Task 7: Add opt-in real `gpt-5.6-luna` cross-engine E2E

**Files:**
- Create: `tests/real_p2_1_agent_control_e2e.py`
- Reference: `tests/real_workflow_p0_e2e.py`, `tests/real_subagent_s12_e2e.py`, `llm.yaml`

- [x] **Step 1: Write the opt-in harness and its skip test**

The harness must be skipped unless `GA_RUN_REAL_P2_1_E2E=1`. In the current `llm.yaml`, the profile name is `luna` and its model id is `gpt-5.6-luna`; keep these constants separate:

```python
TARGET_PROFILE = "luna"
EXPECTED_MODEL = "gpt-5.6-luna"
OPT_IN = os.environ.get("GA_RUN_REAL_P2_1_E2E") == "1"
```

Resolve `binding_from_profile(TARGET_PROFILE)` and assert `binding.model_id == EXPECTED_MODEL` before constructing either executable. Resolve the process `llm_no` by enumerating `load_clients_from_yaml(start_dir=REPO)` and matching both `backend.name == TARGET_PROFILE` and `backend.model == EXPECTED_MODEL`; never use an implicit active profile or `llm_no=0` as a substitute for the target model. The unittest entry point is skipped without opt-in and the direct script entry point prints `skipped: true` without creating a runner or making a network request.

- [x] **Step 2: Run the harness without opt-in**

```powershell
python -m unittest tests.real_p2_1_agent_control_e2e -v
```

Expected: the test is skipped and performs no network request.

- [x] **Step 3: Implement the real cross-engine scenario**

Use `TemporaryDirectory` for both the process root and workflow store, and create one workflow workspace directory under that root. The process prompt and workflow child prompt must both say “不要调用工具，只输出这一行精确文本：`GA_P2_1_LUNA_OK_20260806`”. Start one process subagent with `SubagentManager(root_dir=process_root)` and the resolved `llm_no`; wait until its durable state has `turn_status == "completed"` and assert its output file contains the marker. Its common `workspace` is the process `task_dir` (or a real worktree when one exists), so the test asserts presence and source metadata, not equality with the workflow workspace. Start one workflow run with a one-agent script and `NativeGPTChildAgentRunner(profile_name=TARGET_PROFILE, enable_tools=False)`, then wait for `WorkflowRuntime.run()` to return `succeeded`. Do not run the two provider calls concurrently; this keeps a failure attributable and avoids accidental rate-limit coupling.

Construct `UnifiedAgentControl([ProcessSubagentAdapter(manager), WorkflowChildAdapter(store, controller)])` after both durable records exist, then query records, events and results. Assert:

```text
process record, workflow run record and workflow child record all exist
engines are exactly {process, workflow}
execution_id values are distinct
process and workflow child status projections are succeeded; workflow run status is succeeded
all three records retain their engine-specific workspace/permission metadata
process and workflow child results expose artifact/transcript references
process events and workflow journal events have non-colliding event ids
workflow child capabilities do not include mailbox/resume
process capabilities include result/read and the existing lifecycle actions
```

The summary may print target profile/model, projected/source statuses, record kinds, event counts, capability names and artifact basenames only. It must not print prompts, marker transcripts, full result payloads, API keys, or full workspace paths. Keep the process alive until the common success assertions complete, then call `manager.close_agent(task_name, reason="p2_1_e2e_cleanup", grace_s=2.0)` in `finally`; the explicit temporary root cleanup then removes only the test-owned directory.

- [x] **Step 4: Run the real scenario with the required model**

```powershell
$env:GA_RUN_REAL_P2_1_E2E = "1"
python tests/real_p2_1_agent_control_e2e.py
```

Expected: JSON summary reports `passed: true`, `profile: luna`, `model: gpt-5.6-luna`, one process record, one workflow run record, one workflow child record, and no capability, cursor or artifact mismatch. Provider/network errors remain test failures with redacted diagnostics; they must not be converted into a false pass.

- [x] **Step 5: Commit the opt-in E2E**

```powershell
git add tests/real_p2_1_agent_control_e2e.py
git commit -m "test(agent-control): add opt-in luna cross-engine E2E"
```

### Task 8: Full regression, documentation and handoff

**Files:**
- Modify: `docs/workflow_defect_2026_08_05.md`
- Modify only if needed for test inventory: `docs/ga_subagent_v2_optimization_design_2026-07-27.md`

- [x] **Step 1: Run deterministic backend regression**

```powershell
python -m unittest discover -s tests
```

Expected: zero failures. Record the actual test count and skips in the defect document; do not copy the old 904-test count if the new tests change it.

Also run the Phase A/B focused command once more and record its output separately from the full suite:

```powershell
python -m unittest tests.test_agent_runtime_models tests.test_subagent_manager tests.test_agent_control_process tests.test_workflow_models tests.test_workflow_store tests.test_workflow_scheduler tests.test_workflow_controller tests.test_workflow_runtime tests.test_agent_control_workflow tests.test_agent_control -v
```

- [x] **Step 2: Run Ink UI regression**

```powershell
npm test
npm run typecheck
```

Run in `D:\git_codes\GenericAgent\frontends\ink-ui`. Expected: zero failures and a clean typecheck.

- [x] **Step 3: Run the opt-in real API verification**

Run the exact opt-in command from Task 7 with profile `luna` and assert model `gpt-5.6-luna`; keep this separate from the deterministic suite so provider instability cannot mask local regressions. The result must include process/workflow record counts, source cursor keys and artifact reference counts.

- [x] **Step 4: Update the defect record**

Add a P2-1 progress section to `docs/workflow_defect_2026_08_05.md` containing:

```text
Phase A model/read commit: common identity/status/event/result model and no-write subagent snapshot seam
Phase A workflow source commit: childSummary/executionOutcome finalization plus per-run journal sequence serialization
Phase A adapter commits: process/workflow read-only projections
Phase B facade/control commits: unified routing, capability-aware controls and scope guarantees
Phase C commit: additive Ink snapshot/event reducer
common identity: opaque execution_id, process_agent/workflow_run/workflow_child record kinds
subagent decision: existing process executor/lifecycle unchanged; adapter uses list_agent_snapshots(), which performs no state/registry writes
workflow identity: current GA uses run_id + job_id and does not fabricate logical_key/attempt fields; cachedFromRunId/cachedFromJobId preserved
common status: raw succeeded plus handled child error -> partial; raw failed -> failed; cached/stale and source_status preserved
child aggregate: bounded childSummary and executionOutcome are present in run metadata, progress and final result
event cursors: process plus `make_workflow_source_cursor(run_id)`; no global workflow integer
journal durability: concurrent writers receive unique per-run sequences under the store lock
per-engine capability differences intentionally retained; workflow `skip`/`retry` are not generic `cancel`/`resume`
deterministic Python and Ink commands/results
real gpt-5.6-luna command/result, with profile luna and model assertion
known non-goals: child mailbox, child-level cancel, independent child resume, physical artifact migration, second durable unified journal
```

Do not mark P2-1 fully closed until Task 7 passes; the read-model phase and the real E2E phase must be distinguishable.

- [x] **Step 5: Review the diff and commit documentation**

```powershell
git diff --check
git status --short
git add docs/workflow_defect_2026_08_05.md
git commit -m "docs(agent-control): record P2-1 verification"
```

Before this documentation commit, verify `git diff --name-only` contains only intended P2-1 files plus the pre-existing `.gitignore` change. Stage the defect document by path; never use `git add -A`. The existing user-owned `.gitignore` modification must remain uncommitted and must not be included in any P2-1 commit.

## 5. 兼容、回滚和失败处理

### 5.1 兼容策略

- Existing process tool payloads and workflow bridge event payloads remain additive-compatible.
- Raw files remain source of truth; the facade can be disabled without reconstructing or migrating data.
- Common event consumers ignore unknown event types; old Ink clients keep receiving the existing workflow events.
- Common records contain references, not full transcript bodies, so output limits and secret redaction remain owned by the original stores.
- `execution_id` is never persisted by overwriting existing `run_id`, `job_id` or `agent_path`; if persisted later, it is an added metadata field only.
- `logical_key` and `attempt_id` remain optional additive metadata. Current GA leaves them unset and preserves resume provenance through `cachedFromRunId`/`cachedFromJobId`; a future same-run retry design must add non-overwriting attempt artifacts before populating them.

### 5.2 回滚策略

1. If the Task 2 adapter fails, remove `ProcessSubagentAdapter`; the additive `list_agent_snapshots()` seam can remain because it has its own no-write test and does not change existing `list_agents()` behavior.
2. Task 3 has two commits. If only the adapter fails, remove `WorkflowChildAdapter` and keep the independently tested finalization/journal fix. If the source contract itself regresses, revert only its dedicated commit; no artifact migration or schema rewrite is required.
3. If bridge/UI projection fails, stop emitting `agent_snapshot`/`agent_event`; keep `workflow_*` events and panels.
4. If control routing is unsafe, disable `UnifiedAgentControl.control()` and leave existing `workflow_stop` and process subagent tools as the only mutating paths.
5. No destructive artifact migration is performed, so reverting adapter/facade commits does not require data rollback.

### 5.3 必须拒绝的错误方案

- 把 `NativeGPTChildAgentRunner` 改成 OS process，只为了让 pid 看起来统一。
- 把 workflow `cached`/run resume 映射为 process `resume_agent`，导致调用方误以为有 child sidechain。
- 把 workflow child `cancel` 映射为 `WorkflowController.stop(run_id)`，导致取消一个 job 误杀 siblings。
- 为了共用 dataclass 删除 `WorkflowJob` 的 `schemaValidation`、`toolSummary`、`capability_snapshot` 或 process 的 mailbox/IPC 字段。
- 把两个 source 的 sequence 直接比较，或把同名 `agent_1` 当成全局 id。
- 在 GA 没有同一 run retry 证据时，照搬 Claude 的 `v2:<hash>`、`agentId` 或 attempt counters 并伪装成持久化事实。
- 把一个 physical `agent-<id>.jsonl` 当成一个永恒 logical child，或把 retry 生成的多个 attempts 展示成多个 logical children。
- 用真实 LLM 输出文本作为 common completion 判据；完成状态必须来自原始状态/event contract。

## 6. 完成判据

P2-1 只有同时满足下列条件才算完成：

1. `SubagentManager.list_agent_snapshots()` 通过 `probe_agent()` 枚举状态，测试证明不写 `state.json` 或 registry；现有 process spawn、wait、mailbox、resume、interrupt/close 和 `list_agents()` 行为不变。
2. `UnifiedAgentControl.list_records()` 能稳定列出 process agent、workflow run 和 workflow child，且 execution id、engine、parent/container relation 不冲突。
3. 当前 GA workflow child 不伪造 `logical_key`/attempt metadata；resume cache 保留 `cachedFromRunId`/`cachedFromJobId`，`cached` 不被伪装为 fresh succeeded。
4. 两个 adapter 都能将原始状态、事件、artifact/transcript/workspace/permission 元数据转换为公共 read model。
5. workflow source finalization 持久化 bounded `childSummary` 和 `executionOutcome`；raw `succeeded` + handled child error 投影为 common `partial`，raw `failed` 仍为 `failed`。
6. `WorkflowStore.append_event()` 在 per-run cross-process lock 内分配唯一 sequence；controller、scheduler 和 runtime 的并发 writer 回归通过，stable event id 不冲突。
7. capability matrix 对 mailbox、resume、cancel、close、workflow-specific `skip`/`retry` 等不等价能力有明确结果；不支持动作没有副作用。
8. Ink bridge 能增量发 common snapshot/event，并显示 `partial`；旧 workflow event/detail/progress 测试保持通过。
9. Python 全量测试、Ink UI 测试和 opt-in `gpt-5.6-luna` 跨引擎 E2E 通过。
10. 缺陷文档记录了实际命令、结果、已保留的边界和未做事项。

P2-1 不要求在本轮完成物理 artifact 目录合并、跨引擎 durable unified journal、workflow child mailbox、独立 child resume 或权限产品重设计；这些是后续独立议题，不能用“接口看起来相同”提前宣称已经实现。
