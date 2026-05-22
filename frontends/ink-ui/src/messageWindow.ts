import type { ChatMessage } from './protocol.js'
import { formatAssistantText } from './messageFormat.js'

export type TranscriptLine = {
  id: string
  text: string
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

export function transcriptLines(messages: ChatMessage[], options: { expandedTools: boolean }): TranscriptLine[] {
  const rows: TranscriptLine[] = []
  for (const message of messages) {
    appendMessageLines(rows, message, options.expandedTools)
  }
  return rows
}

export function wrapTranscriptLines(lines: TranscriptLine[], columns: number): TranscriptLine[] {
  const width = Math.max(1, Math.floor(columns))
  const wrapped: TranscriptLine[] = []

  for (const line of lines) {
    const chunks = wrapText(line.text, width)
    chunks.forEach((chunk, index) => {
      wrapped.push({
        ...line,
        id: index === 0 ? line.id : `${line.id}-wrap-${index}`,
        text: chunk,
      })
    })
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
  return visibleTranscriptLines(lines, { maxRows, scrollOffset: 0 }).lines
}

export function clampTranscriptScrollOffset(offset: number, totalRows: number, maxRows: number): number {
  const maxScrollOffset = Math.max(0, Math.floor(totalRows) - Math.max(1, Math.floor(maxRows)))
  return Math.min(maxScrollOffset, Math.max(0, Math.floor(offset)))
}

function appendMessageLines(rows: TranscriptLine[], message: ChatMessage, expandedTools: boolean): void {
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
        color: 'black',
        backgroundColor: '#d7d7d7',
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
  splitDisplayLines(body).forEach((line, index) => {
    rows.push({
      id: `${message.id}-${index}`,
      text: index === 0 ? `✻ ${line}` : `  ${line}`,
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

function wrapText(text: string, width: number): string[] {
  if (text.length <= width) return [text]
  const chunks: string[] = []
  for (let index = 0; index < text.length; index += width) {
    chunks.push(text.slice(index, index + width))
  }
  return chunks.length === 0 ? [' '] : chunks
}

function blankLine(id: string): TranscriptLine {
  return { id, text: ' ' }
}
