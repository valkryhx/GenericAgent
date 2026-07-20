/** 本地图片路径检测（对齐 Claude Code isImageFilePath / Codex paste path）。 */

const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp)$/i

export function stripOuterQuotes(text: string): string {
  const t = text.trim()
  if (
    (t.startsWith('"') && t.endsWith('"')) ||
    (t.startsWith("'") && t.endsWith("'"))
  ) {
    return t.slice(1, -1)
  }
  return t
}

/** 去掉 shell 风格反斜杠转义（非 Windows 盘符路径）。 */
export function stripBackslashEscapes(path: string): string {
  // Windows 绝对路径保留反斜杠
  if (/^[A-Za-z]:[\\/]/.test(path) || path.startsWith('\\\\')) {
    return path
  }
  return path.replace(/\\(.)/g, '$1')
}

export function isImageFilePath(text: string): boolean {
  const cleaned = stripBackslashEscapes(stripOuterQuotes(text.trim()))
  if (!cleaned || cleaned.includes('\n') || cleaned.includes('\0')) return false
  return IMAGE_EXT_RE.test(cleaned)
}

export function asImageFilePath(text: string): string | null {
  const cleaned = stripBackslashEscapes(stripOuterQuotes(text.trim()))
  if (!cleaned || !IMAGE_EXT_RE.test(cleaned)) return null
  return cleaned
}

/**
 * 从粘贴/提交文本中抽出可能的图片路径（多行或空格分隔的绝对路径）。
 * 不访问磁盘；调用方再用 exists 过滤。
 */
export function extractImagePathCandidates(text: string): string[] {
  if (!text) return []
  const parts = text
    .split(/ (?=\/|[A-Za-z]:[\\/]|\\\\)/)
    .flatMap(part => part.split(/\r?\n/))
    .map(line => line.trim())
    .filter(Boolean)

  const out: string[] = []
  const seen = new Set<string>()
  for (const part of parts) {
    const p = asImageFilePath(part)
    if (p && !seen.has(p)) {
      seen.add(p)
      out.push(p)
    }
  }
  // 整段就是一个路径
  if (out.length === 0) {
    const single = asImageFilePath(text)
    if (single) out.push(single)
  }
  return out
}
