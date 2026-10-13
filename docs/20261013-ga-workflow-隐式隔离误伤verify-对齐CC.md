# GA workflow：隐式隔离误伤 verify（对齐 CC「只隔离写者」）

- 日期：2026-10-13
- 触发：用户实测「最近一次 GA workflow 测试一直卡住，verify:html_time 环节前面有 `!`」
- 真实 run：`wf_a56df8bc24af434090851d96d31745d1`（`session_419bf66b10924881a5223e66c4bc71e8`）
- 状态：**已定位根因并修复**

---

## 1. 现象

- 5 路 author + 5 路 verify（`pipeline`），跑完后 run 状态 `degraded`。
- ink workflow 面板里 `verify:html_time` 前有 `!`。
- 观感「卡住」：verify 子代理长时间不出结果。

### `!` 是什么

`frontends/ink-ui/src/workflowPanel.ts:226`：

```ts
if (status === 'degraded' || status === 'partial') return '!'
```

即 **`!` = 该 job 状态为 `degraded`**（非致命失败/降级）。本 run 里
`verify:html_time` = `agent_7`，`status=degraded`（`run.json` jobs 实测）。

---

## 2. 根因：隐式默认隔离把「验证者」也当成了写者

`run.json` 里 **10 个 job 全部** 被标记：

```
isolation = "worktree"
isolationReason = "implicit-parallel-writer"
```

包括 5 个 **verify** job（`capabilities: ["file_read","execute"]`、`requiredTools: ["code_run","file_read"]`、
无 `writeScope`/`deliverables`）——它们本不该被隔离。

链路：

1. `workflow_scheduler._apply_implicit_isolation` 用 `declares_writes(options)` 判定「是不是写者」。
2. 旧 `declares_writes` 的写者集合是 `WRITE_CAPABILITIES = {"file_write", "execute"}` —— **`execute` 被算作写能力**。
3. verify job 声明了 `execute`（+`code_run`）⇒ 被判为写者 ⇒ 满足「并行批次 ≥2 ∧ max_concurrent>1」⇒ 被隐式隔离。
4. 隔离 = 子代理 cwd 落到**私有空目录** `temp/workflow_child_agents/<runId>/<jobId>/`（`workflow_child_agent._isolated_workspace`），
   **不含** 共享 run workspace 里 author 产出的 `wf_outputs/*.py`。
5. verify 于是「看不见被验文件」→ 满盘 `os.walk` 找文件（agent_7 烧了 9+ 轮）→ 超时/降级 → 整个 run `degraded`。

### 直接证据（agent_7 transcript 自述）

> Verification ran on a byte-identical copy … since **the original artifact was outside my isolated workspace**;
> the file was not edited.

即：验证者确实在自己的私有工作区里，看不到 author 的产物。

---

## 3. 与 Claude Code 的对照

- CC 的 `isolation: 'worktree'` 是给**写者**用的：并行写者各拿一个 worktree，避免互相覆盖，改动再合并回主工作区。
- **读/验证者不隔离**：它读的是（已合并回的）共享工作区产物。
- GA 的私有工作区是**空目录**（不像 CC 的 git worktree 是整棵仓库检出），所以「隔离一个读者」= 直接致盲。
- 结论：隐式隔离的触发条件必须是**显式文件写面**，`execute`/`code_run` 单独出现不算写者。

---

## 4. 修复

`workflow_tool_profiles.py`：

- 拆分能力集合：
  - `MUTATING_CAPABILITIES = {"file_write", "execute"}`（可能改写，用于归属/attribution）
  - `FILE_WRITE_CAPABILITIES = {"file_write"}`（**可隔离写者**的判定集）
- `declares_writes(options)` 改用 `FILE_WRITE_CAPABILITIES`：只有
  `writeScope` / `deliverables` / `file_write` 能力 / `file_write` 类工具才算写者；
  **裸 `execute` / `code_run` 不再触发隔离**。

效果（同形脚本实测）：

```
author:a   isolation=worktree   reason=implicit-parallel-writer
author:b   isolation=worktree   reason=implicit-parallel-writer
verify:a   isolation=None       reason=None
verify:b   isolation=None       reason=None
```

验证者回到共享 run workspace，能直接读到 author 的产物。

---

## 5. 测试

- `tests/test_workflow_implicit_isolation.py`
  - 改：`execute`/`code_run` 单独出现 ⇒ **非写者**（`test_bare_execute_is_not_a_writer`）。
  - 新增：`test_parallel_verifier_is_not_isolated`（scheduler 级，verify 形 job 不隔离）。
- 全量 discover：**1381 OK (skipped=3)**。

---

## 6. 遗留 / 后续（未做）

- **隔离工作区是空目录**：CC 的 worktree 是仓库检出，GA 是空目录。若某写者需要读已有文件，隔离后会读不到。
  目前靠「只有显式写者才隔离」规避；更彻底的做法是「隔离目录先播种共享工作区内容」（另案）。
- 【复刻时暂时不需要参考 claudecode 云端会话的实现】
