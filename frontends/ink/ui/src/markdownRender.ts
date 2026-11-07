import { marked, type Token } from 'marked'
import stringWidth from 'string-width'
import type { InkTheme } from './theme.js'
import { GLYPHS } from './glyphs.js'

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

const TOOL_SUMMARY_RE = new RegExp(`^(?:>|${GLYPHS.bullet})\\s+[A-Za-z_][A-Za-z0-9_]*\\(.*\\)$`)

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
        // GA [Action]/[Status] lines must never pick up code indent/color even if a
        // residual fence still classifies them as code (stream-commit mid-fence).
        if (/^\s*\[(?:Action|Status)\]/.test(line)) {
          pushLine(lines, [{ text: line.trimStart() || ' ', color: theme?.muted ?? 'gray', dimColor: true }])
          return
        }
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
    case 'table':
      appendTable(lines, token, theme)
      return
    case 'space':
      return
    default:
      if ('raw' in token && typeof token.raw === 'string') {
        pushLine(lines, [{ text: token.raw }])
      }
  }
}

/**
 * Render a GFM table as box-drawing rows.
 *
 * `marked` emits a `table` token for `| a | b |` blocks, but this renderer had no
 * `table` case, so the token fell through to `default` and the raw source — pipe
 * characters and the `---` delimiter row — was printed verbatim. That is the
 * "| separated table not rendered" symptom.
 *
 * Columns are padded with `stringWidth`, not `String.length`, so CJK cells line up:
 * a Han character is two terminal columns wide but one JS code unit.
 *
 * Wide tables are not truncated. The transcript wraps long lines, so a clipped
 * border would look broken; letting the terminal wrap keeps the data readable.
 * (A `maxWidth` clamp would need an ellipsis policy per cell and a decision about
 * which column absorbs the loss — out of scope for the fix.)
 */
function appendTable(lines: MarkdownLine[], token: Token, theme?: InkTheme): void {
  const table = token as Token & {
    header?: Array<{ tokens?: Token[]; text?: string }>
    rows?: Array<Array<{ tokens?: Token[]; text?: string }>>
    align?: Array<'left' | 'center' | 'right' | null>
  }
  const headerCells = table.header ?? []
  const bodyRows = table.rows ?? []
  if (headerCells.length === 0) return

  const aligns = table.align ?? []
  const cellParts = (cell: { tokens?: Token[]; text?: string } | undefined): MarkdownPart[] => {
    if (!cell) return []
    const tokens = cell.tokens ?? []
    if (tokens.length > 0) return inlineParts(tokens, {}, theme)
    return [{ text: cell.text ?? '' }]
  }
  const cellText = (cell: { tokens?: Token[]; text?: string } | undefined): string => (
    cellParts(cell).map(part => part.text).join('').replace(/\n/g, ' ')
  )

  const columns = headerCells.length
  const widths = new Array<number>(columns).fill(1)
  const measure = (cells: Array<{ tokens?: Token[]; text?: string } | undefined>) => {
    for (let index = 0; index < columns; index += 1) {
      widths[index] = Math.max(widths[index]!, stringWidth(cellText(cells[index])))
    }
  }
  measure(headerCells)
  bodyRows.forEach(row => measure(row))

  const pad = (text: string, index: number): string => {
    const target = widths[index]!
    const missing = Math.max(0, target - stringWidth(text))
    const align = aligns[index] ?? null
    if (align === 'right') return ' '.repeat(missing) + text
    if (align === 'center') {
      const left = Math.floor(missing / 2)
      return ' '.repeat(left) + text + ' '.repeat(missing - left)
    }
    return text + ' '.repeat(missing)
  }
  const rule = (left: string, mid: string, right: string): string => (
    left + widths.map(width => GLYPHS.table.horizontal.repeat(width + 2)).join(mid) + right
  )
  const renderRow = (cells: Array<{ tokens?: Token[]; text?: string } | undefined>, bold: boolean) => {
    const parts: MarkdownPart[] = [{ text: GLYPHS.table.vertical, color: theme?.muted }]
    for (let index = 0; index < columns; index += 1) {
      parts.push({ text: ' ' })
      const inner = cellParts(cells[index])
      const innerText = inner.map(part => part.text).join('').replace(/\n/g, ' ')
      const missing = Math.max(0, widths[index]! - stringWidth(innerText))
      const align = aligns[index] ?? null
      const padLeft = align === 'right' ? missing : align === 'center' ? Math.floor(missing / 2) : 0
      const padRight = missing - padLeft
      if (padLeft > 0) parts.push({ text: ' '.repeat(padLeft) })
      if (inner.length > 0) parts.push(...inner.map(part => ({ ...part, bold: bold || part.bold })))
      if (padRight > 0) parts.push({ text: ' '.repeat(padRight) })
      parts.push({ text: ' ' }, { text: GLYPHS.table.vertical, color: theme?.muted })
    }
    pushLine(lines, parts)
  }

  pushLine(lines, [{ text: rule(GLYPHS.table.topLeft, GLYPHS.table.teeDown, GLYPHS.table.topRight), color: theme?.muted }])
  renderRow(headerCells, true)
  pushLine(lines, [{ text: rule(GLYPHS.table.teeRight, GLYPHS.table.cross, GLYPHS.table.teeLeft), color: theme?.muted }])
  bodyRows.forEach(row => renderRow(row, false))
  pushLine(lines, [{ text: rule(GLYPHS.table.bottomLeft, GLYPHS.table.teeUp, GLYPHS.table.bottomRight), color: theme?.muted }])
}

function flatListItemTokens(tokens: Token[]): Token[] {  if (tokens.length === 1 && tokens[0]?.type === 'text' && 'tokens' in tokens[0] && Array.isArray(tokens[0].tokens)) {
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