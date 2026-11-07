/**
 * Ctrl+C 的二次确认与「有选区先复制」。
 *
 * 对齐 Claude Code 319 的两段行为：
 *
 * 1. 有选区时 Ctrl+C 复制，不中断（`m2124:8688`）：
 *      if (!hasSelection) return
 *      if (ctrl && !shift && !meta && key === "c") { ...copy...; return !0 }
 *    那个 `return !0` 把事件吃掉，绑在 Global 上的 `app:interrupt` 就不会跑。
 *
 * 2. 无选区时 Ctrl+C 中断：第一次提示，再次才真退出（`m1072` 的 `pending`
 *    状态机 + `m1056:5707` 的 "press ctrl+c or q again to exit"）。
 *
 * 这两条在 GA 里合成一个纯函数，好测也好读：给「有没有选区」和「离上次
 * 请求过了多久」，返回该做什么。
 */

export type InterruptDecision =
  /** 有选区：先复制，这次按键不中断。 */
  | { kind: 'copy-selection' }
  /** 第一次：进入待确认，并提示用户再按一次。 */
  | { kind: 'arm' }
  /** 时间窗内第二次：真的退出。 */
  | { kind: 'exit' }

/** 两次 Ctrl+C 之间多久算「同一次意图」。CC 的窗口足够宽，因为误触常伴随犹豫。 */
export const INTERRUPT_CONFIRM_WINDOW_MS = 3000

export type InterruptInput = {
  /** 当前是否有可复制的选区。 */
  hasSelection: boolean
  /** 上一次 Ctrl+C 触发待确认的时间戳；从未按过为 null。 */
  armedAt: number | null
  /** 当前时间。 */
  now: number
  /** 覆盖确认窗口（测试用）。 */
  windowMs?: number
}

/**
 * 决定这次 Ctrl+C 该做什么。
 *
 * 注意顺序：**选区优先**。有选区时不进入待确认流程，也不清掉已有的待确认
 * 状态——用户在「要退出」和「想复制」之间切换时，不该因为顺手复制了一下就
 * 白等一轮。
 */
export function decideInterrupt(input: InterruptInput): InterruptDecision {
  if (input.hasSelection) return { kind: 'copy-selection' }
  const windowMs = input.windowMs ?? INTERRUPT_CONFIRM_WINDOW_MS
  if (input.armedAt !== null && input.now - input.armedAt <= windowMs) return { kind: 'exit' }
  return { kind: 'arm' }
}

/** 第一次 Ctrl+C 之后显示的提示。措辞对齐 CC 的 "press ctrl+c again to exit"。 */
export function interruptConfirmHint(): string {
  return 'Press Ctrl+C again to exit'
}
