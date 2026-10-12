# GenericAgent Dynamic Workflow 实施记录：B2 子 agent 取消语义

> 日期：2026-10-12
> 仓库：`D:\git_codes\GenericAgent`
> 阶段：B2（对应实施方案 G7）
> 上游参考：Claude Code 2.1.283 真实源码（只读）
> 状态：✅ 已完成

---

## 0. 本阶段目标

对齐 Claude Code（下称 CC）Dynamic Workflow 的**取消语义**：

1. 一个被取消的 `agent()` 调用 **永不 settle**（既不 resolve 也不 reject）；
2. 因此 `parallel()` / `pipeline()` **不能**把取消吞成 `null` 后继续跑；
3. 外部对 running 工作流的 `stop` / `kill` / `cancel` 必须**真的停住**脚本，而不是拖到 deadline。

> **边界**：本阶段只复刻本地 workflow 的取消语义。
> **【复刻时暂时不需要参考 claudecode 云端会话的实现】**：`agent({isolation:'remote'})` 在本 build 直接抛
> `Error("agent({isolation:'remote'}) is not available in this build")`（`m0869 @2260`），`Ho()` / `Uo` / `WP()`
> 那条远程路径是**不可达死代码**，GA 不复刻、也不需要复刻。

---

## 1. CC 源码证据

主模块：`claudecode283\modules\m0869_0xd12567a.js`（本仓库 `reconstructed/full/` 与 `reconstructed/deminified/` 均可读）。

| CC 符号 | 位置 | 职责 | 关键代码 |
| --- | --- | --- | --- |
| `jo` | @43713（本仓库格式化文件第 2126 行起） | `agent()` 实现 | 见下 |
| `Ko` | @74546（格式化文件第 3207 行） | `parallel()` | `if (D.abortController?.signal.aborted) return new Promise(() => {});` |
| `zo` | @75362（格式化文件第 3235 行） | `pipeline()` | 同 parallel 的入口守卫 |
| `Io` | @78833（格式化文件第 3419 行） | workflow 主循环 | `Promise.race([scriptResult, abortPromise])` |

### 1.1 `agent()` 的三条取消分支（`jo`）

```js
// ① 启动前已取消 → 永不 settle
if (D.abortController?.signal.aborted) return new Promise(() => {});
...
// ② 限流等待结束后发现已取消 → 仍然永不 settle
if (await Ke(), D.abortController?.signal.aborted) return new Promise(() => {});
// ③ 已经进入 spawn 才被取消 → 抛 "Workflow aborted"
if (await lt.whenClear(), D.abortController?.signal.aborted) throw Error("Workflow aborted");
```

要点：

- **不是所有取消都抛异常**。CC 精确区分"启动前/等待中取消 → 永不 settle"和"spawn 后取消 → 抛 `Workflow aborted`"。
- 永不 settle 的目的是让脚本**停在那个 `await`**，而不是走 catch/失败分支。
- 顶层 `Io` 另外用 `Promise.race` 把整个脚本和 abort 信号赛跑，所以 abort 一旦触发，工作流整体结束。

### 1.2 `parallel()` / `pipeline()` 的失败 vs 取消（`Ko` / `zo`）

```js
let ee = await Promise.allSettled(U.map((pe) => { ... }));
Qe = ee.map((pe, ve) => {
  if (pe.status === "fulfilled") return pe.value.v;
  let { name: Fe, msg: Xe } = MCe(pe.reason);
  if (Fe === "WorkflowBudgetExceededError") return Oe++, null;
  let Me = `parallel[${ve}] failed: ${Xe}`;
  return Se(Me, { log: !0 }), null;   // 普通失败 → null（CC 原生行为，保留）
});
```

要点（**关键区分**）：

- **普通失败** → `allSettled` 捕获 → `null`。这是 CC 原生行为，**不要改**。
- **取消** → 走 §1.1 的永不 settle，`allSettled` 永远不返回 → `parallel()` 永不 resolve。**这才是本阶段要补的语义**。
- 所以"把取消吞成 null"的修复点是**让取消不回帧**，而不是把 `parallel()` 的失败映射改掉。

---

## 2. GA 现状与缺陷（改动前）

| 位置 | 改动前行为 | 问题 |
| --- | --- | --- |
| `workflow_runtime.py::_complete_pending_rpc` | 非 `succeeded/degraded` 的 job 一律回 `ok:false` | 一旦 `cancelled` job 走到这里，worker 侧 `agent()` 会 **reject**，`parallel()`/`pipeline()` 就会把它吞成 `null` 继续跑 |
| `workflow_runtime.py::_raise_if_externally_killed` | 只认 `run.status == "killed"` | `workflow_controller.cancel()` 写的是 `cancelled`，running 工作流**观察不到**，会一直跑到 deadline |
| `workflow_runtime.py::run` 异常分支 | `if current.status == "killed"` 才保留终态 | 外部 `cancel` 会被改写成 `failed`（错误信息还是 `workflow runtime deadline exceeded`） |

> 说明：改动前 `scheduler.tick()` 对 `result.status == "cancelled"` 只调 `_cancel_job()`、**不放进 `completed`**，
> 所以"单条 `await agent()` 被取消"这条路其实已经近似永不 settle（本阶段用回归测试把它锁死）；
> 真正漏的是 **外部 cancel 不被识别** 和 **`_complete_pending_rpc` 缺少显式不变量**。

---

## 3. 改动内容（`workflow_runtime.py`）

### 3.1 `_complete_pending_rpc`：取消不回帧

```python
if rpc_id is None:
    return
if job.status in {"cancelled", "killed"}:
    # CC semantics (m0869 @43713 jo): a cancelled agent() call returns a
    # Promise that never settles. Answering with ok:false would let
    # parallel()/pipeline() swallow the cancellation into null and keep
    # the script running, so the RPC stays pending on purpose.
    return
pending_rpc_jobs.pop(rpc_id, None)
if job.status in {"succeeded", "degraded"}:
    self._send(process, {"type": "rpc_result", "id": rpc_id, "ok": True, "value": ...})
else:
    self._send(process, {"type": "rpc_result", "id": rpc_id, "ok": False, "error": ...})
```

- `cancelled` / `killed` → **不发任何帧**，`pending_rpc_jobs` 里的条目保留，worker 的 Promise 永远 pending。
- `failed` → 仍回 `ok:false`（`parallel`/`pipeline` 映射成 `null`，与 CC 一致）。

### 3.2 `_raise_if_externally_killed` → `_raise_if_externally_stopped`

```python
current = self._safe_load_current_run(run)
if current.status not in {"killed", "cancelled"}:
    return
run.status = current.status
run.error = current.error or ("workflow killed" if current.status == "killed" else "workflow cancelled")
self._cancel_unfinished_jobs(scheduler, reason=run.error)
if process.poll() is None:
    process.kill()
    ...
raise RuntimeError(f"workflow {run.status}: {run.error}")
```

- 现在 `killed` 与 `cancelled` 都算外部停止。
- 调用点在主循环里（`run()` 每轮 tick 之前），所以 running 工作流**下一个 tick 就会观察到**。

### 3.3 `run()` 异常分支：保留 `cancelled` 终态

```python
if current.status in {"killed", "cancelled"}:
    terminal_status = current.status
    run.status = terminal_status
    run.error = current.error or reason
    ...
    self.store.write_final_result(run, self._final_payload(run, terminal_status, ...))
    self._append(run, "workflow_killed" if terminal_status == "killed" else "workflow_cancelled", {"error": run.error})
```

- 终态不再被 deadline 错误覆盖成 `failed`。
- `final-result.json` 的 `status` 与事件名（`workflow_killed` / `workflow_cancelled`）与调用方意图一致。

---

## 4. 测试（TDD）

新增 `tests/test_workflow_runtime.py::WorkflowCancellationSemanticsTest`（4 条）：

| 测试 | 断言 |
| --- | --- |
| `test_cancelled_agent_never_settles_so_script_does_not_advance` | 单条 `await agent()` 被取消后，后续 `agent()` 不再注册、`log()` 不再触发（脚本停在 await） |
| `test_parallel_cancellation_is_not_swallowed_into_null` | `parallel([...])` 里有一个取消 slot 时，`parallel()` 永不 resolve，`after-parallel` 日志不出现 |
| `test_runtime_observes_external_cancel_of_running_workflow` | running 运行被外部置为 `cancelled` 后，线程在 deadline 前结束，终态 `cancelled`，事件 `workflow_cancelled` |
| `test_complete_pending_rpc_never_settles_for_cancelled_job` | 取消 job 不发帧且 RPC 保持 pending；失败 job 仍发 `ok:false` 并出队 |

配套测试替身：`CancelsFirstChildRunner`（第一条子 agent 返回 `cancelled`，其余成功）、`CapturingProcess`（记录 `rpc_result` 帧）。

### 验证命令与结果

```bash
python -m unittest tests.test_workflow_runtime     # 94 tests OK
python -m unittest tests.test_ink_bridge           # 117 tests OK
python -m unittest tests.test_workflow_controller  # 25 tests OK
```

---

## 5. 不做的事（边界）

- **不复刻 CC 云端会话**：`isolation:"remote"` / `Ho()` / `Uo` / `WP()` 是被关掉的死代码路径。**【复刻时暂时不需要参考 claudecode 云端会话的实现】**
- **不改 `parallel()`/`pipeline()` 的"失败 → null"映射**：那是 CC 原生行为，改掉反而会偏离。
- **不引入 abort signal 对象到 GA 的 worker**：GA 用"宿主不回帧 + 进程 teardown"达成同语义，比在 worker 里维护 AbortController 更贴合现有架构。

---

## 6. 与实施方案的对应

- 实施方案 §4 缺口清单 G7。
- 实施方案 §5 阶段 B2（现已标记"已实施"）。
- 相关文档：`docs/20261012-ga-dynamic-workflow-复刻实施方案.md`、
  `docs/20261012-复刻dynamic-workflow指南.md` §5.1.1（remote 死代码）与 §14（必对齐语义）。
