# GA workflow_list 轻量 payload —— 对齐 CC m1906 历史列表（2026-10-14）

> 上游遗留项：`docs/20261014-ga-workflow-异步background-task-实施记录.md` §4.4 / §9
> （「`workflow_list` 仍 `load_run` 全部 run（439 KB，应做轻量列表 payload）」）。
> CC 证据仓（只读）：`D:\git_codes\claudecode283_hack`。
> **云端不复刻：复刻时暂时不需要参考 claudecode 云端会话的实现。**

---

## 1. 问题

`/workflows` 列表（`workflow_list`）对每个 run 发**完整**的 `_workflow_run_payload`，
而列表行其实只需要极少数字段。全仓 108 个 run 的 list payload ≈ **4.5 MB**，
其中绝大部分是列表用不到的 job metadata / artifact 路径 / 结果引用 / 错误等。

---

## 2. CC 的做法（证据）

### 2.1 列表读的是「小快照」，不是完整 run 记录

`dynamic-workflow-internals.md` §8.6（源 `m0571_0xcddb07e.js`）：

- `G7r()`（@1280）：把
  `{runId, timestamp, result, agentCount, logs, workflowProgress, totalTokens, totalToolCalls, status, ...}`
  写成 `<projectsDir>/<sessionId>/workflows/<runId>.json` **快照**。
- `psr()`（@2422）：读回全部 `.json` 快照，按 `startTime` **倒序**，供 `/workflows` 列表展示。

### 2.2 列表行字段（`m1906_0xe19cc02.js`）

```js
{
  label: t.workflowName ?? t.summary ?? t.description,
  icon:  Bt[t.status],
  meta: [
    Zt(v),                                   // 时间
    t.agentCount > 0 ? `${t.agentCount} ${I(t.agentCount,"agent")}` : "",
    i > 0 ? `${cr(i)} tok` : "",             // i = totalTokens
  ],
}
```

即列表只消费：**名称、状态、时间、agentCount、token 总数**。

---

## 3. GA 改法

`frontends/ink_bridge.py` 新增 `_workflow_run_summary(run)`，`workflow_list` 改用它。
摘要只保留 GA 列表行 + 状态条**真正读取**的字段：

| 字段 | 用途 |
| --- | --- |
| `runId` | 列表选中 / `workflow_detail` 命令 / 状态条 key |
| `sessionId` | TS `WorkflowRun` 类型必需 |
| `status` | 列表图标 + 状态条 |
| `metadata.workflowName` | 列表显示名（`workflowDisplayName`） |
| `metadata.workflowTaskType` | 显示名回退 |
| `metadata.integrationStatus` | 状态条 `integration …` |
| `metadata.finalAuditStatus` | 状态条 `audit …` |
| `metadata.launchedBy` | 列表 smoke 报告用（小字符串） |
| `jobs[].jobId` / `jobs[].status` | `run.jobs.length`（agentCount）+ 状态条回退 `progressFromJobs` |
| `jobs[].metadata.label` | 状态条 `activeAgent` |
| `jobs[].metadata.tokenUsage` | 状态条 token 汇总 |

**丢弃**：`script` / `jobs[].prompt` / `artifactDir` / `permissionProfile` /
`permissionPolicyVersion` / `resultRef` / `error` / 其它 job metadata（`model`、`phase`、`callIndex` …）。

面板**无需改动**：`workflowListRunRow` 与 `workflowStatusBarFromState` 只用上面的字段；
打开详情时 `workflow_detail` 事件会带回完整 run，覆盖 `state.workflows[runId]`。

---

## 4. 实测

`temp/sessions` 下 108 个 run：

| | 字节 |
| --- | --- |
| 改前（完整 `_workflow_run_payload` × 108） | 4,512,030 |
| 改后（`_workflow_run_summary` × 108） | **95,374** |
| 降幅 | **−97.9%** |

最大 session 的单 run：232,244 B → **2,374 B**。

---

## 5. 测试

- 新增 `tests/test_ink_bridge.py::test_workflow_list_payload_is_lightweight`：
  断言摘要字段集恰为预期、不含 `prompt`/`script`/`model`，且单个 run 序列化 < 600 B。
- `tests.test_ink_bridge + tests.test_workflow_store + tests.test_agent_control_workflow` → **163 OK**。
- `frontends/ink-ui`：`workflowList.app.test.ts` + `workflowStatusBar.test.ts` → **16 pass**。

---

## 6. 遗留 / 后续（未实施）

- 事件 → 帧节流（对齐 Codex `schedule_frame()`）。
- 子 agent 协作冲突（CC 靠 worktree 隔离 + 值传递）。
