# Codex 启发的 Dynamic Workflow 验收与状态修复指南

- 日期：2026-09-30
- 适用范围：GenericAgent 的 prompt-guided planner、workflow runtime、child agent、verification/review 和 synthesis 链路
- 参考源码：`D:\git_codes\codex\codex-rs`
- 目标：避免“child agent 回合成功”被误报为“业务任务成功”，并提高真实模型驱动 workflow 的可用性

## 1. 已复现的根因

当前 GA 将多个不同语义压缩成了一个 `succeeded`：

```text
child agent 有正常返回
    -> WorkflowJob.status = succeeded
    -> 所有 job succeeded
    -> WorkflowRun.status = succeeded
    -> executionOutcome = succeeded
```

最小复现中，child agent 返回包含失败事实的摘要：

```text
8 NameError failures; acceptance failed
```

但 workflow 仍然得到：

```text
run_status = succeeded
job_status = succeeded
execution_outcome = succeeded
```

这不是单一模型的缺陷。DeepSeek 在 REFACTOR 阶段把 `_APIKEY_RE` 改名/移除，导致 8 个 `NameError`，只是暴露了 GA 没有强制业务验收的结构性缺口。

## 2. GA 当前的具体缺口

### 2.1 Planner 只验证结构，不验证验收契约

`workflow_planner.py:validate_workflow_plan()` 当前检查 phase、label、依赖、coding role 和测试/实现并行关系，但没有强制 coding plan 携带：

- 可执行的测试门禁；
- 机器可读的 verification 输出；
- 验收失败如何改变 workflow 总体状态；
- 独立 review/verification 角色。

### 2.2 Planner 生成的 role 没有完整传递给 child

`render_workflow_plan()` 只把 label、phase、schema 和 fallback 写入 `agent()` options，忽略 planner 生成的 `role`。因此 `verification`、`implementation` 和 `review` 只是计划 JSON 中的标签，未成为 child agent 的强约束上下文。

### 2.3 自然语言失败不会阻断 workflow

Runtime 当前主要识别两类机器信号：

- 宿主实际执行的 `runPythonUnittest` 失败；
- 最终结果顶层 `verificationPassed: false`。

如果模型只在摘要中写“发现 NameError”或“验收失败”，但没有返回明确字段，runtime 会继续成功收尾。

### 2.4 Schema fallback 会削弱验证

渲染计划时，存在 `schemaRef` 的 agent 默认使用 `fallback: "text"`。这对研究类 agent 可以提高容错，但对 verification/acceptance agent 会把结构化输出失败降级成普通文本，削弱硬门禁。

## 3. Codex 的可借鉴机制

### 3.1 Awaiter：只接受明确 terminal state

参考：`codex-rs/core/assets/agent/builtins/awaiter.toml`。

Codex 明确要求 awaiter：持续等待到成功、失败或停止；不得把中间状态当完成；不得 hallucinate completion。GA 的 wait、child turn 和 workflow worker 也应遵守同样原则。

### 3.2 Review delegate：独立角色、独立 prompt、受限能力

参考：`codex-rs/core/src/tasks/review.rs`、`codex-rs/prompts/templates/review/rubric.md`。

Codex 的 review agent 使用专门 rubric，禁用不需要的 web/collab/multi-agent 能力，并将结果解析为结构化 `ReviewOutputEvent`。GA 的 verify/review 不应只是普通 agent 加一个 label。

### 3.3 状态分层：thread、turn、spawn edge、业务结果分开

参考：`codex-rs/agent-graph-store`、`codex-rs/core/src/agent/control` 以及相关 control tests。

Codex 不把进程退出、turn 完成、child 关闭和业务成功视为同一件事。GA 应至少区分：

```text
process_status
turn_status
job_status
acceptance_status
execution_outcome
```

### 3.4 协议测试：验证事件和结构，不信任最终摘要

参考：`codex-rs/app-server/tests/suite/v2/multi_agent_v2_developer_instructions.rs`、`codex-rs/core/tests/suite/review.rs`。

Codex 测试 developer instructions、role precedence、event sequence、structured output 和 completed/aborted/failed 状态转换，而不是只断言最后一句文本。

## 4. 修复契约

### 4.1 Role 传播

计划中的 agent role 必须进入 `agent()` options、job metadata、child prompt 和 transcript metadata：

```json
{
  "label": "verify",
  "phase": "Verification",
  "role": "verification"
}
```

### 4.2 Acceptance contract

coding workflow 必须声明验收契约。契约至少包含：

```json
{
  "acceptance": {
    "required": true,
    "failWorkflowOnError": true,
    "checks": ["python_unittest", "verification_schema"]
  }
}
```

没有验收契约的 coding plan 应被拒绝或标记为不可执行，而不是静默执行。

### 4.3 Verification 的结构化输出

verification agent 应返回机器可校验的结果，例如：

```json
{
  "verificationPassed": false,
  "checks": [
    {
      "name": "python-unittest",
      "passed": false,
      "returncode": 1,
      "evidence": "8 NameError failures"
    }
  ],
  "blockingIssues": ["_APIKEY_RE is undefined"]
}
```

缺少必需字段、schema 不匹配或证据不完整时，应是 `blocked`/`failed`，不能是成功。

### 4.4 Host-side outcome

模型摘要只能作为解释性证据，不能决定 workflow 是否成功。最终 outcome 必须由宿主根据：

- 测试退出码；
- verification schema；
- acceptance contract；
- artifact 和事件证据；

计算得出。

推荐最终状态示例：

```json
{
  "status": "failed",
  "executionOutcome": "failed",
  "childSummary": {"total": 6, "succeeded": 6, "failed": 0},
  "acceptanceStatus": "failed",
  "acceptanceFailures": ["_APIKEY_RE is undefined", "8 tests failed"]
}
```

这表示所有 child turn 都正常结束，但业务任务没有通过验收。

## 5. 实施顺序

严格遵循 TDD：

1. 增加回归测试，复现“自然语言报告失败但 workflow succeeded”。
2. 增加 role 渲染与 metadata 传播测试。
3. 增加 acceptance contract validator 测试。
4. 增加严格 verification schema 和缺字段失败测试。
5. 将 acceptance failure 接入 `executionOutcome`、`final-result.json` 和 workflow status。
6. 为 verification/review 增加独立、受限 prompt。
7. 使用真实 DeepSeek 重新运行 Understand → Tests → Implementation → Verification → Synthesis 闭环。

## 6. 不应采用的修复

- 只把 planner prompt 写得更长，但不增加 host-side gate；
- 通过关键词搜索“失败/NameError”来决定 workflow 失败；
- 让 synthesis 的自然语言覆盖宿主测试结果；
- 对 verification schema 默认使用 `fallback: "text"`；
- 把所有 child turn 的 `succeeded` 直接当作业务成功。

## 7. 验收标准

- child 摘要声称失败时，若没有可验证证据，不得被标记为成功；
- verification 明确返回失败时，workflow 必须失败；
- 测试退出码非零时，workflow 必须失败；
- 所有 child job succeeded 但 acceptance failed 时，状态必须能同时表达两者；
- role 能在 plan、job、prompt 和 transcript 中追踪；
- 旧的 research workflow、缓存、repair/retest 和成功路径测试保持通过。

## 8. 本次按指南实施的改动

已按上述契约完成第一版修复：

- coding plan 现在必须包含 verification role、严格 verification schema 和 acceptance contract；
- planner renderer 将 role 写入 agent options，并在 child prompt/transcript 中保留 role 与角色指令；
- coding acceptance contract 自动生成宿主侧 `runPythonUnittest(..., gateKey: "workflow-acceptance")` 门禁；
- runtime 根据真实 unittest 退出结果和结构化 `verificationPassed` 计算 acceptanceStatus，不能由自然语言摘要覆盖；
- strict schema agent 可以从 child 返回的 JSON 文本中恢复结构化 payload，schema 不匹配仍然失败；
- `executionOutcome`、`workflow-progress.json` 和 `final-result.json` 同时记录 acceptanceStatus/acceptanceFailures；
- 新增回归测试覆盖缺少测试门禁、成功验收、role 传播、严格 schema 和 planner contract。

另外，真实 DeepSeek 首轮重跑暴露出 planner contract 过于脆弱：模型正确生成了 coding role 和 acceptance，但没有填写 GA 内部的 `schemaRef`/`strictSchema`，因此在 runtime 启动前被 `missing_strict_verification_schema` 拒绝。该问题不是模型连接失败，也不是应该移除硬门禁；正确做法是保留 validator 的硬门禁，并在 `LLMWorkflowPlanner` 进入 validator 前执行 contract normalization：

- 仅对 `taskType=coding` 的 `role=verification` agent 生效；
- 已有合法严格 schema 时原样保留；
- 缺失、不完整或不严格时补齐 `GA_WORKFLOW_VERIFICATION_SCHEMA`；
- 将结构化输出要求追加到 verification prompt；
- repair response 也必须再次规范化。

这样模型不需要猜测 GA 的私有 schema 名称，但 runtime 仍只接受严格结构化验收证据。

## 9. 回归验证记录

2026-09-30 已运行（在新增 acceptance 回归前的基线）：

```text
python -m unittest discover -s tests
Ran 974 tests in 161.856s
OK (skipped=3)
```

新增回归重点包括：

- acceptance contract 缺少宿主测试门禁时 workflow 失败；
- unittest 门禁和结构化 verification 同时通过时 workflow 成功；
- role 从 plan 传播到 options、child prompt 和 transcript；
- strict schema 可从 child JSON 文本恢复结构化 payload；
- coding plan 缺少 verification、acceptance 或 strict schema 时被 validator 拒绝。
- prompt-guided coding plan 缺少 `schemaRef`/`strictSchema` 时被规范化为 GA 标准严格 schema，并保持 validator 通过。
- 完成异常路径状态传播后，全量回归为 `Ran 976 tests in 165.180s; OK (skipped=3)`。

## 10. 真实 DeepSeek acceptance retest

2026-09-30 使用真实 `deepseek-v4.1-flash` 重跑完整 planner → runtime 闭环，摘要保存在 `temp/deepseek_acceptance_retest_20260930_v2.json`（不包含 API key）。本次 planner 首次输出的 verification agent 没有填写内部 schema 名称，normalization 自动补齐并在 draft 中落盘为：

```json
{
  "schemaRef": "GA_WORKFLOW_VERIFICATION_SCHEMA",
  "strictSchema": true
}
```

真实 child 阶段证据：

- planner `validation.ok=true`，7 个有序 phase，6 个 job；
- Research child 成功访问 Python 官方 `re`/`shlex` 文档；
- Tests child 先观察到导入缺失的 RED；Implementation 后真实 unittest 达到 46/46 GREEN；
- Verification child 返回内容缺少 `verificationPassed`、`checks`、`blockingIssues`，被严格 schema 拒绝；
- 最终 `runStatus=failed`、`executionOutcome=failed`、`acceptanceStatus=failed`；6 个 job 中 5 个 succeeded、1 个 failed；
- `workflow-progress.json` 和 `final-result.json` 均同步记录 acceptance failure，未出现“所有 job 完成即业务成功”。

这次结果应判定为“验收机制通过、业务任务未通过”：DeepSeek 的实现和测试阶段确实完成，但 verification 协议不合格，GA 正确 fail-closed。为补齐异常路径状态传播，runtime 现在在 required acceptance 的 schema/runtime 异常中也写入 `acceptanceStatus=failed` 和 `acceptanceFailures`。
