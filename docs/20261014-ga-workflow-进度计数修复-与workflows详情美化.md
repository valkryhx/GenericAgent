# GA workflow 进度计数修复 + /workflows 详情美化

- 日期：2026-10-14
- 状态：**已实施，focused 测试通过**（前端 410/410；Python ink_bridge + runtime 233）
- 触发：用户实测「6 个 agent 都执行完毕，但输入框上方状态仍显示 3/6」，并要求美化 `/workflows` 详情（`|` 前的列宽对齐）。

---

## 1. 缺陷：X/Y 计数不准（Y 对但 completed 偏小）

### 1.1 现象

- run 已 `succeeded`、6/6 子代理成功（主代理自报、`run.json` 也是 6/6），
  但 Ink 输入框上方的 workflow 状态条仍显示 **`3/6 agents done`**。
- 用户反馈「这个 X/Y 之前也有 Y 值不准的问题」。

### 1.2 证据（真实 run 数据）

抽查 `temp/sessions/*/workflows/*/workflow-progress.json` 与 `run.json`，**磁盘上的进度快照是对的**：

| runId | status | progress jobs | done | states |
| --- | --- | --- | --- | --- |
| `wf_66e2eaacef86454082367a902d5d16fa` | degraded | 6 | 6 | cached×3, succeeded×2, degraded×1 |
| `wf_39565d7763324de6b829b554d8118755` | succeeded | 6 | 6 | succeeded×6 |

→ 磁盘 6/6，说明**不是后端算错，而是 UI 显示了陈旧快照**。

### 1.3 根因（两条，都会导致 UI 卡在旧计数）

**根因 A（前端信错来源）**：`frontends/ink-ui/src/workflowStatusBar.ts` 的状态条：

```js
const progressEntries = state.workflowDetails[run.runId]?.progress?.workflowProgress ?? []
const agents = progressEntries.length > 0 ? progressEntries : progressFromJobs(run.jobs ?? [])
```

它**优先信任** `workflow-progress.json` 的**轮询快照**，而不是 run 记录里的 `jobs`。而这个快照可能停在
run 收敛前的一拍（例如 3/6）；run 的权威 `jobs`（由终态 `workflow_run` 事件带回，已是 6/6）被忽略。

**根因 B（后端 emit 竞态）**：`frontends/ink_bridge.py` 的 `_run_workflow_runtime`：

```python
watch_stop.set()          # 只“请求” watcher 停止
...
self.emit({"type": "workflow_run", ...})       # 终态 run（6/6）
self.workflow_progress(run_id)                 # 终态进度（6/6）
```

`watch_stop.set()` **不等待** watcher。`_watch_workflow_progress` 若已越过 `stop.wait()`、正读到**旧**快照，
它会在主线程发完终态事件**之后**再 emit 一次**旧**快照（3/6），把 UI 覆盖回旧值。

### 1.4 修复

- **后端（根因 B）**：`ink_bridge.py` 新增 `_stop_watcher()`——`watch_stop.set()` 后 `watcher.join(timeout=2.0)`，
  让 watcher 的最后一次 emit **严格早于**下面的终态 emit。三处 `watch_stop.set()` 全改调它。
- **前端（根因 A，双保险）**：`workflowStatusBar.ts` 新增 `mergeAgentStates(progressEntries, run.jobs)`——
  按 `jobId` 合并两个来源，**取更靠后的状态**（settled 秩 3 > running 2 > queued 1 > registered 0），
  计数天然单调，不再回退。`workflowPanel.ts` 的 `overviewAgentFromProgress` 同法（`moreSettledStatus`）。

> 效果：无论快照新旧，只要 run 记录里某 job 已 settled，计数就不会低于它 —— 「6/6 显示 3/6」不再可能。

---

## 2. /workflows 详情美化（列对齐）

### 2.1 问题

`workflowOverviewRows` 原来是：

```js
rows.push(`${phase ? phaseRow(phase, index === overview.selectedPhase) : ''.padEnd(16)} | ${...}`)
```

- **只有空单元格**才 `padEnd(16)`；非空的 phase 行**完全不补**，标题一长 `|` 就错位：
  ```
  › ✓ Inspect 2/2 | ✓ planner-api ...
    ✓ Matrix 1/1 | ✓ real-e2e-patterns ...
  ```
- phase 行恒显 `✓`，即使该 phase 还没跑完（`0/1` 也画 `✓`），语义矛盾。

### 2.2 改法（对齐 CC 进度树的「分组 + 对齐」观感）

`workflowPanel.ts` `workflowOverviewRows`：

1. **列宽按内容算**：`phaseColumnWidth = max(16, width('Phases'), 所有 phase 行宽)`，
   用 `string-width`（CJK 宽度正确）补空格 → `│` 在所有行**严格对齐**。
2. 分隔符 `|` → `│`（单格制表符），更像进度树。
3. phase 状态字形：整组完成 `✓`，未完成 `◌`（与 agent 状态图标一致）。

改后：

```
prompt-guided-planner-real-e2e-design  3/3 agents · done
Design sufficient real GPT-5.5 E2E coverage ...
Phases           │ Inspect · 2 agents
› ✓ Inspect 2/2  │ ✓ planner-api  24.9k tok · 16 tools
  ✓ Matrix 1/1   │ ✓ real-e2e-patterns  45k tok · 24 tools
Enter agent · j/k phase · PgUp/PgDn · Esc back
```

（CC 的详情是三层 scope `phases→agents→agent` + tab 切换、转录按需读盘；GA 保持自己的两栏总览，
只对齐/美化排版，不引入 CC 的三层导航——那属于另一项能力。）

---

## 3. 改动文件

| 文件 | 改动 |
| --- | --- |
| `frontends/ink_bridge.py` | `_stop_watcher()`：join watcher，杜绝陈旧进度 emit 晚于终态 emit |
| `frontends/ink-ui/src/workflowStatusBar.ts` | `mergeAgentStates` 合并 progress 快照与 run.jobs（取更 settled） |
| `frontends/ink-ui/src/workflowPanel.ts` | `moreSettledStatus`；列宽按内容算 + `│` + phase 字形 `✓/◌` |
| `frontends/ink-ui/src/workflowStatusBar.test.ts` | 新增 2 例（陈旧快照 6/6、run 记录滞后时取新进度）；更新 1 例 active agent |
| `frontends/ink-ui/src/workflowPanel.test.ts` | 更新 3 处对齐后的期望 |
| `frontends/ink-ui/src/workflowList.app.test.ts` | phase 字形 `✓`→`◌`（未完成 phase） |

---

## 4. 测试

- 前端：`npm test` → **410 passed / 0 failed**；`npx tsc --noEmit` → 0。
- 后端：`pytest tests/test_ink_bridge.py tests/test_workflow_runtime.py -q` → **233 passed, 20 subtests**。

---

## 5. 不做

- OS 级隔离（用户明确不做）。
- CC 三层 scope 导航（phases→agents→agent + tab + 转录按需读盘）：属独立能力，本轮只做排版对齐/美化。
- 云端会话：**【复刻时暂时不需要参考 claudecode 云端会话的实现】**。
