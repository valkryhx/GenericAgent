# GA Workflow Autonomous Activation and UI Execution Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 让 GenericAgent 在真实 UI 中根据用户的明确 workflow/多步骤意图可靠激活 workflow，并把用户动作完整执行为“搜索/调用 MCP → 产出文件 → 验证 → 明确完成”，避免当前“成功生成一个只规划的 planner workflow、工具不可见、cwd 不存在、UI 长时间无反馈”的失败链路。

**Architecture:** 采用 Step-Code 的三段式入口：激活决策（activation）只判断当前 turn 是否应进入 workflow；执行型 planner 负责把用户动词映射为 phase、agent、工具能力、artifact 和验收项；runtime 再用硬契约执行能力预检、workspace、DAG barrier、progress snapshot、terminal result。`taskType` 保留为提示字段，不再决定是否执行、是否写文件或是否添加固定 verification agent。

**Tech Stack:** Python 3.10–3.13、标准库 `dataclasses`/`unittest`/`pathlib`/`json`、现有 `workflow_planner.py`、`workflow_controller.py`、`workflow_runtime.py`、`workflow_child_agent.py`、`workflow_store.py`、Ink bridge/TypeScript UI、真实 `deepseek-v4.1-flash` 与 Tavily MCP（仅真实 E2E 使用本机被忽略配置）。

---

## 1. 这次真实 UI 运行暴露的事实

运行输入：

```text
/workflow 使用 tavily 搜索 刘国梁 然后写一个 html 介绍 他
```

证据目录：

```text
temp/sessions/session_93d323d8d6d04cd28a3278e701bd5c4b/
└── workflows/wf_4e325d74bb07475dbf3dfd0d91485245/
```

观测结果：

1. `state.json` 最终为 `status=succeeded`，所以不是进程死锁或 300 秒 startup hang。
2. 会话从 `00:30:55` 到 `00:32:43`，约 108 秒；期间用户看不到可执行产物，因而体感为“卡住”。
3. `workflow-draft.json` 的 classification 是：

   ```json
   {
     "taskType": "planning",
     "readWriteMode": "read_only",
     "needsMcp": false,
     "needsCodeChange": false
   }
   ```

4. `plannerMode` 是 `deterministic`，脚本只有一个 `Plan` phase 和一个 `planner` agent；planner 的任务是“制定最小执行计划和验证建议”，不是执行搜索或写文件。
5. child capability snapshot 显示 `mcpToolNames=[]`、`injectedToolCount=0`，所以 child 根本看不到 Tavily。
6. child 的四次 `code_run` 全部因为默认 cwd 不存在而失败：

   ```text
   D:\git_codes\GenericAgent\temp\workflow_child_agents\agent_1
   ```

7. `web_scan` 也失败，因为没有浏览器标签页；child 最后只能输出一份计划。没有产生 `liuguoliang.html`。

根因不是一个点，而是入口链路同时缺少四个硬契约：

```text
用户意图
  ↓ 缺少可靠激活协议
deterministic planner
  ↓ 未识别“搜索 + 写 HTML”，退化为 planning
Plan-only workflow
  ↓ 没有执行型 agent/ artifact/acceptance
child runtime
  ↓ MCP schema 未注入 + 默认 cwd 不存在
长时间探索失败
  ↓ UI 只有 activity 状态，没有可读的阶段/失败/完成结果
```

---

## 2. Step-Code 研究得到的可借鉴机制

参考源码和文档：

- `D:\git_codes\Step-Code\docs\orchestration-lifecycle.md`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\ultraloop-opt-in.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\step-workflow.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\runtime.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\progress.ts`
- `D:\git_codes\Step-Code\packages\coding-agent\src\features\workflow\agent-runner.ts`

### 2.1 激活不是 taskType 猜测，而是独立的 turn 信号

Step-Code 将以下信息分开：

- workflow 工具是否注册：环境能力；
- 当前 turn 是否授权/请求 workflow：`ultraloop`、`ultracode`、`use a workflow`、`run a workflow`、`fan out agents`、`orchestrate with sub-agents`；
- workflow 内部到底是研究、编码、审查还是混合：由计划显式声明。

GA 应保留“用户直接请求 workflow 时无须再次询问”的行为，同时增加语义激活建议，但不能把任务类型当作激活开关。

### 2.2 工具描述包含运行指导，而不是只描述参数

Step-Code 的 workflow tool promptGuidelines 明确告诉模型：什么时候用 workflow、什么时候只用单个 subagent 或文件工具、`parallel()` 是 barrier、`pipeline()` 是逐项并发、schema 最多重试三次、budget/cancellation/timeout 是硬停止条件、progress 必须持续输出完整 snapshot。

GA 的 workflow 工具也必须把“执行型计划、不允许只返回计划、目标动词必须有对应 evidence”写进模型可见 guidance，但这些 guidance 只作为入口软约束，runtime 仍要进行硬校验。

### 2.3 workflow progress 是完整不可变快照

Step-Code 每次 `onUpdate` 都发送完整 `WorkflowProgress`：phase、queued/running/completed/failed/cached/cancelled 数量、每个 agent 的 label/状态/阶段/耗时、token spend、当前错误或预算信息。

GA 当前 bridge 已有 `workflow_progress`，但必须保证 planner、MCP preflight、child capability、artifact、acceptance 和 terminal outcome 都进入同一个 snapshot；不能只让 UI 看到一个泛化的 `Running workflow ...`。

### 2.4 child 拓扑和能力必须由 runtime 保证

Step-Code 的 workflow child 是一层 fan-out，child 继承 `STEP_CLI_SUBAGENT_CHILD=1` 和 `STEP_DISABLE_WORKFLOW=1`，不注册 workflow/subagent/cron 等递归工具；tool profile、ACL、workspace mount 和 schema 在 host 侧决定。

GA 要采用同样原则：child 不应自行探查“有没有 Tavily”，而应收到 host 已解析的 capability snapshot；必需 MCP 工具缺失时应立即报告结构化 `capability_unavailable`，而不是花十几轮调用 `code_run`/`web_scan` 猜环境。

### 2.5 journal、schema retry、budget 和 terminal event 是一套闭环

Step-Code 记录 `workflow_started`、`workflow_phase`、`workflow_agent_started`、`workflow_agent_finished`、`workflow_schema_failed`、`workflow_acl_blocked`、`workflow_budget_exceeded` 和 `workflow_finished`。GA 的 journal 需要增加/统一 terminal event 和失败类别，UI 订阅 journal/progress 后应能立即显示“哪个阶段、哪个工具、哪种阻塞”，而不是只有 spinner。

---

## 3. 目标行为与不变量

### 3.1 用户入口

以下输入应触发 workflow 激活建议或直接 workflow：

```text
/workflow 使用 tavily 搜索刘国梁，然后写一个 HTML 介绍页
请用 workflow 完成：先搜索，再生成 HTML，最后检查文件
用 multi-agent 先调研资料，再由 writer 生成页面，最后验证
用 agent team 并行查三个来源，然后合成报告
```

以下输入默认不要自动 workflow：

```text
解释一下刘国梁是谁
读取 README 并总结
把这句话翻译成英文
单步修正一个拼写错误
```

“搜索 + 写文件”“多来源 + 合成”“先研究再写再验证”等是高置信度语义信号，但必须先生成 `WorkflowActivationDecision`，不能直接由关键词分支硬编码 taskType 门禁。

### 3.2 执行不变量

1. 用户要求“搜索”时，计划必须声明目标搜索工具和 `tool_call/tool_result` evidence；没有真实调用不得标记成功。
2. 用户要求“写 HTML”时，计划必须声明 writable workspace、具体 artifact 路径和 artifact check；只有自然语言不能验收。
3. 用户未要求“制定计划”时，不得把唯一 agent 命名为 planner 并在执行后只返回计划。
4. 每个用户动作动词必须映射到一个 agent deliverable 和至少一个 acceptance check。
5. 必需 capability 在 child 启动前预检；缺失时 fail-fast，不允许 child 盲目探查环境。
6. child cwd 要么来自显式 workflow workspace，要么由 host 创建后再启动；不存在的目录不能传给工具。
7. workflow 完成必须同时满足 `state.status`、`executionOutcome`、`integrationStatus`、`finalAuditStatus` 和 artifact/acceptance 结果一致。
8. UI 必须展示 planner、capability、phase、agent、artifact、acceptance 和 terminal result；任何终端状态都不能留在无限 running。
9. `taskType` 只用于 planner 提示和 UI 标签；不再根据 `research/coding/planning` 自动插入或删除硬门禁。
10. 任何重试都有上限；工具不可用、schema 错误、MCP transient、provider timeout 必须有不同失败类别。

---

## 4. 目标架构

```text
用户输入
  ↓
WorkflowActivationResolver
  ├─ explicit: /workflow、workflow、multi-agent、agent team、fan-out
  ├─ semantic: 搜索+写文件 / 多来源+合成 / 分阶段+验证
  ├─ session: /workflow on（可选）
  └─ none: 普通单 agent 路径
  ↓
ActivationDecision（requested / recommended / none）
  ↓ requested/recommended
Prompt-guided Planner
  ↓
Normalize + Validate Execution Contract
  ├─ action coverage：每个动词都有 deliverable
  ├─ capability contract：MCP / skills / file tools
  ├─ workspace contract：read/write scope
  ├─ acceptance contract：artifact / schema / command / tool evidence
  └─ bounded retry / budget / DAG
  ↓
Host Capability Preflight
  ├─ MCP snapshot + cache generation
  ├─ child workspace mkdir/canonicalize
  ├─ tool profile / ACL
  └─ missing required capability → fail-fast
  ↓
WorkflowRuntime / Scheduler
  ├─ phase barrier / dependency barrier
  ├─ child start / poll / terminal predicate
  ├─ immutable progress snapshots
  └─ journal events
  ↓
Host Acceptance
  ├─ required MCP tool call evidence
  ├─ artifact exists + path scope + checksum
  ├─ file read-back / command exit code
  └─ synthesis result references
  ↓
Workflow final + UI summary
```


## 5. 具体契约设计

### 5.1 激活决策

新增 `workflow_activation.py`：

```python
@dataclass(frozen=True)
class WorkflowActivationDecision:
    mode: str  # explicit | semantic | session | none
    action: str  # requested | recommended | none
    confidence: float
    matchedSignals: tuple[str, ...]
    taskText: str
    reason: str
    requiresFanout: bool
    requiresPhases: bool
```

规则：

- `/workflow` 或明确 `use workflow/run workflow/multi-agent/agent team`：`action=requested`；
- “搜索多个来源并生成文件/报告”“先研究再写再验证”等同时命中两个独立信号：`action=recommended`；
- 单步解释、翻译、简单读取：`action=none`；
- `action=recommended` 只向主 agent 注入一次 Step-Code 风格 system reminder，让主 agent 自己调用 workflow；不能在 activation 层直接伪造 workflow plan；
- 每个 turn 都重新计算，不能把上一个 turn 的 activation 带到下一个 turn；session-standing 模式必须有显式 `/workflow on/off`。

### 5.2 执行型计划

计划归一化后必须包含以下字段：

```json
{
  "taskType": "mixed",
  "mode": "workflow",
  "phases": [
    {
      "title": "Research",
      "agents": [
        {
          "label": "tavily-research",
          "role": "research",
          "prompt": "调用 mcp__tavily__tavily_search 搜索刘国梁并返回来源摘要",
          "capabilityProfile": "mcp_read_only",
          "requiredTools": ["mcp__tavily__tavily_search"],
          "writeScope": [],
          "deliverables": ["research_sources"],
          "acceptanceChecks": ["required_mcp_call", "source_count"]
        }
      ]
    },
    {
      "title": "Write HTML",
      "agents": [
        {
          "label": "html-writer",
          "role": "implementation",
          "dependsOn": ["tavily-research"],
          "capabilityProfile": "workspace_writer",
          "requiredTools": ["file_write", "file_read"],
          "writeScope": ["args.workspacePath/liuguoliang.html"],
          "deliverables": ["liuguoliang.html"],
          "acceptanceChecks": ["artifact_exists", "artifact_readback"]
        }
      ]
    },
    {
      "title": "Verify",
      "agents": [
        {
          "label": "html-verifier",
          "role": "verification",
          "dependsOn": ["html-writer"],
          "capabilityProfile": "workspace_read_only",
          "writeScope": [],
          "deliverables": ["html_verification"],
          "acceptanceChecks": ["artifact_exists", "html_structure", "no_secret_pattern"]
        }
      ]
    }
  ],
  "artifacts": ["research_sources", "liuguoliang.html", "html_verification"],
  "acceptance": {
    "required": true,
    "checks": [
      {"id": "tavily-call", "kind": "tool_evidence", "tool": "mcp__tavily__tavily_search", "required": true},
      {"id": "html-file", "kind": "artifact", "path": "args.workspacePath/liuguoliang.html", "required": true},
      {"id": "html-readback", "kind": "artifact_readback", "path": "args.workspacePath/liuguoliang.html", "required": true}
    ]
  }
}
```

计划 validator 必须拒绝以下情况：

- 用户要求执行，但只有 `planner` agent；
- 用户要求写文件，但所有 `writeScope` 为空；
- 用户要求 MCP，但 `requiredTools` 为空；
- acceptance 只有自然语言，没有 machine-checkable check；
- 下游 agent 依赖上游结果但未声明 `dependsOn` 或 result reference；
- 计划把 `read_only` 与 `file_write` 同时声明；
- plan status 成功但没有 artifact/evidence。

### 5.3 capability snapshot

`workflow_child_agent.py` 应在 child 启动前接收 host 生成的 snapshot：

```python
@dataclass(frozen=True)
class WorkflowCapabilitySnapshot:
    generation: str
    tools: tuple[str, ...]
    mcp_tools: tuple[str, ...]
    skills: tuple[str, ...]
    workspace_root: str
    write_scopes: tuple[str, ...]
```

child 不再通过 `code_run` 猜测仓库路径，也不再通过 `glob` 猜测 `mcp.json`。缺失 required capability 时由 host 直接生成：

```json
{
  "status": "failed",
  "category": "capability_unavailable",
  "required": ["mcp__tavily__tavily_search"],
  "available": [],
  "nextAction": "repair_or_abort"
}
```

### 5.4 workspace

- `WorkflowRuntime` 在创建 run 时生成默认 workspace：

  ```text
  temp/sessions/<session_id>/workflows/<run_id>/workspace/
  ```

- 所有 job metadata 都携带 `workspacePath`；
- `NativeGPTChildAgentRunner._child_cwd()` 只接受已经存在且 canonicalized 的目录；
- 默认 fallback 目录必须 `mkdir(parents=True, exist_ok=True)` 后再返回；
- 文件工具和 code tool 都复用同一 workspace root；
- workspace 路径进入 cache key，防止不同 run 复用错误 artifact。

### 5.5 MCP cache 与预检

- workflow preflight 使用与 child 相同的 config path 和 cache signature；
- cache entry 增加 `generation`、`complete`、`toolCount`、`serverErrors`；
- enabled server 存在但工具列表为空时，不能永久写入 `complete=true` 空缓存；
- required MCP tool 缺失时，最多做三次有界重试，失败即 `capability_unavailable`；
- 已发现的 MCP snapshot 在同一 run 内固定，不让不同 child 看到不同工具集合；
- 远程 Exa/Context7 等非必需 server timeout 只作为 partial error，不阻塞 Tavily required tool。

---

## 6. 文件边界与实施任务

### Task 1: 建立 activation resolver

**Files:**
- Create: `workflow_activation.py`
- Modify: `ga.py`（主 agent turn 的 workflow recommendation 注入点）
- Modify: `frontends/ink_bridge.py`（`/workflow` 显式请求路径）
- Test: `tests/test_workflow_activation.py`

- [x] 写失败测试：覆盖显式 `/workflow`、`multi-agent`、`agent team`、中文“先搜索再生成并验证”、普通问答和 session mode reset。
- [x] 实现 `WorkflowActivationDecision` 与独立 signal detector；每个 turn 重新计算。
- [x] 显式 `/workflow` 直接标记 `requested`；语义高置信度只注入一次 system reminder。
- [x] 运行：`python -m unittest tests.test_workflow_activation`；预期所有 activation case 通过。

### Task 2: 修正 deterministic planner 的执行语义

**Files:**
- Modify: `workflow_planner.py`
- Test: `tests/test_workflow_planner_execution_intent.py`

- [x] 写失败测试：`使用 tavily 搜索刘国梁然后写 HTML` 必须产生 mixed/research+artifact plan；`制定一个实施计划，不要执行` 才能产生 planning plan；`解释刘国梁` 不应产生 workflow execution plan；`搜索多个来源并生成报告` 必须声明 MCP、writer 和 verification checks。
- [x] 将 action coverage 从 taskType 分离：解析 `search/research/fetch/write/create/generate/save/verify` 等动作和否定词 `只规划/不要执行`。
- [x] 将 `needsMcp`、`readWriteMode`、`needsCodeChange` 改为 plan/verb 推导结果；`taskType` 只做 hint。
- [x] 新增 deterministic mixed template：Research → Artifact Writer → Verification，不得只有 planner agent。
- [x] 运行专项 planner 单测并检查生成 plan 的所有 action 都有 deliverable/check。

### Task 3: 增加执行契约 validator

**Files:**
- Modify: `workflow_verification.py`
- Modify: `workflow_planner.py`
- Test: `tests/test_workflow_execution_contract.py`

- [x] 写失败测试：plan-only、missing required MCP、missing write scope、missing artifact check、invalid dependency 都必须 fail closed。
- [x] 实现 action-to-deliverable coverage 检查。
- [x] 实现 required tool、write scope、artifact、tool evidence 和 downstream dependency 检查。
- [x] 保留 taskType 兼容字段，但禁止其单独触发或跳过门禁。
- [x] 运行 validator 全部测试，验证错误类别稳定为结构化 issue code。

### Task 4: 统一 host capability preflight

**Files:**
- Modify: `workflow_child_agent.py`
- Modify: `workflow_runtime.py`
- Modify: `workflow_scheduler.py`
- Modify: `mcp_runtime.py`
- Test: `tests/test_workflow_capability_preflight.py`
- Test: `tests/test_mcp_runtime.py`

- [x] 写失败测试：required Tavily 存在时 snapshot 包含它；空/陈旧 cache 不得遮蔽 live discovery；required tool 缺失必须 fail-fast。
- [x] 定义 `WorkflowCapabilitySnapshot` 并写入 job metadata/cache key/transcript capability event。
- [x] workflow run 开始时完成一次 MCP discovery，固定 generation；child 只消费 snapshot。
- [x] 修复空 complete cache 语义：配置存在但 enabled server 未产生任何工具时标记 incomplete 并有界 refresh。
- [x] 非必需远程 server partial timeout 不得删除已知 required tool。
- [x] 运行 MCP runtime 与 capability preflight 单测。

### Task 5: 修复 workspace 创建和路径契约

**Files:**
- Modify: `workflow_child_agent.py`
- Modify: `workflow_runtime.py`
- Modify: `workflow_store.py`
- Test: `tests/test_workflow_child_agent.py`
- Test: `tests/test_workflow_runtime.py`

- [x] 写失败测试：无 `args.workspacePath` 的 interactive workflow 也必须创建有效 child cwd。
- [x] run 创建时生成并持久化 workspace root。
- [x] 每个 job 继承 workspacePath；`_child_cwd()` 对默认目录执行 `mkdir` 并 canonicalize。
- [x] 文件读写和 code_run 统一限制在 workspace/writeScope。
- [x] 运行 child/runtime 回归测试，确认不存在 `code_cwd does not exist`。


### Task 6: 将 Step-Code 风格 guidance 接入 workflow tool

**Files:**
- Modify: `workflow_planner.py`
- Modify: `ga_agents_runtime.py` 或现有 workflow system prompt 构造点
- Test: `tests/test_workflow_prompt_guidance.py`

- [x] 写失败测试：prompt 必须包含 activation、execution contract、parallel/pipeline barrier、schema retry、budget、terminal result、不要只返回计划等指导。
- [x] 加入“用户要求执行时，planner 必须生成执行型 plan”的明确 guidance。
- [x] 加入“不要让 child 探查配置，host 提供 capability snapshot”的 guidance。
- [x] guidance 只改善模型行为，不替代 validator/runtime 硬约束。

### Task 7: 完善 journal/progress/terminal UI

**Files:**
- Modify: `workflow_runtime.py`
- Modify: `workflow_store.py`
- Modify: `frontends/ink_bridge.py`
- Modify: `frontends/ink-ui/src/App.tsx`
- Modify: `frontends/ink-ui/src/state.ts`
- Modify: `frontends/ink-ui/src/workflowStatusBar.ts`
- Test: `tests/test_workflow_ui_events.py`
- Test: `frontends/ink-ui/src/workflowStatusBar.test.ts`
- Test: `frontends/ink-ui/src/workflowPanel.test.ts`

- [x] 写失败测试：planner/capability/phase/agent/artifact/acceptance/terminal event 能按顺序进入 UI；workflow_final 后不保留 spinner。
- [x] 增加统一 `workflow_finished` / `workflow_failed` terminal event；payload 包含 status、outcome、integration、audit、artifact refs、blocking issues。
- [x] bridge 每次 progress 发送完整不可变 snapshot，而非只发送 label。
- [x] App 处理 `workflow_final` 时同步写入可读 system/local output：成功显示 artifact 和下一步；失败显示 category/retryability/repair action。
- [x] UI 显示当前 phase、active agent、MCP preflight、workspace、completed/total、elapsed、terminal outcome。
- [x] 运行 Ink 无截图程序化测试和 Python bridge 测试。

### Task 8: 增加真实 UI 回归 E2E

**Files:**
- Create: `frontends/ink-ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts`
- Create: `docs/20261002-ga-workflow-autonomous-activation-hardening-reference.md`

- [x] 使用真实 `deepseek-v4.1-flash`，从 UI bridge 的 workflow 请求入口触发：

  ```text
  /workflow 使用 tavily 搜索刘国梁，然后写一个 html 介绍页并验证文件
  ```

- [x] 断言 planner mode 为 prompt-guided 或 deterministic-execution，但不得是 plan-only。
- [x] 断言 plan 至少包含 research、writer、verification 三个可执行 packet。
- [x] 断言 child capability snapshot 含 `mcp__tavily__tavily_search`、`file_write`、`file_read`。
- [x] 断言真实 Tavily tool call/result 存在。
- [x] 断言 HTML artifact 存在、read-back 成功、无 secret pattern、路径在 workspace 内。
- [x] 断言 journal 存在 `workflow_started → workflow_phase → workflow_agent_started/finished → workflow_finished`。
- [x] 断言 UI 最终状态是 idle，workflow status 是 succeeded，且有可读 final result。
- [x] 测试严格串行运行；真实 key、raw response、Authorization 不写入 artifact。

### Task 9: 更新用户文档和运行诊断

**Files:**
- Modify: `docs/GA_workflow_user_guide.md`
- Modify: `AGENTS.md`
- Create: `docs/20261001-ga-workflow-autonomous-activation-reference.md`

- [x] 记录激活信号、session mode、普通单 agent 与 workflow 的边界。
- [x] 记录 workflow plan/action/evidence contract 和常见失败类别。
- [x] 记录 Step-Code 参考路径以及“activation ≠ taskType”的设计原则。
- [x] 记录 UI 排障命令、run/artifact/journal/progress 路径和真实 E2E 命令。

---

## 7. 分阶段交付顺序

### Phase A：先修复“不会执行”的入口

包含 Task 1、Task 2、Task 3。

验收：`/workflow 搜索并写 HTML` 不再生成单独 planner-only plan；普通“制定计划，不要执行”仍然保持 planning。

### Phase B：修复 child 能力和工作目录

包含 Task 4、Task 5。

验收：required MCP 缺失时在 child 启动前 fail-fast；可用时 child capability snapshot 显示 Tavily；任何默认 workflow 都不会出现 `code_cwd does not exist`。

### Phase C：修复可观测性和 UI 完成语义

包含 Task 6、Task 7。

验收：真实运行过程中 UI 能显示当前 phase/capability/agent 状态；成功或失败都有 terminal result，不再只显示泛化 spinner。

### Phase D：真实 UI 闭环

包含 Task 8、Task 9。

验收：真实 DeepSeek + Tavily 从 UI 入口生成 HTML 并验证，且全量测试通过。

---

## 8. 风险与取舍

### 不采用的方案：只增加关键词

关键词只能作为 activation signal，不能直接决定 taskType、验收门禁或工具权限。否则会重现“研究型任务误套 coding gate”的旧问题，也会把“搜索 + 写文件”误判成单一 research 或 planning。

### 不采用的方案：让 child 自己寻找 MCP 配置

这会把 host 的 capability contract 退化为模型探索，造成多轮 `code_run`、读取敏感配置的风险和不可控延迟。MCP discovery 必须在 host 侧完成并注入 snapshot。

### 不采用的方案：把所有 workflow 默认改成 LLM planner

默认 LLM planner 会增加延迟和 provider 波动。建议：

- 显式 `/workflow`：使用 prompt-guided planner；
- 高置信度语义激活：使用 prompt-guided planner；
- 普通单步任务：不启动 workflow；
- LLM planner 超时：只允许回退到“执行型 deterministic template”，不能回退到 planner-only template。

### 兼容性

- 保留已有 `taskType`、旧 draft 字段和旧 workflow artifact 读取能力；
- 新增字段使用 default/legacy conversion；
- 老的纯 planning workflow 只有在用户明确要求“制定计划”时继续生成；
- 旧 run 的 resume 必须校验 capability generation、workspacePath 和 plan hash，不一致时 fail closed 并要求新 run。

---

## 9. 完成标准

本计划完成的必要条件：

1. 普通 UI 输入“搜索多个来源并写文件”能自动得到 `action=recommended`，主 agent 能调用 workflow；明确 `/workflow` 直接进入 workflow。
2. deterministic fallback 不再把执行型自然语言任务降级为 planner-only。
3. workflow plan 对用户动作有完整 action/deliverable/acceptance coverage。
4. required MCP/tool/skill capability 在 child 启动前可观测且稳定注入。
5. 默认 workspace 永远存在，文件写入不会因为 cwd 缺失失败。
6. runtime journal/progress/UI 对 planner、capability、phase、agent、artifact、acceptance、terminal 都有结构化记录。
7. 真实 `deepseek-v4.1-flash` + Tavily UI E2E 通过，且实际生成并验证 HTML artifact。
8. 全量 Python unittest、Ink 程序化测试和真实 E2E 通过；没有把 provider 429、MCP partial timeout 或 UI 延迟误判成 workflow 成功。

---

## 10. Self-review checklist

- [x] 每个当前故障证据都有对应的实施任务：错误激活、planner-only、MCP 空注入、cwd 不存在、UI 无 terminal 反馈。
- [x] `taskType` 没有重新成为硬门禁；计划显式声明的 action/capability/artifact/check 才是门禁来源。
- [x] Step-Code 机制已映射到 GA：turn activation、session mode、promptGuidelines、capability boundary、progress snapshot、journal/terminal event、bounded retry/budget。
- [x] 所有任务包含文件边界、失败测试、实现动作和验证命令；没有用“以后补充”或未定义的 TODO 占位。
- [x] 真实 E2E 明确要求从 UI bridge 进入，而不是只调用底层 `WorkflowRuntime`。
- [x] 计划未授权提交真实 key、provider transcript、MCP header 或敏感配置。
