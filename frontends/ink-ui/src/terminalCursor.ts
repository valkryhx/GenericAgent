export type TerminalCursorPosition = {
  row: number
  column: number
}

export const saveCursorPosition = '\x1b[s'
export const restoreCursorPosition = '\x1b[u'

export type InputCursorPositionInput = {
  headerRows: number
  messageRows: number
  activityRows: number
  errorRows: number
  panelRows: number
  hintRows: number
  inputBorderTopRows: number
  inputPaddingLeftColumns: number
  inputGutterColumns: number
  inputCursorLine: number
  inputCursorColumn: number
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, Math.floor(value)))
}

export function cursorPosition(row: number, column: number, rows: number, columns: number): string {
  const clampedRow = clamp(row, 0, Math.max(0, rows - 1))
  const clampedColumn = clamp(column, 0, Math.max(0, columns - 1))
  return `\x1b[${clampedRow + 1};${clampedColumn + 1}H`
}

/**
 * Cursor position *relative to the live Ink band* (not absolute screen).
 *
 * Ink owns stdout and redraws the live region with its own cursor bookkeeping.
 * Absolute-screen CUP (Codex-style against the full terminal) races Ink and can
 * leave a second ghost composer (截图/严重bug.png). Park the IME caret with
 * relative geometry + save/restore so Ink's next frame starts from a known spot.
 */
export function inputCursorPosition(input: InputCursorPositionInput): TerminalCursorPosition {
  return {
    row: input.headerRows
      + input.messageRows
      + input.activityRows
      + input.errorRows
      + input.panelRows
      + input.hintRows
      + input.inputBorderTopRows
      + input.inputCursorLine,
    column: input.inputPaddingLeftColumns + input.inputGutterColumns + input.inputCursorColumn,
  }
}
