# GA UI：IME 双光标 / ghost composer — 绝对 CUP 再次回滚

> ⚠️ **本文部分结论已被推翻（2026-07-16）。** 本文对 **ghost composer / 整框晃** 的判断（stdout 所有权竞态）成立，但对 **IME 漂移** 的判断与「不要 park 原生光标、原生光标保持隐藏」的强制结论**是错的**——那恰恰是 IME 候选框漂移长期无解的原因。真正根因是「Windows Terminal 的输入法锚定**可见**的原生光标」，修复是让 park 后的原生光标**可见（SHOW）**。权威结论见 **`docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`**。本文保留作失败尝试的历史记录。

**日期：** 2026-07-15  
**状态：** 绝对 CUP park **已回滚**；保留定宽输入 + inverse caret（⚠️ 结论部分已被 07-16 文档修正）  
**截图：** `截图/屏幕截图 2026-07-15 124017.png`（绝对 CUP 后出现双输入框）

## 结论（强制）

在 **Ink 拥有 stdout** 时：

1. **禁止** App 层写绝对 CUP（含 delay 后写）。
2. **禁止** 用 save/restore + CUP 旁路 Ink。
3. 可见光标只用 **inverse caret**（`renderInputLine`）。
4. ~~原生光标由 Ink `log-update` / `cli-cursor.hide()` 隐藏；不要再 park 原生光标去“对齐 IME”。~~ ⚠️ **此条已被 2026-07-16 推翻。** 正解恰恰相反：必须 park 原生光标到 caret **并让它可见（SHOW）**，因为 Windows Terminal 的输入法候选框锚定的是**可见**的原生光标。把原生光标隐藏正是 IME 漂移无解的根因。见 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`。（本条当时被误判，是因为把「park 会引起 ghost/晃」与「park 能对齐 IME」耦合在了一起——实际前者的元凶是**绝对 CUP + 旁路 stdout**，与「park 原生光标」本身无关；07-16 的相对 park + 单 writer 收口既不 ghost 也能对齐 IME。）

这与 `docs/ga_ui_absolute_cup_ghost_composer_rollback_2026-07-14.md` 一致。  
本次再次验证：延迟绝对 CUP（`nativeCursorParkAfterInkMs`）仍会在 Windows Terminal 留下 **ghost composer**。

## 尝试过且失败的路径

| 路径 | 结果 |
|---|---|
| 相对 live CUP + save/restore（旧） | 原生光标/IME 偏右，整框晃 |
| 定宽 `fixedInputRow` | 横抖改善，但**不解决**右侧原生 IME |
| layoutEffect 立即绝对 CUP | ghost composer（7-14 回滚） |
| setTimeout 后绝对 CUP（无 save/restore） | **仍 ghost composer**（本截图） |

根因不是“CUP 坐标差 1 列”，而是 **stdout 所有权**：Ink `eraseLines` + App 旁路 CUP 两套光标/擦除模型打架。

## 当前实现

| 项 | 状态 |
|---|---|
| `fixedInputRow` / 数值定宽 InputView | **保留** |
| App 内 native CUP park effect | **删除** |
| inverse caret | **保留**（唯一可见 caret） |
| `terminalCursor.ts` 底部对齐 helper | 可保留给单测/未来自管 terminal；**App 不调用 park** |

## 长期正确方向（~~未做~~ → 2026-07-16 已落地，比本节设想更省）

> ⚠️ **本节的「必须二选一」判断偏保守，已被 2026-07-16 证伪。** 实际不必自管 terminal，也不必改 Ink 源码。

要 Codex 级 IME 贴合，~~必须二选一~~ 本节当时以为需要：

- ~~自管 terminal owner（整帧 diff + frame 后 set_cursor），或~~  
- ~~与 Ink 官方 cursor API 协调，而不是 App `stdout.write(ESC[n;mH)`。~~

**实际落地（07-16，路径 A）：** 用 `render(<App/>, { stdout })` 这个 Ink 合法注入点，把一个 **wrapped stdout**（`stdoutCursorPark.ts`）插在 Ink 与真实 stdout 之间。它做到了「单一 writer + cursor 每帧最后写 + 相对移动」——即 Claude Code frame-declared cursor 模型的等价效果——**既不用弃用 Ink，也不用移植 Ink fork**。关键补丁：park 到 caret 后 **SHOW 原生光标**，下一帧写入前先 HIDE。见 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`。

## 验证

```bash
cd frontends/ink-ui
npm run test
```

人工：

```bash
ga ink
```

启动应只见 **一个** 输入框；输入时可见 caret 为 inverse 块，不应再出现第二份 composer。
