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
  return `> ${name}\n  args:\n${indented}`
}

function formatCollapsedTool(name: string, argsText: string): string {
  const { summary, truncated } = summarizeArgs(argsText)
  const hint = truncated ? ' (ctrl+o to expand)' : ''
  return summary ? `> ${name}(${summary})${hint}` : `> ${name}${hint}`
}

function formatToolBlock(name: string, argsText: string, options: FormatOptions): string {
  return options.expanded ? formatExpandedTool(name, argsText) : formatCollapsedTool(name, argsText)
}

export function formatAssistantText(raw: string, options: FormatOptions = {}): string {
  let text = raw || ''
  text = text.replace(/<summary>([\s\S]*?)<\/summary>/g, (_match, summary) => `Summary: ${String(summary).trim()}`)
  text = text.replace(
    /🛠️?\s*Tool:\s*`([^`]+)`\s*📥\s*args:\s*\n````text\n([\s\S]*?)\n````/g,
    (_match, name, argsText) => formatToolBlock(String(name), String(argsText), options),
  )
  text = text.replace(/^[^\S\r\n]*🛠️?[^\S\r\n]+([A-Za-z_][A-Za-z0-9_]*)\((.*?)\)[^\S\r\n]*$/gm, (_match, name, args) => `> ${name}(${args})`)
  text = text.replace(/`{4,}/g, '```')
  text = text.replace(/(?:^|\n)```\s*\n\s*\[Info\] Final response to user\.\s*\n```\s*(?=\n|$)/g, '\n')
  text = text.replace(/(?:^|\n)\s*\[Info\] Final response to user\.\s*(?=\n|$)/g, '\n')
  text = text.replace(/\n{4,}/g, '\n\n\n')
  return text.trimEnd()
}
