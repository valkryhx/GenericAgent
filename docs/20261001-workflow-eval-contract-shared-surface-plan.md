# Workflow eval contract：第二条正交轴（共享面 → 契约档位）

## 1. 背景

上一轮把「是否施加 coding 契约」的判据从 `taskType` 换成了计划声明的形状
（`plan_produces_code`）。同一次排查发现另一处遗留：`evalContract.level`
仍然由 `taskType` 推导：

```python
# workflow_planner.py
"level": str(eval_contract.get("level") or ("full" if task_type in {"coding", "debugging", "mixed"} else "inline")),
```

`mixed` 一律 `full`、`research` 一律 `inline`，与任务是否真的触碰共享面无关。
这是同一个反模式余留在另一个字段上。

它目前「错得无害」——没有任何运行时分支读 `level`，它只落到 `plan.md` 当展示；
但一旦有人真的按 `level` 加严验收，同一个「枚举猜错 → 契约挂错」的失效模式
会立刻复现。

## 2. 真实数据：哪些信号可用

对 4 次真实 `deepseek-v4.1-flash` run 的 `evalContract` 取证：

| run | taskType | level | sharedSurfaces |
| --- | --- | --- | --- |
| complex_detail | mixed | inline | `[]` |
| complex_detail2 | mixed | full | `[]` |
| complex_detail3 | mixed | full | `[]` |
| complex_detail4 | mixed | full | `[]` |

结论：

- `sharedSurfaces` **4 次全空**，模型不填，不能作为判据（与 `writeScope` 同类）；
- `level` 同一任务在 `inline`/`full` 间飘，`taskType` 推导不可信；
- 但**依赖图是可靠且可计算的**：同一个计划里 producer→consumer 消费边稳定存在
  （research 2 条、review 5 条），且这些边正是 `dependsOn`、由 validator 已解析。

因此第二条轴的判据是**宿主从计划图计算**，不是让模型声明。

## 3. 轴的定义（对齐 ultracode）

ultracode `eval-contracts.md` 对档位的定义：

- `none`：任务很小、单文件、无 packet 产出被其他 packet 消费；
- `inline`：中等风险、scope 分离、packet 之间**没有**脆弱的共享面；
- `full`：写型 agent 共享集成面 / **一个 packet 产出的东西被另一个 packet 消费** /
  触及公共 API、schema、CLI、UI 流、迁移、auth、数据契约或共享模块。

映射到 GA：

```python
def plan_shared_surfaces(plan) -> list[dict]
    # 宿主从依赖图推导：对每个被下游 dependsOn 引用的 producer label，
    # 产出一条 {surface, producer, consumers[], structured: bool}
    # structured = producer 声明了 schemaRef（结构化契约），否则为文本交接

def plan_eval_level(plan) -> str   # "none" | "inline" | "full"
    # none  : 没有 phases（direct mode）
    # full  : 存在跨 packet 消费边，或计划声明了 artifacts/schemas 契约，
    #         或 riskLevel == high，或 plan_produces_code 且存在消费边
    # inline: 其余（有多 agent 但无跨 packet 消费）
```

重要：**不再读 `taskType`**。`taskType` 退化为提示与展示信息。

## 4. `full` 档带来的真实行为（本轮引入）

`level` 必须真的改变行为，否则只是换了个装饰字段。本轮定义 `full` 的唯一新增行为：

**要求生成 `final-audit.md`**，由宿主从已记录事实生成（不是 agent 自述）：

```
# Final audit

## Eval contract
- level: full
- outcome: ...

## Shared surfaces
| surface | producer | consumers | structured |

## Checks applied
- plan_validation: pass
- acceptance: pass
- test gates: not_applicable / pass / fail

## Integration
- integrationStatus: accepted
- finalAuditStatus: passed
```

约束：

- audit 内容全部来自 `run.metadata` / `testGates` / job handoff，**不读 agent 自然语言**；
- `full` 档 run 若 audit 无法枚举任何共享面（既无消费边也无声明），记录
  `auditStatus=insufficient_evidence` 并保留，不伪造；
- **不改变现有 pass/fail 语义**：audit 是证据产物，本轮不做「audit 缺失即失败」的
  fail-closed，避免一次性引入未经真实 E2E 验证的新门禁。是否升级为硬门禁留待下一轮。

## 5. 受影响代码

- `workflow_planner.py`
  - 新增 `plan_shared_surfaces()` / `plan_eval_level()`；
  - `_normalize_workflow_execution_contract()` 中 `eval_contract["level"]` 改由
    `plan_eval_level()` 决定，`sharedSurfaces` 改由 `plan_shared_surfaces()` 填充
    （合并模型显式声明，但宿主计算结果为准）；
  - planner prompt 增加「不要臆造 sharedSurfaces，宿主会从依赖图推导」。
- `workflow_runtime.py`
  - run 成功收尾时，`level == "full"` 则生成 `final-audit.md` 并写入
    `run.metadata["evalContractRef"]`；
  - `_final_payload()` 带上 `evalLevel` / `sharedSurfaces` / `finalAuditRef`。
- `workflow_store.py`
  - 新增 `write_final_audit(run, payload)`。

## 6. 不做的事

- 不把 `full` 变成硬门禁（本轮只产出证据）；
- 不让模型声明决定档位（只作为附加输入）；
- 不引入新的 shell 检查执行面；
- 不动上一轮的 `plan_produces_code` 行为。

## 7. 验收标准

1. `sharedSurfaces` 在真实计划中非空，且与 `dependsOn` 图一致；
2. 同一计划重复计算 `plan_eval_level()` 结果稳定（无 LLM 参与）；
3. `taskType` 改变不再影响 `level`（单测锁定）；
4. `full` 档 run 产出 `final-audit.md`，内容来自记录事实；
5. `direct`（无 phases）计划 `level == "none"`；
6. 全量回归通过；真实 deepseek 双 E2E 通过。

## 8. 实施顺序

1. RED 测试：`level` 与 `taskType` 解耦、`sharedSurfaces` 非空、`none` 档；
2. 实现 `plan_shared_surfaces()` / `plan_eval_level()` 并接入 normalization；
3. 实现 `final-audit.md` 生成；
4. 跑聚焦测试 → workflow 全量 → 全量回归；
5. 真实 deepseek 双 E2E 串行验证；
6. 更新文档。


## 9. 实施结果（已完成）

### 9.1 判据回正：`full` 就是「存在跨 packet 消费边」

实现中途曾把 `full` 收紧为「共享面必须脆弱（structured 或 write-capable）」，
但对照 `ultracode/references/eval-contracts.md` 的原文：

> Use `full` when: ... one packet produces a surface another packet consumes

「被下游消费」本身就是 full 的判据，不需要宿主再加一层脆弱性启发式。宿主
自行削弱这条判据，恰恰会重新引入本轴要消除的欠约束交接，而 full 的额外成本
只是一个证据文件。据此回正为：**只要 `plan_shared_surfaces()` 非空即 full**。

### 9.2 第二条依赖通道：`riskLevel` 也曾由 `taskType` 推导

`level` 表面的 `taskType` 耦合被移除后，`riskLevel` 仍是隐藏通道：

```python
risk_level = "high" if task_type in {"coding","debugging","mixed"} else ...
# plan_eval_level() 又读 riskLevel == high -> full
```

同一份 phases，标签写 `coding` 得 `high`，写 `research` 得 `low`。新增
`plan_risk_level()` 改为宿主按计划形状计算，模型声明**只能升不能降**（fail-closed）：

| 计划形状 | 计算结果 |
| --- | --- |
| 有 structured 共享面 / 多个写者 / 共享面 + 写者 | high |
| 单个写者，或仅有非结构化共享面 | medium |
| 无写者、无共享面 | low |

### 9.3 第三条轴：把「不该写」从提示词升级为宿主 ACL

真实数据里 `role` 会被整段省略（同一 complex E2E 一次 `[understanding, implementation, ...]`、
一次全 `None`），所以「验证者不要改代码」这种只写在提示词里的约束不可靠。
对齐 Step-Code `tool-profile.ts` 的 `qa = readOnly + run_command`，新增 `verify` 权限档：

- 允许：读取类工具 + `code_run` / `web_execute_js`（验证必须能跑命令取证）；
- 拒绝：`file_write` / `file_patch`（不能篡改被测代码，也就无法伪造证据）。

由 `resolve_job_permission_profile()` 在派发时强制：`role ∈ {verification, review}`
的子 agent 自动获得 `verify` 档，run 级已是 `read_only` 时不被放松。

### 9.4 第四条轴：用观测替代预测（写了就是写了）

`plan_produces_code()` 仍然只能读声明。补充运行时事实：`_record_observed_mutations()`
扫描子 agent 的 `tool_allowed` 事件，实际调用过写工具就落
`job.metadata["observedMutations"]`、`run.metadata["observedMutationAgents"]` /
`["observedMutationTools"]`，并追加 `state_mutation_observed` journal 事件。
这把「这次到底写没写代码」从猜测变成可审计的事实，供后续门禁读取。

判定用的写工具集合在 `workflow_permissions.py` 中单点定义
（`MUTATING_TOOL_NAMES` / `EXECUTE_TOOL_NAMES`），read-only 档与观测共用同一来源，
不会漂移。

### 9.5 新增/修改测试

- `tests/test_workflow_eval_contract.py`（12 个）：共享面推导、`full`/`inline`/`none`、
  `taskType` 不影响 `level` 与 `riskLevel`、声明风险只能升不能降、normalization 接线。
- `tests/test_workflow_permissions.py`：`verify` 档拒绝写、允许执行。
- `tests/test_workflow_scheduler.py`：证据角色强制 `verify`、read-only run 不被放松、
  观测到的写操作落盘并产生 journal 事件。
- `tests/test_workflow_runtime.py`：`full` 档产出 `final-audit.md` 且内容来自记录事实；
  `inline` 档不产出该文件。

### 9.6 验证

- 聚焦测试：`tests.test_workflow_eval_contract` 12 OK；workflow 相关 114 tests OK。
- 全量回归：`python -m unittest discover -s tests` → **1019 tests OK (skipped=3)**。

### 9.7 `final-audit.md` 的真实行为

`level == "full"` 的 run 收尾（succeeded / failed / killed）由宿主生成
`final-audit.md`，内容全部取自 `plan.md` 的 evalContract、`testGates`、
`integrationStatus`、`acceptanceStatus`，**不读任何 agent 自然语言**。若 full 档
却枚举不到任何共享面，记 `auditStatus=insufficient_evidence` 并保留，不伪造。
本轮 audit 是证据产物，不改变 pass/fail 语义。

## 10. 真实 E2E 暴露的两个生产缺陷（已修）

本节的问题都不是推理出来的，而是 `deepseek-v4.1-flash` 真实跑 planner E2E 时
连续崩出来的。它们与 eval-contract 轴同属「靠约束而不靠猜」的范畴，故一并修复。

### 10.1 `maxAgents` / `maxWaves` 被硬截断（用户指出的设计错误）

`normalize_delegation_policy()` 对所有 mode 无差别执行：

```python
max_agents = max(1, min(MAX_DELEGATED_AGENTS, int(raw_agents)))  # 5
max_waves  = max(1, min(MAX_DELEGATED_WAVES,  int(raw_waves)))   # 4
```

真实后果：一个合法的 5 阶段 TDD 计划（Understand → Tests → Implementation →
Verification → Summary）依赖深度就是 5，模型也如实声明了 `maxAgents: 5`，但
`maxWaves` 被压到 4，注册第 5 波时抛
`RuntimeError: workflow execution limit exceeded`，整个 run 失败。

诊断证据（真实 deepseek 4 场景）：

| scenario | agents | phases | 声明 maxAgents | 声明 maxWaves | 依赖图实际深度 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| research | 4 | 3 | 4 | 3 | 3 | OK |
| review | 6 | 3 | 5 | 3 | 3 | OK |
| coding | 5 | 5 | 5 | 4 | **5** | **失败** |
| planning | 9 | 3 | 5 | 3 | 3 | OK |

关键点：**崩的不是 agent 数量**（coding agents=5 = maxAgents=5），而是 wave 深度。
5/4 是 `delegated`（有界 sidecar）的语义，被错误地套用到了所有 workflow。

修复后的语义：

- `delegated`：保留 `maxAgents<=5`、`maxWaves<=4`（模式定义本身）；
- 普通 `workflow`：只受**安全阀**约束（`HARD_MAX_WORKFLOW_AGENTS = 1000`、
  `HARD_MAX_WORKFLOW_WAVES = 64`），与 Step-Code 的 `DEFAULT_MAX_AGENTS = 1000`
  同口径——安全阀不是对任务规模的判断；
- 新增 `plan_required_waves()`：宿主从 `dependsOn` 图算最长依赖链（实测 coding
  场景 = 5），`maxWaves = max(声明值, phase 数, 图深度)`，宿主永不把预算压到
  低于自己刚接受的计划。

### 10.2 CJK label 导致 JS 标识符冲突

真实 review 场景的 phase/agent label 是中文（如 `中文（PR 范围确认）`）。渲染器
`_js_identifier()` 做 ASCII 清洗后全部退化为空串，统一 fallback 成 `agent`，
于是生成：

```js
const [agent, agent, agent, agent] = await parallel([...])
```

Node 直接 `SyntaxError: Identifier 'agent' has already been declared`，run 失败。
中文任务几乎必踩。

修复：`_js_identifier(label, index=..., used=...)`

- 清洗后为空时使用**位置化**兜底 `agent_{n}`，而不是共享常量；
- 渲染期共享一个 `used` 集合 + 单调计数器，保证全局唯一（防清洗后近碰撞，
  例如两个 label 都只剩下 `PR`）；
- 新增 `_JS_RESERVED_IDENTIFIERS`：label 撞上 `agent` / `phase` / `parallel` /
  `args` / JS 关键字时改名（`agent` → `agent_packet`），避免 TDZ 遮蔽运行时函数
  （`const agent = await agent(...)` 是 ReferenceError）。

### 10.3 连带修复：E2E 夹具本身

`tests/prompt_guided_planner_real_e2e.py` 此前用 `FakeChildAgentRunner`，无法满足
planner 下发的 strict verification schema，也无法提供宿主测试门禁所需的
workspace，导致它测的是夹具而不是 workflow。已补：

- `SchemaAwareFakeRunner`：按声明的 schema 补齐必填字段，`role=verification`
  返回真实的 `verificationPassed/checks/blockingIssues`；
- 提供 `args.workspacePath`，并在计划声明测试时预置 `test_*.py`；
- 传递 `acceptanceContract`（含 `testsDeclared`）与 `evalContract`/`orchestration`，
  与 `WorkflowController.create_planned_run` 的接线一致；
- phase 语义检查改为「phase 或 label 任一命中」，因为 planner 合法地使用
  任务语言命名 phase（实测中文 phase + 英文 label）。

### 10.4 验证

- `tests/prompt_guided_planner_real_e2e.py`（真实 deepseek-v4.1-flash）：
  **passed=true, issues=[]**；research 7 / review 6 / coding 5 / planning 9
  个 agent 全部 succeeded，四个场景 `level=full` 且都产出 `final-audit.md`。
- 新增单测：`test_workflow_policy.py` 4 个（wave 深度、delegated 预算、
  宿主不下压预算）、`test_workflow_plan_validator.py` 2 个（CJK 标识符唯一、
  不遮蔽运行时函数）。
- 全量回归：**1025 tests OK (skipped=3)**。
