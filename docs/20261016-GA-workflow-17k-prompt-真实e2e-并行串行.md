# GA workflow 真实 deepseek-flash E2E：17K prompt 改造后的并行 + 串行任务

日期：2026-10-16
模型：`default/deepseek-flash`
入口：真实 `GenericAgentBridge` + 真实 LLM API + 真实 workflow runtime

## 1. 测试目标

验证两件事：

1. `load_skill` / workflow authoring prompt 从大 `tool_result` 改成
   “短元数据 + 独立 next_prompt 注入”后，是否仍能正常执行 workflow 类任务；
2. MCP per-tool `maxResultSizeChars` / `tool_result_limits` 改动是否影响普通工具调用。

测试任务要求：

- 3 个独立 agent 并行查询 Python、Node、Git 版本；
- 每个 agent 必须真实调用 `code_run`，不能猜版本；
- 三者全部完成后，串行执行 1 个综合 agent；
- 综合 agent 用 `file_write` 写报告，并用 `file_read` 验证；
- workflow 脚本必须使用 `parallel()`，综合步骤依赖前面三个结果。

## 2. 真实结果

运行状态：

```text
runId: wf_9d31a557da1b42f684c999e3d969dc02
status: succeeded
executionOutcome: succeeded
integrationStatus: accepted
finalAuditStatus: passed
elapsedSeconds: 98.4
```

子任务：

```text
agent_1  probe:PYTHON   succeeded
agent_2  probe:NODE     succeeded
agent_3  probe:GIT      succeeded
agent_4  synthesis:report succeeded
```

汇总：

```text
jobCount: 4
jobStatuses: succeeded=4
scriptContainsParallel: true
scriptContainsPipeline: false
```

真实版本结果：

```text
Python 3.12.4
Node   v22.17.0
Git    2.47.1.windows.2
```

产物：

```text
temp/workflow-runs/wf_9d31a557da1b42f684c999e3d969dc02/temp/workflow-parallel-serial-report.md
```

该报告包含三行版本表、Notes 和 Provenance；`file_read` 由综合 agent 真实执行并验证成功。

## 3. 17K prompt 改造后的证据

本次真实执行中，`load_skill('workflow-authoring')` 的 tool_result 不再是完整 skill 正文，
而是短元数据：

```json
{
  "status": "success",
  "name": "workflow-authoring",
  "description": "...",
  "base_dir": "D:/git_codes/GenericAgent/skills/workflow-authoring",
  "content_chars": 4828,
  "loaded": true
}
```

随后完整正文通过独立 `next_prompt` 注入，模型侧可见：

```text
[SYSTEM TIPS] 已加载 skill workflow-authoring。以下正文是独立注入内容，不是 tool_result。
<skill_content>
Base directory for this skill: D:/git_codes/GenericAgent/skills/workflow-authoring
...
</skill_content>
```

模型没有一次性加载全部 references，而是按需读取：

```text
file_read -> skills/workflow-authoring/references/api.md
file_read -> skills/workflow-authoring/references/examples.md
```

之后真实调用 `workflow` 工具，返回：

```json
{
  "status": "async_launched",
  "runId": "wf_9d31a557da1b42f684c999e3d969dc02",
  "workflowName": "parallel-version-probes-then-synthesis"
}
```

结论：**17K authoring 内容改造为“瘦 SKILL + 按需 references + 独立正文注入”后，
workflow 任务仍能正常执行，且没有出现上游 guardrail 拒绝或空回复。**

## 4. 普通工具影响

### 4.1 代码层结论

`tool_result_limits` 目前只由 MCP runtime 注册 `mcp__*` 工具名。普通工具没有注册值时：

```text
file_read / code_run / structured_output / no_tool
  -> 保持 _EXEMPT_TOOLS 原有豁免

其他普通工具（例如 web_scan）
  -> 继续走原有 GA_TOOL_RESULT_MAX_CHARS / 默认 50K 预算
```

MCP registry 不会按前缀或全局匹配普通工具，因此不会改变普通工具阈值。

### 4.2 本次真实工具调用

workflow 子 agent 真实使用并成功完成了：

```text
code_run           -> 3 路并行版本探测
structured_output  -> 3 路 schema 结果
file_write         -> 串行综合写报告
file_read          -> 串行综合读回验证
```

没有出现普通工具因 `maxResultSizeChars` 改动被错误截断、错误持久化或调用失败。

## 5. 证据路径

```text
temp/workflow-selftest/20261016-153345/summary.json
temp/workflow-selftest/20261016-153345/events.jsonl
temp/sessions/session_de9ae0d7c04645ceabb84a3b922b7171.jsonl
temp/sessions/session_de9ae0d7c04645ceabb84a3b922b7171/workflows/wf_9d31a557da1b42f684c999e3d969dc02/script.js
temp/model_responses/model_responses_050663.txt
temp/workflow-runs/wf_9d31a557da1b42f684c999e3d969dc02/temp/workflow-parallel-serial-report.md
```

## 6. 结论

- 普通 tool 调用未被 MCP per-tool limit 改动破坏。
- `file_read` / `code_run` 等原有豁免工具保持原行为。
- workflow authoring prompt 改造后，真实 `deepseek-flash` 仍能完成
  “并行探测 -> 串行综合 -> 文件落盘 -> 读回验证”的中等难度 workflow。
- 本次真实 E2E 不依赖固定模型输出，只断言 workflow 状态、job 数、脚本并行结构、
  工具调用证据和真实产物内容。
