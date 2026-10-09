# GA Workflow 稳定性改造方案（对照 Step-Code）

> 状态：方案已定，P0 已实施，P1/P2 待执行
> 日期：2026-10-09
> 关联实现参考：`D:\git_codes\Step-Code`（`packages/coding-agent/src/features/workflow/`）
> 关联既有文档：`docs/20261002-ga-workflow-autonomous-activation-hardening-reference.md`、`docs/20261002-工作流执行契约语义化验收改造实施方案.md`

## 1. 背景

GA 的 `/workflow` 在执行研究型、写作文档、跨阶段综合类任务时反复出现「多重降级但仍然
显示成功」的现象。典型真实案例：

```text
/workflow 使用 workflow 来调研 openai 这次解决了哪些比较知名的数学猜想
```

该 run 的最终状态是 `succeeded` / `integrationStatus=accepted` /
`finalAuditStatus=passed`，但实际过程包含了 5 层降级：

1. `agent_1` 的 `artifactRefs` 为空；
2. `agent_2` 尝试读取 `temp/agents/agent_1/result.json`，路径不存在；
3. `code_run` 因 `ModuleNotFoundError: workflow_workspace_guard` 全部失败；
4. child 退回到工作区根目录写文件，并用 `_probe.md` 作为临时产物；
5. 清理探针时 `_probe.md` 被覆盖成只剩标题，正式报告正文丢失，最终由主 LLM
   handoff 回合重建。

这说明问题不是「某个功能坏了」，而是 GA 缺少一套**显式的降级语义与终态判定**。

## 2. 根因分析

### 2.1 直接根因：workflow 子进程 workspace guard 导入路径错误

`assets/code_run_header.py` 用相对自身位置的方式寻找 guard：

```python
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'memory'))
if os.environ.get('GA_WORKFLOW_WORKSPACE_ROOT'):
    from workflow_workspace_guard import install as _install_workspace_guard
```

`code_run` 把 AI 生成的脚本写到**调用方 cwd**（workflow child 场景下是
`frontends/ink-ui/` 之类），header 被内联到该临时文件后，`__file__` 指向临时
脚本所在的 `ink-ui` 目录，于是 `../memory` 指向不存在的位置，`sys.path` 里也没有
仓库根目录，导入必然失败。

已实测复现：

```text
exit 1
stderr: ModuleNotFoundError: No module named 'workflow_workspace_guard'
```

同时真实 run 的 `transcript.jsonl` 也留下了同一错误：

```text
File "D:\git_codes\GenericAgent\frontends\ink-ui\tmpgicwwfeq.ai.py", line 28, in <module>
    from workflow_workspace_guard import install as _install_workspace_guard
ModuleNotFoundError: No module named 'workflow_workspace_guard'
```

guard 本身存在（仓库根目录 `workflow_workspace_guard.py`），只是没有被加入
`sys.path`。这是**确定性 bug**，与并发、网络、模型无关。

### 2.2 结构性根因：降级被当作成功

`workflow_scheduler.py` 的 schema 处理：

```python
fallback = str(options.get("fallback") or "").strip().lower()
fallback_applied = fallback == "text"
...
if fallback_applied:
    payload["schemaFallback"] = True
    result.payload = payload
    return result          # job 仍为 succeeded
```

`workflow_planner.py` 又会为「有 `schemaRef` 但没写 `strictSchema`」的 agent 自动
注入 `fallback: "text"`：

```python
if agent.get("schemaRef"):
    if agent.get("strictSchema"):
        options["strictSchema"] = True
    else:
        options["fallback"] = str(agent.get("fallback") or "text")
```

结果是：**schema 不匹配 → 文本降级 → job succeeded → workflow succeeded**。
schema 这类关键契约失败被静默转换成成功。

### 2.3 结构性根因：`failurePolicy=continue` 与最终接受语义耦合

`scheduler.tick(failure_policy="continue")` 的合理含义是「继续调度其他独立 job」。
当前实现里，只要没有其它硬门禁失败，降级后的 job 仍计入 `succeeded/cached`，
`_update_run_completion_state()` 便判定整个 run `succeeded`。
「继续执行」和「成功接受」被混成了同一个概念。

### 2.4 结构性根因：planner 静默降级为确定性计划

`LLMWorkflowPlanner.plan()` 在首次请求或修修请求抛异常时：

```python
except Exception as exc:
    return self._fallback_draft(task_text, context, reason=str(exc))
```

`_fallback_draft` 使用确定性 `WorkflowPlanner` 生成一个完全不同形状的计划，只在
metadata 上写 `plannerMode=fallback_deterministic`，运行结果本身不体现「计划已降级」。
用户看到的是 workflow 成功，但执行的已经不是原本意图的计划。

### 2.5 artifact 契约校验强度不匹配

`_evaluate_execution_contract_evidence` 只在 `requiresExecution is True` 且计划显式
声明 `requiredChecks` 时才校验：

- `artifact_exists` / `artifact_readback` 才校验文件是否存在或被读回；
- 未声明 checks 的 artifact 完全不校验。

因此 child 可以把产物落到工作区根目录、用临时文件名替代声明产物，而 run 依旧通过。

## 3. Step-Code 的对照结论

Step-Code **有**降级处理，但边界清晰：降级只作用于「部分可选分支」与「观察性
信息」，从不作用于 schema、产物、路径、预算这类执行契约。

| 维度 | Step-Code 行为 | GA 当前行为 | 结论 |
| --- | --- | --- | --- |
| schema 失败 | 重试最多 3 次，仍失败抛 `WorkflowSchemaError`，journal 记 `failed`，run 变 `failed` | 默认 `fallback:"text"`，job/run 仍 `succeeded` | GA 需 fail-closed |
| JSON 解析失败 | 保留文本，但**继续**走 schema 校验 | 直接降级为文本成功 | GA 需继续校验 |
| 预算超限 | `budget_exceeded`，脚本吞异常也 fail-closed | 有 guest 门禁但语义未统一到终态 | 对齐 fail-closed |
| 分支失败 | `parallel()/pipeline()` 单分支 `null`，不伪造成功结果 | 降级后仍计入 succeeded | 对齐「部分失败可见」 |
| resume 缓存 | 只有 `completed|cached` 进入缓存前缀 | 降级结果也可能进缓存 | 对齐「降级不进成功缓存」 |
| 路径 ACL | 宿主 `checkWorkflowToolCall` 确定性校验读/写/执行路径 | 依赖 guard 与 prompt | 对齐宿主确定性校验 |
| 工具 profile | `planner/developer/qa` 只读或读写明确 | 主要靠能力集合与 prompt | 对齐显式 profile |
| 进度快照 | 每次 `onUpdate` 带完整不可变 `WorkflowProgress` | 有进度模型但语义未统一 | 对齐快照语义 |
| 并发/agent 上限 | `clampWorkflowConcurrency` 1–32，`clampWorkflowAgentLimit` 1–1000 | `max_concurrent<=16`，`max_total` 默认 1000 | 基本一致，无需强改 |
| opt-in | 注册与使用分离，per-turn/session 双信号 | 已有类似分层 | 保持 |

关键判断：**GA 的问题不是「有降级」，而是「降级没有终态语义」。**

## 4. 目标契约

引入三态终态，替代当前「非失败即成功」的二元判断：

| 状态 | 定义 | 触发 |
| --- | --- | --- |
| `completed` | 所有声明的 schema、产物、证据、路径、预算、硬门禁全部满足 | 默认 |
| `degraded` | 仅**显式声明可选**的检查降级，核心交付仍可用 | 计划显式声明 `schemaPolicy:"optional"` / `fallbackPolicy` |
| `failed` | schema、声明产物、路径、工具能力、预算、执行契约等关键条件失败 | fail-closed |

规则：

1. 声明 schema 默认是硬约束；只有计划显式写 `schemaPolicy:"optional"` 才允许文本降级；
2. 文本降级后 job/run 不得为普通 `succeeded`，至少为 `degraded`；
3. 声明 artifact 但不存在，必须由宿主判定 `failed` 或 `degraded`；
4. `failurePolicy=continue` 只影响调度，不影响最终接受语义；
5. `degraded`/`failed` 的 job 不进入正常 resume 成功前缀；
6. planner 降级到确定性计划时，run 必须显式标记降级原因与差异；
7. 清理临时文件禁止覆盖已声明的产物路径。

## 5. 分阶段实施

### P0 — 修掉确定性根因（已在实施）
1. 修 `assets/code_run_header.py`：把仓库根目录（guard 所在目录）显式加入
   `sys.path`，不再依赖 `__file__` 相对位置；child 在任意 cwd 都能导入 guard。
2. 补回归测试：在非仓库 cwd 下、设置 `GA_WORKFLOW_WORKSPACE_ROOT` 运行 header，
   断言 guard 可用且越界写入被拒绝。

### P1 — 终态语义
3. `workflow_models.py`：新增 `degraded` 到 `RUN_STATUSES`/`JOB_STATUSES`，
   扩展 `project_workflow_execution_outcome` 与 `summarize_workflow_jobs`。
4. `workflow_scheduler.py`：`_apply_schema_contract` 的文本降级改为产出
   `degraded` 终态，并写入可机读的 `degradation` 记录。
5. `workflow_controller.py` / `workflow_runtime.py`：run 终态为 `degraded` 时
   不再写 `integrationStatus=accepted` / `finalAuditStatus=passed`。
6. `workflow_planner.py`：只有显式 `schemaPolicy:"optional"` 才注入
   `fallback:"text"`；默认 strict。

### P2 — 产物与路径确定性
7. `workflow_runtime.py`：对 `executionContract.artifacts` 一律执行
   `artifact_exists`（除非声明 `optional:true`），不依赖 `requiredChecks` 是否书写。
8. 清理临时探针不得写入声明的产物路径；增加宿主侧路径冲突检查。
9. `plannerMode=fallback_deterministic` 时 run 标 `degraded` 并记录 `fallbackReason`。

### P3 — 对齐 Step-Code 的可借鉴机制
10. 路径 ACL 前移到宿主确定性校验（读/写/执行 + 重定向解析）。
11. 进度快照语义对齐：`onUpdate` 携带完整不可变快照。
12. resume 缓存只接受 `completed`（`degraded` 不算成功前缀）。

## 6. 验收

- 定向：`python -m unittest tests.test_workflow_workspace_guard`
- 定向：schema fallback / 产物缺失 / planner 降级各自的失败与 degraded 用例
- 全量：`python -m unittest discover -s tests`
- 真实 E2E：DeepSeek-V4.1-Flash 跑「调研 + 合成 + 产出文档」workflow，
  断言产物落在 workspace 内、artifact 可读回、终态与声明一致。

## 7. P0 实施记录（2026-10-09）

- 修改：`assets/code_run_header.py` 增加仓库根目录 `sys.path` 注入（`GA_CODE_RUN_ROOT`
  环境变量优先，保留旧的 `../memory` fallback）；`ga.py::code_run` 为 python 类型脚本
  设置该环境变量。
- 新增/扩展测试：`tests/test_code_run.py` 覆盖「仓库外 cwd 仍能导入 guard」与
  「越界写入被拒绝」。
- 验证：`python -m unittest tests.test_code_run tests.test_workflow_workspace_guard` 全绿。

## 8. P1 实施记录（2026-10-09）

- `workflow_models.py`：`RUN_STATUSES`/`JOB_STATUSES` 增加 `degraded`；
  `summarize_workflow_jobs` 统计 degraded；`project_workflow_execution_outcome`
  支持 degraded / degraded+failure 两种投影。
- `workflow_scheduler.py`：`_apply_schema_contract` 文本降级时把 `result.status`
  置为 `degraded`；`tick()` 以 `degraded` 完成 job；`_complete_job` 支持 degraded
  终态；`_update_run_completion_state` 的 settled 集合含 degraded，含 degraded 的
  run 终态为 `degraded`；`_dependencies_satisfied` 允许 degraded 上游解锁下游；
  `_cancel_job` 把 degraded 视为终态。
- `workflow_runtime.py`：终态收口时 degraded / 存在 workflowIssues → 
  `integrationStatus=degraded`、`finalAuditStatus=degraded`、`run.status=degraded`；
  `_complete_pending_rpc` 接受 degraded。
- `agent_control_workflow.py` / `frontends/ink_bridge.py`：终态集合与 status map
  加入 degraded（映射到 UI 的 partial）。
- `workflow_planner.py`：renderer 默认不再为普通 schema 注入 `fallback:"text"`，
  只有显式 `schemaPolicy:"optional"` 才注入；validator 校验 `schemaPolicy` 取值
  与「无 schemaRef 却声明 policy」；planner prompt 补充 schemaPolicy 语义。
- 验证：`python -m unittest tests.test_workflow_plan_validator tests.test_workflow_scheduler
  tests.test_workflow_runtime tests.test_workflow_models tests.test_code_run
  tests.test_workflow_workspace_guard` 全绿。


## 9. P2 实施记录（2026-10-09）

- `workflow_runtime.py::_evaluate_execution_contract_evidence`：对
  `executionContract.artifacts` 中非 optional 的产物一律执行存在性校验，不再依赖
  planner 是否写出 `artifact_exists`；`optional: true` 或 `artifact_optional` 才豁免。
- `workflow_planner.py`：normalizer 为未声明 checks 的非 optional 产物确定性补
  `artifact_exists`；validator 只在「optional 且没有任何 check」时报
  `missing_artifact_acceptance_check`；planner prompt 说明宿主强校验与 `optional` 语义。
- `workflow_controller.py`：`plannerMode=fallback_deterministic` 时 run 记录
  `plannerDegraded` / `plannerFallbackReason` 并追加 `planner_fallback_deterministic`
  workflow issue，使降级计划不再冒充正常计划。
- 顺带修复 `workflow_workspace.py::normalize_workspace_relative` 用 `lstrip("./")`
  误删点号开头文件名（`.report.html` → `report.html`）的路径 bug。
- 验证：`python -m unittest tests.test_workflow_execution_contract tests.test_workflow_runtime
  tests.test_workflow_controller tests.test_workflow_planner_compiler` 全绿。
