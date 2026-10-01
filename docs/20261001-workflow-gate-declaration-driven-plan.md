# Workflow 门禁改造：从「任务类型预测」到「声明 + 观测事实」

## 1. 背景与问题

真实 `deepseek-v4.1-flash` forward matrix 暴露失败：

```
RuntimeError: workflow test gate failed: gate-1: Ran 0 tests in 0.000s
NO TESTS RAN
```

链条是：planner prompt 的 `requiredShape.acceptance` 示例**无条件**写着
`["verification_schema", "python_unittest"]`，模型照抄到研究型计划上；
`render_workflow_plan()` 只看 acceptance 含不含 `python_unittest` 就下发宿主
unittest 门禁；研究型工作区没有 `test_*.py`，门禁 `Ran 0 tests` 被记为失败，
整个 run 失败。

## 2. 关键结论：不要预测，要观测

尝试过的每一种「计划期判定是不是 coding」的方案都被真实数据否掉：

| 判据 | 反例 |
| --- | --- |
| 任务文本关键词 | 中文表述多样，「调研 + 落地」混合任务必然误判 |
| `taskType` 枚举 | 模型在 `mixed`/`research`/`coding` 间摇摆；同一任务两次运行结果不同 |
| agent `role` | 同一 complex E2E 两次运行：一次声明 `implementation`，一次 `role` 全为 `None` |
| `writeScope` | 四次真实运行全部为 `[]` |

根因是：**「这次会不会写代码」只有执行完才知道**。宿主在计划期做任何预测，
都会同时产生两类错误：误挂门禁（研究型被套代码约束）和漏挂门禁（真写了代码却
没有验收）。AGENTS.md 已记录 Step-Code 的对照实现——它的 workflow 契约里
根本没有 `taskType`，验收下移到 agent + schema，宿主只做 schema/ACL/budget。

因此本方案的原则是：

1. **计划声明要跑什么检查**（declaration）；
2. **宿主按声明执行，并按运行时可观测事实决定该检查是否适用**（observation）；
3. **`taskType` 只作为 planner 提示与 UI 展示信息，不参与任何门禁判定**。

## 3. 方案设计

### 3.1 两个声明判据（仅用于计划期契约检查，不用于预测代码）

```python
def plan_produces_code(plan) -> bool
    # 计划含 role ∈ {implementation, tests, repair}，或任一 agent 声明非空 writeScope，
    # 或 taskType ∈ {coding, debugging}（fail-closed 兜底）

def plan_declares_tests(plan) -> bool
    # 计划含 role == "tests" 的 agent：即 workflow 自己会产出测试
```

`plan_produces_code` 的定位是**契约一致性检查**：决定是否施加
「canonical role 必填 / tests 与 implementation 不得同 phase / 必须有
verification agent / verification 必须 strict schema」。它不再决定
`python_unittest` 是否下发。

### 3.2 `python_unittest` 门禁：声明 + 观测

门禁是否**适用于本次 run**，由运行时观测决定，而不是计划期预测：

| 计划声明 `tests` role | 工作区发现 `test_*.py` | 门禁行为 |
| --- | --- | --- |
| 是 | 是 | 执行；不通过则失败（硬门禁） |
| 是 | 否 | 失败：声明要产出测试但没有测试，属真实缺陷 |
| 否 | 是 | 执行；不通过则失败（仓库原有测试同样要过） |
| 否 | 否 | **不适用**：记录 `skipped: no tests discovered`，不作为失败 |

关键点：**不适用 ≠ 静默放行**。门禁结果写入 `workflow-progress.json` 与
journal，`acceptanceStatus` 记录该检查为 `not_applicable` 并带原因，可审计。

### 3.3 planner prompt 去偏置

`requiredShape.acceptance` 不再无条件展示 `python_unittest`，改为中性描述 +
前置条件说明：

- acceptance checks 由任务自身决定；
- 仅当本次 workflow 会写或跑 Python 测试时才声明 `python_unittest`；
- 只做研究/审阅/规划时声明 `verification_schema` 即可。

### 3.4 受影响代码

- `workflow_planner.py`
  - 新增 `plan_produces_code()` / `plan_declares_tests()`；
  - `_plan_declares_code_work()`（上一轮临时启发式）被 `plan_produces_code()` 取代；
  - `_normalize_coding_acceptance_contract()` 的 `taskType != "coding"` 早退改为按 `plan_produces_code()`；
  - `validate_workflow_plan()` 的 `is_coding = taskType == "coding"` 改为 `produces_code`；
  - 归一化时把被移除/保留的 acceptance check 记入 `plan["acceptanceNormalization"]`，保证可审计；
  - prompt `requiredShape.acceptance` 去偏置。
- `workflow_runtime.py`
  - `_execute_python_unittest()` 在 `test_count == 0` 且计划未声明 tests 时标记
    `notApplicable=True` 而非 `passed=False`；
  - `_gate_passed_for_expectation()` 与 `_evaluate_acceptance()` 接受
    `notApplicable` 语义，并在 acceptance 结果中记录 `not_applicable` + 原因。
- `workflow_controller.py`
  - 把 `plan_declares_tests` 写入 `run.metadata["acceptanceContract"]["testsDeclared"]`，
    供 runtime 判定适用性。

## 4. 不做的事

- 不放宽 coding 任务的既有硬门禁：`taskType ∈ {coding, debugging}` 仍然要求
  `python_unittest` + strict verification schema（fail-closed 兜底）。
- 不引入「计划声明任意 shell 检查并由宿主执行」（即此前分析的方案 B）：GA 有真实
  runtime，照搬会绕开 approval gate。
- 不删除宿主测试门禁层：它是不可被模型自然语言覆盖的真实退出码证据，
  是 GA 相对 Step-Code 的独有强度。
- 不在本轮引入第二条正交轴（`touches_shared_surface` → eval contract 档位）。

## 5. 验收标准

1. 研究型计划即使被模型塞入 `python_unittest`，也不再因 `NO TESTS RAN` 失败；
   门禁记录为 `not_applicable` 且带原因。
2. 声明 `tests` role 的计划如果没产出测试，仍然失败（硬门禁不回退）。
3. `mixed` 计划只要声明代码产出，就必须满足 coding 契约（修复此前
   `mixed + implementation` 绕过 coding 约束的漏洞）。
4. `taskType == "coding"` 的计划契约行为与本轮之前完全一致。
5. 真实 `deepseek-v4.1-flash` 的两个 E2E（complex workflow、forward matrix）通过。
6. 全量 `python -m unittest discover -s tests` 通过。

## 6. 实施顺序

1. 先写 RED 回归测试（研究型降级、mixed 契约漏洞、tests 声明硬门禁）；
2. 实现 `plan_produces_code` / `plan_declares_tests` 并替换三处 taskType 分支；
3. 实现 runtime 的 `notApplicable` 语义与 controller metadata 传递；
4. 跑聚焦测试 → workflow 全量测试 → 全量回归；
5. 真实 deepseek 双 E2E 串行验证（不并行）；
6. 更新对比文档并提交。


## 7. 实施结果（已完成）

### 7.1 代码改动

- `workflow_planner.py`
  - 新增 `plan_produces_code(plan)`：计划声明了 `role ∈ {implementation, tests, repair}` 或非空
    `writeScope` 即为真；`taskType ∈ {coding, debugging}` 作为 fail-closed 兜底。
  - 新增 `plan_declares_tests(plan)`：计划含 `role == "tests"` 的 agent。
  - `_normalize_workflow_execution_contract()`、`_normalize_coding_acceptance_contract()`、
    `validate_workflow_plan()` 的 `is_coding`、`render_workflow_plan()` 的门禁判定
    四处全部改由 `plan_produces_code()` 驱动，不再读 `taskType`。
  - planner prompt 的 `acceptance` 示例去偏置：默认只展示 `verification_schema`，
    并明确「只有本次会写/跑 Python 测试时才声明 `python_unittest`」。
- `workflow_runtime.py`
  - `_run_python_unittest()` 新增 not-applicable 语义：零测试被发现且计划未声明测试时，
    记录 `notApplicable=True` + 原因，清除 `error`，不再计入失败。
  - 修复了一个隐藏缺陷：CPython 的 `unittest` 在零测试时退出码是 **5**，不是 0。
    原判定条件 `returncode == 0` 会漏判；改为 `testCount == 0` 且
    `returncode ∈ {0, 5}` 或输出含 `NO TESTS RAN`。
  - `_test_gate_failure_reason()` 跳过 not-applicable 门禁；
    `_evaluate_acceptance()` 把 `not_applicable` 记入 `metadata["notApplicableChecks"]`。
- `workflow_controller.py`
  - 把 `plan_declares_tests(draft_plan)` 写入 `run.metadata["acceptanceContract"]["testsDeclared"]`，
    供 runtime 判定适用性。

### 7.2 新增回归测试

- `test_runtime_marks_test_gate_not_applicable_when_no_tests_and_none_declared`
- `test_runtime_fails_when_plan_declared_tests_but_none_discovered`
  （硬门禁不回退：声明了测试却没产出，仍然失败）
- `test_validator_applies_coding_contract_when_mixed_plan_declares_code_work`
  （修复 `mixed + implementation` 绕过 coding 契约的漏洞）
- `test_validator_ignores_role_optional_when_plan_declares_no_code_work`
- `test_renderer_skips_host_test_gate_for_non_coding_acceptance_contract`
- `test_renderer_keeps_host_test_gate_for_mixed_plan_that_declares_code_work`

### 7.3 验证记录

```
python -m unittest discover -s tests -p 'test_workflow*.py'
Ran 231 tests ... OK (skipped=1)

python -m unittest discover -s tests
Ran 1002 tests ... OK (skipped=3)
```

真实 `deepseek-v4.1-flash` 双 E2E（串行执行，未并行）：

- complex workflow E2E：`passed: true`，真实 MCP 调用/返回、`using-superpowers` 加载、
  临时 workspace 编码写入读回全部通过，无 tool denial。
- forward matrix：`passed: true`，六场景全绿；workflow 场景
  `phases=[Context Collection, Synthesis, Verification]`、`jobWaves=[1,1,2,3]`、
  `integrationStatus=accepted`、`finalAuditStatus=passed`。

### 7.4 仍待推进

第二条正交轴 `touches_shared_surface`（公共 API/schema/迁移/UI 流 → eval contract 档位）
本轮未做，属于 ultracode `full` 契约档的对齐项。
