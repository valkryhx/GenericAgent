import os from 'node:os'
import { mouseTrackingOff, mouseTrackingOn, type MouseCaptureMode } from './mouseWheel.js'

const showCursor = '\u001B[?25h'
const enterAlternateScreen = '\u001B[?1049h\u001B[2J\u001B[H'
const exitAlternateScreen = '\u001B[?1049l'

export const enterMainScreenTerminalSequence = ''
export const exitTerminalCleanupSequence = `${mouseTrackingOff()}\u001B[0m${showCursor}\r\u001B[2K`

function finiteFloor(value: number, fallback: number): number {
  return Number.isFinite(value) ? Math.floor(value) : fallback
}

export function clearInlineLiveViewportSequence(input: { rows: number; cursorRow: number }): string {
  const rows = Math.max(1, finiteFloor(input.rows, 1))
  const cursorRow = Math.max(0, Math.min(rows, finiteFloor(input.cursorRow, 0)))
  const upToTop = cursorRow > 0 ? `\u001B[${cursorRow}A` : ''
  const clearLines = Array.from({ length: rows }, (_, index) => (
    `${index === 0 ? '' : '\u001B[1B'}\r\u001B[2K`
  )).join('')
  const upToTopAfterClear = rows > 1 ? `\u001B[${rows - 1}A` : ''

  return `\u001B[0m${upToTop}\r${clearLines}${upToTopAfterClear}\r`
}

const ESC_SEQ = '\u001B'

/**
 * 老 Windows 控制台（Windows 10 build < 10586）不支持清 scrollback 的 CSI 3J，
 * 判定与 ansi-escapes 的 clearTerminal 一致。
 */
function isLegacyWindowsConsole(): boolean {
  if (process.platform !== 'win32') return false
  const parts = os.release().split('.')
  const major = Number(parts[0])
  const build = Number(parts[2] ?? 0)
  return major < 10 || (major === 10 && build < 10586)
}

/**
 * 终端宽度变化（reflow）后的「整屏重置」序列：清屏 + 清 scrollback + 光标归位。
 *
 * 为什么需要它：终端变宽/变窄时，真实终端会对已经写出的缓冲区做 reflow（长行重新折行），
 * 上一帧在屏幕上实际占用的行数随之改变；而 ink 的 log-update 下一帧仍然用改尺寸**之前**的
 * previousLineCount 做相对擦除（eraseLines），于是擦不干净 → 旧帧顶行残留 → 真机表现为
 * 「放大/缩小终端后底部信息重复叠加」（截图 2026-10-10 092005）。
 *
 * 参考实现都不去猜 reflow 之后的几何，而是直接整屏重置 + 全量重绘：
 * - Claude Code：`log-update.ts` 在 viewport 变窄/变矮时返回 fullResetSequence_CAUSES_FLICKER
 *   （clearTerminal + 整帧重画）；
 * - Codex：`tui.rs::draw_with_resize_reflow` 用 clear_after_position + invalidate_viewport
 *   重建 viewport，而不是沿用旧的相对擦除。
 */
export function resetViewportSequence(): string {
  if (isLegacyWindowsConsole()) return `${ESC_SEQ}[2J${ESC_SEQ}[0f`
  return `${ESC_SEQ}[2J${ESC_SEQ}[3J${ESC_SEQ}[H`
}

export function enterMainScreenTerminalSequenceForMode(mode: MouseCaptureMode = 'off'): string {
  return mode === 'full' ? `${enterAlternateScreen}${mouseTrackingOn(mode)}` : enterMainScreenTerminalSequence
}

export function exitTerminalCleanupSequenceForMode(mode: MouseCaptureMode = 'off'): string {
  return `${mouseTrackingOff()}\u001B[0m${showCursor}\r\u001B[2K${mode === 'full' ? exitAlternateScreen : ''}`
}

export function cleanupTerminalForExit(stdout: Pick<NodeJS.WriteStream, 'write'>, mode: MouseCaptureMode = 'off'): void {
  stdout.write(exitTerminalCleanupSequenceForMode(mode))
}

export function reassertMouseTracking(stdout: Pick<NodeJS.WriteStream, 'write'>, mode: MouseCaptureMode = 'off'): void {
  const sequence = mouseTrackingOn(mode)
  if (sequence) stdout.write(sequence)
}
