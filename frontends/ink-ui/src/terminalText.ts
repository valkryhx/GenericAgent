import stringWidth from 'string-width'

export type TerminalSegment = {
  text: string
  startOffset: number
  endOffset: number
  width: number
}

export type WrappedTerminalLine = {
  text: string
  width: number
  startOffset: number
  endOffset: number
}

function clampedOffset(text: string, offset: number): number {
  return Math.max(0, Math.min(text.length, Math.floor(offset)))
}

export function terminalSegments(text: string): TerminalSegment[] {
  if (!text) return []
  if (typeof Intl !== 'undefined' && 'Segmenter' in Intl) {
    const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' })
    return Array.from(segmenter.segment(text), part => ({
      text: part.segment,
      startOffset: part.index,
      endOffset: part.index + part.segment.length,
      width: Math.max(0, stringWidth(part.segment)),
    }))
  }

  const segments: TerminalSegment[] = []
  let offset = 0
  for (const segment of Array.from(text)) {
    segments.push({
      text: segment,
      startOffset: offset,
      endOffset: offset + segment.length,
      width: Math.max(0, stringWidth(segment)),
    })
    offset += segment.length
  }
  return segments
}

export function clampGraphemeOffset(text: string, offset: number): number {
  const target = clampedOffset(text, offset)
  let boundary = 0
  for (const segment of terminalSegments(text)) {
    if (segment.endOffset > target) break
    boundary = segment.endOffset
  }
  return boundary
}

export function previousGraphemeOffset(text: string, offset: number): number {
  const target = clampedOffset(text, offset)
  for (const segment of terminalSegments(text)) {
    if (segment.endOffset >= target) return segment.startOffset
  }
  return text.length
}

export function nextGraphemeOffset(text: string, offset: number): number {
  const target = clampedOffset(text, offset)
  for (const segment of terminalSegments(text)) {
    if (segment.endOffset > target) return segment.endOffset
  }
  return text.length
}

export function terminalTextWidth(text: string): number {
  return Math.max(0, stringWidth(text))
}

export function wrapTerminalText(text: string, columns: number): WrappedTerminalLine[] {
  const maxColumns = Math.max(1, Math.floor(columns))
  const wrapped: WrappedTerminalLine[] = []
  const logicalLines = text.split('\n')
  let logicalStart = 0

  logicalLines.forEach((logicalLine, logicalIndex) => {
    const segments = terminalSegments(logicalLine)
    if (segments.length === 0) {
      wrapped.push({ text: '', width: 0, startOffset: logicalStart, endOffset: logicalStart })
    } else {
      let currentText = ''
      let currentWidth = 0
      let currentStart = logicalStart
      let currentEnd = logicalStart

      const flush = () => {
        wrapped.push({
          text: currentText,
          width: currentWidth,
          startOffset: currentStart,
          endOffset: currentEnd,
        })
        currentText = ''
        currentWidth = 0
      }

      for (const segment of segments) {
        const segmentText = segment.width > maxColumns ? '…' : segment.text
        const segmentWidth = segment.width > maxColumns ? 1 : segment.width
        if (currentText && currentWidth + segmentWidth > maxColumns) flush()
        if (!currentText) currentStart = logicalStart + segment.startOffset
        currentText += segmentText
        currentWidth += segmentWidth
        currentEnd = logicalStart + segment.endOffset
      }
      if (currentText || currentWidth === 0) flush()
    }

    logicalStart += logicalLine.length
    if (logicalIndex < logicalLines.length - 1) logicalStart += 1
  })

  return wrapped
}
