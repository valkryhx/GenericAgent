# GA Workflow 稳定性改造方案（对照 Step-Code）

> 状态：P0/P1 已实施，P2 已实施，P3-10/P3-12 已实施；P3-11 增强项遗留见 §10.5
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
8. 清理临时探针不得写入声明的产物路径；增加宿主侧路径冲突检查。**已完成**，见 §10.2。
9. `plannerMode=fallback_deterministic` 时 run 标 `degraded` 并记录 `fallbackReason`。

### P3 — 对齐 Step-Code 的可借鉴机制
10. 路径 ACL 前移到宿主确定性校验（读/写/执行 + 重定向解析）。**已完成**，见 §10.3。
11. 进度快照语义对齐：`onUpdate` 携带完整不可变快照。**基本满足**，见 §10.1。
12. resume 缓存只接受 `completed`（`degraded` 不算成功前缀）。**已完成**，见 §10.1。

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

## 10. 未完成项与更正（2026-10-09 复核）

本节记录对 §7–§9 实施记录的复核结果。此前口头汇报「P2 主体落地」不够准确：
P2 的第 7、9 条已实施，第 8 条**未实施**。逐项状态与证据如下。

### 10.1 已完成（本次复核确认）

- P2-7 声明产物强制存在性校验：已实施，`workflow_runtime.py`
  `_evaluate_execution_contract_evidence` 对非 optional 产物无条件校验。
- P2-9 planner 降级标记：已实施，`workflow_controller.py` 记录
  `plannerDegraded` / `plannerFallbackReason` / `planner_fallback_deterministic`。
- P3-12 resume 只接受 completed：**已在上一轮一并完成**（先前列为待办是过时记录）。
  `workflow_runtime.py::_build_resume_plan` 只把 `succeeded|cached` 计入可复用前缀，
  `degraded` 不再进入缓存。对应测试：
  `tests/test_workflow_runtime.py` 的 degraded/resume 用例。
- P3-11 进度快照：**基本满足**。bridge 的 `workflow_progress` 每次都从
  `workflow-progress.json` 重新读取完整快照后整体下发
  （`frontends/ink_bridge.py::workflow_progress`），Ink 侧整体替换
  （`frontends/ink-ui/src/state.ts` 的 `workflow_progress` 分支），不存在增量 patch。
  与 Step-Code 的差异是「是否向 child 侧也下发不可变快照」，属于增强而非缺陷。

### 10.2 未完成：P2-8 清理探针覆盖声明产物

**状态：已实施（2026-10-09 第二轮）。**

方案要求「清理临时探针不得写入声明的产物路径；增加宿主侧路径冲突检查」。
当前 `workflow_scheduler.py` / `workflow_runtime.py` / `workflow_store.py` 中
**没有任何**声明产物路径的登记或冲突检查（`grep artifact_conflict` 无结果）。

真实失败案例（run `wf_a2ef49d6cd44423c906b5ae74563113c`）正是此缺陷：清理阶段把
`_probe.md` 覆盖成只剩标题，正式报告正文丢失，而宿主未察觉、run 仍报成功。

已复现的最小形态：在同一 workspace 内对已写好文件执行
`os.remove(report.md)` + `open(report.md,'w')` 写回占位内容——guard 允许，宿主无
事后内容校验。

落地要点：run 记录声明产物路径集合；在产物存在性/读回校验之外，增加
「声明产物在 run 收口时内容非空且规模未异常塌缩」的宿主判定，并把塌缩记为
`degraded` 或 `failed` 而不是成功。

实施结果（确定性规则，不做比例猜测）：

- `workflow_runtime.py::_evaluate_execution_contract_evidence`：非 optional 声明产物
  存在但**大小为 0** 时报 `empty_artifact: <path>`。
- `workflow_runtime.py::_evaluate_declared_artifact_integrity`：把每个
  `file_write`/`file_patch` 的 tool_call 与其 success 结果配对，记录宿主实际观测到的
  最大 `writed_bytes`；run 收口时若产物小于该观测值，报
  `artifact_altered_after_write: <path> (was N bytes, now M)`，即「交付后被改写」。
  这正是 2026-10-09 的清理探针把报告正文改成占位内容的形态。
- 两者都接入 `run()` 的终态门禁，失败即 run `failed`（`integrationStatus=rejected`）。
- 测试：`tests/test_workflow_runtime.py` 新增 `test_empty_declared_artifact_is_rejected`
  与 `test_declared_artifact_overwritten_after_write_is_rejected`。

### 10.3 未完成：P3-10 路径 ACL 前移

**状态：已实施（2026-10-09 第二轮）。** GA 现在的路径约束由单一宿主侧函数统一判定。
（以下保留实施前的缺口分析作为背景。）GA 原先的路径约束分散在子代理进程内 guard、`_get_abs_path`、
以及权限 profile 的提示词里，没有 Step-Code 那种宿主侧统一前置校验。

对照 Step-Code：`features/workflow/tool-profile.ts` 的纯函数
`checkWorkflowToolCall(cwd, toolName, input, acl)` + `acl-extension.ts` 的
`pi.on("tool_call")` —— 读必须在挂载点内、写/执行必须在可写挂载点内、规范化时解析
symlink 祖先，并解析 shell 重定向/`mv`/`cp`/`tee`/`of=` 目标。

已完成的实测（结论：部分挡住，仍有确定缺口）：

- 已挡住：`_get_abs_path` → `resolve_workspace_child` 拒绝 `../` 逃逸；
  shell 类型 `code_run` 在 `workspace_root` 下被直接拒绝
  （`workflow code_run only permits Python under the workspace guard`）；
  guard 对 `os.replace` 写到工作区外抛 `PermissionError`。
- **缺口一（读旁路，已实测复现）**：`ga.py::expand_file_refs` 对
  `{{file:../secret.txt:1:1}}` 只做 `abspath(join(base_dir, path))`，**无 workspace
  包含校验**，而它是 `file_write`/`file_patch` 展开内容的通道，可读出工作区外文件：

  ```text
  $ expand_file_refs("{{file:../secret.txt:1:1}}", base_dir=<workspace>)
  'TOP_SECRET_KEY=sk-abc123\n'
  ```

- **缺口二（约束分散）**：路径规则在 guard / `_get_abs_path` / 提示词三处各写一遍，
  容易漂移；且 guard 只在 `code_run` 的 Python 子进程内以环境变量激活，
  `file_read`/`file_write`（GUI 进程）并未装载，覆盖范围不一致。

落地要点（与 Step-Code 对齐）：

1. 新增宿主侧纯函数 ACL（读/写/执行 + `{{file:}}` 引用 + 重定向解析），单一事实源；
2. 在 `GenericAgentHandler.dispatch` 中于工具执行前统一调用，替换散落检查；
3. `expand_file_refs` 增加 workspace 包含校验（最小修复，可立即堵住缺口一）；
4. 明确 ACL 与权限 profile 的职责边界：profile 判「能不能用这个工具」，ACL 判
   「这个路径能不能碰」。

实施结果：

- 新增 `workflow_path_acl.py`：纯函数 `check_tool_call(workspace_root, tool_name, args)`
  与 `check_path_access`，对标 Step-Code `tool-profile.ts`。包含 workspace 规范化
  （解析已存在祖先的 symlink，防 symlink 逃逸）、读/写/执行分类、`{{file:}}` 引用
  校验、shell 重定向 / `mv` / `cp` / `tee` / `of=` 目标解析。
- `ga.py::GenericAgentHandler.dispatch`：在**工具执行之前**统一调用
  `_check_workspace_path_access`；被拒时返回带 `path_acl` 详情的工具错误，并发出
  `tool_denied` / `path_acl_violation` 权限事件。仅对设了 `workspace_root` 的
  workflow 子代理生效，主会话不受影响；职责边界是 profile 管「工具能不能用」、
  ACL 管「路径能不能碰」。
- `ga.py::expand_file_refs` 增加 `workspace_root` 参数并做包含校验；
  `file_write`/`file_patch` 传入该参数，堵住 `{{file:../x}}` 读旁路。
- 测试：`tests/test_workflow_path_acl.py`（12 例，含 symlink 逃逸、shell 重定向、
  `{{file:}}` 引用、无 workspace 时保持放行）与 `tests/test_code_run.py` 的
  `ExpandFileRefsWorkspaceTest`。
- 真实 E2E：`tests/real_workflow_degraded_semantics_e2e.py` 新增 pathAcl 场景，
  DeepSeek-V4.1-Flash 子代理尝试 `file_read` 工作区外文件时被宿主拒绝
  （`deniedCount=1`，`leakedSecret=false`）。

### 10.5 遗留（本轮未做）

- P3-11 的「向 child 侧也下发完整不可变进度快照」仍属增强项，未做；宿主→UI 方向
  已是完整快照。
- `code_run` 的 Python 分支仍复用 `workflow_workspace_guard`（in-process guard）作为
  第二层防护；ACL 已在其之前拦掉越界参数，但两者尚未合并成单一层。

### 10.4 建议实施顺序

1. P3-10 第 3 点（`expand_file_refs` 包含校验）—— 最小、可立即验证、堵住读旁路；
2. P3-10 第 1、2、4 点（宿主侧 ACL 纯函数 + dispatch 前置校验）；
3. P2-8（声明产物登记 + 收口内容校验）。

## 11. 结构化输出契约修复（2026-10-09，真实 GA ink 复现）

### 11.1 复现

用户在 GA ink 中输入 `/workflow 使用 workflow 来调研 openai 这次解决了哪些比较知名的数学猜想`。
run `wf_300abd40d8e148098f8d7273cdff7635` 终态 `failed`，`agent_1`（source-discovery）
错误为 `schema_validation_failed: missing required field: sources; ... claims; ... risks`。
该 agent 实际做了 5 次 MCP 检索，输出丰富，只是**格式**不是宿主要求的 JSON。

### 11.2 根因（三个独立缺陷叠在一起）

1. **子代理从未被告知输出契约**。`workflow_child_agent.py::_build_prompt` 只把
   `options`（含 `schema`）当作一个普通 dict 打印，没有任何一句告诉模型「你的回答会被
   JSON Schema 机器校验」。模型合理地回了自然语言 + Markdown 表格。
   对照 Step-Code `agent-runner.ts::buildAgentPrompt`：它显式追加
   `<workflow-structured-output>Return exactly one JSON value ...`。
2. **显示压缩器销毁了正确答案**。transcript 里模型**确实**给出了完整的 fenced JSON，
   但 `agent_loop.py::_clean_content` 会把超过 6 行的代码块压成 `... (27 lines)`，
   于是即便格式正确也会被判为无效。该函数本是给人看终端输出的，不该作用于机器校验的产物。
3. **重试是盲重试**。`_schedule_retry` 把同一 prompt 重新入队，不带任何上一次的校验错误，
   等于重复同一次失败；`_schedule_repair` 生成的修复包既不带原任务也不带 schema。

另确认此前怀疑的「中文乱码」是**误报**：transcript/plan 落盘字节是正确的 UTF-8，
只是 PowerShell cp936 终端渲染造成的假象。

### 11.3 修复

| 位置 | 改动 |
| --- | --- |
| `workflow_child_agent.py` | 新增 `_structured_output_contract()`，有 schema 时追加与 Step-Code 逐字对齐的 `<workflow-structured-output>` 块（32 KiB 截断） |
| `workflow_child_agent.py` | 新增 `_retry_feedback_block()`，把上次 `issues` 以 `<workflow-retry>` 回传 |
| `workflow_child_agent.py` | `_run_job` 改走 `_build_success_payload()`：schema 任务把解析出的 JSON 提升到 payload 根，而非只塞 `summary`/`text` |
| `agent_loop.py` | `_clean_content` 不再压缩 JSON / `json` 代码块（机器产物不可截断） |
| `workflow_scheduler.py` | `_schema_retry_feedback()` 把校验 issues 写入下次 attempt 的 `retryFeedback` |
| `workflow_scheduler.py` | `_schedule_repair()` 携带原任务 prompt、schema 与校验反馈，修复包不再是盲目重生成 |
| `workflow_scheduler.py` | `downstream_result()` 暴露已通过校验的 schema 字段，让脚本拿到结构化值（Step-Code 语义） |
| `workflow_planner.py` | 确定性 fallback 的 `SOURCE_SCHEMA` 补 `properties`/`items`；prompt 与 schema 自洽；提示词新增「schema 必须完整、prompt 必须显式要求 JSON」 |

### 11.4 验收

- 单元：`tests/test_workflow_child_agent.py` +6、`tests/test_workflow_scheduler.py` +4。
- 全量 Python：`python -m unittest discover -s tests` → **1264 tests OK**（skip 3）。
- Ink：`npx tsx --test src/*.test.ts` → **391 pass / 0 fail**。
- 真实 E2E（DeepSeek-V4.1-Flash，`profiles.default`）：新增
  `tests/real_workflow_strict_schema_child_e2e.py`，严格 schema 研究子代理
  `status=succeeded`、`schemaValidation.ok=true`、脚本侧
  `{sourceCount: 3, claimCount: 2, riskCount: 1}`、`workflowIssues=[]`，耗时约 6.6s。

## 12. Ink 工作流实时进度与产物引用修复（2026-10-09，第二轮真实 ink 复现）

用户第二轮 GA ink 测试暴露两个新问题（run `wf_007bc8f88b5d4d35a25e333f415dddc5`，
`temp/sessions/session_309145fe0fed4a9096c426f8da8a3e14/`）。

### 12.1 问题一：UI 全程停在 `0/0 agents done`

**现象**：`/workflow ...` 后状态栏一直显示 `research 0/0 agents done`，直到整个 run
结束才一次性跳到终态，中途没有任何进度提示。

**根因**：`frontends/ink_bridge.py::_run_workflow_runtime` 在 runtime 返回前只发
`status=running` + `activity` 标签，唯一一次 `workflow_progress` 推送在 `runtime.run()`
返回之后。UI 因此只能拿到 run 刚创建时的空快照（0 个 agent）。

**修复**：新增 `_watch_workflow_progress()` 后台线程（0.25s 轮询），从
`workflow-progress.json`（durable 快照）读取并在内容变化时推送 `workflow_progress`；
进入 runtime 前先推一次初始快照，runtime 结束后 `stop` 该线程。

### 12.2 问题二：产物"不在预期位置"

**投稿现象**：GA 代理去读
`sessions/<sid>/workflows/<wf>/synthesis_report.md`，然后报"报告不在预期路径"。

**真实情况**：文件**写对了**——`<workspace>/synthesis_report.md`（11238 字节，即
`temp/synthesis_report.md`）。错的不是产物，是**引用语义**：

1. 计划里 `artifacts: ["sources", "synthesis"]` 是**语义标签**，不是路径；
   `_artifact_refs_from_payload` 会丢弃没有斜杠/后缀的名字，导致 handoff
   `artifactRefs` 为空数组。
2. handoff 里既没有真实路径也没有说明基准目录，LLM 只能靠猜，猜到了 run 的内部
   目录（`artifact_dir` 与 workspace 是两个不同根）。
3. `workflow-progress.json` 也从未携带真实产物路径，UI/handoff 都拿不到依据。

**修复**（观测代替猜测，对齐 Step-Code "由宿主确定性记录"的思路）：

| 位置 | 改动 |
| --- | --- |
| `workflow_scheduler.py` | 新增 `_record_observed_artifacts()`：从子代理 **实际** 的 `file_write`/`file_patch` 工具调用里记录 workspace-relative 路径，落到 `job.metadata["observedArtifacts"]`，并发 `artifact_written` 事件 |
| `workflow_scheduler.py` | `_build_handoff()` 用 `_observed_artifact_refs()` 把**存在且在工作区内**的真实文件并入 `artifactRefs`（拒绝 `..`、绝对路径、不存在的文件） |
| `workflow_store.py` | `_build_job_progress()` 把 `observedArtifacts` 写进 `workflow-progress.json` 快照 |
| `frontends/ink_bridge.py` | 新增 `_workflow_job_artifact_refs()` / `_workflow_job_observed_artifacts()`，把已存在的工作区相对路径放进 handoff 的 `intermediateResults[].artifactRefs`；`_handoff_bounded_value` 压缩时优先保留 `artifactRefs`；handoff 提示词明确"这些是 workspace 相对路径，基准是 GA workspace 根，不是 run 内部目录" |
| `frontends/ink_bridge.py` | 构造函数新增可选 `workspace_root`（默认行为不变，便于嵌入与测试） |

### 12.3 遗留（已于 §13 解决）

- workspace 仍是**共享的** `temp/`，两个并发 run 的产物会互相覆盖；run 专属目录属于
  另一条改动（需同时调整 handoff 基准目录与用户预期），本轮不动。
- `_workflow_job_artifact_refs` 只认 `file_write`/`file_patch`；`code_run` 写出的文件
  不在此列，仍需下轮从执行观测扩展。

两条均在下节以「机制替换」的方式解决，而不是继续打补丁。

### 12.4 验收

- 单元：`tests/test_ink_bridge.py` +3（进度流式、handoff 携带真实路径、压缩后仍保留），
  `tests/test_workflow_scheduler.py` +3（观测产物、丢弃不存在、拒绝越界）。
- 全量 Python：**1272 tests OK**（skip 3）。
- Ink：**391 pass / 0 fail**。
- 真实 E2E（DeepSeek-V4.1-Flash）：`tests/real_workflow_strict_schema_child_e2e.py`
  新增 artifact-handoff 场景，两个子代理各自写出文件后，
  `observedArtifacts=["research_notes.md","synthesis_report.md"]` 且两者都真实落盘。

## 13. run 专属 workspace 与工具无关的产物观测（2026-10-09，第三轮）

用户对 §12 的修复提出两点：其一，run 专属目录可以改；其二，**「只认
`file_write`/`file_patch`」是错的**——"未来可能会加入新的工具，所以不能限制死板"。
第二点是设计约束，不是措辞问题：靠枚举工具名判断"是否产生了产物"必然随工具增长而失效。

### 13.1 根因：把「工具名」当成了产物的真值

§12 的 `_record_observed_artifacts()` 遍历 transcript 里 `type == "tool_call"` 的事件，
只接受 `file_write` / `file_patch`，再读 `args.path`。这有三个必然失败：

1. **`code_run` 漏掉**——生成 DOCX/HTML 的常规路径就是 `code_run + python-docx`，
   而这些 run 的 `observedArtifacts` 恒为空。
2. **任何新工具漏掉**——`writeScope`/`deliverables` 之外的写工具，宿主没登记就永远观测不到。
3. **工具名不等于写入**——同名工具可能写别的路径，也可能什么也不写；真值只在文件系统上。

对照 Step-Code：它的 workflow 契约以**执行后的事实**（journal/evidence/artifact 检查）
为准，不维护"哪些工具算写工具"这类清单。GA 应当对齐这一层语义。

### 13.2 修复一：产物观测换成文件系统差分（工具无关）

| 位置 | 改动 |
| --- | --- |
| `workflow_workspace.py` | 新增 `snapshot_workspace()`（用 `os.scandir` 采集 `{相对路径: (mtime_ns, size)}`）与 `diff_workspace()`（返回新增/修改的相对路径）。跳过 `__pycache__`/`.git` |
| `workflow_child_agent.py::_run_tool_job` | 子代理执行前 `snapshot_workspace(workspace)`，执行后再次快照，差分写入 `tool_summary["writtenPaths"]` |
| `workflow_scheduler.py::_record_observed_artifacts` | 不再扫描 `tool_call`、不再匹配任何工具名；直接消费 `writtenPaths`，仍保留 `normalize_workspace_relative` 的越界拒绝 |
| `workflow_scheduler.py::_record_observed_mutations` | 若没有可识别的写工具事件但 `writtenPaths` 非空，记录 `workspace_write`，使"是否改动过状态"同样来自文件系统证据 |
| `workflow_scheduler.py` | 记录产物后立即 `write_workflow_progress()`，Ink 面板不必等批次结束才看到产物 |

差分是**构造上**工具无关的：任何让 workspace 文件发生变化的工具——现有、未来、以及
根本没在宿主登记过的——都会出现在差分里。`file_write`、`file_patch`、`code_run`、
`web_execute_js` 都不再需要出现在判断逻辑里。

性能是这条改动的实际约束：仓库根 17k 文件，`os.walk` 版本单次快照 1.7s（每个 child
调用两次），`os.scandir` + DirEntry 缓存降到 0.07s；这也是选择 `scandir` 的原因。

### 13.3 修复二：run 专属 workspace

`<base>/workflow-runs/<runId>/`：

- `workflow_workspace.py` 新增 `run_workspace_path()` / `create_run_workspace()`；`runId`
  含路径分隔符或 `.`/`..` 一律拒绝，避免目录逃逸。
- `workflow_controller.create_planned_run()` 在创建 run 后即分配 run 专属目录，写入
  `run.metadata["workspacePath"]`，同时记录 `workspaceBasePath`（用户可读的基准根，
  仍然是 `temp/`）。创建失败时降级为共享根并追加 `run_workspace_unavailable` issue，
  不让 workspace 问题把整个 run 变成"计划被拒"。
- `frontends/ink_bridge.py`：
  - `_run_workflow_runtime` 不再无条件用 `workspace_metadata(self.workspace_root)` 覆盖
    run 的 workspace（这正是并发 run 互相覆盖的入口）；仅在 run 无 workspace 时才分配，
    并新增 `_assign_run_workspace()` 覆盖 `workflow_draft`/`workflow_resume` 这些不经
    `create_planned_run` 的入口。
  - **两个根被显式区分**：`_run_workspace_root()`（产物，run 专属目录）用于
    `artifactRefs`；`_run_artifact_base_root()`（run 内部 `result.json`/`transcript.jsonl`，
    位于 `temp/sessions/...`）用于 `resultRef`/`transcriptRef`。混用这两个根是 §12.2
    "产物找不到"的同类错误。
  - handoff 直接带上 `workspacePath`（绝对路径）与 `workspaceBasePath`，并在提示词中说明
    "`artifactRefs`/`finalResultRef` 相对 `workspacePath`"，压缩时两者与 `artifactRefs`
    一同保留。
- `workflow_scheduler._cache_key()` 的 `workspacePathHash` 改用 `workspaceBasePath`：
  run 专属目录是隔离实现细节，用它的哈希会让 resume 永远无法命中缓存前缀。

### 13.4 验收

- `tests/test_workflow_workspace.py` +5：run 目录位置、非法 runId 拒绝、差分检出新增与修改
  文件、忽略 `__pycache__`、缺失目录返回空快照。
- `tests/test_workflow_child_agent.py` +2：`code_run` 写出的 `report.json` 出现在
  `writtenPaths`；工具名叫 `file_read`（宿主登记的非写工具）时写出的文件同样被差分捕获。
- `tests/test_workflow_scheduler.py` +3：未登记写工具落 `workspace_write`；观测产物不依赖
  工具名；越界路径被丢弃。
- `tests/test_workflow_controller.py` +2：同一 base 的两次 planned run 拿到不同 workspace；
  `run_workspace=False` 保留共享根行为。
- `tests/test_ink_bridge.py` +2：handoff 按 **run 专属目录** 解析 artifactRefs（base 根下
  查找会漏掉产物）；两个并发 run 的 workspace 不同。
- 红→绿：把 `_record_observed_artifacts` 还原成工具名扫描、把 runner 的差分还原为
  `_build_tool_summary`，新增测试均按预期失败，再恢复后通过。

- 全量：`python -m unittest discover -s tests` **1286 tests OK**（skip 3，193s）；
  Ink `npx tsx --test src/*.test.ts` **391 pass / 0 fail**。

**排查记录**：全量首轮出现过 1 个 error（`test_subagent_realtime_ipc` 创建命名管道
WinError 5）。原因是更早一次**被中止**的全量测试进程仍在后台运行并占用了固定的管道名
`\.\pipe\ga_subagent_run_owner_child`，不是代码缺陷；终止该残留进程后该文件 36 tests OK，
全量复跑通过。以后遇到同址管道创建被拒，先查是否有残留的 python 测试进程。

**真实模型 E2E**：`tests/real_workflow_strict_schema_child_e2e.py` 的 artifact-handoff 场景
已改为让 synthesis 子代理**用 `code_run` 写 `synthesis_report.md`**（而不是 `file_write`），
并新增断言 `synthesisObservedWithoutFileWrite`——即「该子代理 transcript 里没有
`file_write`，产物仍出现在 `observedArtifacts` 且真实落盘」，这正是工具无关观测的端到端证据。
运行：`GA_RUN_REAL_WORKFLOW_STRICT_SCHEMA_E2E=1 GA_RUN_REAL_WORKFLOW_ARTIFACT_HANDOFF_E2E=1
PYTHONIOENCODING=utf-8 python tests/real_workflow_strict_schema_child_e2e.py`（走 `profiles.default`，
本机当前为 `gpt-6-luna`）。

**真实 E2E 结果（gpt-6-luna，10.5s）**：

```json
{
  "passed": true,
  "runStatus": "succeeded",
  "toolNamesByJob": {
    "source-discovery": ["file_write", "no_tool"],
    "synthesis": ["code_run", "no_tool"]
  },
  "observedArtifacts": ["research_notes.md", "synthesis_report.md"],
  "filesOnDisk": ["research_notes.md", "synthesis_report.md"],
  "synthesisUsedFileWrite": false,
  "synthesisObservedWithoutFileWrite": true
}
```

synthesis 子代理的 transcript 里**完全没有 `file_write`**，产物却仍被观测到并落盘。旧实现
（按工具名扫描）下这个 run 的 `observedArtifacts` 必然是空的，这正是本轮替换机制的差异所在。

## 14. 产物归属与同名冲突（2026-10-09，第四轮）

§13 记了一条余项：`observedArtifacts` 只有路径，没有 writer。它在两种情况下会真的咬人：

1. 两个子代理写**同名文件**——第二个静默覆盖第一个，谁写的、文件里现在是谁的内容，都查不到；
2. 下游需要**按 agent 追溯**产物来源（审计、重跑单个 agent、handoff 里说明"这份数据来自哪个角色"）。

### 14.1 实现：归属在差分时记录，不在事后推断

关键约束是"不能退回到按工具名推断"。写路径与写者这两件事，**只有差分那一刻同时知道**，
所以归属就在那里落盘：

| 位置 | 改动 |
| --- | --- |
| `workflow_workspace.py` | 新增 `workspace_writes_with_writer()`（归一化为 `[{"path","writer"}]`）、`observed_artifact_paths()`、`observed_artifact_owners()` |
| `workflow_scheduler.py::_record_observed_artifacts` | 写差分结果时同时写 `writer`（`label`，缺省回落 `jobId`），并与该 job 已有记录合并 |
| `workflow_scheduler.py::_record_artifact_collisions` | 全 run 扫描各 job 的归属记录；同一路径多于一个 writer 时记 `artifactCollisions` 到 run metadata、追加 `artifact_path_collision` issue 和 `artifact_collision` 事件 |
| `workflow_store.py` | 进度快照输出 `observedArtifacts: [{path, writer}]` |
| `frontends/ink_bridge.py` | 两处消费者改用 `observed_artifact_paths()`，handoff 仍是纯路径列表（下游拿到的语义不变） |

**向后兼容是刻意的**：`workspace_writes_with_writer()` 同时接受裸字符串和 `{path, writer}`
两种形态，所以历史 run 的 state 仍能加载，旧调用点（含测试里直接塞 `["x.md"]` 的用法）不必全部改写。

**冲突不阻塞 run**：同名覆盖本身可能是合法设计（后写者有意覆盖），GA 没有依据替用户判定对错，
所以它记成 run 级事实（issue + 事件 + metadata）而不是抛错——错误地 fail 一个 run 比暴露一个
警告更贵。

### 14.2 验收

- `tests/test_workflow_workspace.py` +4：裸路径归一化（含空值过滤）、`{path,writer}` 保留 writer、
  同名多 writer 映射、新旧混合形态同时可读。
- `tests/test_workflow_scheduler.py` +4：产物带 writer、无 label 时回落 `jobId`、同名冲突被记录
  （issue + 事件 + metadata + 各 job 仍各自持有自己的归属）、不同名时**不**产生冲突记录。
- 既有两个用例改为按新契约断言（`observedArtifacts` 现在是 `[{path, writer}]`）。
- 红→绿：临时移除归属记录逻辑，两个新用例按预期失败，恢复后通过。
- 相关套件：`tests.test_workflow_workspace`/`scheduler`/`controller`/`child_agent`/`store`
  共 138 tests OK；`tests.test_ink_bridge` 106 tests OK。
- 全量：`python -m unittest discover -s tests` **1294 tests OK**（skip 3，201s）；
  Ink **391 pass / 0 fail**。
- 真实 E2E（gpt-6-luna，8.1s）：`ownershipRecorded: true`，
  `observedArtifactOwners = {"research_notes.md": ["source-discovery"], "synthesis_report.md": ["synthesis"]}`，
  同时 `toolNamesByJob.synthesis = ["code_run","no_tool"]`（无 `file_write`）——归属与工具无关观测
  在同一次真实运行里同时成立。
