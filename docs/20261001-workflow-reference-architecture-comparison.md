# GA Dynamic Workflow 参考架构对比与 Codex CLI 调研

- 日期：2026-10-01
- 范围：GenericAgent workflow / verification contract / subagent lifecycle
- 参考项目：
  - `D:\git_codes\Step-Code`
  - `D:\git_codes\ultracode-skill`
  - `D:\git_codes\pi`
  - `D:\git_codes\codex`

## 1. 结论先行

GA 不应只照搬某一个参考项目，也不应把三个项目的规则无层次地混合。推荐采用分层借鉴：

```text
Step-Code       workflow 执行内核
ultracode       验证/评估契约策略
Pi              subagent 生命周期与拓扑适配器
Codex CLI       agent graph、thread/turn 状态、显式协作模式和控制面
```

这不是四套规则叠加，而是让每个参考项目只负责自己最擅长的 module/interface：

- Step-Code 负责 workflow runtime 的实现深度：schema、ACL、DAG、budget、journal、retry、resume。
- ultracode 负责 policy 层：`none / inline / full`、required checks、shared surfaces、evidence。
- Pi 负责子进程 adapter 和 single/parallel/chain 拓扑。
- Codex CLI 负责 thread/turn/agent graph 的生命周期边界、控制面消息和显式协作模式。

当前 GA 的问题不是参考项目太少，而是不同层次的概念被压在同一个 coding validator 中：

```text
coding
  -> 必须有 verification agent
  -> 必须有 verification_schema
  -> 必须有 python_unittest
```

这把角色、证据格式和具体测试命令错误地绑定在一起。

## 2. GA 当前状态与问题

GA 已经通过 `plan_produces_code()` 将“是否产生代码”从纯 `taskType` 判断推进到计划形状判断：

- `role ∈ {implementation, tests, repair}`；
- 或 agent 声明了 `writeScope`；
- `taskType == coding/debugging` 仍作为 fail-closed fallback。

这一步解决了部分 `taskType` 漂移问题，但 coding 契约内部仍然固定要求：

- `role == verification` 的 agent；
- 严格 schema 必须包含 `verificationPassed`、`checks`、`blockingIssues`；
- acceptance 必须同时包含 `python_unittest` 和 `verification_schema`。

相关实现：

- `workflow_planner.py:64-90`：`plan_produces_code()`；
- `workflow_planner.py:976-994`：verification agent 和 strict schema 门禁；
- `workflow_planner.py:1000-1020`：固定 acceptance checks；
- `workflow_runtime.py:638-680`：固定 check 名称的 acceptance 评估。

### 2.1 三种概念不应绑定

| 当前概念 | 实际语义 | 应归属的层次 |
|---|---|---|
| `verification agent` | 执行拓扑/角色选择 | orchestration topology |
| `python_unittest` | Python 项目的一个命令型检查 | verification check adapter |
| `verification_schema` | 结构化证据输出格式 | evidence/schema contract |

更稳妥的 minimum invariant 应该是：

```text
write-capable workflow 必须声明至少一个可观测 verification check；
但不强制具体角色名、具体语言测试框架或固定 schema 名称。
```

## 3. Step-Code 调研结果

### 3.1 通用 workflow 不强制 verification agent

Step-Code 的通用 workflow 通过 `agent(prompt, options)` 组合工作：

- `schema`：结构化输出契约；
- `toolProfile`：工具能力集合；
- `readOnly` / `writable`：路径级 ACL；
- `retries`：schema 失败重试；
- `phase`、label、依赖和并发控制；
- journal、budget、resume 和 progress。

runtime 在 agent 返回后执行 schema 校验；校验失败会把错误反馈给 agent，超过重试次数才终止。agent 的 failed/aborted 状态不会被当成 completed。

因此 Step-Code 的通用 workflow 并没有：

```text
taskType == coding -> 必须创建 verification agent
```

### 3.2 Planner/Developer/QA 是专用 primitive 的语义

Step-Code 的 `iterate()` / HoH 循环确实固定了：

```text
Planner(read-only) -> single-writer Developer -> independent read-only QA
```

但这是 `iterate()` 这个高级 API 的实现契约，不是所有 workflow 的全局门禁。这个区别对 GA 很重要：

- 普通 workflow：拓扑由脚本/计划声明；
- 专用迭代 primitive：API 自己定义固定角色；
- 不应把专用 primitive 的角色要求推广到所有 coding 任务。

### 3.3 Step-Code 对 GA 的主要启发

1. schema 是 agent 输出接口，不等于 verification role；
2. `readOnly/writable` 应在工具调用边界执行，而不是只写 prompt；
3. 预算、并发、依赖、journal、resume 是 runtime 硬约束；
4. 专用工作流可以有固定角色，但固定角色必须由 primitive 语义产生，而不是由模糊的 `taskType` 产生。

## 4. ultracode-skill 调研结果

ultracode 是 skill，不是 runtime。它对 GA 最有价值的是验收策略：

```text
none     小任务、无写入、无跨 packet 消费
inline   普通中等风险任务
full     公共 API/schema、迁移、共享模块、多写者、独立审查
```

其 eval contract 由以下字段组成：

```text
Outcome
Shared surfaces
Required checks
Blocking conditions
Handoff evidence
```

检查项可以是 targeted test、typecheck、lint、build、browser smoke、CLI smoke、manual checklist 或 independent review。`python_unittest` 只是其中一种具体 check。

ultracode 还明确要求：

- 使用能证明结果的最小 workflow；
- required check 必须有 evidence；
- skipped check 必须说明原因；
- independent review 主要用于 high-risk/full contract；
- 不能因为 agent 自己说“完成”就结束。

### 4.1 ultracode 对 GA 的主要启发

GA 应把 verification 建模为：

```json
{
  "verification": {
    "level": "inline",
    "checks": [
      {
        "id": "targeted-tests",
        "kind": "command",
        "required": true,
        "owner": "host"
      },
      {
        "id": "evidence",
        "kind": "schema",
        "required": true,
        "owner": "implementation",
        "schemaRef": "verification_result"
      }
    ],
    "independentReview": false
  }
}
```

高风险任务再升级到 `full`，而不是所有 coding 默认 `full`。

## 5. Pi 调研结果

Pi 的 subagent extension 将拓扑显式建模为：

```text
single
parallel
chain
```

它的主要硬约束不是“coding 必须 reviewer”，而是运行时生命周期：

- 每个 child 是独立进程；
- 结果包含 exit code、stop reason、stderr、usage、model；
- abort 会传播到 child；
- parallel 有任务数和并发上限；
- chain 前一步失败后停止后续步骤；
- 输出展示可以截断，但完整结果保留在 details；
- `implement-and-review` 是显式组合 prompt，而不是所有 coding 自动插入 reviewer。

### 5.1 Pi 对 GA 的主要启发

GA 的 `workflow_child_agent.py` / `subagent_manager.py` 应把以下维度分开：

```text
process_status
turn_status
job_status
acceptance_status
execution_outcome
```

agent 是否退出、turn 是否完成、结果是否落盘、验收是否通过，不能压缩为一个 `succeeded`。

## 6. Codex CLI 调研结果

截至 2026-10-01，Codex CLI 源码中没有发现与 ultracode 同名的通用 dynamic workflow runtime，也没有发现一个把 Planner/Developer/Verifier 作为任意任务统一模板的 workflow engine。它提供的是几个可组合的正交机制：

```text
collaboration mode
multi-agent control plane
thread/turn lifecycle
agent graph store
review task
goal extension
skills / AGENTS.md
```

因此 Codex CLI 并不能替代 Step-Code 的 workflow runtime，但它对 GA 的状态模型、控制面和显式 opt-in 有重要启发。

### 6.1 Collaboration Mode：模式切换，不是任务分类

Codex 的 collaboration mode 主要是 `Default` / `Plan`，通过 world-state fragment 将当前模式说明增量注入上下文。模式变化有快照、hash 和 diff 语义，避免每轮重复注入全部提示。

这说明：

- 模式是 session/turn 的显式状态；
- 不应从任务关键词隐式猜测模式；
- planner、review、implementation 等行为可以由模式或显式工具选择驱动；
- 模式本身不等于 `taskType`。

### 6.2 Multi-Agent V2：显式工具 + 控制面

Codex 的 multi-agent 工具是显式的：

```text
spawn_agent
send_message
followup_task
wait_agent
list_agents
resume_agent
interrupt_agent
close_agent
```

这些工具通过 `LocalAgentControl`、`AgentGraphStore` 和 typed protocol 管理 parent/child 关系。spawn 参数包含：

- `task_name`；
- `agent_type`/role；
- model/reasoning override；
- fork mode；
- parent/root/turn metadata；
- environment snapshot；
- usage hints。

这是一种动态编排能力，但它不是计划式 workflow：Codex 让模型在运行时显式决定何时 spawn、何时等待、何时继续或关闭 agent。

### 6.3 Agent graph：父子关系是持久化实体

`agent-graph-store` 将 parent/child spawn edge 独立持久化，边有：

```text
open
closed
```

同时支持：

- direct children 查询；
- breadth-first descendants 查询；
- 按 edge status 过滤；
- 稳定排序；
- 子 agent 关闭后仍保留图关系。

这直接支持 GA 之前遇到的“list_agents 看不到已关闭 agent，但 read_agent_result 仍能读取结果”问题。GA 应区分：

```text
active registry view
durable result/artifact view
parent-child graph view
```

不能用活跃列表替代持久化结果查询。

### 6.4 Wait semantics：等待活动，不等于读取最终结果

Codex 的 `wait_agent` 只等待 mailbox activity 或新的输入，返回：

```text
wait completed
wait interrupted
wait timed out
```

它不承担读取最终 agent 输出的职责。这与 GA 之前的现象完全一致：

- `wait_agent` 超时不等于 child 失败；
- `wait_agent` 不返回正文不等于 child 没有完成；
- 最终结果需要独立的 result/artifact 查询接口。

因此 GA 的 wait/read/result API 分离是正确方向，不应把 wait 改造成隐式读取最终结果的“大而全”接口。

### 6.5 Review task：独立 reviewer 是专用 task，而不是全局 coding 门禁

Codex 的 review task 会创建受限的子 Codex conversation，并强制：

- 专用 review prompt/rubric；
- 禁用 web search；
- 禁用 collab/multi-agent；
- `AskForApproval::Never`；
- 独立 review model 可选；
- review 输出解析为 `ReviewOutputEvent`；
- review 结束、成功和中断都有独立事件。

这对 GA 的启发是：如果 full contract 需要 independent review，reviewer 不应只是普通 child agent 加上 `role="verification"`。它应该有：

- 独立 capability profile；
- read-only 或受限 writable ACL；
- 独立 prompt/rubric；
- 独立结果 schema；
- 明确的 review terminal state。

但 Codex 的 review 仍然是专用 task。它没有把 reviewer 强制注入所有 coding turn，这进一步支持“reviewer 是契约/拓扑选择，而不是 coding 全局门禁”的结论。

### 6.6 Awaiter：终态约束属于角色协议和 runtime

Codex 内置 `awaiter` role 的开发者指令要求：

- 一直等待到成功、失败或停止；
- 不得把中间状态当完成；
- 不得执行无关动作；
- 使用长 timeout；
- 不能 hallucinate completion。

这不是 workflow planner 的 taskType 判断，而是专用 role 的行为协议，并由 wait/agent status runtime 支撑。GA 可以借鉴为：

```text
long-running task -> awaiter/monitor topology
terminal result -> explicit status transition
```

而不是让主 agent 通过自然语言猜测子任务是否已经结束。

### 6.7 Codex 对当前结论的修正和增强

Codex 没有推翻“分层借鉴”的结论，反而补充了两个重要边界：

1. **dynamic workflow 不一定要先做成一个大的 workflow DSL。** Codex 通过显式 collaboration mode、typed agent tools、thread graph、wait/message/resume/close 已经实现了动态编排的关键控制面。
2. **状态模型比提示词更重要。** Codex 把 thread、turn、spawn edge、agent status、review task、goal status 分开持久化和测试，避免“进程完成 = 业务完成”。

## 7. 对 GA 的最终架构建议

### 7.1 单一职责分层

```text
workflow_planner.py
  只生成声明式 plan 和 verification contract

workflow_policy.py
  根据 declared contract + observed facts 计算门禁和 eval level

workflow_runtime.py
  执行 agent、schema、command、review checks，记录 evidence

workflow_child_agent.py
  负责 child process/turn/abort/exit/usage 生命周期

workflow_models.py
  负责 typed status、job/run、parent-child graph 和 durable references

workflow_controller.py
  负责把各 module 串起来，不重复推断 taskType 或验收语义
```

### 7.2 三层判定模型

GA 应采用：

```text
计划声明（model-produced contract）
        +
宿主观测（tool calls / changed files / exit codes / artifacts）
        +
宿主策略（risk / shared surface / required evidence）
        -> 最终门禁决定
```

任何一层都不能单独决定成功：

- 不能只信 `taskType`；
- 不能只信 agent 自己的 `verificationPassed`；
- 不能只信进程 exit code；
- 不能只信最后自然语言摘要。

### 7.3 推荐的 minimum invariant

```text
无写入、无共享 surface 的任务：verification.level = none 可接受；

普通写入任务：至少一个可观测 check，但不强制 verification agent；

公共 API/schema/迁移/共享模块/多写者：升级为 full；

full contract：至少一个 host-observable check，并按契约要求 independent review；

每个 required check 必须有 passed/failed/skipped/not_applicable 和 evidence。
```

### 7.4 对旧契约的兼容迁移

旧计划可以转换为新契约：

```text
role == verification
  -> independentReview = true

python_unittest
  -> command check(kind=python_unittest)

verification_schema
  -> schema/evidence check(kind=schema)
```

迁移期间保留旧字段并记录 deprecated warning，不要立刻删除，避免历史 workflow artifact 无法恢复。

## 8. 下一步实施顺序

1. 先冻结新的 verification contract 数据模型，去除“角色 + Python 测试 + 固定 schema”三者绑定。
2. 用 Step-Code/Pi 方式补齐 child process、turn、abort、exit、result artifact 和 parent-child graph 的状态边界。
3. 用 ultracode 的 `none/inline/full` 作为 policy 层，不让 planner prompt 直接决定宿主门禁。
4. 将 reviewer 实现为可选 topology/check；full contract 才按契约要求 independent reviewer。
5. 用 Codex 风格的 typed events、terminal state 和协议测试覆盖 wait/list/read/close/resume。
6. 最后再扩展 `python_unittest` 之外的 command adapter，如 pytest、npm test、typecheck、build 和 browser smoke。

## 9. 参考源码索引

### Step-Code

- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\runtime.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\schema.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\tool-profile.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\hoh.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\step-workflow.ts`

### ultracode-skill

- `D:\git_codes\ultracode-skill\ultracode\SKILL.md`
- `D:\git_codes\ultracode-skill\ultracode\references\eval-contracts.md`
- `D:\git_codes\ultracode-skill\ultracode\references\packet-schema.md`

### Pi

- `D:\git_codes\pi\packages\coding-agent\examples\extensions\subagent\README.md`
- `D:\git_codes\pi\packages\coding-agent\examples\extensions\subagent\index.ts`
- `D:\git_codes\pi\packages\coding-agent\examples\extensions\subagent\prompts\implement-and-review.md`

### Codex CLI

- `D:\git_codes\codex\codex-rs\core\src\agent\control\spawn.rs`
- `D:\git_codes\codex\codex-rs\core\src\tools\handlers\multi_agents_v2\spawn.rs`
- `D:\git_codes\codex\codex-rs\core\src\tools\handlers\multi_agents_v2\wait.rs`
- `D:\git_codes\codex\codex-rs\agent-graph-store\src\types.rs`
- `D:\git_codes\codex\codex-rs\agent-graph-store\src\store.rs`
- `D:\git_codes\codex\codex-rs\core\src\tasks\review.rs`
- `D:\git_codes\codex\codex-rs\core\assets\agent\builtins\awaiter.toml`
- `D:\git_codes\codex\codex-rs\core\src\context\world_state\collaboration_mode.rs`
- `D:\git_codes\codex\codex-rs\core\src\context\world_state\multi_agent_mode.rs`
- `D:\git_codes\codex\codex-rs\ext\goal\src\runtime.rs`

## 10. 专项调研：GA 的 wait agent 为什么不够聪明

### 10.1 当前 GA wait 的真实语义

GA 当前 `wait_agent` 的实现位于：

- `ga.py:987-1050`；
- `subagent_manager.py:1215-1325`。

它实际上是一个“等待任意更新”的 watcher：

1. 读取 event bus 中 `since_event_seq` 之后的事件；
2. 读取 parent inbox；
3. probe 每个 target 的 state；
4. 检查 `state_notify` 或 `events.jsonl` 文件大小变化；
5. 有实时 channel 时等待 channel signal，没有时轮询；
6. 第一个变化出现就返回。

这套实现已经比单纯 `sleep` 好，因为它同时支持 event bus、inbox、state probe 和 realtime channel。但它默认等待的是：

```text
“有新事件/状态变化”
```

而不是：

```text
“目标 agent 已达到 terminal state”
“所有目标 agent 都已达到 terminal state”
“最终 result artifact 已可读取”
```

因此真实运行中会出现：

```text
agent_started
turn_started
tool_call
progress update
turn_completed
final_output persisted
process exited
```

每一个阶段都可能唤醒 `wait_agent`。模型得到的只是“changed”，还需要自己判断下一步是否继续 wait、是否读取结果、是否等待其他 agent。对 DeepSeek 这类模型，这会显著增加误判和重复调用概率。

### 10.2 当前实现的几个具体问题

#### 问题一：wait 条件没有显式表达

当前 API 没有明确区分：

```text
wait_for_event
wait_for_turn_terminal
wait_for_process_exit
wait_for_all_targets
wait_for_result_artifact
```

`wait_agent` 统一返回 `changed/timeout`，把不同目的交给模型自行解释。

#### 问题二：第一个中间事件会提前返回

两个子 agent 刚写出 `agent_started` 或 `turn_started` 时，wait 就可能结束。对于“等待两个搜索 agent 完成”的任务，这不是有用的完成信号，只是启动进度。

#### 问题三：等待和读取结果被完全割裂

当前设计明确要求：

```text
wait_agent -> 只等事件
read_agent_result -> 再读最终正文
```

职责分离本身是正确的，但对模型暴露的交互过于低级。wait 返回 completed 后仍只给 `result_hint`，不提供每个 target 的 `result_ref`、artifact 是否存在、下一步动作和失败原因的完整结构。

#### 问题四：默认 target 集合偏向活跃 agent

`targets=None` 时通常从当前 `list_agents()` 获取目标。已经关闭但结果已持久化的 agent 可能不在活跃列表中，导致“等待全部”与“读取全部历史结果”产生语义差异。

#### 问题五：超时结果不够有决策价值

超时会返回 `observed_agents`，但没有明确区分：

```text
仍在运行
已完成但未读结果
进程退出但状态未收敛
已失败
已 stale
```

模型只能再次调用 list/read/probe，容易形成低效轮询。

## 11. 参考项目如何处理等待

### 11.1 Pi：进程级等待 + 显式 barrier

Pi 的单 agent 执行直接等待子进程 `close` 事件，并将最终 `exitCode` 写入结果。它不会把任意 stdout 增量当作完成：

```text
spawn child
  -> stream message/tool updates
  -> wait process close
  -> record exitCode / stopReason / stderr / usage
  -> return SingleResult
```

Pi 的 parallel 使用带并发上限的 worker pool，最终通过 `Promise.all` 形成 barrier；只有所有任务得到 `SingleResult` 后才返回 parallel 结果。chain 则逐步等待上一项，上一项失败时停止后续项。

Pi 的优势不是“更聪明地猜什么时候完成”，而是把完成条件放在 runtime：

- 单任务：进程关闭；
- parallel：所有任务返回结果；
- chain：当前步骤 terminal 且成功后才能进入下一步；
- abort：明确传播并产生 aborted 结果。

### 11.2 Step-Code：workflow 内部 await，模型不需要手工轮询

Step-Code 的 workflow 脚本通过 JavaScript `await agent()`、`parallel()`、`pipeline()` 表达等待。

```ts
const result = await agent(prompt, options)
const results = await parallel(tasks)
```

它的 runtime 负责：

- agent started/running/completed/failed/aborted 状态；
- agent timeout 和 parent abort；
- schema retry；
- budget consume；
- journal append；
- progress update；
- 最终 workflow status；
- journal/progress flush。

因此在正常 workflow 中，模型无需反复调用 `wait_agent`。等待是 workflow implementation 的职责，模型只需要声明依赖和组合关系。

需要注意：Step-Code 的 `parallel()` 为了让一项失败不阻断其它项，会把单项异常转成 `null`。这提高了 fan-out 的容错，但要求 workflow script 显式过滤和检查 `null`；GA 不应直接复制这个行为后再把 `null` 当成功结果。

### 11.3 Codex CLI：事件等待、终态状态和结果通知分开

Codex 的 `wait_agent` 主要等待 mailbox/activity，而不是直接读取最终正文。它使用 input queue 的 activity subscription，在 timeout 前等待：

```text
mailbox activity
steer/new input
timeout
```

同时，Codex 另有独立状态查询：

- `list_agents` 返回 agent status；
- `AgentStatus` 从 `TurnStarted`、`TurnComplete`、`TurnAborted`、`Error`、`ShutdownComplete` 事件派生；
- `is_final()` 明确判断是否达到终态；
- completion control 将 terminal outcome 发送回 parent；
- agent graph store 持久化 open/closed parent-child edge。

这说明 Codex 并没有把“等待事件”和“等待终态”混为一个 API。它的模型是：

```text
wait/activity      等待父侧可能需要处理的新输入或通知
status              查询当前状态
completion          接收终态通知
graph               查询 parent/child 生命周期关系
result/transcript   读取持久化内容
```

### 11.4 Pi durable：恢复后等待 durable task，而不是重新猜测

Pi durable 示例中的 `waitForTask(taskId)` 可以在 harness 重启后继续等待未完成 task。task 每个 phase 都写入 checkpoint，终态由 `runtime.commit()` 提交，abort 也有独立处理器。

对 GA 的启发是：

```text
wait 必须基于 durable task/run state；
不能只看当前进程是否还存在；
重启后应根据 checkpoint 继续等待或直接返回已持久化终态。
```

## 12. GA wait agent 的改造建议

### 12.1 保留当前低层 watcher，但拆出显式 wait mode

当前 `wait_agent` 的 event bus + realtime channel + polling fallback 可以保留，作为底层 watcher。上层 API 增加显式模式：

```json
{
  "targets": ["research_a", "research_b"],
  "wait_for": "all_terminal",
  "timeout_seconds": 900,
  "since_event_seq": 120
}
```

建议支持：

```text
event             任意新事件（兼容当前行为）
turn_terminal     目标 turn 达到 completed/failed/aborted
process_terminal  进程达到 exited/killed/shutdown
all_terminal      所有 targets 的 turn 达到终态
result_available  final output/artifact 已持久化可读
workflow_terminal workflow run 达到 succeeded/failed/aborted/killed
```

默认不应再是 `event`，对于模型发出的“等待子 agent 完成”意图，应默认使用 `all_terminal` 或 `result_available`。

### 12.2 明确三种终态，不要只用一个 completed

GA 至少应区分：

```text
turn_status:
  pending / running / completed / failed / aborted

process_status:
  starting / running / exited / killed / shutdown / stale

result_status:
  unavailable / pending / available / corrupt
```

`wait_for=all_terminal` 应明确等待哪一层。默认建议等待 `turn_status`，但如果后续动作依赖 artifact，则使用 `result_available`。

### 12.3 wait 返回结构化 decision packet

不要只返回 `changed/timeout`，应返回每个 target 的快照：

```json
{
  "status": "completed|partial|timeout|failed|aborted",
  "wait_for": "all_terminal",
  "targets": [
    {
      "task_name": "research_a",
      "turn_status": "completed",
      "process_status": "exited",
      "result_status": "available",
      "result_ref": "final_output_round_0",
      "error": null,
      "last_event_seq": 128
    },
    {
      "task_name": "research_b",
      "turn_status": "running",
      "process_status": "running",
      "result_status": "pending",
      "result_ref": null,
      "error": null,
      "last_event_seq": 129
    }
  ],
  "next_event_seq": 129,
  "remaining_targets": ["research_b"],
  "recommended_next_action": "wait_again"
}
```

这样模型不需要再组合 `list_agents`、`read_agent_result` 和 `probe` 才能知道下一步。

### 12.4 加入 fast path 和 race-safe subscription

wait 的顺序应是：

1. 读取 durable state 和 result index；
2. 如果目标已达到条件，立即返回；
3. 建立 event/realtime subscription；
4. 再次以 event cursor 检查 subscription 建立期间发生的事件；
5. 阻塞等待 signal；
6. signal 到达后只重新计算目标条件，不因任意事件直接返回；
7. timeout 时返回完整 per-target snapshot。

这样可以避免：

- 已完成 agent 仍等待一个新事件；
- check 与 subscribe 之间丢事件；
- `turn_completed` 已写入但父侧刚好错过通知；
- realtime channel 断开后必须等完整 polling interval。

### 12.5 增加 barrier API，减少模型手工编排

对于明确的多 agent 任务，GA 应提供：

```text
wait_all(targets, condition="turn_terminal")
wait_any(targets, condition="result_available")
wait_chain(previous_target, condition="success")
wait_workflow(run_id, condition="terminal")
```

或在现有 `wait_agent` 中以 `wait_for` 参数表达同样语义。

workflow runtime 内部则应像 Step-Code 一样，让 DAG scheduler 自己等待依赖，而不是让 LLM 生成：

```text
先 list
再 sleep
再 wait
再 list
再 read
```

### 12.6 失败不能被“等待成功”吞掉

如果目标进入 failed/aborted/killed/stale，wait 不应只返回“发生变化”。应立即返回 terminal failure，并包括：

- terminal status；
- error code/message；
- stderr/error artifact；
- 是否允许 resume/followup；
- 受影响的下游 targets。

对于 barrier：

```text
wait_all = 任一 required target failed -> overall failed
wait_any = 至少一个成功即可，但必须返回失败项
chain    = 上游失败 -> 下游 skipped/blocked
```

### 12.7 对模型暴露明确操作协议

工具描述和系统提示应明确：

```text
spawn 后不要用短 timeout 的 event wait 代替终态等待；
需要结果时使用 wait_for=all_terminal/result_available；
wait 返回 completed + result_available 后直接读取 result_ref；
wait 返回 timeout 时根据 remaining_targets 继续等待，而不是重新 spawn；
只有 terminal failure 才进入 repair/resume/followup；
list_agents 只表示当前视图，不代表历史结果不存在。
```

提示词不是最终保障，但可以减少模型误用；真正的判断仍由 wait runtime 的 target predicate 和 durable state 完成。

## 13. 对 GA wait 设计的最终判断

GA 目前不是“没有事件机制”，而是“事件机制已经存在，但上层语义过于低级”。

可以概括为：

```text
当前 GA：event watcher
Pi：process completion + Promise barrier
Step-Code：workflow await + scheduler-owned barrier
Codex：activity wait + terminal status + completion notification + graph
```

GA 的正确优化方向不是简单提高 polling 频率或把默认 timeout 调大，而是：

1. 保留 event-driven watcher；
2. 增加终态 predicate；
3. 将 wait、status、result、graph 分开；
4. 提供 all/any/chain/workflow barrier；
5. 用 durable state 做 fast path 和恢复；
6. 返回结构化 decision packet；
7. 让 scheduler/runtime 承担等待，不让 LLM 手工模拟调度器。

这会直接减少 GA 过去出现的：

- 180 秒 wait 只看到 `agent_started`；
- 子 agent 已完成但模型不知道要读结果；
- wait 超时后重复 spawn；
- list_agents 为空但 read_agent_result 成功；
- `/stop` 中断 wait 后无法判断 child 是否仍在后台完成；
- 多 agent 只等到第一个事件就开始 synthesis。


## 7. 2026-10-01 实施 checkpoint

本阶段按路线 2 落地了三项硬约束：

1. **计划形状优先于 taskType**：`plan_produces_code()` 只依据 `writeScope` 与 `implementation/tests/repair` 角色判断写入型工作；`taskType` 保留为 planner 提示，不再单独触发 coding 门禁。旧 `acceptance` 字段仍通过 `workflow_verification` 做兼容转换。
2. **不再隐式注入 verifier/schema**：planner 归一化不再凭空创建 `verification` agent、`GA_WORKFLOW_VERIFICATION_SCHEMA` 或 strict schema；写入型计划必须显式声明 `verification.checks` 中的 required check，检查类型可为 command/schema/artifact 等。
3. **检查执行边界加固**：command adapter 要求 argv 数组、`shell=False`，并限制 executable/module allowlist；拒绝任意网络命令和 `python -c` 内联代码。
4. **结果视图只读**：`list_result_view()` 使用 `probe_agent`，列举 active/closed/stale agent 不会刷新或改写持久化 state。
5. **恢复完整性校验**：workflow job 写入 result 时记录 SHA-256；resume projection 对已成功/缓存 job 校验 artifact，缺失或篡改会标记 `stale` 并要求重新执行，而不是复用损坏结果。

验证：

- planner / validator / verification / controller / scheduler / runtime / adapter / subagent 聚焦套件：322 tests passed。
- 真实 `deepseek-v4.1-flash` forward matrix 已于 2026-10-01 串行通过，耗时 33.29 秒；详见 `docs/20261001-ga-dynamic-workflow-reliability-validation.md`。该 matrix 使用 fake child runtime，真实 child/MCP E2E 仍待单独执行。


## 14. 真实模型复测补充：契约修复必须进入 repair loop

2026-10-01 的真实 `deepseek-v4.1-flash` 复杂 workflow 证明：child runtime、MCP、skill、文件读写和 synthesis 均可成功，但模型偶尔会生成不完整的 verification schema，或使用计划中真实存在的动态 agent label 作为 check owner。

这类错误属于“模型计划契约错误”，不是 provider 不可用，也不应触发 deterministic fallback。GA 现在将 normalization exception 投影成 validator issue，复用有界 repair loop；owner 则由固定 host capability 与受限动态 label 两层规则共同校验。这个分层比继续扩大固定枚举更稳健，也与 Step-Code/ultracode 的“显式计划 + runtime 硬校验”方向一致。


## 15. Phase 5 真实串行验收

新增两个显式 opt-in 的真实 DeepSeek E2E：

- `real_subagent_wait_terminal_e2e.py` 证明 terminal predicate 不会被 `agent_started/turn_started` 提前满足，并验证两个串行 child 的 result refs。
- `real_workflow_wait_barrier_e2e.py` 证明 journal 中上游 completion sequence 先于下游 start sequence，并验证 MCP、临时文件、host verification evidence 和 summary artifact。

两个用例必须串行执行；报告保留 startup phases、wait predicates、duplicate spawn、workflow total latency 和可用的 MCP timing 字段。无法从 provider transcript 得出的指标记录为 `null`，不以猜测填充。
