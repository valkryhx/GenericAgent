export type TerminalCursorPosition = {
  row: number
  column: number
}

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
