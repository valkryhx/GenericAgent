export type TranscriptScrollbar = {
  thumbStart: number
  thumbSize: number
  trackRows: number
  visible: boolean
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
