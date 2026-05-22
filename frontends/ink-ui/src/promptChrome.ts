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

export function inputPrompt(input: string): string {
  return `> ${input}`
}

export function inputPromptLines(input: string, maxRows = DEFAULT_MAX_INPUT_ROWS): string[] {
  const rows = input.split('\n')
  const visibleRows = Math.max(1, Math.floor(maxRows))
  const start = Math.max(0, rows.length - visibleRows)
  return rows.slice(start).map((line, index) => `${start + index === 0 ? '> ' : '  '}${line}`)
}

export type InputLineItem = {
  text: string
  cursorColumn?: number
}

export type InputLinePart = {
  text: string
  inverse?: boolean
}

function clampCursorOffset(input: string, cursorOffset: number): number {
  return Math.max(0, Math.min(input.length, Math.floor(cursorOffset)))
}

export function inputPromptLineItems(input: string, maxRows = DEFAULT_MAX_INPUT_ROWS, cursorOffset = input.length): InputLineItem[] {
  const rows = input.split('\n')
  const visibleRows = Math.max(1, Math.floor(maxRows))
  const offset = clampCursorOffset(input, cursorOffset)
  let rowStart = 0
  let cursorRow = rows.length - 1
  let cursorColumnInRow = rows[cursorRow]?.length ?? 0

  for (let index = 0; index < rows.length; index += 1) {
    const rowEnd = rowStart + rows[index].length
    if (offset <= rowEnd || index === rows.length - 1) {
      cursorRow = index
      cursorColumnInRow = offset - rowStart
      break
    }
    rowStart = rowEnd + 1
  }

  const maxStart = Math.max(0, rows.length - visibleRows)
  const start = Math.min(Math.max(0, cursorRow - visibleRows + 1), maxStart)
  return rows.slice(start, start + visibleRows).map((line, index) => {
    const logicalRow = start + index
    const prefix = logicalRow === 0 ? '> ' : '  '
    const item: InputLineItem = { text: `${prefix}${line}` }
    if (logicalRow === cursorRow) item.cursorColumn = prefix.length + cursorColumnInRow
    return item
  })
}

export function renderInputLine(line: string, showCursor: boolean, cursorColumn = line.length): InputLinePart[] {
  if (!showCursor) return [{ text: line }]
  if (!line) return [{ text: ' ', inverse: true }]
  const safeCursorColumn = Math.max(0, Math.min(line.length - 1, Math.floor(cursorColumn) - 1))
  const head = line.slice(0, safeCursorColumn)
  const cursor = line.slice(safeCursorColumn, safeCursorColumn + 1)
  const tail = line.slice(safeCursorColumn + 1)
  return [
    ...(head ? [{ text: head }] : []),
    { text: cursor, inverse: true },
    ...(tail ? [{ text: tail }] : []),
  ]
}

export function inputVisibleRowCount(input: string, maxRows = DEFAULT_MAX_INPUT_ROWS): number {
  return Math.min(input.split('\n').length, Math.max(1, Math.floor(maxRows)))
}
