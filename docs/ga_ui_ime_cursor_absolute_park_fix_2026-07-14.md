# GA UI：输入框整体晃动 / IME 不贴光标 — Codex 对齐修复

**日期：** 2026-07-14  
**症状：** 打字时**整块用户输入框**左右/上下晃；中文输入法候选窗不在真正 caret 旁。

## Codex 怎么做

`codex-rs/tui/src/app.rs` 每帧 draw 后：

```rust
if let Some((x, y)) = self.chat_widget.cursor_pos(area) {
    frame.set_cursor_position((x, y));
}
```

`textarea.cursor_pos_with_state` 返回 **屏幕绝对坐标**：

```rust
Some((area.x + col, area.y + screen_row))
```

即：composer 在屏幕上的 `(x,y)`，而不是“组件内部相对行号”。

## GA 旧问题

`inputCursorPosition` 只算 **live 区内部相对行**：

```
relative = header + message + activity + hint + border + cursorLine
CUP(relative, col)   // 误当成全屏坐标
```

有 `<Static>` 历史后，live 带贴在终端**底部**，相对 row≈4 的 CUP 却把原生光标/IME 钉到屏幕**靠上**位置。  
每键：`save → 错误 CUP → Ink 重绘 → restore`，终端为追 IME 会带动视口，看起来像**整框晃动**。

## 修复

| 项 | 内容 |
|---|---|
| `inputCursorPosition` | 默认 `absoluteScreen: true`：`liveTop = terminalRows - liveViewportRows`，`row = liveTop + relative` |
| `inputCursorCupSequence` | 用物理 `terminalRows/Columns` 做 CUP clamp |
| `App` park effect | 写入绝对 CUP；cleanup 仍用相对 row（相对 live 带） |
| 测试 | absolute 锚底 → `ESC[23;4H`（24 行终端、6 行 live、相对 row 4） |

固定行宽（`fixedInputRow`）仍保留，消除百分比布局横向抖动；本次补的是 **IME 锚点**。

## 验证

```bash
cd frontends/ink-ui && npm test
```

重启 `ga ink`：中文输入法候选应贴着 `>` 后 caret；有历史对话时输入框不应再整体跳。
