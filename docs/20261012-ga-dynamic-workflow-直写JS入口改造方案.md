# GA Dynamic Workflow 入口改造方案：去掉 JSON Planner，改走 CC 直写 JS 主路径

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 上游只读参考：`D:\git_codes\claudecode283_hack`（Claude Code 2.1.283）
> 触发：用户实测 `/workflow` planner 路径两次产出坏 JSON（`planner_response_unparsable`）导致 run `failed`；用户决定**按 CC 改**：模型直写 JS，不再用 JSON 计划，也不兼容现有 GA planner。
> 流程要求：本文件由 `AGENTS.md` 的「中途补充调研的持久化流程」强制要求；先落本文件，再改代码。
> 关联：`docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`、`docs/20261012-ga-dynamic-workflow-可继续对齐CC能力清单-调研补充.md`

---

## 0. 结论先行

GA 现在有两条入口，其中**只有一条是 CC 主路径**：

| 入口 | 机制 | 是否 CC 主路径 |
| --- | --- | --- |
| `workflow` 工具（C3） | 模型**直写 JS**，`script` 字段内联 | ✅ 是（CC 唯一路径） |
| `/workflow <task>` + semantic activation | 另起一个 LLM 产出 **WorkflowPlan(JSON)** → `render_workflow_plan()` → JS | ❌ GA 特有，脆弱 |

**方案：删掉 JSON planner 路径，把 `/workflow <task>` 和 semantic activation 改成"opt-in 提醒 → 主 agent 直写 JS → 调 `workflow` 工具"**，即 CC 的 `ultracode → system-reminder → 模型调 Workflow` 模型。

- 不再有第二套 LLM 调用（planner），不再有 `WorkflowPlan` JSON 契约，不再有 `render_workflow_plan()`。
- 保留脚本侧契约（`export const meta` 纯字面量 + 确定性闸门）——那是 C3 已经在用的。
- `workflow` 工具成为**唯一**的 workflow 启动入口。

> 云端会话不复刻：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
> B1 `budget` 仍暂缓。

---

## 1. 背景：为什么 JSON planner 必须去掉

实测证据（run `wf_44b2bc5ff8c9407782a64983d57a5712`）：

```
status: failed
error:  workflow_plan_rejected
plannerMode: prompt_guided_rejected
attempt 1: Unterminated string starting at: line 121 column 21 (char 8671)
attempt 2: Expecting value: line 605 column 16 (char 18645)
```

结构性原因（不是"换个模型就好"）：

1. **CC 从不要求模型产大 JSON**。CC 让模型直接写 JS 脚本，只解析开头一小段纯字面量 `meta`（`Fy()`），其余交给 acorn/VM。GA 的 planner 路径凭空加了一个"大嵌套 JSON"契约，弱模型必然踩坑。
2. **GA 的 LLM 层没有结构化输出**（全仓无 `response_format` / json mode），纯靠 prompt 求 JSON。
3. **GA 的 JSON 解析器过于简陋**：`parse_json_object()` 只去 ``` 围栏 + 找第一个 `{`，不修尾逗号/注释/截断。
4. **没有截断检测**：流式拼接后直接 parse，不看 `finish_reason`。
5. **修复重试看不到原文**：repair prompt 只有解析错误文本 + `previousPlan={}`，模型看不到自己写坏的输出。
6. **两套 LLM 调用**：planner 另起 session，和主 agent 的脚本作者不是同一个上下文，还要重复灌一遍巨型 prompt。

结论：**JSON planner 是 GA 特有的、与 CC 设计背道而驰的脆弱层**，应整体移除。

---

## 2. 目标架构（对齐 CC）

CC 的入口模型（`workflow-tool-description.md` + `m1478` banner）：

```
用户 opt-in（ultracode 关键词 / 会话开关 / 明确说 "use a workflow" / skill / 点名已保存 workflow）
   → system-reminder 告知模型"本轮可调 Workflow"
   → 模型自己写 JS 脚本（export const meta = {...} 开头）
   → 调 Workflow 工具（script 字段）
   → 后台跑
```

GA 改造后的等价模型：

```
opt-in（/workflow <task> | 自然语言显式请求 | semantic activation 命中）
   → 给主 agent 注入 workflow opt-in 提醒（含"先 load_skill('workflow-authoring')"）
   → 主 agent 直写 JS 脚本
   → 调 workflow 工具（script 字段）
   → 后台跑（复用现有 C3 runtime / store / /workflows UI）
```

**关键点：opt-in 只负责"告诉模型可以/应该用 workflow"，脚本作者永远是主 agent。** 这既符合 CC，又天然消除了 planner 的 JSON 层。

---

## 3. 现状 → 目标（逐项对照）

| 能力 | 现状（GA） | 目标（CC 风格） |
| --- | --- | --- |
| 模型可见工具 | `workflow`（C3，已有） | 不变，成为**唯一**启动入口 |
| `/workflow <task>` | 另起 planner LLM 产 JSON → 渲染 JS | 注入 opt-in 提醒 → **主 agent 直写 JS** → 调 `workflow` 工具 |
| semantic activation（`recommended`） | 自动跑 planner | 注入同一提醒（或降级为提示），不再自动跑 planner |
| 脚本作者 | planner LLM（另起 session） | **主 agent**（与 CC 一致） |
| 中间表示 | `WorkflowPlan`(JSON) | **无**（直接 JS 源码） |
| 计划校验 | `validate_workflow_plan()` | 删 |
| 计划渲染 | `render_workflow_plan()` | 删 |
| 脚本契约 | `validate_rendered_workflow_script()`（C3 已用） | **保留**（meta 纯字面量 + 确定性闸门） |
| 执行 / 存储 / UI | `WorkflowRuntime` / `WorkflowStore` / `/workflows` | 不变 |
| 审批 | 无（自动启动） | 保留现状（H5 另议） |
| 启动前确认 | `autoApprove: true` | 不变 |

---

## 4. 详细设计

### 4.1 `/workflow <task>` 新语义（推荐：保留命令，改路由）

`/workflow <task>` 不再是"让 GA 规划"，而是"用户显式 opt-in + 把任务交给主 agent，并提示它用 workflow 工具直写 JS"。

**改动点**：`frontends/ink-ui/src/inputController.ts` 里 `workflow_plan` 命令 → 改成普通提交 + 一个 opt-in 标志；`frontends/ink_bridge.py` 新增 `submit_workflow_opt_in(task_text, source)`：
1. 构造 opt-in 提醒文本（§4.5）；
2. `self.agent.put_task(reminder + task_text)`（与普通 turn 相同，走主 agent 循环）；
3. 不再调 `workflow_plan()` / `create_planned_run()`。

**为什么保留命令**：用户已习惯 `/workflow <task>`；保留它作为"显式 opt-in 的语法糖"，脚本作者仍是主 agent —— 语义上等价于 CC 的 `ultracode` 关键词。

> 严格 CC 对齐的替代方案（Option B）：**删掉 `/workflow <task>`**，只保留 `/workflows`（列表/详情/控制）+ `workflow` 工具，opt-in 全靠自然语言与 activation。是否采用由用户决定；本方案默认 Option A。

### 4.2 semantic activation 新语义

`workflow_activation.resolve_workflow_activation()` 仍负责判断"这个任务是不是多 agent 编排"，但返回 `recommended` 时：
- **不再** `_submit_recommended_workflow()` 自动跑 planner；
- 改为**注入同一 opt-in 提醒**交给主 agent（模型自行决定是否真调 `workflow` 工具），或仅在 UI 提示"可用 workflow"。

`action == "requested"`（显式 fanout / 点名 workflow）同样走"注入提醒 + 主 agent"。

> 注意：这样 activation 不再"抢占"用户消息，`workflow` 工具的 opt-in 判断回到模型侧（与 CC 一致）。

### 4.3 `workflow` 工具（不变，成为唯一入口）

保持 C3 现状：
- schema：`script` / `scriptPath` / `name` / `args` / `resumeFromRunId` / `timeoutSeconds`；
- `ga.py::do_workflow` → `ink_bridge.workflow_launch_from_tool`（绕过主 agent busy）；
- `validate_rendered_workflow_script()` 契约 + 确定性闸门；
- 后台启动 + `<workflow_handoff>`。

唯一变化：`/workflow <task>` 不再绕过它，而是**引导模型去调它**。

### 4.4 `workflow-authoring` skill（唯一脚本契约说明）

`skills/workflow-authoring/SKILL.md` 已经是 GA 本地化的 authoring reference。改造后：
- 它是模型写脚本的**唯一权威说明**（提醒里让模型先 `load_skill('workflow-authoring')`）；
- 删除 skill 里任何"先产出 JSON 计划"的暗示（当前已无，需复核）；
- 补一句："opt-in 后直接写脚本调 `workflow` 工具，不要产出 JSON 计划"。

### 4.5 opt-in 提醒文本（本地化 CC，供实现直接抄）

建议注入如下块（放在任务文本前，或作为 system 提示）：

```
[WORKFLOW OPT-IN]
用户已明确选择 dynamic workflow 编排（来源：{/workflow 命令 | 显式请求 | 会话开关}）。
请：
1. 先调用 load_skill('workflow-authoring') 读取脚本契约；
2. 直接写一个以纯字面量 `export const meta = { name, description, phases? }` 开头的
   JavaScript 编排脚本；
3. 用 `workflow` 工具（`script` 字段，内联传入）启动它；
4. 不要产出 JSON 计划，不要先把脚本写进文件；
5. 只有在本提醒存在时才这样做（opt-in 是用户给的，不是你自己推断的）。
[/WORKFLOW OPT-IN]
```

对齐 CC `workflow-tool-description.md` 的 5 条放行条件与"meta 必须是纯字面量、内联传 script、不要先写文件"的要求。

---

## 5. 删除清单（精确到符号）

### 5.1 `workflow_planner.py`（整文件删除，除下列"保留项"外）

删除（planner/plan 专用）：

- 类：`WorkflowPlanner`、`LLMWorkflowPlanner`、`NativeWorkflowPlannerClient`、`PlannerResponseError`
- 函数：`build_workflow_planner_from_env`、`resolve_session`、`parse_json_object`、
  `validate_workflow_plan`、`render_workflow_plan`、`_normalize_plan_contract`、
  `_normalize_workflow_execution_contract`、`_normalize_execution_contract_assignments`、
  `_normalize_coding_acceptance_contract`、`_normalize_plan_retry_policy`、
  `normalize_plan_workspace_paths`、`_split_same_phase_dependencies`、
  `plan_produces_code`、`plan_declares_writes`、`plan_declares_tests`、
  `plan_shared_surfaces`、`plan_risk_level`、`plan_eval_level`、`_plan_declares_code_work`、
  `_js_identifier`、`_js_string`、`_template_string`、`_render_options`、`_planner_prompt`

### 5.2 控制器 / 桥

- `workflow_controller.py::create_planned_run`（整段删除）
- `frontends/ink_bridge.py`：`workflow_plan`、`workflow_plan_async`、`_run_planned_workflow`、
  `_run_recommended_workflow`、`_submit_recommended_workflow`、`_make_workflow_planner`、
  `workflow_planner_factory` 构造参数与实例字段

### 5.3 UI 命令

- `inputController.ts`：`workflow_plan` 命令类型及其解析（改走 §4.1 的新提交）
- `localCommandTranscript.ts`：`workflow_plan` → 文本的映射
- `protocol.ts`：`workflow_plan` 命令类型（若删除）

### 5.4 事件 / 状态

- `workflow_planned` / `workflow_plan_rejected` 事件（planner 专用）——改为启动时直接发 `workflow_started`
- 状态文案 "Planning workflow"（`activity` label）

---

## 6. 保留清单（务必不要删）

| 符号 / 文件 | 为什么留 |
| --- | --- |
| `validate_rendered_workflow_script` + `_workflow_script_contract_preflight` | **C3 工具路径与嵌套 workflow 都在用**（meta 契约 + 确定性闸门）。建议**迁到独立模块**（如 `workflow_script_validation.py`）后再删 `workflow_planner.py` |
| `workflow_script_contract.js` / `workflow_determinism.py` | 脚本契约实现 |
| `WorkflowRun` / `WorkflowJob` / `WorkflowEvent`（`workflow_models.py`） | 运行域模型 |
| `workflow_controller.create_draft` / `workflow_draft` / `workflow_approve` | raw-script 启动路径 |
| `workflow_launch_from_tool`（`ink_bridge.py`） | C3 唯一入口 |
| `workflow_runtime.py` / `workflow_scheduler.py` / `workflow_store.py` | 执行/调度/存储 |
| `/workflows`、`/workflow detail|resume|stop|save|run`、面板键位 | C1/C2 UI |
| `workflow_activation.py` | opt-in 判定（只改 action 的消费方式） |
| `skills/workflow-authoring/SKILL.md` | 唯一脚本契约说明 |
| `workflow_path_acl.py` / `workflow_permissions.py` / `workflow_tool_profiles.py` | 权限与路径收口 |

---

## 7. 影响面

### 7.1 代码

- 新增：`workflow_script_validation.py`（迁入 `validate_rendered_workflow_script`）；
  `ink_bridge.submit_workflow_opt_in()`；opt-in 提醒构造器（可放 `workflow_activation.py`）。
- 删除：`workflow_planner.py` 的 planner/plan/render 部分（见 §5）。
- 修改：`ink_bridge.py`、`inputController.ts`、`localCommandTranscript.ts`、`protocol.ts`、`workflow_controller.py`。

### 7.2 测试（会大改）

**删除/重写**（planner 专用）：
`tests/test_workflow_planner_compiler.py`、`tests/test_workflow_execution_contract.py`、
`tests/test_workflow_eval_contract.py`、`tests/test_workflow_plan_validator.py`、
`tests/test_prompt_guided_planner_real_e2e_contract.py`、`tests/prompt_guided_planner_real_*.py`、
`tests/real_workflow_forward_matrix_e2e.py`（planner 部分）、`tests/test_workflow_controller.py`（planned 部分）、
`tests/test_ink_bridge.py` 里所有 `workflow_planner_factory` / `workflow_plan` / `workflow_planned` 用例。

**保留/新增**：
- `tests/test_workflow_meta_contract.py`（脚本契约，保留，改 import 到新模块）；
- `tests/test_workflow_runtime.py::NestedWorkflowRuntimeTest`（保留）；
- `tests/test_ink_bridge.py::WorkflowToolEntryTest`（保留，这是新主路径的回归）；
- **新增**：`submit_workflow_opt_in` 注入提醒 + 不调 planner 的测试；`/workflow <task>` 不再产生 `workflow_planned` 事件的测试。

### 7.3 文档

- 更新 `AGENTS.md`（新增本次改造条目）。
- 更新 `skills/workflow-authoring/SKILL.md`（补"直接写脚本，不要 JSON 计划"）。
- 更新 `docs/20261012-ga-dynamic-workflow-复刻实施方案.md`（planner 路径标为"已移除，改走 CC 直写 JS"）。

---

## 8. 实施步骤（分批，每批留实施记录）

**批次 0（前置）**：把 `validate_rendered_workflow_script` + `_workflow_script_contract_preflight`
迁到 `workflow_script_validation.py`，更新 C3 / runtime 的 import，跑通脚本契约测试。

**批次 1（入口改造）**：新增 `submit_workflow_opt_in()` + opt-in 提醒；`/workflow <task>` 改走它；
semantic activation 不再自动跑 planner；跑通"提醒注入 + 主 agent 拿到工具"的测试。

**批次 2（删除 planner）**：删 `create_planned_run` / `workflow_plan*` / `_make_workflow_planner` /
planner 事件；删 `workflow_planner.py` 的 planner 部分；修 UI 命令与 protocol；清理测试。

**批次 3（收尾）**：更新 skill / 实施方案 / AGENTS.md；跑全量 `unittest discover` + ink-ui typecheck/test。

> 顺序不可颠倒：先建新入口并验证，再删旧路径，避免中间态把 workflow 入口整没了。

---

## 9. 验收标准

- [ ] `/workflow <task>` 不再触发任何 planner LLM 调用，run 目录里不再出现 `workflow_planned` / `workflow_plan_rejected` / `plan.md`（由 JSON planner 产出）。
- [ ] `/workflow <task>` 后主 agent 收到 opt-in 提醒，并能调 `workflow` 工具直写 JS 启动（run metadata `launchedBy == "workflow_tool"`）。
- [ ] semantic activation 命中后不再"抢占"用户消息去跑 planner。
- [ ] `workflow` 工具路径（C3）行为不变：`validate_rendered_workflow_script` 契约 + 确定性闸门 + `async_launched` + `/workflows` 可见 + `<workflow_handoff>`。
- [ ] `workflow_planner.py` 的 planner/plan/render 代码已删；`validate_rendered_workflow_script` 已迁出并保留。
- [ ] 全量 `python -m unittest discover -s tests` 全绿（planner 测试已清理）。
- [ ] `npm --prefix frontends/ink-ui run typecheck` + `run test` 全绿。
- [ ] 用 `deepseek-flash`（弱模型）实测：`/workflow <task>` 不再因 JSON 解析失败而 `failed`。

---

## 10. 风险与回滚

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 主 agent 不调 `workflow` 工具 | 弱模型可能忽略提醒，直接自己干活 | 提醒要强（§4.5）；必要时把"必须调 workflow 工具"写进 skill；失败可回退为普通任务 |
| 弱模型写不出合法 JS | 脚本 `meta` 契约可能失败 | 契约校验会给出精确错误；模型可迭代（`scriptPath`） |
| 删除 planner 影响其它调用点 | `temp/*.py` / 部分 real e2e 引用 planner | 同步清理；保留 `validate_rendered_workflow_script` |
| `/workflow <task>` 行为变化 | 老用户预期"自动规划" | 文档 + 状态行提示；保留命令但语义改为 opt-in |
| `WorkflowDraft` / `workflowDraftRef` 依赖 | handoff 的 `originalTask` 来自 draft | 迁移时把原始任务存进 run metadata（tool 路径也要存） |

**回滚**：改造在独立分支/批次内做，planner 删除前先打 tag；出问题可 `git revert` 到 C3/G8 提交 `2837315`。

---

## 11. 相关文档与源码坐标

- 上游：`claudecode283/extracted/workflow-tool-description.md`（opt-in 5 条 + meta 契约 + pipeline 范例）
- 上游：`claudecode283/extracted/workflow-authoring-reference.md`（DSL 全文）
- 上游：`claudecode283/extracted/dynamic-workflow-internals.md` §2（触发链）/§3（工具入口）/§4（编译）
- GA：`docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`（C3 工具入口现状）
- GA：`docs/20261012-ga-dynamic-workflow-可继续对齐CC能力清单-调研补充.md`（H1 ultracode / H5 审批 / H29 若补 planner 可观测性）
- GA：`skills/workflow-authoring/SKILL.md`（脚本契约）
