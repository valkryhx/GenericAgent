# GA workflow 等待 run 终态（对齐 CC）+ 缺陷 H/I 实施记录

- 日期：2026-10-13
- 状态：**已实施**（含 3 处修复：缺陷 A、缺陷 H、缺陷 I）
- 触发：用户实测「workflow 有执行失败就一直卡住」——`wait_agent(condition="workflow_terminal", target=<runId>)` 空等到 timeout，run 早已 `degraded` 结束。
- 真实 run：`wf_9af50ac554fd43cc9f9d25cc467cd43b`（session `session_6a6b6fa180be401e997cbaf0994c7661`）
- CC 证据仓库（只读）：`D:/git_codes/claudecode283_hack`
- 关联：`docs/20261012-ga-workflow-实测缺陷清点-待分析.md`（缺陷 A）、`AGENTS.md` 缺陷 F/G

> **复刻时暂时不需要参考 claudecode 云端会话的实现**（`remote_launched` / 云端 run 一律不复刻）。

---

## 1. 现象与根因

### 1.1 缺陷 A：`workflow_terminal` 是假谓词（本次核心）

现象（用户截图 + `temp/model_responses/model_responses_297593.txt`）：

```text
Tool: wait_agent { "condition": "workflow_terminal", "target": "wf_9af50ac554fd43cc9f9d25cc467cd43b", "timeout_seconds": 600 }
-> {"status": "timeout", "satisfied": false, "remaining_targets": ["wf_9af50ac554fd43cc9f9d25cc467cd43b"], ...}
```

根因链（逐条可复现）：

1. `subagent_wait.py`：`workflow_terminal` 只是 `all_terminal` 的别名，按 **subagent 状态机**判定「所有目标 turn_status 终态」。
2. `subagent_manager.py` 的 `probe_agent()`：`target` 被 `normalize_task_name()` 当成 task_name（`wf_<32hex>` 恰好合法），读 `temp/<runId>/state.json`，**找不到就合成**一个非终态（`turn_status="running"`、`process_status="exited"`、`round=0`）。
3. 该合成状态**没有任何人会推进**（workflow run 不是 subagent），于是 `turn_terminal` 永远不成立 → wait 走满 timeout；模型再发一次 wait → 永远卡住。
4. 真实 run 的终态在**另一个地方**：`temp/sessions/<sid>/workflows/<runId>/state.json` 的 `status`（`succeeded`/`degraded`/`failed`/`killed`…），wait 完全不读。

实测：该 run 在 19:57 就 `status="degraded"` 收尾（journal 有 `workflow_finished`），wait 却一直对着孤儿 state 空等。

### 1.2 缺陷 H：verify 的 JSON 答案被误解析（schema 校验假失败）

- run `wf_9af50ac554fd43cc9f9d25cc467cd43b` 的 `verify:queens`（`agent_6`）**答对了**，返回
  `<summary>…</summary>` + `{"file": "eight_queens.py", "ok": true, "reason": "…列向量 [0,4,7,5,2,6,1,3]…", }`。
- 两个叠加原因：
  - **尾逗号** `, }` → `json.loads` 直接失败；
  - `parse_json_answer` 的兜底「取第一个 `[` 到最后一个 `]`」把 reason 里的**数组** `[0,4,7,5,2,6,1,3]` 当成 payload → 变成 `{"result":[...]}` → 缺 `file/ok/reason` → `schema_validation_failed`。

### 1.3 缺陷 I：verifier 传坏路径直接把 job 打死

- `verify:html_text`（`agent_7`）把**工作区根目录本身**当文件传给 `file_read`。
- 链路：`workflow_path_acl.check_path_access` 认为根目录在 workspace 内 → 放行；执行层
  `_get_abs_path → resolve_workspace_child` 要求必须是**子路径** → 抛
  `WorkspacePathError: path must name a workspace child`；`do_file_read` 把 `_get_abs_path` 放在 try 之外 → 异常冒到子代理主循环 → job `failed`（面板 `✗`）。

---

## 2. CC 的设计思路（证据）

来源：`claudecode283/extracted/dynamic-workflow-internals.md` §8.3/8.4、`workflow-tool-description.md`。

| CC 事实 | 出处 |
| --- | --- |
| workflow run 是**后台任务**：工具**立即返回** task id，**不轮询** | `workflow-tool-description.md:1`（"Workflows run in the background — this tool returns immediately with a task ID"） |
| run 结束时由 `RTn()` 组装 **`<task-notification>`**，以 `mode:"task-notification"`、`priority:"next"` 入队 → **唤醒主 agent** | `dynamic-workflow-internals.md` §8.3、§8.4 |
| 通知里的 `<status>` 只有三种：`completed` / `failed` / `killed` | §8.4（`rT="status"`） |
| 有 agent 失败但整体完成时，通知带 **`<failures>`** 列表 + `<usage>`（agents_done/agents_error/agents_empty_result） | §8.3 |
| `<diagnostics>`：`journal.jsonl` 每完成一个 agent 一行 `{"type":"result",…}`；**结果为空或异常时先读它再诊断** | §8.3 |
| 失败/被杀 → `<recovery>` 给出 `Workflow({scriptPath,resumeFromRunId})` | §8.3、§8.6 |

**结论**：CC 没有 `wait_agent(workflow_terminal)` 这种轮询；它靠「run 有唯一终态 + 完成即唤醒 + 失败列表 + 诊断指引」。GA 保留 `workflow_terminal` 谓词（自身 API 已暴露），因此**必须让它真的读 run 终态**，同时把「失败列表 + journal 诊断指引」补进 handoff，才算对齐 CC 的失败处理。

---

## 3. 方案（GA 侧）

1. **把 workflow run 变成可等待实体**：`wf_<32hex>` 目标不再走 subagent 状态机，而是经「run 状态解析器」映射到 subagent 的 `turn_status` 词汇。
   - `succeeded/completed/closed/cached → completed`；`degraded/partial → completed`（**终态**，raw status 另存 `completion_status`）；`failed → failed`；`killed → killed`；`cancelled/interrupted/stale → 各自`；`running/draft/awaiting_approval → running`。
   - 运行中 `process_status="alive"`，终态 `="exited"`（避免 `_is_notify_state` 每轮误报变化而空转）。
2. **未知 runId 立即报错**（不再合成孤儿 state 空等）。
3. **僵尸 run 兜底**：run 非终态但**本进程记录的 launcher 线程已死** → 报 `stale`（终态）→ wait 立即返回。
4. **handoff 补 CC 的失败面**：`failedJobs`（`<failures>` 对应物）+ `journalRef` + 「先读 journal 再诊断」提示。
5. 缺陷 H/I 的健壮性修复（见 §4）。

---

## 4. 实施（文件与坐标）

### 4.1 缺陷 A（workflow 终态）

| 文件 | 改动 |
| --- | --- |
| `subagent_wait.py` | 新增 `WORKFLOW_RUN_ID_RE` / `WORKFLOW_TERMINAL_RUN_STATUSES` / `is_workflow_run_id()` / `workflow_run_is_terminal()` / `workflow_run_turn_status()` |
| `subagent_manager.py` | `SubagentManager.__init__(..., workflow_run_resolver=None)`；新增 `_workflow_resolver` / `_default_workflow_run_resolver`（读 `temp/sessions/*/workflows/<runId>/state.json`）/ `_workflow_state_from_info` / `_workflow_target_state` / `resolve_workflow_run`；`probe_agent`、`read_agent` 前置解析（命中即返回，**不落盘**） |
| `ga.py` | 新增 `_bind_workflow_wait_resolver` / `_unknown_workflow_targets`；`do_wait_agent`、`do_read_agent_result` 注入 resolver 并对未知 runId **快速报错**（含 hint，禁止重试同一 id） |
| `frontends/ink_bridge.py` | `_bind_agent` 绑 `agent.workflow_run_resolver = self._resolve_workflow_run_info`；新增 `_resolve_workflow_run_info`（workflow store 权威 + 死线程 `stale` 兜底）；handoff 增 `failedJobs` / `journalRef` / 诊断指引 |
| `assets/tools_schema.json` / `assets/tools_schema_cn.json` | `workflow_terminal` 语义 + `workflow` 工具描述补「后台任务 / 完成唤醒 / 终态集合」 |

### 4.2 缺陷 H（JSON 尾逗号 + 数组误判）

| 文件 | 改动 |
| --- | --- |
| `workflow_child_agent.py` | 新增 `_strip_trailing_commas()` / `loads_json_answer()`（容忍 `}`/`]` 前尾逗号、保留字符串内逗号）；`parse_json_answer` 改用 `loads_json_answer` |
| `workflow_scheduler.py` | `_coerce_structured_text_payload` 改用 `loads_json_answer` |

### 4.3 缺陷 I（坏路径打死 job）

| 文件 | 改动 |
| --- | --- |
| `ga.py` | 顶部 `from workflow_workspace import WorkspacePathError`；`GenericAgentHandler.dispatch` 末尾包 `try/except WorkspacePathError` → 返回**可恢复**的工具错误（`{"status":"error","msg":"invalid workspace path: …"}`），不再打死 job |

---

## 5. 验证

- 新增 `tests/test_workflow_wait_terminal.py`（12 例）：runId 形态、状态映射、运行中不满足且不永久卡死、终态立即满足、`degraded` 视为终态、运行→终态在超时内满足、**不产生孤儿 state**、未知 runId→None、默认文件系统解析器、死线程 `stale` 兜底、`_unknown_workflow_targets` 只列未解析 run。
- 新增 `tests/test_workflow_child_path_recovery.py`（2 例）：`file_read` 传根目录 → 可恢复错误；传相对子文件 → 正常读。
- 新增 `tests/test_workflow_child_agent.py::StructuredAnswerExtractionTest`（3 例）：尾逗号对象可恢复、prose 里的数组不误判、字符串内逗号保留。
- **对真实卡住的 run 直接复验**（关键证据）：

```text
resolve wf_9af50ac554fd43cc9f9d25cc467cd43b -> ('completed', 'degraded', 'available')
wait_agents(condition=workflow_terminal) -> satisfied=True, terminal_targets=[wf_9af…], next=read_agent_result
未知 runId -> satisfied=False（且 do_wait_agent 会直接报错）
handoff -> failedJobs=[verify:queens(schema_validation_failed…), verify:html_text(path must name a workspace child…)] + journalRef
```

- 回归：`tests.test_ink_bridge + tests.test_ga_subagent_tools + tests.test_workflow_wait_terminal` → **168 OK**；`tests.test_workflow_scheduler/store/child_agent/child_path_recovery/implicit_isolation` → OK。

---

## 6. 已知限制 / 下一步

- `stale` 兜底只对**本进程**启动的 run 生效（`_workflow_threads` 记录）；跨进程（CLI 子进程跑 workflow）暂不判定，避免误报。
- 未做：run 级「心跳超时」更细粒度判定；`/workflows` 面板对 `stale` 的展示文案（当前面板按 run.status 渲染，`stale` 会落到未知态分支，后续可补）。
- **复刻时暂时不需要参考 claudecode 云端会话的实现**（`remote_launched`、云端 run、云端 adopt 一律不做）。
