# GA UI 输入框锚点 + slash 位置 — 实施进度

**日期：** 2026-07-14
**方案：** `docs/ga_ui_composer_position_jump_diagnosis_2026-07-14.md`
**方法：** TDD
**状态：** **二期完成（content-desired）**

## 进度

- [x] 轨道 B：slash 在 input 下
- [x] 轨道 A 一期：取消 none（曾用满 messageRows——过修）
- [x] 诊断：Codex「先下移后固定」+ 用户 A'
- [x] **二期 RED**：`messageViewportPlan` content-desired 单测
- [x] **二期 GREEN**：实现 + App / 自测 / 全量回归
- [x] 更新诊断状态

## 二期核心语义

| 状态 | plan | height |
|---|---|---|
| idle + Static | `none` | 0（composer 贴 Static 尾） |
| ready 无历史 | `ready` | 1 |
| streaming | `live` | `min(liveLines, maxLiveRows=12, messageRows)` |

## 代码改动摘要

| 文件 | 变更 |
|---|---|
| `messageViewportPlan.ts` | content-desired；`DEFAULT_MAX_LIVE_ROWS=12` |
| `messageViewportPlan.test.ts` | 锁定 none / ready=1 / stream 随内容 / cap |
| `App.test.ts` | 光标 6;4H；Ctrl+C 7 行；贴内容 slot≤4；stream 边框 gap=2 |
| `grok_selfcheck_composer_layout.ts` | 二期验收探针 |
| `inputLayout.ts` / 光标 panelRows=0 | 一期保留 |

## 验证

- `npm test`：**253/253 pass**
- `npx tsx src/grok_selfcheck_composer_layout.ts`：**SELFCHECK PASS**
  - idle+static → none；stream height=4；ready=1；cap=12
  - App idle slot=3（贴内容）；slash 在边框下

## 日志

| 时间 | 事件 |
|---|---|
| 一期 | slash 下 + 满高槽；252 pass |
| 用户 A' | 贴底但离内容太远 |
| 文档 | Codex desired_height / insert_history |
| 二期 RED | content-desired 单测 5 fail |
| 二期 GREEN | plan 实现；App/自测更新；**253/253** |
| 后续 | 用户反馈 Running 时本轮刷没 → 见 `docs/ga_ui_running_turn_visibility_diagnosis_2026-07-14.md` |
| 头栏 | 默认 inline 去掉 `GenericAgent` 状态头（`headerRows:0`）；光标 CUP `5;4H`；测试/自测按 empty-gap 量贴内容，不再依赖头栏字符串 |
