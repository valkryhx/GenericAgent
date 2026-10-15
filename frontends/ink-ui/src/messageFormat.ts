import { GLYPHS, EXPAND_HINT } from './glyphs.js'

type FormatOptions = {
  expanded?: boolean
}

const TOOL_SUMMARY_LIMIT = 110
const TOOL_VALUE_LIMIT = 80

function truncate(text: string, limit: number): { text: string; truncated: boolean } {
  if (text.length <= limit) return { text, truncated: false }
  return { text: `${text.slice(0, limit)}...`, truncated: true }
}

function parseJsonArgs(argsText: string): unknown {
  try {
    return JSON.parse(argsText)
  } catch {
    return null
  }
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

const TOOL_OUTPUT_HEAD_LINES = 8
const RESULT_MARKER_LINE_RE = /^[^\S\r\n]*(?:\|[^\S\r\n]*)?\[(?:Action|Status|Stdout)\][^\S\r\n]*$/
const STDOUT_MARKER_LINE_RE = /^[^\S\r\n]*(?:\|[^\S\r\n]*)?\[Stdout\][^\S\r\n]*$/

/**
 * Fold long `[Stdout]` bodies to their first TOOL_OUTPUT_HEAD_LINES lines with a
 * `(`ctrl+o to expand`)` hint, mirroring CC's offscreen-locked tool output
 * (see claudecode283_hack/docs/20261016-CC-UI渲染对标调研.md §4). Transcript mode
 * (`ctrl+o`) shows the full body.
 */
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
    while (j < lines.length && (lines[j] ?? '').trim() !== '' && !RESULT_MARKER_LINE_RE.test(lines[j] ?? '')) {
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
  text = text.replace(
    /🛠️?\s*Tool:\s*`([^`]+)`\s*📥\s*args:\s*\n````text\n([\s\S]*?)\n````/g,
    (_match, name, argsText) => formatToolBlock(String(name), String(argsText), options),
  )
  text = text.replace(/^[^\S\r\n]*🛠️?[^\S\r\n]+([A-Za-z_][A-Za-z0-9_]*)\((.*?)\)[^\S\r\n]*$/gm, (_match, name, args) => `${GLYPHS.bullet} ${name}(${args})`)
  // Repair legacy GA status fences whose closing marker was emitted on the
  // same line as the body. This happens when a tool yields text without a
  // trailing newline (notably the workflow launch result); otherwise the next
  // "**LLM Running ...**" is swallowed by the unclosed Markdown fence.
  text = text.replace(
    /(^|\n)(`{4,})[ \t]*\n(\[(?:Workflow|Warn)\][^\n]*?)`{4,}[ \t]*(?=\n|$)/g,
    (_match, prefix: string, fence: string, body: string) => `${prefix}${fence}\n${body}\n${fence}`,
  )
  // GA wraps tool execution status in 4–5 backtick fences. Unwrap those status
  // blocks (keep [Action]/[Status]/[Stdout] body) so a later turn header is
  // never trapped inside a markdown code fence after stream-commit splits.
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
