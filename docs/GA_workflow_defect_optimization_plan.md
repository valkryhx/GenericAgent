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

第一版不要追求任意任务全自动编排，先实现 deterministic MVP：

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
7. 生成过程写入 artifact，便于复盘。

### 7.5 非目标

第一版不做：

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
