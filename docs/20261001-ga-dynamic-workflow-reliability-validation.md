# GA Dynamic Workflow Reliability 验证记录

- 日期：2026-10-01
- 分支：`feat/ga-dynamic-workflow-reliability`
- 模型：`deepseek-v4.1-flash`
- 配置来源：本地 `llm.yaml`（未提交）
- 测试入口：`tests/real_workflow_forward_matrix_e2e.py`
- 执行方式：单次、串行，未并行启动多个真实 provider 用例

## 命令

```text
GA_RUN_REAL_FORWARD_MATRIX=1 GA_FORWARD_MATRIX_PROFILE=deepseek-v4.1-flash python tests/real_workflow_forward_matrix_e2e.py
```

## 结果

- live model smoke：通过，模型返回预期标记。
- 总耗时：33.29 秒。
- issues：空。
- 总体：passed=true。

| 用例 | 结果 | 关键证据 |
|---|---|---|
| direct | 通过 | mode=direct，riskLevel=low |
| workflow | 通过 | Source Discovery → Synthesis；job waves=1,2；runtime=succeeded；integration=accepted；finalAudit=passed |
| delegated | 通过 | maxAgents=3、maxWaves=2；a/b wave=1，s wave=2；三个 job 均 succeeded |
| fallback | 通过 | plannerMode=fallback_deterministic；fallbackReason 已持久化 |
| approval | 通过 | status=awaiting_approval；reason=explicit_workflow_approval_gate |
| eval_contract | 通过 | validationOk=true；模型计划显式返回 python_unittest 与 verification_schema |

## 诊断结论

本次运行没有出现 startup handshake、wait predicate、scheduler barrier、artifact persistence 或 verification evidence 失败。workflow 与 delegated 的依赖 wave 顺序由 runtime 正确执行，未依赖自然语言摘要判断完成。

本次 matrix 只覆盖 planner + fake child runtime 的综合路径；它不是两个真实 child 进程的 `wait_agent(all_terminal)` E2E，也没有启用真实 MCP 网络调用。因此后续仍需要单独补跑：

1. 真实 subagent 进程的 `agent_started → turn_started → turn_terminal → result_available` predicate 链路；
2. 带真实 MCP 调用和文件 artifact 的 workflow；
3. provider 延迟、GA 启动耗时、MCP 耗时的分段指标。

## 安全

输出仅保留 profile/model、状态、wave、验收状态和耗时；API key、prompt、transcript 和 provider 原始响应未写入本文件。

## 2026-10-01 复杂真实 workflow 复测

测试入口：`tests/real_complex_workflow_mcp_skill_coding_e2e.py`。本次严格串行使用真实 `deepseek-v4.1-flash`，并启用真实 MCP。

```text
GA_RUN_REAL_API_E2E=1 GA_RUN_REAL_MCP_E2E=1 GA_WORKFLOW_LLM_PROFILE=deepseek-v4.1-flash \nGA_REAL_API_EXPECTED_MODEL=deepseek-v4.1-flash GA_REAL_API_EXPECTED_NAME=deepseek-v4.1-flash \npython tests/real_complex_workflow_mcp_skill_coding_e2e.py
```

结果：`passed=true`，耗时 38.97 秒，`plannerCallCount=2`，`plannerMode` 保持 `prompt_guided`，没有 fallback。3 个 job 全部 `succeeded`。

| 验证项 | 证据 |
|---|---|
| MCP discovery | 25 个可用工具；`mcp__tavily__tavily_search` 可用并真实返回 |
| MCP 调用 | research job 调用 `mcp__tavily__tavily_search`，`mcpCalled=true`、`mcpReturned=true` |
| Skill | coding job 真实调用 `load_skill`，加载 `using-superpowers` |
| 文件闭环 | `file_write` 后 `file_read` 回读通过，`codingFileWritten=true`、`codingFileOk=true` |
| synthesis | synthesis job 成功消费前两路结果并输出完成标记 |
| 安全 | `deniedTools=[]`，未访问敏感配置，未提交改动 |

### 首次失败与修复

首次复杂运行的 runtime 实际已经成功，但 planner 在归一化阶段遇到两个模型契约问题：

1. verification check 的 `owner` 是动态 agent label，旧 validator 只接受固定枚举；现已允许长度受限且无控制字符的显式 agent label。
2. schema check 缺少 `schemaRef` 时，旧 `LLMWorkflowPlanner.plan()` 会把 normalization exception 直接当 provider failure 并 fallback；现已将 normalization 异常转换为 `invalid_plan_contract` validator issue，进入 bounded repair loop。

本次复测确认第二次 planner 响应补齐契约后可继续使用 prompt-guided plan，避免把真实成功的 MCP/Skill/文件工作误报为 deterministic fallback 计划缺失。

MCP 预热 discovery 同时显示 `fetch` 与 `context7` 的远程服务可能出现 partial discovery timeout；Tavily 目标工具仍成功发现并执行。该类外部 MCP 瞬态错误应保持有界重试和按工具报告，不应影响已经可用的目标工具。
