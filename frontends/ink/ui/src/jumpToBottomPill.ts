/**
 * "Jump to bottom" 药丸按钮的文案与落点计算。
 *
 * 为什么要有它：GA 打开了鼠标捕获（滚轮翻历史的前提），于是有了应用内滚动；
 * 用户滚上去之后必须能一步回到最新，否则只能用 End 键，而鼠标用户不知道。
 * CC 的做法是在滚动区底部居中浮一个可点的药丸（319 `m2064` 的 `dd` 组件：
 * `position:absolute, bottom:0, left:0, right:0, justifyContent:center`）。
 *
 * 与 CC 的差异：CC 用的是自家 fork 的 Ink，`Box` 支持 `left/bottom` 和
 * `onClick`；GA 用的 ink 5.2.1 两者都没有。所以这里不申请绝对定位，而是
 * **把视口最后一行改画成药丸**，点击则由 GA 自己的鼠标事件链路做命中测试
 * （`hitTestJumpToBottom`）。效果一样——都是覆盖在内容上、不占额外行。
 */

import stringWidth from 'string-width'

export type JumpToBottomInput = {
  /** 滚离底部之后新增的行数；0 时显示通用的 "Jump to bottom"。 */
  count: number
  /** 可用列数（调用方已扣掉左右留白）。 */
  availableColumns: number
  /** 是否支持鼠标点击（决定要不要带 "(click)" 提示）。 */
  clickable?: boolean
  /** 自定义键位描述，如 "ctrl+end"；空则不提键位。 */
  keyHint?: string
}

export const JUMP_TO_BOTTOM_ARROW = '↓'

/** 单复数正确；对齐 CC 的 `P(n, "message")`。 */
export function jumpToBottomLabel(count: number): string {
  const n = Math.max(0, Math.floor(count))
  if (n <= 0) return 'Jump to bottom'
  return `${n} new ${n === 1 ? 'message' : 'messages'}`
}

/**
 * 按「最详尽 → 最简」取第一个放得下的文案。永远返回非空串：极窄终端下
 * `Jump to bottom` 也可能超宽，此时由调用方截断——宁可截断也不能不显示，
 * 否则用户滚上去就没有回去的路。
 */
export function jumpToBottomText(input: JumpToBottomInput): string {
  const label = jumpToBottomLabel(input.count)
  const arrow = `${label} ${JUMP_TO_BOTTOM_ARROW}`
  const withKey = input.keyHint ? `${label}: ${input.keyHint} to scroll` : ''
  const withClick = input.clickable ? `${label} (click) ${JUMP_TO_BOTTOM_ARROW}` : ''

  const candidates = [withClick, withKey, arrow, label].filter(candidate => candidate.length > 0)
  const width = Math.max(0, Math.floor(input.availableColumns))
  for (const candidate of candidates) {
    if (stringWidth(candidate) <= width) return candidate
  }
  return label
}

export type CountableMessage = {
  role: 'user' | 'assistant' | 'system'
  /** 正文；空串表示这条消息没有可显示内容（如纯工具调用占位）。 */
  text: string
}

/**
 * 运行时 step 标记的正则来源。
 *
 * 后端 `genericagent/core/loop.py` 每跑一步就往后**同一条** assistant 消息里追加
 * `\n\n**LLM Running (Step N) ...**\n\n`（或 `**Step N ...**`）。所以一轮对话内部
 * 会持续长出多个 step 块 —— 这正是「滚动期间 N 该不断增长」所计的单位。
 *
 * 定义放在这里而不是 `messageWindow.ts`，是因为两处必须用同一份判据：那里用它
 * 判断 stdout 块边界（`isStepBoundary`），这里用它数块数。各写一份迟早会漂移。
 */
export const STEP_HEADER_LINE_RE =
  /^\s*(?:\*\*LLM Running \(Step \d+\) \.\.\.\*\*|\*\*Step \d+ \.\.\.\*\*|LLM Running \(Step \d+\) \.\.\.|Step \d+ \.\.\.)\s*$/

/** 一行是否是运行时 step 标记。 */
export function isStepHeaderLine(line: string): boolean {
  return STEP_HEADER_LINE_RE.test(line)
}

/** 文本里有多少个 step 标记。 */
export function countStepHeaders(text: string): number {
  if (!text) return 0
  let count = 0
  for (const line of text.split('\n')) {
    if (isStepHeaderLine(line)) count += 1
  }
  return count
}

/**
 * 数「新增的可见块」数，单位是 **step**（对齐 CC 319 `m2064` 的 `$C`）。
 *
 * 为什么是 step 而不是轮次或行数：
 * - 按轮次：正在跑的那一轮还没结束，计数永远是 1，滚动时完全不涨；
 * - 按行数：一轮问答折行后是 4+ 行，「3 轮」显示成 12，放大约 4 倍；
 * - 按 step：后端每步追加一个 `LLM Running (Step N) ...`，是屏幕上真正多出来的
 *   一块，滚动期间持续增长，而且是用户眼里的「新内容」。
 *
 * 没有 step 标记的 assistant 消息（普通一次性回复）算 1 块；user / system 不计。
 */
export function countNewMessages(messages: CountableMessage[], fromIndex: number): number {
  const start = Math.max(0, Math.min(Math.floor(fromIndex), messages.length))
  let count = 0
  for (let index = start; index < messages.length; index += 1) {
    const message = messages[index]!
    if (message.role !== 'assistant') continue
    const steps = countStepHeaders(message.text)
    // A step-less reply is still one new block; an empty assistant placeholder
    // (tool-only turn) adds nothing visible, so it counts as zero.
    if (steps > 0) count += steps
    else if (message.text.trim().length > 0) count += 1
  }
  return count
}

export type PillPlacement = {
  /** 药丸在内容区内的起始显示列（0 基）。 */
  left: number
  /** 药丸的显示宽度（含左右各 1 格内边距）。 */
  width: number
  /** 实际会被绘制的文案（超宽时截断）。 */
  text: string
}

/**
 * 药丸在内容区内的水平落点：居中，且连同左右各 1 格内边距都不超出内容区。
 * `width` 已经把内边距算进去，命中测试和绘制共用同一份几何，不会错位。
 */
export function jumpToBottomPlacement(text: string, contentColumns: number): PillPlacement {
  const columns = Math.max(1, Math.floor(contentColumns))
  const padded = ` ${text} `
  const width = Math.min(stringWidth(padded), columns)
  const left = Math.max(0, Math.floor((columns - width) / 2))
  return { left, width, text }
}

/**
 * 命中测试：视口内坐标是否落在药丸上。
 *
 * `row`/`col` 是**视口内**坐标（与选区同一套），`pillRow` 是药丸所在的行号。
 * 药丸比文字高一行（就一行），所以行号必须相等。
 */
export function hitTestJumpToBottom(
  placement: PillPlacement | null,
  pillRow: number,
  row: number,
  col: number,
): boolean {
  if (!placement) return false
  if (row !== pillRow) return false
  return col >= placement.left && col < placement.left + placement.width
}
