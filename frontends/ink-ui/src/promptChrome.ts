import stringWidth from 'string-width'
import { clampGraphemeOffset, terminalSegments, terminalTextWidth, wrapTerminalText } from './terminalText.js'

export const inputFrameBorderStyle = {
  topLeft: '',
  top: '─',
  topRight: '',
  right: '',
  bottomRight: '',
  bottom: '─',
  bottomLeft: '',
  left: '',
} as const

const DEFAULT_MAX_INPUT_ROWS = 6
export const inputGutterColumns = 2
export const inputRightReserveColumns = 1
export const inputLeftPaddingColumns = 1

export type InputLineItem = {
  gutter: string
  text: string
  cursorColumn?: number
}

export type InputLinePart = {
  text: string
  inverse?: boolean
}
export type InputViewport = {
  lines: InputLineItem[]
  totalRows: number
  startRow: number
  cursorLine: number
  cursorColumn: number
}

export function inputContentColumns(columns: number): number {
  return Math.max(1, Math.floor(columns) - inputLeftPaddingColumns - inputGutterColumns - inputRightReserveColumns)
}

/**
 * Width of each input text row inside the padded border box.
 * Must stay constant across keystrokes so Yoga/Ink does not reflow the
 * composer left/right (horizontal "shake" while typing).
 */
export function inputLineBoxWidth(columns: number): number {
  return Math.max(1, Math.floor(columns) - inputLeftPaddingColumns)
}

/**
 * Render one input visual row as a single fixed-width string:
 * gutter + content (padded/truncated) + right reserve spaces.
 */
export function fixedInputRow(line: InputLineItem, columns: number): string {
  const contentColumns = inputContentColumns(columns)
  const body = fixedInputLine(line.text, contentColumns)
  const rightPad = Math.max(0, inputRightReserveColumns)
  return `${line.gutter}${body}${' '.repeat(rightPad)}`
}

function graphemes(text: string): string[] {
  return terminalSegments(text).map(segment => segment.text)
}

export function inputViewport(
  input: string,
  options: { columns: number; maxRows?: number; cursorOffset?: number },
): InputViewport {
  const wrapped = wrapTerminalText(input, options.columns)
  const totalRows = wrapped.length
  const visibleRows = Math.max(1, Math.floor(options.maxRows ?? DEFAULT_MAX_INPUT_ROWS))
  const cursorOffset = clampGraphemeOffset(input, options.cursorOffset ?? input.length)
  let cursorRow = 0

  for (let index = 0; index < wrapped.length; index += 1) {
    if (wrapped[index]!.startOffset <= cursorOffset) cursorRow = index
  }

  const cursorVisualRow = wrapped[cursorRow]!
  const cursorColumn = Math.min(
    cursorVisualRow.width,
    terminalTextWidth(input.slice(cursorVisualRow.startOffset, Math.min(cursorOffset, cursorVisualRow.endOffset))),
  )
  const maxStartRow = Math.max(0, totalRows - visibleRows)
  const startRow = Math.min(Math.max(0, cursorRow - visibleRows + 1), maxStartRow)
  const visible = wrapped.slice(startRow, startRow + visibleRows)
  const cursorLine = cursorRow - startRow
  const lines = visible.map((line, index): InputLineItem => {
    const visualRow = startRow + index
    return {
      gutter: visualRow === 0 ? '> ' : '  ',
      text: line.text,
      ...(visualRow === cursorRow ? { cursorColumn } : {}),
    }
  })

  return { lines, totalRows, startRow, cursorLine, cursorColumn }
}

export function renderInputLine(line: string, showCursor: boolean, cursorColumn = line.length): InputLinePart[] {
  if (!showCursor) return [{ text: line }]
  if (!line) return [{ text: ' ', inverse: true }]
  const targetColumn = Math.max(1, Math.floor(cursorColumn))
  let head = ''
  let cursor = ''
  let tail = ''
  let width = 0
  let cursorFound = false
  for (const segment of graphemes(line)) {
    if (cursorFound) {
      tail += segment
      continue
    }
    const nextWidth = width + Math.max(0, stringWidth(segment))
    if (nextWidth >= targetColumn) {
      cursor = segment
      cursorFound = true
    } else {
      head += segment
      width = nextWidth
    }
  }
  if (!cursor) {
    const parts = graphemes(line)
    cursor = parts.pop() ?? ' '
    head = parts.join('')
    tail = ''
  }
  return [
    ...(head ? [{ text: head }] : []),
    { text: cursor, inverse: true },
    ...(tail ? [{ text: tail }] : []),
  ]
}

export function fixedInputLine(line: string, columns: number): string {
  const targetWidth = Math.max(1, Math.floor(columns))
  let rendered = ''
  let width = 0
  for (const segment of graphemes(line)) {
    const segmentWidth = Math.max(0, stringWidth(segment))
    if (width + segmentWidth > targetWidth) break
    rendered += segment
    width += segmentWidth
  }
  return rendered + ' '.repeat(Math.max(0, targetWidth - width))
}
