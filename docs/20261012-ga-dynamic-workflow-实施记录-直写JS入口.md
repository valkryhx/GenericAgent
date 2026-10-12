# 实施记录：去掉 JSON Planner，改走 Claude Code 直写 JS 主路径

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 方案：`docs/20261012-ga-dynamic-workflow-直写JS入口改造方案.md`
> 上游源码（只读）：`D:\git_codes\claudecode283_hack`
> 决策：用户拍板 **Option B（严格对齐 CC）** —— 删除 `/workflow <task>` JSON planner，
> 只保留 `/workflows`（列表/详情/控制）+ `workflow` 工具（模型直写 JS）。

---

## 1. 根因（用户实测）

run `wf_44b2bc5ff8c9407782a64983d57a5712`
（`temp/sessions/session_bdb8134368084bec83a50e04159ad3c7/workflows/`）：

```
status: failed / error: workflow_plan_rejected / plannerMode: prompt_guided_rejected
attempt 1: Unterminated string starting at: line 121 column 21 (char 8671)
attempt 2: Expecting value: line 605 column 16 (char 18645)
```

planner LLM 两次产出坏 JSON。结构性原因（换模型治不了根）：

1. CC 从不要求模型产大 JSON —— 模型直接写 JS，只解析开头一小段纯字面量 `meta`（`Fy()`）；
2. GA LLM 层无结构化输出（全仓无 `response_format` / json mode）；
3. `parse_json_object()` 太简陋（只去围栏 + 找第一个 `{`，不修尾逗号/注释/截断）；
4. 无截断检测（不看 `finish_reason`）；
5. repair 重试看不到原文；
6. planner 另起一套 LLM 调用，和主 agent 脚本作者不是同一上下文。

---

## 2. 目标架构（已落地）

```
opt-in（显式请求 | /workflow on | 运行已保存 workflow）
  → 注入 [WORKFLOW OPT-IN] 提醒
  → 主 agent 直写 JS 脚本
  → 调 workflow 工具（script 字段内联）
  → 后台跑 + /workflows 观测
```

opt-in 只负责"告知模型可用 workflow"；**脚本作者永远是主 agent**。
semantic `recommended` 不再自动 hijack 主 agent。

---

## 3. 代码改动

### 3.1 新增

| 文件 | 说明 |
| --- | --- |
| `workflow_script_validation.py` | 从 `workflow_planner.py` 迁出的脚本契约检查：`workflow_script_contract_preflight()` + `validate_rendered_workflow_script()`（meta 契约 → `node --check` → 确定性闸门）。C3/嵌套 workflow 共用。 |
| `workflow_activation.workflow_opt_in_reminder()` | 生成 CC 风格 `[WORKFLOW OPT-IN]...[/WORKFLOW OPT-IN]` 提醒。 |
| `workflow_activation._WORKFLOW_MENTION` | 识别自然语言 opt-in（`用 workflow` / `use workflow` / `with a workflow` 等）。 |

### 3.2 修改

| 文件 | 改动 |
| --- | --- |
| `frontends/ink_bridge.py` | `submit()`：删除 `_workflow_planning` busy 检查与 `_submit_recommended_workflow` hijack；`requested` 时注入 opt-in 提醒（提醒只进模型，不进 transcript）；接入 bridge 级 `WorkflowActivationState`（`/workflow on\|off`）；`workflow_detail`/handoff 的 `originalTask` 改从 run metadata 取。 |
| `workflow_activation.py` | 新增 `workflow_opt_in_reminder()`；`_EXPLICIT_FANOUT` 补中文并行调研；新增 `_WORKFLOW_MENTION` 自然语言 opt-in。 |
| `workflow_controller.py` | 删除 `create_planned_run()`（保留 `create_draft()` 与生命周期方法）。 |
| `workflow_store.py` | 删除 `write_workflow_draft()` / `write_workflow_contract_artifacts()`（planner 专用）。 |
| `skills/workflow-authoring/SKILL.md` | 新增 §1.1「Opt-in flow (direct JS, no JSON planner)」。 |
| `frontends/ink-ui/src/inputController.ts` | 删除 `/workflow <task>` 与 `parseWorkflowStartCommand()`；新增 `/workflow on\|off` 转发。 |
| `frontends/ink-ui/src/protocol.ts` | 删除 `workflow_plan` 命令类型。 |
| `frontends/ink-ui/src/localCommandTranscript.ts` | 删除 `workflow_plan` 映射。 |
| `frontends/ink-ui/src/App.tsx` | help 文案：`/workflow on\|off` 取代 `/workflow [--timeout] TASK`。 |

### 3.3 删除

| 文件 / 符号 | 说明 |
| --- | --- |
| `workflow_planner.py`（整个文件） | `WorkflowPlanner` / `LLMWorkflowPlanner` / `NativeWorkflowPlannerClient` / `WorkflowDraft` / `parse_json_object` / `validate_workflow_plan` / `render_workflow_plan` / `_planner_prompt` 等 planner/plan/render 全部。 |
| `ink_bridge` | `workflow_plan_async()` / `workflow_plan()` / `_make_workflow_planner()` / `_submit_recommended_workflow()` / `_run_recommended_workflow()` / `_run_planned_workflow()` / `workflow_planner_factory` 构造参数与 planning 状态字段；`workflow_plan` 命令分派。 |
| `workflow_controller` | `create_planned_run()`。 |
| UI | `workflow_plan` 命令与 `/workflow <task>` 启动路径。 |
| 事件 | `workflow_planned` / `workflow_plan_rejected`。 |

### 3.4 保留（不要删）

`workflow_runtime.py` / `workflow_scheduler.py` / `workflow_store.py`（除两个 draft 写入方法）/
`workflow_activation.py` / `workflow_script_validation.py` / `workflow_script_contract.js` /
`workflow_determinism.py` / `workflow_launch_from_tool` / `create_draft` / `workflow_draft` /
`/workflows` UI / `skills/workflow-authoring/` / 权限与路径收口。

---

## 4. 测试改动

删除（planner 专用）：

- `tests/test_workflow_planner_compiler.py`
- `tests/test_workflow_execution_contract.py`
- `tests/test_workflow_eval_contract.py`
- `tests/test_workflow_plan_validator.py`
- `tests/test_workflow_planner_execution_intent.py`
- `tests/test_workflow_prompt_guidance.py`
- `tests/test_workflow_prompt_guided_planner.py`
- `tests/test_workflow_prompt_single_source.py`
- `tests/test_prompt_guided_planner_real_e2e_contract.py`
- `tests/prompt_guided_planner_real_child_e2e.py`
- `tests/prompt_guided_planner_real_e2e.py`
- `tests/workflow_planner_real_e2e.py`
- `tests/real_complex_workflow_mcp_skill_coding_e2e.py`
- `tests/real_workflow_forward_matrix_e2e.py`

重写 / 清理：

- `tests/test_ink_bridge.py`：删 planner 用例；新增
  `test_submit_leaves_semantic_multi_step_request_to_the_main_agent`、
  `test_submit_injects_opt_in_reminder_for_explicit_workflow_request`；handoff 用例改用
  run metadata `originalTask`。
- `tests/test_workflow_controller.py`：只保留 `create_draft` + 生命周期用例（6 条）。
- `tests/test_workflow_store.py`：删 `write_workflow_draft` 用例。
- `tests/real_*.py` / UI e2e 脚本：改用直写 JS（`workflow_draft`）或 `/workflow on` + submit。

新增回归断言：

- semantic `recommended` 任务不再被 hijack，原样进主 agent；
- 显式 opt-in 注入 `[WORKFLOW OPT-IN]`，且提醒不泄漏到用户 transcript；
- `/workflow on|off` 是本地开关。

---

## 5. 验证结果

| 命令 | 结果 |
| --- | --- |
| `python -m py_compile frontends/ink_bridge.py workflow_controller.py workflow_store.py workflow_activation.py workflow_script_validation.py workflow_runtime.py` | OK |
| `python -m unittest tests.test_ink_bridge` | 120 tests OK |
| `python -m unittest tests.test_workflow_controller` | 6 tests OK |
| `python -m unittest tests.test_workflow_store` | 27 tests OK |
| `python -m unittest discover -s tests` | **1324 tests OK (skipped=3)** |
| `npm --prefix frontends/ink-ui run typecheck` | OK |
| `npm --prefix frontends/ink-ui test` | **405 tests pass / 0 fail** |

---

## 6. 用户自测入口

- `/workflow on`：开启本 session 的 workflow opt-in；之后任何多步/多 agent 请求都会注入提醒。
- 直接自然语言："用 workflow 帮我并行调研 A、B、C，然后综合验证" —— 主 agent 收到
  `[WORKFLOW OPT-IN]` 提醒，调用 `load_skill('workflow-authoring')`，内联写 JS，
  再调 `workflow` 工具后台启动。
- `/workflows`：列表；`/workflow detail|resume|stop|save|run RUN_ID`：查看/控制/保存/重跑。

---

## 7. 云端说明

**【复刻时暂时不需要参考 claudecode 云端会话的实现】**

本改造完全在本地 run 模型内完成：`agent({isolation:'remote'})`、`remote_launched`、
`Ho()` 远程路径、云端 session 均**不在本次范围内**，也未被引入。

---

## 8. 遗留 / 后续

- B1 `budget` token 预算对象：用户点名暂缓，仍未做。
- H1~H28 剩余可对齐能力（可观测性 H17/H18/H19、启动前批准 H5、per-agent H7、
  引擎一致性 H12/H13/H15、UI H22/H23/H24/H26、校验 H27/H28）：见
  `docs/20261012-ga-dynamic-workflow-可继续对齐CC能力清单-调研补充.md`，按批次推进。
