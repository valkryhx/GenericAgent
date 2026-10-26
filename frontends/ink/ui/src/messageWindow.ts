import type { ChatMessage } from './protocol.js'
import { formatAssistantText, STDOUT_BODY_END, STDOUT_BODY_PREFIX } from './messageFormat.js'
import { renderMarkdownLines, type MarkdownPart } from './markdownRender.js'
import type { InkTheme } from './theme.js'
import { GLYPHS, RESULT_PREFIX, normalizeEmoji } from './glyphs.js'
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
  return transcriptLinesWithChannel(messages, options).lines
}

/**
 * Render a transcript segment and return the result-channel state at its end.
 * The inline UI renders Static and live messages in separate calls; carrying
 * this state across the boundary keeps a `[Stdout]` block dim when a stream
 * commit lands exactly between the marker and its body.
 */
export function transcriptLinesWithChannel(
  messages: ChatMessage[],
  options: { expandedTools: boolean; theme?: InkTheme; initialChannel?: ResultChannel },
): { lines: TranscriptLine[]; channel: ResultChannel } {
  const rows: TranscriptLine[] = []
  let channel: ResultChannel = options.initialChannel ?? null

  for (const message of messages) {
    // Render and compact each message independently. <Static> appends by item
    // count, so an already-emitted row must never be removed or reordered when
    // the next stream-commit segment arrives. Collapsing across messages used
    // to delete the previous segment's trailing blank, shrinking the item
    // prefix by one and making Ink skip the next segment's first row.
    const messageRows: TranscriptLine[] = []
    channel = appendMessageLines(messageRows, message, options.expandedTools, options.theme, channel)
    const compacted = collapseResultSeparators(messageRows)
    // The separator belongs to the message that follows it. This is what keeps
    // Static append-only: once a message is emitted we never remove its last
    // row to make room for the next one. Consecutive tool-result rows stay
    // contiguous (Status -> Stdout across a stream-commit split).
    if (
      rows.length > 0
      && compacted.length > 0
      && rows[rows.length - 1]!.text.trim() !== ''
      && !bothResultBlocks(rows, compacted)
    ) {
      rows.push(blankLine(`boundary-${message.id}`))
    }
    rows.push(...compacted)
  }
  // Deliberately no synthetic trailing blank: the next stream-commit segment
  // may continue a result/stdout block. Separators are emitted at the start of
  // the *following* block, which keeps every rendered prefix append-only.
  return { lines: rows, channel }
}

export type ResultChannel = { kind: 'stdout'; needsMarker: boolean } | null

function isResultRow(row: TranscriptLine): boolean {
  return row.text.startsWith(RESULT_PREFIX) || row.text.startsWith(TOOL_RESULT_CONTINUATION)
}

function firstNonBlank(rows: TranscriptLine[]): TranscriptLine | null {
  return rows.find(row => row.text.trim() !== '') ?? null
}

function lastNonBlank(rows: TranscriptLine[]): TranscriptLine | null {
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i]!.text.trim() !== '') return rows[i]!
  }
  return null
}

/** True when the previous block and the next block are both result rows. */
function bothResultBlocks(previous: TranscriptLine[], next: TranscriptLine[]): boolean {
  const prev = lastNonBlank(previous)
  const following = firstNonBlank(next)
  return Boolean(prev && following && isResultRow(prev) && isResultRow(following))
}

/**
 * Tool-result rows are one visual block. GA emits a blank line between each
 * `[Status]` line and stream-commit adds a trailing blank per segment; CC 303
 * keeps consecutive result rows contiguous. Remove only blank rows whose
 * nearest non-blank neighbours are both result rows, so prose spacing and
 * blank lines inside stdout bodies remain intact.
 */
function collapseResultSeparators(rows: TranscriptLine[]): TranscriptLine[] {
  const out: TranscriptLine[] = []
  let lastContentWasResult = false
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i]!
    if (row.text.trim() !== '') {
      out.push(row)
      lastContentWasResult = isResultRow(row)
      continue
    }
    let next = i + 1
    while (next < rows.length && rows[next]!.text.trim() === '') next += 1
    const nextIsResult = next < rows.length && isResultRow(rows[next]!)
    if (lastContentWasResult && nextIsResult) {
      i = next - 1
      continue
    }
    out.push(row)
  }
  return out
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

  // Pin the *last* user-prompt block (open/current turn question) so a long
  // assistant tail cannot hide it. The blank after the prompt belongs to that
  // header, but assistant output must keep at least one row.
  let pinStart = -1
  let pinEnd = -1
  for (let i = 0; i < lines.length; ) {
    if (lines[i]!.id.startsWith('u-')) {
      pinStart = i
      while (i < lines.length && lines[i]!.id.startsWith('u-')) i += 1
      pinEnd = i
    } else {
      i += 1
    }
  }

  if (pinStart < 0) {
    return visibleTranscriptLines(lines, { maxRows: max, scrollOffset: 0 }).lines
  }

  let separatorEnd = pinEnd
  if (lines[separatorEnd]?.text.trim() === '') separatorEnd += 1
  const promptHeader = lines.slice(pinStart, separatorEnd)
  const body = lines.slice(separatorEnd)

  // Never spend the entire viewport on the prompt header; keep one body row.
  const headerRows = Math.min(promptHeader.length, Math.max(0, max - 1))
  const keptHeader = promptHeader.slice(0, headerRows)
  const bodyBudget = Math.max(0, max - keptHeader.length)
  const visibleBody = body.length <= bodyBudget
    ? body
    : body.slice(Math.max(0, body.length - bodyBudget))

  return [...keptHeader, ...visibleBody]
}

export function clampTranscriptScrollOffset(offset: number, totalRows: number, maxRows: number): number {
  const maxScrollOffset = Math.max(0, Math.floor(totalRows) - Math.max(1, Math.floor(maxRows)))
  return Math.min(maxScrollOffset, Math.max(0, Math.floor(offset)))
}

function appendMessageLines(
  rows: TranscriptLine[],
  message: ChatMessage,
  expandedTools: boolean,
  theme?: InkTheme,
  channel: ResultChannel = null,
): ResultChannel {
  if (message.localCommand === 'input') {
    appendPlainLines(rows, message, message.text || ' ')
    return null
  }
  if (message.localCommand === 'output') {
    appendPlainLines(rows, message, message.text || ' ', { color: 'gray', prefix: '  ', appendBlank: true })
    return null
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
    // The prompt is a complete visual block: the blank after it is stable and
    // can never need removal when an assistant commit arrives later.
    rows.push(blankLine(`${message.id}-blank`))
    return null
  }
  if (message.role === 'system') {
    appendPlainLines(rows, message, message.text || ' ', { color: 'gray', appendBlank: true })
    return null
  }

  const body = formatAssistantText(message.text, { expanded: expandedTools, streaming: !message.done }) || ' '
  const nextChannel = appendAssistantBodyRows(rows, message.id, body, theme, channel)
  // A plain prose block gets its trailing blank immediately: it is stable and
  // gives the composer breathing room at the end of the transcript. A result
  // block must NOT get one, because the next stream-commit segment can still
  // continue the same tool result (`[Status]` then `[Stdout]` body). For
  // result -> prose the separator is added at the start of the next block.
  const last = lastNonBlank(rows)
  if (!message.done) {
    // Live message: reserve one row below the body so the viewport tail does not
    // sit flush against the composer. This row is not committed to Static.
    rows.push(blankLine(`${message.id}-streaming-tail`))
  } else if (!nextChannel && !(last && isResultRow(last))) {
    rows.push(blankLine(`${message.id}-blank`))
  }
  return nextChannel
}

/**
 * GA tool progress lines ([Action]/[Status]/[Stdout]) become CC-style result
 * lines: `  ⎿  <body>` (dim). Matches even when markdown left a blockquote
 * marker or residual indent: "  [Status] ...", "| [Action] ...".
 *
 * `[Stdout]` is a block marker: its body (including `[omitted long output]`)
 * belongs to the same dim result channel. Previously only the marker line was
 * styled, so stdout rendered as ordinary black Markdown and left an empty `⎿`.
 */
const RESULT_MARKER_RE = /^\s*(?:\|\s*)?\[(Action|Status|Stdout)\]\s?(.*)$/
const TOOL_RESULT_CONTINUATION = `      `

function resultMarkerBody(text: string): { kind: string; body: string } | null {
  const match = RESULT_MARKER_RE.exec(text)
  if (!match) return null
  return { kind: match[1]!, body: match[2] ?? '' }
}

function mutedResultLine(body: string, theme?: InkTheme): TranscriptPart[] {
  return [{ text: `${RESULT_PREFIX}${body}`, color: theme?.muted ?? 'gray', dimColor: true }]
}

function appendResultBodyRow(rows: TranscriptLine[], id: string, body: string, theme?: InkTheme): void {
  // CC 303 renders tool output as a child block under the `⎿` result row
  // (marginLeft 5, dimColor). Keep blank bodies empty rather than emitting an
  // isolated, content-free `⎿`.
  if (!body.trim()) return
  rows.push({
    id,
    text: `${TOOL_RESULT_CONTINUATION}${normalizeEmoji(body.trimEnd())}`,
    parts: [{ text: `${TOOL_RESULT_CONTINUATION}${normalizeEmoji(body.trimEnd())}`, color: theme?.muted ?? 'gray', dimColor: true }],
  })
}

/** A bare closing fence whose opener was committed to an earlier Static segment. */
function isOrphanClosingFence(line: string): boolean {
  return /^\s*`{3,}\s*$/.test(line)
}

function isStepBoundary(line: string): boolean {
  // Only a real runtime step header may end a stdout block. Matching generic
  // "Summary:" or "Step ..." text would misclassify legitimate command output.
  const trimmed = line.trim()
  return /^\*\*LLM Running \(Step \d+\) \.\.\.\*\*$/.test(trimmed)
    || /^\*\*Step \d+ \.\.\.\*\*$/.test(trimmed)
    || /^LLM Running \(Step \d+\) \.\.\.$/.test(trimmed)
    || /^Step \d+ \.\.\.$/.test(trimmed)
}

function appendAssistantBodyRows(
  rows: TranscriptLine[],
  messageId: string,
  body: string,
  theme?: InkTheme,
  channel: ResultChannel = null,
): ResultChannel {
  const lines = body.split('\n')
  let plain: string[] = []
  let rowIndex = 0

  const flushPlain = () => {
    if (plain.length === 0) return
    const source = plain.join('\n')
    plain = []
    renderMarkdownLines(source, theme).forEach(line => {
      const styled = styleAssistantTranscriptLine(line.parts ?? [{ text: line.text }], theme)
      rows.push({
        id: `${messageId}-${rowIndex++}`,
        text: styled.map(part => part.text).join('') || ' ',
        parts: styled,
      })
    })
  }

  let inStdout = channel?.kind === 'stdout'
  let stdoutNeedsMarker = channel?.needsMarker ?? false
  for (const line of lines) {
    if (line === STDOUT_BODY_END) {
      flushPlain()
      inStdout = false
      stdoutNeedsMarker = false
      continue
    }
    if (inStdout && isStepBoundary(line)) {
      flushPlain()
      inStdout = false
      stdoutNeedsMarker = false
    }
    if (inStdout && isOrphanClosingFence(line)) {
      // The opening fence may already be in Static; the remaining closing fence
      // still means the tool result ended. Drop it and let following prose
      // return to the normal Markdown channel.
      flushPlain()
      inStdout = false
      stdoutNeedsMarker = false
      continue
    }
    if (line.startsWith(STDOUT_BODY_PREFIX)) {
      const unprefixed = line.slice(STDOUT_BODY_PREFIX.length)
      if (inStdout && isStepBoundary(unprefixed)) {
        inStdout = false
        stdoutNeedsMarker = false
        plain.push(unprefixed)
        continue
      }
      flushPlain()
      // Prefixed rows are authoritative stdout body rows even when the marker
      // lived in an earlier stream-commit segment.
      inStdout = true
      const stdoutBody = normalizeEmoji(unprefixed.trimEnd())
      if (!stdoutBody) continue
      if (stdoutNeedsMarker) {
        rows.push({
          id: `${messageId}-${rowIndex++}`,
          text: `${RESULT_PREFIX}${stdoutBody}`,
          parts: mutedResultLine(stdoutBody, theme),
        })
        stdoutNeedsMarker = false
      } else {
        appendResultBodyRow(rows, `${messageId}-${rowIndex++}`, stdoutBody, theme)
      }
      continue
    }
    const marker = resultMarkerBody(line)
    if (marker) {
      flushPlain()
      inStdout = marker.kind === 'Stdout'
      stdoutNeedsMarker = inStdout
      const markerBody = normalizeEmoji(marker.body.trimEnd())
      if (markerBody) {
        rows.push({
          id: `${messageId}-${rowIndex++}`,
          text: `${RESULT_PREFIX}${markerBody}`,
          parts: mutedResultLine(markerBody, theme),
        })
        stdoutNeedsMarker = false
      } else if (!inStdout) {
        rows.push({
          id: `${messageId}-${rowIndex++}`,
          text: `  ${GLYPHS.resultPrefix}`,
          parts: [{ text: `  ${GLYPHS.resultPrefix}`, color: theme?.muted ?? 'gray', dimColor: true }],
        })
      }
      continue
    }
    if (inStdout) {
      const stdoutBody = normalizeEmoji(line.trimEnd())
      if (!stdoutBody) continue
      if (stdoutNeedsMarker) {
        // Bind the first real stdout line to the `⎿` result row, exactly like
        // CC's tool-result channel. A blank stdout body never yields a bare `⎿`.
        rows.push({
          id: `${messageId}-${rowIndex++}`,
          text: `${RESULT_PREFIX}${stdoutBody}`,
          parts: mutedResultLine(stdoutBody, theme),
        })
        stdoutNeedsMarker = false
      } else {
        appendResultBodyRow(rows, `${messageId}-${rowIndex++}`, stdoutBody, theme)
      }
      continue
    }
    plain.push(line)
  }
  flushPlain()
  return inStdout ? { kind: 'stdout', needsMarker: stdoutNeedsMarker } : null
}

function styleAssistantTranscriptLine(parts: TranscriptPart[], theme?: InkTheme): TranscriptPart[] {
  const text = parts.map(part => part.text).join('')
  const marker = resultMarkerBody(text)
  if (!marker) return parts
  const body = normalizeEmoji(marker.body.trimEnd())
  if (!body) return [{ text: `  ${GLYPHS.resultPrefix}`, color: theme?.muted ?? 'gray', dimColor: true }]
  return mutedResultLine(body, theme)
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
