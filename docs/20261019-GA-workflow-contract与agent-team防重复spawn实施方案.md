# GA workflow contract 与 Agent Team 防重复 spawn 实施方案

日期：2026-10-19
依据：`docs/20261019-CC-workflow-contract与agent-team防重复spawn调研.md`
适用范围：`D:\git_codes\GenericAgent`

> 本方案不是照抄 CC 的表象，而是把 CC 的成熟机制映射到 GA 的真实进程模型。
> 云端会话不纳入范围：【复刻时暂时不需要参考 claudecode 云端会话的实现】。

---

## 0. 目标与非目标

### 目标

1. Workflow 真实执行成功后，E2E 不再因为“没有 GA 扩展 executionContract”而误报失败。
2. 当 prompt 没有显式要求 executionContract 时，用真实 child 工具证据和实际产物验收。
3. 当 prompt 显式要求 executionContract 时，仍然 fail-closed。
4. Agent Team 不再因为 read-only role 误配而启动一轮必然失败的 teammate。
5. `turn_status=completed` 不再把失败任务误标为 completed。
6. `task_update` 自动 owner、空 owner 释放、task_create 去重与 task 绑定形成闭环。
7. team_mode spawn 必须绑定 claimable task；无任务时结构化拒绝，不启动进程。
8. 测试从“至少 N 个 teammate”升级为“每个任务恰好一个有效 teammate”。

### 非目标

- 不实现 CC remote/cloud agent。
- 不把 CC 的 tmux/iTerm2 pane 后端搬进 GA。
- 不把 `team_name` 变成必填；GA 继续使用单一隐式 team。
- 不重新引入 JSON planner。
- 不删除 GA 的 `executionContract`；它保留为可选强验收扩展。

---

## 1. P0：修复 Workflow E2E 的验收来源

### 1.1 修改文件

- `frontends/ink/ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts`

### 1.2 新验收规则

从 persisted `run.json` 读取 jobs：

```text
run.metadata.executionContract
  -> 仅当 TASK 显式包含 executionContract / requiredToolEvidence 时作为强制契约

job.metadata.toolSummary.allowedTools
  -> 始终作为真实工具证据来源

job.metadata.toolSummary.writtenPaths
job.metadata.observedArtifacts
  -> 作为实际产物来源
```

### 1.3 具体逻辑

1. 增加 `taskRequiresExplicitContract = /executionContract|requiredToolEvidence/i.test(TASK)`。
2. 增加 `loadPersistedJobs(run.artifactDir)`。
3. 增加 `collectAllowedTools(jobs)`，从每个 job 的 `metadata.toolSummary.allowedTools` 汇总。
4. 增加 `selectActualArtifact(jobs, contract)`：
   - 优先 `toolSummary.writtenPaths` 中 `.html|.md|.docx|.pdf`；
   - 其次 `metadata.observedArtifacts`；
   - 最后才回退 `executionContract.artifacts`。
5. 如果 `taskRequiresExplicitContract`：
   - 断言 contract 存在；
   - 断言 `requiredToolEvidence` 包含 `EXPECTED_MCP_TOOL`；
   - 断言 contract artifact 路径是 workspace-relative POSIX path。
6. 否则：
   - 不断言 contract；
   - 断言 `collectAllowedTools(jobs)` 包含 `EXPECTED_MCP_TOOL`；
   - 断言实际 artifact 存在且可读；
   - 断言 `workspacePolicy` 仍为 `project-temp-workspace-write-v1`。
7. summary 增加：
   - `contractMode: explicit|actual-evidence`
   - `evidenceTools`
   - `artifactSource: writtenPaths|observedArtifacts|executionContract`

### 1.4 单测/回归

- 用已有的 `wf_e10b3756095e42be972ab64d55aa2e0c` 证据做离线断言：
  - `agent_1..3` 的 `allowedTools` 都含 `mcp__fetch__fetch_markdown`；
  - `agent_4` 的 `writtenPaths` 含 `temp/workflow-e2e-report.html`；
  - 不要求 run metadata 有 `executionContract`。
- 真实 E2E 仍用 `deepseek-flash`，命令见 §5。

### 1.5 文档同步

- `skills/workflow-authoring/SKILL.md` 增加“schema 是 CC 主路径，executionContract 是可选 GA 扩展”。
- `skills/workflow-authoring/references/api.md` 增加“工具证据来自 child toolSummary/transcript；不要用 prose 自称成功替代真实工具调用”。

---

## 2. P0：修复 read-only teammate 的协调能力

### 2.1 修改文件

- `genericagent/workflow/permissions.py`

### 2.2 变更

把 team control-plane 工具加入 `ALLOWED_READ_ONLY_STATIC_TOOLS`：

```text
send_message
task_create
task_get
task_update
task_list
team_status
```

理由：

- 这些工具不修改工作区文件；
- CC teammate 即使在 read-only/review 角色下也必须能更新任务板和通知 lead；
- 否则会出现“任务已认领但无法置完成/无法报告阻塞”的状态机死锁。

### 2.3 测试

- `tests/test_workflow_permissions.py` 或等价测试：
  - read_only profile 允许上述工具；
  - read_only profile 仍拒绝 `file_write`、`file_patch`、`code_run`。
- 增加真实/半真实 child 测试：
  - explore teammate 能 `task_update` 和 `send_message`；
  - 仍不能写文件。

---

## 3. P0：turn completed 不再自动完成 task

### 3.1 修改文件

- `genericagent/core/app.py`
- `tests/test_subagent_tasks.py`（删除/改写 fallback 测试）
- 如保留 `complete_owned_in_progress()`，只作为显式工具，不在 idle hook 自动调用。

### 3.2 变更

在 `_teammate_idle_hook()`：

- 删除“`turn_status == completed` -> `complete_owned_in_progress()`”自动完成逻辑；
- teammate 必须显式 `task_update(status="completed")`；
- `close_agent` 继续通过 `reclaim_tasks_for()` 把未完成任务置回 pending。

### 3.3 为什么这样改

CC 的 TaskUpdate completed 是 teammate 的显式动作。GA 之前的 fallback 在 BLOCKED 输出上也会触发，导致任务板谎报成功。任务板是事实源，不能用进程回合结束代替任务成功。

### 3.4 测试

- teammate 输出 BLOCKED 后：
  - task 仍为 `in_progress`；
  - close 后变为 `pending` 且 owner 清空；
  - 不出现 completed。
- teammate 显式 `task_update(completed)` 后：
  - task 为 completed；
  - close 不会把它重置。

---

## 4. P1：TaskUpdate 自动 owner 与 owner 释放

### 4.1 修改文件

- `genericagent/core/handler.py`
- `genericagent/agents/tasks.py`
- `assets/tools_schema.json`
- `tests/test_ga_task_tools.py`
- `tests/test_subagent_tasks.py`

### 4.2 handler 规则

`do_task_update()`：

1. 取 `task_id`。
2. `owner` 参数归一化：
   - 缺失：不传 owner；
   - `None` 或 `""`：传 `None`，表示释放。
3. 如果 `status == "in_progress"` 且 owner 缺失：
   - 读取任务；
   - 若任务无 owner：自动 owner = `_self_author()`；
   - 若任务已有 owner 且不是当前 agent：返回 `already_claimed`，不修改状态。
4. 如果 status completed：
   - 允许 owner 或 lead 完成；
   - teammate 不应完成别人 owner 的任务。

### 4.3 store 规则

`SubagentTaskStore.update_task()`：

- `owner=None` 清空 owner；
- `owner=<value>` 继续走 `_claim_unlocked`；
- 增加可选 `actor` / `allow_reassign` 参数，避免 teammate 越权修改别人任务；
- 保持跨进程锁和原子写。

### 4.4 schema 文案

`task_update`：

- `owner` 描述改为“省略表示不变；传 null 或空字符串表示释放；不要用 v2 名字绕过 owner”。
- `status` 描述增加“设置 in_progress 且无 owner 时自动认领”。

### 4.5 测试

- 无 owner 任务 `task_update(status=in_progress)` 自动 owner。
- `owner=""` 成功释放。
- 已被别人拥有的任务不能抢占。
- teammate 不能把别人任务置 completed。

---

## 5. P1：team_mode spawn 绑定任务、任务去重与能力预检

### 5.1 修改文件

- `genericagent/core/handler.py`
- `genericagent/agents/tasks.py`
- `assets/tools_schema.json`
- `genericagent/agents/prompts.py`
- `genericagent/agents/team_activation.py`
- `frontends/ink/ui/scripts/real_ink_team_swarm_e2e.ts`
- `tests/test_ga_subagent_tools.py`
- `tests/test_subagent_tasks.py`

### 5.2 spawn_agent 新参数

在 `spawn_agent` schema 增加：

```json
{
  "team_task_id": {
    "type": "string",
    "description": "Exact shared-task id this teammate must own. Required for a new team_mode spawn unless a matching claimable task can be bound deterministically."
  }
}
```

`do_spawn_agent()`：

- 读取 `team_task_id`；
- `team_mode=true` 时先解析任务：
  1. 显式 `team_task_id`：只能 claim 该任务；
  2. 否则按 task_name / message 关键词匹配 claimable task；最高匹配分为 0 时不做 FIFO 兜底；
  3. 找不到 claimable task 或没有确定性匹配：返回 `team_task_required`，不 spawn；
- 绑定成功后，task 状态为 in_progress、owner 为 `/root/<task_name>`；
- 绑定失败（已被别人拥有/blocked/completed）返回结构化错误；
- spawn 失败时 reclaim owner 回 pending。

### 5.3 task_create 去重

`SubagentTaskStore.create_task()`：

- 规范化 subject：trim、collapse whitespace、casefold；
- 如果已有**未完成**任务 subject 相同：
  - 返回已有 task，附带 `duplicate: true`；
  - 不分配新 id；
- handler 返回 `status: "existing"` 或 `status: "success"` + `duplicate: true`；
- schema 描述提示“先 task_list；重复 subject 会复用已有任务”。

### 5.4 role capability preflight

在 spawn 前检查：

- role profile 为 `read_only` 或 `verify`；
- 任务 subject/description/message 命中写/执行意图：
  - 写：`file_write|file_patch|写入|写文件|落盘|保存|输出到|创建文件`
  - 执行：`code_run|运行|执行|命令|subprocess|python`
- 若命中，返回：
  ```json
  {
    "status": "error",
    "reason": "team_role_capability_mismatch",
    "role": "explore",
    "permission_profile": "read_only",
    "msg": "explore is read-only. Omit agent_type or use a write/execute-capable role for this task."
  }
  ```
- 不启动 child，不占用 agent slot。

> 这是 GA 的 fail-closed 适配；CC 依赖更强模型自己选对角色，GA 不应把这类错误留给进程启动后再发现。

### 5.5 提示词收紧

`LEAD_TEAM_USAGE_HINT_*` 和 `team_activation_reminder()`：

- `task_create` 前先 `task_list`；
- 每个 team_mode spawn 必须绑定 `team_task_id`；
- 不要为同一 task 用 v2 名字重开；
- 如果任务已 owner：先 `list_agents`/`read_agent_result` 检查原 teammate，不要重复 spawn；
- 如果 role 是 read-only，不要派写文件/执行代码的任务；
- 任务完成后 `task_list` 找下一项，不要重复建同 subject 任务。

`TEAMMATE_USAGE_HINT_*`：

- 绑定 task 时直接开始，不重复认领；
- 设置 in_progress 可省略 owner，host 会自动认领；
- 完成必须显式 task_update completed；
- 阻塞时 send_message lead，并保留任务为 in_progress 或 pending（不要谎报 completed）。

### 5.6 测试

- team_mode 无 claimable task -> `team_task_required`，manager.spawn_agent 未被调用。
- team_mode 显式 task_id -> 绑定正确，child prompt 含该 id。
- task 已被别人拥有 -> `already_claimed`，不 spawn。
- task_create 相同 subject -> 返回同一个 id，next_id 不增长。
- read_only role + 写任务 -> `team_role_capability_mismatch`，不 spawn。
- read_only role + 纯研究任务 -> 允许 spawn。
- spawn 失败 -> task owner 清空、status 回 pending。

---

## 6. P1：真实 E2E 防回归

### 6.1 Workflow E2E

```bash
GA_WORKFLOW_E2E_PROFILE=default \
GA_WORKFLOW_E2E_MCP_TOOL=mcp__fetch__fetch_markdown \
GA_MCP_DISCOVERY_BUDGET=30 \
GA_WORKFLOW_E2E_START_TIMEOUT_MS=900000 \
GA_WORKFLOW_E2E_TIMEOUT_MS=900000 \
GA_WORKFLOW_E2E_TASK='Use the workflow tool ...' \
npx tsx scripts/real_ink_ui_workflow_autonomous_e2e.ts
```

验收：

- workflow status succeeded；
- jobs 全部 succeeded；
- actual tool evidence 含 `mcp__fetch__fetch_markdown`；
- actual artifact 存在且可读；
- 若 prompt 显式要求 contract，则 contract 断言也通过；
- summary 标记 `contractMode`。

### 6.2 Agent Team E2E

```bash
GA_TEAM_E2E_TIMEOUT_MS=900000 \
npx tsx scripts/real_ink_team_swarm_e2e.ts
```

验收：

- team_mode child 数量 == 任务数（默认 3）；
- 每个 child 通过 `team_task_id` 显式绑定唯一 task id；
- task board subject 无重复；
- 所有任务 completed；
- 没有第一波失败进程；
- `/team` 面板显示 task board；
- 如果故意传 read-only role + 写任务，测试应在 spawn 前失败，而不是启动 child。

### 6.3 真实模型约束

- 默认只用 `deepseek-flash`；
- 不使用 gpt-6 等其他模型判定；
- 不靠固定输出文本，靠事件流、task board、child 状态、transcript、产物。

---

## 7. 实施顺序

1. 修复 `workflow E2E` 验收来源（不改变 runtime 语义）。
2. 更新 workflow-authoring skill 文档。
3. 修复 read-only coordination tools。
4. 移除 idle hook 自动完成任务。
5. 修复 owner 归一化与自动 owner。
6. 增加 team_task_id、任务绑定、task_create 去重。
7. 增加 role capability preflight。
8. 收紧 lead/teammate prompt 与 tool schema 文案。
9. 增加/修改单测。
10. 跑聚焦单测。
11. 跑真实 deepseek-flash Workflow + Agent Team E2E。
12. 更新实施记录与 AGENTS 索引。

---

## 8. 文件影响面

| 文件 | 变更 |
| --- | --- |
| `frontends/ink/ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts` | actual evidence/artifact 验收 |
| `skills/workflow-authoring/SKILL.md` | schema 主路径 / contract 可选 |
| `skills/workflow-authoring/references/api.md` | 工具证据与产物来源 |
| `genericagent/workflow/permissions.py` | read-only 协调工具白名单 |
| `genericagent/core/app.py` | 移除 turn-completed 自动完成 |
| `genericagent/core/handler.py` | owner 归一化、自动 owner、team_task_id、能力预检 |
| `genericagent/agents/tasks.py` | task_create 去重、update_task 权限/释放语义 |
| `genericagent/agents/prompts.py` | lead/teammate 协作规则 |
| `genericagent/agents/team_activation.py` | reminder 增加 task id/role 约束 |
| `assets/tools_schema.json` | spawn/task tool 参数文案 |
| `tests/test_ga_task_tools.py` | 自动 owner、释放、越权 |
| `tests/test_subagent_tasks.py` | 去重、close reclaim、no auto-complete |
| `tests/test_ga_subagent_tools.py` | team_task_required、绑定、能力预检 |
| `frontends/ink/ui/scripts/real_ink_team_swarm_e2e.ts` | 精确 child/task 数量断言 |

---

## 9. 验收标准

- `executionContract` 不再是通用 workflow E2E 的隐含硬要求。
- schema 仍 fail-closed；显式 contract 仍 fail-closed。
- read-only teammate 可协调但不能写文件/执行代码。
- BLOCKED teammate 不会把任务标 completed。
- close_agent 能把未完成任务安全 reclaim 为 pending。
- team_mode 无任务时不启动进程。
- 同一任务不会被 v2 名字重复启动。
- task_create 不产生重复 subject。
- 真实 deepseek-flash E2E 通过，且证据可回放。

---

## 10. 相关文档

- `docs/20261019-CC-workflow-contract与agent-team防重复spawn调研.md`
- `claudecode283_hack/claudecode283/extracted/dynamic-workflow-internals.md`
- `claudecode283_hack/claudecode283/extracted/workflow-authoring-reference.md`
- `claudecode283_hack/claudecode283/extracted/agent-team-internals.md`
- `claudecode283_hack/reconstructed/deminified-agent-team/m0511_0xcd25473.js`
- `claudecode283_hack/reconstructed/deminified-agent-team/m1578_0xdd0c821.js`
- `claudecode283_hack/reconstructed/deminified/m0531_0xcd4d7c8.js`
- `docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`

---

## 11. 实施记录（2026-10-19）

### 11.1 已完成

- Workflow E2E 改为两条验收路径：
  - prompt 未显式要求 `executionContract`：优先从 persisted transcript 的 `tool_call` 事件验收，transcript 不可用时才回退 `toolSummary`；同时验收实际产物；
  - prompt 显式要求 `executionContract`：仍要求 contract、`requiredToolEvidence` 和 workspace-relative POSIX artifact path，保持 fail-closed。
- 已确认 CC 的主路径是 `agent(..., {schema})` + `StructuredOutput`，不是 GA 的全局 execution contract；相关源码证据见调研文档 §1。
- read-only teammate 保留 `task_*` / `send_message` / `team_status` control-plane 工具；写文件与执行代码仍被拒绝。
- `turn_status=completed` 不再自动把任务板任务标成 completed；teammate 必须显式 `task_update(status=completed)`，关闭未完成 child 时 reclaim 为 pending。
- `task_update` 支持自动 owner、空 owner 释放、owner 权限检查；lead 可管理任意任务，teammate 不能完成别人的任务。
- `task_create` 对未完成任务的 normalized subject 去重；已完成任务允许同名的新一轮任务。
- `team_mode=true` 必须绑定 claimable task；优先显式 `team_task_id`，名称匹配必须产生正分，否则返回 `team_task_required`，不启动进程。
- spawn 前先解析实际可用的 task name（例如历史目录存在时从 `worker` 变为 `worker_1`），再 claim task；避免 child 实际身份与 task owner 不一致。
- spawn 前执行 read-only / verify 角色能力预检；收紧执行意图识别，避免把“研究 Python API”误判为“运行 Python 脚本”。
- Agent Team E2E prompt 要求 lead 把 `task_create` 返回的 id 作为 `team_task_id` 传入；断言每个 child 绑定唯一任务、任务板无重复 subject、所有任务 completed。

### 11.2 本轮验证

已执行：

```bash
python -m unittest   tests.test_subagent_tasks   tests.test_ga_task_tools   tests.test_ga_subagent_tools   tests.test_agentmain_teammate_idle   tests.test_workflow_permissions   tests.test_subagent_prompts   tests.test_agentmain_role_prompts
# Ran 111 tests in 3.005s, OK

python -m py_compile genericagent/agents/tasks.py genericagent/core/handler.py
python -m json.tool assets/tools_schema.json
python -m json.tool assets/tools_schema_cn.json
cd frontends/ink/ui && ./node_modules/.bin/tsc --noEmit
```

另已通过 `git diff --check`、UTF-8 无 BOM、LF 校验。

#### Workflow 真实 E2E（默认 `deepseek-flash`）

命令入口：

```bash
cd frontends/ink/ui
npx tsx scripts/real_ink_ui_workflow_autonomous_e2e.ts
```

结果：**通过**。

- runId：`wf_dfb92d598fca4639960544a892b41aa7`
- status：`succeeded`
- transcript jobs：`6`（4 路 research + synthesize + verify）
- contract mode：`actual-evidence`
- evidence source：`transcript`
- 真实工具证据包含：`mcp__fetch__fetch_markdown`、`file_read`、`code_run`、`file_write`、`structured_output`
- 产物：`temp/workflow-runs/wf_dfb92d598fca4639960544a892b41aa7/temp/pathlib_write_text.html`，`38,340` bytes
- artifact source：`writtenPaths`
- UI 返回 idle：`true`
- `passed`：`true`

第一次尝试曾因用户输入没有 `workflow` 入口词而走普通工具路径，属于 E2E 输入构造问题，不是 workflow runtime 失败；第二次输入显式包含 `workflow` 后完整跑通。

这次验证同时确认：没有显式声明 `executionContract` 时，测试从真实 transcript 的 `tool_call` 取证，而不是把 GA 的可选 host 扩展误判成 CC 的通用硬契约。

#### Agent Team 真实 E2E（默认 `deepseek-flash`）

命令入口：

```bash
cd frontends/ink/ui
GA_TEAM_E2E_TIMEOUT_MS=900000 npx tsx scripts/real_ink_team_swarm_e2e.ts
```

结果：**通过**。

- runId：`1792405812532`
- task board：`session-3d9bd493`，3 个任务，3/3 completed
- team_mode children：3 个，分别为
  - `t1_root_py_count_1` → task `1`
  - `t2_tests_py_count` → task `2`
  - `t3_agents_md_headings` → task `3`
- 每个 child 的 task id 唯一，没有第一波失败后重开 #4/#5/#6 的 6-child 现象
- task board subject 无重复
- 三个 child 的 `turn_status` 均为 `completed`
- `/team` 仍返回共享任务板 3 条任务；运行后 active member 为 0 是因为 lead 已按设计关闭 teammate，不是任务板丢失
- lead 产物：`temp/team_e2e_1792405812532/report.md`，`2,694` bytes
- `passed`：`true`

### 11.3 明确边界

- 不实现 CC remote/cloud agent；【复刻时暂时不需要参考 claudecode 云端会话的实现】。
- 不重新引入 JSON planner；workflow 入口仍是 direct JavaScript。
- 不复制 CC 的 tmux/iTerm2 pane 后端；GA 继续使用现有 process/in-process teammate 模型。
- `executionContract` 仍是 GA 可选扩展：显式声明时 fail-closed，未声明时从真实工具证据与产物验收。
- 本轮真实 E2E 只使用默认 `deepseek-flash`；不把固定期望文本当作唯一判据，判据是事件、transcript、任务板、真实产物与 UI 帧。
