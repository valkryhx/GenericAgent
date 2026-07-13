export type TranscriptScrollbar = {
  thumbStart: number
  thumbSize: number
  trackRows: number
  visible: boolean
}

export type TranscriptScrollbarCell = {
  active: boolean
  text: string
}

export function transcriptScrollbar(input: {
  totalRows: number
  viewportRows: number
  scrollOffset: number
}): TranscriptScrollbar {
  const totalRows = Math.max(0, Math.floor(input.totalRows))
  const viewportRows = Math.max(1, Math.floor(input.viewportRows))
  const trackRows = viewportRows
  const maxScrollOffset = Math.max(0, totalRows - viewportRows)
  if (maxScrollOffset === 0) {
    return { thumbStart: 0, thumbSize: trackRows, trackRows, visible: false }
  }

  const thumbSize = Math.max(1, Math.floor((viewportRows / totalRows) * trackRows))
  const maxThumbStart = Math.max(0, trackRows - thumbSize)
  const clampedOffset = Math.min(maxScrollOffset, Math.max(0, Math.floor(input.scrollOffset)))
  const topRow = maxScrollOffset - clampedOffset
  const thumbStart = Math.round((topRow / maxScrollOffset) * maxThumbStart)

  return { thumbStart, thumbSize, trackRows, visible: true }
}

export function transcriptScrollbarLines(input: {
  totalRows: number
  viewportRows: number
  scrollOffset: number
}): string[] {
  return transcriptScrollbarCells(input).map(cell => cell.text)
}

export function transcriptScrollbarCells(input: {
  totalRows: number
  viewportRows: number
  scrollOffset: number
}): TranscriptScrollbarCell[] {
  const scrollbar = transcriptScrollbar(input)
  return Array.from({ length: scrollbar.trackRows }, (_, index) => (
    {
      active: scrollbar.visible && index >= scrollbar.thumbStart && index < scrollbar.thumbStart + scrollbar.thumbSize,
      text: scrollbar.visible ? '▐' : ' ',
    }
  ))
}

export function scrollOffsetForScrollbarClick(input: {
  totalRows: number
  viewportRows: number
  y: number
  viewportTop?: number
}): number | null {
  const totalRows = Math.max(0, Math.floor(input.totalRows))
  const viewportRows = Math.max(1, Math.floor(input.viewportRows))
  const maxScrollOffset = Math.max(0, totalRows - viewportRows)
  if (maxScrollOffset === 0) return null

  const scrollbar = transcriptScrollbar({ totalRows, viewportRows, scrollOffset: 0 })
  if (!scrollbar.visible) return null

  const viewportTop = Math.max(1, Math.floor(input.viewportTop ?? 1))
  const row = Math.max(0, Math.min(viewportRows - 1, Math.floor(input.y) - viewportTop))
  const maxThumbStart = Math.max(1, scrollbar.trackRows - scrollbar.thumbSize)
  const topRow = Math.round((row / maxThumbStart) * maxScrollOffset)
  return Math.max(0, Math.min(maxScrollOffset, maxScrollOffset - topRow))
}

export function isScrollbarColumn(x: number, columns: number): boolean {
  const lastColumn = Math.max(1, Math.floor(columns))
  const column = Math.floor(x)
  return column <= lastColumn && column >= Math.max(1, lastColumn - 1)
}

export function shouldHandleScrollbarDrag(input: {
  kind: 'press' | 'drag'
  x: number
  columns: number
  dragging: boolean
}): boolean {
  if (input.kind === 'drag') return input.dragging
  return isScrollbarColumn(input.x, input.columns)
}
