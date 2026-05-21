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

export function inputVisibleRowCount(input: string, maxRows = DEFAULT_MAX_INPUT_ROWS): number {
  return Math.min(input.split('\n').length, Math.max(1, Math.floor(maxRows)))
}
