/**
 * 从系统剪贴板抓取图片到本地临时 PNG（Windows 优先 PowerShell STA）。
 * 对齐 Codex clipboard_paste / Claude Code imagePaste 的「落盘再传 path」策略。
 *
 * 快捷键注意（见 inputController）：
 * - Windows 上 Ctrl+V 常被终端/系统截获，Claude Code 默认用 Alt+V；
 * - Codex 同时绑 Ctrl+V 与 Ctrl+Alt+V。
 *
 * 尺寸注意：部分 provider（如 Grok）拒绝 <8px 的图。捕获侧会拒绝过小图，
 * 避免自测 1×1 / 空占位图被当成有效截图送进 API。
 */
import { mkdirSync, existsSync, statSync, readFileSync, unlinkSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'

/** 与 image_codec.MIN_DIMENSION / Grok 下限对齐 */
export const MIN_CLIPBOARD_IMAGE_DIMENSION = 8
/** 真实 PNG 截图几乎不会小于该体积；自测 1×1 常约 70–120B */
export const MIN_CLIPBOARD_IMAGE_BYTES = 200

export type ClipboardImageResult =
  | { ok: true; path: string; width?: number; height?: number }
  | { ok: false; error: string }

export function resolveGaImageRoot(): string {
  if (process.env.GA_IMAGE_TEMP) return process.env.GA_IMAGE_TEMP
  // 优先仓库 temp（与 GA 其它运行时产物一致）；不可写时再 fallback
  const repoTemp = join(process.cwd(), 'temp', 'ga-images')
  try {
    mkdirSync(repoTemp, { recursive: true })
    return repoTemp
  } catch {
    return join(tmpdir(), 'ga-images')
  }
}

function gaImageDir(): string {
  const base = resolveGaImageRoot()
  mkdirSync(base, { recursive: true })
  return base
}

function uniquePngPath(): string {
  return join(gaImageDir(), `clipboard-${Date.now()}-${randomBytes(4).toString('hex')}.png`)
}

/** 读 PNG IHDR 宽高；非 PNG 返回 null */
export function readPngDimensions(path: string): { width: number; height: number } | null {
  try {
    const fd = readFileSync(path)
    if (fd.length < 24) return null
    if (fd.subarray(0, 8).toString('binary') !== '\x89PNG\r\n\x1a\n') return null
    const width = fd.readUInt32BE(16)
    const height = fd.readUInt32BE(20)
    if (!width || !height) return null
    return { width, height }
  } catch {
    return null
  }
}

function removeQuiet(path: string): void {
  try {
    if (existsSync(path)) unlinkSync(path)
  } catch {
    /* ignore */
  }
}

/**
 * 校验落盘文件是否像「可用截图」。过小/1×1 直接拒，避免 Grok 400。
 */
export function validateCapturedImageFile(path: string): ClipboardImageResult {
  if (!existsSync(path)) return { ok: false, error: 'clipboard image save produced no file' }
  let size = 0
  try {
    size = statSync(path).size
  } catch {
    return { ok: false, error: 'clipboard image unreadable' }
  }
  if (size <= 0) {
    removeQuiet(path)
    return { ok: false, error: 'clipboard image empty' }
  }
  const dims = readPngDimensions(path)
  if (dims) {
    if (dims.width < MIN_CLIPBOARD_IMAGE_DIMENSION || dims.height < MIN_CLIPBOARD_IMAGE_DIMENSION) {
      removeQuiet(path)
      return {
        ok: false,
        error: `clipboard image too small: ${dims.width}x${dims.height} (min ${MIN_CLIPBOARD_IMAGE_DIMENSION}px)`,
      }
    }
    // 过小体积的「合法」PNG 多半是脏数据/占位，真实截图通常更大
    if (size < MIN_CLIPBOARD_IMAGE_BYTES) {
      removeQuiet(path)
      return {
        ok: false,
        error: `clipboard image too tiny (${size} bytes, ${dims.width}x${dims.height}); copy a real screenshot`,
      }
    }
    return { ok: true, path, width: dims.width, height: dims.height }
  }
  // 非 PNG（少见）：只做体积门槛
  if (size < MIN_CLIPBOARD_IMAGE_BYTES) {
    removeQuiet(path)
    return { ok: false, error: `clipboard image too tiny (${size} bytes)` }
  }
  return { ok: true, path }
}

export function captureClipboardImage(): ClipboardImageResult {
  if (process.platform === 'win32') {
    return captureClipboardImageWindows()
  }
  return captureClipboardImageUnix()
}

/**
 * Windows：优先 Get-Clipboard -Format Image（Codex/CC），Forms 兜底。
 * 必须 -STA；捕获后校验最小边 ≥8 且文件体积合理。
 */
function captureClipboardImageWindows(): ClipboardImageResult {
  const outPath = uniquePngPath()
  const outEsc = outPath.replace(/'/g, "''")
  // Codex WSL fallback 与 CC win32 均用 Get-Clipboard -Format Image；
  // Forms.GetImage 作兜底（部分应用只写 CF_BITMAP）。
  const script = [
    '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8',
    'Add-Type -AssemblyName System.Drawing',
    `$out = '${outEsc}'`,
    '$img = $null',
    'try { $img = Get-Clipboard -Format Image } catch { $img = $null }',
    'if ($null -eq $img) {',
    '  try {',
    '    Add-Type -AssemblyName System.Windows.Forms',
    '    if ([System.Windows.Forms.Clipboard]::ContainsImage()) {',
    '      $img = [System.Windows.Forms.Clipboard]::GetImage()',
    '    }',
    '  } catch { $img = $null }',
    '}',
    'if ($null -eq $img) { exit 2 }',
    'if ($img.Width -lt 8 -or $img.Height -lt 8) {',
    '  $w = $img.Width; $h = $img.Height',
    '  $img.Dispose()',
    '  [Console]::Error.WriteLine(("clipboard image too small: {0}x{1}" -f $w, $h))',
    '  exit 4',
    '}',
    'try {',
    '  $img.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)',
    '  $w = $img.Width; $h = $img.Height',
    '  $img.Dispose()',
    '  Write-Output ("{0}|{1}|{2}" -f $out, $w, $h)',
    '  exit 0',
    '} catch {',
    '  try { $img.Dispose() } catch {}',
    '  [Console]::Error.WriteLine($_.Exception.Message)',
    '  exit 3',
    '}',
  ].join('; ')

  const r = spawnSync(
    'powershell.exe',
    ['-STA', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
    { encoding: 'utf-8', windowsHide: true, timeout: 12000 },
  )
  if (r.error) {
    removeQuiet(outPath)
    return { ok: false, error: r.error.message }
  }
  if (r.status === 0) {
    const line = (r.stdout || '').trim().split(/\r?\n/).filter(Boolean).pop() || ''
    // "path|w|h" 或 仅 path
    const pathPart = line.includes('|') ? line.split('|')[0] : line
    const candidate =
      pathPart && existsSync(pathPart) ? pathPart : existsSync(outPath) ? outPath : ''
    if (!candidate) return { ok: false, error: 'clipboard image save produced no file' }
    return validateCapturedImageFile(candidate)
  }
  if (r.status === 2) {
    removeQuiet(outPath)
    return { ok: false, error: 'no image in clipboard' }
  }
  if (r.status === 4) {
    removeQuiet(outPath)
    const err = (r.stderr || '').toString().trim()
    return { ok: false, error: err || 'clipboard image too small (min 8px)' }
  }
  removeQuiet(outPath)
  const err = (r.stderr || r.stdout || 'powershell clipboard failed').toString().trim()
  return { ok: false, error: err || `powershell clipboard failed (exit ${r.status})` }
}

function captureClipboardImageUnix(): ClipboardImageResult {
  const outPath = uniquePngPath()
  let r = spawnSync('pngpaste', [outPath], { encoding: 'utf-8', timeout: 5000 })
  if (r.status === 0 && existsSync(outPath) && statSync(outPath).size > 0) {
    return validateCapturedImageFile(outPath)
  }
  r = spawnSync(
    'bash',
    [
      '-lc',
      `wl-paste --type image/png > '${outPath}' 2>/dev/null || xclip -selection clipboard -t image/png -o > '${outPath}' 2>/dev/null`,
    ],
    { encoding: 'utf-8', timeout: 5000 },
  )
  if (existsSync(outPath)) {
    try {
      if (statSync(outPath).size > 0) return validateCapturedImageFile(outPath)
    } catch {
      /* ignore */
    }
  }
  removeQuiet(outPath)
  return { ok: false, error: 'no image in clipboard' }
}
