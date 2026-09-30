# Ultracode Skill 与 GenericAgent Dynamic Workflow 对比及稳定性优化指南

- 日期：2026-09-30
- 对比对象：`D:\git_codes\ultracode-skill\ultracode`
- 目标项目：`D:\git_codes\GenericAgent`
- 基线提交：`13c36b9 feat(workflow): enforce acceptance contracts and persist outcomes`
- 适用范围：planner、workflow graph、scheduler、child agent、artifact、verification、repair/retest 和最终集成

## 1. 结论先行

Ultracode 的价值不在于“多启动几个 agent”，而在于把动态 workflow 变成一套可检查、可恢复、可审计的执行协议：

```text
任务判型
  -> 选择 direct / workflow / delegated mode
  -> 生成 plan + orchestration + eval contract
  -> 将工作拆成有 owner、边界和下游契约的 packets
  -> 只委派独立且不阻塞父路径的 sidecar 工作
  -> 父会话负责 integration
  -> 按 contract 顺序执行验证和 final audit
```

GA 已经具备较强的 runtime 基础（phase、依赖、权限、MCP、artifact、journal、schema、host test gate），但当前更像“模型生成 JS 后直接执行”。稳定性问题主要来自协议层缺口：

1. 模型需要一次性猜中内部 plan schema、依赖拓扑和 verification schema；repair 主要依赖再次提示模型。
2. plan 没有显式 mode、风险、成功标准、owner、handoff 和 eval contract，父运行时无法在执行前判断“这是否是可执行计划”。
3. 依赖图校验还没有完整的 cycle、重复依赖和 phase topology 诊断，模型出错时反馈不够具体。
4. runtime 的 acceptance 已能阻止业务误报成功，但 schema/runtime 异常的 bounded repair、重试预算和最终审计仍不统一。
5. GA 有 `workflow-draft.json`、`journal.jsonl`、`workflow-progress.json`，但没有 Ultracode 风格的 plan/orchestration/integration/final-audit 运行协议，排查时需要从多个底层 artifact 反推意图。

因此，优化方向应是“宿主硬约束 + 模型软约束 + 可追踪 artifact + 有界恢复”四层叠加，而不是继续堆长 prompt。

## 2. Ultracode 可借鉴机制

证据来自：

- `ultracode/SKILL.md`
- `ultracode/references/packet-schema.md`
- `ultracode/references/eval-contracts.md`
- `ultracode/references/forward-testing.md`
- `ultracode/references/approval-gates.md`

### 2.1 Mode router：先决定执行深度

Ultracode 将任务分成：

- `direct`：小而清晰的修改，不制造 workflow ceremony；
- `workflow`：有多阶段、不确定性或需要独立证据，但不适合委派；
- `delegated`：存在独立 packet 且宿主允许 native agent 时，受限 fan-out。

可借鉴点：GA planner 当前直接产出 phases/agents，缺少“为什么要 workflow、是否允许 delegation、父路径保留什么”的显式判断。mode router 可以减少不必要的复杂 workflow，也避免把必须由父 agent 集成的任务全部下放给 child。

### 2.2 Packet contract：owner、write scope、handoff

Ultracode 的 packet 至少声明：objective、context、sources、ownership、Do/Do not、expected output、verification、write scope 和 coordination rule。结果必须返回 Summary、Evidence、Handoff、Files changed、Decisions、Risks、Verification run、Open questions。

可借鉴点：GA 的 agent 目前主要有 label、role、prompt、dependsOn、schema。应增加结构化 `owner`、`writeScope`、`deliverables`、`handoff` 和 `evidence`，让下游 agent 不必从长 transcript 猜测上游完成了什么。

### 2.3 Eval contract：把“完成”定义成可验证条件

Ultracode 提供 `none`、`inline`、`full` 三档 eval contract。contract 明确 outcome、shared surfaces、required checks、blocking conditions 和 handoff evidence；高风险/跨表面任务才使用 full contract，避免过度官僚化。

可借鉴点：GA 已有 acceptance contract，但它主要表达 `python_unittest` 和 `verification_schema`。应扩展为 task-level contract，并在 planner、controller、runtime、final-result 中保持同一份 contract，防止“计划目标”和“最终验收”漂移。

### 2.4 Parent critical path：父 agent 保留集成权

Ultracode 明确要求：不要委派父路径上下一步所需的阻塞工作；sidecar 只做独立、可并行、可丢弃的探索或局部修改；最终 integration 留在父会话。

可借鉴点：GA scheduler 可以执行依赖，但没有明确区分“阻塞父路径的必需 job”和“可失败但不阻塞的 sidecar job”。这会导致研究、实现、验证、汇总的失败传播不透明。

### 2.5 有界 delegation：控制 fan-out、wave 和等待点

Ultracode 默认 2-4 个 sidecar agent，总数不超过 5；最多一轮 broad implementation wave 加一轮 review/verification wave；只在结果阻塞下一步时等待。

可借鉴点：GA 有 `max_concurrent` 和 `max_total`，但它们是资源上限，不是 orchestration policy。应把 `maxAgents`、`maxWaves`、`waitPoints`、`failurePolicy` 写进计划并由 host 校验。

### 2.6 Artifact protocol：运行意图和结果分离

Ultracode 的标准运行目录是：

```text
.workflow/ultracode/<run-slug>/
  plan.md
  orchestration.md
  state.json
  packets/
  results/
  integration.md
  final-report.md
```

高风险任务再增加 `eval-contract.md`、`contracts/`、`handoffs/`、`final-audit.md`。

可借鉴点：GA 现有 JSON artifact 更适合机器消费，但缺少面向调试者的执行意图文档。建议保留 JSON 为 source of truth，同时生成精简 Markdown projection，不让模型临时重建上下文。

### 2.7 Forward testing：用行为样例约束 workflow

Ultracode 的 forward-testing 不只测代码，还测试 mode、artifact、delegation、approval gate、fallback 和 eval contract。每条测试都定义 expected behavior，且不把 expected answer 直接塞进模型 prompt。

可借鉴点：GA 目前真实 E2E 更偏能力演示；应增加 planner-only contract tests、artifact protocol tests、repair budget tests 和 delegation fallback tests，降低“模型这次恰好输出正确 JSON”造成的假稳定。

## 3. GA 当前稳定性问题的证据

### 3.1 Planner 输出脆弱

真实 `deepseek-v4.1-flash` 测试曾出现：

- acceptance contract 已生成，但 verification 缺失 `schemaRef`/`strictSchema`；
- coding plan 被分类为 mixed；
- verification 依赖了同 phase 的 refactor label；
- verification child 返回自然语言而不是严格结构化对象。

GA 已补充 schema normalization 和硬门禁，但这些事件说明 planner 仍需要“可恢复 contract”，不能只依赖 prompt 精确命中。

### 3.2 依赖图诊断不够完整

当前 validator 主要通过“依赖 label 是否已出现在前序 phase”判断合法性。它能阻止部分错误，但没有区分：

- 未定义 label；
- same-phase dependency；
- cycle；
- duplicate dependency；
- dependency 指向被跳过/失败的 job；
- sidecar 失败是否阻塞 downstream。

错误码不够细会让 repair prompt 难以采取针对性修复。

### 3.3 Runtime 已有验收门，但缺少统一恢复策略

当前 `repairAndRetest` 主要针对 Python unittest；schema validation、tool denial、MCP discovery failure、child timeout 和 provider anomaly 没有统一的 retry/repair budget 与 escalation reason。

如果无限重试，workflow 会变慢并放大模型幻觉；如果完全 fail-fast，又无法处理一次性 provider/网络波动。需要按错误类型声明 retryable、attempt budget、backoff 和是否允许 repair agent。

### 3.4 Artifact 对排错者不够友好

`run.json`、`state.json`、`script.js`、`journal.jsonl`、`workflow-progress.json`、agent transcript 已经足够保存事实，但缺少：

- 计划目标和成功标准的单页摘要；
- orchestration wave 和 parent critical path；
- packet owner/write scope；
- integration 接受/拒绝哪些结果；
- final audit 是否检查过 contract。

## 4. 稳定性优化方案

### P0：确定性 workflow contract（先做）

1. planner prompt 要求输出 `mode`、`riskLevel`、`successCriteria`、`evalContract`、`orchestration`。
2. planner normalization 为旧模型输出补默认值，但不放宽已有 acceptance/schema 硬门禁。
3. validator 增加依赖图 cycle、duplicate dependency、same-phase dependency 的明确错误码。
4. coding plan 强制单独的 verification phase；research/review plan 要求 synthesis 的上游依赖完整。
5. controller/store 生成 `plan.md`、`orchestration.md` 的 JSON projection，便于人工和后续 agent 读取。

### P1：有界恢复与证据协议

1. agent options 增加 `retryPolicy`：`maxAttempts`、`retryableErrors`、`backoffMs`、`repairRole`。
2. schema failure、timeout、provider transient error 和 MCP transient error 使用不同默认策略。
3. 所有 child result 统一包含 `status`、`summary`、`evidence`、`handoff`、`blockingIssues`；verification 继续使用 strict schema。
4. downstream prompt 只注入上游 result 的 compact handoff，不把整个 transcript 作为上下文。
5. runtime 增加 `integrationStatus` 和 `finalAuditStatus`，区分“child 已完成”和“父集成已接受”。

### P2：模式路由和 forward-testing

1. 增加 direct/workflow/delegated router；小任务不创建复杂 workflow。
2. 增加 bounded delegation policy：总 agent 数、wave 数、并发度和等待点。
3. 增加 approval gate：广泛 codemod、删除/覆盖、外部发布、真实凭据、长时间 agent swarm 必须停在 awaiting approval。
4. 增加 forward-testing 场景：direct、workflow、delegated、fallback、approval、eval-contract、repair budget。

## 5. 本轮实现边界

本轮先实施 P0 的确定性 contract，不立刻重写 scheduler：

- 增加 workflow contract normalization；
- 增加 graph validator 诊断；
- 增加 planner prompt 的 mode/risk/eval/orchestration 软约束；
- 生成可供调试的 plan/orchestration artifact projection；
- 为每个新增行为先写 RED 测试，再实现并跑 workflow 测试。

不在本轮做：

- 改变已有权限默认值；
- 放宽 strict verification；
- 引入第三方依赖；
- 自动提交/推送；
- 无界 retry 或自动批准高风险操作。

## 6. 验收标准

- 旧 research/review/coding 计划仍能通过既有测试；
- 缺失 contract 的模型计划经过 normalization 后具备可执行默认 contract；
- same-phase dependency、duplicate dependency、cycle 分别返回稳定错误码；
- plan/orchestration artifact 与 `workflow-draft.json` 同一次 run 生成，内容不含 secret；
- strict verification 和 acceptance gate 行为不回退；
- 全量 `python -m unittest discover -s tests` 通过；
- 真实模型 E2E 失败时，artifact 能明确区分 planner rejected、child failed、acceptance failed、integration pending。

## 7. 后续切片

完成 P0 后，再按独立提交推进：

1. `retryPolicy` 与 transient error bounded retry；
2. compact handoff/evidence envelope；
3. integration/final-audit 状态；
4. direct/workflow/delegated router；
5. forward-testing 和真实多模型回归矩阵。

## 8. P0 已实施结果

本轮已将 P0 确定性 contract 优化落到 GA：

1. `workflow_planner.py` 增加 contract normalization，兼容旧模型计划的同时补齐 `workflowContractVersion`、`mode`、`riskLevel`、`successCriteria`、`evalContract` 和 `orchestration`。
2. 每个 agent packet 现在会显式补齐 `owner`、`writeScope` 和 `deliverables`，让父 agent 能识别责任边界与交付物。
3. 计划校验增加 `same_phase_dependency`、`forward_dependency`、`duplicate_dependency` 和 `dependency_cycle` 诊断，repair prompt 可据此给出定向修复。
4. `workflow_store.py` 为每次 planned run 投影 `plan.md` 和 `orchestration.md`，保留 goal、success criteria、work packets、parent critical path、delegation、wait points 和 verification order。
5. `workflow_controller.py` 将 contract 字段及 artifact 引用写入 run metadata 和 journal，便于后续 runtime acceptance 与最终审计关联。

本轮没有放宽 strict verification、权限默认值或高风险 approval gate，也没有引入无界重试。新增回归测试覆盖依赖图诊断、prompt-guided contract normalization 以及 artifact projection；完成全量测试后再提交该切片。
