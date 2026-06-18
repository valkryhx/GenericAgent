# Claude Code Dynamic Workflow 会话参考分析

> 日期：2026-06-11  
> 目的：为下一轮 GA workflow 设计改造提供 Claude Code dynamic workflow 的真实会话参考。  
> 范围：本会话 transcript 与 Claude Code workflow/subagent 工件目录的只读分析。  
> 安全：未读取 `mykey.py` / `mykey.json` / `mcp.json`，未提交真实 API/MCP artifacts。

---

## 1. 会话与工件位置

本次可复盘的 Claude Code 会话 transcript：

```text
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82.jsonl
```

本会话 workflow 工件目录：

```text
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\workflows
```

workflow script 目录：

```text
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\workflows\scripts
```

workflow subagent transcript 目录：

```text
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\subagents\workflows
```

本轮真实多 agent 自测后台任务输出目录：

```text
C:\Users\drago\AppData\Local\Temp\claude\D--git-codes-GenericAgent\6ed43969-d9eb-4f04-bd09-fb34e8c7b387\tasks
```

---

## 2. 最近关键 workflow 样本

### 2.1 `wf_bc172f62-454`: 真实 gpt-5.5 + MCP E2E 执行计划

workflow metadata：

```text
runId: wf_bc172f62-454
workflowName: ga-real-workflow-e2e-plan-text
taskId: wu0a5cfx1
status: completed
agentCount: 3
totalTokens: 319825
totalToolCalls: 59
scriptPath: workflows/scripts/ga-real-workflow-e2e-plan-text-wf_bc172f62-454.js
subagentDir: subagents/workflows/wf_bc172f62-454
```

脚本结构：

```js
phase('入口审计')
const entry = await agent('只读审计真实 API + MCP workflow 执行入口...', {
  phase: '入口审计',
  label: 'entry-audit'
})

phase('脚本设计')
const design = await agent('设计一次真实 E2E：gpt-5.5 + MCP 搜集资料 + 中文报告...', {
  phase: '脚本设计',
  label: 'e2e-design'
})

phase('执行计划')
const synthesis = await agent(`综合以下审计...入口审计：${entry}\n\n脚本设计：${design}`, {
  phase: '执行计划',
  label: 'synthesis'
})

return { entry, design, synthesis }
```

子智能体：

| label | phase | agentId | 最后工具 | 作用 |
|---|---|---|---|---|
| `entry-audit` | 入口审计 | `a4d767008c2a9320f` | `Read` | 只读审计 `WorkflowRuntime + NativeGPTChildAgentRunner + MCP` 安全入口 |
| `e2e-design` | 脚本设计 | `a5faff0323cf6b915` | `Read` | 设计真实 E2E prompt、验收点、失败信号、命令建议 |
| `synthesis` | 执行计划 | `ae0b4068abd650c51` | `Skill: writing-plans` | 使用 planning skill 合成可执行计划 |

可参考点：

1. **先分离审计和设计，再综合。** 入口审计和脚本设计分成两个 agent，综合 agent 只消费它们的结果。
2. **综合阶段使用 skill。** `synthesis` 子智能体实际调用了 `writing-plans`，说明 Claude Code workflow subagent 会在适配任务时主动使用已有 skill。
3. **workflow result 保存完整过程。** `wf_bc172f62-454.json` 同时保存 script、result、phases、workflowProgress、每个 agent 的 `lastToolName` / `lastToolSummary` / token / toolCalls。
4. **subagent transcript 单独落盘。** 每个 agent 有独立 `.jsonl` 与 `.meta.json`。

### 2.2 `wf_f03e5c8d-944`: GA dynamic workflow feat 初始实现计划

workflow metadata：

```text
workflowName: ga-dynamic-workflow-feat-plan
scriptPath: workflows/scripts/ga-dynamic-workflow-feat-plan-wf_f03e5c8d-944.js
```

脚本结构：

```js
phase('Read Docs')
const docs = await agent('读取三份路线图文档并提取约束...', {
  label: 'docs-constraints',
  phase: 'Read Docs',
  schema: DOC_SCHEMA
})

phase('Map Code')
const maps = await parallel([
  () => agent('分析 Python 后端落点...', { label: 'python-runtime-map', schema: MAP_SCHEMA }),
  () => agent('分析 Ink bridge/UI 落点...', { label: 'ink-ui-map', schema: MAP_SCHEMA }),
  () => agent('分析现有 tests 与新增测试入口...', { label: 'test-map', schema: MAP_SCHEMA })
])

phase('Plan Phases')
const phased = await agent('基于文档约束和代码映射制定多阶段 feat 实施计划...', {
  label: 'phase-plan',
  schema: PLAN_SCHEMA
})

phase('Synthesize')
const final = await agent('把计划压缩成主会话可执行的中文实施建议...', {
  label: 'synthesis'
})

return { docs, maps, phased, final }
```

可参考点：

1. **schema 强约束用于规划型 agent。** `DOC_SCHEMA`、`MAP_SCHEMA`、`PLAN_SCHEMA` 强制子智能体返回结构化数据，降低综合阶段歧义。
2. **parallel 用在真正独立的 map 阶段。** Python runtime、Ink UI、tests 三个方向互不依赖，适合并行。
3. **串行阶段用于依赖合成。** `Plan Phases` 依赖 docs + maps，`Synthesize` 依赖 phased。
4. **这比本次 GA Level 1 code E2E 更合理。** 它没有把相互依赖的实现和测试并行，而是只把独立分析并行。

### 2.3 `wf_8c212bff-2af`: 真实 API 下一步候选审计

workflow metadata：

```text
workflowName: p8-real-api-next-step-audit
scriptPath: workflows/scripts/p8-real-api-next-step-audit-wf_8c212bff-2af.js
```

脚本结构：

```js
phase('候选审计')
const candidates = await agent('审计 P8 下一步真实 API diagnostic 候选...', {
  phase: '候选审计',
  label: 'real-api-candidates',
  schema: CANDIDATE_SCHEMA
})

phase('安全方案')
const safety = await agent('分析真实 API 测试安全执行方式...', {
  phase: '安全方案',
  label: 'real-api-safety',
  schema: SAFETY_SCHEMA
})

phase('综合计划')
const synthesis = await agent('综合审计结果，给出下一步推荐与执行计划...', {
  phase: '综合计划',
  label: 'synthesis'
})

return { candidates, safety, synthesis }
```

该 workflow 的 `synthesis` agent 使用了：

```text
Skill: using-superpowers
```

可参考点：

1. **安全方案单独成阶段。** 真实 API/MCP 任务前，Claude Code 会把安全执行方式作为单独子任务，而不是混在主逻辑里。
2. **候选审计与安全方案并非并行。** 当前脚本是串行，但二者其实可并行；是否并行取决于是否存在依赖。
3. **综合阶段加载 superpowers。** 说明 CC workflow subagent 并不是只能被动看 skill index，至少在主模型调度下会主动加载 `using-superpowers` / `writing-plans` 等 skill。

---

## 3. Claude Code workflow 的设计特征

### 3.1 Workflow 是“主控脚本 + 子智能体工件”的结构

每个 workflow 至少有：

```text
workflows/wf_<id>.json                 # workflow 总记录
workflows/scripts/<name>-wf_<id>.js    # workflow JS 脚本
subagents/workflows/wf_<id>/journal.jsonl
subagents/workflows/wf_<id>/agent-<id>.jsonl
subagents/workflows/wf_<id>/agent-<id>.meta.json
```

`wf_<id>.json` 中包含：

- `runId`
- `taskId`
- `script`
- `scriptPath`
- `result`
- `agentCount`
- `summary`
- `workflowName`
- `status`
- `phases`
- `workflowProgress`
- `totalTokens`
- `totalToolCalls`

`workflowProgress` 对每个 agent 记录：

- `label`
- `phaseTitle`
- `agentId`
- `model`
- `state`
- `lastToolName`
- `lastToolSummary`
- `promptPreview`
- `resultPreview`
- `tokens`
- `toolCalls`
- `durationMs`

这点对 GA 很有参考价值：GA 当前 workflow result 更关注 runtime/job/artifact，但缺少这种“面向人类回放的 workflowProgress”。

### 3.2 并行只用于独立任务

Claude Code 的较好样本中，parallel 用在：

- 分析 Python runtime；
- 分析 Ink UI；
- 分析 tests；
- 多角度审计；
- 多候选评估。

它不把有直接依赖的最终实现和最终测试并行执行。

这支持本次缺陷判断：GA 的开发型 workflow 若要符合 TDD，不应并行 `implementation` 与最终 `tests`。

### 3.3 综合阶段通常使用 skill

已观察到的 Skill 使用：

| workflow | agent label | phase | skill |
|---|---|---|---|
| `ga-real-workflow-e2e-plan-text` | `synthesis` | 执行计划 | `writing-plans` |
| `p8-real-api-next-step-audit` | `synthesis` | 综合计划 | `using-superpowers` |
| `p8-doc-and-bridge-fallback-audit` | `synthesis` | 综合建议 | `using-superpowers` |
| `p8-provider-anomaly-e2e-audit` | `provider-anomaly-plan` | Implementation Plan | `writing-plans` |
| `p8-provider-anomaly-e2e-audit` | `audit:AgentResult/runtime failure contract` | Contract Audit | `verification-before-completion` |
| `p8-real-native-gpt-suite-run` | `analyze-real-api-result` | 结果分析 | `systematic-debugging` |

这说明 Claude Code 的 workflow subagent 会根据任务语义选择 skill，例如：

- 规划类：`writing-plans`
- 元流程/方法论：`using-superpowers`
- 验证类：`verification-before-completion`
- 异常分析类：`systematic-debugging`

GA 需要从“skill 只是 child prompt 里的可选 index”升级到“workflow role 可显式声明或自动绑定 skill”。

### 3.4 Schema 用于规划/审计，不用于所有任务

Claude Code 早期规划 workflow 大量使用 schema，例如：

- `DOC_SCHEMA`
- `MAP_SCHEMA`
- `PLAN_SCHEMA`
- `CANDIDATE_SCHEMA`
- `SAFETY_SCHEMA`

但真实执行计划中也有无 schema 重试案例，例如 `ga-real-workflow-e2e-plan-text` 是“不使用结构化输出”的版本，原因是前一轮 schema workflow 失败。

对 GA 的启示：

1. schema 适合强约束审计/计划/候选排序；
2. schema 失败时需要优雅降级为文本 workflow，而不是整个流程中断；
3. schema failure 本身应被记录为 workflow issue。

---

## 4. 对 GA workflow 的可借鉴设计

### 4.1 增加 `workflowProgress` 风格的人类可读进度结构

建议 GA 在 `WorkflowRun` 或 `final-result.json` 中补充面向 UI/复盘的结构：

```json
{
  "workflowProgress": [
    {
      "type": "workflow_agent",
      "index": 1,
      "label": "tests",
      "phaseTitle": "Tests",
      "agentId": "agent_2",
      "state": "done",
      "lastToolName": "load_skill",
      "lastToolSummary": "test-driven-development",
      "tokens": 12345,
      "toolCalls": 8,
      "durationMs": 90000,
      "promptPreview": "...",
      "resultPreview": "..."
    }
  ]
}
```

对 P8/P9/P10 UI 很重要：用户需要看到 agent 为什么这么做、用了什么 skill、最后卡在哪里。

### 4.2 Skill 应进入 agent role contract

参考 Claude Code 的实际行为，GA 应支持：

```js
agent(prompt, {
  label: 'synthesis',
  skills: ['using-superpowers', 'writing-plans'],
  requireSkills: true
})
```

或模板层自动声明：

```text
contract agent -> brainstorming / writing-plans
tests agent -> test-driven-development
implementation agent -> test-driven-development
repair agent -> systematic-debugging
review agent -> requesting-code-review / verification-before-completion
final agent -> verification-before-completion
```

runtime 需要记录：

- `requiredSkills`
- `loadedSkills`
- `missingRequiredSkills`
- `skillLoadEvents`

### 4.3 TDD workflow 不应使用错误的 parallel

可参考 Claude Code 的并行原则：

- 可并行：互不依赖的代码映射、风险审计、候选评估；
- 不可并行：最终 tests 与 implementation、repair 与 retest、review 与 final。

GA 内置 TDD template 应固定为：

```text
Contract
→ Tests
→ Red Gate
→ Implementation
→ Green Gate
→ Review
→ Repair Loop
→ Final Verification
→ Final
```

### 4.4 测试 gate 应成为一等 workflow step

Claude Code workflow 通过主控脚本组织 agent，但最终执行/验证仍需要工具或 host gate。GA 如果要做工程 workflow，不能让 agent 自称测试通过。

建议增加安全受控 step：

```js
const red = await runPythonUnittest(args.workspace)
const green = await runPythonUnittest(args.workspace)
```

或先通过特殊 agent/tool 过渡：

```text
test-runner controlled job -> 写 TEST_RESULTS.json / TEST_FAILURES.txt
repair agent -> 读取失败日志修复
```

### 4.5 Artifact 需要分层：机器验收 + 人类复盘

Claude Code 保存：

- workflow 总 JSON；
- workflow script；
- subagent journal；
- subagent transcript；
- progress preview；
- skill 使用摘要。

GA 当前已有 artifact/transcript 分离，但建议补充：

```text
CONTRACT.md
TEST_PLAN.md
TEST_RED_RESULT.json
TEST_GREEN_RESULT.json
REPAIR_LOG.md
REVIEW.md
FINAL_REPORT.md
workflow-progress.json
```

这样才能做长期复盘和 UI 展示。

---

## 5. 需要后续重点阅读的工件

下一次继续会话时，可重点读取这些文件：

### 5.1 最新真实 E2E 规划 workflow

```text
workflows/scripts/ga-real-workflow-e2e-plan-text-wf_bc172f62-454.js
workflows/wf_bc172f62-454.json
subagents/workflows/wf_bc172f62-454/journal.jsonl
subagents/workflows/wf_bc172f62-454/agent-a4d767008c2a9320f.jsonl
subagents/workflows/wf_bc172f62-454/agent-a5faff0323cf6b915.jsonl
subagents/workflows/wf_bc172f62-454/agent-ae0b4068abd650c51.jsonl
```

### 5.2 初始 GA dynamic workflow feat 规划

```text
workflows/scripts/ga-dynamic-workflow-feat-plan-wf_f03e5c8d-944.js
workflows/wf_f03e5c8d-944.json
subagents/workflows/wf_f03e5c8d-944/
```

### 5.3 使用 skills 的 workflow 样本

```text
workflows/wf_8c212bff-2af.json   # synthesis 使用 using-superpowers
workflows/wf_28fb26b9-6de.json   # synthesis 使用 using-superpowers
workflows/wf_a5a9d521-b08.json   # planning 使用 writing-plans，audit 使用 verification-before-completion
workflows/wf_d4b99ac7-b57.json   # 分析真实 API 结果使用 systematic-debugging
```

---

## 6. 进一步分析：Skill 触发与 transcript 证据

### 6.1 子智能体 transcript 中的真实 Skill 调用形态

进一步读取重点 workflow 的 subagent transcript 后，确认 Claude Code workflow 的子智能体不是只在 `wf_*.json` 的 `workflowProgress.lastToolName` 中记录 skill，而是在子智能体对话 transcript 中真实出现了 `Skill` tool use / tool result。

典型片段来自：

```text
subagents/workflows/wf_bc172f62-454/agent-ae0b4068abd650c51.jsonl
```

该 agent 是 `ga-real-workflow-e2e-plan-text` 的 `synthesis` 阶段。transcript 中出现：

```text
assistant tool_use name=Skill input={'skill': 'using-superpowers', 'args': ''}
user tool_result content=Launching skill: using-superpowers
user text=Base directory for this skill: C:\Users\drago\.claude\skills\using-superpowers

assistant tool_use name=Skill input={'skill': 'writing-plans', 'args': ''}
user tool_result content=Launching skill: writing-plans
user text=Base directory for this skill: C:\Users\drago\.claude\skills\writing-plans
```

其他样本也一致：

```text
wf_8c212bff-2af / synthesis:
  Skill(using-superpowers)

wf_a5a9d521-b08 / audit:AgentResult/runtime failure contract:
  Skill(using-superpowers)
  ...后续...
  Skill(verification-before-completion)

wf_a5a9d521-b08 / provider-anomaly-plan:
  Skill(using-superpowers)
  Skill(writing-plans)

wf_d4b99ac7-b57 / analyze-real-api-result:
  Skill(using-superpowers)
  Skill(systematic-debugging)

wf_f03e5c8d-944 / synthesis:
  Skill(using-superpowers)
```

这说明 Claude Code 的 workflow-subagent 具备完整 Skill tool 能力，并会把 skill 内容作为新的 user message 注入后续上下文，而不仅仅是将 skill listing 放在 system prompt 中。

### 6.2 Skill 使用分布统计

对本会话 `workflows/wf_*.json` 中 `workflowProgress` 的 `lastToolName == "Skill"` 记录做初步统计：

| Skill | 次数 |
|---|---:|
| `using-superpowers` | 21 |
| `writing-plans` | 4 |
| `verification-before-completion` | 1 |
| `systematic-debugging` | 1 |
| `finishing-a-development-branch` | 1 |
| 空 `lastToolSummary` | 4 |

最近样本：

| Workflow | Agent label | Phase | Skill |
|---|---|---|---|
| `ga-real-workflow-e2e-plan-text` | `synthesis` | 执行计划 | `writing-plans` |
| `p8-real-api-next-step-audit` | `synthesis` | 综合计划 | `using-superpowers` |
| `p8-doc-and-bridge-fallback-audit` | `synthesis` | 综合建议 | `using-superpowers` |
| `p8-provider-anomaly-e2e-audit` | `provider-anomaly-plan` | Implementation Plan | `writing-plans` |
| `p8-provider-anomaly-e2e-audit` | `audit:AgentResult/runtime failure contract` | Contract Audit | `verification-before-completion` |
| `p8-real-native-gpt-suite-run` | `analyze-real-api-result` | 结果分析 | `systematic-debugging` |

解释：`workflowProgress.lastToolName/lastToolSummary` 只能记录“最后一个工具”，不是完整 skill 调用序列。完整序列必须读对应 `agent-*.jsonl` transcript。比如 `wf_bc172f62-454` 的 `synthesis` 最后工具显示为 `writing-plans`，但 transcript 证明它先加载了 `using-superpowers`，然后加载 `writing-plans`。

### 6.3 `using-superpowers` 的特殊现象：subagent 仍会加载

`using-superpowers` skill 内容开头包含：

```text
<SUBAGENT-STOP>
If you were dispatched as a subagent to execute a specific task, skip this skill.
</SUBAGENT-STOP>
```

但真实 transcript 显示多个 workflow-subagent 仍然先调用 `Skill(using-superpowers)`。这有两个重要含义：

1. **Skill tool 调用发生在模型读到完整 skill 内容之前。** 子智能体先根据 skill index 或内化策略决定调用 `using-superpowers`，调用后才收到完整 SKILL.md，其中才包含 `SUBAGENT-STOP`。
2. **`using-superpowers` 在 workflow-subagent 中更像“skill discovery bootstrap”。** 即使内容提示 subagent 可跳过，它仍常被用作进入其他 skill 的前置步骤，然后再加载更具体的 `writing-plans` / `systematic-debugging` / `verification-before-completion`。

对 GA 的启示：不要把“把 Available Skills listing 放进 prompt”误认为等价于 Claude Code 的 Skill 机制。Claude Code 有真实 `Skill` tool 调用、tool result 注入和 transcript 记录；GA 目前 child agent 虽然有 `load_skill` 工具，但需要 workflow/runner 明确保证它可用并可审计。

### 6.4 Skill 选择与 agent role 的关系

从样本看，Claude Code 的 skill 使用与 agent role 有明显对应关系：

- `synthesis` / `final plan` / `implementation plan` 倾向加载 `using-superpowers` 和/或 `writing-plans`。
- `audit` / `contract` / `completion` 倾向加载 `verification-before-completion`。
- `real api result analysis` / 异常分析倾向加载 `systematic-debugging`。
- 分支收尾 / 完成性判断可加载 `finishing-a-development-branch`。

这说明 CC 的 skill 使用不是随机工具调用，而是 role-sensitive 行为。GA 若要复刻，应把 skill 绑定到 workflow agent role，而不是完全依赖 child 模型自由选择。

建议 role -> skill 默认映射：

| GA workflow role | 建议 skills |
|---|---|
| `contract` / `requirements` | `using-superpowers`, `brainstorming`, `writing-plans` |
| `tests` / `test-writer` | `using-superpowers`, `test-driven-development` |
| `implementation` | `using-superpowers`, `test-driven-development` |
| `repair` / `debug` | `using-superpowers`, `systematic-debugging` |
| `review` / `audit` | `using-superpowers`, `requesting-code-review`, `verification-before-completion` |
| `synthesis` / `final-plan` | `using-superpowers`, `writing-plans` |
| `final-verification` | `verification-before-completion` |

### 6.5 对 GA artifact 的具体改造建议

基于 Claude Code 的 transcript 和 workflow JSON，GA 至少应增加以下可审计字段：

```json
{
  "jobId": "agent_3",
  "label": "tests",
  "phase": "Tests",
  "role": "test-writer",
  "requiredSkills": ["test-driven-development"],
  "loadedSkills": ["using-superpowers", "test-driven-development"],
  "skillEvents": [
    {
      "type": "skill_loaded",
      "name": "using-superpowers",
      "source": "claude",
      "pathRef": ".../using-superpowers/SKILL.md"
    },
    {
      "type": "skill_loaded",
      "name": "test-driven-development",
      "source": "claude",
      "pathRef": ".../test-driven-development/SKILL.md"
    }
  ],
  "missingRequiredSkills": [],
  "lastToolName": "file_write",
  "lastToolSummary": "test_url_utils.py",
  "promptPreview": "...",
  "resultPreview": "..."
}
```

并在 `final-result.json` 或单独 `workflow-progress.json` 中汇总：

```json
{
  "skillUsageSummary": {
    "using-superpowers": 5,
    "test-driven-development": 2,
    "systematic-debugging": 1,
    "verification-before-completion": 1
  },
  "requiredSkillFailures": []
}
```

### 6.6 对 GA runner 的实现建议

GA 当前 `NativeGPTChildAgentRunner._build_system_prompt()` 会拼接 `build_skill_prompt()`，但这只是 soft hint。要更接近 Claude Code，需要补充至少两层机制：

1. **工具可用性保证：** 如果 `agent(..., { skills: [...] })` 或 workflow template 要求 skill，`tools_schema_factory` 不能把 `load_skill` 裁掉；否则应 preflight failed，而不是静默执行。
2. **强制预加载或强制首轮调用：**
   - 简单方案：runner 在构造 child prompt 时直接注入 required skill 内容，记录为 `skill_preloaded`。
   - 更接近 CC 的方案：prompt 要求 child 首先调用 `load_skill`，runner/permission 层验证 transcript 中出现对应 `tool_call/tool_result`。

推荐先实现“预加载 required skills”作为确定性路径，再保留模型主动 `load_skill` 作为可选增强。原因是真实工程 workflow 需要可重复性，不能依赖模型是否想起调用 skill。

---

## 7. 最新补充：动态 Workflow Script 生成器设计结论

> 日期：2026-06-17
> 背景：基于本会话 `workflows/scripts` 与 `subagents/workflows` 的再次只读分析，目标从“模板库”修正为“根据任务语义动态生成 workflow script”。

### 8.1 核心判断

Claude Code dynamic workflow 的关键不是固定模板库，而是运行时生成受限 workflow DSL：

```text
用户任务
→ 任务语义理解
→ 生成 phase/agent/parallel/schema 结构
→ 渲染为受限 JS workflow script
→ 运行并记录 progress / transcript / journal
```

因此 GA 下一步应实现 planner/compiler，而不是让用户选择预置模板。

更进一步，Claude Code-style 的动态性很可能主要来自 **prompt-guided orchestration**，而不是代码中穷举所有任务模板。较旧的本地 Claude Code coordinator 源码仍提供旁证：其系统提示把 Claude 定位为 coordinator，要求它调度 workers 完成 research / implementation / verification，并显式写入并行、worker prompt、验证和失败处理策略，例如：

```text
Parallelism is your superpower.
Launch independent workers concurrently whenever possible.
When doing research, cover multiple angles.
Workers can't see your conversation. Every prompt must be self-contained.
Always synthesize findings before directing follow-up work.
```

这说明 Claude Code 的灵活编排能力来自三者结合：

```text
prompt-level orchestration policy
+ Agent/Workflow primitives
+ runtime / permission / transcript / progress 约束
```

对 GA 来说，正确方向不是继续堆 deterministic `if taskType then template`，而是：

```text
Prompt-guided LLMWorkflowPlanner
→ 输出 WorkflowPlan JSON
→ validator / repair loop
→ deterministic renderer
→ restricted workflow runtime
```

推荐架构：

```text
classify
→ WorkflowPlan JSON
→ validate
→ render
→ execute
→ replay / learn
```

### 8.2 本会话脚本证据

再次分析的脚本包括：

```text
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\workflows\scripts\claude-code-dynamic-workflow-generation-research-wf_26b3a892-03a.js
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\workflows\scripts\ga-dynamic-workflow-feat-plan-wf_f03e5c8d-944.js
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\workflows\scripts\p8-parallel-partial-failure-plan-wf_f57e07a1-8fb.js
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\workflows\scripts\p8-next-task-discovery-wf_49d884bc-f40.js
```

共同模式：

- `export const meta` 声明 name / description / phases；
- `phase()` 表达顺序栅栏；
- `agent(prompt, { label, phase, schema })` 是最小工作单元；
- `parallel()` 只用于同一 phase 内互不依赖的分支；
- `pipeline()` 用于同质列表的逐项处理；
- schema 作为脚本内常量靠近使用点定义；
- 后续 phase 经常通过 `JSON.stringify(previousResults)` 消费上游结构化结果；
- prompt 中显式写入任务边界：只读、不提交、不读敏感配置、不运行真实 API、输出测试入口、返回风险和建议。

### 8.3 subagent 记录证据

再次分析的 subagent 目录：

```text
C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\subagents\workflows
```

观察：

- 约 92 个 `wf_*` workflow run；
- 约 435 个 `agent-*.jsonl`；
- 约 435 个 `agent-*.meta.json`；
- 约 92 个 `journal.jsonl`；
- workflow agent 数量通常为 3-6，最大样本约 15；
- `journal.jsonl` 通常只记录 `started` / `result`，适合作为轻量 run 索引；
- `agent-*.jsonl` 才包含完整 prompt、tool_use、tool_result、Skill 调用和 StructuredOutput；
- `agent-*.meta.json` 当前信息量很少，不能作为主要复盘源。

因此 GA 的 replay/learn 不应只读 meta，而应优先：

```text
journal.jsonl
→ agent 首条 user prompt
→ StructuredOutput / result
→ tool_use / tool_result / Skill 调用
```

### 8.4 对 GA 的实现建议

#### 8.4.1 WorkflowPlan JSON 中间表示

不要让模型直接输出可执行 JS。先生成可验证计划：

```json
{
  "taskType": "research | coding | review | debugging | planning | mixed",
  "meta": {
    "name": "...",
    "description": "..."
  },
  "phases": [
    {
      "title": "Collect",
      "agents": [
        {
          "label": "source-search",
          "prompt": "...",
          "schemaRef": "SOURCE_SCHEMA",
          "dependsOn": []
        }
      ]
    }
  ],
  "schemas": {},
  "constraints": ["no_secret_files", "no_git_commit"],
  "artifacts": []
}
```

#### 8.4.2 Validator

渲染前检查：

- phase 顺序；
- 并行分支独立性；
- 未定义依赖；
- 未定义 schema；
- 禁止 token / forbidden helper；
- prompt 是否包含必要安全边界；
- coding 任务是否错误并行 tests 与 implementation；
- 是否尝试内联 secret 或 transcript；
- schema failure 是否有 fallback / issue 记录。

#### 8.4.3 Deterministic Renderer

由确定性代码渲染：

```text
WorkflowPlan JSON
→ export const meta
→ phase(...)
→ agent(...)
→ parallel(...) / pipeline(...)
→ return {...}
```

模型不直接拼接任意 JS，降低安全和可复盘风险。

#### 8.4.4 Replay / Learn

生成和运行过程都应保存：

- 原始任务；
- classifier 输出；
- WorkflowPlan JSON；
- rendered script；
- validator 结果；
- workflow-progress.json；
- journal.jsonl；
- agents/<job>/transcript.jsonl；
- fallback / partial failure issue。

后续可从历史 run 抽取：

```text
任务类型 -> phase 形状 -> agent label/prompt -> schema -> failure mode -> 修正建议
```

#### 8.4.5 Prompt-guided planner 的下一阶段 TDD 切片

当前 deterministic planner 只应视为 MVP / fallback。下一阶段应测试一个 prompt-guided planner 能根据不同任务生成不同 topology，而不是把所有任务套进固定模板。

必须覆盖四类任务：

1. **Research**
   - 输入可信度 / 风险调研任务；
   - 期望生成 Source Discovery、Credibility / Evidence Check、Synthesis；
   - source discovery 可并行多来源；
   - synthesis 必须依赖上游结果。
2. **Review**
   - 输入安全、性能、测试缺口、回归风险审查任务；
   - 期望按维度 fan out：security、performance、test-gap、regression；
   - 后续 verifier / adversarial check 依赖 review findings；
   - final report 依赖 verified findings。
3. **Coding**
   - 输入实现类任务；
   - 期望生成 `Understand -> Tests -> Implementation -> Verification -> Summary`；
   - `write-failing-tests` 必须先于 `implement-minimal-code`；
   - implementation 必须依赖 tests；
   - tests 与 implementation 不能在同一 independent parallel group；
   - prompt 必须明确先红灯后绿灯、不提交、运行相关测试；
   - validator 必须拒绝 `coding_tests_parallel_implementation`；
   - repair loop 必须能把错误并行结构修成顺序依赖。
4. **Planning / mixed**
   - 输入跨多个子系统的规划任务；
   - 期望生成 Context Discovery、Design Alternatives、Risk Review、Implementation Plan；
   - 不应直接生成写代码 workflow。

还必须覆盖 repair loop：fake planner 第一次输出未定义依赖、未定义 schema 或 coding 错误并行；validator 返回 issues；repair prompt 带入 issues；第二次输出修复后的 `WorkflowPlan JSON`；超过最大轮次则保存 rejected draft。

#### 8.4.6 Prompt-guided planner 实现与真实 E2E 结果（2026-06-18）

GA 已完成 prompt-guided planner 的最小实现，核心类型为 `LLMWorkflowPlanner`：

```text
client.complete(messages)
→ WorkflowPlan JSON
→ validate_workflow_plan
→ repair prompt with validator issues
→ render_workflow_plan
→ WorkflowRuntime
```

当前实现保留 deterministic `WorkflowPlanner` 作为 fallback，而不是继续扩展为主 planner。关键行为：

- planner prompt 包含 `classificationHint`、非空 `phases` 要求、review/coding/research/planning 的 orchestration policy；
- 模型只允许输出 `WorkflowPlan JSON`，不允许输出 JS；
- validator issues 会进入 repair prompt；
- repair 超限返回 rejected draft，保留 invalid plan 和 validation issues；
- provider/client 异常才 fallback 到 deterministic planner。

新增测试文件：

```text
tests/test_workflow_prompt_guided_planner.py
tests/prompt_guided_planner_real_e2e.py
```

GLM-5.1 opt-in 真实矩阵 E2E 已通过：

```json
{
  "passed": true,
  "issues": [],
  "model": "z.ai/glm-5.1",
  "plannerCallCount": 6
}
```

真实 GLM-5.1 生成的 workflow topology 展示了任务语义驱动能力：

| 场景 | 生成 phases | 生成 agents | runtime 结果 |
|---|---|---|---|
| research | `Parallel Multi-Source Investigation` -> `Synthesis and Evaluation` | `credibility-evidence-investigator`, `contradiction-analyst`, `landing-risk-assessor`, `research-synthesizer` | 4 jobs succeeded |
| review | `Dimension Fan-out Analysis` -> `Finding Validation` -> `Review Synthesis` | `security-reviewer`, `performance-reviewer`, `test-gap-analyzer`, `regression-risk-analyzer`, `finding-validator`, `review-synthesizer` | 6 jobs succeeded |
| coding | `Understand Planned Run Requirements` -> `Write Failing Tests` -> `Implement Planned Run Entry` -> `Verify Implementation` | `understand-planned-run-specs`, `write-failing-tests`, `implement-planned-run`, `verify-planned-run-implementation` | 4 jobs succeeded |
| planning/mixed | `Context Research and Information Gathering` -> `Approach Synthesis and Comparison` -> `Risk Review and Assessment` -> `Implementation Planning` | frontend/backend/runtime context researchers, approach synthesizer, risk reviewers, implementation planner | 8 jobs succeeded |

这次 E2E 的重点是验证“真实 LLM planner + 真实 GA workflow runtime”，child agent 端使用 `FakeChildAgentRunner` 控制成本。结论是：prompt-level orchestration policy 已能驱动 GLM-5.1 按任务现场生成不同计划，且 GA 能验证、编译并分阶段调度多个 agent job。

补充真实 child agents E2E：随后用同一 `z.ai/glm-5.1` 同时作为 planner 和 child agent 后端，运行 `tests/prompt_guided_planner_real_child_e2e.py`，验证真实 subagent 使用工具执行 workflow steps：

```json
{
  "passed": true,
  "issues": [],
  "model": "z.ai/glm-5.1",
  "plannerCallCount": 2
}
```

该测试使用 `NativeGPTChildAgentRunner(config_name="native_oai_config", enable_tools=True)`，允许 child agents 使用安全只读工具读取指定代码/文档，不允许读取 `mykey.py` / `mykey.json` / `mcp.json`、修改文件或提交。

| 场景 | 生成 phases | 真实 child agents | 工具调用 | runtime 结果 |
|---|---|---|---|---|
| prompt planner code review | `Dimension Fan-Out: Correctness / TDD Sequence / Secret Hygiene` -> `Synthesis: Consolidate Cross-Dimension Findings` | `correctness-reviewer`, `tdd-sequence-reviewer`, `secret-hygiene-reviewer`, `findings-synthesizer` | 23 次，含 `file_read`, `code_run`, `no_tool` | 4 jobs succeeded |
| docs/code consistency research | `Parallel Document & Code Reading` -> `Synthesis and Gap Analysis` | `read-defect-optimization-doc`, `read-dynamic-workflow-reference-doc`, `read-workflow-planner-code`, `doc-code-gap-analyzer` | 18 次，含 `file_read`, `code_run`, `no_tool` | 4 jobs succeeded |

合计 8 个真实 GLM-5.1 child jobs、41 次工具调用，`workflow-progress.json` 记录了每个 agent 的 `toolCalls`、状态和 `resultPreview`。这证明 GA 已经跑通：真实 prompt-guided planner 生成 topology，真实 child agents 继承工具能力并分步骤执行 workflow。

### 8.5 非目标修正

以下不应作为动态 workflow 的核心方向：

- 固定模板库作为主入口；
- 用户选择 `tdd-python-package` / `deep-research-template`；
- 模型直接生成自由 JS 并执行；
- 把 schema / skill / approval 全局硬门禁化；
- workflow script 直接获得 fs / shell / network / env 能力。

模板或 pattern 可以作为 planner 的安全默认形状和 fallback，但不是产品核心。

### 8.6 与前文旧结论的关系

前文 `role -> skill` / `requiredSkillFailures` 等建议反映的是当时对工程 workflow 可重复性的设想。根据后续决策，当前 GA 阶段三已改为 Claude Code-style optional skill awareness：

```text
默认可见、默认可用、自主调用、调用可审计、不调用不失败。
```

因此后续 planner/compiler 应沿用 optional skill 模式：在 prompt 中建议相关 skill，通过 `workflow-progress.json` 复盘实际使用情况，而不是引入 required/preload 作为默认门禁。

---

## 8. 初步结论

Claude Code dynamic workflow 的原始设计中，最值得 GA 借鉴的是：

1. **任务分层清晰：** 先审计/映射，再计划/综合。
2. **并行有边界：** 只并行独立任务，不并行有强依赖的 TDD tests/implementation。
3. **skill 会在关键阶段被使用：** 尤其是 synthesis、planning、verification、debugging。
4. **每个 workflow 都有可复盘工件：** script、run JSON、subagent transcript、workflow progress。
5. **schema 用于收敛结构化规划：** 但 schema 失败时可降级成文本计划。

GA 下一步应将这些经验产品化：

```text
skill-aware
TDD-aware
quality-gated
repair-loop-enabled
artifact-contract-driven
human-reviewable-progress
```

这与 `docs/P8_real_api_e2e_defect.md` 中记录的缺陷方向一致。
