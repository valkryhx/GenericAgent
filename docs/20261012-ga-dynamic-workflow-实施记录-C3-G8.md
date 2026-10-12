# GenericAgent Dynamic Workflow 实施记录：C3 工具入口 + G8 一层嵌套

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 阶段：C3 / G8（对应实施方案缺口 G9 / G8）
> 上游参考：Claude Code 2.1.283 真实源码（只读），`D:\git_codes\claudecode283_hack`
> 调研补充：`docs/20261012-ga-dynamic-workflow-C3-G8-调研补充.md`
> 状态：✅ 已完成（B1 `budget` 按用户决定暂缓）

---

## 0. 本阶段目标

把 CC 的 **模型入口** 与 **脚本内嵌套** 两条能力补齐到 GA：

- **C3**：新增模型可见的 `workflow` 工具，让模型在明确 opt-in 后**直接写 JS 脚本**；
  工具描述 + `workflow-authoring` skill 组成 CC 的“短提示 + `load_skill` 取 17K reference”分支。
  `/workflow <task>` 保留为兼容的 planner 路径。
- **G8**：脚本内可 `await workflow(nameOrRef, args)` 跑另一个 workflow，**只允许一层**；
  二层拒绝；父 run 停止时子 run 一起停止。

> **边界（务必遵守）**：
> - **不复刻 CC 云端会话**：`isolation:"remote"` / `Ho()` / `Uo` / `WP()` 是被关掉的死代码路径。
>   **【复刻时暂时不需要参考 claudecode 云端会话的实现】**
> - **B1 `budget` 暂缓**：用户点名先跳过，本轮不实现，authoring reference 里也不给 budget 示例。
> - CC 工具层的 `runId` run-op（stop/resume/delete existing run）在本 build provider 不存在（`he()` 恒 `undefined`），
>   GA 继续用 `/workflows` UI 与 bridge 命令控制已有 run，不复刻该字段。

---

## 1. CC 源码证据

| CC 符号 | 模块 | 位置 | 职责 |
| --- | --- | --- | --- |
| Workflow 工具 schema | `m1569_0xdceb889.js` | `@61628 inputSchema` | `script` / `scriptPath` / `name` / `args` / `resumeFromRunId` / `runId` / `remote` |
| `validateInput` | `m1569_0xdceb889.js` | `@62439` | feature gate → named-only → scriptPath 可读性 → resume 校验 → `Fy()` meta 契约 → `LKt()` 确定性闸门 |
| `checkPermissions` | `m1569_0xdceb889.js` | `@64578` | 默认 `ask`（"Review dynamic workflow before running"） |
| `call` | `m1569_0xdceb889.js` | `@65948` | 编译/落盘后注册后台任务，立即返回 `{status:"async_launched", taskId, runId, workflowName, summary, transcriptDir, scriptPath}` |
| 工具描述双分支 | `m1502_0xdb4a986.js` | `xnr(e)` @60/@199 | 有 skill 系统 → 短提示让模型 `load_skill` authoring reference；无 skill 系统 → 内联约 17K reference |
| 子 agent 禁递归 | `m1569_0xdceb889.js` | `Fd` | 子 agent 的 `disallowedTools` 含 Workflow 工具 |
| `workflow()` 引擎 | `m0869_0xd12567a.js` | `yo@30215` / `tsr@29905` | 脚本内启动子 workflow；一层允许，二层拒绝 |

---

## 2. C3：CC 风格 `workflow` 工具入口

### 2.1 GA 改动点

| 文件 | 改动 |
| --- | --- |
| `assets/tools_schema.json` / `assets/tools_schema_cn.json` | 新增 `workflow` 工具；6 个字段：`script` / `scriptPath` / `name` / `args` / `resumeFromRunId` / `timeoutSeconds`（`runId` / `remote` 不复刻）；描述为 CC 原文的本地化版 + “先 `load_skill('workflow-authoring')`” |
| `ga.py::do_workflow` | 从 `self.parent.workflow_tool_launcher` 取 launcher；无 launcher → 结构化 error；有 launcher → 转发 CC 字段并返回 launcher 的 `async_launched` |
| `frontends/ink_bridge.py::workflow_launch_from_tool` | 主入口：**绕过主 agent busy 检查**（工具调用发生在主 agent `is_running == True` 时），校验脚本后创建 run、标记 running、追加 `workflow_started`、启动既有 `_run_workflow_runtime` watcher，立即返回 `async_launched` |
| `frontends/ink_bridge.py::_read_workflow_tool_script_path` | 在 workspace 根与 store 根下解析 `scriptPath`，越界/不存在则 `FileNotFoundError` |
| `frontends/ink_bridge.py`（`__init__`） | 创建 agent 后注入 `self.agent.workflow_tool_launcher = self.workflow_launch_from_tool` |
| `skills/workflow-authoring/SKILL.md` | 新增：GA 本地化的 authoring reference（只暴露 GA 真实支持的 API） |
| `skills_runtime.py` | `get_default_skill_roots()` 第一项加入仓库内 `skills/`，使 `workflow-authoring` 可被发现 |
| `subagent_capabilities.py` | `workflow` 加入 `ORCHESTRATION_TOOLS`，workflow 子 agent 不可见/不可调用，禁止递归 |

### 2.2 工具返回契约（`async_launched`）

```json
{
  "status": "async_launched",
  "taskId": "local_workflow_<runId>",
  "taskType": "local_workflow",
  "runId": "<runId>",
  "workflowName": "<meta.name 或 source name>",
  "summary": "<meta.description>",
  "transcriptDir": "<artifact_dir>/agents",
  "scriptPath": "<artifact_dir>/script.js",
  "workspacePath": "<run workspace 或 base workspace>",
  "message": "Workflow launched in the background; use /workflows to monitor progress."
}
```

完成后既有 `_queue_workflow_handoff` 把 `<workflow_handoff>` 排给主 agent。

### 2.3 与 `/workflow <task>` 的关系

- `/workflow <task>`（planner 路径）：模型不写脚本，GA 生成 `WorkflowPlan(JSON)` → `render_workflow_plan()` → JS。
- `workflow` 工具（CC 路径）：模型**直接写 JS**，走 `validate_rendered_workflow_script()` 契约 + 确定性闸门。
- 两条路径都落到同一个 `WorkflowController` / `WorkflowStore` / `/workflows` UI。

---

## 3. G8：脚本内 `workflow()` 一层嵌套

### 3.1 GA 改动点

| 文件 | 改动 |
| --- | --- |
| `workflow_js_worker.js` | 注入 `workflow(nameOrRef, workflowArgs) => rpc('workflow', { nameOrRef, args: workflowArgs })`；`nameOrRef` 必须是 string 或 plain object，否则 `TypeError` |
| `workflow_runtime.py::_handle_nested_workflow_rpc` | 新增 `workflow` RPC 分支：`workflow_depth >= 1` → 直接 `reply(False, error="nested workflows are limited to one level; workflow() is unavailable inside a nested workflow")`；否则解析脚本 → preflight → 建嵌套 run → 用嵌套 `WorkflowRuntime` 执行 → `reply(True, value=result)` |
| `workflow_runtime.py::_resolve_nested_workflow_script` | 解析 `nameOrRef`：string / `{name}` → named library；`{script}` → inline；`{scriptPath}` → workspace 内脚本 |
| `workflow_runtime.py::_resolve_nested_script_path` | 在 workspace 根与 store 根下解析 `scriptPath` |
| `workflow_runtime.py::_safe_load_run_by_id` | 安全读取父 run，供停止传播判断 |
| `workflow_runtime.py::__init__` | 新增 `workflow_depth` / `parent_run_id` / `job_id_prefix` |
| `workflow_runtime.py::_raise_if_externally_stopped` | 同时检查自身 run 与 `parent_run_id` 指向的父 run |
| `workflow_scheduler.py` | 新增 `job_id_prefix`，3 处 job id 生成改用 `_new_job_id()` |

### 3.2 嵌套 run 的 metadata

```json
{
  "parentRunId": "<外层 runId>",
  "workflowDepth": 1,
  "nestedWorkflow": "<source name>",
  "launchedBy": "nested_workflow",
  "scriptSource": "named | inline | scriptPath",
  "workspacePath": "<与外层共享>",
  "workspaceBasePath": "<base>",
  "workflowTaskType": "nested",
  "workflowName": "<meta.name>",
  "workflowSummary": "<meta.description>"
}
```

### 3.3 为什么需要 `job_id_prefix`

`NativeGPTChildAgentRunner` 按 job id 维护状态键。嵌套 runtime 与父 runtime 若共用 `agent_1`、
`agent_2`… 会互相覆盖。因此嵌套 run 用 `job_id_prefix = f"nested_{nested_run.run_id}_"`，
生成 `nested_<runId>_agent_1`，与父 run 完全隔离。子 runtime 结束时用
`_wire_runner_telemetry(scheduler.run)` 把 runner 的 telemetry sink 还原给外层 run。

### 3.4 父 run 停止传播

`_raise_if_externally_stopped()` 在每次推进前同时读取自身 run 与 `parent_run_id`；
父 run 处于 `killed` / `cancelled` 时，嵌套 runtime 也抛 `RuntimeError` 停止。
嵌套 run 终态保留 `killed`，不会被改写成 `failed`。

---

## 4. UI / 历史复用（满足用户对 `/workflows` 的要求）

工具启动的 run 与嵌套 run 都只是普通的 `WorkflowRun`，因此**自动复用全部既有 `/workflows` 能力**：

| 能力 | 入口 |
| --- | --- |
| 查看 workflow 列表 | `/workflows`（`workflow_list`） |
| 实时执行进度 | `workflow_progress` / `workflow_live` / `workflow_event` |
| 详情 | `/workflow detail`（`workflow_detail`） |
| 停止 | `/workflow stop`、面板 `x` |
| 保存为命名 workflow | `/workflow save`、面板 `s` |
| 重跑历史/命名 workflow | `/workflow run <name>`（`workflow_run_named`） |
| 恢复 | `/workflow resume <runId>`、面板 `r` |

> 已知细节：嵌套 run 会作为独立 run 出现在 `/workflows`；外层 `_run_workflow_runtime` watcher
> 只直接观察外层 run，嵌套 run 的实时帧要等下一次 `/workflows` 刷新才完整显示。
> 若后续要求“嵌套 run 也实时显示”，需要把嵌套 run 的事件转发到 bridge 事件流。

---

## 5. 测试与验证

新增/扩展测试：

- `tests/test_workflow_runtime.py::NestedWorkflowRuntimeTest`（7 条）
  - named / inline / scriptPath 嵌套返回子结果并记录 `parentRunId`
  - 二层嵌套被拒
  - 嵌套 job id 使用 run-scoped 前缀
  - 父 run 停止 → 嵌套 run `killed`
  - `nameOrRef` 非 string/object 被拒
- `tests/test_ink_bridge.py::WorkflowToolEntryTest`（4 条）
  - 工具启动绕过主 agent busy 并复用 UI 路径
  - 非法脚本不创建 run
  - `scriptPath` + 复用返回的 `scriptPath` 可迭代
  - `name` + `resumeFromRunId` 可用
- `tests/test_subagent_capabilities.py`：workflow schema / 工具 / 子 agent 隔离
- `tests/test_skills_runtime.py`：`workflow-authoring` 可被发现

命令与结果：

```bash
python -m unittest tests.test_workflow_runtime.NestedWorkflowRuntimeTest -v   # 7 OK
python -m unittest tests.test_ink_bridge.WorkflowToolEntryTest -v             # 4 OK
python -m unittest tests.test_subagent_capabilities -v                        # 9 OK
python -m unittest tests.test_skills_runtime -v                               # 7 OK
python -m unittest tests.test_workflow_runtime tests.test_ink_bridge \
  tests.test_subagent_capabilities tests.test_skills_runtime -v               # 246 OK
node --check workflow_js_worker.js && node --check workflow_script_contract.js
python -m py_compile ga.py frontends/ink_bridge.py workflow_runtime.py \
  workflow_scheduler.py workflow_planner.py skills_runtime.py
```

手工 smoke：

- named 嵌套成功，返回 `{'child': {'value': 7, 'nested': True}}`；
- 二层嵌套被拒；
- 嵌套 job id 前缀为 `nested_<runId>_agent_1`；
- 父 run killed → 嵌套 run 变 killed；
- bridge 工具启动 inline 成功并终态 `succeeded`；
- `scriptPath` 支持首启与复用返回的 `scriptPath`。

---

## 6. 不做的事（边界）

- **B1 `budget` 暂缓**：用户决定先跳过；authoring reference 里不给 budget 示例。
- **CC 云端会话不复刻**：`isolation:"remote"` / `Ho()` / `Uo` / `WP()`。
  **【复刻时暂时不需要参考 claudecode 云端会话的实现】**
- **CC 工具层 `runId` run-op 不复刻**：本 build provider 不存在。

---

## 7. 相关路径

- 调研补充：`docs/20261012-ga-dynamic-workflow-C3-G8-调研补充.md`
- 实施方案：`docs/20261012-ga-dynamic-workflow-复刻实施方案.md`
- 前置实施记录：`docs/20261012-ga-dynamic-workflow-实施记录-{A1,A2-A3,A4,B2,C1-C2}.md`
- CC 只读参考：
  - `claudecode283/modules/m1569_0xdceb889.js`（工具 schema / validateInput / call）
  - `claudecode283/modules/m1502_0xdb4a986.js`（`xnr(e)` 工具描述双分支）
  - `claudecode283/modules/m0869_0xd12567a.js`（DSL / `workflow()` 嵌套）
  - `claudecode283/extracted/workflow-tool-description.md`
  - `claudecode283/extracted/workflow-authoring-reference.md`
  - `claudecode283/extracted/dynamic-workflow-internals.md`
