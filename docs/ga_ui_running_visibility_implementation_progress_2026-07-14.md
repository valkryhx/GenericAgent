# GA UI Running 可见性 — 实施进度

**日期：** 2026-07-14  
**方案：** `docs/ga_ui_running_turn_visibility_diagnosis_2026-07-14.md`  
**方法：** TDD  
**状态：** **阶段 1+2 生产可用；阶段 3 几何库保留，App ANSI 写入已回滚（Ink 冲突）**

## 进度

- [x] 阶段 1 P0-A
- [x] 阶段 2 增量 commit
- [x] 阶段 3 几何模块 + 单测
- [x] **回滚** App 写 ANSI（`截图/新bug.png` 叠层花屏）
- [x] 文档：`docs/ga_ui_phase3_ansi_ink_conflict_fix_2026-07-14.md`
## 阶段 3（viewport / insert_history）

| 能力 | 实现 |
|---|---|
| 状态 | `ViewportState { areaY, areaHeight, screenRows }` |
| 插入 history | `advanceViewportForHistory`：`areaY += min(lines, roomBelow)`，贴底后 `scrollAmount=0` |
| dock 变高 | `applyDockHeight`：溢出则 `areaY = screenRows - height` |
| ANSI | `insertHistorySequence`：DECSTBM + RI（`\x1bM`）近似 Codex |
| App | Static 行数增加时推进几何并 best-effort `stdout.write(sequence)`；full mouse 模式跳过 |

**边界：** 完整替代 Ink 自管 stdout 不在本阶段；数学与副作用序列已对齐 Codex，Ink 仍是主渲染器。

## 代码

| 文件 | 角色 |
|---|---|
| `insertHistory.ts` / `.test.ts` | **新建** 几何 + ANSI |
| `App.tsx` | `inlineViewportRef` + Static/dock 同步 |
| `streamCommit.ts` | 阶段 2 |
| `state.ts` | P0-A + commit |
| `grok_selfcheck_insert_history.ts` | 阶段 3 自测 |

## 验证

```text
npm test                         → 279/279
npx tsx src/grok_selfcheck_full_visibility.ts → FULL SELFCHECK PASS
npx tsx src/grok_selfcheck_insert_history.ts  → PASS
npx tsx src/grok_selfcheck_stream_commit.ts   → PASS
npx tsx src/grok_selfcheck_user_duplicate.ts  → PASS
npx tsx src/grok_selfcheck_composer_layout.ts → PASS
```

充分自测说明：`docs/ga_ui_running_visibility_selftest_2026-07-14.md`

## 日志

| 时间 | 事件 |
|---|---|
| P0-A | open user live-only |
| 阶段 2 | streamCommit |
| 回归 | 用户反馈「不显示用户输入 + 答在问前」→ 首包 delta 先 finalize user；live 截尾 pin 最后 user 块；见 `docs/ga_ui_user_before_assistant_order_fix_2026-07-14.md`；**277/277** |
| 回归 | `GenericAgent running/idle` 顶栏插在 Static 与 live 中间 → 默认 inline `headerRows:0`，状态只留底部 activity；full mouse 仍保留 1 行头栏；测试帧检测改为 `Enter send` / `Running ·` / 边框；**279/279** |
| 回归 | 用户反馈「LLM Running Turn 1 显示两次 + `:28**` 截断」→ session 仅一份 assistant_text；根因是 `assistant_done` 拼接 pending delta + done 全文导致 stream-commit 前缀失败重画；见 `docs/ga_ui_llm_turn_duplicate_fix_2026-07-14.md`；**283/283** |
