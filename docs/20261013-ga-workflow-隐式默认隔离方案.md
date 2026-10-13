# GA workflow 隐式默认隔离方案（对齐 CC「并行/后台会话强制先隔离」）

> 目标读者：后续实施者（Codex / 人）。本文件先落盘事实与设计，再据此实施。
> 上游证据仓库：`D:\git_codes\claudecode283_hack`（Claude Code 2.1.283）。

---

## 0. 一句话

把 GA 的 `isolation` 从「作者显式声明才开」升级为「**并发写者默认就开**」，
并给出显式逃生口 `isolation:"none"`——对齐 CC「对并行/后台会话强制先隔离」的默认姿态。

---

## 1. 概念澄清：两种「隔离」不是一回事

| | 管什么 | 本轮 |
| --- | --- | --- |
| **隐式默认隔离** | 隔离**何时**生效 | ✅ 做 |
| **OS 级隔离** | 隔离**靠什么**强制 | ❌ 不做（另案） |

### 1.1 隐式默认隔离（implicit default isolation）

- **定义**：作者不写 `isolation` 时，GA 在**并行写者**场景自动加隔离，而不是默默走共享 workspace。
- **CC 对照**（`D:\git_codes\claudecode283_hack`）：
  - `reconstructed/deminified/m0357_0xc63ba04.js:34126-34127`：未隔离的后台会话去改共享 checkout 会被**直接拒绝**，
    报 *"This background session hasn't isolated its changes yet. Call … first so edits land in a worktree instead of the shared checkout"*，
    并提示可用 `"worktree": {"bgIsolation": "none"}` 关掉该 guard。
  - `reconstructed/deminified/m0357_0xc63ba04.js:64417`（EnterWorktree 提示词）：
    *"This is enforced: file edits in the shared checkout are rejected until you isolate, so call EnterWorktree before your first edit…"*。
  - `reconstructed/deminified/m0464_0xcc31217.js:1864`：`isolation: … === "worktree" && source !== "built-in" ? "worktree" : "none"`（内置 agent 不隔离）。
- **关键结论**：CC 不是给所有 agent 无脑建 worktree，而是「**对并行/后台会话强制先隔离**」。
  GA 的类比 = **并发写者默认隔离**（而不是「一律隔离」，也不是「作者自觉」）。

### 1.2 OS 级隔离（OS-level isolation，本轮不做）

- **定义**：隔离由 OS/容器**内核**强制，而不是 GA 进程内 Python 代码「讲道理」。
- **CC 对照**：sandbox-runtime（macOS seatbelt / Linux-WSL bubblewrap / Windows 需 Git Bash；
  `m0357:134643` → `" (Linux bubblewrap)"`，`m0357:40474` → *"Sandboxed bash on Windows requires Git Bash…"*）
  + `filesystem.deny/mask` + `network.allowedDomains/strictAllowlist` + 凭据遮蔽（`mask`+`extract`/`decode:jwt`）
  + 实验性 TLS 终止（`m0052_0xbfaae5d.js` 的 schema）。
- **GA 现状**：`workflow_workspace_guard` 是进程内 monkeypatch（拦 `subprocess.Popen`/`os.system`/`os.popen`），
  **是尽力而为的引导，不是安全边界**（见 `docs/20261013-ga-workflow-受控subprocess-实施记录-阶段4.md` §安全取舍）。
- **结论**：OS 级隔离是另一个量级的工程，主要提供**安全**属性而非「复杂任务能力」；本轮不做。

---

## 2. GA 现状与实现落点（事实）

### 2.1 并发是怎么发生的（DSL → RPC）

- `workflow_js_worker.js` 的沙箱 `agent: (prompt, options) => rpc('agent', {...})`；`rpc()` **只发不收**（emit 一行 + 返回 Promise）。
- `parallel(items)`：`await Promise.allSettled(items.map(item => item()))`——
  `items.map(cb)` **同步**对每个 item 调用 `item()`，`item()` 又**同步**调用 `agent()` 发 RPC。
  所以**整批 `agent()` RPC 在任何一个 await 之前就已全部发出**。
- `pipeline(items, ...stages)`：同样是 `Promise.allSettled(items.map(async ...))`，第 0 阶段并发。

### 2.2 竞态事实（为什么不能靠「瞬时 wave 兄弟数」）

- host 主循环（`workflow_runtime.py:277`）是：
  `for completed in scheduler.tick() {...}`（**起跑 queued job**） → `message = self._next_message(...)`（**读一条**）。
- 于是：读到 `rpcA` 并注册 A 后，下一轮 `tick()` 就把 A 起跑了；此时 `rpcB` 还没被读入 → `_wave_sibling_count` 只看到 1。
  结论：**在起跑时刻用 wave 兄弟数判定并发是不可靠的**（会漏判先起跑的那个）。
- 因此本方案**不**用 wave 兄弟数做并发信号，改用 worker 显式携带的 `parallel` 批次大小（确定性）。

### 2.3 判定所需的既有设施

- `workflow_scheduler.register_agent`（@260）：设 `metadata["options"]/["wave"]/["cacheKey"]`。
- `workflow_scheduler._start_queued_jobs`（@524）：起跑点（`job.status="running"` → `_prepare_declared_workspace` → `runner.start`）。
- `workflow_scheduler._declared_write_paths`（@1338）：读 `writeScope`/`deliverables`。
- `workflow_child_agent._job_isolation`（@~1100）：读 `job.metadata["options"]["isolation"]`；
  `_child_cwd` 为 `"worktree"` 时返回私有目录，diff/回收/清理全链路已就绪（阶段 1-3 + 隔离目录清理）。
- `workflow_runtime` agent RPC（@476）：`isolation` 白名单目前只放行 `"worktree"`，其余（含 `"remote"`）抛错。
- `config.max_concurrent`：并发槽位上限。

---

## 3. 设计

### 3.1 规则（GA 隐式默认隔离）

一个 job 被**隐式隔离**（注入 `isolation:"worktree"`）当且仅当**全部**满足：

1. 作者**未声明** `isolation`（省略）。`isolation:"worktree"` = 显式开；`isolation:"none"` = 显式关。
2. 该 job 是**写者**：声明了非空 `writeScope` 或 `deliverables`，**或** `capabilities` 含 `file_write`/`execute`，
   **或** `requiredTools` 含 mutating/execute 工具。
3. 该 job 属于**并发批次**：来自 `parallel()`/`pipeline()` 且批大小 `>= 2`（worker 的 `parallel` 提示）。
4. `config.max_concurrent > 1`（串行执行下隔离无意义）。

满足则：把 `options["isolation"]` 设为 `"worktree"`，记 `metadata["isolationReason"]="implicit-parallel-writer"`，
并落一条 `agent_isolated` 事件（可审计，不静默）。

### 3.2 逃生口

- `isolation:"none"`：显式声明「我不用隔离，共享 workspace 即可」——对齐 CC 的 `bgIsolation:"none"`。
  runtime 白名单从 `{worktree}` 扩到 `{worktree, none}`；`none` 归一化后不触发私有目录。

### 3.3 与既有机制的关系

- 与「隔离目录清理」正交：隐式隔离产生的私有目录同样按 `GA_WORKFLOW_ISOLATION_KEEP` / TTL 处理。
- 与「归属置信度」正交：隔离后 `artifactConfidence` 自然为 `exact`。
- 不改变显式 `isolation:"worktree"` 的行为。

---

## 4. 实施步骤

1. `workflow_js_worker.js`：模块级 `let parallelGroup = null;`；沙箱 `agent` 带 `parallel: parallelGroup`；
   `parallel`/`pipeline` 在 `items.map(...)` 前设 `parallelGroup = items.length`、`finally` 复原。
2. `workflow_runtime.py`：agent RPC 读 `params.get("parallel")`，透传 `parallel_batch`；`isolation` 白名单加 `"none"`。
3. `workflow_scheduler.py`：`register_agent`/`register_cached_agent` 增参 `parallel_batch=None` → `metadata["parallelBatch"]`；
   新增 `_job_declares_writes(options)`；新增 `_apply_implicit_isolation(job)`，在 `_start_queued_jobs` 起跑前调用。
4. `workflow_tool_profiles.py`：如需，导出 `declares_writes(options)` 供调度复用（单一实现）。
5. `skills/workflow-authoring/SKILL.md`：文档化隐式默认隔离 + `isolation:"none"`。
6. 测试 `tests/test_workflow_implicit_isolation.py`。
7. 更新 `AGENTS.md` / 实施记录；跑 focused + 全量 `unittest discover`；提交。

---

## 5. 测试计划

- 并行写者（`parallelBatch=2` + writeScope）未声明 isolation → 起跑即 `isolation:"worktree"` + `isolationReason` + `agent_isolated` 事件。
- 并行**非写者**（只读/研究）→ 不隔离。
- `parallelBatch=1`（串行）→ 不隔离。
- 显式 `isolation:"none"` → 不隔离（即使并行写者）。
- 显式 `isolation:"worktree"` → 保持隔离，`isolationReason` 为 declared。
- `max_concurrent=1` → 不隔离。
- runtime：`isolation:"none"` 被接受；`isolation:"remote"` 仍抛错。
- worker：`parallel` 提示随 `parallel()` 发出、`pipeline()` 亦发。

---

## 6. 风险 / 不做

- **不做**：靠瞬时 wave 兄弟数判定（§2.2 竞态）。
- **不做**：raw `Promise.all([agent(), agent()])` 的覆盖（DSL 契约用 `parallel()`/`pipeline()`；可在后续按需扩展）。
- **不做**：OS 级隔离（§1.2）。
- 风险：`metadata["options"]` 在注册后被改写 → 需确认 `cacheKey` 在注册时已定、resume 判定确定性不受影响（隐式决策本身确定）。

---

## 7. 实施状态（2026-10-13）

**已实施。** 落点与 §4 一致：
- `workflow_js_worker.js`（`parallelGroup` → `parallel` 提示）、`workflow_runtime.py`（透传 + 白名单加 `none`）、
  `workflow_scheduler.py`（`parallelBatch` + `_apply_implicit_isolation` + `agent_isolated` 事件）、
  `workflow_tool_profiles.py`（`declares_writes`）、`skills/workflow-authoring/SKILL.md`。
- 测试：`tests/test_workflow_implicit_isolation.py`（13 例，含 2 例真跑 node worker 的端到端）。
- 全量 `unittest discover`：见提交信息。
