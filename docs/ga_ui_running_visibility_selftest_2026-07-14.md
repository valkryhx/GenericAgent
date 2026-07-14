# GA UI Running 可见性 — 充分自测说明

**日期：** 2026-07-14  
**范围：** 阶段 1（P0-A）+ 阶段 2（stream commit）+ 阶段 3（viewport 几何）+ composer layout 回归

## 怎么跑

在 `frontends/ink-ui` 下：

```bash
# 1) 全量单元 / App 回归
npm test

# 2) 综合充分自测（推荐）
npx tsx src/grok_selfcheck_full_visibility.ts

# 3) 分阶段探针
npx tsx src/grok_selfcheck_user_duplicate.ts      # P0-A running 可见
npx tsx src/grok_selfcheck_stream_commit.ts       # 阶段 2 增量 commit
npx tsx src/grok_selfcheck_insert_history.ts      # 阶段 3 viewport 数学
npx tsx src/grok_selfcheck_composer_layout.ts     # content-desired + slash 下
```

## 测试代码清单

| 文件 | 覆盖 |
|---|---|
| `src/insertHistory.test.ts` | create/clamp、下移贴底冻结、dock 增高贴底、ANSI 序列、零行 no-op、多轮模拟 |
| `src/streamCommit.test.ts` | 短流不 commit、溢出 tail、多次 commit、跨 task 隔离、prefix 拼接、remaining、**端到端 applyBridgeEvent 长流无重复** |
| `src/state.test.ts` | open user、finalize、长流 commit+done |
| `src/messagePartition.test.ts` | live-only open user、单通道、finalize 后 static |
| `src/messageViewportPlan.test.ts` | content-desired、running placeholder |
| `src/grok_user_input_duplicate.test.ts` | App running 无过早 Static、stream 过程可见 |
| `src/App.test.ts` | slash 下、贴内容槽、open user 在 live 等 |
| `src/grok_selfcheck_full_visibility.ts` | **综合**：viewport + plan + state + App 渲染 |

## 最近一次结果（本机）

```text
npm test                              → 275/275 pass
npx tsx src/grok_selfcheck_full_visibility.ts → FULL SELFCHECK PASS
npx tsx src/grok_selfcheck_user_duplicate.ts  → SELFCHECK PASS
npx tsx src/grok_selfcheck_stream_commit.ts   → SELFCHECK PASS
npx tsx src/grok_selfcheck_insert_history.ts  → SELFCHECK PASS
npx tsx src/grok_selfcheck_composer_layout.ts → SELFCHECK PASS
```

## 关键断言（充分自测在证什么）

1. **Running 可见**：`user→running` 后 live 含 user，Static 无过早 user  
2. **边流边 commit**：长 delta 中途出现 `a-*-c*` 且 live ≤ 8 行  
3. **无双显 / 无二次整段**：finalize 后每行 assistant 仅 1 次；user 单通道  
4. **Viewport**：先 `areaY` 下移，触底冻结；dock 长高再贴底  
5. **Layout**：idle content-desired 无满高 spacer；slash 在 input 下  
