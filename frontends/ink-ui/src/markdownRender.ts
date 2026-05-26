import { marked, type Token } from 'marked'
import type { InkTheme } from './theme.js'

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

export function renderMarkdownLines(markdown: string, theme?: InkTheme): MarkdownLine[] {
  const source = markdown || ''
  if (TOOL_SUMMARY_RE.test(source.trim())) {
    const text = source.trimEnd()
    return [{ text, parts: [{ text }] }]
  }

  const tokens = marked.lexer(source, { gfm: true })
  const lines: MarkdownLine[] = []
  for (const token of tokens) {
    appendToken(lines, token, {}, theme)
  }
  return lines.length > 0 ? lines : [{ text: ' ', parts: [{ text: ' ' }] }]
}

function appendToken(lines: MarkdownLine[], token: Token, style: Partial<MarkdownPart>, theme?: InkTheme): void {
  switch (token.type) {
    case 'heading':
      pushLine(lines, inlineParts(token.tokens ?? [], { ...style, bold: true }, theme))
      return
    case 'paragraph':
      pushLine(lines, inlineParts(token.tokens ?? [], style, theme))
      return
    case 'code':
      token.text.split('\n').forEach((line: string) => {
        pushLine(lines, [{ text: `  ${line || ' '}`, color: theme?.code ?? 'gray' }])
      })
      return
    case 'blockquote':
      for (const child of token.tokens ?? []) {
        const before = lines.length
        appendToken(lines, child, { ...style, italic: true }, theme)
        for (let i = before; i < lines.length; i++) {
          const line = lines[i]!
          line.parts = [{ text: '| ', dimColor: true }, ...line.parts]
          line.text = `| ${line.text}`
        }
      }
      return
    case 'list':
      token.items.forEach((item: { tokens?: Token[] }, index: number) => {
        const marker = token.ordered ? `${token.start + index}. ` : '- '
        const parts = inlineParts(flatListItemTokens(item.tokens ?? []), style, theme)
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

function inlineParts(tokens: Token[], style: Partial<MarkdownPart>, theme?: InkTheme): MarkdownPart[] {
  const parts: MarkdownPart[] = []
  for (const token of tokens) {
    switch (token.type) {
      case 'text':
        if ('tokens' in token && Array.isArray(token.tokens)) {
          parts.push(...inlineParts(token.tokens as Token[], style, theme))
        } else {
          parts.push({ ...style, text: token.text })
        }
        break
      case 'strong':
        parts.push(...inlineParts(token.tokens ?? [], { ...style, bold: true }, theme))
        break
      case 'em':
        parts.push(...inlineParts(token.tokens ?? [], { ...style, italic: true }, theme))
        break
      case 'codespan':
        parts.push({ ...style, text: token.text, color: theme?.code ?? 'cyan' })
        break
      case 'link':
        parts.push(...inlineParts(token.tokens ?? [], { ...style, color: theme?.link, underline: true }, theme))
        if (token.href && token.href !== parts.at(-1)?.text) {
          parts.push({ ...style, text: ` (${token.href})`, color: theme?.linkUrl ?? 'gray' })
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