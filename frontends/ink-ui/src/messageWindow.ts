import type { ChatMessage } from './protocol.js'
import { formatAssistantText } from './messageFormat.js'
import { renderMarkdownLines, type MarkdownPart } from './markdownRender.js'
import type { InkTheme } from './theme.js'
import { terminalSegments, terminalTextWidth } from './terminalText.js'

export type TranscriptPart = MarkdownPart & {
  backgroundColor?: string
}

export type TranscriptLine = {
  id: string
  text: string
  parts?: TranscriptPart[]
  color?: string
  backgroundColor?: string
}

export type VisibleTranscriptLines = {
  lines: TranscriptLine[]
  totalRows: number
  maxScrollOffset: number
  scrollOffset: number
}

export function visibleMessages(messages: ChatMessage[], maxMessages = 80): ChatMessage[] {
  return messages.slice(-maxMessages)
}

export function tailLines(text: string, maxLines: number): string {
  const lines = (text || '').split('\n')
  if (lines.length <= maxLines) return text
  const omitted = lines.length - maxLines
  return [`... ${omitted} earlier lines omitted ...`, ...lines.slice(-maxLines)].join('\n')
}

export function assistantDisplayText(
  text: string,
  options: {
    expanded: boolean
    done: boolean
    maxExpandedLines?: number
  },
): string {
  if (options.expanded) {
    return tailLines(text, options.maxExpandedLines ?? (options.done ? 80 : 40))
  }
  return tailLines(text, options.done ? 30 : 18)
}

export function estimateMessageRows(message: ChatMessage, expandedTools: boolean): number {
  if (message.role === 'assistant') {
    return Math.max(1, assistantDisplayText(message.text, { expanded: expandedTools, done: message.done }).split('\n').length + 1)
  }
  return Math.max(1, (message.text || ' ').split('\n').length)
}

export function visibleMessagesForViewport(
  messages: ChatMessage[],
  options: { maxRows: number; expandedTools: boolean; scrollOffset?: number },
): ChatMessage[] {
  const maxRows = Math.max(1, Math.floor(options.maxRows))
  const messageRows = messages.map(message => estimateMessageRows(message, options.expandedTools))
  const totalRows = messageRows.reduce((sum, rows) => sum + rows, 0)
  const maxScrollOffset = Math.max(0, totalRows - maxRows)
  let remainingScrollOffset = Math.min(Math.max(0, Math.floor(options.scrollOffset ?? 0)), maxScrollOffset)
  let endIndex = messages.length

  while (endIndex > 0 && remainingScrollOffset > 0) {
    const rows = messageRows[endIndex - 1] ?? 1
    if (remainingScrollOffset < rows) break
    remainingScrollOffset -= rows
    endIndex -= 1
  }

  const selected: ChatMessage[] = []
  let rows = 0

  for (let index = endIndex - 1; index >= 0; index--) {
    const message = messages[index]!
    const rowsForMessage = messageRows[index] ?? estimateMessageRows(message, options.expandedTools)
    if (selected.length > 0 && rows + rowsForMessage > maxRows) break
    selected.unshift(message)
    rows += rowsForMessage
  }

  return selected
}

export function transcriptLines(messages: ChatMessage[], options: { expandedTools: boolean; theme?: InkTheme }): TranscriptLine[] {
  const rows: TranscriptLine[] = []
  for (const message of messages) {
    appendMessageLines(rows, message, options.expandedTools, options.theme)
  }
  return rows
}

export function wrapTranscriptLines(lines: TranscriptLine[], columns: number): TranscriptLine[] {
  const width = Math.max(1, Math.floor(columns))
  const wrapped: TranscriptLine[] = []

  for (const line of lines) {
    wrapped.push(...wrapStyledLine(line, width))
  }

  return wrapped
}

export function visibleTranscriptLines(
  lines: TranscriptLine[],
  options: { maxRows: number; scrollOffset?: number },
): VisibleTranscriptLines {
  const maxRows = Math.max(1, Math.floor(options.maxRows))
  const totalRows = lines.length
  const scrollOffset = clampTranscriptScrollOffset(options.scrollOffset ?? 0, totalRows, maxRows)
  const end = Math.max(0, totalRows - scrollOffset)
  const start = Math.max(0, end - maxRows)

  return {
    lines: lines.slice(start, end),
    totalRows,
    maxScrollOffset: Math.max(0, totalRows - maxRows),
    scrollOffset,
  }
}

export function liveTranscriptViewportLines(lines: TranscriptLine[], maxRows: number): TranscriptLine[] {
  const max = Math.max(1, Math.floor(maxRows))
  if (lines.length <= max) return lines

  // Pin the *last* user-prompt block (open/current turn question) so a long assistant
  // tail cannot hide it — without re-pinning older historical user lines.
  let pinStart = -1
  let pinEnd = -1
  for (let i = 0; i < lines.length; ) {
    if (lines[i]!.id.startsWith('u-')) {
      const start = i
      while (i < lines.length && lines[i]!.id.startsWith('u-')) i += 1
      pinStart = start
      pinEnd = i
    } else {
      i += 1
    }
  }

  if (pinStart < 0) {
    return visibleTranscriptLines(lines, { maxRows: max, scrollOffset: 0 }).lines
  }

  const pinned = lines.slice(pinStart, pinEnd)
  const after = lines.slice(pinEnd)
  const pinnedCount = Math.min(pinned.length, Math.max(0, max - 1))
  const keptPinned = pinned.slice(0, pinnedCount)
  const restBudget = Math.max(0, max - keptPinned.length)
  const restTail = after.length <= restBudget ? after : after.slice(after.length - restBudget)
  return [...keptPinned, ...restTail]
}

export function clampTranscriptScrollOffset(offset: number, totalRows: number, maxRows: number): number {
  const maxScrollOffset = Math.max(0, Math.floor(totalRows) - Math.max(1, Math.floor(maxRows)))
  return Math.min(maxScrollOffset, Math.max(0, Math.floor(offset)))
}

function appendMessageLines(rows: TranscriptLine[], message: ChatMessage, expandedTools: boolean, theme?: InkTheme): void {
  if (message.localCommand === 'input') {
    appendPlainLines(rows, message, message.text || ' ')
    return
  }
  if (message.localCommand === 'output') {
    appendPlainLines(rows, message, message.text || ' ', { color: 'gray', prefix: '  ', appendBlank: true })
    return
  }
  if (message.role === 'user') {
    const lines = splitDisplayLines(message.text || ' ')
    lines.forEach((line, index) => {
      rows.push({
        id: `${message.id}-${index}`,
        text: index === 0 ? `> ${line}` : `  ${line}`,
        color: theme?.userText ?? 'black',
        backgroundColor: theme?.userBackground ?? '#d7d7d7',
      })
    })
    rows.push(blankLine(`${message.id}-blank`))
    return
  }
  if (message.role === 'system') {
    appendPlainLines(rows, message, message.text || ' ', { color: 'gray', appendBlank: true })
    return
  }

  const body = formatAssistantText(message.text, { expanded: expandedTools }) || ' '
  const markdownLines = renderMarkdownLines(body, theme)
  markdownLines.forEach((line, index) => {
    // Do NOT prefix assistant body with ✻ — that glyph is reserved for the activity
    // status line (formatRunningStatus / Thinking…). Stream-commit segments used to
    // re-apply ✻ on every a-*-c* chunk, so it wrongly appeared many times in content.
    const prefix = index === 0 ? '' : '  '
    rows.push({
      id: `${message.id}-${index}`,
      text: prefix ? `${prefix}${line.text}` : line.text,
      parts: prefix ? [{ text: prefix }, ...line.parts] : line.parts,
    })
  })
  rows.push(blankLine(`${message.id}-blank`))
}

function appendPlainLines(
  rows: TranscriptLine[],
  message: ChatMessage,
  text: string,
  options: { color?: string; prefix?: string; appendBlank?: boolean } = {},
): void {
  const prefix = options.prefix ?? ''
  splitDisplayLines(text).forEach((line, index) => {
    rows.push({
      id: `${message.id}-${index}`,
      text: `${prefix}${line}`,
      color: options.color,
    })
  })
  if (options.appendBlank) rows.push(blankLine(`${message.id}-blank`))
}

function splitDisplayLines(text: string): string[] {
  const lines = text.split('\n')
  return lines.length === 0 ? [' '] : lines.map(line => line || ' ')
}

function lineParts(line: TranscriptLine): TranscriptPart[] {
  return line.parts ?? [{ text: line.text, color: line.color, backgroundColor: line.backgroundColor }]
}

function wrapStyledLine(line: TranscriptLine, width: number): TranscriptLine[] {
  if (terminalTextWidth(line.text) <= width) return [line]
  const wrapped: TranscriptLine[] = []
  let currentText = ''
  let currentWidth = 0
  let currentParts: TranscriptPart[] = []

  const flush = () => {
    wrapped.push({
      ...line,
      id: wrapped.length === 0 ? line.id : `${line.id}-wrap-${wrapped.length}`,
      text: currentText || ' ',
      parts: currentParts.length > 0 ? currentParts : [{ text: ' ' }],
    })
    currentText = ''
    currentWidth = 0
    currentParts = []
  }

  for (const part of lineParts(line)) {
    for (const segment of terminalSegments(part.text)) {
      const segmentText = segment.width > width ? '…' : segment.text
      const segmentWidth = segment.width > width ? 1 : segment.width
      if (currentText && currentWidth + segmentWidth > width) flush()
      currentText += segmentText
      currentWidth += segmentWidth
      const last = currentParts[currentParts.length - 1]
      const sameStyle = last
        && last.color === part.color
        && last.backgroundColor === part.backgroundColor
        && last.bold === part.bold
        && last.italic === part.italic
        && last.underline === part.underline
        && last.dimColor === part.dimColor
      if (sameStyle) {
        last.text += segmentText
      } else {
        currentParts.push({ ...part, text: segmentText })
      }
    }
  }
  if (currentText || currentParts.length === 0) flush()
  return wrapped
}

function blankLine(id: string): TranscriptLine {
  return { id, text: ' ' }
}
