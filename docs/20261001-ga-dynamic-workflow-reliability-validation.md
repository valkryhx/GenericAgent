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
