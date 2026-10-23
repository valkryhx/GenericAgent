import { GLYPHS, EXPAND_HINT } from './glyphs.js'

type FormatOptions = {
  expanded?: boolean
  /** Mid-stream render: fold an args fence that has not closed yet to a tool chip. */
  streaming?: boolean
}

const TOOL_SUMMARY_LIMIT = 110
const TOOL_VALUE_LIMIT = 80

function truncate(text: string, limit: number): { text: string; truncated: boolean } {
  if (text.length <= limit) return { text, truncated: false }
  return { text: `${text.slice(0, limit)}...`, truncated: true }
}

/**
 * GA's `get_pretty_json()` decodes escaped `\n` sequences inside the
 * serialized script argument into real newlines. That makes the tool-args JSON
 * invalid for multi-line `code_run` scripts. Repair only newlines that occur
 * inside a JSON string so the chip can still summarize args instead of dumping
 * the raw block.
 */
function parseJsonArgs(argsText: string): unknown {
  try {
    return JSON.parse(argsText)
  } catch {
    try {
      return JSON.parse(repairJsonStringNewlines(argsText))
    } catch {
      return null
    }
  }
}

const BACKSLASH = String.fromCharCode(92)
const NEWLINE_CHAR = String.fromCharCode(10)
const CARRIAGE_RETURN_CHAR = String.fromCharCode(13)
const TAB_CHAR = String.fromCharCode(9)

function repairJsonStringNewlines(text: string): string {
  let out = ''
  let inString = false
  let escaped = false
  for (const ch of text) {
    if (!inString) {
      if (ch === '"') inString = true
      out += ch
      continue
    }
    if (escaped) {
      // The old get_pretty_json() also half-decoded literal `\n` sequences
      // (a JSON string holding `\n` became `\` + a real newline), so a
      // backslash followed by a real control character must be restored as an
      // escaped JSON sequence instead of passing the control character through.
      if (ch === NEWLINE_CHAR) out += BACKSLASH + 'n'
      else if (ch === CARRIAGE_RETURN_CHAR) out += BACKSLASH + 'r'
      else if (ch === TAB_CHAR) out += BACKSLASH + 't'
      else out += ch
      escaped = false
      continue
    }
    if (ch === BACKSLASH) {
      out += ch
      escaped = true
      continue
    }
    if (ch === '"') {
      out += ch
      inString = false
      continue
    }
    if (ch === NEWLINE_CHAR) {
      out += BACKSLASH + 'n'
      continue
    }
    if (ch === CARRIAGE_RETURN_CHAR) {
      out += BACKSLASH + 'r'
      continue
    }
    if (ch === TAB_CHAR) {
      out += BACKSLASH + 't'
      continue
    }
    out += ch
  }
  return out
}

function formatArgValue(value: unknown): { text: string; truncated: boolean } {
  if (typeof value === 'string') {
    const oneLine = value.replace(/\s+/g, ' ').trim()
    return truncate(oneLine, TOOL_VALUE_LIMIT)
  }
  if (typeof value === 'number' || typeof value === 'boolean' || value === null) {
    return { text: String(value), truncated: false }
  }
  const compact = JSON.stringify(value)
  return truncate(compact ?? String(value), TOOL_VALUE_LIMIT)
}

function summarizeArgs(argsText: string): { summary: string; truncated: boolean } {
  const parsed = parseJsonArgs(argsText)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    const compact = argsText.replace(/\s+/g, ' ').trim()
    const shortened = truncate(compact, TOOL_SUMMARY_LIMIT)
    return { summary: shortened.text, truncated: shortened.truncated }
  }

  const parts: string[] = []
  let truncated = false
  for (const [key, value] of Object.entries(parsed)) {
    const formatted = formatArgValue(value)
    truncated ||= formatted.truncated
    parts.push(`${key}: ${formatted.text}`)
  }
  const summary = parts.join(', ')
  const shortened = truncate(summary, TOOL_SUMMARY_LIMIT)
  return { summary: shortened.text, truncated: truncated || shortened.truncated }
}

function formatExpandedTool(name: string, argsText: string): string {
  const parsed = parseJsonArgs(argsText)
  const pretty = parsed === null ? argsText.trim() : JSON.stringify(parsed, null, 2)
  const indented = pretty.split('\n').map(line => `  ${line}`).join('\n')
  return `${GLYPHS.bullet} ${name}\n  args:\n${indented}`
}

function formatCollapsedTool(name: string, argsText: string): string {
  const { summary, truncated } = summarizeArgs(argsText)
  const hint = truncated ? ` ${EXPAND_HINT}` : ''
  return summary ? `${GLYPHS.bullet} ${name}(${summary})${hint}` : `${GLYPHS.bullet} ${name}${hint}`
}

function formatToolBlock(name: string, argsText: string, options: FormatOptions): string {
  return options.expanded ? formatExpandedTool(name, argsText) : formatCollapsedTool(name, argsText)
}

/**
 * Mid-stream safety net: if a tool header is followed by an args fence that has
 * not closed yet, keep only the tool chip and drop the in-flight JSON body.
 * Once the closing fence arrives, the complete-tool regex above handles it.
 */
function foldOpenStreamingToolBlock(text: string): string {
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i++) {
    const header = lines[i]!.match(/Tool:\s*`([^`]+)`.*args:\s*$/)
    if (!header) continue
    const opener = (lines[i + 1] ?? '').trim().match(/^(`{3,})([^`]*)$/)
    if (!opener) continue
    const openLength = opener[1]!.length
    let closed = false
    for (let j = i + 2; j < lines.length; j++) {
      const closer = lines[j]!.trim().match(/^(`{3,})\s*$/)
      if (closer && closer[1]!.length >= openLength) {
        closed = true
        break
      }
    }
    if (closed) continue
    return [...lines.slice(0, i), `${GLYPHS.bullet} ${header[1]}`].join('\n')
  }
  return text
}

/**
 * A bare args block at the start of a segment belongs to a tool header that was
 * committed to an earlier Static message. Fold it into `● tool(args)` when the
 * preceding visible line is a tool chip; otherwise render a neutral `● args`
 * placeholder so raw JSON never becomes assistant prose.
 */
function foldOrphanToolArgsFence(text: string, options: FormatOptions): string {
  const lines = text.split('\n')
  const out: string[] = []
  let i = 0
  while (i < lines.length) {
    const opener = lines[i]!.trim().match(/^(`{3,})text\s*$/)
    const atStart = out.every(line => !line.trim())
    const chip = out.length > 0
      ? (out[out.length - 1] ?? '').match(/^[^\S\r\n]*(?:⏺|●|•)[^\S\r\n]*([A-Za-z_][A-Za-z0-9_]*)[^\S\r\n]*$/)
      : null
    if (opener && (atStart || chip)) {
      const fence = opener[1]!.length
      let close = -1
      for (let j = i + 1; j < lines.length; j++) {
        const closer = lines[j]!.trim().match(/^(`{3,})\s*$/)
        if (closer && closer[1]!.length >= fence) {
          close = j
          break
        }
      }
      const body = lines.slice(i + 1, close === -1 ? lines.length : close).join('\n')
      const parsed = parseJsonArgs(body)
      // Legacy `get_pretty_json()` output for multi-line code_run was invalid
      // JSON, so a parse failure must not send the raw body back to Markdown.
      const looksLikeArgs = body.trimStart().startsWith('{')
      if (chip && close !== -1 && (parsed || looksLikeArgs)) {
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          out[out.length - 1] = formatToolBlock(chip[1]!, body, options)
        }
        i = close + 1
        continue
      }
      if (!chip && parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        // Header is in an earlier Static segment; drop only the orphan args body.
        i = close + 1
        continue
      }
      if ((chip || atStart) && close === -1 && options.streaming && looksLikeArgs) {
        // Mid-stream orphan: the fence is still open, so JSON.parse cannot work.
        return out.join('\n')
      }
      if (!chip && atStart && close !== -1 && looksLikeArgs) {
        // Completed legacy orphan args block; drop the raw JSON entirely.
        i = close + 1
        continue
      }
    }
    out.push(lines[i]!)
    i += 1
  }
  return out.join('\n')
}

const TOOL_OUTPUT_HEAD_LINES = 8
const RESULT_MARKER_LINE_RE = /^[^\S\r\n]*(?:\|[^\S\r\n]*)?\[(?:Action|Status|Stdout)\][^\S\r\n]*$/
const STDOUT_MARKER_LINE_RE = /^[^\S\r\n]*(?:\|[^\S\r\n]*)?\[Stdout\][^\S\r\n]*$/

/**
 * Fold long `[Stdout]` bodies to their first TOOL_OUTPUT_HEAD_LINES lines with a
 * `(`ctrl+o to expand`)` hint, mirroring CC's offscreen-locked tool output
 * (see claudecode283_hack/docs/20261016-CC-UI渲染对标调研.md §4). Transcript mode
 * (`ctrl+o`) shows the full body.
 */
/**
 * Private marker prefixed to every line of a `[Stdout]` body while the original
 * tool fences are still visible. It lets the renderer keep the whole stdout
 * block (including `[omitted long output]` and blank lines) in the dim result
 * channel without guessing where the next assistant paragraph starts.
 */
export const STDOUT_BODY_PREFIX = '\uE000'
/** End-of-block sentinel emitted when a fenced `[Stdout]` block closes. */
export const STDOUT_BODY_END = '\uE001'

function markStdoutBodiesInFences(text: string): string {
  const lines = text.split('\n')
  const out: string[] = []
  let i = 0
  while (i < lines.length) {
    const opener = lines[i]!.trim().match(/^(`{3,})([^`]*)$/)
    if (!opener) {
      out.push(lines[i]!)
      i += 1
      continue
    }
    const fence = opener[1]!.length
    let close = -1
    for (let j = i + 1; j < lines.length; j++) {
      const closer = lines[j]!.trim().match(/^(`{3,})\s*$/)
      if (closer && closer[1]!.length >= fence) {
        close = j
        break
      }
    }
    const block = lines.slice(i + 1, close === -1 ? lines.length : close)
    // Only GA's tool-status wrappers (4+ backticks) are stripped here. GA
    // always emits 4- or 5-backtick status fences; ordinary 3-backtick
    // assistant Markdown must reach the renderer unchanged, even if its body
    // happens to contain a line beginning with `[Status]`.
    const isToolFence = opener[1]!.length >= 4
      && block.some(line => /^\s*\[(?:Action|Status|Stdout)\]/.test(line))
    if (!isToolFence) {
      out.push(lines[i]!, ...block)
      if (close !== -1) out.push(lines[close]!)
      i = close === -1 ? lines.length : close + 1
      continue
    }
    let inStdout = false
    for (const line of block) {
      if (/^\s*\[(?:Action|Status|Stdout)\]/.test(line)) {
        inStdout = /^\s*\[Stdout\]/.test(line)
        out.push(line)
        continue
      }
      out.push(inStdout ? `${STDOUT_BODY_PREFIX}${line}` : line)
    }
    // Fence close is a hard end of the result block. Without this sentinel the
    // renderer would treat assistant prose after the closing fence as more
    // stdout because the body lines are otherwise indistinguishable.
    if (inStdout) out.push(STDOUT_BODY_END)
    i = close === -1 ? lines.length : close + 1
  }
  return out.join('\n')
}

function foldToolOutput(text: string, expanded: boolean): string {
  if (expanded) return text
  const lines = text.split('\n')
  const out: string[] = []
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? ''
    out.push(line)
    if (!STDOUT_MARKER_LINE_RE.test(line)) continue
    let j = i + 1
    let total = 0
    while (j < lines.length && (lines[j] ?? '').trim() !== '' && lines[j] !== STDOUT_BODY_END && !RESULT_MARKER_LINE_RE.test(lines[j] ?? '')) {
      if (total < TOOL_OUTPUT_HEAD_LINES) out.push(lines[j] ?? '')
      total++
      j++
    }
    if (total > TOOL_OUTPUT_HEAD_LINES) {
      out.push(`... (+${total - TOOL_OUTPUT_HEAD_LINES} more lines) ${EXPAND_HINT}`)
    }
    i = j - 1
  }
  return out.join('\n')
}

export function formatAssistantText(raw: string, options: FormatOptions = {}): string {
  let text = raw || ''
  // Normalize newlines early so fence/tool regexes match Windows CRLF transcripts.
  text = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  text = text.replace(/<summary>([\s\S]*?)<\/summary>/g, (_match, summary) => `Summary: ${String(summary).trim()}`)
  // Complete tool block. GA has emitted both 4- and 5-backtick `text`
  // openers; match the opener's run length and require the same closer.
  text = text.replace(
    /^[^\S\r\n]*[^\w\s]*[^\S\r\n]*Tool:[^\S\r\n]*`([^`]+)`[^\S\r\n]*[^\w\s]*[^\S\r\n]*args:[^\S\r\n]*\n(`{3,})text[^\S\r\n]*\n([\s\S]*?)\n\2[^\S\r\n]*(?=\n|$)/gm,
    (_match, name, _fence, argsText) => formatToolBlock(String(name), String(argsText), options),
  )
  // Historical split shape left by the first-turn occlusion bug: the tool
  // header became `● code_run` while its args fence remained a later segment.
  // Rejoin the pair when both are in the same message.
  text = text.replace(
    /^[^\S\r\n]*(?:⏺|●|•)[^\S\r\n]*([A-Za-z_][A-Za-z0-9_]*)[^\S\r\n]*\n(`{3,})text[^\S\r\n]*\n([\s\S]*?)\n\2[^\S\r\n]*(?=\n|$)/gm,
    (match, name, _fence, argsText) => {
      const parsed = parseJsonArgs(String(argsText))
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return match
      return formatToolBlock(String(name), String(argsText), options)
    },
  )
  // Historical stream-commit split (or a screenshot-era first turn): the tool
  // header already lives in an earlier Static message and this message starts
  // with the bare ```text JSON args. Never paint that JSON as a cyan code block;
  // fold it to one args row. Only object-shaped `text` fences at the head of a
  // message (or directly after a tool chip) are touched.
  text = foldOrphanToolArgsFence(text, options)
  // Streaming fallback: an args fence that is still open must not paint its raw
  // JSON into the live transcript. This is the first-turn code_run regression:
  // the tool header had already been committed to Static, so the orphan
  // ```text block could no longer match the complete-tool regex above.
  if (options.streaming) {
    text = foldOpenStreamingToolBlock(text)
  }
  // Streaming fallback: a tool header whose args fence has not arrived yet still
  // collapses to the bullet form instead of showing the raw emoji header.
  text = text.replace(
    /^[^\S\r\n]*[^\w\s]*[^\S\r\n]*Tool:[^\S\r\n]*`([^`]+)`[^\S\r\n]*[^\w\s]*[^\S\r\n]*args:[^\S\r\n]*$/gm,
    (_match, name) => `${GLYPHS.bullet} ${String(name)}`,
  )
  text = text.replace(/^[^\S\r\n]*[^\w\s]*[^\S\r\n]+([A-Za-z_][A-Za-z0-9_]*)\((.*?)\)[^\S\r\n]*$/gm, (_match, name, args) => `${GLYPHS.bullet} ${name}(${args})`)
  // Repair legacy GA status fences whose closing marker was emitted on the
  // same line as the body. This happens when a tool yields text without a
  // trailing newline (notably the workflow launch result); otherwise the next
  // "**LLM Running ...**" is swallowed by the unclosed Markdown fence.
  text = text.replace(
    /(^|\n)(`{4,})[ \t]*\n(\[(?:Workflow|Warn)\][^\n]*?)`{4,}[ \t]*(?=\n|$)/g,
    (_match, prefix: string, fence: string, body: string) => `${prefix}${fence}\n${body}\n${fence}`,
  )
  // GA wraps tool execution status in 4–5 backtick fences. Mark stdout bodies
  // before removing the fences so the renderer can preserve the full result
  // channel, then unwrap so a later turn header is never trapped inside a
  // markdown code fence after stream-commit splits.
  text = markStdoutBodiesInFences(text)
  text = text.replace(
    /(?:^|\n)`{4,}\s*\n((?:.*\n)*?(?:\[Action\]|\[Status\]|\[Stdout\])[\s\S]*?)\n`{4,}\s*(?=\n|$)/g,
    (_match, body: string) => `\n${String(body).replace(/^\n+|\n+$/g, '')}\n`,
  )
  // Collapse remaining 4+ backtick fences to triple for markdown safety.
  text = text.replace(/`{4,}/g, '```')
  text = text.replace(/(?:^|\n)```\s*\n\s*\[Info\] Final response to user\.\s*\n```\s*(?=\n|$)/g, '\n')
  text = text.replace(/(?:^|\n)\s*\[Info\] Final response to user\.\s*(?=\n|$)/g, '\n')
  // Drop empty fence pairs left after stripping tool/info wrappers.
  text = text.replace(/(?:^|\n)```(?:[^\n`]*)\n\s*```(?=\n|$)/g, '\n')
  text = foldToolOutput(text, options.expanded === true)
  text = text.replace(/\n{4,}/g, '\n\n\n')
  return text.trimEnd()
}
