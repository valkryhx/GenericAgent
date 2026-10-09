# 2026-10-03 GA Workflow 证据模型稳定性修复与真实 DeepSeek 验证

## 背景

用真实的 `deepseek-v4.1-flash` 通过 GA Ink 反复执行同一个 workflow 任务
（2 个 Tavily 研究 subagent 写两个 JSON，再综合成一个 HTML），每一轮都稳定地
暴露**一个新的失败点**：

1. `artifact path must be a non-empty path within workspace`
2. `missing_verification_check`
3. `missing_artifact_readback_evidence: tmp/liu_guoliang_profile.html`
4. `required verification check sports_json_schema has no evidence`
5. `output writer does not depend on <research agent>`（最终被判定为 E2E harness 缺陷）

这种「修一个冒一个」的模式不是偶发 LLM 抖动，而是 GA workflow 的**证据模型**
问题。本文记录根因、对照 Step-Code 的结论、修复内容与真实验证结果。

## 根因：GA 的「双契约 + transcript 事后取证」

对照 `D:\git_codes\Step-Code`（`packages/coding-agent/src/features/workflow/`）：

- Step-Code 只有一个契约——**生成的 JS 脚本本身**。校验在执行期发生：
  - `agent(prompt, {schema})` 由宿主校验 JSON Schema，最多重试 3 次后失败
    （`runtime.ts` `validateWorkflowSchema` / `WorkflowSchemaError`）；
  - 文件访问由 `readOnly` / `writable` ACL 在执行期硬约束
    （`tool-profile.ts` `checkWorkflowPathAccess`）；
  - **没有** post-run 的 artifact registry，也**没有**扫描子 agent transcript
    来「重建证据」的阶段。

- GA 有两套并行描述同一件事的契约，并且**事后扫描 transcript 反推证据**：
  - `executionContract.artifacts[].requiredChecks`（权威、机器检查）
  - `verification.checks`（模型自撰、kind 可为 artifact/schema/…）
  - `workflow_runtime._evaluate_execution_contract_evidence()` 通过读取子 agent
    transcript 来判断「是否有 readback 证据」。

  LLM 输出是不确定的：两次运行里 `verification.checks` 的形状、字段名、是否为空
  都不一样。宿主又用脆弱的位置/启发式去匹配这些自由文本，于是每个新形状都变成
  一个新的假失败。这就是「一直在补漏」的来源。

## 具体缺陷与修复

### 1. transcript 取证有 64 KB 头部窗口偏差（真实运行触发）

`WorkflowStore.read_agent_transcript_events()` 只读取 transcript 的前 64 KB。
真实 run 中两个研究 transcript 分别约 591 KB / 940 KB，HTML 的 readback
`file_read` 落在约 78 KB 处，**物理存在却被判为缺失**，workflow 因此假失败。

修复：改为流式逐行读取整个 transcript，`max_bytes` 降级为「单行安全上限」，
`max_events` 才是返回条数上限。回归测试
`test_transcript_reader_sees_events_beyond_legacy_64k_head_window`（先红后绿）。

### 2. 权威契约是 `executionContract.artifacts`，不是模型自撰的 verification 视图

`verification.checks` 里的 artifact 检查改为对执行契约的**确定性派生视图**，
绑定规则只有三条，不做 owner/扩展名/散文猜测：

1. 显式 `path`（兼容别名 `artifact` / `artifactRef` 归一为 `path`）；
2. 检查 id 恰好命中某一个 artifact 声明的 `requiredChecks`；
3. 整个契约只声明了一个 artifact。

无法绑定的 artifact 检查被丢弃（执行契约已经强制它），空视图则从
`executionContract.artifacts[].requiredChecks` 派生必检项。相关测试：
`test_defers_unbound_artifact_checks_to_execution_contract`、
`test_binds_artifact_checks_by_declared_required_check_id`、
`test_derives_verification_checks_when_model_leaves_view_empty`。

### 3. 无证据来源的 standalone schema 检查降级为 advisory

模型会把 artifact 的 `schema_valid` 又抄成一个独立的 `verification.checks`
（如 `sports_json_schema`），而 GA 并没有把 plan schema 接到该检查的宿主求值器。
把它当硬门禁会让「产物完全合法」的 run 失败。现在这类检查保留可见但标记为
advisory，真正可执行的约束留在执行契约 artifact 的 `schema_valid` 上。
测试：`test_unbacked_schema_check_becomes_advisory`。

### 4. E2E harness 的 artifact 选择过于刚性（测试自身缺陷）

harness 假设 `executionContract.artifacts[0]` 就是最终交付物，但真实计划里
artifacts[0] 是研究 JSON，最后一个才是 HTML。workflow 本身已经
`status: succeeded`。已改为按依赖图选择「消费全部研究产物的终态 artifact」，
并按扩展名回退。

## 真实验证（deepseek-v4.1-flash，GA Ink）

命令：

```bash
cd frontends/ink-ui
GA_WORKFLOW_E2E_PROFILE=deepseek-v4.1-flash GA_WORKFLOW_E2E_START_TIMEOUT_MS=600000 GA_WORKFLOW_E2E_TIMEOUT_MS=600000 GA_WORKFLOW_E2E_TASK='使用workflow进行如下任务：启动2个subagent分别使用Tavily从不同角度调研刘国梁，把详细来源写入workspace内的两个JSON文件；随后读取这两个文件，生成一个workspace内的HTML介绍页面并输出路径。' npx tsx scripts/real_ink_ui_workflow_autonomous_e2e.ts
```

结果：

```json
{
  "passed": true,
  "modelProfile": "deepseek-v4.1-flash",
  "status": "succeeded",
  "runId": "wf_ac573b0741c8418bb221824515b75529",
  "workspacePath": "D:\git_codes\GenericAgent\frontends\ink-ui",
  "artifactRelativePath": "tmp/liuguoliang_profile.html",
  "artifactBytes": 27395,
  "uiReturnedToIdle": true,
  "realTavilyEvidenceContract": true,
  "researchAgentsWithTavily": 2
}
```

关键证据（handoff 压缩生效）：

| job | label | 状态 | prompt 字符数 | transcript 字节数 |
| --- | --- | --- | --- | --- |
| agent_1 | Research career and coaching achievements | succeeded | 254 | 591,064 |
| agent_2 | Research public influence and controversies | succeeded | 265 | 940,070 |
| agent_3 | Synthesize HTML profile page | succeeded | 310 | 76,938 |

即研究 child 的 transcript 接近 1 MB，但下游综合 child 只收到约 310 字符的
bounded prompt + `workflow-handoffs/*.json` 引用，不再把完整 transcript 灌进
LLM 请求。产物落在 GA 启动位置（ink-ui）下的 `tmp/liu_guoliang_profile.html`。

## 测试

- 聚焦套件：`tests.test_workflow_execution_contract`（19）、
  `tests.test_workflow_store`（22）、runtime / scheduler / verification 等
  合计 194 项通过。
- 全量：`python -m unittest discover -s tests` → **1181 tests, OK (skipped=3)**。

## 结论与后续方向

GA 剩余的 workflow 不稳定主要来自「用 LLM 自由文本 + transcript 事后取证」这一段。
已完成的修复把 artifact 校验收敛到权威执行契约，并消除了取证的位置偏差。后续若继续
提质，应沿 Step-Code 方向推进：把更多校验前移到执行期由宿主确定性完成
（如把 `schema_valid` 接到 plan schema 的宿主求值器），而不是继续增加事后启发式。
