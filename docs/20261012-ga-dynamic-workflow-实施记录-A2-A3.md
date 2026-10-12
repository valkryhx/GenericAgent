# GenericAgent Dynamic Workflow 复刻实施记录（A2/A3 DSL 语义对齐）

> 日期：2026-10-12
> 对应实施方案：`D:\git_codes\GenericAgent\docs\20261012-ga-dynamic-workflow-复刻实施方案.md`
> 对应 CC 源码调研：`D:\git_codes\claudecode283_hack\docs\20261012-复刻dynamic-workflow指南.md` §5 / §14
> CC 源码坐标：`m0869_0xd12567a.js` `Ko()` @74546（parallel）、`zo()` @75362（pipeline）
> 范围：阶段 A / A2（G2）、A3（G3）

## 1. 决策

用户明确要求：**不保留 GA 旧语义兼容开关，直接按 Claude Code 语义修改。**
因此本次是 GA 的契约变更，旧的 fail-fast / pipeline barrier 测试已同步更新。

## 2. 实现

`workflow_js_worker.js`：

### 2.1 `parallel(items)`

- 对齐 CC：要求 `items` 必须是数组，数组元素必须是 thunk 函数。
- 非函数元素抛 CC 风格 `TypeError`，不再把 Promise 当普通值透传。
- 用 `Promise.allSettled` 执行所有 thunk。
- 单项 fulfilled → 保留返回值；单项 rejected → 映射为 `null`。
- 单项失败不会取消其他 thunk，也不会让整个 `parallel` reject。

### 2.2 `pipeline(items, ...stages)`

- 对齐 CC：要求第一参数是数组，所有 stage 必须是函数。
- 每个 item 独立异步执行自己的 stage 链。
- item 之间并行，stage 之间**没有 barrier**。
- 当前 item 的结果为 `null` 时，只短路该 item 的后续 stage；其他 item 继续。
- 单项 stage 抛错时，该项最终映射为 `null`，不影响其他 item。
- 最终结果顺序仍与输入 items 顺序一致。

## 3. 行为变化

### `parallel`

```js
const results = await parallel([
  () => agent('ok'),
  () => agent('fail'),
])
// 旧：整个 parallel reject，run failed
// 新：results = [成功结果, null]，run 可正常完成
```

### `pipeline`

```js
const results = await pipeline(
  ['a', 'b'],
  async item => agent('stage1 ' + item),
  async prev => agent('stage2 ' + prev.summary),
)
```

旧实现要求所有 item 完成 stage1 后才进入 stage2；新实现中 item A 可在 item B 尚未完成 stage1 时进入自己的 stage2。

若某 item 的 stage 返回 `null`，该 item 的后续 stage 不再执行；其他 item 不受影响。

## 4. 测试同步

| 文件 | 变更 |
| --- | --- |
| `tests/test_workflow_runtime.py` | 增加无 barrier 断言、per-item `null` 短路、parallel 非函数 TypeError、parallel 部分失败返回 `null`、429 部分失败返回 `null` 等回归。 |
| `tests/test_ink_bridge.py` | 更新 raw script 经 Ink Bridge 的端到端断言：thunk throw / stage throw 变成 `null`，部分失败变成 `partial` 成功终态并保留失败 child 产物。 |

被移除/改写的旧契约测试：

- `test_runtime_worker_error_after_parallel_cancels_pending_jobs`
- `test_runtime_parallel_partial_failure_preserves_success_artifact_and_failed_result`
- `test_runtime_pipeline_runs_stage_batches_and_preserves_item_order`
- `test_runtime_parallel_thunk_sync_throw_fails_without_agent_jobs`
- `test_ink_bridge` 中对应的 fail-fast / barrier 断言

## 5. 验证记录

```bash
python -m unittest tests.test_workflow_determinism tests.test_workflow_plan_validator tests.test_workflow_controller tests.test_workflow_runtime tests.test_ink_bridge
# Ran 278 tests ... OK

python -m unittest discover -s tests
# Ran 1409 tests ... OK (skipped=3)
```

`workflow_js_worker.js` 已通过 `node --check`；相关 Python 文件已通过 `py_compile`；`git diff --check` 通过。

## 6. 影响与边界

- 这是**有意打破 GA 旧契约**的变更，调用方不能再依赖 parallel 单项失败拖垮整批，也不能依赖 pipeline stage barrier。
- 失败 child 的 result / transcript / journal 仍保留；run 是否成功由脚本最终是否正常返回决定。
- 部分失败通常表现为 `status=succeeded` 且 `metadata.executionOutcome=partial`。
- CC 云端会话路径（`isolation:"remote"` / `Ho()` / 云端会话）不在本次范围内，也不依赖本次修改。

## 7. 后续

- A4 `meta` 契约强化（P1）。
- B1 `budget`（P1）。
- B2 子 agent 取消语义（P1）。
- C1/C2 `/workflows` UI 历史恢复与状态行文案（P2）。

## 8. 查阅路径

- `D:\git_codes\claudecode283_hack\docs\20261012-复刻dynamic-workflow指南.md`
- `D:\git_codes\claudecode283_hack\claudecode283\extracted\dynamic-workflow-internals.md`
- `D:\git_codes\GenericAgent\docs\20261012-ga-dynamic-workflow-复刻实施方案.md`