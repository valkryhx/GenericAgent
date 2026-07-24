# GA UI 用户输入双显 — 实施进度

**日期：** 2026-07-14
**方案：** `docs/ga_ui_user_input_duplicate_display_fix_plan_2026-07-14.md`（Codex 对齐修订版）
**方法：** Test-Driven Development
**状态：** **已完成（主方案 P0）**

## 进度清单

- [x] 阅读修订方案与 TDD skill
- [x] Phase 1 RED：改写 partition / grok 断言为「live 无 user，static 有 user」
- [x] Phase 1 验证：确认测试按预期失败（9 fail / 3 pass）
- [x] Phase 2 GREEN：实现 `splitStaticAndActiveMessages` 最小改动（active 仅 `!done`）
- [x] Phase 2 验证：目标测试全绿（12/12）
- [x] Phase 3：全量 ink-ui 相关回归 + 修复连带失败
  - [x] 更新 `App.test.ts` 中依赖「live 含活动问题」的用例
  - [x] `npm test`：**246/246 pass**
- [x] 更新诊断/方案状态

## 代码改动摘要

| 文件 | 变更 |
|---|---|
| `frontends/ink-ui/src/messagePartition.ts` | **主修复**：`staticMessages = all done`；`activeMessages = latest task 的 !done`（不再 `slice` 整 task） |
| `frontends/ink-ui/src/messagePartition.test.ts` | Codex 语义断言 |
| `frontends/ink-ui/src/grok_user_input_duplicate.test.ts` | 从 bug 复现器改为回归门禁：liveHits(user)=0，static 有 user |
| `frontends/ink-ui/src/App.test.ts` | 静态/活动 transcript 用例改为 user 在 Static、live 不含 user |

## 核心实现

```ts
const staticMessages = messages.filter(message => message.done)
// holdActive 时：
const activeMessages = messages.filter(
  message => message.taskId === latestTaskId && !message.done,
)
```

## 日志

| 时间 | 事件 |
|---|---|
| 开始 | 建立本进度文件；准备 RED |
| RED 写测 | 已改写 `messagePartition.test.ts` + `grok_user_input_duplicate.test.ts` 为 Codex 语义 |
| RED 验证 | 12 测中 9 失败：running 时 u-2 进 active、App live 仍含 user |
| GREEN | 改 `messagePartition.ts`：static=all done；active=latest task 的 !done |
| GREEN 验证 | 目标 12/12 pass |
| 回归 | App.test 1 失败（期望 live 含「活动问题」）→ 按 Codex 改断言 |
| 全量 | `npm test` 246/246 pass |
| 收尾 | 诊断/方案状态已更新为已修复 |
| 二次自测 | 聚焦 24/24 pass；`npx tsx src/grok_selfcheck_user_duplicate.ts` **SELFCHECK PASS** |
| 自证证据 | 旧 partition active=`[u-1]` vs 新 `[]`；App: staticHits=1, liveHits(user)=0, liveHits(assistant)=1, total user occurrences=1 |
