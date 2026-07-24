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
P1：固化 Claude Code-style optional skill awareness，并把 skill 使用写入 progress
P2：完善 artifact、Claude Code-style 高置信 secret hygiene、schema 降级和错误恢复
P3：实现动态 Workflow Planner / Compiler，让 GA 根据任务语义生成受限 workflow script
P4：建立真实 API 回归矩阵，并把常见 pattern 作为 planner 的可选兜底形状沉淀
```

---

## 1. 阶段一：Workflow child agent 工具继承模型校正（P0）【已完成：50803dc】

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

### 1.8 完成记录与真实 E2E 验证

本阶段已在提交 `50803dc fix(workflow): 恢复子智能体完整工具继承能力` 中完成首轮实现，并通过后续真实 E2E 验证。

已完成的实现要点：

- `NativeGPTChildAgentRunner._load_tools_schema()` 不再因为 `tools_schema_factory` 存在而直接跳过默认工具；
- 默认加载完整 `assets/tools_schema.json`；
- 默认注入 MCP discovery 发现的工具；
- legacy zero-arg `tools_schema_factory` 仍兼容，但会补回 `file_read`、`load_skill` 和 discovered MCP tools；
- 新增 `capability_snapshot` transcript event，记录 `toolSchemaCount`、`toolNames`、`loadSkillAvailable`、`mcpToolNames`、`mcpDiscovery`；
- MCP discovery 异常不再完全静默，而是记录 redacted capability 状态；
- 真实 E2E harness 不再用 file-only / selected-only schema 代表真实 workflow child agent 能力。

真实多 agent 复杂编码 E2E 验证结果：

```text
configName=native_oai_config
profile=gpt-native / gpt-5.5
passed=true
status=succeeded
finalStatus=succeeded
startedJobIds=[agent_1, agent_2, agent_3, agent_4, agent_5]
agent_completed=5
secretScan=[]
unitTest=Ran 10 tests OK
markerFound=true
```

该 E2E 证明：

- 5 个真实 child agent 全部 `loadSkillAvailable=true`；
- 每个 agent 的 `mcpDiscovery.status=ok`，`mcpToolCount=16`；
- 所有 agent 均未出现 `tool_denied`；
- `load_skill` 被实际调用 6 次；
- role-sensitive skill 选择已经出现：

```text
agent_1 design         -> using-superpowers, brainstorming
agent_2 implementation -> test-driven-development
agent_3 tests          -> test-driven-development
agent_4 review         -> requesting-code-review
agent_5 final          -> using-superpowers
```

这说明修复后 GA workflow child agent 不只是“看得到 skills”，而是可以在真实复杂工程任务中实际加载并使用 skills。

真实 MCP deep research E2E 验证结果：

```text
任务=传闻美国要限制对海力士和三星的买入导致股票跳水，deep research 找传闻来源
configName=native_oai_config
profile=gpt-native / gpt-5.5
selectedMcpTool=mcp__tavily__tavily_research
passed=true
status=succeeded
finalStatus=succeeded
mcpCalled=true
mcpReturned=true
tavily_research 调用 3 次
reportFileExists=true
reportLength=7292
secretScan=[]
deniedTools=[]
```

该 E2E 的 `capability_snapshot` 显示：

```text
toolSchemaCount=27
loadSkillAvailable=true
fileReadAvailable=true
mcpDiscovery.status=ok
mcpDiscovery.injectedToolCount=16
mcpToolCount=16
```

这说明修复后 GA workflow child agent 不只是“看得到 MCP tools”，而是可以在真实 API 工作流中实际调用 MCP deep research，并把结果用于 Markdown 报告产出。

因此阶段一可视为完成。下一步应进入阶段二：`workflow-progress.json` / progress artifact，把这些能力证据从 transcript 中提取到更易于 UI、验收和复盘使用的结构化 artifact。

---

## 2. 阶段二：Workflow 可观测性与缺陷基线固化（P0）【已完成：07ce3dd】

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

实际实现于提交 `07ce3dd feat(workflow): 生成工作流进度快照`。

实现选择：采用独立 artifact `workflow-progress.json`，并在 `final-result.json` 中写入：

```json
{
  "workflowProgressRef": "workflow-progress.json"
}
```

这样 UI、E2E harness 和人工复盘可以稳定读取结构化进度，而不需要解析完整 child transcript，也不会让 final result 内联大块 transcript 内容。

当前 `workflow-progress.json` 顶层结构为：

```json
{
  "runId": "wf_test",
  "sessionId": "session_test",
  "status": "succeeded",
  "workflowProgress": [
    {
      "type": "workflow_agent",
      "index": 1,
      "agentId": "agent_1",
      "jobId": "agent_1",
      "label": "research",
      "phase": "Research",
      "phaseTitle": "Research",
      "state": "succeeded",
      "resultRef": "agents/agent_1/result.json",
      "transcriptRef": "agents/agent_1/transcript.jsonl",
      "lastToolName": "mcp__tavily__tavily_research",
      "lastToolSummary": "success",
      "toolCalls": ["load_skill", "mcp__tavily__tavily_research"],
      "allowedTools": ["mcp__tavily__tavily_research"],
      "deniedTools": [],
      "loadedSkills": ["using-superpowers"],
      "missingRequiredSkills": [],
      "capability": {
        "loadSkillAvailable": true,
        "fileReadAvailable": true,
        "mcpDiscoveryStatus": "ok",
        "mcpDiscoveryInjectedToolCount": 16,
        "mcpToolCount": 16
      },
      "capabilities": {
        "loadSkillAvailable": true,
        "fileReadAvailable": true,
        "mcpDiscoveryStatus": "ok",
        "mcpDiscoveryInjectedToolCount": 16,
        "mcpToolCount": 16
      },
      "tokenUsage": {
        "input_tokens": 10,
        "output_tokens": 20
      },
      "promptPreview": "...",
      "resultPreview": "...",
      "error": null
    }
  ]
}
```

阶段二实现要点：

- `WorkflowStore.write_workflow_progress(run)` 生成 `workflow-progress.json`；
- 从 `agents/<job_id>/transcript.jsonl` 提取：
  - `toolCalls`
  - `loadedSkills`
  - `capability_snapshot` 摘要
  - `allowedTools` / `deniedTools`
  - `lastToolName` / `lastToolSummary`
- progress 不内联完整 `transcriptEvents`；
- progress 不复制 verbose assistant transcript；
- progress 不复制 `load_skill` 返回的 skill content；
- progress 不复制纯字符串 `tool_result` 正文，避免把大块工具输出或敏感内容带入进度 artifact；
- `project_resume_state()` 会把 interrupted/stale 状态同步写入 progress；
- `AgentScheduler.register_agent()` / `register_cached_agent()` 后会立即写 queued/cached progress；
- scheduler `tick()` 后写最新 progress；
- runtime done/error/killed 路径会兜底写 progress，并在 final result 中写 `workflowProgressRef`。

新增/更新测试覆盖：

```text
tests/test_workflow_store.py
tests/test_workflow_scheduler.py
tests/test_workflow_runtime.py
```

验证结果：

```text
python -m unittest tests.test_workflow_store tests.test_workflow_scheduler tests.test_workflow_runtime tests.test_workflow_child_agent
Ran 92 tests
OK

python -m unittest discover -s tests
Ran 375 tests in 52.452s
OK (skipped=1)
```

因此阶段二可视为完成。下一步应进入阶段三：`Claude Code 风格的可选 Skill 感知（P1）`，即不新增 required/preload DSL，而是固化 Claude Code dynamic workflow 的真实默认行为：child agent 默认继承完整 skill 能力、在 system prompt 中看到 skill index、按任务语义自主调用 `load_skill`，并把调用情况写入 transcript 与 `workflow-progress.json`。

---

## 3. 阶段三：Claude Code 风格的可选 Skill 感知（P1）【已完成】

### 2.1 目标

把 GA workflow 的 skill 行为调整为与 Claude Code dynamic workflow 真实模式一致：

```text
subagent 继承完整 skill 能力；
system prompt / tool surface 里能看到 skill；
subagent 根据任务语义自行决定是否调用 Skill；
调用后 workflowProgress / transcript 能看到使用情况；
不调用 skill 不视为失败。
```

该阶段不实现 `skills` / `requireSkills` / `role` / `skillMode` DSL，也不做 host-side preload。重点是把当前已经基本具备的 optional skill 行为固化成稳定、可测试、可复盘的 baseline。

### 2.2 要解决的问题

对应缺陷：

- DEFECT-05：skills 目前主要是 soft prompt，需要明确固化为 Claude Code-style optional 行为；
- DEFECT-06：tools_schema 裁剪曾静默禁用 skills，阶段一已修复，但需要回归测试继续锁定；
- DEFECT-07：skill 使用需要进入验收指标和 progress artifact。

### 2.3 设计原则

采用 **Claude Code-style optional**，而不是 required/preload：

```text
默认可见、默认可用、自主调用、调用可审计、不调用不失败。
```

明确不做：

```text
不新增 agent({ skills: [...] })；
不新增 requireSkills；
不新增 role -> skill 自动映射；
不新增 skillMode；
不做 runner 预加载完整 SKILL.md；
不因 agent 没有调用 skill 而失败。
```

原因：本会话中 Claude Code 自己调用 dynamic workflow 时，subagent 的真实行为并不是 host 强制预加载 skill，而是：

1. workflow subagent 默认有 Skill/tool 能力；
2. system prompt 暴露可用 skill index；
3. subagent 在规划、综合、debug、review 等任务中主动调用合适 skill；
4. workflowProgress / transcript 记录其使用情况。

### 2.4 当前 GA 已具备的基础

阶段一和阶段二已经提供了 optional skill baseline 的大部分基础：

- `workflow_child_agent.py` 默认加载完整静态 tools，并保留 `load_skill`；
- `workflow_child_agent.py` 的 `_build_system_prompt()` 会调用 `skills_runtime.build_skill_prompt()`，把 `[Available Skills]` listing 注入 child system prompt；
- child transcript 已记录：
  - `tool_call(toolName=load_skill)`
  - `tool_result(toolName=load_skill)`
- `capability_snapshot` 已记录：
  - `loadSkillAvailable`
  - `fileReadAvailable`
  - MCP discovery 摘要
- `workflow-progress.json` 已记录：
  - `toolCalls`
  - `loadedSkills`
  - `capability` / `capabilities`
  - `lastToolName`
  - `lastToolSummary`

### 2.5 主要任务

#### 任务 1：锁定 child agent 默认 skill 可见性

新增或强化测试，确保 workflow child agent 默认 system prompt 包含 skill listing 指令：

```text
[Available Skills]
When a user task matches one of these skills, call load_skill...
The listing is only an index; load the full SKILL.md before following it.
```

测试应 mock `skills_runtime.build_skill_prompt()`，避免依赖用户本地真实 skill 目录。

#### 任务 2：锁定 `load_skill` 默认可用且不会被裁掉

继续保留并强化阶段一测试：

- 默认 tools schema 包含 `load_skill`；
- legacy `tools_schema_factory` 不能静默裁掉 `load_skill`；
- `capability_snapshot.loadSkillAvailable=true` 可进入 transcript；
- `workflow-progress.json.capability.loadSkillAvailable=true` 可被读取。

#### 任务 3：增强 progress 的 skill 摘要

在 `workflow-progress.json` 每个 agent entry 中补充：

```json
{
  "skillToolCalls": 1,
  "skillLoadEvents": [
    {
      "name": "using-superpowers",
      "status": "success",
      "source": "claude",
      "path": ".../using-superpowers/SKILL.md",
      "baseDir": ".../using-superpowers",
      "allowedTools": []
    }
  ]
}
```

要求：

- `skillToolCalls` 从 `toolCalls` 中 `load_skill` 的次数派生；
- `skillLoadEvents` 从 `tool_result(toolName=load_skill)` 派生；
- 只保留摘要字段；
- 不复制 `content`；
- 不复制完整 SKILL.md；
- 不复制 verbose tool result 正文。

#### 任务 4：明确 optional 行为：不调用 skill 不失败

新增测试覆盖普通 workflow agent：

```json
{
  "capability_snapshot": {
    "loadSkillAvailable": true
  },
  "toolCalls": [],
  "loadedSkills": [],
  "skillToolCalls": 0,
  "skillLoadEvents": [],
  "state": "succeeded"
}
```

断言：

```text
loadSkillAvailable=true 但 loadedSkills=[] 时，job 仍然 succeeded。
```

这点是本阶段与 required skill 方案的核心区别。

#### 任务 5：文档和真实 E2E 复盘口径调整

后续真实 E2E 不应检查“每个 agent 必须调用 skill”，而应检查：

```text
1. 每个 agent 都能看到 load_skill；
2. 适合使用 skill 的 agent 会自然调用 skill；
3. workflow-progress.json 能记录调用结果；
4. 未调用 skill 的 agent 不视为失败。
```

### 2.6 涉及文件

优先改动：

```text
workflow_store.py
tests/test_workflow_store.py
tests/test_workflow_child_agent.py
docs/GA_workflow_defect_optimization_plan.md
```

通常不需要改：

```text
workflow_js_worker.js
workflow_runtime.py
workflow_scheduler.py
workflow_models.py
```

除非测试发现 progress 写入链路还缺少字段传递。

### 2.7 验收标准

- workflow child agent 默认 system prompt 包含 skill listing；
- workflow child agent 默认 tools schema 包含 `load_skill`；
- `capability_snapshot` 和 `workflow-progress.json` 能体现 `loadSkillAvailable=true`；
- agent 调用 `load_skill` 后，progress 记录：
  - `loadedSkills`
  - `skillToolCalls`
  - `skillLoadEvents`
- `skillLoadEvents` 不包含完整 skill content；
- agent 不调用 `load_skill` 时 job 不失败；
- 单元测试通过；
- 不读取、不打印、不提交真实密钥。

### 2.8 完成记录与验证结果

本阶段按 TDD 小步实现，保持 Claude Code-style optional 模式，没有新增 `skills` / `requireSkills` / `role` / `skillMode` DSL，也没有实现 host-side skill preload。

实现内容：

- `workflow-progress.json` 每个 agent entry 新增：
  - `skillToolCalls`
  - `skillLoadEvents`
- `skillToolCalls` 由 `toolCalls` 中的 `load_skill` 调用次数派生；
- `skillLoadEvents` 从 `tool_result(toolName=load_skill)` 派生，只记录摘要字段：
  - `name`
  - `status`
  - `source`
  - `path`
  - `baseDir`
  - `allowedTools`
- progress 明确不复制 `load_skill` 返回的完整 `content` / SKILL.md 正文；
- 新增测试锁定 optional 行为：`loadSkillAvailable=true` 但 agent 未调用 `load_skill` 时，job 仍然可以 `succeeded`；
- 新增测试锁定 child agent system prompt 会包含 optional skill listing。

TDD 红灯记录：

```text
python -m unittest tests.test_workflow_store.WorkflowStoreTest.test_workflow_progress_records_skill_load_events_without_skill_content

KeyError: 'skillToolCalls'
```

绿灯与回归验证：

```text
python -m unittest tests.test_workflow_store tests.test_workflow_child_agent
Ran 34 tests
OK

python -m unittest tests.test_workflow_store tests.test_workflow_child_agent tests.test_workflow_scheduler tests.test_workflow_runtime
Ran 95 tests
OK

python -m unittest discover -s tests
Ran 378 tests in 54.715s
OK (skipped=1)
```

真实 `gpt-5.4` E2E 验证：

```text
脚本：tests/optional_skill_real_e2e.py
命令环境：
  GA_RUN_REAL_WORKFLOW_E2E=1
  GA_REAL_API_CONFIG=native_oai_config
  GA_REAL_API_EXPECTED_NAME=gpt-native
  GA_REAL_API_EXPECTED_MODEL=gpt-5.4

结果：
  passed=true
  profileOk=true
  model=gpt-5.4
  status=succeeded
  elapsedSeconds=26.06
  finalWorkflowProgressRef=workflow-progress.json
  progressExists=true
  jobStatus=succeeded
  jobLabel=optional-skill-real-agent
  toolCalls=[load_skill, no_tool]
  loadedSkills=[test-driven-development]
  skillToolCalls=1
  skillLoadEvents[0].name=test-driven-development
  skillLoadEvents[0].status=success
  loadSkillAvailable=true
  progressContainsSkillContent=false
  markerFound=true
```

该 E2E 证明真实 child agent 会按 optional 模式自主调用 `load_skill`，并且 `workflow-progress.json` 能稳定记录 skill 使用摘要，同时不泄露完整 SKILL.md content。

因此阶段三可视为完成。下一步应进入阶段四：`受控 Test Gate 一等化（P1）`，即让测试执行成为 workflow 内部受控 gate，而不是只依赖外部 harness 或 agent 自述。


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
await agent('写 CONTRACT.md，明确 API、边界和验收标准。若可用且适合，请先调用 load_skill 使用 planning / methodology 相关 skill。', {
  label: 'contract'
})

phase('Tests')
await agent('读取 CONTRACT.md，只写失败测试，不写实现。若可用且适合，请先调用 load_skill 使用 TDD / testing 相关 skill。', {
  label: 'tests'
})

phase('Red Gate')
const red = await runPythonUnittest(args.workspace)
if (red.passed) throw new Error('Red gate failed: tests unexpectedly passed')

phase('Implementation')
await agent('读取 CONTRACT.md 和失败测试，写最小实现。若可用且适合，请先调用 load_skill 使用 TDD / implementation 相关 skill。', {
  label: 'implementation'
})

phase('Green Gate')
let green = await runPythonUnittest(args.workspace)

phase('Repair Loop')
let rounds = 0
while (!green.passed && rounds < 2) {
  await writeWorkflowArtifact('TEST_FAILURES.txt', green.stderr || green.stdout)
  await agent('读取 TEST_FAILURES.txt、源码和测试，修复失败。若可用且适合，请先调用 load_skill 使用 debugging / repair 相关 skill。', {
    label: `repair-${rounds + 1}`
  })
  green = await runPythonUnittest(args.workspace)
  rounds++
}
if (!green.passed) throw new Error('Green gate failed after repair loop')

phase('Review')
await agent('审查实现、测试和 CONTRACT.md，输出 REVIEW.md。若可用且适合，请先调用 load_skill 使用 review / verification 相关 skill。', {
  label: 'review'
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

#### 任务 3：模板 prompt 建议使用相关 skill

模板不通过 DSL 强制 role -> skill 映射，而是在对应 agent prompt 中自然提示：

| 阶段 | 建议 skill 方向 |
|---|---|
| `Contract` | methodology / planning |
| `Tests` | TDD / testing |
| `Implementation` | TDD / implementation |
| `Repair` | debugging / repair |
| `Review` | review / verification |
| `Final Verification` | verification |

这些建议依赖 child agent 默认可见 `[Available Skills]` listing 和 `load_skill` tool。是否调用由 subagent 根据任务语义自主决定；调用情况由 `workflow-progress.json.loadedSkills`、`skillToolCalls`、`skillLoadEvents` 复盘。

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
- skill 调用情况可在 `workflow-progress.json` 中复盘，但未调用 skill 不视为失败；
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

## 6. 阶段六：Claude Code-style 高置信 Secret Hygiene（P2）

### 5.1 目标

保持真实 secret 检测严格，同时避免把 workflow transcript、prompt、tool schema、安全说明和测试 fixture 中的普通 `api_key` / `token` / `secret` 词汇误判为密钥。

阶段一后的真实 MCP deep research E2E 暴露了一个重要修正：泛化 keyword-context secret scan 不适合扫 workflow transcript。Claude Code 自身的 team memory secret scanner 使用的是 curated high-confidence rules，并明确省略 generic keyword-context rules，因为这类规则误报率高。

因此 GA workflow E2E 的 secret hygiene 应从“泛化关键词扫描”改为“高置信格式扫描 + artifact hygiene”。

### 5.2 要解决的问题

对应缺陷：

- DEFECT-08：secret scan 对 URL 脱敏 fixture 误报；
- 真实 MCP deep research E2E 中，旧 `api_key_field` 规则对 transcript 中的安全说明 / tool schema 字段误报；
- `api_key:`、`token:`、`secret:`、`password:` 这类 keyword-context 规则不适合作为 blocking E2E gate。

### 5.3 设计原则

参考 Claude Code 源码中的 secret scanner 原则：

```text
只使用高置信、特征前缀明确、低误报的规则；
省略 generic keyword-context rules；
不要因为 transcript/prompt/tool schema 中出现 token/api_key/secret 字样而阻断 workflow E2E。
```

保留 blocking 的高置信格式示例：

```text
sk-ant-api... / sk-ant-admin...
sk-proj... / sk-svcacct... / sk-admin...
github_pat_...
ghp_...
JWT 三段式 token
长 Bearer token
其他有明确 provider/token 前缀且长度足够的真实密钥格式
```

不应作为 blocking 的泛化规则：

```text
api_key: VALUE
token: VALUE
secret: VALUE
password: VALUE
x-api-key: VALUE
```

这些在 prompt、安全说明、tool schema、测试 fixture、Markdown 报告和 transcript 中都可能合法出现。

### 5.4 主要任务

#### 任务 1：移除 workflow E2E 中的 generic keyword-context blocking 规则

删除或降级以下模式：

```text
api_key_field
x_api_key
任意 token/secret/password key-value
```

如果保留，只能作为 warning / diagnostic，不得导致 `passed=false`。

#### 任务 2：只保留高置信 secret 格式

建议规则集对齐 Claude Code 思路：

```text
Anthropic API key
OpenAI project/service/admin key
GitHub PAT
JWT
长 Bearer token
常见云厂商强特征 token
```

#### 任务 3：artifact hygiene 优先于 transcript keyword scan

E2E 重点检查：

- 不读取 `mykey.py` / `mykey.json` / `mcp.json` 原文；
- 不打印 provider config；
- 不提交真实 API artifacts/transcripts/logs；
- result.json 不内联完整 transcript；
- transcript 中若出现安全说明文字，不视为 secret 泄漏。

#### 任务 4：输出语义调整

建议输出：

```json
{
  "secretHygiene": {
    "confirmed": [],
    "warnings": [],
    "scannerMode": "high-confidence-only"
  }
}
```

而不是让 broad `secretScan=[]` 成为所有 E2E 的核心能力门禁。

### 5.5 验收标准

- 真实 key pattern 继续失败；
- transcript / prompt / tool schema 中的 `api_key`、`token`、`secret` 安全说明不阻断；
- URL 参数名 `api_key` / `token` 搭配 placeholder 值不阻断；
- `.pyc` / `.pyo` 不参与扫描；
- E2E 报告中明确 `scannerMode=high-confidence-only` 或等价语义；
- secret hygiene 不再掩盖 workflow skill/MCP 能力验收结果。

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

## 8. 阶段八：动态 Workflow Planner / Compiler（P3）

### 7.1 目标

实现从自然语言任务动态生成 workflow script 的 planner/compiler，而不是让用户选择固定模板或手写裸 JS。

核心链路：

```text
用户任务
→ task classify
→ WorkflowPlan JSON
→ validate
→ render 受限 workflow script
→ approval / draft
→ execute
→ replay / learn
```

这才是 GA workflow 接近 Claude Code dynamic workflow 的关键能力：workflow 不是预置模板，而是由任务语义和上下文动态生成。

### 7.2 Claude Code 本会话证据

本会话 `C:\Users\drago\.claude\projects\D--git-codes-GenericAgent\e7319f01-e60a-4cd0-83cd-d4b15df2de82\workflows\scripts` 下的脚本显示：Claude Code dynamic workflow 是“运行时脚本 DSL”，不是静态配置。

典型结构：

```js
export const meta = {
  name: '...',
  description: '...',
  phases: [{ title: 'Collect' }, { title: 'Synthesize' }]
}

phase('Collect')
const scouts = await parallel([
  () => agent('...', { label: 'docs', phase: 'Collect', schema: DOC_SCHEMA }),
  () => agent('...', { label: 'code', phase: 'Collect', schema: CODE_SCHEMA })
])

phase('Synthesize')
const synthesis = await agent(`基于上游结果：${JSON.stringify(scouts)}`, {
  label: 'synthesis',
  phase: 'Synthesize',
  schema: SYNTHESIS_SCHEMA
})

return { scouts, synthesis }
```

观察到的关键模式：

- `export const meta` 提供 UI/approval 可展示的 workflow 名称、描述和阶段；
- `phase()` 是顺序执行的阶段栅栏；
- `agent()` 是最小执行单元，必须带 `label`，复杂任务带 `schema`；
- `parallel()` 只用于同 phase 内互不依赖的子任务；
- `pipeline()` 用于同质列表逐项处理；
- 上游结果通过 `JSON.stringify(...)` 显式传给后续综合/验证 agent；
- prompt 中会编译进任务边界，例如只读、不提交、不读 `mykey.py` / `mykey.json` / `mcp.json`、是否允许真实 API、输出格式和风险要求；
- 子智能体 transcript 和 `journal.jsonl` 是后续 replay/learn 的证据来源。

### 7.3 Planner / Compiler 架构

#### 7.3.0 Prompt-guided planner 是动态性的核心

阶段八的核心不是继续扩充 deterministic 模板，也不是把 `research` / `coding` / `review` 写成更多 `if/else`。Claude Code-style dynamic workflow 更接近：通过强系统提示 / workflow 指令把模型定位为 **planner / orchestrator**，让模型基于当前任务、仓库上下文、可用工具、MCP、skills、风险边界和历史经验，现场生成 task-specific workflow topology。

本地较旧 Claude Code coordinator 源码仍能作为旁证：`coordinatorMode.ts` 中系统提示把 Claude 定义为协调者，明确要求其调度 worker 完成 research / implementation / verification，并写入如下策略：

```text
Parallelism is your superpower.
Launch independent workers concurrently whenever possible.
When doing research, cover multiple angles.
Workers can't see your conversation. Every prompt must be self-contained.
Always synthesize findings before directing follow-up work.
```

这类设计说明“动态性”主要来自 prompt-level orchestration policy：模型被告知可用 primitives、并行边界、验证要求、worker prompt 质量要求和安全限制，然后根据任务语义现场决定拆分方式。GA 不应照搬旧源码的 coordinator 形态，但应复刻这个原则：**提示词驱动动态规划，确定性代码负责验证和编译**。

因此阶段八最终目标应拆成两层：

```text
Prompt-guided LLMWorkflowPlanner
→ 输出 WorkflowPlan JSON，不输出 JS
→ validator / repair loop
→ deterministic renderer
→ restricted workflow runtime
```

当前已实现的 deterministic `WorkflowPlanner` 是最小 MVP / fallback：它证明了 `WorkflowPlan JSON -> validate -> render -> run` 闭环，但还不是最终核心 planner。后续应将其降级为 `FallbackDeterministicPlanner` 或安全默认形状来源；真正核心应是 prompt-guided planner。

prompt-guided planner 的系统提示必须明确：

- 不选择用户可见固定模板；pattern 只能作为 few-shot 示例或 fallback；
- 根据任务语义决定 phase 数量、agent role、依赖、并行、pipeline、schema、artifacts；
- read-only research 可并行多视角，但 synthesis 必须消费上游结果；
- review 应按风险维度 fan out，并对 findings 做验证/反驳；
- coding 必须遵守 TDD 顺序，不得并行 tests 与 implementation；
- planning / mixed 任务应先 context discovery 和 design alternatives，不应直接写代码；
- prompt 必须带入安全边界：不读 `mykey.py` / `mykey.json` / `mcp.json`，不提交，除非用户明确授权；
- 高风险、external_io、 destructive、真实提交/发布类动作需要 approval gate；
- validator 报错后最多进行有限轮 repair，仍失败则保存 rejected draft 供复盘。

#### 7.3.1 Classifier

输入：

```text
用户任务
当前仓库上下文
git 状态
可用 tools / MCP / skills
用户安全约束
历史 workflow run 摘要
```

输出：

```json
{
  "taskType": "research | coding | review | debugging | planning | mixed",
  "readWriteMode": "read_only | may_write | external_io",
  "needsMcp": true,
  "needsCodeChange": false,
  "needsVerification": true,
  "riskLevel": "low | medium | high",
  "clarifyingQuestions": [],
  "constraints": ["不要读取 mykey.py", "不要提交"]
}
```

#### 7.3.2 WorkflowPlan JSON

Planner 不直接输出自由 JS，而是先输出结构化中间表示：

```json
{
  "meta": {
    "name": "research-chip-rumor",
    "description": "Research semiconductor market rumor"
  },
  "phases": [
    {
      "title": "Source Discovery",
      "agents": [
        {
          "label": "source-search",
          "prompt": "搜索并列出最早和最可信公开来源...",
          "schemaRef": "SOURCE_SCHEMA"
        }
      ]
    },
    {
      "title": "Synthesis",
      "agents": [
        {
          "label": "report-writer",
          "prompt": "基于上游 sources 写结构化中文报告...",
          "dependsOn": ["source-search"]
        }
      ]
    }
  ],
  "schemas": {
    "SOURCE_SCHEMA": {
      "type": "object",
      "required": ["sources", "claims", "risks"]
    }
  },
  "artifacts": ["sources", "synthesis"],
  "constraints": ["no_secret_files", "no_git_commit"]
}
```

#### 7.3.3 Validator

渲染前必须静态检查：

- phase 是否有序；
- `parallel` 是否只包含互不依赖节点；
- 是否存在未定义依赖；
- 是否引用未定义 schema；
- 是否试图使用禁止 helper；
- prompt 是否带入必要安全边界；
- coding 任务是否错误并行 tests / implementation；
- 是否尝试把 secret / transcript 全量写入 artifact；
- schema 失败是否有 fallback / issue 记录。

#### 7.3.4 Renderer

Renderer 由确定性代码把 `WorkflowPlan JSON` 转成受限 workflow script：

```text
WorkflowPlan JSON -> export const meta -> phase() -> agent() -> parallel()/pipeline() -> return {...}
```

模型不直接拼接任意可执行 JS。这样可以让 GA 在执行前审计 plan，并把风险收敛在 renderer 和 validator。

#### 7.3.5 Execute / Replay / Learn

执行后保留：

- 原始任务；
- classifier 输出；
- WorkflowPlan JSON；
- rendered script；
- validator 结果；
- `workflow-progress.json`；
- `journal.jsonl`；
- `agents/<job>/transcript.jsonl`；
- schema/fallback/partial failure issue。

后续 planner 可以从历史 run 中学习：

```text
任务类型 -> phase 形状 -> agent label/prompt -> schema -> failure mode -> 修正建议
```

### 7.4 最小实现切片

第一版已完成 deterministic MVP，用于证明闭环：

1. `WorkflowPlanner.plan(task_text, context) -> WorkflowDraft`；
2. 支持少量任务类型分类：
   - research
   - coding
   - review
   - planning
3. 生成 `WorkflowPlan JSON`；
4. validator 检查安全边界和 DAG 形状；
5. deterministic renderer 生成 workflow script；
6. 用 fake child runner 验证生成脚本能被 `WorkflowRuntime` 执行；
7. 生成过程写入 artifact，便于复盘；
8. opt-in GLM-5.1 真实 E2E 验证 `planner -> draft -> runtime -> progress` 链路。

下一阶段必须实现 prompt-guided dynamic planner，而不是只把 deterministic planner 接到 UI。推荐 TDD 切片：

1. **LLMWorkflowPlanner contract**
   - fake planner client 输入 task/context/tool/skill/risk 摘要；
   - 输出 `WorkflowPlan JSON`；
   - 不允许输出 JS；
   - deterministic planner 仅作为 fallback。
2. **Research 动态拓扑**
   - 输入可信度/风险调研任务；
   - 期望生成 `Source Discovery -> Credibility / Evidence Check -> Synthesis`；
   - discovery 可按来源方向 fan out；
   - synthesis 依赖上游并使用 `JSON.stringify(...)` 消费结果。
3. **Review 动态 fan-out + verification**
   - 输入“全面审查 PR 的安全、性能、测试缺口和回归风险”；
   - 期望生成 security / performance / test-gap / regression 等并行 review agent；
   - 后续 verifier / adversarial check 依赖 review；
   - final report 依赖 verified findings。
4. **Coding TDD 顺序红线**
   - 输入实现类任务；
   - 期望生成 `Understand -> Tests -> Implementation -> Verification -> Summary`；
   - `write-failing-tests` 必须先于 `implement-minimal-code`；
   - implementation 必须依赖 tests；
   - tests 与 implementation 不能在同一 independent parallel group；
   - prompt 必须明确先红灯后绿灯、不提交、运行相关测试；
   - validator 必须拒绝 `coding_tests_parallel_implementation`；
   - repair loop 必须能把错误并行结构改成顺序依赖。
5. **Planning / mixed 动态拆分**
   - 输入跨前端/后端/运行时的规划任务；
   - 期望生成 `Context Discovery -> Design Alternatives -> Risk Review -> Implementation Plan`；
   - 不应误判成 coding 并直接生成写代码 workflow。
6. **Validator-guided repair loop**
   - fake LLM 第一次返回未定义依赖 / 未定义 schema / coding 错误并行；
   - validator 返回结构化 issues；
   - repair prompt 带入 issues；
   - 第二次输出修复后的 plan；
   - 最多 2-3 轮，失败保存 rejected draft。
7. **Artifact / replay evidence**
   - 保存原始 planner prompt 摘要、LLM plan、validator issues、repair attempts、rendered script；
   - 供后续 replay/learn 从 `journal.jsonl`、agent prompt、tool calls、result summary 中抽取经验。

这一步的目标不是让 planner 一次性支持任意任务，而是证明：同一 prompt-guided planner 能根据 research / review / coding / planning 的任务语义生成不同拓扑，并用 validator/repair 保证安全边界。

### 7.4.1 Prompt-guided planner 实现进度（2026-06-18）

本阶段已按 TDD 完成 prompt-guided planner 的最小可运行切片：

1. 新增 `LLMWorkflowPlanner(client=..., fallback=..., max_repair_attempts=...)`；
2. planner client contract 固定为 `complete(messages) -> WorkflowPlan dict | JSON string`；
3. planner prompt 明确要求：
   - 只输出 `WorkflowPlan JSON`；
   - 不输出 JS；
   - `phases` 必须非空；
   - 使用 `classificationHint` 辅助区分 research / review / coding / planning / mixed；
   - review 按安全、性能、测试缺口、回归风险等维度 fan out；
   - coding 遵守 `Understand -> Tests -> Implementation -> Verification`，禁止 tests 与 implementation 并行；
   - research 可多来源并行，并包含 credibility / evidence 检查；
   - planning / mixed 先做 context discovery / design alternatives / risk review，不直接写代码；
   - agent prompt 必须包含不读 `mykey.py` / `mykey.json` / `mcp.json`、不提交等安全边界。
4. validator 失败时会把 `validatorIssues` 和 `previousPlan` 注入 repair prompt；
5. repair 成功后渲染为受限 workflow script；
6. planner client 异常时 fallback 到 deterministic `WorkflowPlanner`，并在 draft 中记录 `plannerMode=fallback_deterministic`；
7. repair 超限仍失败时返回 `prompt_guided_rejected` draft，`script=""`，保留 invalid plan 与 validation issues 供复盘，不再静默 fallback 掩盖坏计划；
8. rejected draft 可通过 `WorkflowStore.write_workflow_draft()` 持久化 validation evidence。

新增测试：

```text
tests/test_workflow_prompt_guided_planner.py
tests/prompt_guided_planner_real_e2e.py
```

单元/集成测试覆盖：

- review 多维 fan-out + validation + synthesis；
- coding 错误并行经 repair 修正；
- client 故障 fallback；
- planning / mixed 不直接写代码；
- research 多来源 + credibility topology；
- generated script 可由 `WorkflowRuntime + FakeChildAgentRunner` 执行；
- rejected draft 持久化 validation evidence。

相关测试通过：

```text
python -m unittest tests.test_workflow_prompt_guided_planner tests.test_workflow_planner_compiler tests.test_workflow_plan_validator tests.test_workflow_store
# Ran 29 tests ... OK
```

### 7.4.2 GLM-5.1 真实 Prompt-guided planner 矩阵 E2E（2026-06-18）

使用 `mykey.py` 中 `native_oai_config` 指向的 `z.ai/glm-5.1` 进行了 opt-in 真实 planner E2E：

```powershell
$env:GA_RUN_REAL_PROMPT_PLANNER_E2E = '1'
$env:GA_REAL_API_CONFIG = 'native_oai_config'
$env:GA_REAL_API_EXPECTED_NAME = ''
$env:GA_REAL_API_EXPECTED_MODEL = 'z.ai/glm-5.1'
python tests/prompt_guided_planner_real_e2e.py
```

结果：

```json
{
  "passed": true,
  "issues": [],
  "model": "z.ai/glm-5.1",
  "plannerCallCount": 6
}
```

该 E2E 验证的是：planner 端真实 GLM-5.1 生成 `WorkflowPlan JSON`，GA validator / renderer / runtime 真实执行；child agent 端使用 `FakeChildAgentRunner`，避免一次矩阵测试消耗大量真实 child agent API。

四类任务生成了不同 topology，并全部通过 `WorkflowRuntime + FakeChildAgentRunner`：

1. **Research**
   - phases：`Parallel Multi-Source Investigation -> Synthesis and Evaluation`；
   - agents：`credibility-evidence-investigator`、`contradiction-analyst`、`landing-risk-assessor`、`research-synthesizer`；
   - runtime jobs：4 个，全部 succeeded。
2. **Review**
   - phases：`Dimension Fan-out Analysis -> Finding Validation -> Review Synthesis`；
   - agents：`security-reviewer`、`performance-reviewer`、`test-gap-analyzer`、`regression-risk-analyzer`、`finding-validator`、`review-synthesizer`；
   - runtime jobs：6 个，全部 succeeded。
3. **Coding**
   - phases：`Understand Planned Run Requirements -> Write Failing Tests -> Implement Planned Run Entry -> Verify Implementation`；
   - agents：`understand-planned-run-specs`、`write-failing-tests`、`implement-planned-run`、`verify-planned-run-implementation`；
   - tests 明确先于 implementation；
   - runtime jobs：4 个，全部 succeeded。
4. **Planning / Mixed**
   - taskType：`mixed`（符合 planning/mixed 允许范围）；
   - phases：`Context Research and Information Gathering -> Approach Synthesis and Comparison -> Risk Review and Assessment -> Implementation Planning`；
   - agents：`frontend-context-researcher`、`backend-context-researcher`、`runtime-context-researcher`、`approach-synthesizer`、`security-risk-reviewer`、`performance-and-test-reviewer`、`regression-risk-reviewer`、`implementation-planner`；
   - runtime jobs：8 个，全部 succeeded。

同时验证 fallback：真实 planner client 故障时，`LLMWorkflowPlanner` 回退到 deterministic planner，`validationMode=fallback_deterministic` 且 `ok=true`。

结论：prompt-level orchestration policy 已能让真实 GLM-5.1 根据不同任务现场生成不同 workflow topology；GA 能将其验证、编译并通过 workflow runtime 分阶段、多 agent 执行。这证明 prompt-guided planner 是 GA workflow 的可行核心方向。

### 7.4.3 GLM-5.1 真实 planner + 真实 child agents + 工具调用 E2E（2026-06-18）

进一步使用真实 GLM-5.1 同时作为 planner 和 workflow child agent 后端，验证端到端执行能力：

```powershell
$env:GA_RUN_REAL_PROMPT_PLANNER_REAL_CHILD_E2E = '1'
$env:GA_REAL_API_CONFIG = 'native_oai_config'
$env:GA_REAL_API_EXPECTED_NAME = ''
$env:GA_REAL_API_EXPECTED_MODEL = 'z.ai/glm-5.1'
python tests/prompt_guided_planner_real_child_e2e.py
```

结果：

```json
{
  "passed": true,
  "issues": [],
  "model": "z.ai/glm-5.1",
  "plannerCallCount": 2
}
```

该 E2E 与 7.4.2 的区别是：child agent 端不再使用 fake runner，而是使用真实 `NativeGPTChildAgentRunner(config_name="native_oai_config", enable_tools=True)`。测试允许 child agents 使用安全只读工具读取任务指定文件，禁止读取 `mykey.py` / `mykey.json` / `mcp.json`、禁止修改文件和提交。

两个真实仓库场景通过：

1. **Prompt planner 代码 review**
   - 任务：审查 `workflow_planner.py`、`tests/test_workflow_prompt_guided_planner.py`、`tests/prompt_guided_planner_real_e2e.py`；
   - phases：`Dimension Fan-Out: Correctness / TDD Sequence / Secret Hygiene -> Synthesis: Consolidate Cross-Dimension Findings`；
   - agents：`correctness-reviewer`、`tdd-sequence-reviewer`、`secret-hygiene-reviewer`、`findings-synthesizer`；
   - runtime jobs：4 个真实 GLM-5.1 child jobs，全部 succeeded；
   - tool calls：23 次，包含 `file_read`、`code_run`、`no_tool`。
2. **文档/代码一致性 research**
   - 任务：读取 `docs/GA_workflow_defect_optimization_plan.md`、`docs/claude_code_dynamic_workflow_reference.md`、`workflow_planner.py`，分析 Prompt-guided planner 文档与代码是否一致；
   - phases：`Parallel Document & Code Reading -> Synthesis and Gap Analysis`；
   - agents：`read-defect-optimization-doc`、`read-dynamic-workflow-reference-doc`、`read-workflow-planner-code`、`doc-code-gap-analyzer`；
   - runtime jobs：4 个真实 GLM-5.1 child jobs，全部 succeeded；
   - tool calls：18 次，包含 `file_read`、`code_run`、`no_tool`。

合计：8 个真实 child jobs、41 次工具调用，均由 `workflow-progress.json` 记录 `toolCalls` 和 `resultPreview`。这证明 GA 的 prompt-guided dynamic workflow 不仅能让真实 LLM 生成 workflow topology，也能让真实 child agents 继承工具能力并执行具体 workflow steps。

注意：真实 provider 过程中出现 HTTP 503 / ReadTimeout retry，但最终成功。该类高成本测试耗时约 21 分钟，应保持 opt-in，不进入默认 unittest。

### 7.4.4 下一步：Planner control-plane integration / auto-approved planned run

当前 prompt-guided planner 已经证明三层能力：

1. `LLMWorkflowPlanner` 能生成、修复、拒绝或 fallback `WorkflowPlan JSON`；
2. 真实 GLM-5.1 planner 能按 research / review / coding / planning/mixed 任务生成不同 topology；
3. 真实 GLM-5.1 child agents 能继承工具能力并执行 workflow steps。

下一步不应优先做 Ink UI，也不应继续只增加脚本式 E2E，而应把 planner 收进正式 control-plane：

```text
WorkflowController.create_planned_run(...)
```

推荐 API：

```python
WorkflowController.create_planned_run(
    *,
    session_id: str,
    task_text: str,
    planner,
    context: dict | None = None,
    auto_approve: bool = True,
) -> WorkflowRun
```

默认应 `auto_approve=True`：当用户已经明确要求使用 workflow 执行任务时，planner 生成的 draft 不应再要求二次人工审批。但 draft artifact 仍必须保存，因为它是审计 / replay / learn 的证据。

目标流程：

```text
task_text
→ planner.plan(task_text, context)
→ WorkflowRun(script=draft.script)
→ WorkflowStore.write_workflow_draft(run, draft)
→ run.metadata.workflowDraftRef = "workflow-draft.json"
→ run.metadata.plannerMode = draft.context.plannerMode
→ if draft.validation.ok and auto_approve:
      run.status = "running"
      journal: workflow_planned, workflow_started
  elif draft.validation.ok and not auto_approve:
      run.status = "awaiting_approval"
      journal: workflow_planned, workflow_approval_requested
  else:
      run.status = "failed"
      journal: workflow_planned, workflow_plan_rejected
```

TDD 切片：

1. `test_create_planned_run_auto_approves_valid_draft`
   - fake planner 返回 valid draft；
   - run.status == `running`；
   - `workflow-draft.json` 存在；
   - metadata 包含 `workflowDraftRef` / `plannerMode`；
   - journal 包含 `workflow_planned` / `workflow_started`。
2. `test_create_planned_run_can_request_approval_when_auto_approve_false`
   - run.status == `awaiting_approval`；
   - journal 包含 `workflow_planned` / `workflow_approval_requested`；
   - 不启动 runtime。
3. `test_create_planned_run_records_rejected_draft_without_running`
   - planner 返回 validation ok false；
   - run.status == `failed`；
   - `workflow-draft.json` 仍存在；
   - run.script 为空或不可运行；
   - journal 包含 `workflow_plan_rejected`。
4. `test_create_planned_run_records_fallback_mode`
   - planner 返回 fallback draft；
   - metadata / draft validation 记录 `fallback_deterministic`。
5. `test_create_planned_run_script_executes_with_fake_runtime`
   - controller 生成 running run；
   - `WorkflowRuntime + FakeChildAgentRunner` 能执行该 run。

后续顺序：

```text
1. Controller planned run API
2. Controller + runtime fake integration
3. Ink bridge experimental command
4. Ink UI 展示 generated draft / plannerMode / validation issues
5. 风险 gating：high risk / external_io / destructive / git commit / deploy 时 auto_approve=False
```

### 7.4.5 已完成：Controller planned run API 与 GLM-5.1 真实自测

本切片已按 TDD 完成 `WorkflowController.create_planned_run(...)` 最小实现，并通过真实 GLM-5.1 自测。

实现行为：

```text
task_text
→ planner.plan(task_text, context)
→ create WorkflowRun(script=draft.script)
→ write workflow-draft.json
→ metadata.workflowDraftRef = "workflow-draft.json"
→ metadata.plannerMode = prompt_guided / fallback_deterministic / deterministic
→ metadata.workflowTaskType = draft.classification.taskType
→ journal: workflow_planned
→ valid + auto_approve=True:
      status = running
      journal: workflow_started
→ valid + auto_approve=False:
      status = awaiting_approval
      journal: workflow_approval_requested
→ invalid/rejected:
      status = failed
      error = workflow_plan_rejected
      journal: workflow_plan_rejected
```

新增测试：

```text
tests/test_workflow_controller.py::test_create_planned_run_auto_approves_valid_draft
tests/test_workflow_controller.py::test_create_planned_run_can_request_approval_when_auto_approve_false
tests/test_workflow_controller.py::test_create_planned_run_records_rejected_draft_without_running
tests/test_workflow_controller.py::test_create_planned_run_records_fallback_mode
tests/test_workflow_controller.py::test_create_planned_run_script_executes_with_fake_runtime
```

验证命令：

```bash
python -m unittest tests.test_workflow_controller
python -m unittest tests.test_workflow_controller tests.test_workflow_planner_compiler tests.test_workflow_prompt_guided_planner tests.test_workflow_plan_validator tests.test_workflow_store tests.test_workflow_integration tests.test_workflow_runtime tests.test_workflow_scheduler
```

结果：

```text
Ran 11 tests ... OK
Ran 102 tests ... OK
```

真实 GLM-5.1 自测结果：

1. `tests/prompt_guided_planner_real_e2e.py`
   - `configName`: `native_oai_config`
   - `model`: `z.ai/glm-5.1`
   - `passed`: true
   - `issues`: []
   - 覆盖 research / review / coding / planning-mixed / fallback。
2. Controller planned run E2E
   - 真实 GLM-5.1 生成 coding planned run：`Understand -> Write Failing Tests -> Implementation -> Verification`，4 jobs succeeded；
   - 真实 GLM-5.1 生成 review planned run：状态流 / journal / draft artifact / rejected plan 分维度 fan-out，再 synthesis，5 jobs succeeded；
   - 两个场景均自动进入 `running`，journal 前缀均为 `workflow_planned`, `workflow_started`；
   - metadata 均包含 `plannerMode=prompt_guided`、`workflowDraftRef=workflow-draft.json`、`workflowTaskType`。

注意：真实 API 测试仍保持 opt-in，不进入默认 unittest；测试过程不读取、不打印、不提交真实密钥文件或真实 API artifacts。

### 7.4.6 下一步：Ink bridge experimental `workflow_plan` command

在 `WorkflowController.create_planned_run(...)` 已进入正式 control-plane 后，下一步应做 **Ink bridge experimental command**，但范围必须严格限制在 JSONL bridge 层，不直接做完整 Ink React UI。

#### 为什么下一步是 bridge command

当前链路已经完成：

```text
LLMWorkflowPlanner / WorkflowPlanner
→ WorkflowDraft
→ WorkflowController.create_planned_run(...)
→ WorkflowRun + workflow-draft.json + journal
→ WorkflowRuntime 可执行
```

剩余缺口不是 planner/compiler，也不是 runtime，而是前端入口还只能走旧的手写 JS draft：

```text
workflow_draft(script) → awaiting_approval → workflow_approve(run_id)
```

这条旧路径要求调用方自己提供 script，不会触发 prompt-guided planner，也绕过了新建的 `create_planned_run(...)`。因此下一步应新增一个实验 JSONL command，把自然语言任务接入 planned run：

```json
{
  "type": "workflow_plan",
  "taskText": "...",
  "context": {},
  "autoApprove": true,
  "args": {},
  "timeoutSeconds": 120
}
```

#### 最小产品行为

```text
Ink JSONL command: workflow_plan
→ GenericAgentBridge.workflow_plan(...)
→ planner = workflow_planner_factory()
→ workflow_controller.create_planned_run(
      session_id=current_session,
      task_text=taskText,
      planner=planner,
      context=context,
      auto_approve=autoApprove,
  )
→ emit workflow_run
→ emit workflow_event 序列
→ if run.status == running:
      start _run_workflow_runtime(run_id, args, timeout_seconds)
→ elif run.status == awaiting_approval:
      不启动 runtime，等待现有 workflow_approve
→ elif run.status == failed:
      不启动 runtime，emit failed run / rejected event
```

#### 关键设计约束

1. **实验入口，不做完整 UI**
   - 只新增 JSONL command 和 bridge method；
   - React/Ink UI 可先不增加按钮、不增加完整可视化；
   - 后续 UI 只消费已有 `workflow_run` / `workflow_event` / `workflow_final`。

2. **planner 可注入，先不强绑真实 API**
   - `GenericAgentBridge(..., workflow_planner_factory=None)`；
   - 单元测试使用 fake planner；
   - 默认真实 planner 的配置接入可作为后续小切片，避免把 bridge command 与 provider 配置耦合。

3. **默认 auto-approved，但保留 manual path**
   - `autoApprove` 默认 true；
   - `autoApprove=false` 时 run 进入 `awaiting_approval`，复用现有 `workflow_approve`；
   - 这保持“用户显式要求 workflow 执行时不二次卡住”的原则，同时保留人工审批路径。

4. **rejected draft 不运行**
   - `draft.validation.ok == false` 时 controller 已产生 failed run；
   - bridge 只 emit，不启动 runtime；
   - UI/调用方通过 `workflow_plan_rejected` event 查看 validator issues。

5. **busy 语义沿用现有 workflow_draft / workflow_approve**
   - agent 正在运行或 bridge 正在消费时拒绝；
   - 避免普通 chat turn 与 workflow plan 同时争用 session。

6. **安全边界**
   - bridge 不读取 `mykey.py` / `mykey.json` / `mcp.json`；
   - 不打印 provider 配置和 API key；
   - emitted event 继续走现有 `sanitize(...)`；
   - `workflow_plan` command 不允许直接提交代码或执行外部 deploy，风险 gating 后续再做。

#### TDD 切片

建议新增测试：

```text
tests/test_ink_bridge.py::test_workflow_plan_creates_auto_approved_run_and_starts_runtime
tests/test_ink_bridge.py::test_workflow_plan_can_create_awaiting_approval_run_when_auto_approve_false
tests/test_ink_bridge.py::test_workflow_plan_emits_rejected_plan_without_runtime
tests/test_ink_bridge.py::test_jsonl_loop_dispatches_workflow_plan_command
```

测试要点：

1. `test_workflow_plan_creates_auto_approved_run_and_starts_runtime`
   - fake planner 返回 valid draft；
   - bridge emit `workflow_run`，run.status 初始为 `running`；
   - journal 包含 `workflow_planned` / `workflow_started`；
   - runtime thread 被启动，最终 emit `workflow_final`；
   - metadata 包含 `workflowDraftRef` / `plannerMode` / `workflowTaskType`。

2. `test_workflow_plan_can_create_awaiting_approval_run_when_auto_approve_false`
   - `autoApprove=False`；
   - run.status == `awaiting_approval`；
   - emit `workflow_approval_requested`；
   - 不启动 runtime；
   - 后续仍可用既有 `workflow_approve(run_id)` 执行。

3. `test_workflow_plan_emits_rejected_plan_without_runtime`
   - fake planner 返回 `validation.ok == false`；
   - run.status == `failed`；
   - run.error == `workflow_plan_rejected`；
   - emit `workflow_plan_rejected`；
   - 不启动 runtime。

4. `test_jsonl_loop_dispatches_workflow_plan_command`
   - JSONL 输入 `{"type":"workflow_plan",...}`；
   - 断言调用 `bridge.workflow_plan(...)`；
   - 参数覆盖 `taskText` / `context` / `autoApprove` / `args` / `timeoutSeconds`。

#### 建议代码改动

```text
frontends/ink_bridge.py
- GenericAgentBridge.__init__(..., workflow_planner_factory=None)
- GenericAgentBridge.workflow_plan(...)
- run_jsonl_loop 分发 cmd_type == "workflow_plan"

tests/test_ink_bridge.py
- fake planner / fake runtime 覆盖 planned run bridge path
```

#### 非目标

本切片不做：

- 不做完整 Ink React UI；
- 不做风险 gating；
- 不把真实 GLM-5.1 planner 硬编码进 bridge；
- 不让模型直接输出自由 JS；
- 不改变既有 `workflow_draft(script)` / `workflow_approve(run_id)` 兼容路径。

#### 完成后的下一步

完成 bridge command 后，再进入：

```text
1. 默认真实 planner factory 配置接入
2. Ink UI 展示 generated draft / plannerMode / validation issues
3. 风险 gating：high risk / external_io / destructive / git commit / deploy → autoApprove=False
```

### 7.4.7 已完成：Ink bridge experimental `workflow_plan` command

本切片已按 TDD 实现 bridge-level planned workflow 入口。它不要求调用方手写 JS script，而是从自然语言 `taskText` 进入 planner/control-plane/runtime 链路：

```text
workflow_plan JSONL command
→ GenericAgentBridge.workflow_plan(...)
→ workflow_planner_factory()
→ WorkflowController.create_planned_run(...)
→ emit workflow_run / workflow_event
→ auto-approved running run 启动 _run_workflow_runtime(...)
```

实现内容：

```text
frontends/ink_bridge.py
- GenericAgentBridge.__init__(..., workflow_planner_factory=None)
- GenericAgentBridge.workflow_plan(...)
- GenericAgentBridge._make_workflow_planner()
- run_jsonl_loop 分发 cmd_type == "workflow_plan"

tests/test_ink_bridge.py
- test_workflow_plan_creates_auto_approved_run_and_starts_runtime
- test_workflow_plan_can_create_awaiting_approval_run_when_auto_approve_false
- test_workflow_plan_emits_rejected_plan_without_runtime
- test_jsonl_loop_dispatches_workflow_plan_command
```

已验证行为：

1. `auto_approve=True`
   - fake planner 返回 valid draft；
   - bridge 调用 `create_planned_run(...)`；
   - run 初始 emit 为 `running`；
   - metadata 包含 `workflowDraftRef` / `plannerMode` / `workflowTaskType`；
   - journal 包含 `workflow_planned` / `workflow_started`；
   - runtime thread 启动并 emit `workflow_final`。
2. `auto_approve=False`
   - run.status == `awaiting_approval`；
   - journal 包含 `workflow_planned` / `workflow_approval_requested`；
   - 不启动 runtime。
3. rejected draft
   - run.status == `failed`；
   - run.error == `workflow_plan_rejected`；
   - journal 包含 `workflow_plan_rejected`；
   - 不启动 runtime。
4. JSONL dispatch
   - `{"type":"workflow_plan",...}` 正确路由到 `bridge.workflow_plan(...)`；
   - 参数覆盖 `taskText` / `context` / `autoApprove` / `args` / `timeoutSeconds`。

验证命令：

```bash
python -m unittest tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_creates_auto_approved_run_and_starts_runtime tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_can_create_awaiting_approval_run_when_auto_approve_false tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_emits_rejected_plan_without_runtime tests.test_ink_bridge.InkBridgeTest.test_jsonl_loop_dispatches_workflow_plan_command
python -m unittest tests.test_ink_bridge
python -m unittest tests.test_workflow_controller tests.test_workflow_planner_compiler tests.test_workflow_prompt_guided_planner tests.test_workflow_plan_validator tests.test_workflow_store tests.test_workflow_integration tests.test_workflow_runtime tests.test_workflow_scheduler
```

结果：

```text
Ran 4 tests ... OK
Ran 74 tests ... OK
Ran 102 tests ... OK
```

注意：本切片仍未把真实 GLM-5.1 planner 硬编码进 bridge。默认 `_make_workflow_planner()` 暂用 deterministic `WorkflowPlanner`，真实 prompt-guided planner 应在下一小切片通过配置/factory 注入，以避免 bridge 与 provider secret 配置耦合。

### 7.4.8 已完成：真实 prompt-guided planner factory 配置接入

本切片继续推进 `workflow_plan` 的默认 planner 配置接入：bridge 不再只能默认 deterministic planner，而是通过环境配置选择 deterministic 或 prompt-guided planner。

新增能力：

```text
workflow_planner.py
- NativeWorkflowPlannerClient
- parse_json_object(...)
- resolve_session(...) wrapper
- build_workflow_planner_from_env()

frontends/ink_bridge.py
- GenericAgentBridge._make_workflow_planner() 默认调用 build_workflow_planner_from_env()
```

环境变量：

```text
GA_WORKFLOW_PLANNER_MODE=deterministic | prompt_guided | llm | real
GA_WORKFLOW_PLANNER_CONFIG=native_oai_config
GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS=1
```

行为：

```text
GA_WORKFLOW_PLANNER_MODE 未设置或非 prompt_guided/llm/real
→ WorkflowPlanner()

GA_WORKFLOW_PLANNER_MODE=prompt_guided / llm / real
→ LLMWorkflowPlanner(
      client=NativeWorkflowPlannerClient(config_name),
      fallback=WorkflowPlanner(),
      max_repair_attempts=GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS,
  )
```

`NativeWorkflowPlannerClient` 通过 `llmcore.resolve_session(config_name)` 调用现有 LLM 配置，但不读取、不打印 `mykey.py` / `mykey.json`。它要求模型只输出 `WorkflowPlan JSON`，并自动解析 Markdown code fence 包裹的 JSON。

新增测试：

```text
tests/test_workflow_prompt_guided_planner.py::NativeWorkflowPlannerClientTest::test_native_client_uses_resolve_session_and_parses_json_without_markdown
tests/test_workflow_prompt_guided_planner.py::NativeWorkflowPlannerClientTest::test_build_workflow_planner_from_env_defaults_to_deterministic
tests/test_workflow_prompt_guided_planner.py::NativeWorkflowPlannerClientTest::test_build_workflow_planner_from_env_returns_prompt_guided_planner
tests/test_workflow_prompt_guided_planner.py::NativeWorkflowPlannerClientTest::test_build_workflow_planner_from_env_accepts_real_api_alias
tests/test_ink_bridge.py::InkBridgeTest::test_default_workflow_planner_factory_uses_env_builder
```

验证命令：

```bash
python -m unittest tests.test_workflow_prompt_guided_planner.NativeWorkflowPlannerClientTest tests.test_ink_bridge.InkBridgeTest.test_default_workflow_planner_factory_uses_env_builder
python -m unittest tests.test_ink_bridge tests.test_workflow_prompt_guided_planner
```

结果：

```text
Ran 5 tests ... OK
Ran 89 tests ... OK
```

当前 bridge 仍支持显式 `workflow_planner_factory` 注入，便于测试和未来 UI/配置层覆写。默认路径则可通过环境变量启用真实 prompt-guided planner。

### 7.4.9 下一步：从 bridge planned run 入口推进到 Ink Workflow UI

提交 `168c2622018ad8a5c987941f4cc33c1dc9809441 feat(workflow): 接入 Ink bridge planned workflow 入口` 已完成的是 **JSONL bridge / control-plane 入口**，不是完整 Ink React UI。该提交让自然语言任务可以通过 `workflow_plan` command 进入：

```text
workflow_plan JSONL command
→ GenericAgentBridge.workflow_plan(...)
→ build_workflow_planner_from_env() / injected workflow_planner_factory
→ WorkflowController.create_planned_run(...)
→ workflow_run / workflow_event / workflow_final events
→ WorkflowRuntime
```

因此当前目标已经从“能不能把自然语言任务送进 planner/runtime”转为：

```text
如何把 workflow run 的状态、draft、progress、phase/agent 层级和 agent detail 以 Claude Code-style UI 展示给用户。
```

#### 当前已具备能力

```text
Python bridge / backend:
- frontends/ink_bridge.py 已有 workflow_plan / workflow_draft / workflow_approve / workflow_resume / workflow_deny / workflow_stop / workflow_list / workflow_detail。
- workflow_plan 已能自动创建 planned run，并在 running 时启动 runtime thread。
- workflow_run payload 会移除 script，但保留 WorkflowRun.to_dict() 中的 metadata / jobs / resultRef / error。
- workflow_detail 当前返回 run、script、events。
- workflow_final 当前读取 final-result.json 或返回 fallback。

Workflow artifact:
- workflow-progress.json 已记录 workflowProgress entries，包括 label、phase、state、toolCalls、loadedSkills、tokenUsage、promptPreview、resultPreview、transcriptRef、resultRef 等字段。
- final-result.json 带 workflowProgressRef。

Ink UI:
- frontends/ink-ui/src/protocol.ts 已有 workflow_draft / workflow_approve / workflow_resume / workflow_deny / workflow_stop / workflow_list / workflow_detail command 类型。
- frontends/ink-ui/src/state.ts 已能接收 workflow_run / workflow_runs / workflow_detail / workflow_event / workflow_final。
- frontends/ink-ui/src/workflowPanel.ts 当前只是审批/脚本面板：显示 runId、status、permission、jobs、raw script 和 approve/deny/stop/resume 快捷键。
- /workflows 当前会发 workflow_list，但 App.tsx 收到 workflow_runs 后会自动打开第一个 run 的 workflow_detail，而不是停留在 Claude Code-style 的 workflow 汇总列表页。
```

#### 主要缺口

1. **TypeScript 协议还没有声明 `workflow_plan` command**
   Python bridge 已支持 `cmd_type == "workflow_plan"`，但 `frontends/ink-ui/src/protocol.ts` 的 `BridgeCommand` union 尚未包含：

   ```ts
   { type: 'workflow_plan'; taskText: string; context?: Record<string, unknown>; autoApprove?: boolean; args?: unknown; timeoutSeconds?: number }
   ```

   因此 Ink UI 还不能类型安全地直接发起 planned workflow。

2. **`/workflows` UI 仍是旧审批面板行为**
   当前 `/workflows` 更像“列出后自动打开某个 workflow 的 raw script detail”。Claude Code-style UI 要求它先显示汇总列表：

   ```text
   Dynamic workflows
   97 completed

   › ✓ prompt-guided-planner-real-e2e-design  4 agents · 133k tok · 5m 40s
     ✓ ...
   ```

3. **workflow_detail 缺少 progress / draft artifact 数据**
   图 3/4 所需的 phase/agent overview 和 agent detail 不应从 raw script 或 journal 里猜，而应优先消费 `workflow-progress.json`。计划审阅页还需要 `workflow-draft.json` 中的 `plan.meta`、`phases`、`agents`、`validation issues`、`constraints` 等原始计划结构。当前 bridge `workflow_detail` 没有直接返回这些 artifact。

4. **phase attribution 尚未完全可靠**
   后端已有 `phase(...)` journal 事件、`WorkflowJob.phase` 字段以及 `workflow-progress.json` 的 `phase/phaseTitle` 输出位置，但仍需要补测试确认普通脚本路径 `phase('X')` 后 `agent(...)` 注册的 job 会带上 phase。UI 第一版必须支持 `phase` 为空时的“未分阶段”fallback。

5. **UI 层缺少 Workflow List / Overview / Agent Detail 三层 view model**
   现有 `workflowPanelRows()` 只有 raw script rows，无法表达：

   ```text
   Workflow List
   → Phases × Agents overview
   → Agents × Agent detail
   ```

6. **底部 live workflow status bar 尚未实现**
   图 1 里的底部固定区域：

   ```text
   Enter to view · x to stop
   › ◌ read-recent-git-commits ... 3/4 agents done · 6m 0s · ↓ 119.5k tokens
   ```

   当前 Ink UI 只在 panel 中显示 workflow，尚未在主会话底部常驻显示运行中 workflow 摘要。

7. **缺少从自然语言发起 planned workflow 的用户入口**
   bridge 已有 `workflow_plan` JSONL command，但 Ink UI 尚无 slash command，例如：

   ```text
   /workflow plan <task text>
   /workflow plan --manual <task text>
   ```

   更高级的自然语言自动识别“使用 workflow”可以后置，第一版应先做显式 slash command。

#### 推荐实施顺序

##### Slice 1：补齐 bridge/UI 数据契约（最小阻塞）

目标：让 Ink UI 能拿到 list/detail/progress 所需数据，但不先做复杂布局。

改动建议：

```text
frontends/ink_bridge.py
- workflow_detail 增加 progress 字段：读取 artifactDir/workflow-progress.json，如果不存在则为 null。
- workflow_detail 增加 draft 字段：读取 run.metadata.workflowDraftRef 指向的 workflow-draft.json，如果不存在则为 null；只返回 plan / validation / classification / context 等结构化 JSON，不让 UI 从 script 反推计划。
- workflow_final 可继续保持现状，不内联大 transcript。
- _list_workflow_runs 排序建议从 run_id 字典序改为最近更新时间/创建时间优先；若模型缺少 createdAt/updatedAt，可先保持现状并在 UI 明确不承诺排序。

frontends/ink-ui/src/protocol.ts
- BridgeCommand 增加 workflow_plan。
- BridgeEvent.workflow_detail 增加 draft?: WorkflowDraftPayload | null 和 progress?: WorkflowProgressPayload | null。
- 新增 WorkflowDraftPayload / WorkflowProgressEntry / WorkflowProgressPayload 类型。

frontends/ink-ui/src/state.ts
- workflowDetails 存储 draft / progress。
```

TDD：

```text
Python:
- tests/test_ink_bridge.py::test_workflow_detail_includes_workflow_progress_when_available
- tests/test_ink_bridge.py::test_workflow_detail_includes_workflow_draft_when_available
- tests/test_ink_bridge.py::test_workflow_detail_allows_missing_workflow_progress_and_draft

TypeScript:
- frontends/ink-ui/src/state.test.ts 增加 workflow_detail draft/progress 存储测试
- frontends/ink-ui/src/bridgeClient.test.ts 或 protocol 相关测试覆盖 workflow_plan command JSON 序列化
```

##### Slice 2：实现 `/workflows` 汇总列表页

目标：使 `/workflows` 显示 workflow run 汇总列表，而不是自动打开第一个详情。

改动建议：

```text
frontends/ink-ui/src/workflowPanel.ts
- 引入 WorkflowPanelState union：
  - { mode: 'list', runs, selected }
  - { mode: 'overview', detail, selectedPhase }
  - { mode: 'agent', detail, selectedAgent, scrollOffset }
- 新增 workflowListRows(...)，输出 Dynamic workflows + completed count + run rows。
- 新增 workflowListCommandForKey(...): Up/Down 移动，Enter 请求 workflow_detail，Esc 关闭。

frontends/ink-ui/src/App.tsx
- event.type === 'workflow_runs' 时 setWorkflowPanel(list)，不要自动 workflow_detail。
- workflowPanel key handling 支持 list mode Up/Down/Enter。
```

验收 UI 形态对齐 `workflow_ui.md` 图 2：

```text
Dynamic workflows
97 completed

› ✓ name  4 agents · 133k tok · 5m 40s
  ✓ name  4 agents · 145.4k tok · 3m 26s
```

TDD：

```text
frontends/ink-ui/src/workflowPanel.test.ts
- workflowListRows_renders_completed_summary_and_selected_prefix
- workflowPanelCommandForKey_list_moves_selection
- workflowPanelCommandForKey_list_enter_requests_detail

frontends/ink-ui/src/App.test.tsx 或 state/app-level test
- /workflows 收到 workflow_runs 后显示 list，不自动请求 detail
```

##### Slice 3：实现 Workflow Overview：Phases × Agents

目标：打开某个 workflow 后显示图 3 的两列概览，不再默认显示 raw script。

数据来源：

```text
workflow_detail.run.jobs
workflow_detail.events
workflow_detail.progress.workflowProgress
workflow_detail.draft.plan.meta / phases / agents / validation / constraints
workflow_detail.run.metadata.workflowDraftRef / plannerMode / workflowTaskType
workflow_detail.script 仅作为调试/approval fallback，不作为主 UI。
```

改动建议：

```text
frontends/ink-ui/src/workflowPanel.ts
- buildWorkflowOverview(detail): group progress entries by phaseTitle/phase。
- workflowOverviewRows(...) 输出顶部 title/description/summary + 左 Phases + 右 selected phase agents。
- Up/Down 在 phase 列移动；Enter 进入 agent detail；Esc 返回 list。
```

注意：`WorkflowRun` 目前未必有完整 meta name/description。可以按优先级推导：

```text
name = run.metadata.workflowName || draft.plan.meta.name || run.runId
summary/description = run.metadata.workflowDescription || draft.plan.meta.description || run.metadata.workflowTaskType || ''
```

如果 progress 中的 phase/phaseTitle 为空，先按 `draft.plan.phases[].agents[].label` 与 job metadata label 对齐；仍无法对齐时归入“未分阶段”。后端应另补 phase attribution 测试和修复，但 UI MVP 不应因此阻塞。

TDD：

```text
workflowPanel.test.ts
- overview groups agents by phaseTitle
- overview falls back to draft.plan phase/agent labels when progress phase is missing
- selected phase controls right-side agents
- agent rows include label/model/token/tool/duration fallback fields where available
```

##### Slice 4：实现 Agent Detail：Prompt / Activity / Outcome

目标：打开某个 agent 后显示图 4 的详情页。

第一版数据来源：

```text
Prompt: job.prompt 或 progress.promptPreview
Activity: progress.toolCalls 最后 N 个
Outcome: progress.resultPreview；如果需要全文，后续再增加 bridge artifact read command
Status header: progress.state + job.metadata.model/config + tokenUsage + toolCalls.length
```

改动建议：

```text
workflowPanel.ts
- workflowAgentDetailRows(...)
- j/k 控制 detail scrollOffset
- Up/Down 切换左侧 agent
- Esc 返回 overview
- s save 第一版可先提示 unsupported 或保存 resultPreview；完整 artifact save 后续再做
```

TDD：

```text
workflowPanel.test.ts
- agent detail renders Prompt / Activity / Outcome sections
- j/k changes scroll range
- Esc returns overview preserving selected phase/agent
```

##### Slice 5：实现底部运行中 workflow status bar

目标：主会话不打开 `/workflows` 时，也能看到类似图 1 的运行中 workflow 摘要，并能从空输入状态快速进入当前 workflow detail 或停止正在运行的 workflow。

实现原则：

```text
- 不把 live workflow status bar 塞进 footerPanel；footerPanel 仍用于显式 help/status/model 等临时面板。
- 不自动请求 workflow_detail；第一版只消费 state.workflows 与已有 workflowDetails/progress。
- 不从 raw script、raw event text 或 transcript 反推状态；只消费结构化 WorkflowRun / WorkflowProgressPayload。
- 不拦截用户正在输入的 Enter/x；只有 input.trim()==='' 且没有 active panel 时才响应。
- awaiting_approval 只显示 Enter review，不显示 x stop；x stop 只对 running workflow 生效。
```

改动建议：

```text
新增 frontends/ink-ui/src/workflowStatusBar.ts
- workflowStatusBarFromState(state)：从 AppState 派生当前 live workflow summary。
- workflowStatusBarRows(summary)：格式化底部两行文本。
- workflowStatusBarCommandForKey(summary, key, rawInput)：Enter -> workflow_detail；x -> workflow_stop（仅 running）。
- live workflow 定义：running / awaiting_approval；terminal succeeded/failed/killed/cancelled 不显示。
- 数据优先级：workflowDetails[runId].progress.workflowProgress -> run.jobs -> run.metadata/runId fallback。

frontends/ink-ui/src/App.tsx
- 用 useMemo 从 state 派生 workflowStatusBar。
- 只在无 workflowPanel / selector / mcpPanel / modelPanel / themePanel / footerPanel / slash suggestions 时显示。
- status bar 为 ambient row，不作为 blocking panel。
- 键盘处理低于已有 panel/selector/slash，高于普通空输入提交。
- 若新增行影响底部高度，必须同步检查 computeLayoutMetrics / inputChromeSections / cursorPosition 相关预算。

测试与验证
- 先做纯函数 TDD，再接 App；不要先写 React 布局代码。
- App 层第一版优先测渲染，键盘映射由纯函数测，因为 Ink stdin harness 对组合输入曾不稳定。
```

TDD：

```text
新增 workflowStatusBar.test.ts
- workflowStatusBarFromState returns null when no live workflow。
- workflowStatusBarFromState selects latest running workflow and ignores terminal workflows。
- workflowStatusBarRows formats N/M agents done and token text from progress/jobs。
- awaiting_approval rows say Enter review and do not include x stop。
- workflowStatusBarCommandForKey maps Enter to workflow_detail when status bar is visible。
- workflowStatusBarCommandForKey maps x to workflow_stop only for running workflow。

新增或扩展 App test
- fake bridge 推送 workflow_run running 后，App frame 显示 workflow status bar。
- 打开 workflowPanel / slash suggestions / footer panel 时，status bar 不抢占面板。
```

真实/集成验证：

```text
- 复用真实 gpt-5.5 planned run 导出的 workflow_detail JSON，验证 awaiting_approval 可显示 Enter review summary。
- 对运行中 workflow 的 live 更新，第一版如果无法从现有 bridge event 获得实时 progress，不在本 slice 扩展后端协议；后续单独评估 workflow_progress BridgeEvent。
```

##### Slice 6：显式 `/workflow plan <task>` 入口

目标：用户可以从 Ink UI 发起 planned workflow，而不只是外部 JSONL 测试能调用 `workflow_plan`。

改动建议：

```text
frontends/ink-ui/src/protocol.ts
- BridgeCommand 加 workflow_plan。

frontends/ink-ui/src/inputController.ts
- parse /workflow plan <task>
- parse /workflow plan --manual <task> → autoApprove=false
- 可选 parse /workflow plan --timeout 120 <task>

frontends/ink-ui/src/slashCommands.ts
- /workflow description 更新，或新增说明。
```

TDD：

```text
inputController.test.ts
- /workflow plan 调用 workflow_plan command
- /workflow plan --manual 设置 autoApprove=false
- 空 task 不发 command 或由 bridge 返回 workflow_empty_task
```

#### 风险与非目标

当前阶段不要一次性做：

```text
- 不做自然语言自动识别“使用 workflow”并隐式触发 workflow_plan。
- 不把 raw script 作为主 UI；raw script 只用于 approval/debug fallback。
- 不从 raw events、raw script 或完整 transcript 反推 UI 主结构；主结构优先来自 workflow_detail.draft 和 workflow_detail.progress。
- 不让 UI 读取 mykey.py / mykey.json / mcp.json。
- 不在 UI 层实现风险 gating；high risk gating 应在 planner/controller 层完成。
- 不把完整 transcript 内联到 workflow_detail；agent detail 第一版使用 progress preview。
```

#### 建议更新进度判断

`168c262` 后，GA workflow UI 接入的进度应记录为：

```text
已完成：natural language task → workflow_plan JSONL command → planner/control-plane/runtime。
未完成：Ink React UI 的 Claude Code-style /workflows list、phase/agent overview、agent detail、bottom live status bar、/workflow plan 用户入口。
下一步：先补 bridge detail progress contract，再做 /workflows list view。
```

这能保证后续 UI 是消费结构化事实，而不是从日志、script 或 transcript 中猜测 workflow 状态。

#### Slice 1 进度记录（2026-06-30）

状态：**已实现并通过自测**。

本 slice 已补齐 bridge/detail 与 Ink UI 状态层的最小数据契约：

```text
Python bridge:
- workflow_detail 现在返回 draft 字段：安全读取 run.metadata.workflowDraftRef 指向的 workflow-draft.json；缺失、非法路径、非法 JSON 或非 object 时返回 null。
- workflow_detail 现在返回 progress 字段：安全读取 artifactDir/workflow-progress.json；缺失、非法路径、非法 JSON 或非 object 时返回 null。
- 仍然不内联 child transcript；detail payload 继续经过 sanitize/redaction。

Ink UI protocol/state:
- BridgeCommand 增加 workflow_plan：taskText/context/autoApprove/args/timeoutSeconds。
- BridgeEvent.workflow_detail 增加 draft?: WorkflowDraftPayload | null 和 progress?: WorkflowProgressPayload | null。
- 新增 WorkflowDraftPayload / WorkflowPlanPayload / WorkflowPlanPhase / WorkflowPlanAgent / WorkflowProgressPayload / WorkflowProgressEntry 类型。
- AppState.workflowDetails 现在保存 draft/progress，供后续 list/overview/agent detail view model 消费。
```

TDD 与自测记录：

```text
红灯：
- python -m unittest tests.test_ink_bridge.InkBridgeTest.test_workflow_detail_includes_workflow_progress_and_draft_when_available tests.test_ink_bridge.InkBridgeTest.test_workflow_detail_allows_missing_workflow_progress_and_draft
  - 初次失败：workflow_detail 缺少 draft/progress 字段。

绿灯 / 回归：
- python -m unittest tests.test_ink_bridge.InkBridgeTest.test_workflow_detail_includes_workflow_progress_and_draft_when_available tests.test_ink_bridge.InkBridgeTest.test_workflow_detail_allows_missing_workflow_progress_and_draft
  - Ran 2 tests ... OK
- python -m unittest tests.test_ink_bridge
  - Ran 79 tests ... OK
- cd frontends/ink-ui && npm exec -- tsx --test src/state.test.ts src/bridgeClient.test.ts
  - 13 tests pass
- cd frontends/ink-ui && npm run test && npm run typecheck
  - 185 tests pass；tsc --noEmit 无错误。
```

真实 gpt-5.5 smoke：

```text
GA_RUN_REAL_API_E2E=1 GA_WORKFLOW_PLANNER_MODE=prompt_guided GA_WORKFLOW_PLANNER_CONFIG=native_oai_config GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS=2 python tests/real_ink_bridge_workflow_detail_smoke.py
```

结果摘要（已 sanitize，未打印密钥）：

```text
passed: true
profile: gpt-native / gpt-5.5
status: awaiting_approval
plannerMode: prompt_guided
workflowTaskType: review
draftPresent: true
validationOk: true
progressPresent: false
progressIsNullForAwaitingApproval: true
```

说明：`autoApprove=false` 的 planned run 尚未注册 agent jobs，因此通常还没有 `workflow-progress.json`；此时 `workflow_detail.progress === null` 是预期行为。progress artifact 存在时的读取路径已由 Python 单元测试覆盖。

下一步进入 **Slice 2：实现 `/workflows` 汇总列表页**，目标是对齐 `workflow_ui.md` 图 2：`/workflows` 先展示 `Dynamic workflows` 列表，不再自动打开第一个 workflow 的 raw script detail。

#### Slice 2 进度记录（2026-06-30）

状态：**已实现并通过自测**。

本 slice 已把 `/workflows` 从旧的 detail-first / raw-script 面板入口改为 list-first 信息架构，对齐 `workflow_ui.md` 图 2：

```text
Dynamic workflows
N completed

› ✓ workflow-name  4 agents
  ◌ running-workflow  2 agents
```

实现内容：

```text
frontends/ink-ui/src/workflowPanel.ts
- WorkflowPanelState 增加 list mode：{ mode: 'list', runs, selected }。
- 新增 workflowListPanelFromRuns(...)。
- 新增 workflowListRows(...)：渲染 Dynamic workflows、completed 统计、选中前缀 `›`、状态图标和 agent 数。
- 新增 workflowListCommandForKey(...)：Up/Down 移动 selection，Enter 生成 workflow_detail command。
- workflowPanelRows(...) 兼容 list/detail 两种模式。
- workflowPanelCommandForKey(...) 对 list mode 委托到 workflowListCommandForKey(...)。

frontends/ink-ui/src/App.tsx
- 收到 workflow_runs 后 setWorkflowPanel(workflowListPanelFromRuns(event.runs))。
- 不再自动选择 awaiting_approval 或第一条 run 并发送 workflow_detail。
- workflow_run 到达时可刷新打开中的 list mode run 列表。
- workflowPanel 键盘处理支持 list mode Up/Down/Enter。

frontends/ink-ui/src/workflowList.app.test.ts
- 增加 App 层事件渲染测试：workflow_runs 到达后显示 Dynamic workflows 列表，且不会自动发送 workflow_detail。
```

TDD 与自测记录：

```text
红灯：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowPanel.test.ts src/workflowList.app.test.ts
  - 初次失败：workflowListRows/workflowListCommandForKey 尚不存在；App 收到 workflow_runs 后仍无法显示 list-first UI。

绿灯 / 回归：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowPanel.test.ts src/workflowList.app.test.ts
  - 6 tests pass
- cd frontends/ink-ui && npm run test && npm run typecheck
  - 188 tests pass；tsc --noEmit 无错误。
- python -m unittest tests.test_ink_bridge
  - Ran 79 tests ... OK
```

UI 自测说明：

```text
- 纯函数测试覆盖 workflow_ui.md 图 2 的核心文本契约：Dynamic workflows、completed count、`› ✓ name` 顺序、agent count、Up/Down/Enter command。
- App 层测试不伪造实现：直接通过 fake bridge 推送真实 BridgeEvent.workflow_runs，验证 React/Ink 渲染层显示 list，且不再 auto-open detail。
- 当前 Ink test harness 对 stdin 注入组合键不稳定，因此 Enter 导航由 workflowListCommandForKey 纯函数测试覆盖；后续如抽出 App 键盘状态机，可再做更细 App integration。
```

真实 gpt-5.5 smoke：

```text
GA_RUN_REAL_API_E2E=1 GA_WORKFLOW_PLANNER_MODE=prompt_guided GA_WORKFLOW_PLANNER_CONFIG=native_oai_config GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS=2 python tests/real_ink_workflows_list_smoke.py
```

结果摘要（已 sanitize，未打印密钥）：

```text
passed: true
profile: gpt-native / gpt-5.5
runListed: true
listedStatus: awaiting_approval
listedPlannerMode: prompt_guided
listedTaskType: planning
```

下一步进入 **Slice 4：实现 Agent Detail：Prompt / Activity / Outcome**。目标是从 overview 进入 agent detail，右侧显示状态头、Prompt、Activity、Outcome，并支持 `workflow_ui.md` 图 4 的左侧 agent 列表 / 右侧内容结构。

#### Slice 3 进度记录（2026-06-30）

状态：**已实现并通过自测**。

本 slice 已把 workflow detail 从 raw script 主视图推进为 Workflow Overview view model，对齐 `workflow_ui.md` 图 3 的 Phases × Agents 信息架构雏形。

实现内容：

```text
frontends/ink-ui/src/workflowPanel.ts
- 新增 WorkflowDetailPayload / WorkflowOverview / WorkflowOverviewPhase / WorkflowOverviewAgent 类型。
- 新增 workflowOverviewFromDetail(detail)：基于 workflow_detail.draft + workflow_detail.progress + run.jobs 构造 overview。
- 新增 workflowOverviewRows(overview)：输出 title/description/summary、Phases 列、所选 phase 的 Agents 列。
- workflowPanelFromDetail(...) 现在默认返回 mode='overview'，不再把 raw script 作为主 UI。
- 保留 workflowRawDetailPanelFromDetail(...)，作为 approval/debug fallback 的 raw script 面板。
- phase/agent 派生优先级：
  1. progress.workflowProgress[].phaseTitle / phase；
  2. run.jobs[].phase；
  3. draft.plan.phases[].agents[].label fallback；
  4. 仍无法对齐时归入“未分阶段”。
- 对 awaiting_approval planned run 的真实数据形态（run.jobs=[] 且 progress=null）增加 draft-only fallback：直接从 draft.plan.phases[].agents[] 生成未完成 agent rows。

frontends/ink-ui/src/App.tsx
- workflow_detail 到达后打开 overview panel。
- workflow_run 到达时可刷新打开中的 overview.run。
```

系统调试记录：

```text
真实 gpt-5.5 overview smoke 首次失败：
- 真实 awaiting_approval planned run 的 workflow_detail 有 draft.plan.phases[].agents[]，但 run.jobs=[] 且 progress=null。
- 原 workflowOverviewFromDetail 只从 progress 或 jobs 派生 agents，导致 phaseCount=0 / totalAgents=0。

根因：
- planned run 在 approval 前尚未执行 workflow script，也就没有 scheduler jobs/progress；此时 draft.plan 是唯一真实、结构化、可审阅的 phase/agent 数据源。

修复：
- 增加 draft-only fallback：当 progress 和 jobs 都为空时，从 draft.plan.phases[].agents[] 派生 registered agent rows。
```

TDD 与自测记录：

```text
红灯：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowPanel.test.ts
  - workflowOverviewFromDetail derives awaiting approval agents from draft when jobs and progress are empty 失败：overview.phases=[]。

绿灯 / 回归：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowPanel.test.ts
  - 9 tests pass
- cd frontends/ink-ui && npm run typecheck
  - tsc --noEmit 无错误
- cd frontends/ink-ui && npm run test
  - 192 tests pass
- python -m unittest tests.test_ink_bridge
  - Ran 79 tests ... OK
```

真实 gpt-5.5 smoke：

```text
GA_RUN_REAL_API_E2E=1 GA_WORKFLOW_PLANNER_MODE=prompt_guided GA_WORKFLOW_PLANNER_CONFIG=native_oai_config GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS=2 GA_REAL_OVERVIEW_DETAIL_OUT=temp/real-overview-detail.json python tests/real_workflow_overview_detail_export.py
frontends/ink-ui/node_modules/.bin/tsx tests/real_workflow_overview_from_detail_smoke.ts temp/real-overview-detail.json
```

结果摘要（已 sanitize，未打印密钥）：

```text
Python export:
passed: true
profile: gpt-native / gpt-5.5
status: awaiting_approval
phaseCount: 2

TS overview smoke:
passed: true
name: Workflow Overview UI Data Contract Read-Only Review
phaseCount: 2
totalAgents: 2
completedAgents: 0
rows include:
- Workflow Overview UI Data Contract Read-Only Review  0/2 agents · awaiting_approval
- Phases | Data Contract Review · 1 agent
- › ✓ Data Contract Review 0/1 | · Workflow Overview UI Data Contract Reviewer
```

说明：该 smoke 证明真实 gpt-5.5 prompt-guided planner 产生的 draft/detail 可被 TS overview selector 消费；UI 没有依赖 raw script 作为主结构。

#### Slice 4 进度记录（2026-07-06）

状态：**已实现并通过自测**。

本 slice 已把 Workflow Overview 推进到 Agent Detail：用户可从 overview 进入当前 phase 的 agent 详情页，左列展示当前 phase 的 agents，右列展示所选 agent 的状态头、Prompt、Activity、Outcome，对齐 `workflow_ui.md` 图 4 的第一版信息架构。

实现内容：

```text
frontends/ink-ui/src/workflowPanel.ts
- 新增 WorkflowAgentDetail / WorkflowAgentDetailPanelState / WorkflowPanelDecision 类型。
- workflowPanelFromDetail(detail) 返回 overview panel 时保留 detailSource，供 agent detail 派生 Prompt / Activity / Outcome。
- 新增 workflowAgentDetailPanelFromOverview(...)：从 overview 进入当前 phase 的 agent detail。
- 新增 agent detail rows：
  - 左列：当前 phase 的 agent 列表与状态图标；
  - 右列：状态头、Prompt、Activity、Outcome；
  - Activity 显示最近 3 条 toolCalls，并保留总数文案；
  - draft-only / awaiting_approval 场景下显示 pending、draft prompt、no recent activity、no outcome yet。
- workflowPanelCommandForKey(...) 改为返回 decision：{ panel?, command? }。
  - overview + Enter：进入 agent_detail；
  - agent_detail + Up/Down：切换当前 phase 内 agent；
  - agent_detail + j/k：滚动右侧 detail；
  - agent_detail + Esc：返回 overview；
  - raw detail approval/deny/stop/resume 仍返回原 bridge command。

frontends/ink-ui/src/App.tsx
- workflowPanel 键盘分支统一消费 WorkflowPanelDecision。
- Esc 先交给 workflowPanelCommandForKey 处理，使 agent_detail 能返回 overview；其他 panel 仍保持 Esc close。
- workflow_run 更新时兼容 agent_detail 面板中的 overview.run 刷新。

tests/real_workflow_overview_from_detail_smoke.ts
- 从真实 sanitized workflow_detail export 进入 agent detail selector。
- 验证真实 draft-only planned run 也能产出 Prompt / Activity / Outcome sections。
- 继续验证 overview / agent detail 都不把 raw script 作为主 UI。
```

TDD 记录：

```text
红灯：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowPanel.test.ts
  - 首次有效红灯：SyntaxError: requested module './workflowPanel.js' does not provide an export named 'workflowAgentDetailPanelFromOverview'。
  - 说明 Slice 4 的 agent detail 构造函数和状态机尚未实现。

绿灯 / 回归：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowPanel.test.ts
  - 12 tests pass
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowPanel.test.ts src/workflowList.app.test.ts src/state.test.ts
  - 24 tests pass
- cd frontends/ink-ui && npm run typecheck
  - tsc --noEmit 无错误
- cd frontends/ink-ui && npm run test
  - 195 tests pass
```

真实 detail smoke：

```text
cmd /c "cd /d D:\\git_codes\\GenericAgent && npx tsx tests/real_workflow_overview_from_detail_smoke.ts temp/real-overview-detail.json"
```

结果摘要（已 sanitize，未打印密钥）：

```text
passed: true
name: Workflow Overview UI Data Contract Read-Only Review
phaseCount: 2
totalAgents: 2
completedAgents: 0
agentRows include:
- Data Contract Review · 1 agent | Workflow Overview UI Data Contract Reviewer
- › · Workflow Overview UI Data Contract Reviewer | · Pending
- Prompt
- Activity
- Outcome
```

说明：本次真实验证消费的是前一轮由真实 gpt-5.5 prompt-guided planner 导出的 `temp/real-overview-detail.json`。本轮未重新调用真实 API，因为独立 dynamic workflow 的 verify agent 遇到上游网关 `502 请求转发失败`；主线实现未依赖该失败结果，而是在当前目录重新按 TDD 落地并完成本地与真实导出数据验证。

下一步进入 **Slice 6：显式 `/workflow plan <task>` 入口**。Slice 5 第一版已经完成 ambient live workflow status bar；后续如果需要真正 per-agent live progress，应单独评估新增 `workflow_progress` BridgeEvent。

#### Slice 5 进度记录（2026-07-06）

状态：**已实现并通过自测**。

本 slice 已实现主会话底部 ambient live workflow status bar 第一版：当没有打开 `/workflows` panel、selector、MCP/model/theme/footer panel 或 slash suggestions 时，App 会在原 activity row 位置显示当前 live workflow 摘要。该实现没有新增底部高度，因此不改变 `computeLayoutMetrics` 的行预算。

实现内容：

```text
frontends/ink-ui/src/workflowStatusBar.ts
- 新增 workflowStatusBarFromState(state)：从 AppState 派生当前 live workflow summary。
- live workflow 第一版定义：running / awaiting_approval；terminal succeeded/failed/killed/cancelled 不显示。
- 多 workflow 时倒序选择最近更新/追加的 live run。
- 数据优先级：workflowDetails[runId].progress.workflowProgress -> run.jobs -> metadata/runId fallback。
- token 汇总从 progress/job tokenUsage 聚合，格式沿用 k tok。
- workflowStatusBarRows(summary)：
  - running: Enter view · x stop + N/M agents done / active agent / last tool / tokens；
  - awaiting_approval: Enter review，不暴露 x stop。
- workflowStatusBarCommandForKey(summary, key, rawInput)：
  - Enter -> workflow_detail；
  - x -> workflow_stop，仅 running 生效。

frontends/ink-ui/src/App.tsx
- useMemo 派生 workflowStatusBar。
- 仅在无 active panel、无 slash suggestions 时显示。
- status bar 作为 ambient row 复用 Activity 行，不进入 footerPanel，不作为 blocking panel。
- 空输入状态下 Enter/x 可触发 workflow detail/stop；不会拦截用户已有输入。

frontends/ink-ui/src/workflowStatusBar.test.ts
- 覆盖无 live workflow、最近 running 选择、terminal 忽略、rows 格式、awaiting approval 无 stop、Enter/x command mapping。

frontends/ink-ui/src/workflowList.app.test.ts
- 增加 App 渲染测试：fake bridge 推送 workflow_run running 后显示 live workflow status bar，且不自动打开 workflow detail/panel。

tests/real_workflow_status_bar_from_detail_smoke.ts
- 复用真实 gpt-5.5 planned run 导出的 sanitized workflow_detail JSON，验证 awaiting_approval 可生成 Enter review status bar，且不泄露 raw script。
```

TDD 与自测记录：

```text
红灯：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowStatusBar.test.ts
  - ERR_MODULE_NOT_FOUND: workflowStatusBar.js 不存在。
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowList.app.test.ts
  - App shows live workflow status bar without opening workflow panel 失败：frame 未包含 Enter view · x stop。

绿灯 / 回归：
- cd frontends/ink-ui && npm exec -- tsx --test src/workflowStatusBar.test.ts src/workflowList.app.test.ts
  - 7 tests pass
- cd frontends/ink-ui && npm run typecheck
  - tsc --noEmit 无错误
- cd frontends/ink-ui && npm run test
  - 201 tests pass
```

真实 detail smoke：

```text
cmd /c "cd /d D:\\git_codes\\GenericAgent && npx tsx tests/real_workflow_status_bar_from_detail_smoke.ts temp/real-overview-detail.json"
```

结果摘要（已 sanitize，未打印密钥）：

```text
passed: true
status: awaiting_approval
rows:
- Enter review
- › ◌ review  awaiting approval
```

说明：本 slice 未新增后端 `workflow_progress` event；如果后续发现运行中 agent 完成数无法随 bridge 事件实时更新，应单独开后端/协议 slice，而不是在 status bar UI 中从 raw events 或 transcript 猜测进度。

#### Slice 4/5 验证修复记录（2026-07-07）

状态：**multi-agent 复验发现 3 个确认缺陷，已按 TDD 修复并验证**。

本轮通过 dynamic workflow 重新复验 Slice 4/5，并使用用户现有 `mykey` 配置中的真实 `gpt-5.5` 完成 E2E smoke。复验过程中严格未读取、打印或暴露 `mykey.py` / `mykey.json` / `mcp.json` / API key / token / credential。

确认问题与修复：

```text
frontends/ink-ui/src/workflowStatusBar.ts
- 修复 status bar 的 destructive stop 快捷键误触发：rawInput 为 x 时，只有 plain key（无 ctrl/meta/shift）才发送 workflow_stop。
- 修复 jobs-only fallback 下 active agent label 丢失：active agent 统一从最终 agents 数组选择，因此仅有 workflow_run.run.jobs、尚无 workflow_detail/progress 时也能显示 metadata.label。
- 同时避免无 label 的 jobs-only fallback 把内部 jobId 当作用户可见 active label。

frontends/ink-ui/src/workflowPanel.ts
- 新增 workflowPanelWithRunUpdate(panel, run)：集中处理 workflow_run 对 list/overview/agent_detail/raw detail 的刷新。
- 修复 agent_detail 打开时同 run workflow_run 更新导致可见 detail stale 的问题：用保留的 detailSource + 新 run 重建 overview，再保留 phaseIndex/agentIndex/scrollOffset 重建 agent detail。

frontends/ink-ui/src/App.tsx
- workflow_run 分支改为复用 workflowPanelWithRunUpdate，避免 App 内重复维护 panel 刷新逻辑。

frontends/ink-ui/src/workflowStatusBar.test.ts
- 增加 jobs-only running label 覆盖。
- 增加 Ctrl/Meta/Shift + x 不触发 workflow_stop 覆盖。

frontends/ink-ui/src/workflowPanel.test.ts
- 增加 workflowPanelWithRunUpdate refreshes visible agent detail status and outcome。
- 补强 Esc 返回 overview 时 selectedPhase 保留断言。
```

TDD 红灯 / 绿灯：

```text
红灯：
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/workflowStatusBar.test.ts
  - jobs-only fallback activeAgent: undefined !== implementation。
  - Ctrl+X/Meta+X 仍返回 workflow_stop。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/workflowPanel.test.ts
  - requested module './workflowPanel.js' does not provide an export named 'workflowPanelWithRunUpdate'。

绿灯：
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/workflowStatusBar.test.ts
  - 6 tests pass。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/workflowPanel.test.ts
  - 13 tests pass。
- npm --prefix frontends/ink-ui run typecheck
  - tsc --noEmit 无错误。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/workflowList.app.test.ts
  - 2 tests pass。
- npx tsx tests/real_workflow_overview_from_detail_smoke.ts temp/real-overview-detail.json
  - passed: true；真实导出 detail 可生成 overview 与 agent detail rows。
- npx tsx tests/real_workflow_status_bar_from_detail_smoke.ts temp/real-overview-detail.json
  - passed: true；awaiting_approval status bar 不暴露 x stop 且不泄露 raw script。
```

本轮修复后仍保留后续增强建议：App 级 non-empty input Enter/x 不被 status bar 截获、active panel/slash/footer 时隐藏 status bar、real smoke 区分 progress/jobs structured-path 与 awaiting_approval zero-agent smoke，可作为后续覆盖率增强项处理。

#### Slice 6 进度记录（2026-07-07）

状态：**已实现并通过自测**。

本 slice 补齐了 Ink React UI 中显式 `/workflow plan <task>` planned workflow 用户入口，使用户不再只能通过外部 JSONL 脚本调用 `workflow_plan`。入口保持显式触发，不做自然语言隐式识别；UI 不读取 `mykey.py` / `mykey.json` / `mcp.json`，也不在前端实现高风险 gating。

实现内容：

```text
frontends/ink-ui/src/inputController.ts
- 在 /workflow list/control 分支之间新增 /workflow plan 解析。
- 支持 /workflow plan <task> -> workflow_plan command，autoApprove 默认 true。
- 支持 /workflow plan --manual <task> -> autoApprove=false。
- 支持 /workflow plan --timeout 120 <task>，并兼容 --manual / --timeout 顺序互换。
- 当 skillNames 中存在 workflow skill 时，/workflow plan 仍优先解析为内置 workflow_plan command，避免被 skill slash command 截获。

frontends/ink-ui/src/slashCommands.ts
- 新增 /workflow plan 建议项。
- 新增 requiresArgs 标记，确保 Enter/Tab 只补全 `/workflow plan `，不会在没有 task text 时直接执行。

frontends/ink-ui/src/localCommandTranscript.ts
- local command transcript 可把 workflow_plan command 还原为 `/workflow plan ...` 文本。
- autoApprove=false 时显示 --manual；timeoutSeconds 存在时显示 --timeout。

frontends/ink-ui/src/App.tsx
- helpText 中加入 `/workflow plan [--manual] [--timeout SECONDS] TASK` 说明。
- 导出 helpText 以便对用户可见帮助内容做稳定单测。

既有后端闭环
- protocol.ts 已包含 workflow_plan BridgeCommand 类型。
- frontends/ink_bridge.py 已支持 workflow_plan JSONL dispatch，并调用 GenericAgentBridge.workflow_plan。
- GenericAgentBridge.workflow_plan 已校验空 task、调用 planner/controller，autoApprove=true 时启动 runtime，autoApprove=false 时进入 awaiting_approval。
```

TDD 与自测记录：

```text
红灯：
- cd frontends/ink-ui && npm exec -- tsx --test src/App.test.ts
  - 新增 App 级 stdin 驱动测试最初失败：测试 harness 的 stdin.emit('data') 未触发 Ink internal_eventEmitter；该红灯被判定为测试方式不可靠，而非实现缺陷。
  - 调整为稳定验证 helpText 用户可见命令说明，并保留 inputController/bridgeClient/ink_bridge 三层对命令链的覆盖。

绿灯 / 回归：
- cd frontends/ink-ui && npm exec -- tsx --test src/App.test.ts src/inputController.test.ts src/slashCommands.test.ts src/bridgeClient.test.ts src/localCommandTranscript.test.ts
  - 相关前端测试通过。
- cd frontends/ink-ui && npm run typecheck
  - tsc --noEmit 无错误。
- cd frontends/ink-ui && npm run test
  - 206 tests pass。
- python -m unittest tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_creates_auto_approved_run_and_starts_runtime tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_can_create_awaiting_approval_run_when_auto_approve_false tests.test_ink_bridge.InkBridgeTest.test_workflow_plan_emits_rejected_plan_without_runtime tests.test_ink_bridge.InkBridgeTest.test_jsonl_loop_dispatches_workflow_plan_command
  - Ran 4 tests ... OK。
```

真实 gpt-5.5 E2E：

```text
GA_RUN_REAL_API_E2E=1 GA_WORKFLOW_PLANNER_MODE=prompt_guided GA_WORKFLOW_PLANNER_CONFIG=native_oai_config GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS=2 python tests/real_ink_bridge_workflow_detail_smoke.py
```

结果摘要（已 sanitize，未打印密钥）：

```text
passed: true
profile: gpt-native / gpt-5.5
runId: wf_e3ac8ad0e3114c87aa62a8a78d20521c
status: awaiting_approval
plannerMode: prompt_guided
workflowTaskType: review
draftPresent: true
validationOk: true
progressIsNullForAwaitingApproval: true
```

验收结论：Slice 6 的显式 planned workflow 入口已经具备用户可发现性（slash suggestions/help）、输入解析、local transcript、bridge serialization、JSONL dispatch、controller/runtime 闭环覆盖，并已通过真实 `gpt-5.5` planned workflow smoke。后续如果需要真正端到端键盘交互测试，应先抽象 Ink stdin 测试 harness 或增加受控 input dispatcher，而不是依赖 `stdin.emit('data')` 触发 Ink 私有 internal event。

复杂任务入口复验（2026-07-07）：

```text
# 复杂基准 E2E
GA_RUN_REAL_API_E2E=1 GA_RUN_REAL_MCP_E2E=1 GA_REAL_API_CONFIG=native_oai_config GA_REAL_API_EXPECTED_MODEL=gpt-5.5 GA_REAL_API_EXPECTED_NAME=gpt-native GA_COMPLEX_WORKFLOW_DETAIL_OUT=temp/slice6-complex-baseline-detail.json python tests/real_complex_workflow_mcp_skill_coding_e2e.py

# Slice 6 入口 + 复杂任务 + 真实 gpt-5.5 planner + UI detail/status 消费 smoke
npx tsx temp/slice6_complex_entry_real_gpt55_smoke.ts
```

结果摘要（已 sanitize，未读取或打印 `mykey.py` / `mykey.json` / `mcp.json`）：

```text
复杂基准 E2E：
- passed: true
- profile: gpt-native / gpt-5.5
- mcpDiscovery.selectedTool: mcp__tavily__tavily_search
- status/runtimeStatus: succeeded / succeeded
- jobCount: 3，jobStatuses: succeeded, succeeded, succeeded
- toolCalls 包含 mcp__tavily__tavily_search、load_skill、file_write、file_read
- usingSuperpowersLoaded: true
- codingFileOk: true
- progressEntryCount: 3

Slice 6 入口复杂 smoke：
- entry: /workflow plan --manual --timeout 180 <complex task>
- 入口产物 command: workflow_plan，autoApprove=false，timeoutSeconds=180
- status: awaiting_approval
- plannerMode: prompt_guided
- taskType: mixed
- validationOk: true
- phaseTitles 包含 Parallel Research and Coding Understanding / Coding Tests / Coding Implementation / Coding Verification / Synthesis and Evidence Review
- labels 包含 World Cup MCP Research Agent / Temporary Coding ... / Credibility Synthesis Agent
- hasMcpResearch: true
- hasSkillCoding: true
- hasSynthesis: true
- workflowPanelFromDetail(panel).mode: overview
- workflowStatusBarRows: Enter review / awaiting approval
```

结论：复杂基准证明 GA workflow runtime 可真实调用 gpt-5.5、真实 MCP、真实 using-superpowers skill、临时编码写读和 progress 生成；Slice 6 入口复杂 smoke 进一步证明 `/workflow plan` UI 入口生成的 `workflow_plan` command 能进入 bridge，用真实 gpt-5.5 planner 生成复杂 mixed workflow draft，并被 workflow overview/status bar 消费。因此 Slice 6 不是只在内部 planner 可用，而是 UI 显式入口可用。

复杂真实 E2E 维护提醒：后续涉及 workflow planner/runtime/child agent/MCP/skills/progress 或 Slice 4/5 UI 的关键改动后，优先运行 `tests/real_complex_workflow_mcp_skill_coding_e2e.py`。该脚本覆盖真实 `gpt-5.5` planner、真实 Tavily MCP 搜索、真实 `using-superpowers` skill 加载、临时编码写读、多 agent runtime 和 Slice 4/5 selector 消费。详细命令与验收证据见 `docs/GA_workflow_complex_e2e_verification.md`。






#### Slice 7 / live workflow_progress 协议进度记录（2026-07-09）

状态：**已实现并通过自测，已提交**。

相关提交：

```text
c1ae55b feat(workflow-ui): 推送 workflow 进度事件
```

本 slice 解决的缺口：`workflow_store.py` / `workflow_scheduler.py` / `workflow_runtime.py` 已经持续维护 `workflow-progress.json`，但 Ink bridge/protocol/UI 之前没有独立的 live progress 事件。前端只能通过 `/workflow detail` 获得 progress snapshot，status bar 未打开 detail 时只能退回 `run.jobs` 低保真 fallback，缺少 token、last tool、active agent 等结构化进度。

实现内容：

```text
frontends/ink-ui/src/protocol.ts
- 新增 BridgeCommand: { type: 'workflow_progress'; runId: string }。
- 新增 BridgeEvent: { type: 'workflow_progress'; progress: WorkflowProgressPayload }。

frontends/ink_bridge.py
- 新增 GenericAgentBridge.workflow_progress(run_id)，读取 workflow-progress.json 并 emit workflow_progress。
- JSONL loop 支持 workflow_progress command。
- _run_workflow_runtime 在 workflow_run 后、workflow_final 前推送一次 workflow_progress，使 UI 不必先打开 detail 也能拿到最终 progress snapshot。
- 读取 progress 继续复用 _workflow_artifact_payload，保持 artifact ref 有界、JSON payload sanitize、不内联 transcriptEvents。

frontends/ink-ui/src/state.ts
- applyBridgeEvent 支持 workflow_progress。
- 已知 run 无 detail 时创建最小 detail cache：script='', events=[], draft=null, progress=...。
- 已有 detail 时只合并 progress，保留 script/events/draft。
- workflow_progress 可更新 run status；后续 workflow_run 到达时仍以 workflow_run payload 为权威 run 数据。

frontends/ink-ui/src/workflowStatusBar.test.ts
- 覆盖 status bar 使用 live workflow_progress 展示 completed/total agents、activeAgent、lastToolName、tokenText。

frontends/ink-ui/src/bridgeClient.test.ts
- 覆盖 workflow_progress JSONL serialization。

tests/test_ink_bridge.py
- 覆盖 bridge.workflow_progress(run_id) 只发 progress payload，不携带 script/transcriptEvents。
- 覆盖 JSONL workflow_progress dispatch。
- 覆盖 runtime 完成时 workflow_run -> workflow_progress -> workflow_final 链路。
```

TDD 红灯 / 绿灯：

```text
红灯：
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/state.test.ts
  - workflow_progress 未被 applyBridgeEvent 处理，live progress cache 断言失败。
- python -m unittest tests.test_ink_bridge.InkBridgeTest.test_workflow_progress_emits_current_progress_without_detail_payload
  - GenericAgentBridge 没有 workflow_progress 方法。

绿灯 / 回归：
- python -m unittest tests.test_ink_bridge
  - Ran 80 tests ... OK。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/state.test.ts
  - 16 tests pass。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/workflowStatusBar.test.ts
  - 7 tests pass。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/workflowPanel.test.ts
  - 13 tests pass。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/bridgeClient.test.ts
  - 4 tests pass。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/inputController.test.ts
  - 26 tests pass。
- npm --prefix frontends/ink-ui run typecheck
  - tsc --noEmit 无错误。
- git diff --check -- <本 slice 相关文件>
  - 无 whitespace 错误，仅 Windows CRLF 提示。
```

真实 gpt-5.5 smoke：

```text
GA_RUN_REAL_API_E2E=1 GA_WORKFLOW_PLANNER_MODE=prompt_guided GA_WORKFLOW_PLANNER_CONFIG=native_oai_config GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS=2 python tests/real_ink_bridge_workflow_detail_smoke.py
```

结果摘要（已 sanitize，未读取或打印 `mykey.py` / `mykey.json` / `mcp.json`）：

```text
passed: true
profile: gpt-native / gpt-5.5
runId: wf_e5519c90b06e49f7a1018bcb755a3f3a
status: awaiting_approval
plannerMode: prompt_guided
workflowTaskType: review
draftPresent: true
validationOk: true
progressIsNullForAwaitingApproval: true
```

本次 dynamic workflow 并行调研结论补充：

```text
- UI polish 方向仍有后续空间：/workflows list、overview、status bar、agent detail 的 token/duration/description/scroll position/快捷键文案还可继续对齐 workflow_ui.md。
- Stage 7 robustness 方向更偏 runtime：schema fallback、workflow_issue、provider anomaly classification 仍是下一片候选。
- workflow_progress 协议缺口已在本 slice 修复：已有 artifact -> bridge/protocol -> state/status bar 的最小闭环。
```

下一步建议：优先做 **Stage 7 robustness 最小 TDD 切片：agent schema fallback + workflow_issue 记录**。

建议红灯测试：

```text
tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_falls_back_to_text_and_records_workflow_issue
tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_without_fallback_fails_with_schema_issue
```

目标：当 agent options 带 `schema` 且 child output 不满足最小 schema 时，`fallback: 'text'` 可让 workflow 继续但必须记录 `workflow_issue`；没有 fallback 时应明确失败为 `schema_validation_failed`，不能退化成不可诊断的泛化 child failure。

#### Slice 7 continuation / agent schema fallback + workflow_issue（2026-07-09）

状态：**已实现，并通过自测与真实 gpt-5.5 E2E**。

本 continuation 确认 `c1ae55b` 只完成了 live `workflow_progress` 协议闭环；runtime robustness 的 `schema fallback` / `workflow_issue` 仍需继续推进。因此本次补齐最小 TDD 切片：

```text
workflow_scheduler.py
- 新增 agent payload 最小 JSON Schema 子集校验：type / required / properties.<field>.type。
- 当 agent options 带 schema 且 payload 不匹配时，记录 job.metadata.schemaValidation。
- fallback: 'text' 时保持 job succeeded，向 payload 写入 schemaFallback=true / schemaValidation，并记录 workflow_issue。
- 未配置 fallback 时把 job 标为 failed，错误码固定为 schema_validation_failed，避免退化为泛化 child failure。
- standalone scheduler final-result.json 也写入 workflowIssues。

workflow_store.py
- workflow-progress.json 顶层写入 workflowIssues。
- 每个 workflowProgress entry 写入 schemaValidation。

workflow_runtime.py
- final-result.json 写入 workflowIssues。

workflow_planner.py
- renderer 对 schemaRef 默认生成 fallback: 'text'，保持 planner 生成的 research workflow 在真实 child 返回文本摘要时可降级继续。

frontends/ink-ui/src/protocol.ts
- WorkflowProgressPayload 允许 workflowIssues。
- WorkflowProgressEntry 允许 schemaValidation。

tests/test_workflow_runtime.py
- 新增 schema fallback 继续执行并记录 workflow_issue 的红绿测试。
- 新增无 fallback 时 schema_validation_failed 的红绿测试。

tests/test_workflow_scheduler.py
- 新增 standalone scheduler artifact 覆盖：result/progress/final 都包含 schema fallback / workflow_issue 证据。
```

TDD 红灯 / 绿灯：

```text
红灯：
- python -m unittest tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_falls_back_to_text_and_records_workflow_issue tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_without_fallback_fails_with_schema_issue
  - fallback 场景没有 schemaFallback 标记。
  - 无 fallback 场景未抛出 schema_validation_failed。

绿灯 / 回归：
- python -m unittest tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_falls_back_to_text_and_records_workflow_issue tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_without_fallback_fails_with_schema_issue
  - Ran 2 tests ... OK。
- python -m unittest tests.test_workflow_scheduler tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_falls_back_to_text_and_records_workflow_issue tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_agent_schema_failure_without_fallback_fails_with_schema_issue
  - Ran 21 tests ... OK。
- python -m unittest tests.test_workflow_runtime tests.test_workflow_store tests.test_workflow_scheduler tests.test_workflow_planner_compiler tests.test_workflow_plan_validator tests.test_workflow_prompt_guided_planner tests.test_ink_bridge
  - Ran 180 tests ... OK。
- npm --prefix frontends/ink-ui exec -- tsx --test frontends/ink-ui/src/state.test.ts frontends/ink-ui/src/workflowStatusBar.test.ts frontends/ink-ui/src/workflowPanel.test.ts frontends/ink-ui/src/bridgeClient.test.ts
  - 46 tests pass。
- npm --prefix frontends/ink-ui run typecheck
  - tsc --noEmit 无错误。
```

真实 gpt-5.5 Slice 7 E2E：

```text
GA_RUN_REAL_API_E2E=1 GA_REAL_API_CONFIG=native_oai_config GA_REAL_API_EXPECTED_MODEL=gpt-5.5 GA_REAL_API_EXPECTED_NAME=gpt-native python tests/real_workflow_slice7_schema_fallback_e2e.py
```

结果摘要（已 sanitize，未打印 `mykey.py` / `mykey.json` / `mcp.json` 内容）：

```text
passed: true
profile: gpt-native / gpt-5.5
runId: wf_slice7_schema_fallback_real
status: succeeded
jobStatus: succeeded
jobLabel: schema-fallback-real
schemaValidation.code: schema_validation_failed
schemaValidation.fallback: text
schemaValidation.fallbackApplied: true
workflowIssueCount: 1
progressEntryCount: 1
finalWorkflowIssueCount: 1
workflowIssueEventCount: 1
result.schemaFallback: true
```

本次范围刻意保持最小：

- 不引入第三方 JSON Schema validator；
- 不实现深层 nested schema / oneOf / enum / format；
- 不把 schema 变成全局硬门禁；
- 不改变无 schema 的 agent 行为；
- 不在 UI 中新增 issue 展示，只先稳定协议和 artifact 字段。

下一步建议：继续做 **Stage 7 robustness：provider anomaly classification**，把 provider 429 / SDK exception / empty content / schema_validation_failed 统一进入可查询的 failure category，并让 progress/detail 能直接展示失败类型。

- 不做固定模板库作为主入口；
- 不让用户选择 `tdd-python-package` / `deep-research-template` 之类模板；
- 不让模型直接输出自由 JS 并执行；
- 不在 Test Gate 完成前大规模开放 coding workflow 自动执行；
- 不把 schema / skill / approval 全局硬门禁化；
- 不让 workflow script 直接访问 fs / shell / network / env / import / require / process。

模板或 pattern 只能作为 planner 的安全默认形状或 fallback，而不是产品核心。

### 7.6 涉及文件

可能新增：

```text
workflow_planner.py
workflow_plan_models.py
workflow_plan_renderer.py
workflow_plan_validator.py
tests/test_workflow_planner.py
tests/test_workflow_plan_renderer.py
tests/test_workflow_plan_validator.py
```

可能改动：

```text
workflow_controller.py
workflow_runtime.py
frontends/ink_bridge.py
frontends/ink-ui/src/*
docs/GA_workflow_defect_optimization_plan.md
```

### 7.7 验收标准

- 输入 research 类任务能生成包含 Source Discovery / Synthesis 等阶段的 workflow draft；
- 输入 coding 类任务能生成理解 / 修改 / 验证顺序正确的 workflow draft，且不会并行 tests 与 implementation；
- 生成 script 含 `export const meta`、`phase()`、`agent()`、`label`；
- validator 能拒绝未定义依赖、非法 helper、禁止 token、明显错误并行结构；
- rendered script 可由 `WorkflowRuntime` + fake runner 执行；
- draft / plan / script / validator result 可被保存和复盘；
- 生成过程不读取、不打印、不提交真实密钥。

---

## 9. 阶段九：真实 API 回归矩阵与质量门禁（P3/P4）

### 9.1 目标

建立稳定的 opt-in 真实 API 回归体系，持续验证 GA workflow 是否真的能完成工程任务。

### 9.1 执行顺序建议

1. 先实现 `WorkflowPlanner` / `WorkflowPlan` / renderer / validator 的动态生成闭环；
2. 再把现有受控 gate（例如 unittest、review、research 产物校验）作为 planner 可用的 primitives；
3. 最后才考虑把常见 pattern 包装成可复用入口或 UI 快捷方式，但它们始终只是 planner 的输出形状，不是主入口。

### 9.2 回归层级

| Level | 名称 | Provider | MCP | 目标 |
|---|---|---|---|---|
| L0 | mock workflow unit | 否 | 否 | 快速验证 runtime 控制流 |
| L1 | local sandbox TDD | 否 | 否 | 验证 TDD template / test gate |
| L1.1 | real gpt-5.5 TDD sandbox | 是 | 否 | 验证真实 child agent 工程能力 |
| L2 | real gpt-5.5 + MCP research report | 是 | 是 | 验证 MCP + report workflow |
| L3 | real bugfix workflow | 是 | 可选 | 验证真实 repair/retest 闭环 |

### 9.3 真实 API 执行约束

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

### 9.4 质量门禁

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

### 9.5 验收标准

- 真实 E2E 可以按 opt-in 手动运行；
- 失败报告能区分 runtime / provider / workflow-methodology / generated-code-quality；
- 每次真实 E2E 后更新 docs，但不提交敏感 artifacts。

---

## 10. 推荐实施顺序

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

### Milestone C：Claude Code 风格的可选 Skill 感知

包含：

1. child agent 默认可见 `load_skill` 与 `[Available Skills]` listing；
2. subagent 按任务语义自主调用 skill，不调用不失败；
3. `workflow-progress.json` 记录 `loadedSkills`、`skillToolCalls`、`skillLoadEvents`；
4. 回归测试锁定 `load_skill` 不会被 tools schema 裁剪静默禁用。

完成后收益：GA workflow agent 的 skill 使用方式与 Claude Code dynamic workflow 的真实默认模式一致：默认可用、自主调用、调用可审计。

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
3. optional skill usage baseline；
4. L1.1 sandbox E2E。

完成后收益：修复真实 E2E 暴露的核心方法论缺陷。

### Milestone F：Claude Code-style Secret Hygiene / Schema Fallback / UI

包含：

1. Claude Code-style 高置信 secret hygiene；
2. schema fallback；
3. failure classification；
4. CLI/Ink progress 展示。

完成后收益：真实工作流更稳、更适合长期使用。

### Milestone G：动态 Planner / Compiler 与真实回归矩阵

包含：

1. `WorkflowPlanner.plan(task_text, context) -> WorkflowDraft`；
2. `WorkflowPlan JSON` 中间表示；
3. deterministic renderer + validator；
4. opt-in real API regression matrix。

完成后收益：GA workflow 从“能执行手写 workflow script”升级为“能根据任务语义动态生成、审计并执行 workflow”。

---

## 11. 不建议优先做的事

### 10.1 不建议先扩展更多底层 DSL 语法

原因：当前底层 `phase/log/agent/parallel/pipeline` 已够用，真正缺的是工程语义和质量门禁。

### 10.2 不建议先追求复杂多 provider 真实 E2E

原因：当前单 provider `gpt-5.5` 已暴露方法论问题。应先修复 TDD / skill / gate，再扩展 provider 矩阵。

### 10.3 不建议依赖 prompt 让 agent 自觉遵守 TDD

原因：真实 E2E 已证明 prompt 约束不足。必须通过模板和 gate 固化流程。

### 10.4 不建议只做 UI，不做 artifact

原因：UI 应展示结构化事实，而不是从日志里猜。应先补 artifact，再做 UI。

---

## 12. 最小可行版本（MVP）定义

如果只做最小闭环，建议 MVP 包含：

```text
1. workflow child agent 默认继承完整工具能力，不再被真实 E2E 降级成 file_read/file_write-only
2. workflowProgress + optional skill usage summary 可复盘
3. WorkflowPlanner 能根据自然语言任务生成 WorkflowPlan JSON
4. renderer 能把 WorkflowPlan JSON 转成受限 workflow script
5. validator 能拒绝非法 helper、未定义依赖和明显错误并行结构
6. 生成脚本可由 WorkflowRuntime + fake runner 执行
7. 至少一个真实 gpt workflow E2E 验证动态生成链路
```

MVP 完成的判断标准：

```text
GA 可以根据用户自然语言任务动态生成受限 workflow script，并在批准后运行：
先分类任务，
再生成 WorkflowPlan JSON，
再通过 validator 检查安全与依赖，
再由 renderer 输出 `export const meta` / `phase()` / `agent()` / `parallel()` / `pipeline()` 组成的脚本，
执行后 artifact 能证明 progress、optional skill 使用情况、MCP/tool 能力和中间 agent 结果真实参与了流程。
```

---

## 13. 最终目标状态

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
