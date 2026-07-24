# GA UI：绝对 CUP 导致双输入框 — 回滚说明

**日期：** 2026-07-14
**截图：** `截图/严重bug.png`

> ⚠️ **2026-07-16 修正**：本文对「双输入框 / ghost composer」的根因判断（**stdout 所有权** / 绝对 CUP 旁路 Ink）是**正确的**，请放心参考。但本文末尾「后续 IME 方向」把 **IME 漂移**与 ghost 混为同一根因链，这一点不准确——它们是**两个独立根因**：ghost = stdout 所有权，IME 漂移 = 原生光标不可见。IME 漂移的真正根因与最终解见 `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`。

## 现象

启动 `ga ink` 后同时出现**两个**用户输入框（ghost composer）。

## 根因

为对齐 Codex IME，曾把 park 光标改成**全屏绝对 CUP**（`liveTop + relative` → 如 `ESC[23;4H`）。

Codex 可以这样做，因为它**整帧自绘**并统一 `set_cursor_position`。
GA 的 live 区由 **Ink** 拥有 stdout：绝对 CUP 与 Ink 内部光标/擦除序列打架，重绘时留下**第二份 composer**。

## 处置

- **立即回滚** absolute-screen park。
- 恢复 **相对 live 带** CUP + `save/restore`（与回滚前稳定行为一致）。
- 保留 `fixedInputRow` 定宽（防百分比布局横抖），与绝对 CUP 无关。

## 后续 IME 方向（勿再写绝对 CUP）

1. 继续在 Ink 坐标系内 park。
2. 若 IME 仍漂：查 `showCursor` / inverse caret 与 native park 是否双画。
3. 长期若要 Codex 式绝对坐标，需 **接管整帧绘制** 或与 Ink 官方 cursor API 协调，不能旁路写 `ESC[n;mH` 到全屏。

## 验证

```bash
cd frontends/ink-ui && npm test
```

启动应只见**一个**输入框。
