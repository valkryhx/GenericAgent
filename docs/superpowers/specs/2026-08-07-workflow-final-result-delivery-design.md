# Workflow 最终结果交付修复设计

日期：2026-08-07

## 问题

真实 GA Ink UI workflow 已完成执行，child transcript 与 `final-result.json` 也包含最终结果，但 `workflow_final` 事件返回 `artifactError=missing_ref`。Ink reducer 因而只能记录回退 payload，无法得到真实 workflow 结果。

确定性复现表明：

- `WorkflowRuntime.run()` 返回的内存 `run.result_ref` 为 `final-result.json`。
- `final-result.json` 已存在并包含 worker result。
- 从 `WorkflowStore` 重新加载同一 run 后，`result_ref` 为 `None`。
- `GenericAgentBridge._workflow_final_payload()` 因缺少持久化引用而进入 `missing_ref` 回退。

## 根因

`WorkflowRuntime` 的 `succeeded`、`failed` 和 `killed` 终态路径均先调用 `save_run(run)`，随后调用 `write_final_result(run, payload)`。后者只写 artifact 并在内存对象上设置 `run.result_ref`，不会再次保存 `run.json`/`state.json`。

bridge 在 runtime 返回后重新加载 run，因此看不到仅存在于旧内存对象上的 `result_ref`。

## 设计决策

在 `WorkflowRuntime` 的三条终态路径中保留现有第一次 `save_run(run)`，并在 `write_final_result()` 后增加第二次 `save_run(run)`。

终态顺序为：

1. 设置终态、错误和 bounded execution metadata。
2. 第一次 `save_run(run)`，保留现有 external-kill guard 和终态状态持久化语义。
3. 写 `workflow-progress.json`。
4. 写 `final-result.json`；该调用设置内存 `run.result_ref`。
5. 第二次 `save_run(run)`，持久化 `result_ref`。
6. 发终态 journal event 或返回 runtime result。

第二次保存必须应用于：

- 正常 `succeeded`。
- external kill 投影后的 `killed`。
- runtime/provider/test-gate/verification 异常后的 `failed`。

## 未采用方案

### 让 `WorkflowStore.write_final_result()` 自动保存 run

不采用。该方法当前是低层 artifact writer；自动调用 `save_run()` 会给所有调用方增加锁、kill guard 和状态写入副作用，并改变已有调用顺序。

### bridge 在 `result_ref` 缺失时猜测 `final-result.json`

不采用作为主修复。它只能修复一个消费者的显示症状，durable run state、其他 adapter 和后续恢复逻辑仍会读取到不完整状态。

### 删除第一次 `save_run()`，只在 final artifact 后保存一次

不采用。第一次保存承担 external-kill guard；删除它会改变终态竞争窗口，超出本缺陷范围。

## TDD 验证

先增加一个跨真实组件边界的确定性测试：

```text
WorkflowRuntime（真实 Node worker + FakeChildAgentRunner）
→ WorkflowStore durable state/final artifact
→ GenericAgentBridge workflow_final payload
```

测试应在修复前证明：

- runtime worker result 含 marker。
- `final-result.json` 含 marker。
- 重新加载的 run 缺少 `result_ref`。
- bridge final payload 缺少 marker，并返回 `missing_ref`。

修复后断言：

- 重新加载的 succeeded run 的 `result_ref == "final-result.json"`。
- bridge `workflow_final.result` 包含 worker marker。
- failed 与 killed run 重新加载后也保留 `final-result.json` 引用及对应终态 payload。

随后运行：

- 聚焦 runtime/bridge 回归。
- workflow runtime、store、controller、bridge 与 agent-control 相关测试。
- Python 全量测试。
- Ink UI tests 与 typecheck。
- 使用 `llm.yaml` 中 `luna / gpt-5.6-luna / gpt-super-responses` 的真实 headless Ink bridge/reducer E2E。

## 非目标

- 不改变 final payload schema 或 64 KiB bridge 上限。
- 不改变 workflow child 的工具、技能或权限策略。
- 不修改 common agent identity、cursor、capability 或 lifecycle contract。
- 不为历史已损坏 run 增加 bridge 猜测兼容逻辑。
- 不重构 `WorkflowStore` artifact API。

## 验收标准

1. 三条 terminal runtime 路径均持久化 `result_ref`。
2. 真实 runtime 返回后重新加载 run 不丢失 final artifact 引用。
3. GA Ink bridge 的 `workflow_final` 能读取真实 `final-result.json`，不再因该时序返回 `missing_ref`。
4. 真实 `gpt-5.6-luna` child 输出 marker 能进入 Ink reducer 的 `workflowResults[runId]`。
5. 现有安全回退、bounded payload、common agent projection 和全量测试保持通过。
