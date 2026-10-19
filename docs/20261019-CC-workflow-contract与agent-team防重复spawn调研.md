# CC workflow contract 与 Agent Team 防重复 spawn 调研

日期：2026-10-19
目标：先回答两个不稳定现象背后的真实机制，再给出 GA 可借鉴、不可误抄的边界。

相关源码仓库：`D:\git_codes\claudecode283_hack`
GA 仓库：`D:\git_codes\GenericAgent`

---

## 0. 结论先行

### 0.1 Workflow：CC 没有 GA 的 `executionContract.requiredToolEvidence`

CC 2.1.283 的 workflow 质量主路径不是“每个 workflow 都声明一个 host execution contract”，而是：

1. 需要结构化结果时给 `agent()` 传 `schema`；
2. CC 给该 child 注入 `StructuredOutput` 工具；
3. child 必须恰好调用一次 `StructuredOutput`，脚本只读该 tool call；
4. schema 在 spawn 前做 JSON Schema lint；
5. 验证质量由提示词方法论承担：adversarial verify、perspective-diverse verify、judge panel、completeness critic 等；
6. 并行写冲突由 `isolation: "worktree"` 和“传值不共享文件”解决。

GA 的 `executionContract` / `requiredToolEvidence` 是 GA host 扩展，不是 CC 主路径。因此：

- workflow 执行成功但没声明 `executionContract`，不等于 workflow 失败；
- E2E 不应默认强制要求 `executionContract`；
- 当 prompt 没有显式要求该扩展时，测试应从真实 child 的 `toolSummary` / transcript 中验证工具证据；
- 当 prompt 显式要求 `executionContract` 时，才应把缺失该 contract 视为测试失败。

### 0.2 Agent Team：CC 靠“身份预留 + 任务板事实源”，不靠“没有任务板就硬拒绝 spawn”

CC 的真实机制是：

- Agent 工具的 `name` 是 teammate 身份；
- `team_name` 已标记为 deprecated；当前会话是单一隐式 team；
- `spawnTeammate` 先在 team 文件中 CAS 预留身份，再启动 pane/in-process；失败时回滚；
- TaskCreate 创建 `pending`、无 owner、空依赖的任务；
- TaskUpdate 设置 `status="in_progress"` 且原任务无 owner 时，自动把 owner 设为当前 teammate；
- owner 变化后向 owner 发送 `task_assignment`；
- team context 注入 teammate 的身份、team config、task list 路径和“完成后找下一个任务”的工作流；
- CC **没有**“没有 task board 就禁止 spawn teammate”的规则。

GA 的实测失败不是“模型随机重复”，而是一个可复现的链路：

1. lead 创建任务 #1/#2/#3；
2. lead 用 `agent_type="explore"` + `team_mode=true` spawn 第一波；
3. `explore` 是只读 profile，`code_run` / `file_write` 不可用；
4. 更严重的是 `task_update` / `send_message` 也被 `read_only_static_unknown` 拒绝；
5. teammate 只能输出 BLOCKED；
6. GA idle hook 看到 `turn_status=completed`，误以为任务完成，把 owner 的 in_progress 任务自动标成 completed；
7. lead 想释放任务时遇到 `already_claimed`，因为 owner 仍在、状态又被误改；
8. lead 删除 #1/#2/#3，创建 #4/#5/#6，再用 generic teammate 重跑；
9. 最终产生 6 个 team_mode child，看起来就是“重复启动”。

所以要修的不是单纯“禁止第二次 spawn”，而是同时修：

- team 协调工具不应被只读 profile 误杀；
- `turn_status=completed` 不能当作任务成功证据；
- role 能力必须与任务写/执行需求做 spawn 前校验；
- task board 的 owner/状态必须能被正确释放；
- `team_mode` spawn 应绑定明确任务，避免“换名字重开”。

---

## 1. CC Workflow 源码证据

### 1.1 schema / StructuredOutput

源码：`claudecode283_hack/reconstructed/full/m0869_0xd12567a.js`

关键证据：

- `agent(prompt, opts)` 的 opts 包含 `schema`、`isolation`、`agentType` 等，见约 1020 行。
- schema 在 spawn 前走 `r3e(ve.schema)` lint，见约 2332-2341 行；不可满足 schema 直接拒绝 spawn。
- 有 schema 时给 child 注入 `StructuredOutput` 工具，见约 2317-2318 行。
- 有 schema 的 child system prompt 明确要求“恰好调用一次 `StructuredOutput`，脚本只读 tool call”，见约 1856-1862 行。
- 没有 schema 的 child 则把最终文本作为返回值，见约 1840-1851 行。
- `StructuredOutput` 重试上限与错误反馈在约 2638-2736 行。
- `isolation: "remote"` 在本 build 直接抛错，见约 2260 行；这是 CC 云端路径不可达的证据，不是 GA 要实现的目标。
- worktree 隔离说明见约 2458-2473 行；只有并行写冲突时才建议 `isolation: "worktree"`。

源码：`claudecode283_hack/reconstructed/full/m0227_0xc482590.js`

- `root_not_object`：tool input schema 根必须是 object，见约 642-652 行。
- `required` 中的字段必须存在于 `properties`，且 `additionalProperties: false` 时不可满足会报 lint 错误，见约 692-700 行。
- `r3e` 是 schema lint 入口，见约 1082 行。

CC authoring reference：

- `claudecode283_hack/claudecode283/extracted/workflow-authoring-reference.md`
  - `agent()` 带 schema 时返回 validated object，不需要解析 JSON 文本。
  - `isolation: "worktree"` 只用于并行修改文件的场景。
  - 子 agent 的最终文本就是返回值；结构化输出在 tool-call 层验证，失败会重试。
  - 质量模式包括 adversarial verify、perspective-diverse verify、judge panel、completeness critic。
- `claudecode283_hack/claudecode283/extracted/dynamic-workflow-internals.md`
  - §5.1 记录 schema lint、`StructuredOutput`、`isolation: remote` 不可达。
  - §5.2/5.3 记录 `parallel` barrier 与 `pipeline` 无 barrier 语义。
  - §9 记录 workflow 执行期 UI、`/workflows` 列表/详情和再跑历史 run。

### 1.2 CC 没有 GA 的 executionContract

检索结果：

- `claudecode283_hack/claudecode283/modules/*.js` 中没有 `executionContract` / `requiredToolEvidence` 的有效定义；
- `claudecode283_hack/claudecode283/extracted/dynamic-workflow-internals.md` 也没有该机制；
- CC 的 workflow 结果验证是 schema + tool-call 层验证 + 提示词质量方法，不是 GA 的 host evidence contract。

结论：GA 可以保留 `executionContract` 作为可选的强验收扩展，但不能把它当成 CC 默认语义，也不能让通用 E2E 默认依赖它。

### 1.3 对 GA 的直接映射

| 能力 | CC | GA 应怎么做 |
| --- | --- | --- |
| 结构化输出 | `agent(..., {schema})` + `StructuredOutput` | 保留 GA 的 `structured_output` 工具与 schema lint |
| 输出验证 | tool-call 层校验 + 重试 | 保留 `schemaValidation` / retry feedback |
| 质量方法 | adversarial verify / judge panel / completeness critic | 写进 workflow-authoring skill 与 planner/child 提示词，不新增 JSON planner |
| 并行写冲突 | worktree + 值传递 | 保留 GA 的隔离 workspace / worktree + handoff |
| 强验收扩展 | CC 没有 GA 同名机制 | `executionContract` 可选；显式声明时才 fail-closed |
| 云端会话 | `isolation: remote` 本 build 抛错 | 【复刻时暂时不需要参考 claudecode 云端会话的实现】 |

---

## 2. CC Agent Team 源码证据

### 2.1 Agent 工具与隐式 team

源码：`claudecode283_hack/reconstructed/deminified-agent-team/m0511_0xcd25473.js`

- `team_name` 的 schema 描述是 `Deprecated; ignored. The session has a single implicit team.`，见约 330 行。
- `name` 是 teammate 的可寻址身份；`name` 规则禁止保留名。
- 带 `name` 且满足条件时走 `spawnTeammate`，返回 `status: "teammate_spawned"`，见约 458-481 行。
- CC 不要求先 `task_create` 才允许 spawn teammate。

### 2.2 spawnTeammate 的身份预留

源码：`claudecode283_hack/reconstructed/deminified-agent-team/m1578_0xdd0c821.js`

- `reserveTeammateIdentity` 在 team 文件上做 CAS 更新，见约 526-546 行。
- 预留内容包括 `agentId`、`name`、`color`、`joinedAt`、`tmuxPaneId`、`subscriptions`。
- 启动失败且尚未 commit 时回滚；已 commit 后失败则保留成员条目，见约 546-563 行。
- 保留名：`main`、`team-lead`、`user`、`system`（任意拼写）以及 agent-id 形状，见约 576-577 行。
- 重名会自动追加 `name-2`、`name-3`，见约 579-588 行。

这意味着 CC 的“唯一身份”是**先写 team 文件的 CAS 预留**，不是靠模型自己避免重名。

### 2.3 TaskCreate / TaskUpdate / TaskList

源码：`claudecode283_hack/reconstructed/deminified/m0531_0xcd4d7c8.js`

- TaskCreate：
  - 新任务 `status: "pending"`、`owner: undefined`、`blocks: []`、`blockedBy: []`；
  - prompt 明确 `Check TaskList first to avoid creating duplicate tasks`，见约 4632 行。
- TaskUpdate：
  - `isConcurrencySafe() { return true }`；
  - 当 `status === "in_progress"` 且没有显式 owner、且原任务无 owner 时：
    ```js
    let currentAgent = cg()
    if (currentAgent) {
      O.owner = currentAgent
      V.push("owner")
    }
    ```
    见约 5000-5140 行。
  - owner 改变后构造 `task_assignment` 消息并投递给 owner。
  - 任务 completed 后返回提示：`Task completed. Call TaskList now to find your next available task...`，见约 5140 行。
- TaskList：
  - 返回 `id, subject, status, owner, blockedBy`；
  - `blockedBy` 只保留未完成 blocker；
  - teammate 工作流文本见约 5157-5175 行：
    1. 完成当前任务后调用 TaskList；
    2. 找 pending、无 owner、空 blockedBy；
    3. 优先最小 ID；
    4. 用 TaskUpdate 认领；
    5. 被阻塞则通知 lead。

### 2.4 Team context 注入

源码：`claudecode283_hack/claudecode283/modules/m0357_0xc63ba04.js`（约 3722-3732 行）

- `team_context` 携带 `agentId`、`agentName`、`teamName`、`teamConfigPath`、`taskListPath`、`hasTaskListTools`。
- 注入文本包括：
  - 你是 teammate；
  - team lead 名字是 `team-lead`；
  - 读 team config 发现其他成员；
  - 周期性检查 task list；
  - 有新工作就创建任务，完成就标记 resolved；
  - 必须用成员名字称呼 active teammate。

### 2.5 CC 没有的规则

以下内容不能写成“CC 源码事实”：

- CC 没有 `team_mode` 参数；CC 是 Agent 工具传 `name` 进入 teammate 分支。
- CC 没有“无 task board 就禁止 spawn”的硬门禁。
- CC 没有 GA 的 `executionContract`。
- CC 的 `team_name` 现在是 deprecated/ignored，不是必填。
- CC 的 remote/cloud agent 在本 build 不可达；【复刻时暂时不需要参考 claudecode 云端会话的实现】。

---

## 3. GA 实测失败链

证据：

- `D:\git_codes\GenericAgent\temp\sessions\session_e4f335d9eb2f4f6e9b35db4d7f8df2a7.jsonl`
- `D:\git_codes\GenericAgent\temp\sessions\session_e4f335d9eb2f4f6e9b35db4d7f8df2a7\subagents\run_000218.jsonl`
- `D:\git_codes\GenericAgent\temp\team_e2e_1792393179791\summary.json`
- `D:\git_codes\GenericAgent\temp\teams\session-e4f335d9\tasks.json`

可复现事实：

1. 第一波 spawn 参数包含 `agent_type: "explore"` 和 `team_mode: true`。
2. child 权限为 `read_only`；`task_update` 与 `send_message` 均被拒绝，原因是 `read_only_static_unknown`。
3. child 输出 BLOCKED，但进程 turn_status 仍是 completed。
4. `genericagent/core/app.py::_teammate_idle_hook()` 看到 completed 后调用 `complete_owned_in_progress()`，把任务 #1/#2/#3 误标 completed。
5. lead 关闭 child 后，`close_agent` 只能 reclaim 未完成任务；已误标 completed 的任务不会 reclaim。
6. lead 尝试 `owner=""` 释放失败；当前 `do_task_update` 把空字符串当作 owner 值，触发 `already_claimed`。
7. lead 删除 #1/#2/#3，创建 #4/#5/#6，第二波 generic teammate 成功。
8. 测试只断言 `teamChildren.length >= 2`，因此 6 个 child 仍 PASS，掩盖了重复启动和第一波无效开销。

### 3.1 根因分层

| 层 | 根因 | 影响 |
| --- | --- | --- |
| 权限层 | read_only 的静态白名单没有 team coordination tools | teammate 无法更新任务/通知 lead |
| 生命周期层 | turn completed 被当作 task completed | 失败任务被标成功，owner 无法释放 |
| 参数层 | `owner=""` 没有归一化为释放 | lead 无法手工回收任务 |
| 角色层 | explore 的只读能力与写/执行任务不匹配 | 第一波必然失败 |
| 编排层 | team_mode spawn 没有绑定 task id / 没有任务板事实校验 | 可以用 v2 名字重开，重复消耗 |
| 测试层 | 只断言“至少 N 个 teammate”，不检查是否只执行一次、任务是否真实完成 | 回归不可见 |

---

## 4. 借鉴思路

### 4.1 直接借鉴 CC 的部分

1. **身份唯一与失败回滚**：GA 已有 `SubagentRegistry` 的 live-name conflict 和 worktree 生命周期；继续保留，不改成“自动 v2 名字重开”。
2. **任务板是事实源**：TaskCreate / TaskUpdate / TaskList 的字段和状态语义继续对齐 CC。
3. **TaskUpdate 自动 owner**：设置 `in_progress` 时，如果任务无 owner，自动写当前 agent path。
4. **完成提示**：TaskUpdate completed 后提示 teammate 调 TaskList 找下一项。
5. **team context**：teammate 的 prompt 必须包含身份、lead、task board 和“完成当前任务后找下一项”的规则。
6. **协调工具不受只读误杀**：`send_message`、`task_*`、`team_status` 是 control-plane，不是 workspace mutation。
7. **角色能力与任务需求匹配**：read-only 角色可以参与只读研究，但不能被派去写文件/执行代码。

### 4.2 GA 必须额外做的硬化（不是 CC 原样）

1. **team_mode 必须有可认领任务**
   - CC 允许 unbound teammate，因为 CC 模型通常能自己按 team context 找任务；
   - GA 的 deepseek-flash 实测会先 spawn、再补任务板，或用 v2 名字重开；
   - 因此 GA 在 `team_mode=true` 时要求绑定 claimable task（显式 `team_task_id` 或按任务名匹配），没有则返回结构化错误，不启动进程。

2. **task_create 去重**
   - CC 用 prompt 提醒 `Check TaskList first`；
   - GA 再在 store 层做 normalized subject 去重，防止同一任务被创建成 #4/#5/#6。

3. **spawn 前 role capability preflight**
   - 如果 role profile 是 `read_only` / `verify`，而任务描述明确要求写文件或执行命令，直接拒绝 spawn；
   - 错误要告诉模型“省略 agent_type 或换 write-capable role”，不要让 child 启动后才 BLOCKED。

4. **turn completed 不再自动完成 task**
   - CC 依赖 teammate 自己调用 TaskUpdate；
   - GA 应移除 idle hook 的自动完成 fallback，或只在有明确成功证据时完成；
   - 关闭 child 时把未完成任务 reclaim 为 pending，让 lead 可以安全重派同一任务。

5. **owner 释放语义**
   - `owner=""` 在 handler 归一化为 `None`；
   - teammate 不能把别人 owner 的任务直接改状态；lead 可以管理任务板。

6. **测试防回归**
   - 断言 team_mode child 数量等于任务数，而不是“至少 N”；
   - 断言每个 child 绑定唯一 task id；
   - 断言第一波失败时没有产生重复 OS 进程；
   - 断言 task board 最终没有重复 subject；
   - 断言 read_only teammate 仍可 task_update/send_message。

---

## 5. 不照抄 / 暂不做的部分

- 不实现 CC 的 remote/cloud agent；【复刻时暂时不需要参考 claudecode 云端会话的实现】。
- 不重新引入 JSON planner；workflow 仍是 direct JS。
- 不把 CC 的 pane/tmux/iTerm2 teammate 后端搬进 GA；GA 的 in-process/process 模型已经够用。
- 不把 `team_name` 变成 GA 必填；GA 继续使用单一隐式 team。
- 不把 `executionContract` 强加给所有 workflow；它只在显式声明时 fail-closed。

---

## 6. 证据索引

### CC

- `claudecode283_hack/reconstructed/full/m0869_0xd12567a.js`
- `claudecode283_hack/reconstructed/full/m0227_0xc482590.js`
- `claudecode283_hack/reconstructed/deminified-agent-team/m0511_0xcd25473.js`
- `claudecode283_hack/reconstructed/deminified-agent-team/m1578_0xdd0c821.js`
- `claudecode283_hack/reconstructed/deminified/m0531_0xcd4d7c8.js`
- `claudecode283_hack/claudecode283/modules/m0357_0xc63ba04.js`
- `claudecode283_hack/claudecode283/extracted/dynamic-workflow-internals.md`
- `claudecode283_hack/claudecode283/extracted/workflow-authoring-reference.md`
- `claudecode283_hack/claudecode283/extracted/agent-team-internals.md`

### GA

- `genericagent/core/handler.py`
- `genericagent/core/app.py`
- `genericagent/agents/tasks.py`
- `genericagent/agents/permissions.py`
- `genericagent/workflow/permissions.py`
- `genericagent/agents/manager.py`
- `frontends/ink/ui/scripts/real_ink_team_swarm_e2e.ts`
- `frontends/ink/ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts`

---

## 7. GA 真实 E2E 验证（2026-10-19）

调研结论已经落到 GA 并用默认 `deepseek-flash` 做过真实 Ink UI E2E，避免把“源码看起来合理”误当成“GA 已经稳定”。

### 7.1 Workflow contract

- runId：`wf_dfb92d598fca4639960544a892b41aa7`
- 结果：`succeeded`，6 个 transcript job，UI 回到 idle。
- E2E 的 prompt 没有显式要求 GA 的 `executionContract`，因此验收走 `actual-evidence`：从 persisted transcript 读取真实 `tool_call`，并验收实际产物；没有把缺失的可选 host contract 误报为 workflow 失败。
- 真实证据包含 `mcp__fetch__fetch_markdown`、`file_read`、`code_run`、`file_write`、`structured_output`；最终 HTML 产物位于 workflow 隔离工作区，`38,340` bytes。
- 这与 CC 的机制一致：需要结构时用 `agent(..., {schema})` + `StructuredOutput`；质量由 verify/judge 方法论保证；GA 的 execution contract 只是可选扩展。

### 7.2 Agent Team duplicate spawn

- runId：`1792405812532`
- task board：`session-3d9bd493`，3 个任务全部 completed。
- 只有 3 个 team_mode children，分别绑定 task `1`、`2`、`3`；task id 唯一，subject 无重复。
- 没有出现第一波只读角色失败后，lead 删除任务并重开 #4/#5/#6 的 6-child 现象。
- `/team` 仍能返回共享任务板；active member 为 0 是因为 lead 已关闭 teammate，不是任务板状态丢失。
- lead 真实写出 `report.md`（`2,694` bytes），说明 task board、消息通知、结果汇总和 UI 面板形成了闭环。

### 7.3 验证边界

真实 E2E 只使用默认 `deepseek-flash`；不依赖固定期望文本，而依赖事件、transcript、任务板、真实产物和 UI 帧。CC remote/cloud agent 仍不在复刻范围；【复刻时暂时不需要参考 claudecode 云端会话的实现】。
