# GenericAgent Dynamic Workflow：C3 工具入口 + G8 嵌套调研补充

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 上游只读参考：`D:\git_codes\claudecode283_hack`（Claude Code 2.1.283 真实源码）
> 触发：用户要求 GA 的 workflow 入口必须按 CC 设计，不能只停留在 `/workflow` 的 JSON plan 路径；除 B1 `budget` 外，CC 能对齐的能力都要对齐。
> 关联实施记录：`docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`（**已创建**）
> 流程要求：本文件由 `AGENTS.md` 的「中途补充调研的持久化流程」强制要求；先落本文件，再改代码。

---

## 0. 结论先行

本轮补齐两个 CC 能力：

1. **C3：CC 风格的 `Workflow` 工具入口**
   - CC 模型看到的不是 `/workflow` 命令，而是一个 `Workflow` 工具。
   - 工具描述说明何时该用、何时不该用，并让模型写 JS 脚本。
   - CC 有 skill 系统时，工具描述只给短提示，模型通过 `load_skill` 加载约 17K 的 authoring reference。
   - GA 有 `load_skill` + `skills_runtime`，因此走同一分支：新增 `workflow` 工具 + `workflow-authoring` skill。

2. **G8：脚本内 `workflow(nameOrRef, args)` 一层嵌套**
   - CC 允许脚本内启动子 workflow，但只允许一层，二层被拒绝。
   - GA 在 worker 注入 `workflow()` RPC，宿主解析 named workflow / inline script / scriptPath，用嵌套 `WorkflowRuntime` 执行。
   - 嵌套 run 记录 `parentRunId`，父 run 停止时子 run 也停止；嵌套 run 使用独立 job id 前缀，避免共用 runner 的状态键冲突。

不在范围：

- B1 `budget`：用户点名暂缓，本轮不实现，也不在 authoring reference 中给 budget 示例。
- CC 云端会话：`isolation:"remote"` / `Ho()` / `Uo` / `WP()`。**【复刻时暂时不需要参考 claudecode 云端会话的实现】**
- CC 的 `runId` 工具层 run-op（stop/resume/delete existing run）：CC 本 build 的 provider 不存在，该枚举值不出现；GA 继续用 `/workflows` UI 和 bridge 命令控制已有 run。

---

## 1. CC 源码证据：Workflow 工具入口

### 1.1 工具 schema

源码坐标：`claudecode283/modules/m1569_0xdceb889.js @61628 inputSchema()`。

字段：

| 字段 | 含义 |
| --- | --- |
| `script` | 内联 workflow JavaScript 脚本 |
| `scriptPath` | 已落盘脚本路径，用于迭代时避免重发全文 |
| `name` | 已保存 workflow 名 |
| `args` | 传给脚本的 JSON 值 |
| `resumeFromRunId` | 从某个 runId 恢复，命中未变化前缀缓存 |
| `runId` | 命名 workflow 的 run 操作入口（本 build provider 不存在，GA 不复刻） |
| `remote` | 云端会话开关（本 build 路径不可达；GA 不复刻） |

### 1.2 `validateInput` 顺序

源码坐标：`m1569_0xdceb889.js @62439`。

CC 按顺序做：

1. managed settings / session feature gate：`disableWorkflows`、session `enableWorkflows`。
2. 若传 `runId`，走命名 workflow 的 run-op 校验。
3. named-only 会话限制：只允许 `{name, args}`，不允许 `script` / `scriptPath` / `resumeFromRunId` / `remote`。
4. 若传 `scriptPath`，做可读性/路径校验。
5. 若传 `resumeFromRunId`，校验 resume 合法性与碰撞。
6. `Sr()` 解析来源，得到脚本内容。
7. `Fy(script)` 做 meta 契约解析；失败返回 `Invalid workflow script: ...`。
8. 若 `script` 是内联传入且 `LKt(scriptBody)` 命中确定性违规，拒绝：
   `Workflow scripts must be deterministic: Date.now()/Math.random()/new Date() are unavailable (breaks resume).`

### 1.3 `checkPermissions`

源码坐标：`m1569_0xdceb889.js @64578`。

- 默认返回 `behavior: "ask"`，message 为 `Review dynamic workflow before running`。
- 允许按 workflow 名匹配 allow/deny 规则；命中 allow 可直接放行。
- 未命中时给 suggestions，可把该 workflow 写入 localSettings 白名单。
- GA 对应：主 agent 权限档 `read_only` / `ask` / `full_access` 已覆盖“只读拒绝、ask 审批、full_access 放行”；`workflow` 是未知静态工具，`permission_policy.classify_tool()` 按 mutating 处理，正是 CC 的默认 ask 语义。

### 1.4 `call` 的异步启动契约

源码坐标：`m1569_0xdceb889.js @65948`。

关键流程：

1. 解析脚本来源 `Sr()`。
2. `Fy()` 再做一次契约解析。
3. `t7(scriptBody)` 编译成 VM Script；编译失败时仍返回 `async_launched`，但 data 里带 `error`。
4. 生成/复用 `runId`。
5. 生成 `taskId`，注册后台任务。
6. 落盘脚本，持久化 run 状态。
7. 立即返回：
   `{status:"async_launched", taskId, taskType:"local_workflow", workflowName, runId, summary, transcriptDir, scriptPath}`。
8. 真正执行在后台 async IIFE；完成后通过 task notification 推回主 agent。
9. 返回的 `scriptPath` 让模型可以 `Write/Edit` 后再次 `Workflow({scriptPath, ...})`，不必重发完整脚本。

---

## 2. CC `xnr(e)`：工具描述与 authoring reference 的双分支

源码坐标：`claudecode283/modules/m1502_0xdb4a986.js @60 Anr`、`@199 xnr`。

CC 的工具描述构造：

```js
function xnr(e) {
  return e ? `${t}\n\n${i}` : `${t}\n\n${Anr()}`
}
```

- `t`：Workflow 工具描述正文，约 3034 字符；说明何时该调用、何时不该调用、meta 契约、pipeline 示例。
- `i`：短提示：让模型先 `load_skill` 加载 workflow authoring reference。
- `Anr()`：完整 authoring reference，约 17190 字符。
- `e`：当前会话是否有 skill 系统。
  - `e=true`：只给短提示 + skill 索引；模型按需 `load_skill`，节省每轮 context。
  - `e=false`：把完整 reference 全文塞进工具描述。

调用点：`m1569_0xdceb889.js @2836/@2839` 附近，`xnr(fSe(e?.tools)) + pEr(...) + dt()`。

GA 对应：

- GA 已有 `skills_runtime.py`、`load_skill` 工具、`agentmain.build_system_prompt_sections()` 的 `skills` 段，以及 workflow child 的 `build_skill_prompt()`。
- 因此 GA 走 `e=true` 分支：`workflow` tool description 只放本地化工具正文 + “先 `load_skill('workflow-authoring')`”短提示；完整 17K 级内容放进 `skills/workflow-authoring/SKILL.md`。
- 这样既对齐 CC 的上下文预算设计，也让 planner 路径和 raw-script 路径共享同一份 DSL 参考。

## 3. CC 禁止 workflow 子 agent 递归起 workflow

源码坐标：`dynamic-workflow-internals.md §6`；`m1569` 里内置 agent 定义 `tools:["*"], disallowedTools:[O_,ht,Fd]`，其中 `Fd` 就是 Workflow 工具本身。

结论：

- Workflow 子 agent 不能递归调用 Workflow 工具。
- GA 对应：把 `workflow` 加入 `subagent_capabilities.ORCHESTRATION_TOOLS`。
  - 根 agent：`is_subagent=False` 时允许 orchestration 工具，因此可以看到/调用 `workflow`。
  - workflow 子 agent：`is_subagent=True` 且未开 delegation 时，orchestration 工具从 allowed 集合移除，`workflow` 不会出现在 child tool schema 中，dispatch 也会被 capability gate 拒绝。

---

## 4. GA 现状与 C3 实施设计

### 4.1 GA 现状

- GA 当前没有模型可见的 `workflow` 工具；`assets/tools_schema.json` 与 `_cn.json` 中没有该工具。
- 用户入口是 `/workflow <task>` → `frontends/ink_bridge.py::workflow_plan_async` → `workflow_plan` → `LLMWorkflowPlanner`。
- planner 路径让模型产出 **WorkflowPlan JSON**，再由 `render_workflow_plan()` 模板渲染成 JS；模型不直接写 JS。
- GA 已有 raw-script 运行时：`WorkflowController.create_draft()`、`WorkflowRuntime`、`workflow_js_worker.js`、`workflow_script_contract.js`、`workflow_determinism.py`。
- GA 已有后台运行与 UI 观察链路：`_run_workflow_runtime()` 启动 `_watch_workflow_progress()`，`_queue_workflow_handoff()` 在终态把 `<workflow_handoff>` 排给主 agent。
- GA 已有 `/workflows` 列表、详情、状态行、保存/重跑历史 workflow（C1/C2）。

### 4.2 目标入口

新增模型可见工具 `workflow`：

- 工具名：`workflow`（GA 工具名统一 snake_case；对应 CC 的 `Workflow`）。
- 描述：CC `workflow-tool-description.md` 的本地化版。
- 描述末尾短提示：`Before writing a script, load the workflow-authoring skill...`。
- schema 字段：
  - `script`：内联脚本。
  - `scriptPath`：workspace 内已有脚本路径。
  - `name`：已保存 workflow 名。
  - `args`：注入脚本的 JSON 值。
  - `resumeFromRunId`：恢复 runId。
  - `timeoutSeconds`：可选超时。
- 工具调用后立即返回 `status: "async_launched"`、`runId`、`workflowName`、`summary`、`scriptPath`、`message`。

### 4.3 接线设计

新增 `GenericAgentHandler.do_workflow(args, response)`：

- 从 `self.parent.workflow_tool_launcher` 取 bridge 注入的 launcher。
- 无 launcher 时返回结构化错误，提示使用 `/workflow <task>`；不伪造成功。
- 有 launcher 时把 `script` / `scriptPath` / `name` / `args` / `resumeFromRunId` / `timeoutSeconds` 传入。
- 返回 `StepOutcome(result, next_prompt=...)`，让主 agent 得到工具结果。

新增 `GenericAgentBridge.workflow_launch_from_tool(...)`：

- 与 `workflow_plan` / `workflow_draft` / `workflow_approve` 不同：它是在主 agent 工具调用内部被调用的，此时 `agent.is_running == True`，所以**不能复用这些方法的 busy 检查**。
- 解析输入：
  - `name` 与 `script` / `scriptPath` / `resumeFromRunId` 互斥。
  - `script` 与 `scriptPath` 互斥。
  - `resumeFromRunId` 可以单独使用，也可以与 `script` / `scriptPath` 组合。
- 读取脚本：
  - `script`：直接使用。
  - `scriptPath`：通过 `workflow_workspace.resolve_workspace_child()` 限制在 GA workflow workspace 内，再读 UTF-8 文本。
  - `name`：`workflow_store.load_named_workflow(name)`。
  - 仅 `resumeFromRunId`：读取源 run 的 `script`。
- 校验脚本：
  - `workflow_planner.validate_rendered_workflow_script(script)`：先跑 `workflow_script_contract.js --check`（meta 第一条 + 纯字面量），再 `node --check`，再确定性闸门。
  - 校验失败直接返回 `{"status":"error","error":...}`，不创建 run。
- 创建 run：
  - `workflow_controller.create_draft(session_id, script)`。
  - `run.status = "running"`。
  - metadata 写 `workflowName` / `workflowSummary` / `launchedBy: "workflow_tool"` / `scriptSource` / 可选 `namedWorkflow` / `resumeFromRunId`。
  - `workflow_store.save_run(run)`。
  - 追加 `workflow_started` 事件。
- 启动后台线程：
  - 复用 `_run_workflow_runtime(run_id, args, effective_timeout, resume_from_run_id)`。
  - 线程放入 `self._workflow_threads[run_id]`，保证 `/workflows` stop / wait 可管理。
- UI 事件：
  - `_emit_agent_read_model()`。
  - `emit({"type":"workflow_run", ...})`。
  - `_emit_workflow_events(run_id)`。
  - 之后 watcher 会持续发 `workflow_progress`，终态发 `workflow_final` 并排队 `<workflow_handoff>`。
- 返回：
  - `status: "async_launched"`。
  - `taskId: "local_workflow_<runId>"`。
  - `taskType: "local_workflow"`。
  - `runId`、`workflowName`、`summary`。
  - `scriptPath` 指向 `run.artifact_dir/script.js`。
  - `message`：提示 `/workflows` 监控；完成后会收到 handoff。

### 4.4 与 `/workflow <task>` 的关系

- `/workflow <task>` 保留：它是用户显式 opt-in 的 planner 路径，适合“给任务文本，让 GA 规划并渲染 JS”。
- `workflow` 工具是 CC 风格主入口：模型在明确 opt-in 后直接写 JS，获得 CC 的循环、条件、动态 fan-out、pipeline 能力。
- 两条路径共用：
  - `workflow_script_contract.js` / `workflow_determinism.py`；
  - `WorkflowStore` / `WorkflowController` / `WorkflowRuntime`；
  - `/workflows` UI、状态行、save/run named/resume。

---

## 5. G8：脚本内 `workflow()` 一层嵌套

### 5.1 CC 语义

源码坐标：`m0869_0xd12567a.js @30215 yo`、`@29905 tsr`；`dynamic-workflow-internals.md §5.1.1 / §6`。

- 脚本内可 `await workflow(nameOrRef, args)`。
- 只允许一层嵌套；第二层走拒绝桩 `tsr`。
- 嵌套 run 与外层共享 abort / 生命周期；外层停止时子 workflow 不能继续独立跑。
- 子 workflow 的结果是普通脚本返回值，直接回给外层脚本。

### 5.2 GA 实现设计

Worker 侧 `workflow_js_worker.js`：

- sandbox 增加 `workflow: (nameOrRef, args) => rpc('workflow', { nameOrRef, args })`。
- 校验 `nameOrRef` 是 string 或 plain object；否则抛 TypeError。

Runtime 侧 `workflow_runtime.py`：

- `_handle_rpc()` 增加 `method == "workflow"` 分支。
- `workflow_depth >= 1` 时直接回 `ok:false`，错误文案说明只允许一层。
- 解析 `nameOrRef`：
  - string：`workflow_store.load_named_workflow(name)`。
  - object `{name}`：同上。
  - object `{script}`：内联脚本。
  - object `{scriptPath}`：通过 `resolve_workspace_child()` 限制在工作区内读取。
- 用 `validate_rendered_workflow_script()` 做契约/语法/确定性校验。
- 创建嵌套 run：
  - `session_id` 继承外层。
  - `metadata.parentRunId = outer_run.run_id`。
  - `metadata.workflowDepth = 1`。
  - `metadata.nestedWorkflow = name`。
  - workspace 继承外层 workspace（共享外层上下文）。
  - 追加 `workflow_started` 事件。
- 创建嵌套 `WorkflowRuntime`：
  - 复用外层 store / runner / scheduler_config / worker_path / workspace_root。
  - `nested_runtime.workflow_depth = self.workflow_depth + 1`。
  - `nested_runtime.parent_run_id = outer_run.run_id`。
  - `nested_runtime.job_id_prefix = f"nested_{nested_run.run_id}_"`。
- 调 `nested_runtime.run(nested_run, args=nested_args)`，把 `nested_result.result` 通过 `rpc_result` 返回给外层 worker。
- 异常时回 `ok:false`，让外层脚本按普通 `await` 失败处理。

### 5.3 为什么需要 `job_id_prefix`

外层和嵌套 run 都会由 `AgentScheduler` 生成 `agent_1`、`agent_2` 等 job id。

- `NativeGPTChildAgentRunner` 的 `_states` 以 `job_id` 为键。
- 如果内外层共用 runner 且 job id 相同，嵌套 run 的 `agent_1` 会覆盖外层 run 的 `agent_1` 状态，导致 poll 拿错结果或外层 job 卡死。
- 解决：`AgentScheduler` 增加 `job_id_prefix`；嵌套 run 使用 `nested_<runId>_agent_N`，与外层 `agent_N` 不冲突。
- 这比“嵌套必须新建一个 runner”更小改动，也保留同一 runner 的 binding / capability 配置。

### 5.4 父 run 停止传播

- 嵌套 runtime 增加 `parent_run_id`。
- `_raise_if_externally_stopped()` 同时检查自身 run 和父 run：
  - 自身 `killed` / `cancelled` → 按自身状态停止。
  - 父 run `killed` / `cancelled` → 嵌套 run 标记同样状态、取消未完成 jobs、杀 worker。
- 这保证用户从 `/workflows` 停止外层 run 时，子 workflow 不会继续偷偷跑完。

### 5.5 嵌套 run 的 UI / 历史

- 嵌套 run 是独立 `WorkflowRun`，会出现在 `/workflows` 列表。
- 元数据 `parentRunId` 让它可被追踪为外层 run 的子运行。
- 共享 workspace：嵌套 run 的 artifact 与父 run 在同一 workspace；文档要求脚本作者用不同文件名或子目录，避免覆盖。
- 共享 store：外层和内层都走同一 `WorkflowStore`，resume / journal / result 机制一致。

---

## 6. GA 与 CC 的 DSL 差异（authoring reference 必须本地化）

| 能力 | CC | GA | 处理 |
| --- | --- | --- | --- |
| `agent` / `parallel` / `pipeline` / `phase` / `log` / `args` | 支持 | 支持 | 保留 |
| `schema` / `strictSchema` / `schemaRef` | 支持 | 支持 | 保留 |
| `budget` | 支持 | **不支持（B1 暂缓）** | 不写示例，明确标注不支持 |
| `workflow(nameOrRef,args)` | 支持一层 | 本轮实现一层 | 保留，标注只允许一层 |
| `runPythonUnittest` / `repairAndRetest` | 无 | GA 专有 | 必须写进 GA reference |
| `role` / `toolProfile` / `writeScope` / `deliverables` / `acceptanceChecks` / `capabilities` / `requiredTools` / `dependsOn` / `fallback` / `retryPolicy` | 无或不同 | GA 支持 | 必须写进 GA reference |
| `model` / `effort` | 支持 | 不支持 | 标注不支持 |
| `isolation` | worktree/remote | 不支持 | 标注不支持；remote 写云端声明 |
| `agentType` / `disallowedTools` / `bashCommandClamp` | 支持 | 不支持 | 标注不支持 |
| CC 工具名 `Write` / `Read` / `Bash` | CC | 不适用 | 必须换成 GA `file_write` / `file_read` / `code_run` |
| `Date.now()` / `Math.random()` / 无参 `new Date()` | 禁止 | 禁止 | 保留 |
| `meta` 纯字面量、maxBytes=524288 | 支持 | 支持 | 保留 |

---

## 7. UI / 历史复用要求

新增工具入口必须复用已有 UI，而不是另起一套：

- `/workflows`：列表能看到 tool-launched run。
- `/workflow detail RUN_ID`：能看到脚本、事件、进度、agent transcript 引用。
- `/workflows` 面板：运行中显示实时进度；终态显示 completed/degraded/failed/stopped。
- `s`：保存为命名 workflow；之后可用 `workflow({name, args})` 或 `/workflow run <name>` 重跑。
- `x` / `/workflow stop RUN_ID`：停止运行中的 tool-launched run。
- `resumeFromRunId`：复用已有最长未变前缀缓存。
- 完成后 `<workflow_handoff>`：主 agent 能读到终态、artifact refs、artifact owners、collision/side-effect 信息。
- 云端 `remote_launched` 不显示、不复刻。

---

## 8. 测试与验收

### 8.1 C3

- `assets/tools_schema.json` / `_cn.json` 都包含 `workflow`，参数覆盖 script/scriptPath/name/args/resumeFromRunId/timeoutSeconds。
- `skills_runtime.get_default_skill_roots()` 包含仓库 `skills/`；`find_skill("workflow-authoring")` 可加载。
- `GenericAgentHandler.do_workflow`：
  - 无 launcher → 结构化 error，不伪造成功。
  - 有 launcher → 传递字段，返回 launcher 的 async_launched payload。
- `GenericAgentBridge.workflow_launch_from_tool`：
  - 合法内联脚本 → 创建 run、立即返回 async_launched、后台完成。
  - 非法 meta / 非确定性脚本 → error，不创建 run。
  - named workflow / resume / scriptPath 至少各一条。
- workflow 子 agent 的 tool schema 不含 `workflow`，dispatch 被 capability gate 拒绝。

### 8.2 G8

- 外层脚本 `await workflow('named-child', args)` 能拿到子 workflow 返回值。
- 二层嵌套被拒绝，错误包含 “only one level” 或等价文案。
- 嵌套 run 的 metadata 含 `parentRunId`。
- 嵌套 run 的 job id 带 `nested_<runId>_` 前缀，不覆盖外层 runner state。
- 停止外层 run 时，嵌套 run 也进入 killed/cancelled。
- `workflow()` 的参数错误（非 string/object）在 worker 侧抛 TypeError。

### 8.3 回归

- `python -m unittest discover -s tests`
- `npm --prefix frontends/ink-ui run typecheck`
- `npm --prefix frontends/ink-ui run test`
- `node --check workflow_js_worker.js`
- `node --check workflow_script_contract.js`
- `git diff --check`

---

## 9. 实施顺序

1. `AGENTS.md` 中途调研记录（已完成）。
2. 本调研文档（已完成）。
3. 新增 `skills/workflow-authoring/SKILL.md`（已落盘，待测试）。
4. `skills_runtime.py` 发现仓库 `skills/`（已落盘，待测试）。
5. `workflow` 加入 `subagent_capabilities.ORCHESTRATION_TOOLS`（已落盘，待测试）。
6. 两个 `tools_schema*.json` 增加 `workflow`（已落盘，待测试）。
7. `ga.py::do_workflow`。
8. `frontends/ink_bridge.py::workflow_launch_from_tool` + agent launcher 注入。
9. `workflow_js_worker.js::workflow()` + `workflow_runtime.py` 嵌套 RPC + `workflow_scheduler.py::job_id_prefix`。
10. 新增/更新测试。
11. 新增 `docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`，并回填实施方案 §4/§5/§6。

---

## 10. 相关路径

- CC 源码：
  - `D:\git_codes\claudecode283_hack\claudecode283\modules\m1569_0xdceb889.js`
  - `D:\git_codes\claudecode283_hack\claudecode283\modules\m1502_0xdb4a986.js`
  - `D:\git_codes\claudecode283_hack\claudecode283\modules\m0869_0xd12567a.js`
- CC 抽取资产：
  - `D:\git_codes\claudecode283_hack\claudecode283\extracted\workflow-tool-description.md`
  - `D:\git_codes\claudecode283_hack\claudecode283\extracted\workflow-authoring-reference.md`
  - `D:\git_codes\claudecode283_hack\claudecode283\extracted\dynamic-workflow-internals.md`
- GA 上游方案：
  - `docs/20261012-ga-dynamic-workflow-复刻实施方案.md`
- GA 实施记录：
  - `docs/20261012-ga-dynamic-workflow-实施记录-A1.md`
  - `docs/20261012-ga-dynamic-workflow-实施记录-A2-A3.md`
  - `docs/20261012-ga-dynamic-workflow-实施记录-A4.md`
  - `docs/20261012-ga-dynamic-workflow-实施记录-B2.md`
  - `docs/20261012-ga-dynamic-workflow-实施记录-C1-C2.md`
  - `docs/20261012-ga-dynamic-workflow-实施记录-C3-G8.md`（实施后创建）
