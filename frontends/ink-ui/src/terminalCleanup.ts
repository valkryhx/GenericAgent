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
