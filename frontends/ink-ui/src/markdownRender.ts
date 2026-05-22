import { marked, type Token } from 'marked'

export type MarkdownPart = {
  text: string
  color?: string
  bold?: boolean
  italic?: boolean
  underline?: boolean
  dimColor?: boolean
}

export type MarkdownLine = {
  text: string
  parts: MarkdownPart[]
}

const TOOL_SUMMARY_RE = /^>\s+[A-Za-z_][A-Za-z0-9_]*\(.*\)$/

export function lineText(line: MarkdownLine): string {
  return line.text
}

export function renderMarkdownLines(markdown: string): MarkdownLine[] {
  const source = markdown || ''
  if (TOOL_SUMMARY_RE.test(source.trim())) {
    const text = source.trimEnd()
    return [{ text, parts: [{ text }] }]
  }

  const tokens = marked.lexer(source, { gfm: true })
  const lines: MarkdownLine[] = []
  for (const token of tokens) {
    appendToken(lines, token, {})
  }
  return lines.length > 0 ? lines : [{ text: ' ', parts: [{ text: ' ' }] }]
}

function appendToken(lines: MarkdownLine[], token: Token, style: Partial<MarkdownPart>): void {
  switch (token.type) {
    case 'heading':
      pushLine(lines, inlineParts(token.tokens ?? [], { ...style, bold: true }))
      return
    case 'paragraph':
      pushLine(lines, inlineParts(token.tokens ?? [], style))
      return
    case 'code':
      token.text.split('\n').forEach(line => {
        pushLine(lines, [{ text: `  ${line || ' '}`, color: 'gray' }])
      })
      return
    case 'blockquote':
      for (const child of token.tokens ?? []) {
        const before = lines.length
        appendToken(lines, child, { ...style, italic: true })
        for (let i = before; i < lines.length; i++) {
          const line = lines[i]!
          line.parts = [{ text: '| ', dimColor: true }, ...line.parts]
          line.text = `| ${line.text}`
        }
      }
      return
    case 'list':
      token.items.forEach((item, index) => {
        const marker = token.ordered ? `${token.start + index}. ` : '- '
        const parts = inlineParts(flatListItemTokens(item.tokens ?? []), style)
        pushLine(lines, [{ text: marker }, ...parts])
      })
      return
    case 'space':
      return
    default:
      if ('raw' in token && typeof token.raw === 'string') {
        pushLine(lines, [{ text: token.raw }])
      }
  }
}

function flatListItemTokens(tokens: Token[]): Token[] {
  if (tokens.length === 1 && tokens[0]?.type === 'text' && 'tokens' in tokens[0] && Array.isArray(tokens[0].tokens)) {
    return tokens[0].tokens as Token[]
  }
  if (tokens.length === 1 && tokens[0]?.type === 'paragraph' && 'tokens' in tokens[0] && Array.isArray(tokens[0].tokens)) {
    return tokens[0].tokens as Token[]
  }
  return tokens
}

function inlineParts(tokens: Token[], style: Partial<MarkdownPart>): MarkdownPart[] {
  const parts: MarkdownPart[] = []
  for (const token of tokens) {
    switch (token.type) {
      case 'text':
        if ('tokens' in token && Array.isArray(token.tokens)) {
          parts.push(...inlineParts(token.tokens as Token[], style))
        } else {
          parts.push({ ...style, text: token.text })
        }
        break
      case 'strong':
        parts.push(...inlineParts(token.tokens ?? [], { ...style, bold: true }))
        break
      case 'em':
        parts.push(...inlineParts(token.tokens ?? [], { ...style, italic: true }))
        break
      case 'codespan':
        parts.push({ ...style, text: token.text, color: 'cyan' })
        break
      case 'link':
        parts.push(...inlineParts(token.tokens ?? [], { ...style, underline: true }))
        if (token.href && token.href !== parts.at(-1)?.text) {
          parts.push({ ...style, text: ` (${token.href})`, color: 'gray' })
        }
        break
      case 'br':
        parts.push({ ...style, text: '\n' })
        break
      default:
        if ('raw' in token && typeof token.raw === 'string') {
          parts.push({ ...style, text: token.raw })
        }
    }
  }
  return parts.length > 0 ? parts : [{ ...style, text: ' ' }]
}

function pushLine(lines: MarkdownLine[], parts: MarkdownPart[]): void {
  const splitLines: MarkdownPart[][] = [[]]
  for (const part of parts) {
    const chunks = part.text.split('\n')
    chunks.forEach((chunk, index) => {
      if (index > 0) splitLines.push([])
      if (chunk) splitLines[splitLines.length - 1]!.push({ ...part, text: chunk })
    })
  }
  for (const lineParts of splitLines) {
    const normalized = lineParts.length > 0 ? lineParts : [{ text: ' ' }]
    lines.push({
      text: normalized.map(part => part.text).join('') || ' ',
      parts: normalized,
    })
  }
}