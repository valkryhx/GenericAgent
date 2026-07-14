import type { ChatMessage } from './protocol.js'

/** Live stream keeps only this many trailing lines (Codex-like active_cell tail). */
export const DEFAULT_STREAM_LIVE_TAIL_LINES = 8

/**
 * When a streaming assistant exceeds `tailLines`, commit complete overflow lines as
 * done Static segments (`a-{taskId}-c{n}`) and leave only the tail in the live message.
 *
 * Codex analogy: StreamController commit ticks + sync_active_stream_tail.
 */
export function commitStreamingAssistantMessages(
  messages: ChatMessage[],
  taskId: number,
  tailLines: number = DEFAULT_STREAM_LIVE_TAIL_LINES,
): ChatMessage[] {
  const liveId = `a-${taskId}`
  const liveIndex = messages.findIndex(message => message.id === liveId && message.role === 'assistant' && !message.done)
  if (liveIndex === -1) return messages

  const live = messages[liveIndex]!
  const lines = splitLines(live.text)
  const maxTail = Math.max(1, Math.floor(tailLines))
  if (lines.length <= maxTail) return messages

  const overflowCount = lines.length - maxTail
  const overflowText = lines.slice(0, overflowCount).join('\n')
  const tailText = lines.slice(overflowCount).join('\n')
  if (!overflowText) return messages

  const existingCommits = messages.filter(
    message => message.taskId === taskId && message.role === 'assistant' && message.done && message.id.startsWith(`${liveId}-c`),
  ).length
  const commitId = `${liveId}-c${existingCommits}`

  const next = messages.slice()
  next.splice(liveIndex, 0, {
    id: commitId,
    role: 'assistant',
    text: overflowText,
    done: true,
    taskId,
  })
  const newLiveIndex = next.findIndex(message => message.id === liveId)
  next[newLiveIndex] = { ...live, text: tailText, done: false }
  return next
}

/**
 * After incremental commits, `assistant_done` may send the full turn text.
 * Strip any already-committed prefix so Static does not receive the whole body twice.
 *
 * Matching is whitespace-flexible: agentmain rewrites `</summary>` → `</summary>\n\n`
 * only on the final `done` payload, while streamed deltas keep the original spacing.
 * Exact string prefix strip would fail and re-append Turn 1 + tools on top of commits.
 */
export function remainingAssistantTextAfterCommits(fullText: string, committedPrefix: string): string {
  if (!committedPrefix) return fullText
  if (fullText.startsWith(committedPrefix)) {
    const rest = fullText.slice(committedPrefix.length)
    return rest.startsWith('\n') ? rest.slice(1) : rest
  }

  // Prefer line-prefix match (delta path may have normalized newlines slightly differently).
  const fullLines = splitLines(fullText)
  const committedLines = splitLines(committedPrefix)
  if (committedLines.length > 0 && committedLines.length <= fullLines.length) {
    let matches = true
    for (let i = 0; i < committedLines.length; i++) {
      if (fullLines[i] !== committedLines[i]) {
        matches = false
        break
      }
    }
    if (matches) return fullLines.slice(committedLines.length).join('\n')
  }

  // Whitespace-flexible: collapse runs of blank lines / trailing spaces, then re-slice
  // the original fullText at the corresponding non-empty line boundary.
  const flex = matchFlexiblePrefix(fullText, committedPrefix)
  if (flex !== null) return flex

  return fullText
}

/** Collapse blank-line runs so summary newline injection still matches streamed prefix. */
function normalizeForPrefixMatch(text: string): string {
  return text
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
}

/**
 * If `committedPrefix` is a whitespace-flexible prefix of `fullText`, return the
 * remainder of `fullText` (preserving original spacing after the matched prefix).
 * Otherwise null.
 */
function matchFlexiblePrefix(fullText: string, committedPrefix: string): string | null {
  const fullNorm = normalizeForPrefixMatch(fullText)
  const committedNorm = normalizeForPrefixMatch(committedPrefix)
  if (!committedNorm) return fullText
  if (!fullNorm.startsWith(committedNorm)) {
    // Also try non-empty-line sequence match (ignore pure blank lines entirely).
    const fullNonEmpty = nonEmptyLines(fullText)
    const committedNonEmpty = nonEmptyLines(committedPrefix)
    if (committedNonEmpty.length === 0) return fullText
    if (committedNonEmpty.length > fullNonEmpty.length) return null
    for (let i = 0; i < committedNonEmpty.length; i++) {
      if (fullNonEmpty[i] !== committedNonEmpty[i]) return null
    }
    // Map non-empty prefix length back onto original fullText lines.
    return remainderAfterNonEmptyPrefix(fullText, committedNonEmpty.length)
  }

  // Map normalized prefix length back to an index in the original fullText by walking
  // both with the same normalization rules character-by-character on newlines loosely.
  return remainderAfterNormalizedPrefix(fullText, committedNorm)
}

function nonEmptyLines(text: string): string[] {
  return splitLines(text).map(line => line.replace(/[ \t]+$/g, '')).filter(line => line.length > 0)
}

function remainderAfterNonEmptyPrefix(fullText: string, nonEmptyCount: number): string {
  const lines = splitLines(fullText)
  let seen = 0
  let idx = 0
  for (; idx < lines.length; idx++) {
    if (lines[idx]!.replace(/[ \t]+$/g, '').length > 0) {
      seen += 1
      if (seen >= nonEmptyCount) {
        idx += 1
        break
      }
    }
  }
  // Skip a single following blank line introduced only by summary rewrite (cosmetic).
  while (idx < lines.length && lines[idx] === '') idx += 1
  return lines.slice(idx).join('\n')
}

function remainderAfterNormalizedPrefix(fullText: string, committedNorm: string): string {
  // Walk original text, building the same normalization, stop when built length
  // reaches committedNorm length; return the rest of original.
  let built = ''
  let i = 0
  const src = fullText.replace(/\r\n/g, '\n')
  while (i < src.length && built.length < committedNorm.length) {
    const ch = src[i]!
    if (ch === '\n') {
      // collapse trailing spaces before newline already handled by not adding them
      // collapse 3+ newlines to \n\n in built
      if (built.endsWith('\n\n')) {
        // skip extra newlines in source while built already has \n\n
        i += 1
        continue
      }
      built += '\n'
      i += 1
      continue
    }
    if (ch === ' ' || ch === '\t') {
      // only keep spaces if not immediately before a newline (trim line ends)
      let j = i
      while (j < src.length && (src[j] === ' ' || src[j] === '\t')) j += 1
      if (j < src.length && src[j] === '\n') {
        i = j
        continue
      }
      built += ch
      i += 1
      continue
    }
    built += ch
    i += 1
  }
  if (!normalizeForPrefixMatch(built).startsWith(committedNorm) && built !== committedNorm) {
    // Fallback already handled by non-empty path; if we got here with startsWith true
    // on fullNorm, built should match. If not, give up.
    if (!normalizeForPrefixMatch(src).startsWith(committedNorm)) return fullText
  }
  // Skip one extra blank line after the matched prefix (summary rewrite).
  let rest = src.slice(i)
  if (rest.startsWith('\n')) rest = rest.slice(1)
  if (rest.startsWith('\n') && committedNorm.endsWith('\n')) {
    // keep single separator; if rewrite added one more blank after summary, drop one
  }
  return rest
}

export function committedAssistantPrefix(messages: ChatMessage[], taskId: number): string {
  const commits = messages.filter(
    message => message.taskId === taskId
      && message.role === 'assistant'
      && message.done
      && message.id.startsWith(`a-${taskId}-c`),
  )
  if (commits.length === 0) return ''
  return commits.map(message => message.text).join('\n')
}

function splitLines(text: string): string[] {
  if (text === '') return []
  return text.split('\n')
}
