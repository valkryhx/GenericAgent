/**
 * Ink 渲染选项。
 *
 * 单独成一个模块，是为了让 `main.tsx` 和测试用**同一份**配置。之前测试直接
 * `render(...)` 没传这个选项，于是走 Ink 默认的 `exitOnCtrlC: true`——Ctrl+C
 * 由 Ink 自己 unmount，事件到不了 App，二次确认测不出来，真机上也不生效。
 */
import type { RenderOptions } from 'ink'

/**
 * `exitOnCtrlC: false` —— 把 Ctrl+C 的所有权交还应用。
 *
 * Ink 的默认值（`ink/build/render.js:14`）是 `true`：它在 stdin 上自己挂监听并
 * 立刻 unmount，绕过 App 的 `handleTerminalInput`。而 GA 需要两种语义，对齐
 * Claude Code 319：
 *   - 有选区时 Ctrl+C 只复制、不退出（`m2124:8688` 复制后 `return !0` 吞掉事件）；
 *   - 无选区时第一次提示、二次才退出（`m1072` 的 pending 状态机）。
 */
export const inkRenderOptions = {
  exitOnCtrlC: false,
} satisfies Pick<RenderOptions, 'exitOnCtrlC'>
