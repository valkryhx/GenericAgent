# GA Workflow 复杂真实 E2E 验证记录

> 目的：提醒后续每次推进 GA workflow 关键能力、Slice 4/5 UI、MCP/skills/tool 继承、真实 runtime 相关改动后，都要优先考虑运行这个复杂真实 E2E，而不是只跑简单 smoke。

## 验证脚本

```text
tests/real_complex_workflow_mcp_skill_coding_e2e.py
```

该脚本默认不会调用真实 API；只有显式设置 opt-in 环境变量才会运行真实 E2E。

## 覆盖范围

这个测试用于验证 GA workflow 是否真实承载中等复杂度任务链，而不只是能生成 planned draft 或简单 UI smoke。

它覆盖：

```text
1. 真实 gpt-5.5 planner
   - 使用 mykey 配置中的 native_oai_config。
   - 校验 profile.name == gpt-native。
   - 校验 profile.model == gpt-5.5。
   - planner 任务要求 mixed workflow：MCP research + using-superpowers coding + synthesis。

2. 真实 MCP discovery 与 MCP 调用
   - 不读取或打印 mcp.json。
   - 通过 mcp_runtime discovery 找到真实 MCP tools。
   - 强制选择 mcp__tavily__tavily_search。
   - research child agent 必须搜索：
     2026 FIFA World Cup July 7 2026 matches results today
   - 验证 mcpCalled == true 且 mcpReturned == true。

3. 真实 skills 机制
   - 不创建临时 fake skill。
   - coding child agent 必须调用 load_skill。
   - skill 必须是 GA 已发现的真实 using-superpowers。
   - 验证 usingSuperpowersLoaded == true。
   - workflow-progress 必须记录 loadedSkills / skillLoadEvents。

4. 编码场景
   - coding child agent 只能写临时 workspace。
   - 使用 file_write 创建 ga_complex_skill_demo.py。
   - 使用 file_read 读回该文件。
   - 验证 codingFileWritten == true 且 codingFileOk == true。
   - 不写仓库代码，不提交。

5. 多 agent runtime
   - workflow 至少包含 research、coding、synthesis 三个真实 child agents。
   - 验证 runtimeStatus == succeeded。
   - 验证所有 jobStatuses 都是 succeeded。
   - 验证 deniedTools 为空。
   - 验证 progressEntryCount >= 3。

6. Slice 4/5 UI 消费
   - 脚本可导出 sanitized workflow_detail JSON。
   - 用 Slice 4 overview / agent detail selector 消费复杂真实 detail。
   - 用 Slice 5 status bar selector 验证 terminal/live 行为。
```

## 推荐运行命令

从仓库根目录运行：

```powershell
$env:GA_RUN_REAL_API_E2E='1'; `
$env:GA_RUN_REAL_MCP_E2E='1'; `
$env:GA_WORKFLOW_PLANNER_MODE='prompt_guided'; `
$env:GA_WORKFLOW_PLANNER_CONFIG='native_oai_config'; `
$env:GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS='2'; `
$env:GA_COMPLEX_WORKFLOW_DETAIL_OUT='D:\git_codes\GenericAgent\temp\complex-workflow-detail-real.json'; `
python tests/real_complex_workflow_mcp_skill_coding_e2e.py
```

然后继续运行 Slice 4 selector smoke：

```powershell
npx tsx tests/real_workflow_overview_from_detail_smoke.ts temp/complex-workflow-detail-real.json
```

Slice 5 注意事项：复杂 workflow 正常成功后是 `succeeded` terminal 状态，live status bar 应返回 `null`。如需验证 live 派生，可用同一真实 progress 克隆成 `running` 状态做 selector 级检查；不要把 terminal succeeded 下 `bar == null` 误判为失败。

## 必须检查的通过证据

复杂 E2E 输出中至少应满足：

```json
{
  "passed": true,
  "profileOk": true,
  "profile": {
    "name": "gpt-native",
    "model": "gpt-5.5"
  },
  "mcpDiscovery": {
    "selectedTool": "mcp__tavily__tavily_search"
  },
  "status": "succeeded",
  "runtimeStatus": "succeeded",
  "jobStatuses": ["succeeded", "succeeded", "succeeded"],
  "mcpCalled": true,
  "mcpReturned": true,
  "usingSuperpowersLoaded": true,
  "codingFileWritten": true,
  "codingFileOk": true,
  "progressEntryCount": 3,
  "issues": []
}
```

还要检查 `jobsByLabel` 或 `progressEntries` 中包含这些事实：

```text
world-cup-real-mcp-research:
- toolCalls 包含 mcp__tavily__tavily_search。
- deniedTools 为空。

using-superpowers-coding:
- toolCalls 包含 load_skill、file_write、file_read。
- loadedSkills 包含 using-superpowers。
- skillLoadEvents 中 using-superpowers status 为 success。
- deniedTools 为空。

complex-e2e-synthesis:
- status 为 succeeded。
```

## 最近一次已知通过记录

日期：2026-07-07

提交：

```text
5db3c20 test(workflow): 增加复杂真实 MCP 与 skill 编码 E2E
```

当次真实 E2E 结果摘要：

```text
passed: true
configName: native_oai_config
profile.name: gpt-native
profile.model: gpt-5.5
selected MCP: mcp__tavily__tavily_search
planner taskType: mixed
runtimeStatus: succeeded
jobCount: 3
jobStatuses: succeeded, succeeded, succeeded
mcpCalled: true
mcpReturned: true
usingSuperpowersLoaded: true
codingFileWritten: true
codingFileOk: true
progressEntryCount: 3
deniedTools: []
```

Slice 4 selector smoke 对导出的复杂 detail 通过：

```text
npx tsx tests/real_workflow_overview_from_detail_smoke.ts temp/complex-workflow-detail-real.json
passed: true
totalAgents: 3
completedAgents: 3
rows 包含 MCP research、using-superpowers coding、synthesis 三个 agent。
```

## 什么时候必须优先运行

以下改动后，优先运行本复杂 E2E：

```text
- workflow_planner.py / prompt-guided planner 相关改动。
- workflow_runtime.py / workflow_scheduler.py / workflow_store.py 相关改动。
- workflow_child_agent.py / NativeGPTChildAgentRunner / tools schema 相关改动。
- mcp_runtime.py / MCP discovery / MCP tool dispatch 相关改动。
- skills_runtime.py / load_skill / skill prompt 注入相关改动。
- workflow-progress.json 结构或 progress extraction 改动。
- Ink UI Slice 4/5：workflowPanel / workflowStatusBar / App workflow_run refresh 相关改动。
- 声称 GA workflow 已能承载复杂真实任务前。
```

## 安全边界

运行或报告该测试时必须遵守：

```text
- 不读取、不打印、不 grep、不 cat mykey.py / mykey.json / mcp.json。
- 不输出 API key、token、credential。
- 不提交 mcp.json 或任何 secret-bearing 文件。
- 编码 agent 只能写临时 workspace。
- 不让 child agent 修改仓库代码或提交。
- 输出 summary 必须经过 sanitize；如发现 secret pattern，测试应失败。
```

## 默认 skipped 行为

不设置 opt-in 时，脚本必须跳过，避免默认测试烧真实 API：

```powershell
python tests/real_complex_workflow_mcp_skill_coding_e2e.py
```

期望：

```json
{
  "skipped": true,
  "reason": "set GA_RUN_REAL_API_E2E=1 to run real complex workflow E2E"
}
```
