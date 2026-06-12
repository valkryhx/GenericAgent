# GA Workflow 缺陷优化实施计划

> 日期：2026-06-12  
> 依据：`docs/P8_real_api_e2e_defect.md`、`docs/claude_code_dynamic_workflow_reference.md`  
> 目标：把 GA Dynamic Workflow 从“可调度多 agent 的底层 DSL”升级为“符合真实工程方法论、可验证、可复盘、可持续优化的开发工作流系统”。

---

## 0. 总体判断

真实 `gpt-5.5` 多 agent E2E 已证明：

```text
GA workflow runtime 底座基本可用；主要缺陷在 workflow 产品层、工程方法论层和可观测性层。
```

当前最关键的问题不是 `agent()` / `parallel()` 能不能跑，而是：

1. workflow 作者容易写出错误流程，例如把最终测试与实现并行；
2. 测试、review、repair 没有形成闭环；
3. skills 只是 soft prompt，不是一等约束；
4. 执行 GA workflow 的 child agent 在部分真实 E2E / diagnostic 中被人为裁剪成少量工具，无法代表真正承担任务的 workflow agent 能力；
5. 真实执行结果缺少可审计的 progress / skill / gate artifact；
6. 没有内置工程模板帮助用户走正确流程。

因此优化路线应按以下优先级推进：

```text
P0：先校正 workflow child agent 工具继承模型，确保真正承担任务的 agent 不被人为降级
P0：补齐可观测性和安全边界，避免继续黑盒测试
P1：把 skill 和 test gate 做成 workflow 一等能力
P2：实现 TDD / repair loop 模板，修复真实 E2E 暴露的核心方法论缺陷
P3：完善 artifact、secret scan、schema 降级和 UI 复盘能力
P4：沉淀模板库与真实 API 回归矩阵
```

---

## 1. 阶段一：Workflow child agent 工具继承模型校正（P0）

### 1.1 目标

确保执行 GA workflow 的 child agent 默认拥有与 Claude Code workflow agent 一致的完整任务执行能力：

```text
完整静态工具 schema
+ load_skill 能力
+ MCP discovered tools
+ 当前会话/运行上下文允许的权限策略
```

这些 child agent 是 workflow 中真正承担任务的角色，不应在真实 workflow 中被故意限制成只能 `file_read` / `file_write`。它们必须能够按任务需要使用 skills、MCP、读写、检索、审计、验证等能力；安全边界应由 permission policy、sandbox、MCP allowlist/denylist、approval 机制和 artifact 审计来控制，而不是靠隐藏工具 schema 来削弱 agent。

### 1.2 要解决的问题

新增缺陷：

#### DEFECT-10：真实 workflow diagnostic 过度裁剪 child agent 工具能力

现象：部分真实 E2E / diagnostic 中使用 `tools_schema_factory` 只暴露少量工具，例如：

```text
file_read
file_write
load_skill
单个 MCP tool
```

甚至有些诊断只暴露一个 selected MCP schema。这会导致：

1. workflow child agent 行为与 Claude Code workflow agent 不一致；
2. `load_skill`、MCP、检索、代码执行/验证等能力可能被静默禁用；
3. 无法验证 GA workflow 在“完整工具上下文 + 权限策略”下的真实工作能力；
4. 容易误判 GA workflow 能力，以为 workflow agent 只能读写文件；
5. 安全边界分散在测试 harness 的 schema 裁剪里，而不是统一进入 permission policy 和 transcript 审计。

### 1.3 原则：工具可见性与权限控制分离

GA workflow 应采用以下模型：

```text
工具可见性：默认完整继承当前 GA 可用工具，包括 static tools、load_skill、MCP tools。
权限控制：由 ToolPermissionPolicy / permissionProfile / restricted_mcp / approval / sandbox 决定是否允许执行。
审计记录：所有 tool_call、tool_allowed、tool_denied、tool_result 都进入 transcript 和 workflow artifact。
```

不应把“裁掉工具 schema”作为真实 workflow 的常规安全机制。

允许保留最小 schema 的场景仅限：

```text
单元测试特定工具分派逻辑
mock runner 测试
专门验证 schema 缺失时错误处理的负向测试
```

这些测试必须明确命名为 minimal-schema / restricted-schema，不得代表真实 workflow child agent 能力。

### 1.4 推荐默认策略

| 场景 | tools schema | permission profile |
|---|---|---|
| 普通 workflow child agent | 完整静态工具 + MCP discovered tools | `inherit-current-permissions` |
| 只读审计 workflow | 完整静态工具 + MCP discovered tools | `read_only` |
| 限定 MCP workflow | 完整静态工具 + MCP discovered tools | `restricted_mcp` |
| 高风险真实 API E2E | 完整静态工具 + MCP discovered tools | profile 限制 + sandbox 限制 |
| 单元测试 mock | 可使用最小 schema | 仅验证局部逻辑，不代表真实 workflow |

### 1.5 主要任务

#### 任务 1：移除真实 E2E 中 file-only / selected-only 工具裁剪

重点检查：

```text
tests/p8_real_api_e2e.py
temp/run_p5_multi_agent_code_e2e.py
其他真实 workflow diagnostic / harness
```

将真实 E2E 中的：

```python
tools_schema_factory=lambda: [selected_schema]
tools_schema_factory=lambda: selected_file_tools()
tools_schema_factory=lambda: _minimal_tool_inheritance_schema(...)
```

改成：

```text
不传 tools_schema_factory，使用 NativeGPTChildAgentRunner 默认完整工具加载
```

或在必须注入 stub MCP tool 时使用“完整工具 + 额外 MCP schema”的 helper，而不是 selected-only schema。

#### 任务 2：新增 full tool inheritance helper

如测试必须注入 in-process MCP stub，可使用：

```python
def full_tools_with_extra_mcp(extra_tools: list[dict]) -> list[dict]:
    tools = load_full_static_tools()
    tools.extend(discover_mcp_tools_cached())
    append missing extra_tools
    return tools
```

该 helper 的语义必须是“完整继承后追加”，不是“挑选少量工具”。

#### 任务 3：为真实 workflow 增加工具继承验收

真实 E2E 应验证：

```text
load_skill 可见
至少一个 MCP tool 可见或可由 MCP discovery 注入
常规 static tools 可见
permission profile 生效
被禁止的工具会 tool_denied，而不是从 schema 中消失
```

#### 任务 4：文档中明确禁止真实 workflow agent 降级

所有 P8/P9 后续文档中应明确：

```text
执行 workflow 的 child agent 是实际工作者，不是弱化版 summarizer。
真实 workflow 中不得为了“安全感”把它们限制成 file_read/file_write-only。
如果任务需要 skills 和 MCP，必须保证 load_skill 与 MCP tools 对 child agent 可见。
```

### 1.6 涉及文件

```text
workflow_child_agent.py
workflow_permissions.py
tests/p8_real_api_e2e.py
tests/test_workflow_child_agent.py
tests/test_workflow_permission_inheritance_e2e.py
docs/P8_real_api_e2e_defect.md
docs/claude_code_dynamic_workflow_reference.md
```

### 1.7 验收标准

- 默认 `NativeGPTChildAgentRunner` 不传 `tools_schema_factory` 时加载完整 static tools + MCP tools；
- 真实 E2E 不再使用 file-only / selected-only schema 代表真实 workflow；
- `load_skill` 在真实 workflow child agent 中可见；
- MCP tools 在真实 workflow child agent 中按当前 MCP discovery 可见；
- 安全限制通过 `ToolPermissionPolicy` 表达，并产生 `tool_allowed` / `tool_denied` 事件；
- artifact 可以证明 agent 看到了并使用了所需 skills / MCP 能力；
- minimal schema 测试仍可存在，但必须只作为局部单元测试。

---

## 2. 阶段二：Workflow 可观测性与缺陷基线固化（P0）

### 1.1 目标

先让每次 workflow 执行都能被稳定复盘，避免继续依赖临时 harness、人肉读取 transcript 或只看 final result。

该阶段不改变 workflow 行为，主要补充记录、诊断和回归测试，是后续所有改造的安全网。

### 1.2 要解决的问题

对应缺陷：

- DEFECT-04：测试结果没有进入 workflow 上下文；
- DEFECT-07：skill 使用没有进入验收指标；
- Claude Code 参考分析中提到 GA 缺少 `workflowProgress` 风格的人类可读进度结构。

### 1.3 主要任务

#### 任务 1：新增 `workflow-progress.json` 或等价字段

建议在 `WorkflowRun` / final artifact 中记录：

```json
{
  "workflowProgress": [
    {
      "type": "workflow_agent",
      "index": 1,
      "label": "tests",
      "phaseTitle": "Tests",
      "agentId": "agent_2",
      "state": "succeeded",
      "lastToolName": "file_write",
      "lastToolSummary": "test_url_utils.py",
      "tokens": 12345,
      "toolCalls": 8,
      "durationMs": 90000,
      "promptPreview": "...",
      "resultPreview": "...",
      "requiredSkills": [],
      "loadedSkills": [],
      "missingRequiredSkills": []
    }
  ]
}
```

优先记录字段：

- `label`
- `phaseTitle`
- `agentId`
- `state`
- `lastToolName`
- `lastToolSummary`
- `toolCalls`
- `durationMs`
- `promptPreview`
- `resultPreview`
- `requiredSkills`
- `loadedSkills`
- `missingRequiredSkills`

#### 任务 2：把 child agent tool events 汇总到 job metadata

当前 transcript 里有细节，但 workflow 总结果里不一定方便看到。

建议汇总：

```json
{
  "toolUsageSummary": {
    "file_read": 3,
    "file_write": 4,
    "load_skill": 2
  },
  "lastToolName": "file_write",
  "lastToolSummary": "REVIEW.md"
}
```

#### 任务 3：新增缺陷基线 diagnostic

保留一组不会调用真实 API 的单元测试，复现以下设计缺陷：

- progress 缺少必要字段时失败；
- result 内联 transcript 过大时失败；
- job artifact 缺失时失败；
- agent state 与 final result 不一致时失败。

### 1.4 涉及文件

优先检查和改造：

```text
workflow_runtime.py
workflow_store.py
workflow_child_agent.py
agent_loop.py
tests/test_workflow*.py
```

如存在 Ink bridge / UI 展示需求，再扩展：

```text
frontends/ink_bridge.py
frontends/ink-ui/src/*
```

### 1.5 验收标准

- 本地单元测试通过；
- 每个 workflow run 都能产出人类可读 progress；
- progress 不内联完整 transcript，只保留 preview；
- 可以从 final artifact 直接看出：每个 agent 在哪个 phase、用过什么工具、最终状态是什么；
- 不改变真实 API 行为，不引入额外 provider 调用。

---

## 3. 阶段三：Skill-aware Agent Options 一等化（P1）

### 2.1 目标

把 skills 从“system prompt 里的可选 listing”升级为 workflow 可声明、runtime 可验证、artifact 可审计的一等能力。

这是 GA 学习 Claude Code workflow 的最关键一步之一。

### 2.2 要解决的问题

对应缺陷：

- DEFECT-05：skills 仅软提示，不是 workflow 约束；
- DEFECT-06：tools_schema 裁剪会静默禁用 skills；
- DEFECT-07：skill 使用没有进入验收指标。

### 2.3 设计原则

优先实现确定性方案：

```text
required skills 由 runner 预加载 / 注入，而不是完全依赖模型主动调用 load_skill。
```

原因：工程 workflow 需要可重复性，不能把“是否调用 skill”完全交给模型自由发挥。

### 2.4 推荐 DSL

支持：

```js
await agent('读取 CONTRACT.md，只写测试，不写实现', {
  label: 'tests',
  role: 'test-writer',
  skills: ['using-superpowers', 'test-driven-development'],
  requireSkills: true
})
```

字段语义：

| 字段 | 含义 |
|---|---|
| `skills` | 希望该 agent 使用的 skill 列表 |
| `requireSkills` | true 时缺失 skill 应导致 preflight/job failed |
| `role` | 可选，用于模板层自动映射默认 skills |
| `skillMode` | 可选，`preload` / `tool-call` / `optional` |

### 2.5 主要任务

#### 任务 1：扩展 workflow `agent()` options schema

在 workflow runtime 中接受：

```text
skills: string[]
requireSkills: boolean
role: string
skillMode: string
```

非法输入应 preflight fail，例如：

- `skills` 不是数组；
- skill 名称为空；
- `requireSkills=true` 但找不到 skill；
- skill 名称不在 discovery 列表里。

#### 任务 2：runner 预加载 required skills

建议第一版采用 `preload`：

1. runtime 把 `skills` 写入 job spec；
2. child runner 调用 `skills_runtime.load_skill_content()`；
3. 将完整 SKILL.md 内容注入 child system prompt 或首轮上下文；
4. 记录 `skill_preloaded` 事件；
5. final artifact 记录 `loadedSkills`。

#### 任务 3：保证 `load_skill` 不会被静默裁掉

如果采用 tool-call 模式，或允许模型主动再加载 skill：

- 当 `skills` / `requireSkills` 存在时，`tools_schema_factory` 必须包含 `load_skill`；
- 如果不包含，应明确失败：

```text
WorkflowPreflightError: required skills need load_skill tool, but load_skill is not available
```

#### 任务 4：记录 skill 事件

每个 job artifact 增加：

```json
{
  "requiredSkills": ["test-driven-development"],
  "loadedSkills": ["using-superpowers", "test-driven-development"],
  "missingRequiredSkills": [],
  "skillEvents": [
    {
      "type": "skill_preloaded",
      "name": "test-driven-development",
      "source": "claude",
      "pathRef": ".../test-driven-development/SKILL.md"
    }
  ]
}
```

workflow 汇总增加：

```json
{
  "skillUsageSummary": {
    "using-superpowers": 3,
    "test-driven-development": 2,
    "systematic-debugging": 1
  },
  "requiredSkillFailures": []
}
```

### 2.6 涉及文件

```text
workflow_runtime.py
workflow_child_agent.py
skills_runtime.py
ga.py
assets/tools_schema.json
tests/test_workflow*.py
tests/test_skills*.py
```

### 2.7 验收标准

- `agent(..., { skills: [...], requireSkills: true })` 可执行；
- required skill 缺失时 workflow 失败且错误明确；
- required skill 存在时 job artifact 记录 `loadedSkills`；
- `tools_schema_factory` 裁掉 `load_skill` 时不会静默通过；
- 不读取、不打印、不提交真实密钥；
- 新增单元测试覆盖 preload、missing、schema-cropped 三类场景。

---

## 4. 阶段四：受控 Test Gate 一等化（P1）

### 3.1 目标

把测试执行从“workflow 结束后的外部 harness 检查”迁移到 workflow 内部的受控 gate，让测试失败能反馈给 repair agent。

这是修复真实 E2E 失败的核心能力。

### 3.2 要解决的问题

对应缺陷：

- DEFECT-02：缺少 TDD red/green/refactor gate；
- DEFECT-03：review 不能驱动 repair；
- DEFECT-04：测试结果没有进入 workflow 上下文。

### 3.3 推荐 DSL

第一版只做 Python unittest，避免任意 shell：

```js
const red = await runPythonUnittest(args.workspace, {
  pattern: 'test_*.py',
  timeoutMs: 30000
})

if (red.passed) {
  throw new Error('TDD red phase failed: tests unexpectedly passed')
}
```

返回结构：

```json
{
  "type": "python_unittest_result",
  "passed": false,
  "returncode": 1,
  "stdout": "...",
  "stderr": "...",
  "durationMs": 1234,
  "commandKind": "python_unittest",
  "cwd": "...",
  "truncated": false
}
```

### 3.4 安全边界

必须避免引入任意命令执行能力。

第一版限制：

- 只允许 `python -m unittest discover`；
- `workspace` 必须在允许的 sandbox 根目录下；
- 禁止 `..` 越界；
- timeout 必须有上限；
- stdout/stderr 截断；
- 不允许传入任意 shell 字符串；
- 不允许访问 `mykey.py` / `mykey.json` / `mcp.json`。

### 3.5 主要任务

#### 任务 1：实现 host-side `runPythonUnittest`

在 workflow runtime 中新增受控 helper。

#### 任务 2：将测试结果写入 artifact

建议自动写：

```text
TEST_RED_RESULT.json
TEST_GREEN_RESULT.json
TEST_FAILURES.txt
```

其中 `TEST_FAILURES.txt` 可由 repair agent 读取。

#### 任务 3：测试 gate 事件进入 workflow journal

新增事件：

```text
workflow_test_gate_started
workflow_test_gate_completed
workflow_test_gate_failed
```

#### 任务 4：新增单元测试

覆盖：

- passing tests；
- failing tests；
- timeout；
- workspace 越界被拒绝；
- stdout/stderr 截断；
- gate result 写入 artifact。

### 3.6 涉及文件

```text
workflow_runtime.py
workflow_store.py
tests/test_workflow*.py
```

如果已有 sandbox / permission 模块，也应复用。

### 3.7 验收标准

- workflow script 可调用 `runPythonUnittest`；
- 测试失败不会只留在外部 harness，而会进入 workflow result；
- repair agent 可以读取 `TEST_FAILURES.txt`；
- 所有 gate 都有结构化 artifact；
- 单元测试覆盖安全边界。

---

## 5. 阶段五：TDD Workflow Template 与 Repair Loop（P1/P2）

### 4.1 目标

把正确的工程方法论固化为模板，避免用户或未来 agent 再写出：

```text
design -> parallel(implementation, tests) -> review -> final
```

这种不符合 TDD 的开发流程。

### 4.2 要解决的问题

对应缺陷：

- DEFECT-01：开发型 workflow 允许错误并行结构；
- DEFECT-02：缺少 TDD red/green/refactor gate；
- DEFECT-03：review 不能驱动 repair；
- DEFECT-09：缺少工程 workflow 模板层。

### 4.3 标准 TDD 模板

内置模板建议命名：

```text
tdd-python-package
```

固定流程：

```text
Contract
→ Tests
→ Red Gate
→ Implementation
→ Green Gate
→ Repair Loop
→ Review
→ Final Verification
→ Final
```

### 4.4 推荐 workflow skeleton

```js
phase('Contract')
await agent('写 CONTRACT.md，明确 API、边界和验收标准', {
  label: 'contract',
  role: 'contract',
  skills: ['using-superpowers', 'writing-plans'],
  requireSkills: true
})

phase('Tests')
await agent('读取 CONTRACT.md，只写失败测试，不写实现', {
  label: 'tests',
  role: 'test-writer',
  skills: ['using-superpowers', 'test-driven-development'],
  requireSkills: true
})

phase('Red Gate')
const red = await runPythonUnittest(args.workspace)
if (red.passed) throw new Error('Red gate failed: tests unexpectedly passed')

phase('Implementation')
await agent('读取 CONTRACT.md 和失败测试，写最小实现', {
  label: 'implementation',
  role: 'implementation',
  skills: ['using-superpowers', 'test-driven-development'],
  requireSkills: true
})

phase('Green Gate')
let green = await runPythonUnittest(args.workspace)

phase('Repair Loop')
let rounds = 0
while (!green.passed && rounds < 2) {
  await writeWorkflowArtifact('TEST_FAILURES.txt', green.stderr || green.stdout)
  await agent('读取 TEST_FAILURES.txt、源码和测试，修复失败', {
    label: `repair-${rounds + 1}`,
    role: 'repair',
    skills: ['using-superpowers', 'systematic-debugging'],
    requireSkills: true
  })
  green = await runPythonUnittest(args.workspace)
  rounds++
}
if (!green.passed) throw new Error('Green gate failed after repair loop')

phase('Review')
await agent('审查实现、测试和 CONTRACT.md，输出 REVIEW.md', {
  label: 'review',
  role: 'review',
  skills: ['using-superpowers', 'requesting-code-review', 'verification-before-completion'],
  requireSkills: true
})

phase('Final Verification')
const finalTest = await runPythonUnittest(args.workspace)
if (!finalTest.passed) throw new Error('Final verification failed')

return {
  marker: 'GA_TDD_WORKFLOW_DONE',
  testsPassed: true,
  repairRounds: rounds
}
```

### 4.5 并行规则

模板层应明确：

允许并行：

- 需求风险分析；
- edge case brainstorm；
- 安全审计；
- 代码结构 map；
- 多角度 review。

禁止并行：

- 最终 tests 与 implementation；
- repair 与 retest；
- review 与 final verification；
- contract 尚未稳定时直接生成最终实现。

### 4.6 主要任务

#### 任务 1：新增模板注册机制

可以先从简单方式开始：

```text
workflows/templates/tdd-python-package.js
```

或 Python 侧内置模板字符串。

#### 任务 2：新增 template 参数

支持：

```text
workspace
packageName
contractPrompt
maxRepairRounds
```

#### 任务 3：模板内置 role -> skill 映射

默认映射：

| role | skills |
|---|---|
| `contract` | `using-superpowers`, `writing-plans` |
| `test-writer` | `using-superpowers`, `test-driven-development` |
| `implementation` | `using-superpowers`, `test-driven-development` |
| `repair` | `using-superpowers`, `systematic-debugging` |
| `review` | `using-superpowers`, `requesting-code-review`, `verification-before-completion` |
| `final-verification` | `verification-before-completion` |

#### 任务 4：新增真实 E2E diagnostic

新建 Level 1.1 真实 E2E：

```text
GA_RUN_REAL_WORKFLOW_E2E=1
GA_REAL_API_CONFIG=native_oai_config
GA_REAL_API_EXPECTED_NAME=gpt-native
GA_REAL_API_EXPECTED_MODEL=gpt-5.5
```

验收：

- contract 先生成；
- tests 先生成；
- Red gate 失败；
- implementation 后 Green；
- repair loop 至少在一个带缺陷 fixture 中能修复；
- required skills 被记录；
- final unittest passed；
- `secretScan=[]`；
- artifacts/transcripts 不被内联提交。

### 4.7 涉及文件

```text
workflow_runtime.py
workflow_child_agent.py
workflow_store.py
skills_runtime.py
tests/test_workflow*.py
tests/p8_real_api_e2e.py
```

可能新增：

```text
workflows/templates/tdd-python-package.js
docs/P8-tdd-workflow-template.md
```

### 4.8 验收标准

- 可以通过模板启动 TDD workflow；
- 模板不会并行最终 tests 与 implementation；
- review 后存在 repair/retest 闭环；
- 真实或模拟测试失败能进入 repair agent 上下文；
- 至少一组本地单元测试验证 repair loop 控制流。

---

## 6. 阶段六：Secret Scan 分级与 Fixture-aware 优化（P2）

### 5.1 目标

保持真实 secret 检测严格，同时减少 URL 脱敏测试 fixture 造成的误报。

### 5.2 要解决的问题

对应缺陷：

- DEFECT-08：secret scan 对 URL 脱敏 fixture 误报。

### 5.3 设计原则

不能为了减少误报而放松真实密钥检测。

应分级：

```text
confirmed_secret     -> 失败
suspicious_secret    -> 失败或人工确认
fixture_placeholder  -> 记录 warning，不阻断
ignored_binary_cache -> 跳过
```

### 5.4 主要任务

#### 任务 1：跳过缓存和二进制派生文件

跳过：

```text
__pycache__/
*.pyc
*.pyo
*.cache
```

#### 任务 2：识别 placeholder/demo 值

例如：

```text
REDACTED
<redacted>
example
placeholder
dummy
fake-token
not-a-real-key
```

但以下仍应失败：

```text
sk-...
sk-ant-...
Bearer <长随机串>
JWT 三段式 token
高熵长字符串
真实 provider key pattern
```

#### 任务 3：输出分级报告

建议：

```json
{
  "secretScan": {
    "confirmed": [],
    "suspicious": [],
    "fixturePlaceholders": [],
    "ignored": []
  }
}
```

### 5.5 验收标准

- 真实 key pattern 继续失败；
- URL 参数名 `api_key` / `token` 搭配 placeholder 值不阻断；
- `.pyc` 不参与扫描；
- E2E 的 `secretScan=[]` 或 `confirmed=[]` 语义明确。

---

## 7. 阶段七：Schema 降级、错误恢复与 Workflow Robustness（P2）

### 6.1 目标

让 schema failure、provider anomaly、child agent partial failure 不会直接把整个工程流程变成不可诊断状态。

### 6.2 背景

Claude Code 样本中存在“结构化输出失败后改为 text workflow”的实践。这说明 schema 对规划/审计很有用，但不能成为唯一成功路径。

### 6.3 主要任务

#### 任务 1：schema failure 进入 workflow issue

记录：

```json
{
  "issues": [
    {
      "type": "schema_validation_failed",
      "agentLabel": "phase-plan",
      "retryable": true,
      "fallbackUsed": "text"
    }
  ]
}
```

#### 任务 2：支持 schema -> text fallback 策略

agent options 可支持：

```js
agent(prompt, {
  schema: PLAN_SCHEMA,
  fallback: 'text'
})
```

或模板级配置：

```text
schemaFallback=true
```

#### 任务 3：provider anomaly 诊断标准化

当真实 provider 出现 chunked response / premature close / timeout：

- 标记为 provider anomaly；
- 不误判为 workflow logic failure；
- artifact 中记录可复现上下文但不包含 secret。

### 6.4 验收标准

- schema 验证失败能被记录；
- fallback 路径可测；
- provider 异常与 workflow 缺陷区分清晰；
- final report 中能看到 failure classification。

---

## 8. 阶段八：工程 Workflow 模板库与 UI 复盘（P3）

### 7.1 目标

把底层 DSL 封装成用户真正会用、且默认正确的工程 workflow 模板。

### 7.2 模板优先级

第一批模板建议：

| 优先级 | 模板 | 目的 |
|---|---|---|
| P1 | `tdd-python-package` | 修复当前真实 E2E 暴露的核心问题 |
| P1 | `bugfix-with-regression-test` | 真实维护场景最常见 |
| P2 | `review-repair-retest` | 将 review 变成闭环 |
| P2 | `research-then-implement` | 防止没调研就写代码 |
| P3 | `multi-agent-code-review` | 多视角审查并综合 |

### 7.3 UI / CLI 展示

在 CLI / Ink UI 中展示：

```text
Phase: Tests
  agent_2 tests succeeded
  skills: using-superpowers, test-driven-development
  last tool: file_write test_url_utils.py

Gate: Red
  passed=false expected=false duration=1.2s

Phase: Repair Loop
  repair-1 succeeded
  gate passed=true
```

### 7.4 主要任务

- workflow 模板列表命令；
- 模板参数说明；
- workflow progress UI；
- skill usage summary UI；
- test gate result UI；
- failure classification UI。

### 7.5 验收标准

- 用户能选择模板而不是手写裸 JS workflow；
- UI 能看出每个 agent 的 role、skill、gate 结果；
- workflow 失败时能快速定位是 skill 缺失、test gate、provider anomaly 还是 agent 失败。

---

## 9. 阶段九：真实 API 回归矩阵与质量门禁（P3/P4）

### 8.1 目标

建立稳定的 opt-in 真实 API 回归体系，持续验证 GA workflow 是否真的能完成工程任务。

### 8.2 回归层级

| Level | 名称 | Provider | MCP | 目标 |
|---|---|---|---|---|
| L0 | mock workflow unit | 否 | 否 | 快速验证 runtime 控制流 |
| L1 | local sandbox TDD | 否 | 否 | 验证 TDD template / test gate |
| L1.1 | real gpt-5.5 TDD sandbox | 是 | 否 | 验证真实 child agent 工程能力 |
| L2 | real gpt-5.5 + MCP research report | 是 | 是 | 验证 MCP + report workflow |
| L3 | real bugfix workflow | 是 | 可选 | 验证真实 repair/retest 闭环 |

### 8.3 真实 API 执行约束

必须继续保持：

```text
GA_RUN_REAL_API_E2E=1
GA_RUN_REAL_WORKFLOW_E2E=1
GA_RUN_REAL_MCP_E2E=1  # 仅 MCP 测试需要
```

并遵守：

- 不读取 `mykey.py` / `mykey.json` / `mcp.json`；
- 不打印 API key；
- 不提交真实 API artifacts/transcripts/logs；
- 不把 secret 放入命令行参数；
- `secretScan.confirmed=[]` 才能通过。

### 8.4 质量门禁

真实 E2E 通过标准：

```text
profileOk=true
expected provider/model matched
workflow status=succeeded
all required agents succeeded
requiredSkills loaded
red gate observed
green gate passed
repair loop works when needed
final unittest passed
secretScan.confirmed=[]
artifact isolation passed
transcript not inlined in final result
marker found
```

### 8.5 验收标准

- 真实 E2E 可以按 opt-in 手动运行；
- 失败报告能区分 runtime / provider / workflow-methodology / generated-code-quality；
- 每次真实 E2E 后更新 docs，但不提交敏感 artifacts。

---

## 9. 推荐实施顺序

### Milestone A：工具继承模型校正

优先级：最高。

包含：

1. 真实 workflow child agent 默认继承完整 static tools + `load_skill` + MCP discovered tools；
2. 移除真实 E2E / diagnostic 中 file-only / selected-only schema 裁剪；
3. 将安全控制统一收敛到 permission profile、sandbox、MCP allowlist/denylist、approval 和 transcript 审计；
4. 保留 minimal schema 仅用于局部单元测试和负向测试。

完成后收益：执行 GA workflow 的 agent 不再被人为降级，能够像 Claude Code workflow agent 一样真正承担任务，并按需使用 skills 与 MCP。

### Milestone B：可观测性安全网

包含：

1. `workflowProgress`；
2. tool usage summary；
3. skill metadata 字段先占位；
4. workflow artifact 结构测试。

完成后收益：所有后续改造都有可复盘证据。

### Milestone C：Skill-aware runner

包含：

1. `agent({ skills, requireSkills, role })`；
2. required skill preload；
3. skill artifact / summary；
4. tools_schema 裁剪保护。

完成后收益：GA 能真正使用 Claude/Codex/Superpowers skills，而不是只把它们列出来。

### Milestone D：Test Gate

包含：

1. `runPythonUnittest`；
2. gate result artifact；
3. `TEST_FAILURES.txt`；
4. gate events / tests。

完成后收益：测试结果进入 workflow 闭环。

### Milestone E：TDD Template + Repair Loop

包含：

1. `tdd-python-package`；
2. Red/Green/Repair/Final Verification；
3. role -> skill 默认映射；
4. L1.1 sandbox E2E。

完成后收益：修复真实 E2E 暴露的核心方法论缺陷。

### Milestone F：Secret Scan / Schema Fallback / UI

包含：

1. fixture-aware secret scan；
2. schema fallback；
3. failure classification；
4. CLI/Ink progress 展示。

完成后收益：真实工作流更稳、更适合长期使用。

### Milestone G：模板库与真实回归矩阵

包含：

1. `bugfix-with-regression-test`；
2. `review-repair-retest`；
3. `research-then-implement`；
4. opt-in real API regression matrix。

完成后收益：GA workflow 从单个实验能力升级为工程产品能力。

---

## 10. 不建议优先做的事

### 10.1 不建议先扩展更多底层 DSL 语法

原因：当前底层 `phase/log/agent/parallel/pipeline` 已够用，真正缺的是工程语义和质量门禁。

### 10.2 不建议先追求复杂多 provider 真实 E2E

原因：当前单 provider `gpt-5.5` 已暴露方法论问题。应先修复 TDD / skill / gate，再扩展 provider 矩阵。

### 10.3 不建议依赖 prompt 让 agent 自觉遵守 TDD

原因：真实 E2E 已证明 prompt 约束不足。必须通过模板和 gate 固化流程。

### 10.4 不建议只做 UI，不做 artifact

原因：UI 应展示结构化事实，而不是从日志里猜。应先补 artifact，再做 UI。

---

## 11. 最小可行版本（MVP）定义

如果只做最小闭环，建议 MVP 包含：

```text
1. workflow child agent 默认继承完整工具能力，不再被真实 E2E 降级成 file_read/file_write-only
2. agent({ skills, requireSkills }) + required skill preload
3. runPythonUnittest(workspace)
4. tdd-python-package template
5. repair loop 最多 2 轮
6. workflowProgress + skillUsageSummary
7. L1.1 真实 gpt-5.5 TDD sandbox E2E
```

MVP 完成的判断标准：

```text
GA 可以用真实 gpt-5.5 子智能体，在 sandbox 中按 TDD 顺序：
先写 contract，
再写测试，
确认红灯，
再写实现，
确认绿灯，
必要时 repair，
最后 review / final verification，
并且 artifact 能证明 required skills 与 MCP/tool 能力真实参与了流程。
```

---

## 12. 最终目标状态

完成上述阶段后，GA workflow 应具备以下特征：

```text
skill-aware
TDD-aware
quality-gated
repair-loop-enabled
artifact-contract-driven
human-reviewable-progress
real-api-regression-tested
```

这时 GA workflow 才不只是“能运行多 agent”，而是能稳定支撑真实软件开发任务的工程工作流系统。
