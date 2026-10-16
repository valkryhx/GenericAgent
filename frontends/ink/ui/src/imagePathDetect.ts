/** 本地图片路径检测（对齐 Claude Code isImageFilePath / Codex paste path）。 */

const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp|bmp)$/i

/** 绝对路径：Windows 盘符 / UNC / Unix 根。相对裸名（如粘贴分片 `048_89.jpg`）不算。 */
const ABSOLUTE_PREFIX_RE = /^(?:[A-Za-z]:[\\/]|\\\\|\/)/

/**
 * 从自由文本里抓「绝对」图片路径。
 *
 * - 引号包裹：允许路径内任意空格 / 中文 / 符号（除引号与换行）
 * - 无引号：从盘符/根起匹配到图片扩展名，路径段内允许空格；
 *   扩展名后必须以空白/标点/结尾为界（避免吞掉后面的「这图是什么」）
 *
 * 相对裸名（`048_89.jpg`）故意不匹配，防止粘贴分片误伤完整路径。
 */
const ABS_IMAGE_PATH_IN_TEXT_RE =
  /(?:"((?:[A-Za-z]:[\\/]|\\\\|\/)[^"\r\n]+\.(?:png|jpe?g|gif|webp|bmp))"|'((?:[A-Za-z]:[\\/]|\\\\|\/)[^'\r\n]+\.(?:png|jpe?g|gif|webp|bmp))'|((?:[A-Za-z]:[\\/]|\\\\|\/)[^\r\n]*?\.(?:png|jpe?g|gif|webp|bmp))(?=[\s"'\)\]\},;]|$))/gi

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

export function isAbsoluteImagePath(path: string): boolean {
  const cleaned = stripBackslashEscapes(unwrapShellPathWrapper(path))
  if (!cleaned || cleaned.includes('\n') || cleaned.includes('\0')) return false
  return ABSOLUTE_PREFIX_RE.test(cleaned) && IMAGE_EXT_RE.test(cleaned)
}

export function isImageFilePath(text: string): boolean {
  // 对外语义：仅绝对路径（或引号包裹的绝对路径）算「可自动贴图路径」。
  // 裸相对名如 `048_89.jpg` 在 bracketed-paste 分片时会误伤完整 Windows 路径。
  return isAbsoluteImagePath(text)
}

/**
 * 去掉 PowerShell/cmd 粘贴常见外壳：`& 'path'` / `. "path"` / 纯引号。
 * 只在「整段像一条路径命令」时剥；避免误伤普通句子。
 */
export function unwrapShellPathWrapper(text: string): string {
  let t = text.trim()
  // & '…' / & "…" / . '…' / . "…"
  const call = /^[&.]\s*(['"])([\s\S]*)\1\s*$/.exec(t)
  if (call) return call[2]!.trim()
  // 纯引号
  return stripOuterQuotes(t)
}

export function asImageFilePath(text: string): string | null {
  const cleaned = stripBackslashEscapes(unwrapShellPathWrapper(text))
  if (!cleaned || cleaned.includes('\n') || cleaned.includes('\0')) return null
  if (!ABSOLUTE_PREFIX_RE.test(cleaned) || !IMAGE_EXT_RE.test(cleaned)) return null
  return cleaned
}

function preferLongerUnique(paths: string[]): string[] {
  const uniq: string[] = []
  const seen = new Set<string>()
  const sorted = [...paths].sort((a, b) => b.length - a.length)
  for (const p of sorted) {
    if (seen.has(p)) continue
    // 丢弃已被更长候选包含的后缀/子串（防 `048_89.jpg` 叠在完整路径上）
    if (uniq.some(kept => kept.includes(p))) continue
    seen.add(p)
    uniq.push(p)
  }
  return uniq
}

/**
 * 从粘贴/提交文本中抽出可能的图片路径。
 * 不访问磁盘；调用方再用 exists 过滤。
 *
 * 只认绝对路径，避免终端把长路径拆成 `…_2` + `048_89.jpg` 时误把后缀当图。
 * 支持路径内空格（`屏幕截图 2026-07-15 124017.png`）与引号包裹。
 */
export function extractImagePathCandidates(text: string): string[] {
  if (!text) return []
  const found: string[] = []

  ABS_IMAGE_PATH_IN_TEXT_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = ABS_IMAGE_PATH_IN_TEXT_RE.exec(text)) !== null) {
    const raw = m[1] || m[2] || m[3]
    const p = asImageFilePath(raw)
    if (p) found.push(p)
  }

  // 多路径粘贴：按「下一个绝对路径起点」切开（对齐 CC usePasteHandler）
  // 注意：此切分对「路径内空格」不敏感（不在空格处切开路径），只在
  // ` path2` 前有绝对路径前缀时切开。
  const parts = text
    .split(/ (?=\/|[A-Za-z]:[\\/]|\\\\)/)
    .flatMap(part => part.split(/\r?\n/))
    .map(line => line.trim())
    .filter(Boolean)
  for (const part of parts) {
    const p = asImageFilePath(part)
    if (p) found.push(p)
    else {
      // 行内「路径 + 说明文字」
      ABS_IMAGE_PATH_IN_TEXT_RE.lastIndex = 0
      let mm: RegExpExecArray | null
      while ((mm = ABS_IMAGE_PATH_IN_TEXT_RE.exec(part)) !== null) {
        const raw = mm[1] || mm[2] || mm[3]
        const q = asImageFilePath(raw)
        if (q) found.push(q)
      }
    }
  }

  // 整段就是一个绝对路径（可含空格）
  if (found.length === 0) {
    const single = asImageFilePath(text)
    if (single) found.push(single)
  }

  return preferLongerUnique(found)
}

/**
 * 扩展路径匹配区间：吃掉外层引号，以及 Windows/PowerShell 粘贴常见的
 * call operator（`& 'C:\path with space.png'` / `. "C:\a.png"`）。
 *
 * 否则只替换 bare path 会留下 `& '[Image #N]'` 这种半残包装。
 */
export function expandImagePathMatchSpan(
  text: string,
  pathStart: number,
  pathEnd: number,
): { start: number; end: number } {
  let start = pathStart
  let end = pathEnd
  if (start > 0 && end < text.length) {
    const q = text[start - 1]
    if ((q === "'" || q === '"') && text[end] === q) {
      start -= 1
      end += 1
    }
  }
  // 可选空白 + &/. call operator
  let i = start
  while (i > 0 && /[ \t]/.test(text[i - 1]!)) i -= 1
  if (i > 0 && (text[i - 1] === '&' || text[i - 1] === '.')) {
    const opIdx = i - 1
    if (opIdx === 0 || /[\s;(]/.test(text[opIdx - 1]!)) {
      start = opIdx
    }
  }
  return { start, end }
}

/**
 * 将文本中的绝对图片路径替换为 placeholder（最长优先、非重叠、边界安全）。
 * 不会把 `…2048_89.jpg` 里的 `048_89.jpg` 单独替换掉。
 * 会连同 PowerShell `& '…'` / 引号包装一并替换，避免 `& '[Image #N]'` 残留。
 */
export function replaceImagePathsInText(
  text: string,
  replacements: Array<{ path: string; placeholder: string }>,
): string {
  if (!text || replacements.length === 0) return text
  const ordered = [...replacements].sort((a, b) => b.path.length - a.path.length)
  let out = text
  for (const { path, placeholder } of ordered) {
    if (!path || !out.includes(path)) continue
    let next = ''
    let i = 0
    while (i < out.length) {
      const at = out.indexOf(path, i)
      if (at === -1) {
        next += out.slice(i)
        break
      }
      const afterIdx = at + path.length
      const before = at === 0 ? '' : out[at - 1]
      const after = afterIdx >= out.length ? '' : out[afterIdx]
      // 边界：前不能是路径续写字符，后不能继续路径（防子串误替换）
      // 路径本身可含空格，故「后」允许空白/标点/结束；引号留给 expand 吃掉
      const beforeOk = before === '' || /[\s"'([{,;&.]/.test(before)
      const afterOk = after === '' || /[\s"')\]},;]/.test(after)
      if (beforeOk && afterOk) {
        const span = expandImagePathMatchSpan(out, at, afterIdx)
        next += out.slice(i, span.start)
        next += placeholder
        i = span.end
      } else {
        next += out.slice(i, at + 1)
        i = at + 1
      }
    }
    out = next
  }
  return out
}

/**
 * 粘贴分片时：若「光标前文本 + 本次输入」能拼出绝对图片路径，返回该完整路径。
 * 用于修复终端非 bracketed paste 把长路径拆成多段的情况
 * （例如 `…_2` + `048_89.jpg 这图是什么` → 完整 wechat 路径）。
 * start/end 已扩展到 PS `& '…'` / 引号外壳，便于整段替换成 placeholder。
 */
export function completeAbsoluteImagePathAtCursor(
  value: string,
  cursorOffset: number,
  chunk: string,
): { path: string; start: number; end: number } | null {
  if (!chunk) return null
  const offset = Math.max(0, Math.min(cursorOffset, value.length))
  const before = value.slice(0, offset)
  const combined = before + chunk
  const matches = extractImagePathCandidates(combined)
  let best: { path: string; start: number; end: number } | null = null
  for (const p of matches) {
    const pathStart = combined.lastIndexOf(p)
    if (pathStart === -1) continue
    const pathEnd = pathStart + p.length
    // 路径必须跨过光标（说明本次 chunk 在补全路径）
    if (pathEnd <= before.length) continue
    if (pathStart >= offset + chunk.length) continue
    const span = expandImagePathMatchSpan(combined, pathStart, pathEnd)
    if (!best || p.length > best.path.length) {
      best = { path: p, start: span.start, end: span.end }
    }
  }
  return best
}
